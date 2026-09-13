"""熔断与黑名单自愈。

错误分级（每一类都映射到一个**枚举**，日志与指标只出现枚举，不出现上游原文）：
  403 + cf_chl_opt   → bucket 降级 CIRCUIT_403_COOLDOWN；桶内账号一并延长冷却
  403 + 挑战族       → PoW / Turnstile / Arkose 各自归类，账号退避（上游确证的挑战）
  429 rate-limit     → 账号指数退避冷却（1800→3600→7200s 封顶）
  401 invalid_grant  → 加入 error_token_list，等 refreshToken 恢复
  account_deactivated→ 永久黑名单 antiban_dead.json
  account unavailable→ 账号退避（临时不可用，不等于封号）
  5xx                → 轻度退避
  200 成功           → 重置账号退避等级

死号复活不是本模块的自由行为：revive_token 要求**刚发生的认证探针证据**
（store 里新鲜的 healthy 判定），探针成功的持续时长（dwell）由号池探活负责。
定时自愈只恢复桶，不复活死号。

脱敏约束：detail 可能是上游响应正文（含会话内容、账号信息）。它只用于**分类**，
分类结果是枚举；原文不进日志、不进持久化的 dead 记录、不进指标。
"""

import json
import math
import threading
import time
from typing import Dict, List, Optional

import utils.globals as globals
from utils import configs
from utils.antiban import bucket as _bucket
from utils.antiban import cooldown
from utils.antiban.concurrency import anon_id
from utils.Logger import logger

_write_lock = threading.Lock()

_account_backoff_level = {}  # token -> 0..N
_bucket_network_errors = {}  # bucket_id -> 连续网络层错误计数（连接超时/拒绝/DNS）

# 同一桶连续网络层错误阈值；达到后降级 300s（代理本身可能挂了）
_NETWORK_ERROR_THRESHOLD = 3
_NETWORK_ERROR_COOLDOWN = 300

# 错误分类枚举。UI/运维按此区分业务状态，因此必须保留；上游原文一律丢弃。
REASON_CF_CHALLENGE = "cf_challenge"
REASON_POW = "pow_challenge"
REASON_TURNSTILE = "turnstile_challenge"
REASON_ARKOSE = "arkose_challenge"
REASON_RATE_LIMIT = "rate_limit"
REASON_AUTH_INVALID = "auth_invalid"
REASON_ACCOUNT_DEAD = "account_deactivated"
REASON_ACCOUNT_UNAVAILABLE = "account_unavailable"
REASON_UPSTREAM_5XX = "upstream_5xx"
REASON_UNCLASSIFIED = "unclassified"
REASON_NETWORK = "network_error"

# 桶降级事件的指标前缀：`_count` 的键空间已经有 reason[:status]，这里再加一个
# 有界的事件族，好让「熔断真的动作了」可以在指标里查到（只有日志等于查不到）。
EVENT_BUCKET_DEGRADED = "bucket_degraded"

# 挑战族：上游明确告知「你得先过这一关」。它们的降载动作一致（账号退避），
# 但**必须分开计数**：PoW 失败、Turnstile 要求、Arkose 要求对应不同的运维动作，
# 合并成一个 unclassified 就只剩「上游拒绝了」这一条信息。
_CHALLENGE_REASONS = frozenset({REASON_POW, REASON_TURNSTILE, REASON_ARKOSE})

# 上游确证、但不足以判死的拒绝 → 账号退避（不降级整桶：这些是账号/会话级信号）。
_COOLDOWN_REASONS = _CHALLENGE_REASONS | frozenset({REASON_ACCOUNT_UNAVAILABLE})

# 允许写入持久化 dead 记录与日志的死号原因。未知一律归一到 unclassified，
# 防止调用方把上游响应片段当作 reason 传进来并落盘。
DEAD_REASONS = frozenset({
    REASON_ACCOUNT_DEAD,
    "banned",
    "deactivated",
    "degraded_quality",
    REASON_AUTH_INVALID,
    "manual",
    REASON_UNCLASSIFIED,
})

