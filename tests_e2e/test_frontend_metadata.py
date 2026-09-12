"""Business metadata must survive the same rewrite pipeline as profile privacy."""
import json
import re

import pytest

from gateway.reverseProxy import _rewrite_and_scrub


def test_user_metadata_keeps_upgrade_eligibility_with_anonymous_identity(
        client, monkeypatch, seed_account, seed_user, make_access_token):
    from gateway import reverseProxy as proxy
    from utils import resp_cache
    resp_cache.invalidate_all()
    token = make_access_token(plan_type='free')
    seed_account(token, plan_type='free')
    seed_user('seed-free', token, plan_type='free')

    class Result:
        status_code = 200
        headers = {'content-type': 'application/json'}

        async def atext(self):
            return json.dumps({'id': 'user-example', 'email': 'owner@example.test',
                               'name': 'Private owner', 'email_domain_type': 'consumer',
                               'groups': ['actual-group'], 'mfa_flag_enabled': True,
                               'orgs': {'data': [{'name': 'private-org', 'title': 'Private org',
                                                 'description': 'owner@example.test', 'role': 'owner'}]}})

        async def close(self):
            pass

    async def send(*args, **kwargs):
        return Result(), Result()

    monkeypatch.setattr(proxy, '_request_with_retry', send)
    response = client.get('/backend-api/me', cookies={'token': 'seed-free'})
    assert response.status_code == 200
    assert response.json().get('email_domain_type') == 'consumer'
    assert response.json()['groups'] == ['actual-group']
    assert response.json()['mfa_flag_enabled'] is True
    assert response.json()['email'] == ''
    assert response.json()['name'] == 'ChatGPT'
    assert response.json()['orgs']['data'] == [
        {'name': 'ChatGPT', 'title': 'ChatGPT', 'description': '', 'role': 'owner'}]


@pytest.mark.parametrize('plan', ['free', 'plus', 'pro'])
def test_subscription_uses_upstream_plan_without_exposing_billing(client, monkeypatch, plan):
    from fastapi.responses import JSONResponse
    from gateway import backend
    calls = []

    async def upstream(request, path):
        calls.append((request.cookies.get('token'), path))
        return JSONResponse({'plan_type': plan, 'will_renew': False,
                             'is_delinquent': False, 'billing_email': 'private@example.test',
                             'payment_method': {'last4': '1234'}, 'id': 'private-subscription'})

    monkeypatch.setattr(backend, 'chatgpt_reverse_proxy', upstream)
    response = client.get('/backend-api/subscriptions', cookies={'token': 'seed-' + plan})
    assert response.status_code == 200
    assert response.json() == {'plan_type': plan, 'will_renew': False, 'is_delinquent': False}
    assert calls == [('seed-' + plan, 'backend-api/subscriptions')]


def test_subscription_upstream_failure_is_not_a_fake_free_plan(client, monkeypatch):
    from fastapi.responses import JSONResponse
    from gateway import backend

    async def unavailable(request, path):
        return JSONResponse({'detail': 'temporarily unavailable'}, status_code=503)

    monkeypatch.setattr(backend, 'chatgpt_reverse_proxy', unavailable)
    response = client.get('/backend-api/subscriptions', cookies={'token': 'seed-pro'})
    assert response.status_code == 503
    assert 'plan_type' not in response.json()


@pytest.mark.parametrize('query', ['', '?account_id=another-workspace'])
def test_subscription_query_uses_verified_bound_account(
        client, monkeypatch, seed_account, seed_user, make_access_token, query):
    from gateway import reverseProxy as proxy
    token = make_access_token(account_id='bound-account')
    seed_account(token)
    seed_user('subscription-seed', token)
    seen = {}

    class Result:
        status_code = 200
        headers = {'content-type': 'application/json'}

        async def atext(self):
            return json.dumps({'plan_type': 'plus'})

        async def close(self):
            pass

    async def send(*args, **kwargs):
        seen.update(kwargs)
        return Result(), Result()

    monkeypatch.setattr(proxy, '_request_with_retry', send)
    response = client.get('/backend-api/subscriptions' + query,
                          cookies={'token': 'subscription-seed'})
    assert response.status_code == 200
    assert seen['params'].get('account_id') == 'bound-account'


def test_bootstrap_redaction_accepts_the_same_attribute_order_as_fetcher():
    from gateway.chatgpt import _rewrite_client_bootstrap
    session = {'account': {'id': 'a', 'planType': 'plus'},
               'sessionToken': 'PRIVATE-SESSION', 'refreshToken': 'PRIVATE-REFRESH'}
    html = '<script nonce="example" id="client-bootstrap" type="application/json">' + json.dumps(
        {'session': session, 'flags': {'enabled': True}}) + '</script>'
    rendered = _rewrite_client_bootstrap(html, {})
    assert 'PRIVATE-SESSION' not in rendered
    assert 'PRIVATE-REFRESH' not in rendered
    assert json.loads(re.search(r'>(.*?)</script>', rendered).group(1))['flags']['enabled']


