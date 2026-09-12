import json
import random
import time
import uuid

import ua_generator
from ua_generator.data.version import VersionRange
from ua_generator.options import Options

import utils.globals as globals
from utils import configs
from utils.Logger import logger
from utils.proxy_health import weighted_choice
from utils.routing import get_bound_proxy

MAX_SUPPORTED_CHROME_MAJOR = 124
MIN_SUPPORTED_CHROME_MAJOR = 119

# ---------------------------------------------------------------------------
# Outbound HTTP header boundary
# ---------------------------------------------------------------------------
# 一个 fp_map 条目是单一扁平命名空间，却服务于需求相反的两类消费者：
#   1) transport：稳定的标量，作为 HTTP 头离开本进程（下表 allowlist）；
#   2) antiban / 浏览器画像：结构化元数据（screen/viewport/webgl/... 由
#      utils.antiban.fingerprint.ensure_extended 写入），以及路由元数据
#      （group/proxy_name/updated_at 由 utils.routing 写入）。
# 只允许 allowlist 里的字段变成 HTTP 头。这里必须是白名单而不是黑名单：黑名单
# 会腐烂——每新增一个画像字段都得靠每个调用点记得排除它，而漏排的代价是
# curl_cffi 编码 dict 头抛 AttributeError，用户侧看到 502（见 reverseProxy）。
FP_HEADER_FIELDS = frozenset({
    "user-agent",
    "oai-device-id",
    "oai-session-id",
    "sec-ch-ua",
    "sec-ch-ua-arch",
    "sec-ch-ua-bitness",
    "sec-ch-ua-form-factors",
    "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-mobile",
    "sec-ch-ua-model",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
})


def extract_header_fp(fp):
    """取出 fp 记录中可出网的 HTTP 头字段，返回新的 {小写名: str} 字典。

    只有 str 原样透传；int/float 转字符串（float 必须转：curl_cffi 对 float 头值会抛
    AttributeError，int 才能通过）；其余类型（dict/list/None/bool/其他对象）一律丢弃
    并告警——JSON 序列化它们会向上游发出一个格式合法但语义错误的头，而这正是修复前
    502 的成因。bool 也在丢弃之列：记录里没有任何布尔头字段，而布尔的文本形式是逐字段
    约定的（sec-ch-ua-mobile 用 "?1"/"?0"，libcurl 发出来是 "1"/"0"），猜错就是又一
    个"合法但错误"的头。画像字段不在白名单里，走不到这里就已经被排除了。
    """
    headers = {}
    for key, value in (fp or {}).items():
        name = str(key).lower()
        if name not in FP_HEADER_FIELDS:
            continue
        if isinstance(value, str):
            headers[name] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            headers[name] = str(value)
        else:
            logger.warning(
                f"[fp] dropped unsupported value for header field {name!r} "
                f"({type(value).__name__})"
            )
    return headers


# Static-asset fingerprint cache: CDN 静态资源请求 req_token=""，get_fp 每次走全新生成分支，
# 随机 UA/impersonate/代理 → 连接池 key 每次不同 → 每个 JS/CSS 子资源一次完整 socks5h 握手。
# 缓存一份稳定画像复用，让同一页面几十个子资源共享同一条 keep-alive 连接。
# 带 TTL 到期重建：节点死亡后最多 _STATIC_FP_TTL 内通过 weighted_choice 重选到健康节点。
_static_fp_cache = None
_static_fp_cache_at = 0.0
_STATIC_FP_TTL = 3600.0  # 1h


def _stringify_ch_value(value):
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "?1" if value else "?0"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _infer_arch(platform_str):
    """根据 ch.platform 推断 sec-ch-ua-arch（Chromium 不暴露 arch 库字段，需手动推断）。

    主流分布：macOS Apple Silicon (arm) 占 70%、Intel x86 30%；
    Windows 几乎全是 x86_64；Linux x86_64 居多。
    """
    p = (platform_str or "").strip('"').lower()
    if p == "macos":
        # 与真实分布对齐：70% arm
        return '"arm"' if random.random() < 0.7 else '"x86"'
    if p in ("windows", "linux", "chromeos"):
        return '"x86"'
    return '"x86"'