# 网络层错误的类别枚举。调用方传的是 `type(e).__name__`——那是**任意文本**：
#   * 直接进指标键 = 高基数炸弹（每请求一个新类名就能撑爆指标后端）；
#   * 直接进日志 = 异常类名可被拼接成带账号/上游片段的内容。
# 因此只认已知类别，其余一律入 other，基数被这个集合的大小硬性封顶。
KIND_TIMEOUT = "timeout"
KIND_CONNECT = "connect"
KIND_RESET = "reset"
KIND_DNS = "dns"
KIND_SSL = "ssl"
KIND_PROXY = "proxy"
KIND_PROTOCOL = "protocol"
KIND_OTHER = "other"

NETWORK_KINDS = frozenset({
    KIND_TIMEOUT, KIND_CONNECT, KIND_RESET, KIND_DNS,
    KIND_SSL, KIND_PROXY, KIND_PROTOCOL, KIND_OTHER,
})

_NETWORK_KIND_MAP = {
    "timeouterror": KIND_TIMEOUT,
    "connecttimeout": KIND_TIMEOUT,
    "connecttimeouterror": KIND_TIMEOUT,
    "readtimeout": KIND_TIMEOUT,
    "readtimeouterror": KIND_TIMEOUT,
    "asyncio.timeouterror": KIND_TIMEOUT,
    "connectionerror": KIND_CONNECT,
    "connectionrefusederror": KIND_CONNECT,
    "connectionabortederror": KIND_CONNECT,
    "connectionreseterror": KIND_RESET,
    "brokenpipeerror": KIND_RESET,
    "remotedisconnected": KIND_RESET,
    "gaierror": KIND_DNS,
    "socket.gaierror": KIND_DNS,
    "nodedisconnected": KIND_DNS,
    "sslerror": KIND_SSL,
    "sslcertverificationerror": KIND_SSL,
    "certificateverifyerror": KIND_SSL,
    "proxyerror": KIND_PROXY,
    "sockserror": KIND_PROXY,
    "protocolerror": KIND_PROTOCOL,
    "curl_error": KIND_PROTOCOL,
    "chunkedencodingerror": KIND_PROTOCOL,
    "incompleteread": KIND_PROTOCOL,
}

# 错误的处理策略表。指标之外还要能回答「这一类错误我们会怎么处理」——
# 否则运维只能回去读源码。signal 是识别依据，action 是触发的降载动作。
_ERROR_CLASSES: List[Dict[str, str]] = [
    {"reason": REASON_CF_CHALLENGE, "signal": "403 + cf_chl_opt", "action": "degrade_bucket"},
    {"reason": REASON_POW, "signal": "403 + proof of work", "action": "extend_cooldown"},
    {"reason": REASON_TURNSTILE, "signal": "403 + turnstile required", "action": "extend_cooldown"},
    {"reason": REASON_ARKOSE, "signal": "403 + arkose required", "action": "extend_cooldown"},
    {"reason": REASON_RATE_LIMIT, "signal": "429 / rate-limit", "action": "extend_cooldown"},
    {"reason": REASON_AUTH_INVALID, "signal": "401 + invalid_grant", "action": "error_token_list"},
    {"reason": REASON_ACCOUNT_DEAD, "signal": "account_deactivated / banned", "action": "mark_dead"},
    {"reason": REASON_ACCOUNT_UNAVAILABLE, "signal": "account unavailable", "action": "extend_cooldown"},
    {"reason": REASON_UPSTREAM_5XX, "signal": "5xx", "action": "extend_cooldown"},
    {"reason": REASON_NETWORK, "signal": "transport failure", "action": "degrade_bucket"},
    {"reason": REASON_UNCLASSIFIED, "signal": "anything else", "action": "count_only"},
]

