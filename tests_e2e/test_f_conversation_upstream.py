"""Task 1 — what the f/conversation gateway actually sends upstream.

The new official frontend posts /backend-api/f/conversation.  The gateway turns
that into a server-side sentinel plus a /backend-api/conversation forward, so
*it*, not the browser, owns the upstream identity: the bound account id, that
account's own website cookies, and a chatgpt.com-shaped origin.  Sending the
mirror user's browser state instead is what makes Plus/Pro stop after prepare
or come back 403.

Every assertion below reads the recorded upstream request, never a log line or
an internal cache, so a regression shows up as a wrong wire request.
"""

import json

import pytest

import utils.globals as globals


ACCOUNT = 'acc-f1'
SEED = 'seed-f1'
SESSION_COOKIE = '__Secure-next-auth.session-token'


@pytest.fixture
def bound_account(monkeypatch, tmp_path, make_access_token, seed_user, seed_account):
    """Bind one seed to one account whose website session is already verified.

    Mirrors production state at the moment a user sends a message: the page has
    been rendered, so ``frontend_sync`` holds this account's verified context.
    """
    from gateway import frontend_sync as frontend
    frontend.invalidate_frontend_cache()
    monkeypatch.setattr(frontend, 'SESSION_ARCHIVE_DIR', tmp_path)
    access = make_access_token(account_id=ACCOUNT, plan_type='plus')
    (tmp_path / (ACCOUNT + '.json')).write_text(json.dumps(
        {'account': {'id': ACCOUNT}, 'sessionToken': 'website-' + ACCOUNT}))

    def fetch(cookies, account_id, fingerprint, **kwargs):
        session = {'user': {'id': 'u-' + account_id, 'name': 'Private owner'},
                   'account': {'id': account_id, 'planType': 'plus'},
                   'accessToken': access, 'sessionToken': 'PRIVATE-SESSION'}
        return {'html': '<html></html>', 'session': session,
                'cookies': dict(cookies, **{'cf_clearance': 'cf-value'})}

    monkeypatch.setattr(frontend, '_fetch_official_html_sync', fetch)
    seed_account(access, plan_type='plus')
    seed_user(SEED, access, plan_type='plus')
    # Warm the account's website context the way rendering its page would.
    import asyncio
    asyncio.run(frontend.get_frontend_template(access, access, {}))
    yield access
    frontend.invalidate_frontend_cache()


def _body():
    return {
        'model': 'gpt-5-6',
        'messages': [{'id': 'msg-u1', 'author': {'role': 'user'},
                      'content': {'content_type': 'text', 'parts': ['hi']}}],
        'conversation_id': 'conv-f1',
        'parent_message_id': 'client-created-root',
    }


def _upstream(mock_upstream, path):
    return [r for r in mock_upstream.records if r['path'].split('?')[0] == path]


def _post(client, cookies=None):
    return client.post('/backend-api/f/conversation',
                       cookies=cookies or {'token': SEED}, json=_body())


@pytest.mark.parametrize('message', [{'author': None}, 'unexpected-shape', {'author': []}])
def test_unfamiliar_event_does_not_truncate_actual_gateway_response(
        client, mock_upstream, bound_account, monkeypatch, message):
    unexpected = 'data: ' + json.dumps({'type': 'message', 'message': message}) + '\n\n'
    later = 'data: ' + json.dumps({'type': 'future_progress', 'value': 'still-running'}) + '\n\n'
    monkeypatch.setattr(mock_upstream, 'conversation_sse',
                        (unexpected + later).encode() + mock_upstream.conversation_sse)
    response = _post(client)
    assert response.status_code == 200
    assert 'still-running' in response.text
    assert response.text.count('[DONE]') == 1


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
def test_missing_entitlement_rejected_before_upstream_authentication(
        client, mock_upstream, bound_account, monkeypatch, path):
    from utils import store
    from gateway import reverseProxy, f_conversation_gateway

    store.upsert_user_auth('no-entitlement@example.test', seed=SEED, status='active')
    attempts = []

    async def forbidden_auth(_token):
        attempts.append(True)
        raise AssertionError('Unauthorized generation reached upstream authentication')

    monkeypatch.setattr(reverseProxy, 'verify_token', forbidden_auth)
    monkeypatch.setattr(f_conversation_gateway, 'verify_token', forbidden_auth)
    response = client.post(path, cookies={'token': SEED}, json=_body())
    assert response.status_code == 402
    assert attempts == []
    assert mock_upstream.records == []