def test_temporary_website_auth_failure_is_not_rendered_as_login(
        client, monkeypatch, seed_user, seed_account, make_access_token):
    from fastapi import HTTPException
    from gateway import chatgpt
    token = make_access_token(account_id='temporary', user_id='u-temporary')
    seed_account(token)
    seed_user('temporary-seed', token)

    async def unavailable(req_token):
        raise HTTPException(status_code=503, detail='Account website session unavailable')

    monkeypatch.setattr(chatgpt, 'verify_token', unavailable)
    response = client.get('/?token=temporary-seed')
    assert response.status_code == 503
    assert response.headers['cache-control'] == 'no-store'
    assert '官网会话暂不可用' in response.text


def test_plugin_and_model_names_survive_identity_redaction():
    payload = {"plugins": [{"name": "Deep research", "id": "research"}],
               "models": [{"name": "Thinking", "slug": "thinking"}],
               "user": {"name": "Private owner", "email": "owner@example.test"}}
    result = json.loads(_rewrite_and_scrub(
        json.dumps(payload), path="backend-api/plugins", base_url="https://chatgpt.com",
        petrol="http", origin_host="testserver", seed_cookie="seed-a",
        content_type="application/json"))
    assert result["plugins"] == payload["plugins"]
    assert result["models"] == payload["models"]
    assert result["user"]["name"] == "ChatGPT"
    assert result["user"]["email"] == ""


def test_entry_preserves_bound_capabilities_and_session_privacy(
        client, monkeypatch, tmp_path, seed_user, seed_account, make_access_token):
    from gateway import frontend_sync as frontend
    frontend.invalidate_frontend_cache()
    monkeypatch.setattr(frontend, 'SESSION_ARCHIVE_DIR', tmp_path)
    calls = []
    tokens = {}
    for name in ('a', 'b'):
        tok = make_access_token(account_id=name, user_id='u-' + name)
        tokens[name] = tok
        seed_account(tok)
        seed_user('seed-' + name, tok)
        (tmp_path / (name + '.json')).write_text(json.dumps(
            {'account': {'id': name}, 'sessionToken': 'private-' + name}))

    def fetch(cookies, account_id, fingerprint, *, refresh=False):
        assert cookies['__Secure-next-auth.session-token'] == 'private-' + account_id
        calls.append(account_id)
        session = {'user': {'id': 'u-' + account_id, 'name': 'Private owner', 'email': 'private@example.test'},
                   'account': {'id': account_id, 'planType': 'plus', 'hasFloraFeature': account_id == 'b',
                               'isDelinquent': account_id == 'a'},
                   'accessToken': (make_access_token(account_id=account_id, user_id='u-' + account_id,
                                                      iat=1700000001) if refresh else tokens[account_id]),
                   'sessionToken': 'DO-NOT-EXPOSE'}
        data = {'authStatus': 'logged_in', 'session': session, 'user': session['user'],
                'flags': {'assignment': account_id}}
        return {'html': '<html><head></head><script type="application/json" id="client-bootstrap">'
                        + json.dumps(data) + '</script></html>', 'session': session, 'cookies': cookies}

    monkeypatch.setattr(frontend, '_fetch_official_html_sync', fetch)
    cold = client.get('/api/auth/session', cookies={'token': 'seed-b'})
    assert cold.status_code == 200
    assert cold.json()['account'].get('hasFloraFeature') is True
    assert calls == ['b']
    frontend.invalidate_frontend_cache()
    calls.clear()
    for name in ('a', 'b', 'a'):
        response = client.get('/?token=seed-' + name)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        data = json.loads(re.search(r'id="client-bootstrap"[^>]*>(.*?)</script>', response.text).group(1))
        assert data['flags']['assignment'] == name
        assert data['session']['account']['hasFloraFeature'] == (name == 'b')
        assert data['session']['account']['isDelinquent'] == (name == 'a')
        assert data['session']['accessToken'] == ''
        assert 'DO-NOT-EXPOSE' not in response.text
        assert tokens[name] not in response.text
        assert 'private@example.test' not in response.text
        session = client.get('/api/auth/session').json()
        assert session['account'] == data['session']['account']
        assert session['accessToken'] == ''
        assert session['sessionToken'] == ''
    assert calls == ['a', 'b']
    renewed = client.get('/api/auth/session?refresh=true&reason=integrity_state_missing&account_id=b',
                         cookies={'token': 'seed-a'})
    assert renewed.status_code == 200
    assert renewed.headers['cache-control'] == 'no-store'
    assert renewed.json()['account']['id'] == 'a'
    assert renewed.json()['accessToken'] == ''
    assert 'DO-NOT-EXPOSE' not in renewed.text
    assert calls == ['a', 'b', 'a']
    import utils.globals as globals
    globals.seed_map['seed-a']['conversations'] = ['owned-conversation']
    response = client.get('/c/owned-conversation', cookies={'token': 'seed-a'})
    assert response.status_code == 200
    assert response.url.path == '/c/owned-conversation'
    assert 'client-bootstrap' in response.text
    denied = client.get('/c/owned-conversation', cookies={'token': 'seed-b'})
    assert denied.status_code == 404
    frontend.invalidate_frontend_cache()


