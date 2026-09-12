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


def user_usage_total(seed, since=0) -> int:
    """落库 + 未 flush 的内存 pending 之和（额度执行的准确读数）。"""
    db = store.query_usage_count(seed=seed, since=since)
    with _lock:
        pending = sum(1 for s, _a, _k, t in _pending if s == seed and t >= since)
    return db + pending


def user_events(seed, since=0, limit=500):
    """Return bounded seed-scoped events from SQLite plus unflushed memory.

    The page only needs ``kind`` and ``created_at``. Keep the internal seed and
    account identifiers inside this module so presentation code cannot leak them.
    """
    if not seed or limit <= 0:
        return []
    limit = min(int(limit), 500)
    events = store.query_seed_usage(seed=seed, since=since, limit=limit)
    with _lock:
        events.extend(
            {"kind": kind, "created_at": created_at}
            for event_seed, _account, kind, created_at in _pending
            if event_seed == seed and created_at >= since
        )
    events.sort(key=lambda event: event["created_at"], reverse=True)
    return events[:limit]


def user_daily_usage(seed, since=0):
    """Return local-date and kind usage counts, including unflushed events."""
    if not seed:
        return []
    groups = {
        (event["date"], event["kind"]): event["count"]
        for event in store.query_seed_usage_daily(seed=seed, since=since)
    }
    with _lock:
        pending = [
            (kind, created_at)
            for event_seed, _account, kind, created_at in _pending
            if event_seed == seed and created_at >= since
        ]
    for kind, created_at in pending:
        date = time.strftime("%Y-%m-%d", time.localtime(created_at))
        key = (date, kind)
        groups[key] = groups.get(key, 0) + 1
    return [
        {"date": date, "kind": kind, "count": count}
        for (date, kind), count in sorted(groups.items(), reverse=True)
    ]


def account_usage(account, since=0) -> int:
    """Aggregated usage count for an account."""
    return store.query_usage_count(account=account, since=since)
