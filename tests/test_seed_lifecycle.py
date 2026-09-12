"""Isolated RED/GREEN tests for utils.seed_lifecycle.

All tests drive a real file-backed SQLite DB through the actual store._connect
seam — no row_factory, rows accessed by column index, matching production.
Each test redirects store._db_path to a fresh tmp file, calls init_db() to
create the canonical schema, then exercises freeze_if_expired / activate_seed.
globals.seed_map is patched to an empty dict so memory writes are visible.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

import utils.store as _store
import utils.globals as _globals
from utils.seed_lifecycle import LifecycleDenied, activate_seed, freeze_if_expired, freeze_expired_seeds
from utils.store import StoreError

# ---------------------------------------------------------------------------
# Deterministic timestamps
# ---------------------------------------------------------------------------
NOW = 1_700_000_000
ACTIVE_EXPIRES  = NOW + 86400 * 20   # 20 days from now (active)
EXPIRED_EXPIRES = NOW - 86400 * 10   # 10 days ago (expired)
ACTIVE_CREATED  = NOW - 86400 * 10

# ---------------------------------------------------------------------------
# Fixture: isolated temp DB via _db_path patch + real init_db
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """Redirect store to a fresh temp DB; yield a connection factory for setup."""
    db_path = str(tmp_path / "test.db")

    seed_map_orig = dict(_globals.seed_map)
    _globals.seed_map.clear()

    with patch.object(_store, "_db_path", return_value=db_path):
        # Reset the initialized flag so init_db runs on this new path.
        _store._INITIALIZED = False
        _store.init_db()

        def _conn():
            c = sqlite3.connect(db_path, timeout=5, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            return c

        yield _conn

    _globals.seed_map.clear()
    _globals.seed_map.update(seed_map_orig)
    _store._INITIALIZED = False


# ---------------------------------------------------------------------------
# Row-index helpers (columns match init_db schema)
# ---------------------------------------------------------------------------

def _insert_user(conn, seed, status="active", current_account="", plan_type="plus"):
    conn.execute(
        "INSERT OR REPLACE INTO users "
        "(seed, plan_type, current_account, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (seed, plan_type, current_account, status, NOW - 100, NOW - 100),
    )


def _insert_user_auth(conn, seed, email, status="active"):
    conn.execute(
        "INSERT OR REPLACE INTO user_auth "
        "(email, seed, status, created_at, updated_at) VALUES (?,?,?,?,?)",
        (email, seed, status, NOW - 100, NOW - 100),
    )


def _insert_paid_order(conn, email, tier_id, expires_at, order_id=None):
    oid = order_id or f"ord_{email}_{tier_id}"
    conn.execute(
        "INSERT OR REPLACE INTO orders "
        "(order_id, email, tier_id, amount, status, expires_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (oid, email, tier_id, "1000", "paid", expires_at, ACTIVE_CREATED, NOW - 1),
    )


def _insert_trial_grant(conn, email, seed, total=3, used=0, tier="plus"):
    conn.execute(
        "INSERT OR REPLACE INTO trial_grants "
        "(email, seed, tier, total, used, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (email, seed, tier, total, used, NOW - 100, NOW - 100),
    )


def _insert_account(conn, token, plan_type="plus", status="healthy"):
    conn.execute(
        "INSERT OR REPLACE INTO accounts "
        "(token, plan_type, status, created_at, updated_at) VALUES (?,?,?,?,?)",
        (token, plan_type, status, NOW - 100, NOW - 100),
    )


def _get_user(conn, seed):
    row = conn.execute("SELECT * FROM users WHERE seed=?", (seed,)).fetchone()
    if row is None:
        return None
    # users: seed=0, plan_type=1, current_account=2, status=3
    return {"seed": row[0], "plan_type": row[1], "current_account": row[2], "status": row[3]}


# ---------------------------------------------------------------------------
# freeze_if_expired
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('operation', ['freeze', 'activate'])
def test_missing_memory_rebuilt_with_history(db, operation):
    with db() as conn:
        _insert_user(conn, 'restore', current_account='original')
        _insert_user_auth(conn, 'restore', 'restore@example.test')
        _insert_paid_order(conn, 'restore@example.test', 'plus-solo-1m',
                           EXPIRED_EXPIRES if operation == 'freeze' else ACTIVE_EXPIRES)
        _insert_account(conn, 'original')
        conn.execute("INSERT INTO conversations(conv_id, seed, account) VALUES ('history', 'restore', 'original')")
    if operation == 'freeze':
        freeze_if_expired('restore', now=NOW)
    else:
        activate_seed('restore', 'original', 2, now=NOW)
    entry = _globals.seed_map['restore']
    assert entry['token'] == 'original'
    assert entry['plan_type'] == 'plus'
    assert entry['status'] == ('frozen' if operation == 'freeze' else 'active')
    assert entry['conversations'] == ['history']


def test_startup_reconciliation_recovers_legacy_auth_without_user_row_as_frozen(db):
    with db() as conn:
        _insert_user_auth(conn, 'legacy', 'legacy@example.test')
        _insert_paid_order(conn, 'legacy@example.test', 'plus-shared-1m', ACTIVE_EXPIRES)
    _globals.seed_map['legacy'] = {'token': 'remembered-account', 'conversations': ['legacy-history']}

    result = freeze_expired_seeds(now=NOW)

    assert result == {'checked': 1, 'frozen': 1, 'errors': 0}
    with db() as conn:
        assert _get_user(conn, 'legacy') == {
            'seed': 'legacy', 'plan_type': 'plus',
            'current_account': 'remembered-account', 'status': 'frozen',
        }
    assert _globals.seed_map['legacy']['status'] == 'frozen'
    assert _globals.seed_map['legacy']['conversations'] == []


@pytest.mark.parametrize('grant_seed,grant_tier', [('stale-seed', 'plus'), ('trial', 'pro')])
def test_stale_or_wrong_tier_grant_cannot_activate(db, grant_seed, grant_tier):
    with db() as conn:
        _insert_user(conn, 'trial', status='frozen')
        _insert_user_auth(conn, 'trial', 'trial@example.test')
        _insert_trial_grant(conn, 'trial@example.test', grant_seed, tier=grant_tier)
        _insert_account(conn, 'candidate', plan_type=grant_tier)
    with pytest.raises(LifecycleDenied):
        activate_seed('trial', 'candidate', 2, now=NOW)
    with db() as conn:
        assert _get_user(conn, 'trial')['status'] == 'frozen'


def test_original_full_uses_same_tier_candidate(db):
    with db() as conn:
        _insert_user(conn, 'renew', status='frozen', current_account='original')
        _insert_user(conn, 'busy', current_account='original')
        _insert_user_auth(conn, 'renew', 'renew@example.test')
        _insert_paid_order(conn, 'renew@example.test', 'plus-solo-1m', ACTIVE_EXPIRES)
        _insert_account(conn, 'original')
        _insert_account(conn, 'candidate')
    activate_seed('renew', 'candidate', 1, now=NOW)
    with db() as conn:
        assert _get_user(conn, 'renew')['current_account'] == 'candidate'


@pytest.mark.parametrize('operation', ['freeze', 'activate'])
def test_connection_failure_is_store_error(db, monkeypatch, operation):
    def unavailable():
        raise sqlite3.OperationalError('synthetic connection detail')
    monkeypatch.setattr(_store, '_connect', unavailable)
    with pytest.raises(StoreError):
        if operation == 'freeze':
            freeze_if_expired('seed', now=NOW)
        else:
            activate_seed('seed', 'candidate', 1, now=NOW)


def test_boolean_capacity_is_not_an_operator_limit(db):
    with pytest.raises(LifecycleDenied) as error:
        activate_seed('seed', 'candidate', True, now=NOW)
    assert error.value.reason == 'invalid_args'


@pytest.mark.parametrize('operation', ['freeze', 'activate'])
def test_sql_failure_rolls_back_without_memory_changes(db, operation):
    with db() as conn:
        _insert_user(conn, 'rollback', status='active' if operation == 'freeze' else 'frozen', current_account='original')
        _insert_user_auth(conn, 'rollback', 'rollback@example.test')
        _insert_paid_order(conn, 'rollback@example.test', 'plus-solo-1m',
                           EXPIRED_EXPIRES if operation == 'freeze' else ACTIVE_EXPIRES)
        _insert_account(conn, 'original')
        conn.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON users BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    _globals.seed_map['rollback'] = {'token': 'original', 'status': 'before', 'conversations': ['history']}
    before = dict(_globals.seed_map['rollback'])
    with pytest.raises(StoreError):
        if operation == 'freeze':
            freeze_if_expired('rollback', now=NOW)
        else:
            activate_seed('rollback', 'original', 1, now=NOW)
    assert _globals.seed_map['rollback'] == before
    with db() as conn:
        assert _get_user(conn, 'rollback')['status'] == ('active' if operation == 'freeze' else 'frozen')


@pytest.mark.parametrize('operation', ['freeze', 'activate'])
def test_publish_failure_is_explicit_and_retry_reconciles(db, monkeypatch, caplog, operation):
    with db() as conn:
        _insert_user(conn, 'publish', status='active' if operation == 'freeze' else 'frozen', current_account='original')
        _insert_user_auth(conn, 'publish', 'publish@example.test')
        _insert_paid_order(conn, 'publish@example.test', 'plus-solo-1m',
                           EXPIRED_EXPIRES if operation == 'freeze' else ACTIVE_EXPIRES)
        _insert_account(conn, 'original')
    class RejectPublish(dict):
        def __setitem__(self, key, value):
            raise RuntimeError('synthetic-private-detail')
    monkeypatch.setattr(_globals, 'seed_map', RejectPublish())
    def transition():
        return (freeze_if_expired('publish', now=NOW) if operation == 'freeze'
                else activate_seed('publish', 'original', 1, now=NOW))
    with pytest.raises(StoreError):
        transition()
    assert 'synthetic-private-detail' not in caplog.text
    monkeypatch.setattr(_globals, 'seed_map', {})
    transition()
    assert _globals.seed_map['publish']['status'] == ('frozen' if operation == 'freeze' else 'active')

@pytest.mark.parametrize('incoming', ['solo', 'shared', 'trial'])
def test_solo_binding_cannot_be_shared_even_below_capacity(db, incoming):
    with db() as conn:
        _insert_account(conn, 'exclusive')
        _insert_user(conn, 'owner', current_account='exclusive')
        _insert_user_auth(conn, 'owner', 'owner@example.test')
        _insert_paid_order(conn, 'owner@example.test', 'plus-solo-1m', ACTIVE_EXPIRES)
        _insert_user(conn, 'new', status='frozen')
        _insert_user_auth(conn, 'new', 'new@example.test')
        if incoming == 'trial':
            _insert_trial_grant(conn, 'new@example.test', 'new')
        else:
            _insert_paid_order(conn, 'new@example.test', f'plus-{incoming}-1m', ACTIVE_EXPIRES)
    with pytest.raises(LifecycleDenied):
        activate_seed('new', 'exclusive', 10, now=NOW)
    with db() as conn:
        assert _get_user(conn, 'new')['status'] == 'frozen'
        assert _get_user(conn, 'new')['current_account'] == ''


def test_solo_renewal_moves_from_shared_account_to_empty_candidate(db):
    with db() as conn:
        for account in ('shared', 'empty'):
            _insert_account(conn, account)
        _insert_user(conn, 'peer', current_account='shared')
        _insert_user(conn, 'renew', status='frozen', current_account='shared')
        _insert_user_auth(conn, 'renew', 'renew@example.test')
        _insert_paid_order(conn, 'renew@example.test', 'plus-solo-1m', ACTIVE_EXPIRES)
        conn.execute("INSERT INTO conversations(conv_id, seed, account) VALUES ('kept', 'renew', 'shared')")
    activate_seed('renew', 'empty', 10, now=NOW)
    assert _globals.seed_map['renew']['token'] == 'empty'
    assert _globals.seed_map['renew']['conversations'] == ['kept']
    with db() as conn:
        assert conn.execute("SELECT account FROM conversations WHERE conv_id='kept'").fetchone()[0] == 'shared'


@pytest.mark.parametrize('peer_plan', ['plus-shared-1m', 'plus'])
def test_shared_and_legacy_peers_allow_shared_binding(db, peer_plan):
    with db() as conn:
        _insert_account(conn, 'shared')
        for seed in ('peer', 'new'):
            _insert_user(conn, seed, current_account='shared' if seed == 'peer' else '')
            _insert_user_auth(conn, seed, f'{seed}@example.test')
            _insert_paid_order(conn, f'{seed}@example.test', peer_plan if seed == 'peer' else 'plus-shared-1m', ACTIVE_EXPIRES)
    activate_seed('new', 'shared', 2, now=NOW)
    assert _globals.seed_map['new']['token'] == 'shared'


@pytest.mark.parametrize('extra_plan,extra_expiry,allowed', [
    ('plus-solo-1m', ACTIVE_EXPIRES, False),
    ('plus-solo-1m', EXPIRED_EXPIRES, True),
    ('pro-shared-1m', ACTIVE_EXPIRES, True),
])
def test_exclusivity_uses_current_winning_tier_and_order_window(db, extra_plan, extra_expiry, allowed):
    with db() as conn:
        _insert_account(conn, 'account')
        for seed in ('peer', 'new'):
            _insert_user(conn, seed, current_account='account' if seed == 'peer' else '')
            _insert_user_auth(conn, seed, f'{seed}@example.test')
            _insert_paid_order(conn, f'{seed}@example.test', 'plus-shared-1m', ACTIVE_EXPIRES)
        # A lower-tier solo order does not reserve a higher-tier shared binding.
        if extra_plan.startswith('pro'):
            _insert_paid_order(conn, 'peer@example.test', 'plus-solo-1m', ACTIVE_EXPIRES)
        _insert_paid_order(conn, 'peer@example.test', extra_plan, extra_expiry)
        if extra_plan.startswith('pro'):
            conn.execute("UPDATE accounts SET plan_type='pro' WHERE token='account'")
            _insert_paid_order(conn, 'new@example.test', 'pro-shared-1m', ACTIVE_EXPIRES)
    if allowed:
        activate_seed('new', 'account', 10, now=NOW)
        assert _globals.seed_map['new']['token'] == 'account'
    else:
        with pytest.raises(LifecycleDenied):
            activate_seed('new', 'account', 10, now=NOW)


def test_solo_and_shared_race_cannot_share_account(db):
    with db() as conn:
        _insert_account(conn, 'account')
        for density in ('solo', 'shared'):
            _insert_user(conn, density, status='frozen')
            _insert_user_auth(conn, density, f'{density}@example.test')
            _insert_paid_order(conn, f'{density}@example.test', f'plus-{density}-1m', ACTIVE_EXPIRES)
    barrier = threading.Barrier(2)
    outcomes = []

    def activate(seed):
        barrier.wait(timeout=5)
        try:
            activate_seed(seed, 'account', 10, now=NOW)
            outcomes.append(('active', seed))
        except LifecycleDenied:
            outcomes.append(('denied', seed))

    threads = [threading.Thread(target=activate, args=(seed,)) for seed in ('solo', 'shared')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert sorted(state for state, _ in outcomes) == ['active', 'denied']
    winner = next(seed for state, seed in outcomes if state == 'active')
    with db() as conn:
        assert conn.execute("SELECT seed FROM users WHERE current_account='account'").fetchall() == [(winner,)]
    assert list(_globals.seed_map) == [winner]


class TestFreezeIfExpired:

    def test_operator_seed_raises(self, db):
        """No user_auth row → LifecycleDenied('operator_seed')."""
        conn = db()
        _insert_user(conn, "op1")
        conn.close()

        with pytest.raises(LifecycleDenied) as exc:
            freeze_if_expired("op1", now=NOW)
        assert exc.value.reason == "operator_seed"

    def test_active_paid_stays_active(self, db):
        """Unexpired paid order: status must stay 'active', never changed."""
        conn = db()
        _insert_user(conn, "s1", status="active", plan_type="plus")
        _insert_user_auth(conn, "s1", "u1@x.com")
        _insert_paid_order(conn, "u1@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        conn.close()

        result = freeze_if_expired("s1", now=NOW)
        assert result == "active"

        conn2 = db()
        assert _get_user(conn2, "s1")["status"] == "active"
        conn2.close()

    def test_active_paid_does_not_activate_frozen(self, db):
        """freeze_if_expired never activates a currently-frozen seed even if paid."""
        conn = db()
        _insert_user(conn, "s1b", status="frozen", plan_type="")
        _insert_user_auth(conn, "s1b", "u1b@x.com")
        _insert_paid_order(conn, "u1b@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        conn.close()

        result = freeze_if_expired("s1b", now=NOW)
        # Must return current status unchanged, not 'active'
        assert result == "frozen"

        conn2 = db()
        assert _get_user(conn2, "s1b")["status"] == "frozen"
        conn2.close()

    def test_expired_paid_freezes_preserves_account(self, db):
        """Expired paid order → frozen; current_account and plan_type preserved."""
        conn = db()
        _insert_user(conn, "s2", status="active", current_account="acct_old", plan_type="plus")
        _insert_user_auth(conn, "s2", "u2@x.com")
        _insert_paid_order(conn, "u2@x.com", "plus-solo-1m", EXPIRED_EXPIRES)
        conn.close()

        result = freeze_if_expired("s2", now=NOW)
        assert result == "frozen"

        conn2 = db()
        row = _get_user(conn2, "s2")
        conn2.close()
        assert row["status"] == "frozen"
        assert row["current_account"] == "acct_old"   # binding preserved
        assert row["plan_type"] == "plus"              # plan_type preserved

    def test_trial_user_stays_trial_status_unchanged(self, db):
        """Trial user with admission_balance > 0: returns current status unchanged."""
        conn = db()
        _insert_user(conn, "s3", status="trial", plan_type="plus")
        _insert_user_auth(conn, "s3", "u3@x.com")
        _insert_trial_grant(conn, "u3@x.com", "s3", total=3, used=1)
        conn.close()

        result = freeze_if_expired("s3", now=NOW)
        assert result == "trial"

    def test_trial_exhausted_freezes(self, db):
        """used >= total → frozen."""
        conn = db()
        _insert_user(conn, "s4", status="trial", plan_type="plus")
        _insert_user_auth(conn, "s4", "u4@x.com")
        _insert_trial_grant(conn, "u4@x.com", "s4", total=3, used=3)
        conn.close()

        result = freeze_if_expired("s4", now=NOW)
        assert result == "frozen"

    def test_banned_user_auth_leaves_status_unchanged(self, db):
        """Auth-banned seed: auth layer owns status; freeze_if_expired returns current."""
        conn = db()
        _insert_user(conn, "s5", status="active", plan_type="plus")
        _insert_user_auth(conn, "s5", "u5@x.com", status="banned")
        _insert_paid_order(conn, "u5@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        conn.close()

        result = freeze_if_expired("s5", now=NOW)
        assert result == "active"   # unchanged

        conn2 = db()
        assert _get_user(conn2, "s5")["status"] == "active"
        conn2.close()

    def test_already_frozen_stays_frozen(self, db):
        """Already-frozen seed stays frozen; no spurious UPDATE."""
        conn = db()
        _insert_user(conn, "s6", status="frozen", current_account="old_acct")
        _insert_user_auth(conn, "s6", "u6@x.com")
        _insert_paid_order(conn, "u6@x.com", "plus-solo-1m", EXPIRED_EXPIRES)
        conn.close()

        result = freeze_if_expired("s6", now=NOW)
        assert result == "frozen"

        conn2 = db()
        row = _get_user(conn2, "s6")
        conn2.close()
        assert row["status"] == "frozen"
        assert row["current_account"] == "old_acct"

    def test_missing_users_row_raises_store_error(self, db):
        """If there is no users row, freeze must raise StoreError (fail-closed)."""
        conn = db()
        # user_auth exists but no users row
        _insert_user_auth(conn, "smissing", "umissing@x.com")
        conn.close()

        with pytest.raises(StoreError):
            freeze_if_expired("smissing", now=NOW)


# ---------------------------------------------------------------------------
# activate_seed
# ---------------------------------------------------------------------------

class TestActivateSeed:

    def test_paid_activation_succeeds(self, db):
        """Paid user with matching plus account activates; DB and memory updated."""
        _globals.seed_map["a1"] = {"token": "", "plan_type": "", "conversations": []}

        conn = db()
        _insert_user(conn, "a1", status="frozen", plan_type="")
        _insert_user_auth(conn, "a1", "a1@x.com")
        _insert_paid_order(conn, "a1@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        _insert_account(conn, "acct1", plan_type="plus")
        conn.close()

        status, tier = activate_seed("a1", "acct1", max_active_seeds=5, now=NOW)
        assert status == "active"
        assert tier == "plus"

        conn2 = db()
        row = _get_user(conn2, "a1")
        conn2.close()
        assert row["status"] == "active"
        assert row["current_account"] == "acct1"
        assert row["plan_type"] == "plus"

        assert _globals.seed_map["a1"]["token"] == "acct1"
        assert _globals.seed_map["a1"]["plan_type"] == "plus"

    def test_trial_activation_returns_trial_status(self, db):
        """Trial seed (no paid, grant balance > 0) gets status='trial'."""
        conn = db()
        _insert_user(conn, "a2", status="frozen", plan_type="")
        _insert_user_auth(conn, "a2", "a2@x.com")
        _insert_trial_grant(conn, "a2@x.com", "a2", total=3, used=0)
        _insert_account(conn, "acct2", plan_type="plus")
        conn.close()

        status, tier = activate_seed("a2", "acct2", max_active_seeds=5, now=NOW)
        assert status == "trial"
        assert tier == "plus"

    def test_operator_seed_raises(self, db):
        """No user_auth → operator_seed."""
        conn = db()
        _insert_user(conn, "op2")
        _insert_account(conn, "aop")
        conn.close()

        with pytest.raises(LifecycleDenied) as exc:
            activate_seed("op2", "aop", max_active_seeds=5, now=NOW)
        assert exc.value.reason == "operator_seed"

    def test_banned_raises_auth_not_active(self, db):
        conn = db()
        _insert_user(conn, "a3", status="active")
        _insert_user_auth(conn, "a3", "a3@x.com", status="banned")
        _insert_paid_order(conn, "a3@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        _insert_account(conn, "acct3", plan_type="plus")
        conn.close()

        with pytest.raises(LifecycleDenied) as exc:
            activate_seed("a3", "acct3", max_active_seeds=5, now=NOW)
        assert exc.value.reason == "auth_not_active"

    def test_no_entitlement_stays_frozen(self, db):
        """Expired paid, no trial → no_entitlement; seed stays frozen."""
        conn = db()
        _insert_user(conn, "a4", status="frozen")
        _insert_user_auth(conn, "a4", "a4@x.com")
        _insert_paid_order(conn, "a4@x.com", "plus-solo-1m", EXPIRED_EXPIRES)
        _insert_account(conn, "acct4", plan_type="plus")
        conn.close()

        with pytest.raises(LifecycleDenied) as exc:
            activate_seed("a4", "acct4", max_active_seeds=5, now=NOW)
        assert exc.value.reason == "no_entitlement"

        conn2 = db()
        assert _get_user(conn2, "a4")["status"] == "frozen"
        conn2.close()

    def test_cross_tier_denied(self, db):
        """Plus-entitled seed cannot activate against a pro account."""
        conn = db()
        _insert_user(conn, "a5", status="frozen")
        _insert_user_auth(conn, "a5", "a5@x.com")
        _insert_paid_order(conn, "a5@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        _insert_account(conn, "pro_acct", plan_type="pro")
        conn.close()

        with pytest.raises(LifecycleDenied) as exc:
            activate_seed("a5", "pro_acct", max_active_seeds=5, now=NOW)
        assert exc.value.reason == "cross_tier"

    def test_capacity_exceeded_stays_frozen(self, db):
        """Full capacity keeps seed frozen."""
        conn = db()
        _insert_user(conn, "a6", status="frozen")
        _insert_user_auth(conn, "a6", "a6@x.com")
        _insert_paid_order(conn, "a6@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        _insert_account(conn, "acct6", plan_type="plus")
        _insert_user(conn, "other_s", status="active", current_account="acct6")
        conn.close()

        with pytest.raises(LifecycleDenied) as exc:
            activate_seed("a6", "acct6", max_active_seeds=1, now=NOW)
        assert exc.value.reason == "capacity_exceeded"

        conn2 = db()
        assert _get_user(conn2, "a6")["status"] == "frozen"
        conn2.close()

    def test_renewal_prefers_original_binding(self, db):
        """Re-activating to the same account is allowed even at max_active_seeds=1
        because the seed itself is excluded from the COUNT."""
        conn = db()
        _insert_user(conn, "a7", status="frozen", current_account="acct7")
        _insert_user_auth(conn, "a7", "a7@x.com")
        _insert_paid_order(conn, "a7@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        _insert_account(conn, "acct7", plan_type="plus")
        conn.close()

        status, tier = activate_seed("a7", "acct7", max_active_seeds=1, now=NOW)
        assert status == "active"
        assert tier == "plus"

    def test_original_healthy_account_preferred_over_candidate(self, db):
        """If the seed's current_account is healthy and same-tier, it is used
        even when a different candidate_account is passed."""
        conn = db()
        _insert_user(conn, "a8", status="frozen", current_account="orig_acct")
        _insert_user_auth(conn, "a8", "a8@x.com")
        _insert_paid_order(conn, "a8@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        _insert_account(conn, "orig_acct", plan_type="plus", status="healthy")
        _insert_account(conn, "new_acct",  plan_type="plus", status="healthy")
        conn.close()

        status, tier = activate_seed("a8", "new_acct", max_active_seeds=5, now=NOW)
        assert status == "active"
        assert tier == "plus"

        conn2 = db()
        row = _get_user(conn2, "a8")
        conn2.close()
        # Original binding preferred
        assert row["current_account"] == "orig_acct"

    def test_unhealthy_account_raises(self, db):
        conn = db()
        _insert_user(conn, "a9", status="frozen")
        _insert_user_auth(conn, "a9", "a9@x.com")
        _insert_paid_order(conn, "a9@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        _insert_account(conn, "sick_acct", plan_type="plus", status="unhealthy")
        conn.close()

        with pytest.raises(LifecycleDenied) as exc:
            activate_seed("a9", "sick_acct", max_active_seeds=5, now=NOW)
        assert exc.value.reason == "account_not_healthy"

    def test_unknown_account_raises(self, db):
        conn = db()
        _insert_user(conn, "a10", status="frozen")
        _insert_user_auth(conn, "a10", "a10@x.com")
        _insert_paid_order(conn, "a10@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        conn.close()

        with pytest.raises(LifecycleDenied) as exc:
            activate_seed("a10", "ghost_token", max_active_seeds=5, now=NOW)
        assert exc.value.reason == "account_unknown"

    def test_invalid_max_active_seeds_raises(self, db):
        with pytest.raises(LifecycleDenied) as exc:
            activate_seed("x", "y", max_active_seeds=0, now=NOW)
        assert exc.value.reason == "invalid_args"

    def test_missing_users_row_raises_store_error(self, db):
        """activate_seed must fail-closed when users row is absent."""
        conn = db()
        _insert_user_auth(conn, "amiss", "amiss@x.com")
        _insert_paid_order(conn, "amiss@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
        _insert_account(conn, "amiss_acct", plan_type="plus")
        conn.close()

        with pytest.raises(StoreError):
            activate_seed("amiss", "amiss_acct", max_active_seeds=5, now=NOW)


# ---------------------------------------------------------------------------
# Concurrency: last slot — exactly one thread wins, DB and memory agree
# ---------------------------------------------------------------------------

class TestConcurrency:

    def test_two_threads_last_slot_only_one_succeeds(self, db):
        """Two threads race for the single remaining slot; exactly one wins."""
        conn = db()
        for i in (1, 2):
            s = f"race{i}"
            _insert_user(conn, s, status="frozen")
            _insert_user_auth(conn, s, f"race{i}@x.com")
            _insert_paid_order(conn, f"race{i}@x.com", "plus-solo-1m", ACTIVE_EXPIRES)
            _globals.seed_map[s] = {"token": "", "plan_type": "", "conversations": []}
        _insert_account(conn, "race_acct", plan_type="plus")
        conn.close()

        results: list = []
        errors: list = []

        def _try(seed):
            try:
                out = activate_seed(seed, "race_acct", max_active_seeds=1, now=NOW)
                results.append((seed, out))
            except LifecycleDenied as e:
                errors.append((seed, e.reason))

        t1 = threading.Thread(target=_try, args=("race1",))
        t2 = threading.Thread(target=_try, args=("race2",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert len(results) == 1, f"expected 1 winner, got {results}"
        assert len(errors) == 1

        winner_seed, (winner_status, winner_tier) = results[0]
        assert winner_status == "active"
        assert winner_tier == "plus"

        conn2 = db()
        w = _get_user(conn2, winner_seed)
        loser_seed = "race2" if winner_seed == "race1" else "race1"
        l = _get_user(conn2, loser_seed)
        conn2.close()

        assert w["status"] == "active"
        assert w["current_account"] == "race_acct"
        assert l["status"] == "frozen"

        assert _globals.seed_map[winner_seed]["token"] == "race_acct"
        assert _globals.seed_map[loser_seed]["token"] == ""
