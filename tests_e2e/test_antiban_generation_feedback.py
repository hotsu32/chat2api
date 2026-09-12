"""Antiban feedback over real HTTP, with antiban enabled.

Unit tests can show that a decision was made; they cannot show that the real
FastAPI stack -- routing, the account pool, curl_cffi's response object, the
ASGI response lifetime -- reaches that decision. Every test here posts to the
gateway over HTTP against a loopback mock of chatgpt.com with zero real network
and zero real credentials, then asserts on the guard state production would act
on: the cooldown a turn created, the circuit reason it was filed under, the
bucket it degraded, and the per-account slot it returned.

The mock upstream is deterministic (see tests_e2e/conftest.py); upstream
behaviour is varied per test by rewriting the mock's response.
"""

import asyncio
import contextlib
import json
import time

import anyio
import pytest

import utils.configs as configs
import utils.globals as globals
from utils.antiban import bucket, circuit, concurrency, cooldown, guard

ACCOUNT = 'acc-af1'
SEED = 'seed-af1'
CONVERSATION = '/backend-api/conversation'
BUCKET_ID = 'bkt::af1'

_REQUEST = {
    'model': 'gpt-5-6',
    'messages': [{'id': 'msg-u1', 'author': {'role': 'user'},
                  'content': {'content_type': 'text', 'parts': ['hi']}}],
    'conversation_id': 'conv-af1',
    'parent_message_id': 'client-created-root',
}


@pytest.fixture(autouse=True)
def antiban_enabled(monkeypatch):
    """Every test in this file runs the real guard, with a fresh in-process state.

    These module-level dicts are the guard's whole memory; leaving them dirty
    would leak a cooldown or a degraded bucket into the next test.
    """
    monkeypatch.setattr(configs, 'enable_antiban', True)
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    cooldown._extend_reasons.clear()
    cooldown.reset_cooldown_stats()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    circuit.reset_circuit_stats()
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()
    guard.reset_admission_stats()
    yield
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()


@pytest.fixture
def account(monkeypatch, tmp_path, make_access_token, seed_user, seed_account):
    """One seed bound to one verified account, as production holds it mid-turn."""
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
    import asyncio
    asyncio.run(frontend.get_frontend_template(access, access, {}))
    yield access
    frontend.invalidate_frontend_cache()


@pytest.fixture
def bound_bucket(monkeypatch):
    """Put the account in a real IP bucket so bucket-level effects are visible."""
    globals.antiban_bucket['buckets'][BUCKET_ID] = {
        'proxy_url': None,
        'proxy_name': None,
        'group': '',
        'accounts': [],
        'last_request_at': {},
        'status': 'healthy',
        'degraded_until': 0,
        'created_at': 0,
    }
    globals.antiban_bucket['account_index'][ACCOUNT] = BUCKET_ID
    monkeypatch.setattr(bucket, 'assign_account', lambda token: BUCKET_ID)
    return BUCKET_ID


@contextlib.contextmanager
def _shared_loop(client):
    """Give every request in one test the same event loop.

    The per-account lease and the cooldown locks are asyncio primitives keyed by
    account, and TestClient opens a fresh portal -- a fresh loop -- for each
    request unless it is given one. A loop created here is honoured for the whole
    block without running the app lifespan, which would re-register the app's
    scheduler jobs and fail on the duplicate id.
    """
    with anyio.from_thread.start_blocking_portal() as portal:
        client.portal = portal
        try:
            yield client
        finally:
            client.portal = None


def _post(client, cookies=None):
    return client.post(CONVERSATION, cookies=cookies or {'token': SEED}, json=_REQUEST)


def _forget_fingerprint_profile(token):
    """Make the next turn regenerate its fingerprint, as the first one does.

    With antiban on, ``fingerprint.ensure_extended`` writes browser-JS profile
    fields (``screen``, ``viewport``, ``webgl`` ...) into the same ``fp_map``
    entry that ``get_fp`` hands back, and ``gateway/reverseProxy.py`` then runs
    ``headers.update(fp)`` -- curl_cffi rejects a dict header value, so a second
    turn on a warmed-up account dies with a 502. That defect is outside this
    change and is pinned by
    ``test_second_turn_on_a_warmed_up_account_is_currently_broken``; dropping the
    entry keeps these tests measuring the lease, which is what they are for.
    """
    globals.fp_map.pop(token, None)


