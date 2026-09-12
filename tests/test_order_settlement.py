"""Settlement owns renewal arithmetic and state transition in one DB transaction."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import threading

import pytest
from utils import store

NOW = 1_800_000_000


def order(oid, plan='plus-shared-1m', email='buyer@example.test'):
    store.create_order(oid, email, plan, '39', status='pending')


def test_repeat_settlement_does_not_extend_expiry(db):
    order('one')
    assert store.settle_order('one', now=NOW)
    expiry = store.get_order('one')['expires_at']
    assert expiry == NOW + 30 * 86400
    assert not store.settle_order('one', now=NOW + 500)
    assert store.get_order('one')['expires_at'] == expiry


def test_renewal_stacks_only_same_tier_and_owner(db):
    order('prior')
    store.activate_order('prior', NOW + 10 * 86400)
    order('other-owner', email='other@example.test')
    store.activate_order('other-owner', NOW + 100 * 86400)
    order('other-tier', plan='pro-shared-1m')
    store.activate_order('other-tier', NOW + 200 * 86400)
    order('new')
    assert store.settle_order('new', now=NOW)
    assert store.get_order('new')['expires_at'] == NOW + 40 * 86400


def test_concurrent_connections_stack_without_process_lock(db, monkeypatch):
    for oid in ('one', 'two'):
        order(oid)
    # Independent workers do not share a Python mutex. SQLite must serialize.
    monkeypatch.setattr(store, '_WRITE_LOCK', nullcontext())
    barrier = threading.Barrier(2)

    def settle(oid):
        barrier.wait(timeout=5)
        return store.settle_order(oid, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(settle, ('one', 'two'))) == [True, True]
    assert sorted(store.get_order(oid)['expires_at'] for oid in ('one', 'two')) == [
        NOW + 30 * 86400, NOW + 60 * 86400]


def test_write_failure_is_explicit_and_rolls_back(db, caplog):
    order('one')
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER reject_paid BEFORE UPDATE ON orders BEGIN SELECT RAISE(ABORT, 'private-diagnostic'); END")
    with pytest.raises(store.StoreError):
        store.settle_order('one', now=NOW)
    assert store.get_order('one')['status'] == 'pending'
    assert store.get_order('one')['expires_at'] is None
    assert 'private-diagnostic' not in caplog.text


def test_connection_failure_is_explicit(db, monkeypatch):
    def fail():
        raise sqlite3.OperationalError('private-diagnostic')
    monkeypatch.setattr(store, '_connect', fail)
    with pytest.raises(store.StoreError):
        store.settle_order('one', now=NOW)


def test_unknown_and_delisted_orders_do_not_gain_entitlement(db):
    order('delisted', plan='removed-plan')
    assert not store.settle_order('unknown', now=NOW)
    assert not store.settle_order('delisted', now=NOW)
    assert store.get_order('delisted')['status'] == 'pending'


@pytest.mark.parametrize('strict', [False, True])
def test_order_read_failure_is_anonymous_and_distinguishable(db, monkeypatch, caplog, strict):
    def fail():
        raise sqlite3.OperationalError('private-order-diagnostic')
    monkeypatch.setattr(store, '_connect', fail)
    if strict:
        with pytest.raises(store.StoreError, match='Order lookup unavailable'):
            store.get_order('one', strict=True)
    else:
        assert store.get_order('one') is None
    assert 'private-order-diagnostic' not in caplog.text


def test_strict_lookup_distinguishes_absent_from_unavailable(db):
    assert store.get_order('absent', strict=True) is None
