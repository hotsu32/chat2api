"""每号并发上限（出租率控制）。

核心不变量：
  1. 一个账号 token 同一时刻最多 N 个在飞请求（active SSE stream）；
  2. N 由账号 plan_type 分层：free 号多共享（廉价可弃），plus/paid 号少共享（保护）；
  3. 超上限先排队（等 slot 释放），超过 max_wait 仍无 slot 则返回 False（上游 failover 信号）；
  4. 槽位按「租约」归属单个请求：同一租约重复释放是 no-op，不会放掉别的在飞请求的槽位。

与 cooldown 的区别：cooldown 是「最小间隔限流」（两次请求之间至少间隔 X 秒），
本模块是「并发上限」（同时最多 N 个在飞流）。二者正交。

边界：信号量是单进程内存状态。多 Worker 部署时每个 Worker 各自持有一份上限，
总在飞量为 workers × N；需要全局硬上限时必须换共享后端（见 docs 边界说明）。
"""

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Dict, Optional

from utils import configs
from utils.Logger import logger

# BoundedSemaphore：多释放会抛 ValueError，而不是静默把容量放大到上限之上。
_account_semaphores: Dict[str, asyncio.BoundedSemaphore] = {}
_account_limits: Dict[str, int] = {}


def anon_id(token: str) -> str:
    """账号的匿名标识。用于日志和指标。

    token 前缀不算脱敏（前缀可直接比对/关联账号），因此这里用不可逆摘要。
    同一 token 稳定，便于按号排查；不同 token 不同；无法反推原值。
    """
    if not token:
        return "anon:none"
    return "anon:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


@dataclass
class Lease:
    """一个请求对某账号槽位的持有凭证。

    持有信号量对象本身而不是 token：释放时归还的必须是当初占用的那一个。
    账号改档或热重载会给同一 token 换新信号量，按 token 查表释放会把容量灌进
    新信号量（凭空 +1）。释放后置 released，重复释放为 no-op。
    """
    token: str
    semaphore: Optional[asyncio.BoundedSemaphore] = None
    released: bool = False


def _resolve_limit(token: str, persona: Optional[str] = None) -> int:
    """free 号 → 高并发（廉价可弃）；其余 → 低并发（保护）。"""
    if persona == "chatgpt-freeaccount":
        return configs.free_account_max_concurrency
    # persona 在 acquire 阶段通常未知（chat-requirements 在 acquire 之后），用 plan_type 兜底
    try:
        from utils import store as _store
        acct = _store.get_account(token)
        if acct and str(acct.get("plan_type") or "").lower() == "free":
            return configs.free_account_max_concurrency
    except Exception:
        pass
    return configs.account_max_concurrency


def _get_semaphore(token: str, persona: Optional[str] = None) -> asyncio.BoundedSemaphore:
    sem = _account_semaphores.get(token)
    if sem is None:
        limit = _resolve_limit(token, persona)
        sem = asyncio.BoundedSemaphore(limit)
        _account_semaphores[token] = sem
        _account_limits[token] = limit
        logger.info(f"[antiban][concurrency] {anon_id(token)} limit={limit}")
    return sem


async def acquire(token: str, persona: Optional[str] = None, max_wait: Optional[float] = None) -> bool:
    """占一个并发槽位。空闲立即成功；满则等至多 max_wait 秒；仍满返回 False。

    兼容旧调用方（按 token 释放）。新代码请用 acquire_lease/release_lease。
    """
    return await acquire_lease(token, persona, max_wait) is not None


async def acquire_lease(
    token: str, persona: Optional[str] = None, max_wait: Optional[float] = None
) -> Optional[Lease]:
    """占一个并发槽位并返回租约；超时未拿到返回 None。

    antiban 关闭或无 token 时返回一个已标记 released 的空租约：调用方一律放行，
    释放时也不会误动任何信号量。
    """
    if not configs.enable_antiban or not token:
        return Lease(token=token or "", released=True)
    sem = _get_semaphore(token, persona)
    wait = max_wait if max_wait is not None else configs.account_concurrency_wait_seconds
    try:
        await asyncio.wait_for(sem.acquire(), timeout=wait)
        return Lease(token=token, semaphore=sem)
    except asyncio.TimeoutError:
        logger.warning(
            f"[antiban][concurrency] {anon_id(token)} at limit "
            f"{_account_limits.get(token, 0)}, waited {wait}s → failover signal"
        )
        return None


def release_lease(lease: Optional[Lease]) -> None:
    """按租约释放槽位。重复调用是 no-op，不会放掉别的请求持有的槽位。"""
    if lease is None or lease.released:
        return
    lease.released = True  # 先置位：即便 release 抛错也不会被重复计入
    _release_semaphore(lease.semaphore, lease.token)


def release(token: str) -> None:
    """释放一个并发槽位（旧调用路径，按 token 查表）。

    只适用于「占用与释放之间信号量未被替换」的场景；新代码用 release_lease。
    """
    if not token:
        return
    _release_semaphore(_account_semaphores.get(token), token)


def _release_semaphore(sem: Optional[asyncio.BoundedSemaphore], token: str) -> None:
    if sem is None:
        return
    try:
        sem.release()
    except ValueError:
        # BoundedSemaphore：释放次数超过占用次数。说明有调用方多放了一次，
        # 吞掉即可——关键是容量不会被放大到上限之上。
        logger.warning(f"[antiban][concurrency] {anon_id(token)} over-release ignored")
