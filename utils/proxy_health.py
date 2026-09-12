"""Per-proxy-node health tracking for weighted node selection.

Maintains a latency EWMA plus a time-windowed failure count per proxy URL, so
``weighted_choice`` can prefer fast, healthy nodes and downweight slow or
failing ones.  This replaces the uniform ``random.choice`` node pick in
``chatgpt/fp.py`` (and a few other call sites) with a health-aware pick.

Design notes:
- Health is ephemeral (in-memory only).  A restart starts fresh, which is fine
  because node quality drifts over time anyway.
- Failures are forgotten after ``_WINDOW`` seconds (slow-start recovery): a node
  that stops failing regains weight on its own, with no re-entry probe.
- ``_FLOOR`` keeps a minimum weight so no node is ever fully starved; this
  prevents the avalanche where all traffic collapses onto a single surviving
  node and immediately overloads it.
- Unknown nodes get full weight (1.0) so freshly added nodes are adopted right
  away — matching the operator flow of "swap in a faster node and have it take
  traffic immediately".
- Node keys are the *resolved* proxy URL (after any ``{}`` session-id fill).
  The current fleet uses fixed-node ``socks5h://`` URLs without ``{}``, so the
  key equals the template in ``configs.proxy_url_list`` and lookups line up.
"""

import random
import threading
import time

_WINDOW = 300.0        # seconds; failures older than this are forgotten
_ALPHA = 0.2           # EWMA smoothing factor for latency
_FLOOR = 0.05          # minimum selection weight (never fully excluded)
_FAIL_PENALTY = 0.3    # weight loss per recent failure (up to 3 counted)

# proxy_url -> {"ema": float|None, "fails": [ts, ...], "last": float}
_health = {}
_lock = threading.Lock()


def record(proxy_url, ok, latency_ms=None):
    """Record one request outcome for a proxy node.

    ``ok`` False marks a transport failure (node down / timeout / TLS error);
    ``latency_ms`` feeds the latency EWMA.  Both may be recorded together: a
    timed-out request still carries a latency signal (the timeout duration).
    """
    if not proxy_url:
        return
    now = time.time()
    with _lock:
        e = _health.setdefault(proxy_url, {"ema": None, "fails": [], "last": 0.0})
        e["fails"] = [t for t in e["fails"] if now - t <= _WINDOW]
        if latency_ms is not None:
            v = float(latency_ms)
            e["ema"] = v if e["ema"] is None else _ALPHA * v + (1.0 - _ALPHA) * e["ema"]
        if not ok:
            e["fails"].append(now)
        e["last"] = now


def _weight(candidate, now, min_ema):
    e = _health.get(candidate)
    if e is None:
        return 1.0
    fails = len([t for t in e["fails"] if now - t <= _WINDOW])
    fail_factor = 1.0 - min(fails, 3) * _FAIL_PENALTY
    lat_factor = 1.0
    ema = e.get("ema")
    if ema and min_ema:
        # Relative latency: fastest observed node keeps full weight, slower
        # nodes scale down proportionally (min_ema / own_ema).
        lat_factor = min_ema / max(ema, 1.0)
    return max(fail_factor * lat_factor, _FLOOR)


def weighted_choice(candidates):
    """Pick a node from ``candidates`` with probability proportional to health."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    now = time.time()
    with _lock:
        emas = []
        for c in candidates:
            e = _health.get(c)
            if e and e.get("ema"):
                emas.append(e["ema"])
        min_ema = min(emas) if emas else None
        items = [(c, _weight(c, now, min_ema)) for c in candidates]
    total = sum(w for _, w in items)
    if total <= 0:
        return random.choice(candidates)
    r = random.random() * total
    acc = 0.0
    for c, w in items:
        acc += w
        if r <= acc:
            return c
    return items[-1][0]