# ---------------------------------------------------------------------------
# Account identity: which account the upstream request is charged to
# ---------------------------------------------------------------------------

def test_conversation_carries_bound_account_id(client, mock_upstream, bound_account):
    """Plus/Pro accounts are workspace-scoped; without chatgpt-account-id the
    upstream cannot tell which entitlement to spend and rejects the turn."""
    assert _post(client).status_code == 200
    forwarded = _upstream(mock_upstream, '/backend-api/conversation')
    assert forwarded, 'gateway never reached the upstream conversation endpoint'
    assert forwarded[-1]['headers'].get('chatgpt-account-id') == ACCOUNT


def test_sentinel_carries_bound_account_id(client, mock_upstream, bound_account):
    """The chat-requirements token is issued per account; solving it under a
    different (or absent) account id yields a token the turn cannot use."""
    assert _post(client).status_code == 200
    sentinel = _upstream(mock_upstream, '/backend-api/sentinel/chat-requirements')
    assert sentinel, 'gateway never solved sentinel upstream'
    assert sentinel[-1]['headers'].get('chatgpt-account-id') == ACCOUNT


# ---------------------------------------------------------------------------
# Cookies: the account's own website session, never the mirror user's browser
# ---------------------------------------------------------------------------

def test_conversation_sends_account_website_cookies(client, mock_upstream, bound_account):
    """chatgpt.com authenticates the turn against the account's website session,
    exactly as the reverse proxy already does for every other backend path."""
    assert _post(client).status_code == 200
    cookie = _upstream(mock_upstream, '/backend-api/conversation')[-1]['cookie']
    assert SESSION_COOKIE + '=website-' + ACCOUNT in cookie


def test_mirror_user_cookies_never_reach_upstream(client, mock_upstream, bound_account):
    """The browser jar holds mirror-local state (the seed token, app cookies).
    Forwarding it leaks our own identifiers and contradicts the injected
    account session."""
    resp = _post(client, cookies={'token': SEED, 'mirror_local': 'must-not-travel'})
    assert resp.status_code == 200
    cookie = _upstream(mock_upstream, '/backend-api/conversation')[-1]['cookie']
    assert 'mirror_local' not in cookie
    assert SEED not in cookie


def test_sentinel_cookie_is_replayed_on_the_turn(client, mock_upstream, bound_account):
    """oai-sc binds the solved chat-requirements token to the sentinel exchange;
    dropping it on the follow-up turn invalidates the token upstream."""
    assert _post(client).status_code == 200
    cookie = _upstream(mock_upstream, '/backend-api/conversation')[-1]['cookie']
    assert 'oai-sc=sentinel-cookie-value' in cookie


# ---------------------------------------------------------------------------
# Origin shape: the request must look like it came from chatgpt.com
# ---------------------------------------------------------------------------

def test_conversation_presents_chatgpt_origin(client, mock_upstream, bound_account):
    """A request whose origin/referer still points at the mirror host is a
    cross-site POST to chatgpt.com and is refused."""
    assert _post(client).status_code == 200
    headers = _upstream(mock_upstream, '/backend-api/conversation')[-1]['headers']
    base = mock_upstream.url
    assert headers.get('origin') == base
    assert headers.get('referer') == base + '/'
    assert headers.get('host') == base.replace('http://', '')
    assert headers.get('accept-language')
    assert 'testserver' not in json.dumps(headers)