# 分类依据：上游**确证**的标记 → (枚举, 是否要求 403)。顺序即优先级：越具体的越先判。
# 三档可信度：
#   机器令牌（cf_chl_opt / ark0se / proofofwork / account_deactivated ...）：
#     响应正文里出现即成立，与状态码无关——自然语言里不会出现这些串。
#     这类标记**不能**要求 403：挑战页也可能是 503，只认 403 就会把它降级成
#     upstream_5xx（轻度退避），恰好漏掉最该做的整桶降级。
#   自然语言（turnstile / arkose / proof of work）：只有 403 才算挑战，其它状态码
#     的正文里可能出现同一个词（429 的正文提到 turnstile 仍然是频控）。
#   账号状态（account_deactivated / account unavailable）：与状态码无关。
# 约束：detail 只用于匹配，匹配结果才是枚举——原文一律不进日志/指标/落盘。
_CLASSIFY_MARKERS: List[tuple] = [
    ("cf_chl_opt", REASON_CF_CHALLENGE, False),
    ("cf-chl", REASON_CF_CHALLENGE, False),
    ("ark0se", REASON_ARKOSE, False),
    ("proofofwork", REASON_POW, False),
    ("turnstile", REASON_TURNSTILE, True),
    ("arkose", REASON_ARKOSE, True),
    ("proof of work", REASON_POW, True),
    ("account_deactivated", REASON_ACCOUNT_DEAD, False),
    ("account unavailable", REASON_ACCOUNT_UNAVAILABLE, False),
    ("account_unavailable", REASON_ACCOUNT_UNAVAILABLE, False),
    ("account is unavailable", REASON_ACCOUNT_UNAVAILABLE, False),
    ("account not available", REASON_ACCOUNT_UNAVAILABLE, False),
]


def known_error_classes() -> List[Dict[str, str]]:
    """已识别的错误类别 → 处理动作。供快照/运维面板查询（匿名，不含账号标识）。"""
    return [dict(entry) for entry in _ERROR_CLASSES]


def normalize_network_kind(error_kind) -> str:
    """把任意 error_kind 归一到 NETWORK_KINDS 中的一个。

    只通过已知类别映射，未注册的值一律 other——**绝不**把入参截断后当标签用。
    """
    key = str(error_kind or "").strip().lower()
    if not key:
        return KIND_OTHER
    kind = _NETWORK_KIND_MAP.get(key)
    if kind:
        return kind
    # 允许 "TimeoutError" / "timeout" / "asyncio.TimeoutError" 这类同义写法
    for suffix in NETWORK_KINDS:
        if key == suffix or key.endswith("." + suffix) or key.endswith(suffix + "error"):
            return suffix
    return KIND_OTHER


# transport markers in an exception's *text* -> enum. curl_cffi puts the curl
# error code in the message rather than the class name
# (RequestsError("Failed to perform, curl: (35) SSL_ERROR_SYSCALL...")), so
# class-name-only detection would file the most common proxy failure as other.
# The vocabulary matches the gateway's retry predicate: "retried" and "counted
# as network" must not drift apart.
_NETWORK_TEXT_MARKERS = (
    ("ssl_error_syscall", KIND_SSL),
    ("ssl_connect", KIND_SSL),
    ("sslerror", KIND_SSL),
    ("ssl routines", KIND_SSL),
    ("connection reset", KIND_RESET),
    ("connection aborted", KIND_CONNECT),
    ("connection refused", KIND_CONNECT),
    ("connection closed", KIND_RESET),
    ("remote end closed", KIND_RESET),
    ("broken pipe", KIND_RESET),
    ("timed out", KIND_TIMEOUT),
    ("timeout", KIND_TIMEOUT),
    ("recv failure", KIND_PROTOCOL),
    ("send failure", KIND_PROTOCOL),
    ("network is unreachable", KIND_CONNECT),
    ("proxyerror", KIND_PROXY),
    ("proxy error", KIND_PROXY),
    ("socks5", KIND_PROXY),
    ("eof occurred", KIND_PROTOCOL),
)

# Upper bound on walking an exception chain/group: a cycle or a pathologically
# deep chain must still terminate.
_CHAIN_LIMIT = 12


