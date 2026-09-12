"""账号级冷却与请求节奏。

行为：
  wait_or_skip(token) → 在 max_wait 预算内等到账号可用则返回 True，否则返回 False；
                         预算是**一次请求的总等待时间**，锁排队、睡眠和睡眠期间被重新
                         延长的冷却都从同一个 deadline 里扣。
  record_request(token) → 把 next_available 推到 now + interval * (1 ± jitter)；
                           只前移不回退——成功一次不能抵消 429/限流刚加的退避。
  extend_cooldown(token, sec, reason) → 上游确证的限流证据（429/403/chatLimit）强制延长冷却。

不变量：
  1. wait_or_skip 返回 True 时，账号在该时刻确实已过冷却；
  2. 任何路径都不会让 next_available 变小（单调不减），除非显式 reset；
  3. 日志与指标只含匿名账号标识和原因枚举，不含 token、代理或上游原文。
"""

import asyncio
import random
import time
from typing import Dict, Optional

from utils import configs
from utils.antiban.concurrency import anon_id
from utils.Logger import logger

# 延长冷却的原因枚举。这里定义而不是从 circuit 导入：cooldown 是更底层模块，
# 反向依赖 circuit 会形成循环（circuit 已经 import cooldown）。
REASON_RATE_LIMIT = "rate_limit"
REASON_CF_CHALLENGE = "cf_challenge"
REASON_CHAT_LIMIT = "chat_limit"
REASON_DEGRADED_QUALITY = "degraded_quality"
REASON_UPSTREAM_5XX = "upstream_5xx"
REASON_NETWORK = "network_error"
REASON_ACCOUNT_DEAD = "account_deactivated"
REASON_AUTH_INVALID = "auth_invalid"
REASON_UNSPECIFIED = "unspecified"
REASON_OTHER = "other"

_account_next_available: Dict[str, float] = {}
_account_locks: Dict[str, asyncio.Lock] = {}

# 匿名诊断：只按原因枚举计数，绝不记录账号标识。
#   admitted_immediately  无冷却，直接放行
#   admitted_after_wait   等待后放行
#   skip_over_max_wait    进入时剩余冷却就超过预算
#   skip_lock_timeout     等同 token 的串行锁超过预算
#   skip_extended_in_wait 等待期间冷却被重新延长，超出预算
_cooldown_events: Dict[str, int] = {}

# 冷却被延长的原因枚举（UI/运维需要据此区分业务状态，故保留原因但不保留原文）。
# 与 circuit.DEAD_REASONS 同理：调用方传入的是任意文本，直接当指标键/日志值
# 既泄漏又高基数。EXTEND_REASONS 是**有界**白名单，未注册的一律 other。
#   unspecified 调用方明说「没原因」；other 调用方给了我们没注册的原因——
#   两者必须分开，否则新增调用点会静默混进默认桶里再也看不见。
EXTEND_REASONS = frozenset({
    REASON_RATE_LIMIT,
    REASON_CF_CHALLENGE,
    REASON_CHAT_LIMIT,
    REASON_DEGRADED_QUALITY,
    REASON_UPSTREAM_5XX,
    REASON_NETWORK,
    REASON_ACCOUNT_DEAD,
    REASON_AUTH_INVALID,
    REASON_UNSPECIFIED,
    REASON_OTHER,
})

_extend_reasons: Dict[str, int] = {}


def normalize_extend_reason(reason) -> str:
    """把任意 reason 归一到 EXTEND_REASONS 中的一个。未注册 → other。"""
    r = str(reason or "").strip().lower()
    if not r:
        return REASON_UNSPECIFIED
    return r if r in EXTEND_REASONS else REASON_OTHER


def _count(event: str) -> None:
    _cooldown_events[event] = _cooldown_events.get(event, 0) + 1


def get_cooldown_stats() -> Dict[str, Dict[str, int]]:
    """匿名指标：{"events": {...}, "extend_reasons": {...}}。不含任何账号标识。"""
    return {"events": dict(_cooldown_events), "extend_reasons": dict(_extend_reasons)}


def reset_cooldown_stats() -> None:
    _cooldown_events.clear()
    _extend_reasons.clear()


def _get_lock(token: str) -> asyncio.Lock:
    lock = _account_locks.get(token)
    if lock is None:
        lock = asyncio.Lock()
        _account_locks[token] = lock
    return lock