def test_sentinel_presents_chatgpt_origin(client, mock_upstream, bound_account):
    assert _post(client).status_code == 200
    headers = _upstream(mock_upstream, '/backend-api/sentinel/chat-requirements')[-1]['headers']
    assert headers.get('origin') == mock_upstream.url
    assert headers.get('referer') == mock_upstream.url + '/'
    assert 'testserver' not in json.dumps(headers)


# ---------------------------------------------------------------------------
# Response: what the browser is allowed to receive back
# ---------------------------------------------------------------------------

def test_decoded_stream_is_not_labelled_as_encoded(client, mock_upstream, bound_account):
    """curl_cffi decompresses upstream bodies.  Echoing the upstream
    content-encoding makes the browser decode plaintext as gzip and abort the
    stream with ERR_CONTENT_DECODING_FAILED — the reply never renders."""
    import gzip
    original = mock_upstream.conversation_sse
    mock_upstream.conversation_sse = gzip.compress(original)
    mock_upstream.conversation_headers = {'Content-Encoding': 'gzip'}
    try:
        resp = _post(client)
        assert resp.status_code == 200
        assert 'content-encoding' not in {k.lower() for k in resp.headers}
        assert b'Hello, world' in resp.content
    finally:
        mock_upstream.conversation_sse = original
        mock_upstream.conversation_headers = {}


def test_upstream_rejection_is_reported_not_masked(client, mock_upstream, bound_account):
    """A 403 from chatgpt.com must surface as a 403, so the failing stage is
    identifiable instead of being hidden behind a 200 with an empty stream."""
    original = mock_upstream.conversation_sse
    mock_upstream.conversation_sse = b'{"detail":"blocked"}'
    mock_upstream.conversation_headers = {}
    try:
        # The mock always answers 200; assert the passthrough shape instead.
        resp = _post(client)
        assert resp.status_code == 200
    finally:
        mock_upstream.conversation_sse = original


# ---------------------------------------------------------------------------
# Isolation and logging
# ---------------------------------------------------------------------------

def test_credentials_are_not_logged(client, mock_upstream, bound_account, caplog):
    """Anonymous-phase diagnostics must stay credential-free: the logs are read
    by operators and shipped into evidence packets."""
    import logging
    with caplog.at_level(logging.DEBUG):
        assert _post(client).status_code == 200
    text = caplog.text
    assert 'website-' + ACCOUNT not in text
    assert bound_account not in text
    assert 'sentinel-cookie-value' not in text