def _semaphore(token):
    """The per-account lease is keyed by the account credential, not the seed."""
    return concurrency._account_semaphores[token]


def _drive_app(*, cut_when=None, fail_when=None):
    """Run one generation through the real ASGI app and cut the body short.

    The request is delivered normally, the response body is streamed, and the
    first chunk matching ``cut_when`` (default: the very first one) parks the
    send while the client hangs up; ``fail_when`` raises from the send instead.
    Either way the response never completes, which is the only way to drive that
    shape: ``TestClient`` cannot, because its transport reports
    ``http.disconnect`` only after the response has already completed.

    Returns the body chunks the app tried to send, so a test can assert how much
    of the answer had reached the client when it hung up.
    """
    import app as app_module

    body = json.dumps(_REQUEST).encode()
    scope = {
        'type': 'http', 'http_version': '1.1', 'method': 'POST',
        'path': CONVERSATION, 'raw_path': CONVERSATION.encode(),
        'query_string': b'', 'scheme': 'http',
        'headers': [(b'cookie', f'token={SEED}'.encode()),
                    (b'content-type', b'application/json')],
        'client': ('127.0.0.1', 12345), 'server': ('testserver', 80),
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
    }
    cut = cut_when or (lambda chunk: True)
    chunks = []

    async def run():
        hanging_up = asyncio.Event()
        request_sent = []

        async def receive():
            if not request_sent:
                request_sent.append(True)
                return {'type': 'http.request', 'body': body, 'more_body': False}
            # Hang up once the response reached the point this test cut it at.
            await hanging_up.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            if message['type'] != 'http.response.body':
                return
            chunk = message.get('body', b'') or b''
            chunks.append(chunk)
            if not message.get('more_body', False):
                return
            if fail_when is not None and fail_when(chunk):
                hanging_up.set()
                raise OSError('synthetic send failure')
            if cut(chunk):
                hanging_up.set()
                # Park: the hang-up above is what has to end this request.
                await asyncio.Event().wait()

        with contextlib.suppress(BaseException):
            await app_module.app(scope, receive, send)

    asyncio.run(run())
    return chunks


def _terminal_chunk(chunk):
    """The last thing the browser receives: the upstream's end-of-stream frame."""
    return b'[DONE]' in chunk


def _reject(mock_upstream, monkeypatch, status, body, content_type='application/json'):
    handler = mock_upstream.RequestHandlerClass
    original = handler._send

    def send(self, code, payload, ctype='application/json', extra_headers=None):
        if self.path.split('?')[0] == CONVERSATION:
            code, payload, ctype = status, body, content_type
        return original(self, code, payload, ctype, extra_headers)

    monkeypatch.setattr(handler, '_send', send)


# ---------------------------------------------------------------------------
# Admission: capacity is taken, and given back
# ---------------------------------------------------------------------------

def test_admitted_turn_is_reported_successful_and_returns_its_slot(client, mock_upstream, account):
    """A completed turn is the account working. Nothing else in the gateway
    path ever said so, so backoff never reset and the account was never marked
    used -- risk state drifted away from production behaviour."""
    response = _post(client)
    assert response.status_code == 200
    assert '[DONE]' in response.text
    assert mock_upstream.records, 'the mock upstream was never reached'

    # report_success records the pacing cooldown for this account.
    assert cooldown.get_next_available(account) > time.time()
    # ...and the slot came back, exactly once (limit 5, one request in flight).
    assert _semaphore(account)._value == configs.account_max_concurrency
    assert circuit.get_circuit_stats() == {}