def _resolve_interval(token: str, persona: Optional[str] = None) -> int:
    """Team/Plus persona → 较短间隔；free/未知 → 较长间隔。"""
    if persona == "chatgpt-freeaccount":
        return configs.free_account_min_interval_seconds
    return configs.account_min_interval_seconds


async def wait_or_skip(token: str, persona: Optional[str] = None, max_wait: Optional[int] = None) -> bool:
    """等待账号冷却结束。超出 max_wait 预算则返回 False（由调用方换号/拒绝）。

    max_wait 是**整个调用**的时间预算，而不是单次 sleep 的上限：
      - 同 token 的串行锁排队要计入（否则前面排 5 个人时实际等待无上限）；
      - 睡眠期间冷却被 429/chatLimit 重新延长也要计入（否则醒来直接放行，
        等于「冷却超时仍然继续发送请求」）。
    """
    if not configs.enable_antiban or not token:
        return True

    max_wait_seconds = max_wait if max_wait is not None else configs.account_max_wait_seconds
    deadline = time.time() + max_wait_seconds

    remaining = _account_next_available.get(token, 0.0) - time.time()
    if remaining <= 0:
        _count("admitted_immediately")
        return True

    if remaining > max_wait_seconds:
        _count("skip_over_max_wait")
        logger.info(
            f"[antiban] cooldown skip {anon_id(token)} "
            f"remaining={remaining:.1f}s > max_wait={max_wait_seconds}s"
        )
        return False

    # 串行化同 token 的并发请求，避免"N 个协程同时读到已冷却完毕"。
    # 锁本身的排队时间必须受同一个 deadline 约束。
    lock = _get_lock(token)
    lock_budget = deadline - time.time()
    if lock_budget <= 0:
        _count("skip_lock_timeout")
        logger.info(f"[antiban] cooldown skip {anon_id(token)} reason=lock_timeout")
        return False
    try:
        await asyncio.wait_for(lock.acquire(), timeout=lock_budget)
    except asyncio.TimeoutError:
        _count("skip_lock_timeout")
        logger.info(f"[antiban] cooldown skip {anon_id(token)} reason=lock_timeout")
        return False

    try:
        # 循环重读：睡眠期间冷却可能被重新延长，醒来必须重新判断而不是直接放行
        while True:
            now = time.time()
            remaining = _account_next_available.get(token, 0.0) - now
            if remaining <= 0:
                _count("admitted_after_wait")
                return True
            if now + remaining > deadline:
                _count("skip_extended_in_wait")
                logger.info(
                    f"[antiban] cooldown skip {anon_id(token)} reason=extended_in_wait "
                    f"remaining={remaining:.1f}s beyond budget"
                )
                return False
            await asyncio.sleep(min(remaining, max(0.0, deadline - now)))
    finally:
        lock.release()


def record_request(token: str, persona: Optional[str] = None, min_interval: Optional[int] = None) -> None:
    """记录一次成功请求的节奏冷却。只前移，绝不缩短已有冷却。

    成功一次并不能证明刚才的 429 / chatLimit 退避已经失效；若直接覆盖，
    一次成功就能把 30 分钟的退避抹成 60 秒的节奏间隔，退避形同虚设。
    """
    if not configs.enable_antiban or not token:
        return
    interval = min_interval if min_interval is not None else _resolve_interval(token, persona)
    jitter = configs.account_cooldown_jitter
    delta = interval * (1 + random.uniform(-jitter, jitter))
    target = time.time() + max(0.0, delta)
    current = _account_next_available.get(token, 0.0)
    if target <= current:
        _count("record_kept_longer_cooldown")
        return
    _account_next_available[token] = target


def extend_cooldown(token: str, seconds: int, reason: str = "unspecified") -> None:
    """延长账号冷却（单调不减）。reason 归一到枚举后才进指标与日志。"""
    if not configs.enable_antiban or not token or seconds <= 0:
        return
    reason = normalize_extend_reason(reason)
    now = time.time()
    current = _account_next_available.get(token, now)
    _account_next_available[token] = max(current, now + seconds)
    _extend_reasons[reason] = _extend_reasons.get(reason, 0) + 1
    logger.info(f"[antiban] cooldown extended {anon_id(token)} reason={reason} +{seconds}s")


def get_next_available(token: str) -> float:
    return _account_next_available.get(token, 0.0)