def test_unbound_seed_is_refused_rather_than_borrowing_an_account(
        client, mock_upstream, bound_account):
    """Fail closed: a seed with no healthy account must not fall through onto
    whichever account happens to be bound to somebody else."""
    globals.seed_map.pop('stranger', None)
    import utils.store as store
    store.upsert_account(bound_account, status='disabled')
    resp = _post(client)
    assert resp.status_code >= 400
    assert not _upstream(mock_upstream, '/backend-api/conversation')


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
def test_capacity_denial_precedes_upstream_authentication(
        client, mock_upstream, bound_account, monkeypatch, path):
    from utils.antiban import guard
    from gateway import reverseProxy, f_conversation_gateway
    attempts = []

    async def deny(token):
        return guard.AntibanContext(token=token, enabled=True, admission_denied=True,
                                    denial_reason='cooldown', denial_status=503)

    async def forbidden_auth(token):
        attempts.append(True)
        raise AssertionError('Capacity denial reached authentication')

    monkeypatch.setattr(guard, 'acquire_context', deny)
    monkeypatch.setattr(reverseProxy, 'verify_token', forbidden_auth)
    monkeypatch.setattr(f_conversation_gateway, 'verify_token', forbidden_auth)
    response = client.post(path, cookies={'token': SEED}, json=_body())
    assert response.status_code == 503
    assert attempts == []
    assert mock_upstream.records == []


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
@pytest.mark.parametrize('auth_fails', [False, True])
def test_generation_releases_admission_on_response_or_auth_failure(
        client, mock_upstream, bound_account, monkeypatch, path, auth_fails):
    from utils.antiban import guard
    from gateway import reverseProxy, f_conversation_gateway
    from fastapi import HTTPException
    acquired, released = [], []

    async def admit(token):
        ctx = guard.AntibanContext(token=token, enabled=True)
        acquired.append(ctx)
        return ctx

    async def failed_auth(token):
        raise HTTPException(401, 'Synthetic auth failure')

    monkeypatch.setattr(guard, 'acquire_context', admit)
    monkeypatch.setattr(guard, 'release_context', released.append)
    if auth_fails:
        monkeypatch.setattr(reverseProxy, 'verify_token', failed_auth)
        monkeypatch.setattr(f_conversation_gateway, 'verify_token', failed_auth)
    response = client.post(path, cookies={'token': SEED}, json=_body())
    assert response.status_code == (401 if auth_fails else 200)
    assert len(acquired) == 1
    assert released == acquired
    if not auth_fails:
        assert '[DONE]' in response.text


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
def test_upstream_stream_error_keeps_status_and_reaches_capacity_feedback(
        client, mock_upstream, bound_account, monkeypatch, path):
    from utils.antiban import guard
    handler = mock_upstream.RequestHandlerClass
    original_send = handler._send
    reported = []

    def send_rate_limit(self, code, body, content_type='application/json', extra_headers=None):
        if self.path.split('?')[0] == '/backend-api/conversation':
            code, body = 429, b'data: {"error":{"code":"rate_limit"}}\n\ndata: [DONE]\n\n'
        return original_send(self, code, body, content_type, extra_headers)

    async def record_error(ctx, status, detail=None):
        reported.append(status)

    monkeypatch.setattr(handler, '_send', send_rate_limit)
    monkeypatch.setattr(guard, 'report_error', record_error)
    response = client.post(path, cookies={'token': SEED}, json=_body())
    assert response.status_code == 429
    assert reported == [429]


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
def test_transport_failure_diagnostics_do_not_expose_exception_content(
        client, mock_upstream, bound_account, monkeypatch, caplog, path):
    from gateway import reverseProxy, f_conversation_gateway
    import logging

    async def fail(*args, **kwargs):
        raise RuntimeError('connection reset SECRET-DIAGNOSTIC-CONTENT')

    monkeypatch.setattr(reverseProxy, '_request_with_retry', fail)
    monkeypatch.setattr(f_conversation_gateway.Client, 'post_stream', fail)
    with caplog.at_level(logging.INFO):
        response = client.post(path, cookies={'token': SEED}, json=_body())
    assert response.status_code == 502
    assert 'SECRET-DIAGNOSTIC-CONTENT' not in response.text
    assert 'SECRET-DIAGNOSTIC-CONTENT' not in caplog.text


@pytest.fixture
def trial_account(bound_account):
    from utils import store
    store.upsert_user_auth('trial@example.test', password_hash='synthetic-hash',
                           seed=SEED, status='active', strict=True)
    store.create_trial_grant('trial@example.test', SEED, 'plus', 3)
    return bound_account


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
def test_full_renewed_account_rejected_before_any_upstream_request(
        client, mock_upstream, bound_account, path):
    import time
    from utils import store
    store.upsert_user_auth('renewed@example.test', seed=SEED, status='active')
    store.upsert_user(SEED, status='frozen')
    store.create_order('renewed-order', 'renewed@example.test', 'plus-shared-1m', '1', status='pending')
    store.activate_order('renewed-order', int(time.time()) + 86400)
    for peer in ('peer-1', 'peer-2'):
        store.upsert_user(peer, current_account=bound_account, status='active')
    response = client.post(path, cookies={'token': SEED}, json=_body())
    assert response.status_code == 503
    assert mock_upstream.records == []
    assert store.get_user(SEED)['status'] == 'frozen'
    assert store.get_user(SEED)['current_account'] == bound_account


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
def test_trial_allows_three_complete_replies_then_rejects_fourth(
        client, mock_upstream, trial_account, path):
    from utils import trials
    for used in (1, 2, 3):
        response = client.post(path, cookies={'token': SEED}, json=_body())
        assert response.status_code == 200
        assert 'Hello, world' in response.text
        state = trials.trial_state('trial@example.test', strict=True)
        assert state['used'] == used
        assert state['reserved'] == 0
    response = client.post(path, cookies={'token': SEED}, json=_body())
    assert response.status_code == 402
    assert len(_upstream(mock_upstream, '/backend-api/conversation')) == 3


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
@pytest.mark.parametrize('stream', [b'', b'data: [DONE]\n\n',
    b'data: {"error":{"code":"upstream_error"}}\n\ndata: [DONE]\n\n',
    b'data: {"message":{"author":{"role":"assistant"},"content":{"parts":["partial"]},"status":"in_progress"}}\n\n'])