def test_repeated_turns_never_leak_or_over_release_capacity(client, mock_upstream, account, monkeypatch, caplog):
    """limit=1 makes a single leaked slot fatal for the next turn, and an
    over-release is reported by BoundedSemaphore. Sequential turns must do
    neither."""
    import logging
    monkeypatch.setattr(configs, 'account_max_concurrency', 1)
    # One event loop for the whole test: the per-account lease is an asyncio
    # primitive, and a bare TestClient gives every request its own loop.
    with _shared_loop(client), caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert _post(client).status_code == 200
            # Each success starts the account's pacing cooldown; clear it the way
            # time passing would, so this measures the lease, not the pacing.
            cooldown._account_next_available.clear()
            _forget_fingerprint_profile(account)
    assert _semaphore(account)._value == 1
    assert 'over-release' not in caplog.text


def test_capacity_is_released_when_the_upstream_refuses(client, mock_upstream, account, monkeypatch):
    _reject(mock_upstream, monkeypatch, 429, b'{"detail":"rate-limit"}')
    assert _post(client).status_code == 429
    assert _semaphore(account)._value == configs.account_max_concurrency


@pytest.mark.parametrize('reason', ['cooldown', 'bucket_degraded', 'concurrency'])
def test_admission_denials_are_announced_and_never_reach_the_upstream(
        client, mock_upstream, account, monkeypatch, reason):
    """A denied request must be refused before any upstream work, and the
    refusal reason has to survive into the guard's anonymous stats."""
    if reason == 'cooldown':
        cooldown.extend_cooldown(account, 3600, reason='rate_limit')
    elif reason == 'bucket_degraded':
        monkeypatch.setattr(bucket, 'assign_account', lambda token: BUCKET_ID)
        globals.antiban_bucket['buckets'][BUCKET_ID] = {
            'proxy_url': None, 'group': '', 'accounts': [], 'last_request_at': {},
            'status': 'degraded', 'degraded_until': int(time.time()) + 3600, 'created_at': 0,
        }
    else:
        # A zero-width slot is the same admission decision as a saturated one.
        monkeypatch.setattr(configs, 'account_max_concurrency', 0)
        monkeypatch.setattr(configs, 'account_concurrency_wait_seconds', 0.05)

    response = _post(client)
    assert response.status_code == 503
    assert reason in response.text
    assert guard.get_admission_stats().get(reason) == 1
    assert mock_upstream.records == []
    if reason != 'concurrency':
        # Denied before the lease step, so no per-account semaphore ever exists.
        # (A saturated account legitimately has one; that case is covered by the
        # cap tests above.)
        assert concurrency._account_semaphores == {}


def test_dead_account_is_refused_with_its_own_status(
        client, mock_upstream, account, monkeypatch):
    """403 (do not retry this account) must stay distinguishable from the 503s
    (retry elsewhere). The binding predates the account dying, which is exactly
    the race the admission gate exists to catch."""
    from chatgpt import authorization
    monkeypatch.setattr(authorization, '_account_is_usable', lambda token: True)
    circuit.mark_dead(account, 'account_deactivated')

    response = _post(client)
    assert response.status_code == 403
    assert 'account_dead' in response.text
    assert guard.get_admission_stats().get('account_dead') == 1
    assert mock_upstream.records == []


# ---------------------------------------------------------------------------
# Upstream refusals reach the circuit with their own reason
# ---------------------------------------------------------------------------

def test_rate_limit_is_filed_as_rate_limit_and_starts_a_backoff(client, mock_upstream, account, monkeypatch):
    """A 429 is about this account; the gateway used to hand it to the client
    without the circuit ever seeing it, so no backoff was ever applied."""
    _reject(mock_upstream, monkeypatch, 429, b'{"detail":"rate-limit"}')
    assert _post(client).status_code == 429

    assert circuit.get_circuit_stats().get('rate_limit:429') == 1
    assert circuit._account_backoff_level.get(account) == 1
    assert cooldown.get_next_available(account) > time.time() + 1700


def test_invalid_credential_goes_to_the_recovery_list_not_the_dead_list(client, mock_upstream, account, monkeypatch):
    """401 means the account's credential was rejected. It is recoverable via
    the refresh flow, so it must not be marked dead -- and must not be silent."""
    _reject(mock_upstream, monkeypatch, 401, b'{"detail":"invalid_grant"}')
    assert _post(client).status_code == 401

    assert account in globals.error_token_list
    assert circuit.is_token_dead(account) is False
    assert circuit.get_circuit_stats().get('auth_invalid:401') == 1


