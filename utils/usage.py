"""In-memory usage counters + periodic flush (fleet 用量统计).

Usage is counted in memory (per seed + per account, double granularity) on the
reverse-proxy hot path and flushed to ``usage_events`` on a schedule, so a single
request never touches SQLite. Aggregated reads go straight to SQLite via store.
"""
import threading
import time

import utils.store as store
from utils.Logger import logger

_pending = []  # list of (seed, account, kind, created_at)
_lock = threading.Lock()


def record_usage(seed, account, kind) -> None:
    """Record one usage event (called from the reverse proxy success path)."""
    if not seed and not account:
        return
    if not kind:
        return
    with _lock:
        _pending.append((seed, account, kind, int(time.time())))


def flush_usage() -> int:
    """Flush pending usage events to SQLite and reset the in-memory buffer."""
    global _pending
    with _lock:
        batch = _pending
        _pending = []
    if not batch:
        return 0
    store.add_usage_events(batch)
    logger.info(f"[usage] flushed {len(batch)} events")
    return len(batch)


def pending_count() -> int:
    """Current in-memory pending count (for tests / diagnostics)."""
    with _lock:
        return len(_pending)


def user_usage(seed, since=0) -> int:
    """Aggregated usage count for a seed."""
    return store.query_usage_count(seed=seed, since=since)


def account_usage(account, since=0) -> int:
    """Aggregated usage count for an account."""
    return store.query_usage_count(account=account, since=since)