def _infer_form_factors(device_str):
    """根据 ua_generator 返回的 device 类型推断 sec-ch-ua-form-factors 值。

    Chrome 124+ 在 same-origin 请求中默认携带，未携带会被识别为非主流客户端。
    返回 CH 字符串形式（双引号包裹的列表字面量）。
    """
    d = (device_str or "").lower()
    if d == "mobile":
        return '"Mobile"'
    if d == "tablet":
        return '"Tablet"'
    # 桌面：极少数 2-in-1 设备会上报 "Desktop", "Tablet"，但绝大多数仅 "Desktop"
    return '"Desktop"'


def _extract_full_version(ua_text, brands_full):
    """从 UA 或 brands_full_version_list 中提取 Chrome 完整版本号，如 "146.0.7680.121"。"""
    try:
        if "Chrome/" in ua_text:
            ver = ua_text.split("Chrome/")[1].split(" ")[0]
            if ver and ver != "0.0.0.0":
                return f'"{ver}"'
    except Exception:
        pass
    try:
        if brands_full:
            # 形如 '"Chromium";v="146.0.7680.121", "Google Chrome";v="146.0.7680.121", ...'
            import re as _re
            m = _re.search(r'"Google Chrome";v="([0-9.]+)"', brands_full)
            if m:
                return f'"{m.group(1)}"'
            m = _re.search(r'"Chromium";v="([0-9.]+)"', brands_full)
            if m:
                return f'"{m.group(1)}"'
    except Exception:
        pass
    return None


def _extract_chrome_major(ua_text):
    """从 UA 中解析 Chrome 主版本号；解析失败返回 None。"""
    ua = (ua_text or "").lower()
    try:
        if "edg/" in ua:
            return int(ua.split("edg/")[1].split(".")[0])
        if "chrome/" in ua:
            return int(ua.split("chrome/")[1].split(".")[0])
    except (IndexError, ValueError):
        return None
    return None


def _clamp_ua_to_supported(ua_text):
    """若 UA Chrome 主版本超过 MAX_SUPPORTED_CHROME_MAJOR，按上限重写 UA。

    避免 UA=Chrome 130 但 curl_cffi TLS=chrome124 的 6 版本错配。
    返回 (clamped_ua, was_clamped)。
    """
    if not ua_text:
        return ua_text, False
    major = _extract_chrome_major(ua_text)
    if major is None or major <= MAX_SUPPORTED_CHROME_MAJOR:
        return ua_text, False
    import re as _re
    # 替换 Chrome/X.Y.Z.W 与可能存在的 Edg/X.Y.Z.W 主版本号
    replaced = _re.sub(r"(Chrome/)(\d+)", lambda m: m.group(1) + str(MAX_SUPPORTED_CHROME_MAJOR), ua_text)
    replaced = _re.sub(r"(Edg/)(\d+)", lambda m: m.group(1) + str(MAX_SUPPORTED_CHROME_MAJOR), replaced)
    return replaced, True


def select_impersonate(user_agent):
    """根据 UA 选择最接近的 curl_cffi 浏览器指纹（TLS/JA3 + HTTP2 帧）。

    与真实 Chrome 主版本对齐能显著降低风控评分；偏离过远（如 UA=Chrome147 但 TLS=chrome119）
    会被识别为自动化客户端。当前镜像固定 curl_cffi==0.7.3，最高安全使用 chrome124。
    """
    ua = (user_agent or "").lower()
    def by_major(ver):
        if ver >= 124:
            return "chrome124"
        if ver >= 123:
            return "chrome123"
        if ver >= 120:
            return "chrome120"
        return "chrome119"

    # Edge 与 Chrome 共用 Chromium 内核，按主版本选择 curl_cffi 支持的最近指纹。
    if "edg/" in ua:
        try:
            ver = int(ua.split("edg/")[1].split(".")[0])
        except (IndexError, ValueError):
            return "chrome124"
        return by_major(ver)
    if "chrome/" in ua or "chromium/" in ua:
        # 解析 Chrome 主版本号
        try:
            ver = int(ua.split("chrome/")[1].split(".")[0])
        except (IndexError, ValueError):
            return "chrome124"
        return by_major(ver)
    return "chrome124"


