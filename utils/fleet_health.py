"""Periodic account health check (fleet 健康检查).

Probes every account and writes a three-state status into ``accounts.status``:
  healthy   - /backend-api/me returned JSON whose identity matches the account's
              own authenticated JWT user
  unhealthy - circuit-dead, in the error list, unusable credential, or the probe
              did not produce that evidence
  disabled  - manual override; never probed or overwritten here

This module *reads* the antiban verdicts (dead list, error list, cooldown) rather
than re-deriving them. It is a liveness probe, not a second health model.

状态模型：``resolve_account_status`` 把每个能限制账号的来源（人工停用、熔断判定、
错误列表、``accounts.status``）折叠成 healthy / degraded / unhealthy / dead /
disabled 之一，扫描与面板共用它。``accounts.status`` 本身仍然只写
healthy/unhealthy —— 其余状态由人工或熔断器持有。

恢复：非 healthy 的账号经 ``diagnose_access_token`` 重新取证（不能用 verify_token，
原因见其 docstring），并且必须在 ``_RECOVERY_DWELL_SECONDS`` 内持续探针成功才会
恢复；任何一次失败都重置计时。

判据约束（探针是证据来源，所以证据标准必须高于"连上了"）：
  1. HTTP 200 不构成成功。登录墙、Cloudflare 页面、空 body、非 JSON 一律失败——
     被挡在墙外的死号如果被判 healthy，路由会继续把流量送给它；
  2. 成功 = JSON 对象 且 body.id 与本账号 access_token 里的 chatgpt_user_id 一致。
     拿到别人的身份或一个通用页面，都不是"这个账号可用"的证据；
  3. 请求带 workspace 作用域头（chatgpt-account-id），否则 Plus/Pro 的工作区权益
     不归属，探到的可用性不是这个账号真实的可用性；
  4. 畸形/过期 token、身份不可验证 → 直接判 unhealthy，不发上游请求；
  5. 代理只用**既有**显式绑定（routing 绑定 → 账号行 proxy_url）。探针不给账号
     分配新出口：那会破坏粘性绑定，并让健康结论对应到另一条链路；
  6. 日志与指标只含匿名标识、状态码与原因枚举；token 前缀、代理串、异常原文
     与上游 body 一律不落；
  7. 目标只取显式配置的 CHATGPT_BASE_URL：显式为空 = 没有目标，不发请求、不构造
     客户端、也不回落到真实站点。探针测的必须是运维配的那条上游。
"""

import asyncio
import hashlib
import json
import math
import random
import time
from typing import Dict, Optional, Sequence, Tuple

from fastapi import HTTPException

import utils.globals as globals
import utils.store as store
from chatgpt.authorization import verify_token
from chatgpt.refreshToken import rt2ac, sess2ac
from utils import configs
from utils.Client import Client
from utils.Logger import logger
from utils.antiban import circuit as antiban_circuit
from utils.antiban.concurrency import anon_id
from utils.routing import get_bound_proxy

# 单个账号的探针结果原因枚举。UI/运维按此区分业务状态，故保留；上游原文一律丢弃。
REASON_OK = "ok"
REASON_CIRCUIT_DEAD = "circuit_dead"
# 账号行自己就是 dead（与熔断器的 dead 列表分开持有）。
REASON_PERSISTED_DEAD = "persisted_dead"
REASON_ERROR_LIST = "error_list"
REASON_AUTH_FAILED = "auth_failed"
REASON_TOKEN_MALFORMED = "token_malformed"
REASON_TOKEN_EXPIRED = "token_expired"
REASON_IDENTITY_UNVERIFIABLE = "identity_unverifiable"
REASON_IDENTITY_MISMATCH = "identity_mismatch"
REASON_NOT_JSON = "not_json"
REASON_UNAUTHORIZED = "unauthorized"
REASON_FORBIDDEN = "forbidden"
REASON_RATE_LIMITED = "rate_limited"
REASON_UPSTREAM_5XX = "upstream_5xx"
REASON_HTTP_ERROR = "http_error"
REASON_NETWORK_ERROR = "network_error"
REASON_INTERNAL_ERROR = "internal_error"
# 显式空的 CHATGPT_BASE_URL：没有探针目标。这不是账号的问题，也不许回落到真实站点。
REASON_NO_BASE_URL = "no_base_url"
# 探针已成功，但 dwell 未满：仍不接流量，也不对外报成 healthy。
REASON_RECOVERING = "recovering"
# 人工停用：这不是健康问题，探针不参与判定。
REASON_DISABLED = "disabled"