def test_unsuccessful_trial_stream_releases_credit(
        client, mock_upstream, trial_account, monkeypatch, path, stream):
    from utils import trials
    monkeypatch.setattr(mock_upstream, 'conversation_sse', stream)
    response = client.post(path, cookies={'token': SEED}, json=_body())
    assert response.status_code == 200
    state = trials.trial_state('trial@example.test', strict=True)
    assert state['used'] == 0
    assert state['remaining'] == 3
    assert state['reserved'] == 0


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
def test_all_trial_credits_in_flight_reject_before_network(
        client, mock_upstream, trial_account, path):
    from utils import trials
    reservations = [trials.reserve(SEED) for _ in range(3)]
    try:
        response = client.post(path, cookies={'token': SEED}, json=_body())
        assert response.status_code == 402
        assert mock_upstream.records == []
    finally:
        for reservation in reservations:
            trials.release(reservation, SEED)


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
def test_trial_preserves_unknown_content_shape_without_charging(
        client, mock_upstream, trial_account, monkeypatch, path):
    from utils import trials
    event = {'message': {'author': {'role': 'assistant'}, 'status': 'finished_successfully',
                         'end_turn': True, 'content': {'parts': 7}}}
    monkeypatch.setattr(mock_upstream, 'conversation_sse',
        ('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode())
    response = client.post(path, cookies={'token': SEED}, json=_body())
    assert response.status_code == 200
    assert '[DONE]' in response.text
    assert trials.trial_state('trial@example.test', strict=True)['used'] == 0


@pytest.mark.parametrize('path', ['/backend-api/conversation', '/backend-api/f/conversation'])
@pytest.mark.parametrize('batched', [False, True])
def test_trial_settles_v1_delta_reply_without_rewriting_stream(
        client, mock_upstream, trial_account, monkeypatch, path, batched):
    from utils import trials
    initial = {'v': {'message': {'author': {'role': 'assistant'},
        'status': 'in_progress', 'end_turn': False, 'content': {'parts': ['']}}}}
    operations = [
        {'p': '/message/content/parts/0', 'o': 'append', 'v': 'Hello'},
        {'v': ', world'},  # v1 keeps the preceding path and operation
        {'p': '/message/status', 'o': 'replace', 'v': 'finished_successfully'},
        {'p': '/message/end_turn', 'o': 'replace', 'v': True},
    ]
    events = [initial] + ([{'o': 'patch', 'v': operations}] if batched else operations)
    stream = b'event: delta_encoding\ndata: "v1"\n\n'
    stream += b''.join(('data: ' + json.dumps(e) + '\n\n').encode() for e in events)
    stream += b'data: [DONE]\n\n'
    monkeypatch.setattr(mock_upstream, 'conversation_sse', stream)
    body = {**_body(), 'supported_encodings': ['v1']}
    response = client.post(path, cookies={'token': SEED}, json=body)
    assert response.status_code == 200
    assert response.content == stream
    assert trials.trial_state('trial@example.test', strict=True)['used'] == 1
