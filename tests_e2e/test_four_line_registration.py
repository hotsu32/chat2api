"""Registration grants Plus trial access through the actual public route."""

from utils import configs, entitlements, store
import pytest


def test_registered_user_receives_plus_trial(client, monkeypatch):
    monkeypatch.setattr(configs, "require_email_verification", False)
    email = "new-trial@example.test"
    client.get("/register")
    response = client.post(
        "/register",
        data={
            "email": email,
            "password": "Trial-example-password-734!",
            "csrf_token": client.cookies.get(configs.user_csrf_cookie) or "",
        },
    )
    assert response.status_code < 400
    user = store.get_user_auth(email)
    assert user is not None, "Registration must create the user"
    assert entitlements.effective_tier(user["seed"]) == "plus"


def test_startup_reclaims_only_confirmed_dead_trial_owners(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from utils import trials, store
    import api.chat2api as api

    store.create_user_with_trial('restart@example.test', password_hash='synthetic',
                                 seed='restart-seed', status='active', trial_tier='plus', trial_total=3)
    dead = trials.reserve('restart-seed')
    live = trials.reserve('restart-seed')
    store.create_user_with_trial('expired-startup@example.test', password_hash='synthetic',
                                 seed='expired-startup-seed', status='active', trial_tier='plus', trial_total=3)
    store.upsert_user('expired-startup-seed', current_account='original', status='active')
    store.create_order('startup-expired-order', 'expired-startup@example.test', 'plus-shared-1m', '1', status='pending')
    store.activate_order('startup-expired-order', 100)
    store.upsert_conversation('startup-history', 'expired-startup-seed', 'original', 'Example', '1', '2')
    with store._connect() as conn:
        conn.execute('UPDATE trial_reservations SET instance_id=? WHERE res_id=?',
                     ('99999999:dead-instance', dead))
    original_kill = trials.os.kill

    def check_process(pid, signal):
        if pid == 99999999:
            raise ProcessLookupError()
        return original_kill(pid, signal)

    async def no_antiban():
        pass

    monkeypatch.setattr(trials.os, 'kill', check_process)
    monkeypatch.setattr(api, 'initialize_from_env', lambda: None)
    monkeypatch.setattr(api.antiban, 'init', no_antiban)
    monkeypatch.setattr(api, 'enable_session_sticky', False)
    monkeypatch.setattr(api, 'scheduled_refresh', False)
    jobs = {}
    monkeypatch.setattr(api, 'scheduler', SimpleNamespace(add_job=lambda **kw: jobs.update({kw['id']: kw}), start=lambda: None))
    asyncio.run(api.app_start())
    assert store.get_trial_reservation(dead)['status'] == 'released'
    assert store.get_trial_reservation(live)['status'] == 'reserved'
    expired = store.get_user('expired-startup-seed')
    assert expired['status'] == 'frozen'
    assert expired['current_account'] == 'original'
    assert store.list_seed_conversations('expired-startup-seed')[0]['conv_id'] == 'startup-history'
    assert jobs['seed_expiry']['trigger'] == 'interval'
    assert jobs['seed_expiry']['seconds'] <= 60


def test_registration_only_writes_its_own_seed(client, monkeypatch):
    import utils.globals as globals
    monkeypatch.setattr(configs, 'require_email_verification', False)

    def forbid_whole_pool_write():
        raise AssertionError('Registration must not replace the whole seed pool')

    monkeypatch.setattr(globals, 'persist_seed_map', forbid_whole_pool_write)
    client.get('/register')
    response = client.post('/register', data={
        'email': 'targeted@example.test', 'password': 'Example-strong-password-123!',
        'csrf_token': client.cookies.get(configs.user_csrf_cookie) or '',
    })
    assert response.status_code < 400
    auth = store.get_user_auth('targeted@example.test')
    seed = store.get_user(auth['seed'])
    assert seed is not None
    assert seed['status'] == 'trial'
    assert globals.seed_map[auth['seed']]['status'] == 'trial'


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
def test_expired_generation_freezes_seed_without_losing_history_or_login(client, mock_upstream, path):
    import time
    from utils import globals
    client.get('/register')
    response = client.post('/register', data={
        'email': 'expired-history@example.test', 'password': 'Example-strong-password-123!',
        'csrf_token': client.cookies.get(configs.user_csrf_cookie) or '',
    })
    assert response.status_code == 200
    auth = store.get_user_auth('expired-history@example.test')
    seed = auth['seed']
    store.upsert_user(seed, current_account='synthetic-original', status='active', plan_type='plus')
    globals.seed_map[seed].update(token='synthetic-original', status='active', plan_type='plus')
    store.upsert_conversation('synthetic-history', seed, 'synthetic-original', 'Example', '1', '2')
    store.create_order('expired-history-order', auth['email'], 'plus-shared-1m', '1', status='pending')
    store.activate_order('expired-history-order', int(time.time()) - 60)
    before = len(mock_upstream.records)
    response = client.post(path, cookies={'token': seed}, json={'model': 'auto', 'messages': []})
    assert response.status_code == 402
    assert len(mock_upstream.records) == before
    persisted = store.get_user(seed)
    assert persisted['status'] == 'frozen'
    assert persisted['current_account'] == 'synthetic-original'
    assert globals.seed_map[seed]['status'] == 'frozen'
    assert store.list_seed_conversations(seed)[0]['conv_id'] == 'synthetic-history'
    assert store.get_user_auth(auth['email'])['status'] == 'active'
    assert client.get('/dashboard').status_code == 200
    assert '续费' in client.get('/dashboard').text
