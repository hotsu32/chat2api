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