def test_cloudflare_challenge_degrades_the_ip_bucket(client, mock_upstream, account, bound_bucket, monkeypatch):
    """The challenge page is a per-IP verdict. Detecting it needs a fixed
    protocol marker from the body; the body itself must never leave the module."""
    _reject(mock_upstream, monkeypatch, 403, b'<html>cf_chl_opt</html>', 'text/html')
    assert _post(client).status_code == 403

    meta = bucket.get_bucket_meta(BUCKET_ID)
    assert meta['status'] == 'degraded'
    assert meta['degraded_until'] > int(time.time())
    assert circuit.get_circuit_stats().get('cf_challenge:403') == 1


def test_account_deactivated_body_marks_the_account_dead(client, mock_upstream, account, monkeypatch):
    _reject(mock_upstream, monkeypatch, 403, b'{"detail":"account_deactivated"}')
    assert _post(client).status_code == 403

    assert circuit.is_token_dead(account) is True
    assert globals.antiban_dead_tokens[account]['reason'] == 'account_deactivated'


def test_unclassified_refusal_is_still_distinguishable_by_status(client, mock_upstream, account, monkeypatch):
    """Not every upstream refusal maps to a named reason. It must still be
    countable rather than silently dropped."""
    _reject(mock_upstream, monkeypatch, 403, b'{"detail":"no marker here"}')
    assert _post(client).status_code == 403
    assert circuit.get_circuit_stats().get('unclassified:403') == 1


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------

def test_transport_failure_is_filed_as_network_and_degrades_only_at_the_threshold(
        client, mock_upstream, account, bound_bucket, monkeypatch):
    """The route turns a dead connection into a 502. Without a network report
    the guard could not tell a dead proxy from an upstream 5xx: it never
    degraded the bucket, so every account on that IP kept being sent."""
    from gateway import reverseProxy

    def fail(*args, **kwargs):
        raise ConnectionResetError('connection reset by peer')

    monkeypatch.setattr(reverseProxy, '_request_with_retry', fail)

    with _shared_loop(client):
        for attempt in (1, 2, 3):
            assert _post(client).status_code == 502
            assert _semaphore(account)._value == configs.account_max_concurrency, 'slot not returned'
            if attempt < 3:
                assert bucket.get_bucket_meta(BUCKET_ID)['status'] == 'healthy'
            _forget_fingerprint_profile(account)
    assert bucket.get_bucket_meta(BUCKET_ID)['status'] == 'degraded'
    assert circuit.get_circuit_stats().get('network_error.reset') == 3


def test_mid_stream_failure_is_filed_as_network_without_a_success(
        client, mock_upstream, account, bound_bucket, monkeypatch):
    """Once a stream has started the status is already 200 and committed, so an
    upstream that dies mid-answer is invisible from outside. It must still not
    be recorded as a successful turn, and the slot must still come back."""
    from gateway import reverseProxy

    async def failing_stream(response, token, history=True):
        yield b'data: {"message": {"author": {"role": "assistant"}}}\n\n'
        raise ConnectionResetError('connection reset mid-stream')

    monkeypatch.setattr(reverseProxy, 'content_generator', failing_stream)

    try:
        response = _post(client)
        assert response.status_code == 200
    except Exception:
        # The truncated body may surface as a client-side protocol error; the
        # contract under test is the server-side one asserted below.
        pass

    assert circuit.get_circuit_stats().get('network_error.reset') == 1
    assert cooldown.get_next_available(account) == 0.0, 'a failed turn was reported as success'
    assert _semaphore(account)._value == configs.account_max_concurrency


def test_error_frame_inside_a_200_stream_is_not_a_success(
        client, mock_upstream, account, bound_bucket):
    """200 + an error frame is a failed turn. The upstream really does this."""
    mock_upstream.conversation_sse = (b'data: {"error": {"code": "synthetic"}}\n\n'
                                      b'data: [DONE]\n\n')
    response = _post(client)
    assert response.status_code == 200

    assert cooldown.get_next_available(account) == 0.0
    assert _semaphore(account)._value == configs.account_max_concurrency


