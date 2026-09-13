"""D4: 客户端版本启动时自检。

策略：
  1. 启动时 GET <CHATGPT_BASE_URL>/，从 HTML 中提取 data-build 属性；
  2. 与本地 configs.oai_client_version / oai_client_build_number 比对；
  3. 偏差大（前缀完全不同 或 build 号差距 > 阈值）→ 日志告警，
     提示用户手工同步避免"客户端版本过旧"风控。

不强制更新，仅告警，避免影响主流程。

HTTP 走 ``utils.Client``（curl_cffi）。生产 requirements 没有 httpx——它只在
requirements-dev.txt 里，而本模块由 guard 在启动时调度：用 dev-only 依赖会让生产
启动直接 ModuleNotFoundError。

目标只取显式配置：``CHATGPT_BASE_URL`` 显式为空 = 没有目标，直接跳过，
**不回落**到真实 chatgpt.com。
"""

import asyncio
import re
from typing import Optional, Tuple

from utils import configs
from utils.Client import Client
from utils.Logger import logger

# 启动时使用的最小请求超时（避免阻塞）
_PROBE_TIMEOUT = 5

# build number 偏差阈值：超过则告警
_BUILD_NUMBER_GAP_THRESHOLD = 200_000

# 显式空 base URL：没有探测目标。与 fleet_health.REASON_NO_BASE_URL 是同一个判断，
# 取值沿用本模块的连字符风格（skipped / no-build / in-sync）。
REASON_NO_BASE_URL = "no-base-url"

_HTML_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def _extract_data_build(html: str) -> Optional[str]:
    """从 HTML 中解析 <html data-build="prod-xxxx"> 字符串。"""
    m = re.search(r'<html[^>]*data-build="([^"]+)"', html)
    return m.group(1) if m else None


def _extract_build_number(html: str) -> Optional[int]:
    """从 ChatGPT HTML 中解析 buildNumber（嵌入在 __NEXT_DATA__ 或 script 中）。"""
    # buildNumber 通常出现在 _buildManifest.js 或全局变量中；做宽匹配
    m = re.search(r'"buildNumber"\s*:\s*(\d+)', html)
    return int(m.group(1)) if m else None


def _build_prefix(version: str) -> str:
    """提取 build 字符串的"前缀稳定段"用于粗比对。

    例: "prod-f501fe933b3edf57aea882da888e1a544df99840" → "prod"
    """
    if not version:
        return ""
    if "-" in version:
        return version.split("-", 1)[0]
    return version[:8]


def _probe_target() -> str:
    """显式配置的上游；没有配置就返回空串（不回落到任何真实站点）。"""
    configured = configs.chatgpt_base_url_list
    candidates = configured if isinstance(configured, list) else [configured]
    for candidate in candidates:
        target = str(candidate or "").strip().rstrip("/")
        if target:
            return target
    return ""


async def _fetch_html(target: str) -> Tuple[Optional[str], str]:
    """取回目标首页 HTML。返回 ``(html, reason)``：reason 非空表示跳过。

    取消向上传播（启动任务被取消时不能吞掉），但连接不泄漏：响应到手 → close()
    归还池子；异常/取消 → discard() 硬关闭。
    """
    headers = dict(_HTML_HEADERS)
    headers["accept-language"] = configs.accept_language or "en-US,en;q=0.9"

    client = Client(timeout=_PROBE_TIMEOUT)
    released = False
    try:
        resp = await client.get(target + "/", headers=headers, timeout=_PROBE_TIMEOUT,
                                allow_redirects=False)
        await client.close()
        released = True
        status_code, body = resp.status_code, resp.text
    except asyncio.CancelledError:
        await client.discard()
        released = True
        raise
    except Exception as e:
        # 只记异常类别：异常原文会带上游地址与本机端口。
        await client.discard()
        released = True
        logger.info(f"[antiban] version_check skipped (network: {type(e).__name__})")
        return None, "skipped"
    finally:
        if not released:  # pragma: no cover - 防御：上面每条路径都已归还
            await client.discard()

    if status_code >= 400:
        logger.info(f"[antiban] version_check skipped (status {status_code})")
        return None, "skipped"
    return body, ""


async def probe_and_compare() -> Tuple[bool, str]:
    """探测官网 data-build，与本地配置比对。

    返回 (is_drift, message)：
      is_drift=True 表示偏差大，已发告警；False 表示同步或无法探测（跳过告警）。
    """
    local_version = configs.oai_client_version or ""
    local_build_num = configs.oai_client_build_number

    target = _probe_target()
    if not target:
        # 显式空 base URL：没有目标，也就不构造客户端、不发请求。
        logger.info("[antiban] version_check skipped (no CHATGPT_BASE_URL configured)")
        return False, REASON_NO_BASE_URL

    html, reason = await _fetch_html(target)
    if reason:
        return False, reason

    remote_build = _extract_data_build(html)
    remote_build_num = _extract_build_number(html)

    if not remote_build:
        logger.info("[antiban] version_check: data-build not found in HTML; sentinel may have changed")
        return False, "no-build"

    # 比对 1: 前缀（prod- / dev- 等）
    if _build_prefix(local_version) != _build_prefix(remote_build):
        msg = (
            f"oai-client-version prefix mismatch: local={local_version[:30]}... "
            f"remote={remote_build[:30]}..."
        )
        logger.warning(f"[antiban] version_check DRIFT: {msg}")
        return True, msg

    # 比对 2: build number 偏差
    if remote_build_num and local_build_num:
        try:
            gap = abs(int(remote_build_num) - int(local_build_num))
            if gap > _BUILD_NUMBER_GAP_THRESHOLD:
                msg = (
                    f"oai-client-build-number gap={gap} exceeds "
                    f"threshold={_BUILD_NUMBER_GAP_THRESHOLD}; consider updating configs.py "
                    f"(local={local_build_num} remote={remote_build_num})"
                )
                logger.warning(f"[antiban] version_check DRIFT: {msg}")
                return True, msg
        except (TypeError, ValueError):
            pass

    logger.info(
        f"[antiban] version_check OK: local={_build_prefix(local_version)}... "
        f"remote build_num={remote_build_num}"
    )
    return False, "in-sync"