def iter_exception_chain(exc, limit: int = _CHAIN_LIMIT):
    """Bounded walk of exception groups and __cause__/__context__ (outer first).

    Yields exception objects only. No caller may treat an exception's str() as
    loggable content: the text can carry upstream body fragments or a proxy
    credential.
    """
    pending = [exc]
    seen = set()
    while pending and len(seen) < limit:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.append(current.__cause__)
        pending.append(current.__context__)


def network_kind_from_exception(exc) -> Optional[str]:
    """Normalise an exception (group) to one of NETWORK_KINDS; None if it is not
    a transport failure.

    Order: class name (via normalize_network_kind), then fixed text markers.
    Class names go through the existing map, so standard exceptions such as
    ReadTimeout / ConnectionResetError / gaierror need no extra registration.
    None means "no network-layer evidence"; callers must not invent an enum.
    """
    if exc is None:
        return None
    for current in iter_exception_chain(exc):
        kind = normalize_network_kind(type(current).__name__)
        if kind != KIND_OTHER:
            return kind
        text = str(current).lower()
        if not text:
            continue
        for marker, marker_kind in _NETWORK_TEXT_MARKERS:
            if marker in text:
                return marker_kind
    return None


# 匿名指标：(reason, status) → 次数。不含任何账号标识。
_error_events: Dict[str, int] = {}


def _count(reason: str, status_code: Optional[int] = None) -> None:
    key = f"{reason}:{status_code}" if status_code is not None else reason
    _error_events[key] = _error_events.get(key, 0) + 1


def get_circuit_stats() -> Dict[str, int]:
    """按 reason:status 的匿名计数。"""
    return dict(_error_events)


def reset_circuit_stats() -> None:
    _error_events.clear()


def _normalize_dead_reason(reason: str) -> str:
    """把任意入参归一到枚举，杜绝上游文本落盘/落日志。"""
    r = (reason or "").strip().lower()
    return r if r in DEAD_REASONS else REASON_UNCLASSIFIED


def _persist_dead() -> None:
    with _write_lock:
        with open(globals.ANTIBAN_DEAD_FILE, "w", encoding="utf-8") as f:
            json.dump(globals.antiban_dead_tokens, f, indent=2, ensure_ascii=False)


def bucket_ineligible_reason(bucket_id: Optional[str]) -> Optional[str]:
    """桶此刻是否接收新流量。不合格时返回原因枚举，合格返回 None。

    语义（bucket.py 定义状态，这里只做资格判定）：
      healthy   → 放行；
      degraded  → 降级窗口内拒绝。窗口时间读不到/畸形 → **fail closed**：
                  读不到恢复时间不等于已经恢复；
      dead      → 拒绝。代理已从 routing 移除，桶内账号的出口不再存在；
      未识别状态 → 拒绝。bucket.py 新增状态时必须显式登记，不能静默放行；
      **完全没有这条桶记录** → 放行，但计入 bucket_unknown 异常计数。

    最后一条是刻意的取舍，不是遗漏：bucket.py 在代理从 routing 移除时是**保留**
    bucket 记录并改标 dead 的，所以「有桶 id 却查不到记录」只可能来自状态文件损坏
    或部分写入。在那种情况下全局拒绝会把一次簿记故障放大成整个号池 503；因此这里
    放行并把它变成可查询的异常计数，而不是静默放行。这个取舍属于部署策略，
    是否要改成硬拒绝需要 configs/app 层开关（已作为接口建议提交主控）。

    antiban 关闭时一律放行：关闭开关必须保持既有行为，不能因为残留状态开始拦截。
    """
    if not configs.enable_antiban or not bucket_id:
        return None

    meta = _bucket.get_bucket_meta(bucket_id) or {}
    status = meta.get("status")

    if status == "healthy":
        return None
    if status == "dead":
        return "bucket_dead"
    if status is None:
        # 元数据整条缺失：记异常计数，但不制造全局停摆
        _count("bucket_unknown")
        logger.warning("[antiban] bucket metadata missing; traffic not blocked (see bucket_unknown)")
        return None
    if status == "degraded":
        until = meta.get("degraded_until")
        if isinstance(until, bool) or not isinstance(until, (int, float)):
            return "bucket_degraded"
        if not math.isfinite(until):
            return "bucket_degraded"
        return "bucket_degraded" if until > time.time() else None
    # 记录了但状态不认识：显式拒绝，等 bucket.py 登记
    return "bucket_degraded"