def test_auth_verification_failure_does_not_return_success(
        client, monkeypatch, seed_account, seed_user, make_access_token):
    from fastapi import HTTPException
    from gateway import backend
    token = make_access_token(plan_type='free')
    seed_account(token, plan_type='free')
    seed_user('seed-free', token, plan_type='free')

    async def unavailable(token):
        raise HTTPException(status_code=503, detail='Website session unavailable')

    monkeypatch.setattr(backend, 'verify_token', unavailable)
    response = client.get('/api/auth/session?refresh=true', cookies={'token': 'seed-free'})
    assert response.status_code == 503
    assert response.headers['cache-control'] == 'no-store'
    assert 'accessToken' not in response.json()


def test_cold_session_missing_website_credentials_is_not_synthetic_success(
        client, monkeypatch, tmp_path, seed_account, seed_user, make_access_token):
    from gateway import frontend_sync as frontend
    frontend.invalidate_frontend_cache()
    monkeypatch.setattr(frontend, 'SESSION_ARCHIVE_DIR', tmp_path)
    monkeypatch.setattr(frontend, 'SESSION_COOKIE_FILE', str(tmp_path / 'missing'))
    token = make_access_token(account_id='cold-account')
    seed_account(token)
    seed_user('cold-seed', token)
    response = client.get('/api/auth/session', cookies={'token': 'cold-seed'})
    assert response.status_code == 503
    assert response.json() == {}
    assert response.headers['cache-control'] == 'no-store'


def test_page_sets_integrity_state_but_auth_session_does_not(
        client, monkeypatch, tmp_path, seed_account, seed_user, make_access_token):
    from gateway import frontend_sync as frontend
    frontend.invalidate_frontend_cache()
    monkeypatch.setattr(frontend, 'SESSION_ARCHIVE_DIR', tmp_path)
    token = make_access_token(account_id='integrity-account')
    seed_account(token)
    seed_user('integrity-seed', token)
    (tmp_path / 'integrity.json').write_text(json.dumps({'account': {'id': 'integrity-account'},
                                                         'sessionToken': 'private-integrity'}))
    def fetch(cookies, account_id, fingerprint, **kwargs):
        cookies = dict(cookies)
        cookies['__Secure-oai-is'] = 'ois1.test.state.nonce'
        session = {'user': {'id': 'u-123'}, 'account': {'id': account_id, 'planType': 'plus'},
                   'accessToken': token, 'sessionToken': 'private'}
        data = {'authStatus': 'logged_in', 'session': session}
        return {'html': '<html><head></head><script id="client-bootstrap">' + json.dumps(data) + '</script></html>',
                'session': session, 'cookies': cookies}
    monkeypatch.setattr(frontend, '_fetch_official_html_sync', fetch)
    page = client.get('/?token=integrity-seed')
    assert page.status_code == 200
    assert '__Secure-oai-is=ois1.test.state.nonce' in page.headers.get('set-cookie', '')
    auth = client.get('/api/auth/session', cookies={'token': 'integrity-seed'})
    assert '__Secure-oai-is=' not in auth.headers.get('set-cookie', '')


def test_public_assets_never_forward_browser_credentials(client, monkeypatch):
    from gateway import reverseProxy as proxy
    from utils import resp_cache
    resp_cache.invalidate_all()
    seen = {}

    class Result:
        status_code = 200
        headers = {'content-type': 'application/javascript'}
        async def atext(self):
            return '/* static */'
        async def close(self):
            pass

    async def send(method, url, **kwargs):
        seen.update(kwargs)
        return Result(), Result()

    monkeypatch.setattr(proxy, '_request_with_retry', send)
    response = client.get('/cdn/assets/privacy-check.js',
                          headers={'Authorization': 'Bearer private-access'},
                          cookies={'token': 'private-seed', '__Secure-next-auth.session-token': 'private-cookie'})
    assert response.status_code == 200
    assert seen['cookies'] == {}
    assert 'authorization' not in seen['headers']
    assert 'chatgpt-account-id' not in seen['headers']
