"""Actual authorization routing must enter the transactional Seed lifecycle."""
import time

import pytest
from fastapi import HTTPException

from chatgpt import authorization as auth
from utils import configs, globals, store


@pytest.fixture(autouse=True)
def isolated(db, monkeypatch):
    monkeypatch.setattr(globals, 'seed_map', {})
    monkeypatch.setattr(globals, 'error_token_list', [])
    monkeypatch.setattr(globals, 'antiban_dead_tokens', {})
    monkeypatch.setattr(configs, 'max_shared_seeds_per_account', 2, raising=False)


def user(seed, account='', tier='plus', density='shared', status='frozen'):
    email = f'{seed}@example.test'
    store.create_user_with_trial(email, password_hash='synthetic', seed=seed,
                                 status='active', trial_tier='plus', trial_total=3)
    store.upsert_user(seed, current_account=account, plan_type=tier, status=status)
    if density != 'trial':
        store.create_order(f'order-{seed}', email, f'{tier}-{density}-1m', '1', status='pending')
        store.activate_order(f'order-{seed}', int(time.time()) + 86400)
    globals.seed_map[seed] = dict(token=account, plan_type=tier, status=status, conversations=[])


@pytest.mark.parametrize('tier', ['plus', 'pro'])
def test_renewed_frozen_binding_is_capacity_checked_before_reuse(tier):
    store.upsert_account('original', plan_type=tier, status='healthy')
    store.upsert_account('alternative', plan_type=tier, status='healthy')
    for seed in ('peer-1', 'peer-2'):
        user(seed, 'original', tier=tier, status='active')
    user('renew', 'original', tier=tier)
    store.upsert_conversation('history', 'renew', 'original', 'Example', '1', '2')
    assert auth._resolve_seed_account('renew') == 'alternative'
    assert store.get_user('renew')['status'] == 'active'
    assert globals.seed_map['renew']['conversations'] == ['history']
    assert store.list_seed_conversations('renew')[0]['account'] == 'original'


def test_routing_uses_persisted_original_not_stale_memory():
    for account in ('original', 'stale'):
        store.upsert_account(account, plan_type='plus', status='healthy')
    user('renew', 'original')
    globals.seed_map['renew']['token'] = 'stale'
    assert auth._resolve_seed_account('renew') == 'original'
    assert globals.seed_map['renew']['status'] == 'active'


def test_active_healthy_binding_uses_read_only_hot_path(monkeypatch):
    store.upsert_account('original', plan_type='plus', status='healthy')
    user('hot', 'original', status='active')
    from utils import seed_lifecycle
    monkeypatch.setattr(seed_lifecycle, 'route_seed', lambda *a, **k: (_ for _ in ()).throw(AssertionError('hot path opened router')))
    assert auth._resolve_seed_account('hot') == 'original'


def test_routing_does_not_persist_unrelated_seed_snapshot(monkeypatch):
    store.upsert_account('plus', plan_type='plus', status='healthy')
    user('new')
    store.upsert_user('other', current_account='authoritative')
    globals.seed_map['other'] = {'token': 'stale', 'plan_type': 'plus'}

    def forbid():
        raise AssertionError('No whole-map persistence during SaaS allocation')

    monkeypatch.setattr(globals, 'persist_seed_map', forbid)
    assert auth._resolve_seed_account('new') == 'plus'
    assert store.get_user('other')['current_account'] == 'authoritative'


@pytest.mark.parametrize('density', ['trial', 'shared'])
def test_shared_capacity_must_be_explicit(monkeypatch, density):
    monkeypatch.setattr(configs, 'max_shared_seeds_per_account', 0)
    store.upsert_account('plus', plan_type='plus', status='healthy')
    user('new', density=density)
    with pytest.raises(HTTPException) as failure:
        auth._resolve_seed_account('new')
    assert failure.value.status_code == 503
    assert store.get_user('new')['current_account'] == ''


def test_solo_does_not_depend_on_shared_capacity(monkeypatch):
    monkeypatch.setattr(configs, 'max_shared_seeds_per_account', 0)
    store.upsert_account('plus', plan_type='plus', status='healthy')
    user('new', density='solo')
    assert auth._resolve_seed_account('new') == 'plus'
    assert store.get_user('new')['status'] == 'active'


def test_switch_checks_capacity_and_excludes_current():
    for account in ('original', 'full', 'available'):
        store.upsert_account(account, plan_type='plus', status='healthy')
    user('switch', 'original', status='active')
    user('peer-1', 'full', status='active')
    user('peer-2', 'full', status='active')
    assert auth.switch_seed_account('switch') == 'available'
    assert store.get_user('switch')['current_account'] == 'available'


def test_switch_without_candidate_preserves_binding():
    store.upsert_account('original', plan_type='plus', status='healthy')
    user('switch', 'original', status='active')
    assert auth.switch_seed_account('switch') == ''
    assert store.get_user('switch')['current_account'] == 'original'


@pytest.mark.parametrize('auto_seed', [False, True])
def test_seed_mode_cannot_bypass_saas_capacity(monkeypatch, auto_seed):
    monkeypatch.setattr(configs, 'auto_seed', auto_seed)
    store.upsert_account('original', plan_type='plus', status='healthy')
    user('renew', 'original')
    user('peer-1', 'original', status='active')
    user('peer-2', 'original', status='active')
    assert auth.get_req_token('renew') == ''
    assert store.get_user('renew')['status'] == 'frozen'


def test_concurrent_routing_cannot_allocate_last_slot_twice(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(configs, 'max_shared_seeds_per_account', 1)
    store.upsert_account('plus', plan_type='plus', status='healthy')
    for seed in ('one', 'two'):
        user(seed)
    barrier = threading.Barrier(2)

    def route(seed):
        barrier.wait(timeout=5)
        return seed, auth._resolve_seed_account(seed)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(route, ('one', 'two')))
    assert sorted(token for _, token in outcomes) == ['', 'plus']
    for seed, token in outcomes:
        assert store.get_user(seed)['current_account'] == token
        assert globals.seed_map[seed]['token'] == token
        assert store.get_user(seed)['status'] == ('active' if token else 'frozen')


def test_missing_persisted_seed_fails_closed():
    store.upsert_account('plus', plan_type='plus', status='healthy')
    user('missing')
    with store._connect() as conn:
        conn.execute("DELETE FROM users WHERE seed='missing'")
    with pytest.raises(HTTPException) as failure:
        auth._resolve_seed_account('missing')
    assert failure.value.status_code == 503
    assert globals.seed_map['missing']['token'] == ''


def test_binding_write_failure_keeps_seed_state_and_is_sanitized(caplog):
    store.upsert_account('plus', plan_type='plus', status='healthy')
    user('failure')
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER fail_binding BEFORE UPDATE ON users BEGIN SELECT RAISE(ABORT, 'synthetic-private-detail'); END")
    with pytest.raises(HTTPException) as failure:
        auth._resolve_seed_account('failure')
    assert failure.value.status_code == 503
    assert 'synthetic-private-detail' not in str(failure.value)
    assert 'synthetic-private-detail' not in caplog.text
    assert store.get_user('failure')['current_account'] == ''
    assert globals.seed_map['failure']['token'] == ''