# 整轮扫描的节奏间隔：大号池不要一次打满上游。
_PACING_SECONDS = 0.2

# 恢复所需的持续成功时长。单次成功不足以恢复：一个抖动或半坏的账号会在一次
# 侥幸响应后被重新放回流量。任何一次失败都会重置该计时。
_RECOVERY_DWELL_SECONDS = 900

# 匿名指标：reason → 次数。不含任何账号标识。
_health_events: Dict[str, int] = {}

# token -> 当前成功连击的起点（首次成功且已认证的探针时间）
_recovery_state: Dict[str, int] = {}
# token -> {"status", "reason", "checked_at"}：最近一次判定，供面板解释失败原因
_last_verdict: Dict[str, Dict[str, object]] = {}


# ---------------------------------------------------------------------------
# 统一账号状态模型
#
# ``accounts.status`` 不是唯一能让账号不可用的来源：熔断器另有 dead 判定，错误列表
# 是独立的内存信号，人工停用则是运营决定。扫描与面板必须给出同一个折叠后的答案，
# 并且这个答案在任何时候都不能把一个受限账号说成可用。
# ---------------------------------------------------------------------------

STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_UNHEALTHY = "unhealthy"
STATUS_DEAD = "dead"
STATUS_DISABLED = "disabled"

STATUS_LABELS = {
    STATUS_HEALTHY: "正常",
    STATUS_DEGRADED: "降级",
    STATUS_UNHEALTHY: "异常",
    STATUS_DEAD: "已熔断",
    STATUS_DISABLED: "停用",
}
# 未建模的取值不是「正常」：没有证据表明可用时，面板不能替它下结论。
STATUS_UNKNOWN_LABEL = "未知"

# 处于这两个状态时探针要重新取证；也因此它们是唯一需要走 dwell 的入口状态。
_RECOVERABLE_STATUSES = (STATUS_UNHEALTHY, STATUS_DEGRADED, STATUS_DEAD)

# 需要运维关注的状态。人工停用不在其中：那是运营决定，不是健康问题。
IMPAIRED_STATUSES = (STATUS_DEGRADED, STATUS_UNHEALTHY, STATUS_DEAD)


def is_impaired(status: Optional[str]) -> bool:
    return status in IMPAIRED_STATUSES


def resolve_account_status(token: str, persisted_status: Optional[str] = None) -> str:
    """把每个能限制账号的来源折叠成一个状态。

    优先级：人工停用 > dead（熔断器或账号行）> 持久化 degraded > 错误列表 > 已被证实的
    healthy。其余情况——未建模的取值、缺失的账号行——一律落到 ``unhealthy``：面板不得从
    「没有记录」推断出「可路由」。
    """
    if persisted_status is None:
        persisted_status = (store.get_account(token) or {}).get("status")
    if persisted_status == STATUS_DISABLED:
        return STATUS_DISABLED
    if persisted_status == STATUS_DEAD or antiban_circuit.is_token_dead(token):
        return STATUS_DEAD
    if persisted_status == STATUS_DEGRADED:
        return STATUS_DEGRADED
    if token in globals.error_token_list:
        return STATUS_UNHEALTHY
    if persisted_status == STATUS_HEALTHY:
        return STATUS_HEALTHY
    return STATUS_UNHEALTHY


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, STATUS_UNKNOWN_LABEL)


