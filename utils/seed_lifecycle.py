"""Transactional SaaS Seed freezing and capacity-checked reactivation.

SQLite is authoritative. All reads, the targeted write and subsequent memory
publication use one store write lock, with no nested DAO calls. A publication
failure raises StoreError after the DB commit; retry reconciles the memory entry
from persisted state. No transition deletes or reassigns conversation history.
The explicit capacity counts bound active/trial Seeds, not concurrent requests.
"""
from contextlib import closing
import time

from utils import store
from utils.entitlements import _order_window, _TIER_RANK
from utils.plans import parse_plan_id
from utils.store import StoreError


class LifecycleDenied(Exception):
    """An explicit transition denial; reason is an anonymous fixed code."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _entitlement(conn, email, seed, now):
    columns = ('order_id', 'email', 'tier_id', 'amount', 'status',
               'expires_at', 'created_at', 'updated_at')
    rows = conn.execute(
        'SELECT ' + ', '.join(columns) + ' FROM orders WHERE email=?', (email,)
    ).fetchall()
    paid = [dict(zip(columns, row)) for row in rows if row[4] == 'paid']
    tier, density = '', 'shared'
    for order in paid:
        window = _order_window(order)
        if not window or window[2] <= now:
            continue
        parsed = parse_plan_id(order['tier_id'])
        order_density = parsed[1] if parsed else 'shared'
        if _TIER_RANK.get(window[0], 0) > _TIER_RANK.get(tier, 0):
            tier, density = window[0], order_density
        elif window[0] == tier and order_density == 'solo':
            density = 'solo'
    if tier or paid:
        return tier, 'active', density
    grant = conn.execute(
        'SELECT tier, total, used FROM trial_grants WHERE email=? AND seed=?',
        (email, seed),
    ).fetchone()
    if grant and grant[0] == 'plus' and grant[1] - grant[2] > 0:
        return 'plus', 'trial', 'shared'
    return '', '', 'shared'


def _fleet_health():
    """The one account state machine. Lazy: fleet_health imports routing at import.

    Every caller must resolve this **before** taking ``store._WRITE_LOCK``
    (see ``_transition``): importing a module graph while holding that
    non-reentrant lock can deadlock if the graph itself writes to SQLite.
    """
    from utils import fleet_health
    return fleet_health


def _candidate_denial(conn, account, tier, seed, capacity, density, now):
    row = conn.execute('SELECT plan_type, status FROM accounts WHERE token=?', (account,)).fetchone()
    if row is None:
        return 'account_unknown'
    # One state machine for the panel and the routing gate: circuit/persisted dead,
    # manual disable, degraded, error-listed and unproven rows are all non-routable
    # here, exactly as the operator view reports them.
    health = _fleet_health()
    if health.resolve_account_status(account, row[1]) != health.STATUS_HEALTHY:
        return 'account_not_healthy'
    if row[0] != tier:
        return 'cross_tier'
    peers = conn.execute(
        "SELECT u.seed, a.email FROM users u LEFT JOIN user_auth a ON a.seed=u.seed "
        "WHERE u.current_account=? AND u.status IN ('active','trial') AND u.seed!=?",
        (account, seed),
    ).fetchall()
    if len(peers) >= capacity:
        return 'capacity_exceeded'
    if density == 'solo' and peers:
        return 'exclusive_conflict'
    for peer_seed, email in peers:
        if email:
            peer_tier, _, peer_density = _entitlement(conn, email, peer_seed, now)
            if peer_tier and peer_density == 'solo':
                return 'exclusive_conflict'
    return None


def _publish(seed, row, history):
    from utils import globals
    existing = globals.seed_map.get(seed)
    entry = dict(existing) if isinstance(existing, dict) else {}
    entry.update(status=row[0], token=row[1] or '', plan_type=row[2], conversations=history)
    globals.seed_map[seed] = entry


def _transition(seed, candidate=None, capacity=None, now=None, *, route=False, force_switch=False):
    if not isinstance(seed, str) or not seed:
        raise LifecycleDenied('invalid_args')
    if candidate is not None and (
        not isinstance(candidate, str) or not candidate
        or type(capacity) is not int or capacity < 1
    ):
        raise LifecycleDenied('invalid_args')
    if route and (type(capacity) is not int or capacity < 0):
        raise LifecycleDenied('capacity_unconfigured')
    activating = route or candidate is not None
    now = int(time.time()) if now is None else int(now)
    # Prewarm the state-machine import outside the write lock; the lock is not
    # reentrant, so a first-import inside the transaction can deadlock.
    _fleet_health()
    try:
        with store._WRITE_LOCK, closing(store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                auth = conn.execute('SELECT email, status FROM user_auth WHERE seed=?', (seed,)).fetchone()
                if auth is None:
                    raise LifecycleDenied('operator_seed')
                row = conn.execute(
                    'SELECT status, current_account, plan_type FROM users WHERE seed=?', (seed,)
                ).fetchone()
                if row is None:
                    raise StoreError('Seed state unavailable')
                status, account, plan = row
                if activating and auth[1] != 'active':
                    raise LifecycleDenied('auth_not_active')
                tier, new_status, density = (
                    _entitlement(conn, auth[0], seed, now)
                    if auth[1] == 'active' else ('', '', 'shared')
                )
                if not activating:
                    if auth[1] == 'active' and not tier:
                        status = 'frozen'
                else:
                    if not tier:
                        raise LifecycleDenied('no_entitlement')
                    # Original first, then caller-supplied same-tier alternative.
                    if route:
                        capacity = 1 if density == 'solo' else capacity
                        if capacity < 1:
                            raise LifecycleDenied('capacity_unconfigured')
                        # Fill existing eligible accounts before opening an empty
                        # one, within the explicit bound. Original stays first.
                        pool = [r[0] for r in conn.execute(
                            "SELECT a.token FROM accounts a LEFT JOIN users u "
                            "ON u.current_account=a.token AND u.status IN ('active','trial') "
                            "WHERE a.plan_type=? AND a.status='healthy' GROUP BY a.token "
                            "ORDER BY COUNT(u.seed) DESC, a.token", (tier,),
                        ).fetchall()]
                        candidates = ([a for a in pool if a != account] if force_switch
                                      else list(dict.fromkeys(a for a in [account, *pool] if a)))
                    else:
                        candidates = list(dict.fromkeys(a for a in (account, candidate) if a))
                    # Route candidates come from the tier pool, so an exhausted pool is
                    # denied as such instead of borrowing the "unknown account" code:
                    # the two failures need different operator responses.
                    reason = 'no_healthy_candidate' if route else 'account_unknown'
                    for target in candidates:
                        reason = _candidate_denial(conn, target, tier, seed, capacity, density, now)
                        if reason is None:
                            account, status, plan = target, new_status, tier
                            break
                    else:
                        raise LifecycleDenied(reason)
                if (status, account, plan) != row:
                    result = conn.execute(
                        'UPDATE users SET status=?, current_account=?, plan_type=?, updated_at=? WHERE seed=?',
                        (status, account, plan, now, seed),
                    )
                    if result.rowcount != 1:
                        raise StoreError('Seed transition did not persist')
                history = [r[0] for r in conn.execute(
                    'SELECT conv_id FROM conversations WHERE seed=? ORDER BY update_time DESC, rowid DESC',
                    (seed,),
                ).fetchall()]
                conn.execute('COMMIT')
                # Still under the write lock. Failure is explicit; the committed
                # DB state is retained and an idempotent retry republishes it.
                _publish(seed, (status, account, plan), history)
                return status, plan, account
            except BaseException:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
                raise
    except (LifecycleDenied, StoreError):
        raise
    except Exception as exc:
        raise StoreError('Seed lifecycle persistence or publication unavailable') from exc


def freeze_if_expired(seed, now=None):
    """Freeze lapsed SaaS entitlement; never unfreeze a renewed Seed implicitly."""
    return _transition(seed, now=now)[0]


def activate_seed(seed, candidate_account, max_active_seeds, now=None):
    """Prefer original healthy same-tier capacity; fall back only to the candidate.

    Returns (status, tier). LifecycleDenied leaves the binding and status
    unchanged. StoreError may follow a committed DB write if memory publication
    fails; an idempotent retry reconciles it. Operator Seeds are outside this
    SaaS lifecycle.
    """
    if candidate_account is None:
        raise LifecycleDenied('invalid_args')
    return _transition(seed, candidate_account, max_active_seeds, now)[:2]


def route_seed(seed, max_shared_seeds, *, force_switch=False, now=None):
    """Select and activate atomically, returning the committed account.

    Shared/trial capacity must be explicitly configured; solo has one binding.
    Forced switching excludes the original. Denial never rewrites its history.
    """
    return _transition(seed, capacity=max_shared_seeds, now=now,
                       route=True, force_switch=force_switch)[2]


def freeze_expired_seeds(now=None):
    """Reconcile idle registered Seeds; one failed Seed does not stop the scan.

    Each Seed is rechecked inside its transaction, so a renewal during the scan
    is not frozen using stale order data. This never activates a frozen Seed.
    """
    from utils.Logger import logger
    now = int(time.time()) if now is None else int(now)
    try:
        with closing(store._connect()) as conn:
            rows = conn.execute(
                "SELECT a.seed, u.seed IS NULL FROM user_auth a LEFT JOIN users u ON u.seed=a.seed "
                "WHERE a.status='active' AND a.seed IS NOT NULL"
            ).fetchall()
    except Exception as exc:
        raise StoreError('Seed expiry scan unavailable') from exc
    result = {'checked': 0, 'frozen': 0, 'errors': 0}
    for seed, missing_user in rows:
        try:
            if missing_user:
                # Repair only during startup/scheduled reconciliation. Direct
                # activation remains fail-closed when its durable state is
                # absent. Recovered rows start frozen and cannot receive work
                # until a normal route transition revalidates all constraints.
                with store._WRITE_LOCK, closing(store._connect()) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    auth = conn.execute(
                        'SELECT email, status FROM user_auth WHERE seed=?', (seed,)
                    ).fetchone()
                    present = conn.execute('SELECT 1 FROM users WHERE seed=?', (seed,)).fetchone()
                    if auth and auth[1] == 'active' and not present:
                        tier, _, _ = _entitlement(conn, auth[0], seed, now)
                        from utils import globals
                        memory = globals.seed_map.get(seed)
                        account = memory.get('token', '') if isinstance(memory, dict) else ''
                        conn.execute(
                            'INSERT INTO users '
                            '(seed, plan_type, current_account, status, created_at, updated_at) '
                            'VALUES (?, ?, ?, ?, ?, ?)',
                            (seed, tier, account, 'frozen', now, now),
                        )
                    conn.execute('COMMIT')
            status = freeze_if_expired(seed, now)
            result['checked'] += 1
            result['frozen'] += status == 'frozen'
        except (LifecycleDenied, StoreError):
            result['errors'] += 1
            logger.error('[lifecycle] Seed expiry reconciliation unavailable')
    return result