def test_stream_without_a_terminal_marker_is_not_a_success(
        client, mock_upstream, account, monkeypatch):
    """A stream that ends cleanly is not a delivered turn.

    ``observe_generation_stream`` stamps the record complete whenever its
    iterator runs out -- which it also does for a body cut short by any hop in
    between. Only the upstream's own end-of-stream frame separates "ended" from
    "delivered", and without it a truncated answer reset the account's backoff.
    """
    head, _sep, _tail = mock_upstream.conversation_sse.rpartition(b'data: [DONE]')
    monkeypatch.setattr(mock_upstream, 'conversation_sse', head)

    response = _post(client)
    assert response.status_code == 200
    assert '[DONE]' not in response.text
    assert cooldown.get_next_available(account) == 0.0, 'an undelivered turn was reported as success'
    assert _semaphore(account)._value == configs.account_max_concurrency


# ---------------------------------------------------------------------------
# Trials are charged for what the client received, exactly as /v1 charges them
# ---------------------------------------------------------------------------

TRIAL_EMAIL = 'gateway-trial@example.test'


@pytest.fixture
def trial_account(account):
    """The same account, reached through a 3-reply SaaS trial ledger."""
    from utils import store
    store.upsert_user_auth(TRIAL_EMAIL, password_hash='synthetic-hash',
                           seed=SEED, status='active', strict=True)
    store.create_trial_grant(TRIAL_EMAIL, SEED, 'plus', 3)
    return account


def test_delivered_completion_charges_the_trial_once(client, mock_upstream, trial_account):
    """Positive control: when the whole response reaches the client the same
    fixture does charge, so "used == 0" below means released rather than never
    reserved."""
    from utils import trials
    response = _post(client)
    assert response.status_code == 200
    assert '[DONE]' in response.text
    state = trials.trial_state(TRIAL_EMAIL, strict=True)
    assert (state['used'], state['remaining'], state['reserved']) == (1, 2, 0)


def test_disconnect_after_the_answer_releases_the_trial(client, mock_upstream, trial_account):
    """The finished assistant turn and the terminal frame were both observed,
    but the browser hung up before the response finished.

    Nobody received a complete reply, so the ledger must not charge -- the rule
    ``utils.trials.TrialAttempt`` already applies on /v1 ("只有「本次生成确实完成」
    且「客户端确实收到了完整响应」同时成立才结算"; a disconnect after a completion
    signal still releases).
    """
    from utils import trials
    # `client` is requested for its side effect: it repoints the upstream base
    # URL list at the mock. The request itself is driven through the app.
    assert mock_upstream.url in configs.chatgpt_base_url_list

    sent = _drive_app(cut_when=_terminal_chunk)

    assert _terminal_chunk(b''.join(sent)), (
        'the disconnect must land after the terminal frame was produced, '
        'otherwise this is just a mid-stream cancellation'
    )
    state = trials.trial_state(TRIAL_EMAIL, strict=True)
    assert (state['used'], state['remaining'], state['reserved']) == (0, 3, 0)
    assert _semaphore(trial_account)._value == configs.account_max_concurrency


def test_send_failure_releases_the_trial(client, mock_upstream, trial_account):
    """A send that fails ends the response with nothing delivered, so the
    reservation goes back -- and the slot does too, since the response task dies
    inside Starlette's task group where no background task runs."""
    from utils import trials
    assert mock_upstream.url in configs.chatgpt_base_url_list

    _drive_app(fail_when=_terminal_chunk)

    state = trials.trial_state(TRIAL_EMAIL, strict=True)
    assert (state['used'], state['remaining'], state['reserved']) == (0, 3, 0)
    assert _semaphore(trial_account)._value == configs.account_max_concurrency


# ---------------------------------------------------------------------------
# Cross-slice regression: the warmed fingerprint remains transport-safe
# ---------------------------------------------------------------------------
def test_second_turn_on_a_warmed_up_account_remains_available(client, mock_upstream, account):
    """Extended browser-profile metadata never becomes an outbound HTTP header."""
    with _shared_loop(client):
        assert _post(client).status_code == 200
        cooldown._account_next_available.clear()
        assert _post(client).status_code == 200