def health_rate(statuses: Sequence[str]) -> int:
    """探针可管理账号中处于 healthy 的百分比。

    人工停用的账号不计入分母：运营把账号关掉是一个决定，不是健康结果，把它算成
    unhealthy 会让这个比率描述别的东西。全停用的池子报 0，而不是虚高的 100%。
    """
    counted = [s for s in statuses if s != STATUS_DISABLED]
    if not counted:
        return 0
    return round(100 * counted.count(STATUS_HEALTHY) / len(counted))


def _count(reason: str) -> None:
    _health_events[reason] = _health_events.get(reason, 0) + 1


def get_health_stats() -> Dict[str, int]:
    """按 reason 的匿名计数。"""
    return dict(_health_events)


def reset_health_stats() -> None:
    _health_events.clear()


def reset_recovery_state() -> None:
    """清空进程内的恢复证据（测试与显式复位用）。"""
    _recovery_state.clear()
    _last_verdict.clear()


def get_last_verdict(token: str) -> Dict[str, object]:
    """最近一次判定的 ``{"status", "reason", "checked_at"}``；未知账号返回 {}。"""
    return dict(_last_verdict.get(token) or {})


def _record_verdict(token: str, status: str, reason: str, checked_at: int) -> Tuple[str, str]:
    """记录并返回一次判定。原因枚举同时进入匿名指标与面板的失败原因。"""
    _count(reason)
    _last_verdict[token] = {"status": status, "reason": reason, "checked_at": checked_at}
    return status, reason


def _decode_jwt_payload(token: str) -> dict:
    """解码 JWT payload（不校验签名，只取身份/有效期字段）。任何失败返回 {}。

    本地实现而不是复用 gateway 的同名函数：健康探针不应把自己挂到网关线上。
    """
    if not token or "." not in token:
        return {}
    try:
        import base64
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _identity_of(access_token: str) -> Tuple[Optional[str], dict]:
    """返回 (原因枚举 or None, {"user_id", "account_id"})。

    None 表示凭据本身可用于探测；否则调用方必须在**不发请求**的情况下判 unhealthy。
    """
    if not access_token or not isinstance(access_token, str):
        return REASON_TOKEN_MALFORMED, {}
    claims = _decode_jwt_payload(access_token)
    if not claims:
        return REASON_TOKEN_MALFORMED, {}

    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        return REASON_TOKEN_MALFORMED, {}
    if exp <= time.time():
        return REASON_TOKEN_EXPIRED, {}

    auth = claims.get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        return REASON_IDENTITY_UNVERIFIABLE, {}
    user_id = auth.get("chatgpt_user_id") or auth.get("user_id") or ""
    if not user_id:
        # 没有账号用户标识 → 无法判断响应属于谁 → 探针拿不到可用的证据。
        return REASON_IDENTITY_UNVERIFIABLE, {}
    return None, {"user_id": user_id, "account_id": auth.get("chatgpt_account_id") or ""}


def _resolve_proxy(token: str):
    """既有的显式出口绑定；没有绑定就**不带**代理。

    旧实现在无绑定时 random.choice(configs.proxy_url_list)，等于探针替账号做了一次
    新的出口分配：粘性绑定被打破，而且健康结论对应的是另一条链路。
    """
    bound = get_bound_proxy(token)
    if not bound:
        account = store.get_account(token) or {}
        bound = account.get("proxy_url")
        if not bound:
            saved = account.get("fingerprint") or {}
            if isinstance(saved, str):
                saved = json.loads(saved)
            if isinstance(saved, dict):
                bound = saved.get("proxy_url")
    # The normal chat path also falls back to the configured pool when an
    # account has no sticky egress. Probe through that same pool; probing direct
    # would classify a proxy-only deployment as unhealthy for the wrong reason.
    if not bound and configs.proxy_url_list:
        bound = random.choice(configs.proxy_url_list)
    if not bound:
        return None
    session_id = hashlib.md5(token.encode()).hexdigest()
    return bound.replace("{}", session_id)