def is_bucket_allowed(bucket_id: Optional[str]) -> bool:
    if not bucket_id:
        return True
    return bucket_ineligible_reason(bucket_id) is None


def bucket_denial_reason(bucket_id: Optional[str]) -> str:
    """拒绝原因标签。与判定分开：判定 fail closed，标签默认 degraded（可重试）。

    查不到桶元数据时也返回 bucket_degraded——调用方 monkeypatch 了
    is_bucket_allowed 的场景下没有真实桶，仍要给出一个可重试的 503 语义。
    """
    return bucket_ineligible_reason(bucket_id) or "bucket_degraded"


def is_token_dead(token: str) -> bool:
    return token in globals.antiban_dead_tokens


def mark_dead(token: str, reason: str = "") -> None:
    if not token:
        return
    reason = _normalize_dead_reason(reason)
    globals.antiban_dead_tokens[token] = {
        "reason": reason,
        "dead_at": int(time.time()),
    }
    try:
        from utils import store
        store.set_account_status(token, "dead")
    except Exception as e:  # the in-memory gate still fails closed
        logger.error(f"[antiban] failed to persist dead status: {type(e).__name__}")
    try:
        _persist_dead()
    except Exception as e:  # pragma: no cover
        logger.error(f"[antiban] failed to persist dead token: {type(e).__name__}")
    _count(f"mark_dead.{reason}")
    logger.error(f"[antiban] {anon_id(token)} marked dead: reason={reason}")


# 复活证据的新鲜度上限：store 里那条 healthy 判定必须来自**刚发生**的探针。
# 无限期的 healthy 行只证明「历史上健康过」，不构成放回流量的证据。
#
# 为什么这个上界不会把恢复卡死：号池探活是在**同一轮扫描里**先条件写入 healthy
# （store.apply_health_probe 写 status 与 last_health_check），紧接着调用本函数。
# 所以「能触发复活的那次写入」与复活之间没有 dwell 间隔——dwell 是探针在写入
# healthy 之前自己等满的。这个上界只会拒绝「与本轮无关的旧判定」。
_REVIVE_EVIDENCE_MAX_AGE_SECONDS = 300
# 允许的时钟偏移：判定时间戳落在未来超过这个量即视为不可信（否则一个坏掉的
# 未来时间戳会让证据永久新鲜）。
_REVIVE_EVIDENCE_CLOCK_SKEW_SECONDS = 60


def _has_fresh_probe_evidence(token: str) -> bool:
    """库里是否存在「刚由认证探针写入的 healthy 判定」。

    这是复活的第二把锁：调用方（号池探活）负责 dwell 与条件写，这里负责
    「没有证据就不放行」——revive_token 是公开原语，裸调用本身不携带任何证据。
    读不到（异常 / 没有记录 / 判定不新鲜 / 字段畸形）一律按无证据处理：
    复活失败是可重试的，把一个被封的号放回流量不是。
    """
    try:
        from utils import store
        account = store.get_account(token) or {}
    except Exception as e:
        # 只记异常类型：异常原文可能带上游/凭据片段
        logger.error(f"[antiban] revive evidence unreadable: {type(e).__name__}")
        return False
    if str(account.get("status") or "").strip().lower() != "healthy":
        return False
    checked_at = account.get("last_health_check")
    if isinstance(checked_at, bool) or not isinstance(checked_at, (int, float)):
        return False
    if not math.isfinite(float(checked_at)):
        return False
    age = time.time() - float(checked_at)
    return -_REVIVE_EVIDENCE_CLOCK_SKEW_SECONDS <= age <= _REVIVE_EVIDENCE_MAX_AGE_SECONDS


