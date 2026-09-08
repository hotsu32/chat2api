"""每号并发上限（出租率控制）。

核心不变量：
  1. 一个账号 token 同一时刻最多 N 个在飞请求（active SSE stream）；
  2. N 由账号 plan_type 分层：free 号多共享（廉价可弃），plus/paid 号少共享（保护）；
  3. 超上限先排队（等 slot 释放），超过 max_wait 仍无 slot 则返回 False（上游 failover 信号）。

与 cooldown 的区别：cooldown 是「最小间隔限流」（两次请求之间至少间隔 X 秒），
本模块是「并发上限」（同时最多 N 个在飞流）。二者正交。
"""

import asyncio
from typing import Dict, Optional

from utils import configs
from utils.Logger import logger

_account_semaphores: Dict[str, asyncio.Semaphore] = {}
_account_limits: Dict[str, int] = {}


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


def _get_semaphore(token: str, persona: Optional[str] = None) -> asyncio.Semaphore:
    sem = _account_semaphores.get(token)
    if sem is None:
        limit = _resolve_limit(token, persona)
        sem = asyncio.Semaphore(limit)
        _account_semaphores[token] = sem
        _account_limits[token] = limit
        logger.info(f"[antiban][concurrency] token={token[:12]}... limit={limit}")
    return sem


async def acquire(token: str, persona: Optional[str] = None, max_wait: Optional[float] = None) -> bool:
    """占一个并发槽位。空闲立即成功；满则等至多 max_wait 秒；仍满返回 False。"""
    if not configs.enable_antiban or not token:
        return True
    sem = _get_semaphore(token, persona)
    wait = max_wait if max_wait is not None else configs.account_concurrency_wait_seconds
    try:
        await asyncio.wait_for(sem.acquire(), timeout=wait)
        return True
    except asyncio.TimeoutError:
        logger.warning(
            f"[antiban][concurrency] token={token[:12]}... at limit "
            f"{_account_limits.get(token, 0)}, waited {wait}s → failover signal"
        )
        return False


def release(token: str) -> None:
    """释放一个并发槽位。幂等，异常吞掉（释放失败不应影响主流程）。"""
    if not token:
        return
    sem = _account_semaphores.get(token)
    if sem is None:
        return
    try:
        sem.release()
    except ValueError:
        pass  # 已释放到上限，忽略