def _content_type(response) -> str:
    headers = getattr(response, "headers", None) or {}
    try:
        items = headers.items()
    except Exception:
        return ""
    for k, v in items:
        if str(k).lower() == "content-type":
            return str(v).lower()
    return ""


def _classify_status(status_code: int) -> str:
    if status_code == 401:
        return REASON_UNAUTHORIZED
    if status_code == 403:
        return REASON_FORBIDDEN
    if status_code == 429:
        return REASON_RATE_LIMITED
    if 500 <= status_code < 600:
        return REASON_UPSTREAM_5XX
    return REASON_HTTP_ERROR


def _verify_identity(response, expected_user_id: str) -> str:
    """把一个 200 响应判成 REASON_OK / REASON_NOT_JSON / REASON_IDENTITY_MISMATCH。

    body 只用于判定，不留存、不进日志。
    """
    if "json" not in _content_type(response):
        return REASON_NOT_JSON
    try:
        body = response.json()
    except Exception:
        return REASON_NOT_JSON
    if not isinstance(body, dict):
        return REASON_NOT_JSON
    actual = body.get("id") or ""
    if actual and actual == expected_user_id:
        return REASON_OK
    return REASON_IDENTITY_MISMATCH


async def _probe_account(token: str, access_token: str, identity: dict, proxy_url) -> str:
    """/backend-api/me 探针。返回原因枚举（REASON_OK 才算这个账号可用）。

    取消必须向上传播（整轮扫描被取消时不能被吞成 unhealthy），但连接不能泄漏：
    响应到手 → close() 归还池子；异常/取消 → discard() 硬关闭。
    """
    # 显式空 base URL = 没有配置上游，也就没有探针目标。旧实现回落到
    # https://chatgpt.com：那会把运维明确关掉的外呼重新打开，而且探到的可用性
    # 对应的是另一个上游。没有目标就**不构造客户端**，直接返回一个有界原因。
    if not configs.chatgpt_base_url_list:
        logger.info(f"[health] {anon_id(token)} probe skipped reason={REASON_NO_BASE_URL}")
        return REASON_NO_BASE_URL
    base_url = random.choice(configs.chatgpt_base_url_list)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    # 工作区作用域：Plus/Pro 的权益按 account 归属，缺这个头探到的不是该账号的可用性。
    if identity.get("account_id"):
        headers["chatgpt-account-id"] = identity["account_id"]

    client = Client(proxy=proxy_url, impersonate="safari15_3")
    released = False
    try:
        r = await client.get(f"{base_url}/backend-api/me", headers=headers, timeout=15)
        if r.status_code == 200:
            reason = _verify_identity(r, identity["user_id"])
        else:
            reason = _classify_status(r.status_code)
            logger.info(f"[health] {anon_id(token)} probe status={r.status_code} reason={reason}")
        await client.close()
        released = True
        if reason != REASON_OK:
            logger.info(f"[health] {anon_id(token)} probe rejected reason={reason}")
        return reason
    except asyncio.CancelledError:
        await client.discard()
        released = True
        raise
    except Exception as e:
        # 只记异常类型：异常原文会带代理地址与上游响应片段
        await client.discard()
        released = True
        logger.info(f"[health] {anon_id(token)} probe error={type(e).__name__}")
        return REASON_NETWORK_ERROR
    finally:
        if not released:  # pragma: no cover - 防御：上面每条路径都已归还
            await client.discard()