def revive_token(token: str, reason: str = "probe_recovered") -> bool:
    """Remove the legacy dead marker after the canonical probe transition.

    The caller owns the dwell gate; this function owns the evidence gate and
    refuses to revive without a *fresh* healthy verdict persisted by an
    authenticated probe.  A bare call carries no evidence, so it must not be
    able to put a restricted account back into routing.
    """
    if not token or token not in globals.antiban_dead_tokens:
        return False
    if not _has_fresh_probe_evidence(token):
        _count("revive.refused_no_evidence")
        logger.warning(
            f"[antiban] {anon_id(token)} revive refused: no fresh authenticated-probe verdict"
        )
        return False
    del globals.antiban_dead_tokens[token]
    try:
        _persist_dead()
    except Exception as e:  # pragma: no cover
        logger.error(f"[antiban] failed to persist dead removal: {type(e).__name__}")
    label = "probe_recovered" if reason == "probe_recovered" else "manual"
    _count(f"revive.{label}")
    logger.info(f"[antiban] {anon_id(token)} revived: reason={label}")
    return True


def reset_backoff(token: str) -> None:
    _account_backoff_level.pop(token, None)


def bump_backoff(token: str) -> int:
    level = _account_backoff_level.get(token, 0) + 1
    _account_backoff_level[token] = level
    return level


def _cooldown_bucket_accounts(bucket_id: str, seconds: int, reason: str) -> None:
    """IP 桶降级时，给桶内所有账号加一次冷却延长，避免下次还用该桶相关账号立刻命中。"""
    meta = _bucket.get_bucket_meta(bucket_id)
    for token in meta.get("accounts", []):
        cooldown.extend_cooldown(token, seconds, reason=reason)


def classify_response_error(status_code: int, detail) -> str:
    """把上游错误归一成枚举。detail 只读不留存。

    判定顺序（先硬证据后正文标记，避免正文里的词把状态码结论吃掉）：
      1. 429 / rate-limit——状态码本身就是频控证据；
      2. 401 + invalid_grant|unauthorized——凭据失效，走 refresh 恢复路径；
      3. 正文标记（挑战族 / 账号停用 / 账号不可用）；
      4. banned；5. 5xx；其余 unclassified（仍可计数，不猜类别）。

    状态类规则排在标记之前：正文里恰好提到某个标记词，不足以覆盖「这是一个 429 /
    一个 invalid_grant」这种更硬的证据。反之标记要求 403 而未满足时**跳过该标记
    继续往下找**，不是就地放弃：否则 401 + "turnstile ... account unavailable"
    会在 turnstile 处停住，永远看不到后面那个更明确的账号状态标记。
    """
    detail_str = (str(detail) if detail is not None else "").lower()
    if status_code == 429 or "rate-limit" in detail_str:
        return REASON_RATE_LIMIT
    if status_code == 401 and ("invalid_grant" in detail_str or "unauthorized" in detail_str):
        return REASON_AUTH_INVALID
    for marker, reason, requires_403 in _CLASSIFY_MARKERS:
        if marker not in detail_str:
            continue
        if requires_403 and status_code != 403:
            continue
        return reason
    if "banned" in detail_str:
        return REASON_ACCOUNT_DEAD
    if 500 <= status_code < 600:
        return REASON_UPSTREAM_5XX
    return REASON_UNCLASSIFIED


