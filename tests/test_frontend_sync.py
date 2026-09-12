"""Account-bound official bootstrap: exercise auth, HTML and cache as one path."""
import asyncio
import json

import pytest

from gateway import frontend_sync as frontend


@pytest.fixture
def upstream(monkeypatch, tmp_path, make_access_token):
    monkeypatch.setattr(frontend, "SESSION_ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(frontend, "SESSION_COOKIE_FILE", str(tmp_path / "missing-cookie"))
    frontend.invalidate_frontend_cache()
    state = {"calls": [], "version": 1, "wrong_account": False, "logged_in": True, "feature": False,
             "devices": [], "refreshes": [], "issued_at": 1700000000, "challenge_once": False}
    for name in ("a", "b"):
        record = {"account": {"id": name}, "sessionToken": "cookie-" + name}
        (tmp_path / (name + ".json")).write_text(json.dumps(record))

    class Response:
        status_code = 200

        def __init__(self, data=None, text=""):
            self.data, self.text = data, text
            self.headers = {'cf-mitigated': 'challenge'} if text == 'cf challenge' else {}
            if text == 'cf challenge':
                self.status_code = 403

        def json(self):
            return self.data

    class Session:
        def __init__(self, **kwargs):
            self.cookies = self
            self.values = {}

        def set(self, name, value, **kwargs):
            self.values[name] = value

        def items(self):
            return self.values.items()

        def get(self, url, **kwargs):
            name = self.values["__Secure-next-auth.session-token"].removeprefix("cookie-")
            state["calls"].append((name, url))
            if url.endswith('/api/auth/session') and state.get('challenge_once'):
                state['challenge_once'] = False
                return Response({}, text='cf challenge')
            if kwargs.get('params', {}).get('refresh') == 'true':
                state['refreshes'].append((name, kwargs['params']))
                state['issued_at'] += 1
            account = "intruder" if state["wrong_account"] else name
            session = {"user": {"id": "u-" + account},
                       "account": {"id": account, "planType": "plus", "hasFloraFeature": state['feature']},
                       "accessToken": make_access_token(account_id=account, user_id="u-" + account,
                                                       iat=state['issued_at']),
                       "sessionToken": "private-cookie"}
            stage = 'auth' if url.endswith('/api/auth/session') else 'html'
            if state.get('invalid_expiry_at') == stage:
                session['accessToken'] = make_access_token(
                    account_id=account, user_id='u-' + account, exp=state['invalid_expiry'])
            if state.get('session_error_at') == stage:
                session['error'] = 'RefreshAccessTokenError'
            if url.endswith("/api/auth/session"):
                self.values.setdefault('oai-did', 'device-' + str(len(state['calls'])))
                state['devices'].append(self.values['oai-did'])
                return Response(session)
            bootstrap = {"authStatus": "logged_in" if state["logged_in"] else "logged_out",
                         "session": session, "flags": {"experiment": name, "build": state["version"]}}
            return Response(text='<script type="application/json" id="client-bootstrap">'
                            + json.dumps(bootstrap) + '</script>')

        def close(self):
            pass

    monkeypatch.setattr(frontend.cffi_requests, "Session", Session)
    return state


def fetch(name, make_access_token):
    token = make_access_token(account_id=name, user_id="u-" + name)
    return asyncio.run(frontend.get_frontend_template(token, token, {}))


def test_same_tier_a_b_a_and_hot_cache(upstream, make_access_token):
    a = fetch("a", make_access_token)
    b = fetch("b", make_access_token)
    assert '"experiment": "a"' in a
    assert '"experiment": "b"' in b
    assert fetch("a", make_access_token) == a
    assert len(upstream["calls"]) == 4


def test_auth_challenge_is_retried_once(upstream, make_access_token):
    upstream['challenge_once'] = True
    assert '"experiment": "a"' in fetch('a', make_access_token)
    assert [url for _, url in upstream['calls'] if url.endswith('/api/auth/session')] == [
        'https://chatgpt.com/api/auth/session', 'https://chatgpt.com/api/auth/session']


@pytest.mark.parametrize('stage', ['auth', 'html'])
@pytest.mark.parametrize('expiry', [1, None, 'not-a-timestamp', float('inf')])
def test_invalid_upstream_token_expiry_never_publishes_frontend(
        upstream, make_access_token, stage, expiry):
    token = make_access_token(account_id='a', user_id='u-a')
    fetch('a', make_access_token)
    upstream.update(invalid_expiry_at=stage, invalid_expiry=expiry)
    with pytest.raises(frontend.FrontendSessionError):
        asyncio.run(frontend.refresh_cached_frontend(token, token, {}, refresh=True))
    assert frontend.get_cached_frontend(token, token) is None
    assert frontend.get_session_cookie(token, token) == {}


def test_expiry_refetches_build_and_permissions(upstream, make_access_token, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(frontend.time, "monotonic", lambda: clock[0])
    before = fetch("a", make_access_token)
    upstream["version"] = 2
    upstream['feature'] = True
    clock[0] += frontend.TEMPLATE_TTL + 1
    after = fetch("a", make_access_token)
    assert before != after
    assert '"build": 2' in after
    assert '"hasFloraFeature": true' in after
    assert len(upstream["calls"]) == 4


def test_refresh_retains_verified_device_but_new_source_discards_it(
        upstream, make_access_token, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(frontend.time, 'monotonic', lambda: clock[0])
    fetch('a', make_access_token)
    fetch('b', make_access_token)
    clock[0] += frontend.TEMPLATE_TTL + 1
    fetch('a', make_access_token)
    assert upstream['devices'][2] == upstream['devices'][0]
    assert upstream['devices'][2] != upstream['devices'][1]
    token = make_access_token(account_id='a', user_id='u-a')
    asyncio.run(frontend.get_frontend_template(token, token, {'proxy_url': 'changed'}))
    assert upstream['devices'][3] != upstream['devices'][0]


def test_replaced_archive_does_not_reuse_previous_website_cookies(
        upstream, make_access_token, monkeypatch):
    fetch('a', make_access_token)
    path = frontend.SESSION_ARCHIVE_DIR / 'a.json'
    path.write_text(json.dumps({'account': {'id': 'a'}, 'sessionToken': 'replacement-cookie'}))
    seen = []

    def new_source(cookies, account_id, fingerprint):
        seen.append(dict(cookies))
        raise frontend.FrontendSessionError('Replacement session rejected')

    monkeypatch.setattr(frontend, '_fetch_official_html_sync', new_source)
    with pytest.raises(frontend.FrontendSessionError):
        fetch('a', make_access_token)
    assert seen == [{'__Secure-next-auth.session-token': 'replacement-cookie'}]
    token = make_access_token(account_id='a', user_id='u-a')
    assert frontend.get_cached_frontend(token, token) is None


def test_backend_refresh_observes_proxy_change_even_with_hot_template(
        upstream, make_access_token):
    fetch('a', make_access_token)
    token = make_access_token(account_id='a', user_id='u-a')
    context = asyncio.run(frontend.refresh_cached_frontend(token, token, {'proxy_url': 'new'}))
    assert context['fingerprint'] == {'proxy_url': 'new'}
    assert len(upstream['calls']) == 4


@pytest.mark.parametrize('warm', [False, True])
def test_explicit_refresh_obtains_new_access_token_even_with_hot_cache(
        upstream, make_access_token, warm):
    token = make_access_token(account_id='a', user_id='u-a')
    if warm:
        fetch('a', make_access_token)

    async def run():
        return await asyncio.gather(*(frontend.refresh_cached_frontend(
            token, token, {}, refresh=True) for _ in range(4)))

    contexts = asyncio.run(run())
    assert upstream['refreshes'] == [('a', {'refresh': 'true'})]
    for context in contexts:
        renewed = context['session']['accessToken']
        assert renewed != token
        assert frontend.decode_jwt_payload(renewed)['iat'] == 1700000001
        assert context['account_id'] == 'a'


def test_explicit_refresh_error_does_not_publish_success_or_stale_token(
        upstream, make_access_token):
    token = make_access_token(account_id='a', user_id='u-a')
    fetch('a', make_access_token)
    upstream['session_error_at'] = 'auth'
    with pytest.raises(frontend.FrontendSessionError):
        asyncio.run(frontend.refresh_cached_frontend(token, token, {}, refresh=True))
    assert frontend.get_cached_frontend(token, token) is None


def test_expired_cached_access_token_renews_before_business_use(
        upstream, make_access_token, monkeypatch):
    token = make_access_token(account_id='a', user_id='u-a')
    fetch('a', make_access_token)
    original = frontend.decode_jwt_payload

    def expired_old_token(value):
        claims = original(value)
        if claims.get('iat') == 1700000000:
            claims['exp'] = 1
        return claims

    monkeypatch.setattr(frontend, 'decode_jwt_payload', expired_old_token)
    renewed = asyncio.run(frontend.verified_access_token(token))
    assert original(renewed)['iat'] == 1700000001
    assert len(upstream['refreshes']) == 1


def test_ordinary_request_joins_pending_explicit_renewal(
        upstream, make_access_token, monkeypatch):
    token = make_access_token(account_id='a', user_id='u-a')
    fetch('a', make_access_token)
    original = frontend.run_in_threadpool

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(frontend, 'run_in_threadpool', delayed)
        renewal = asyncio.create_task(frontend.get_frontend_template(token, token, {}, refresh=True))
        await started.wait()
        ordinary = asyncio.create_task(frontend.get_frontend_template(token, token, {}))
        await asyncio.sleep(0)
        assert not ordinary.done()
        release.set()
        assert await ordinary == await renewal

    asyncio.run(run())
    assert len(upstream['calls']) == 4


@pytest.mark.parametrize("failure", ["wrong_account", "logged_out", "missing"])
def test_invalid_session_never_uses_other_account(upstream, make_access_token, failure):
    fetch("a", make_access_token)
    upstream["wrong_account"] = failure == "wrong_account"
    upstream["logged_in"] = failure != "logged_out"
    with pytest.raises(frontend.FrontendSessionError):
        fetch("missing" if failure == "missing" else "b", make_access_token)


def test_revoked_context_is_not_returned_after_failed_revalidation(upstream, make_access_token, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(frontend.time, 'monotonic', lambda: clock[0])
    fetch('a', make_access_token)
    clock[0] += frontend.TEMPLATE_TTL + 1
    upstream['logged_in'] = False
    token = make_access_token(account_id='a', user_id='u-a')
    for _ in range(2):
        with pytest.raises(frontend.FrontendSessionError):
            asyncio.run(frontend.refresh_cached_frontend(token, token, {}))
        assert frontend.get_cached_frontend(token, token) is None
        assert frontend.get_session_cookie(token, token) == {}


@pytest.mark.parametrize('stage', ['auth', 'html'])
def test_http_200_session_error_rejects_matching_account_and_stale_cache(
        upstream, make_access_token, monkeypatch, stage):
    clock = [100.0]
    monkeypatch.setattr(frontend.time, 'monotonic', lambda: clock[0])
    fetch('a', make_access_token)
    clock[0] += frontend.TEMPLATE_TTL + 1
    token = make_access_token(account_id='a', user_id='u-a')
    upstream['session_error_at'] = stage
    with pytest.raises(frontend.FrontendSessionError):
        asyncio.run(frontend.refresh_cached_frontend(token, token, {}))
    assert frontend.get_cached_frontend(token, token) is None
    assert frontend.get_session_cookie(token, token) == {}


def test_simultaneous_cold_requests_fetch_one_bootstrap(upstream, make_access_token):
    token = make_access_token(account_id='a', user_id='u-a')
    async def run():
        return await asyncio.gather(*(frontend.get_frontend_template(token, token, {}) for _ in range(6)))
    result = asyncio.run(run())
    assert len(set(result)) == 1
    assert len(upstream['calls']) == 2


def test_invalidation_rejects_inflight_old_bootstrap(upstream, make_access_token, monkeypatch):
    token = make_access_token(account_id='a', user_id='u-a')
    original = frontend.run_in_threadpool

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            result = await original(*args)
            started.set()
            await release.wait()
            return result

        monkeypatch.setattr(frontend, 'run_in_threadpool', delayed)
        pending = asyncio.create_task(frontend.get_frontend_template(token, token, {}))
        await started.wait()
        frontend.invalidate_frontend_cache()
        release.set()
        with pytest.raises(frontend.FrontendSessionError):
            await pending
        assert frontend.get_cached_frontend(token, token) is None

    asyncio.run(run())


def test_new_context_cannot_be_overwritten_by_slower_old_fetch(upstream, make_access_token, monkeypatch):
    token = make_access_token(account_id='a', user_id='u-a')
    original = frontend.run_in_threadpool

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(func, cookies, account_id, fingerprint):
            result = await original(func, cookies, account_id, fingerprint)
            if fingerprint['proxy_url'] == 'old':
                started.set()
                await release.wait()
            return result

        monkeypatch.setattr(frontend, 'run_in_threadpool', delayed)
        pending = asyncio.create_task(frontend.get_frontend_template(token, token, {'proxy_url': 'old'}))
        await started.wait()
        upstream['version'] = 2
        current = await frontend.get_frontend_template(token, token, {'proxy_url': 'new'})
        release.set()
        with pytest.raises(frontend.FrontendSessionError):
            await pending
        assert '"build": 2' in current
        assert frontend.get_cached_frontend(token, token)['html'] == current

    asyncio.run(run())


def test_chat_reuses_verified_website_auth_without_second_session_exchange(
        upstream, make_access_token, monkeypatch, db):
    from chatgpt import authorization
    import utils.globals as globals
    req = 'sess-cookie-a'
    access = make_access_token(account_id='a', user_id='u-a')
    db.upsert_account(req, plan_type='plus', status='healthy')
    monkeypatch.setattr(globals, 'error_token_list', [])
    asyncio.run(frontend.get_frontend_template(req, access, {}))
    async def unexpected_refresh(*args, **kwargs):
        pytest.fail('chat must reuse the account authentication already verified for its page')
    monkeypatch.setattr(authorization, 'sess2ac', unexpected_refresh)
    assert asyncio.run(authorization.verify_token(req)) == access
    db.upsert_account(req, status='disabled')
    from fastapi import HTTPException
    before = len(upstream['calls'])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(authorization.verify_token(req))
    assert exc.value.status_code == 401
    assert len(upstream['calls']) == before


def test_static_shell_composes_distinct_account_bootstrap():
    html_a = '<html><script id="client-bootstrap">{"session":{"account":{"id":"a"}}}</script></html>'
    html_b = '<html><script id="client-bootstrap">{"session":{"account":{"id":"b"}}}</script></html>'
    shell = frontend._static_shell(html_a)
    assert shell == frontend._static_shell(html_b)
    assert '"id":"a"' in frontend.compose_frontend({'html': html_a, 'static_shell': shell})
    assert '"id":"b"' in frontend.compose_frontend({'html': html_b, 'static_shell': shell})