async def diagnose_access_token(token: str) -> str:
    """把存储的凭据换成 access token，**仅用于诊断**。

    这里刻意不用 ``verify_token``。那个函数是路由闸门：它会拒绝任何持久化状态已经是
    unhealthy/degraded 的账号。探针若复用它，就永远无法重新检查它本来要重新检查的
    账号——unhealthy 因此变成一句无期徒刑。

    凭据种类分派与 verify_token 保持一致，以免支持面变小；但它**不**继承两件事：
      - 路由闸门（见上）；
      - 前端会话查询：那回答的是网关的问题，不是「这个账号在上游是否可用」。

    它本身不授予任何权限：调用方仍须通过身份探针与 dwell 窗口，账号才会重新可路由。
    """
    if token.startswith("eyJhbGciOi") or token.startswith("fk-"):
        return token
    if token.startswith("sess-"):
        return await sess2ac(token, force_refresh=False)
    if (token.startswith("rt_") and len(token) >= 60) or len(token) == 45:
        return await rt2ac(token, force_refresh=False)
    return token


async def _diagnose(token: str) -> Tuple[str, Optional[str]]:
    """执行诊断换票；失败时返回 ``("", reason)``。"""
    try:
        return await diagnose_access_token(token), None
    except HTTPException:
        return "", REASON_AUTH_FAILED
    except Exception as exc:
        # CancelledError 是 BaseException，不会被这里吞掉；只记异常类型，
        # 异常原文会带代理地址与上游响应片段。
        logger.info(f"[health] {anon_id(token)} diagnosis error={type(exc).__name__}")
        return "", REASON_NETWORK_ERROR


async def check_account_detail(token: str, now: Optional[int] = None) -> Tuple[str, str]:
    """Return ``(probe_status, reason)`` for one account (no status side effects).

    ``probe_status`` is always ``healthy`` or ``unhealthy`` -- the vocabulary
    ``store.apply_health_probe`` accepts. It answers "does the probe find this account
    serving", not "what is this account's canonical status": for the latter (dead /
    disabled / degraded) see ``resolve_account_status``, which the panel uses.
    """
    now_ts = int(time.time()) if now is None else int(now)

    persisted = (store.get_account(token) or {}).get("status")

    # 人工停用优先：它不是健康问题，也不允许被其他信号（包括错误列表）改写或遮蔽。
    if persisted == STATUS_DISABLED:
        return _record_verdict(token, STATUS_UNHEALTHY, REASON_DISABLED, now_ts)

    # A dead account is not routable, but it is diagnosable.  Recovery uses the
    # same authenticated upstream probe and dwell gate as other restricted
    # states; the ordinary request path remains fail-closed throughout.
    circuit_dead = antiban_circuit.is_token_dead(token)
    recoverable = persisted in _RECOVERABLE_STATUSES or circuit_dead

    # 错误列表是**派生**信号：store.load_all 会从持久化为 unhealthy 的账号行重建它。
    # 所以当账号本身还带持久化受限状态时，这个列表不能成为它自己的理由——重启后那会让
    # 受限永久化（每轮扫描只记 error_list，永远取不到证据）。只有当列表是账号不可用的
    # **唯一**理由（没有可重新评估的持久化状态）时，它才保持权威、探针让位。
    if token in globals.error_token_list and persisted not in _RECOVERABLE_STATUSES:
        return _record_verdict(token, STATUS_UNHEALTHY, REASON_ERROR_LIST, now_ts)

    try:
        access_token = await verify_token(token)
    except HTTPException:
        if recoverable:
            # 路由闸门按设计拒绝受限账号，所以它的拒绝不构成关于凭据的证据。
            # 走显式诊断路径重新取证（不是绕过判定：下面仍要过身份探针与 dwell）。
            access_token, auth_reason = await _diagnose(token)
            if auth_reason is not None:
                _recovery_state.pop(token, None)
                return _record_verdict(token, STATUS_UNHEALTHY, auth_reason, now_ts)
        else:
            # 未受限的账号保持走生产闸门：诊断路径不得成为 fail-open。
            _recovery_state.pop(token, None)
            return _record_verdict(token, STATUS_UNHEALTHY, REASON_AUTH_FAILED, now_ts)

    reason, identity = _identity_of(access_token)
    if reason is not None:
        # 凭据不可用 → 不发请求。发了也只会拿回一个 401，白打一次上游。
        logger.info(f"[health] {anon_id(token)} not probed reason={reason}")
        _recovery_state.pop(token, None)
        return _record_verdict(token, STATUS_UNHEALTHY, reason, now_ts)

    reason = await _probe_account(token, access_token, identity, _resolve_proxy(token))
    if reason != REASON_OK:
        _recovery_state.pop(token, None)
        return _record_verdict(token, STATUS_UNHEALTHY, reason, now_ts)

    # 已认证探针成功。本就健康的账号直接确认；受限账号必须先撑过 dwell。
    if not recoverable:
        _recovery_state.pop(token, None)
        return _record_verdict(token, STATUS_HEALTHY, REASON_OK, now_ts)

    since = _recovery_state.get(token)
    if since is None:
        _recovery_state[token] = now_ts
        return _record_verdict(token, STATUS_UNHEALTHY, REASON_RECOVERING, now_ts)
    if now_ts - since < _RECOVERY_DWELL_SECONDS:
        return _record_verdict(token, STATUS_UNHEALTHY, REASON_RECOVERING, now_ts)

    _recovery_state.pop(token, None)
    return _record_verdict(token, STATUS_HEALTHY, REASON_OK, now_ts)