def get_fp(req_token):
    fp = globals.fp_map.get(req_token, {})
    bound_proxy = get_bound_proxy(req_token)
    if fp and fp.get("user-agent") and fp.get("impersonate"):
        if bound_proxy:
            fp["proxy_url"] = bound_proxy
            globals.fp_map[req_token] = fp
            globals.persist_fp_token(req_token)
        elif "proxy_url" in fp.keys() and (fp["proxy_url"] is None or fp["proxy_url"] not in configs.proxy_url_list):
            fp["proxy_url"] = weighted_choice(configs.proxy_url_list) if configs.proxy_url_list else None
            globals.fp_map[req_token] = fp
            globals.persist_fp_token(req_token)
        if "user-agent" in fp.keys():
            fp["impersonate"] = select_impersonate(fp["user-agent"])
            globals.fp_map[req_token] = fp
            globals.persist_fp_token(req_token)
        elif globals.impersonate_list and "impersonate" in fp.keys() and fp["impersonate"] not in globals.impersonate_list:
            fp["impersonate"] = globals.impersonate_list[-1]
            globals.fp_map[req_token] = fp
            globals.persist_fp_token(req_token)
        # 老 fp 迁移：补齐 oai-session-id 与高熵 sec-ch-ua-* 头（旧账号也能享受加固）
        _migrated = False
        if "oai-session-id" not in fp:
            fp["oai-session-id"] = str(uuid.uuid4())
            _migrated = True
        if "sec-ch-ua-platform" in fp and "sec-ch-ua-arch" not in fp:
            fp["sec-ch-ua-arch"] = _infer_arch(fp.get("sec-ch-ua-platform"))
            _migrated = True
        # 老 fp 缺 form-factors → 桌面默认 "Desktop"
        if "sec-ch-ua-platform" in fp and "sec-ch-ua-form-factors" not in fp:
            fp["sec-ch-ua-form-factors"] = '"Desktop"'
            _migrated = True
        # 老 fp 缺少完整高熵 CH 头时，仅打提示日志（不强制重生，避免破坏 strict_ip_binding 画像）
        # 用户可手动删除 data/fp_map.json 让新账号走完整生成流程
        if "sec-ch-ua-platform" in fp and "sec-ch-ua-full-version-list" not in fp:
            # 至少补 full-version（基于 UA 解析出 Chrome 版本号）
            full_ver = _extract_full_version(fp.get("user-agent", ""), None)
            if full_ver and "sec-ch-ua-full-version" not in fp:
                fp["sec-ch-ua-full-version"] = full_ver
                _migrated = True
        if _migrated:
            globals.fp_map[req_token] = fp
            globals.persist_fp_token(req_token)
        # 严格指纹绑定：开启后绝不因 user_agents_list 变化而漂移 UA，保留历史画像
        if (not (configs.enable_antiban and configs.strict_ip_binding)
                and configs.user_agents_list
                and "user-agent" in fp.keys()
                and fp["user-agent"] not in configs.user_agents_list):
            picked_ua = random.choice(configs.user_agents_list)
            # H4: 用户配置的 UA 若主版本号超过 curl_cffi 支持上限，自动降级避免 TLS/UA 错配
            picked_ua, clamped = _clamp_ua_to_supported(picked_ua)
            if clamped:
                logger.warning(
                    f"[fp] UA Chrome major > {MAX_SUPPORTED_CHROME_MAJOR}, "
                    f"clamped to avoid TLS mismatch"
                )
            fp["user-agent"] = picked_ua
            fp["impersonate"] = select_impersonate(picked_ua)
            globals.fp_map[req_token] = fp
            globals.persist_fp_token(req_token)
        fp = {k.lower(): v for k, v in fp.items()}
        return fp
    else:
        options = Options(version_ranges={
            'chrome': VersionRange(min_version=MIN_SUPPORTED_CHROME_MAJOR, max_version=MAX_SUPPORTED_CHROME_MAJOR),
            'edge': VersionRange(min_version=MIN_SUPPORTED_CHROME_MAJOR, max_version=MAX_SUPPORTED_CHROME_MAJOR),
        })
        ua = ua_generator.generate(
            device=configs.device_tuple if configs.device_tuple else ('desktop'),
            browser=configs.browser_tuple if configs.browser_tuple else ('chrome', 'edge'),
            platform=configs.platform_tuple if configs.platform_tuple else ('windows', 'macos'),
            options=options
        )
        user_agent = ua.text if not configs.user_agents_list else random.choice(configs.user_agents_list)
        fp = {
            "user-agent": user_agent,
            "impersonate": select_impersonate(user_agent),
            "proxy_url": bound_proxy or (weighted_choice(configs.proxy_url_list) if configs.proxy_url_list else None),
            "oai-device-id": str(uuid.uuid4()),
            # 浏览器 tab/session 级稳定标识（真实浏览器同一 tab 内不变）
            "oai-session-id": str(uuid.uuid4()),
        }
        if ua.device == "desktop" and ua.browser in ("chrome", "edge"):
            # 标准 3 个低熵 CH 头
            fp["sec-ch-ua-platform"] = _stringify_ch_value(ua.ch.platform)
            fp["sec-ch-ua"] = _stringify_ch_value(ua.ch.brands)
            fp["sec-ch-ua-mobile"] = _stringify_ch_value(ua.ch.mobile)
            # 高熵 CH 头（Chromium 124+ 默认在 same-origin 请求中携带）
            brands_full = getattr(ua.ch, "brands_full_version_list", None)
            if brands_full:
                fp["sec-ch-ua-full-version-list"] = _stringify_ch_value(brands_full)
            full_ver = _extract_full_version(user_agent, brands_full)
            if full_ver:
                fp["sec-ch-ua-full-version"] = full_ver
            bitness = getattr(ua.ch, "bitness", None)
            if bitness:
                fp["sec-ch-ua-bitness"] = _stringify_ch_value(bitness)
            model = getattr(ua.ch, "model", None)
            if model is not None:
                fp["sec-ch-ua-model"] = _stringify_ch_value(model)
            platform_version = getattr(ua.ch, "platform_version", None)
            if platform_version:
                fp["sec-ch-ua-platform-version"] = _stringify_ch_value(platform_version)
            # arch 需手动推断（ua_generator 不直接提供）
            fp["sec-ch-ua-arch"] = _infer_arch(fp.get("sec-ch-ua-platform"))
            # D1: sec-ch-ua-form-factors（Chrome 124+ 默认携带；桌面恒为 "Desktop"）
            # 真实浏览器返回数组形式："Desktop"，移动端 "Mobile"，二合一 "Desktop", "Tablet"
            fp["sec-ch-ua-form-factors"] = _infer_form_factors(ua.device)

        if not req_token:
            global _static_fp_cache, _static_fp_cache_at
            now = time.time()
            if _static_fp_cache is None or now - _static_fp_cache_at > _STATIC_FP_TTL:
                _static_fp_cache = fp
                _static_fp_cache_at = now
            return dict(_static_fp_cache)
        else:
            globals.fp_map[req_token] = fp
            globals.persist_fp_token(req_token)
            return fp