def handle_response_error(token: str, bucket_id: Optional[str], status_code: int, detail) -> str:
    """分类并执行降载动作，返回分类枚举（供调用方做匿名诊断）。"""
    if not configs.enable_antiban:
        return REASON_UNCLASSIFIED

    reason = classify_response_error(status_code, detail)
    _count(reason, status_code)

    # Cloudflare 挑战 → 整桶降级
    if reason == REASON_CF_CHALLENGE:
        if bucket_id:
            _bucket.degrade_bucket(bucket_id, configs.circuit_403_cooldown)
            _cooldown_bucket_accounts(bucket_id, configs.circuit_403_cooldown, REASON_CF_CHALLENGE)
            _count(f"{EVENT_BUCKET_DEGRADED}.{REASON_CF_CHALLENGE}")
        return reason

    # 挑战族（PoW/Turnstile/Arkose）与账号暂时不可用 → 账号退避。
    # 这些是账号/会话级信号，不降级整桶：同一出口上的其它号未必被挑战。
    if reason in _COOLDOWN_REASONS:
        if token:
            cooldown.extend_cooldown(token, configs.circuit_403_cooldown, reason=reason)
        return reason

    # 频控 → 账号指数退避
    if reason == REASON_RATE_LIMIT:
        level = bump_backoff(token)
        base = configs.circuit_429_cooldown
        cooldown.extend_cooldown(
            token, min(base * (2 ** (level - 1)), 7200), reason=REASON_RATE_LIMIT
        )
        return reason

    # 刷新凭据失效 → error_token_list（refreshToken 流程会尝试恢复）
    if reason == REASON_AUTH_INVALID:
        if token and token not in globals.error_token_list:
            globals.error_token_list.append(token)
            globals.persist_error_tokens()
        return reason

    # 账号停用/封禁 → 永久黑名单。只记枚举，不记上游原文
    if reason == REASON_ACCOUNT_DEAD:
        mark_dead(token, REASON_ACCOUNT_DEAD)
        return reason

    # 其他 5xx：不立即降级，但轻度退避（避免风暴）
    if reason == REASON_UPSTREAM_5XX and token:
        cooldown.extend_cooldown(token, 30, reason=REASON_UPSTREAM_5XX)
    return reason


def handle_response_success(token: str) -> None:
    if not configs.enable_antiban:
        return
    reset_backoff(token)


def handle_network_error(token: str, bucket_id: Optional[str], error_kind: str = "") -> None:
    """M3: 网络层错误（连接被拒/超时/DNS 失败）连续 N 次 → 标记代理桶不健康。

    与 handle_response_error 区分：那是 HTTP 状态码，这是 transport 失败。
    error_kind 是调用方的 `type(e).__name__`（任意文本），先归一到枚举再入
    指标与日志：未注册的值一律 other，既封住基数也封住文本泄漏面。

    计数与降级分开：计数属于「发生了什么」，降级属于「拿哪条出口补偿」。
    号还没绑桶时（严格绑定下会被准入层拒绝，但已绑桶的号也可能在分配失败时
    落到这里）没有出口可降级，**但失败本身必须被计数**——否则「绑不上桶的号
    在一个打不出去的出口上连续失败」在指标里完全不可见，正好是最该看见的一段。
    """
    if not configs.enable_antiban:
        return
    kind = normalize_network_kind(error_kind)
    _count(f"{REASON_NETWORK}.{kind}")
    if not bucket_id:
        return
    count = _bucket_network_errors.get(bucket_id, 0) + 1
    _bucket_network_errors[bucket_id] = count
    if count >= _NETWORK_ERROR_THRESHOLD:
        _bucket.degrade_bucket(bucket_id, _NETWORK_ERROR_COOLDOWN)
        _cooldown_bucket_accounts(bucket_id, _NETWORK_ERROR_COOLDOWN, REASON_NETWORK)
        _bucket_network_errors[bucket_id] = 0
        _count(f"{EVENT_BUCKET_DEGRADED}.{REASON_NETWORK}")
        logger.warning(
            f"[antiban] bucket degraded after {_NETWORK_ERROR_THRESHOLD}x network errors "
            f"reason={REASON_NETWORK} kind={kind}"
        )


def reset_network_errors(bucket_id: Optional[str]) -> None:
    """成功响应后清空网络错误计数。"""
    if bucket_id and bucket_id in _bucket_network_errors:
        _bucket_network_errors.pop(bucket_id, None)


async def scheduled_heal() -> None:
    """定时任务入口：由 APScheduler 调用。

    只自愈 bucket（代理层，可观测且可逆）。死号**不**在此自动复活：
    复活需要「认证探针成功 + dwell 窗口」，走号池的 fleet_health 路径
    （`fleet_health.check_account` → `revive_token`），不是靠定时器无条件翻回来。
    """
    restored = _bucket.heal_buckets()
    if restored:
        logger.info(f"[antiban] scheduled_heal restored {restored} bucket(s)")