async def check_account(token: str, now: Optional[int] = None) -> str:
    """Return the health status of one account (no status side effects)."""
    status, _ = await check_account_detail(token, now=now)
    return status


async def check_all_accounts():
    """Probe every account and sync status + last_health_check. Disabled are skipped.

    单个账号的意外异常只记一次 errors 并继续：一个号把整轮扫描打断，会让其余账号
    的状态无限期停留在上一轮的旧值。内部异常也**不**写进 accounts.status——
    那是我们自己的故障，不是关于这个账号的证据。

    恢复期（探针成功但 dwell 未满）按 ``unhealthy`` 落库并额外计入 ``recovering``：
    计数是保守的，面板可以解释为什么一个探针已经成功的号还没接流量。
    """
    now = int(time.time())
    summary = {"healthy": 0, "unhealthy": 0, "recovering": 0, "skipped_disabled": 0,
               "skipped_changed": 0, "errors": 0}
    for token in list(globals.token_list):
        acct = store.get_account(token)
        if (acct or {}).get("status") == STATUS_DISABLED:
            summary["skipped_disabled"] += 1
            continue
        try:
            status = await check_account(token)
            if not store.apply_health_probe(token, (acct or {}).get("status"), status, now):
                summary["skipped_changed"] += 1
                continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            summary["errors"] += 1
            _count(REASON_INTERNAL_ERROR)
            logger.error(f"[health] {anon_id(token)} sweep error={type(e).__name__}")
            continue
        summary[status] = summary.get(status, 0) + 1
        if status == STATUS_HEALTHY:
            # The guarded write above just published a healthy status, so the in-memory
            # error list must stop contradicting it: routing reads that list, and a
            # stale membership would make the recovery cosmetic. This is a consequence
            # of a verified recovery, never a cause of one -- it runs only after the
            # authenticated probe succeeded, the dwell elapsed, and the conditional
            # UPDATE matched.
            globals.clear_error_token(token)
            antiban_circuit.revive_token(token)
        if get_last_verdict(token).get("reason") == REASON_RECOVERING:
            summary["recovering"] += 1
        # Gentle pacing so a large fleet does not hammer the upstream at once.
        if _PACING_SECONDS:
            await asyncio.sleep(_PACING_SECONDS)
    logger.info(
        f"[health] fleet probe done: healthy={summary['healthy']} "
        f"unhealthy={summary['unhealthy']} disabled={summary['skipped_disabled']} "
        f"errors={summary['errors']}"
    )
    return summary
