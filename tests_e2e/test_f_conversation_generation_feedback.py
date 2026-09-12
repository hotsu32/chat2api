"""Antiban feedback for the f/conversation stream, over the real HTTP route.

The ordinary proxy hands its body iterator to ``observe_generation_stream``,
which is what stamps the request as complete/failed/cancelled. The
f/conversation route instead forwards its own iterator to ``StreamingResponse``
and only reported *events*; nothing ever ended the record, so it stayed
``pending`` for the whole request -- and ``pending`` counted as delivered. A
2xx stream that raised mid-answer, that the browser cancelled, or that simply
ended without the upstream's terminal frame was therefore reported to the guard
as a successful turn, resetting a backoff and clearing risk state the account
had not earned.

These tests drive the real route over HTTP (or, for the disconnect, over the
real ASGI app with a hang-up receive) against the loopback mock, and assert on
guard state production acts on: the pacing cooldown ``report_success`` writes,
the circuit's anonymous counters, the per-account lease, and the trial ledger.
Nothing under test is mocked -- only the upstream's bytes.

Upstream behaviour is varied per test by replacing ``content_generator`` (the
transport -> SSE-event stage) or the mock's canned body.
"""

import asyncio
import contextlib
import json
import time

import pytest

import utils.configs as configs
import utils.globals as globals
from utils.antiban import bucket, circuit, concurrency, cooldown, guard

ACCOUNT = 'acc-ff1'
SEED = 'seed-ff1'
CONVERSATION = '/backend-api/f/conversation'
BUCKET_ID = 'bkt::ff1'
TRIAL_EMAIL = 'f-trial@example.test'

_REQUEST = {
    'model': 'gpt-5-6',
    'messages': [{'id': 'msg-u1', 'author': {'role': 'user'},
                  'content': {'content_type': 'text', 'parts': ['hi']}}],
    'conversation_id': 'conv-ff1',
    'parent_message_id': 'client-created-root',
}


@pytest.fixture(autouse=True)
def antiban_enabled(monkeypatch):
    """Every test here runs the real guard, with a fresh in-process state.

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


@pytest.fixture
def trial_account(account):
    """The same account, reached through a 3-reply SaaS trial ledger."""
    from utils import store
    store.upsert_user_auth(TRIAL_EMAIL, password_hash='synthetic-hash',
                           seed=SEED, status='active', strict=True)
    store.create_trial_grant(TRIAL_EMAIL, SEED, 'plus', 3)
    return account


@pytest.fixture
def outcomes(monkeypatch):
    """The end-state the route publishes for its iterator, as it publishes it.

    The wrapper still applies the state (the assertions below depend on the
    guard acting on it); this only makes the state machine itself visible, so a
    test can tell "reported as nothing" from "reported as cancelled".
    """
    from gateway import f_conversation_gateway, generation
    seen = []
    original = generation.observe_generation_end

    def record(request, outcome, exc=None):
        seen.append(outcome)
        return original(request, outcome, exc)

    monkeypatch.setattr(f_conversation_gateway, 'observe_generation_end', record)
    return seen


def _post(client, cookies=None):
    return client.post(CONVERSATION, cookies=cookies or {'token': SEED}, json=_REQUEST)


def _semaphore(token):
    """The per-account lease is keyed by the account credential, not the seed."""
    return concurrency._account_semaphores[token]


def _without_terminal_marker(sse):
    """The mock's stream with its end-of-stream frame removed.

    This is the shape a truncated upstream leaves behind: the assistant message
    is complete and the body ends cleanly, but nothing ever says the turn was
    delivered.
    """
    head, _sep, _tail = sse.rpartition(b'data: [DONE]')
    return head


def _drive_app(path=CONVERSATION, *, cut_when=None, fail_when=None):
    """Run one generation through the real ASGI app and cut the body short.

    The request is delivered normally, the response body is streamed, and the
    first chunk matching ``cut_when`` (default: the very first one) parks the
    send while the client hangs up; ``fail_when`` raises from the send instead.
    Either way the response never completes, which is the only way to drive that
    shape: ``TestClient`` cannot, because its transport reports
    ``http.disconnect`` only after the response has already completed.

    Returns the body chunks the app tried to send, so a test can assert which
    part of the answer had reached the client when it hung up.
    """
    import app as app_module

    body = json.dumps(_REQUEST).encode()
    scope = {
        'type': 'http', 'http_version': '1.1', 'method': 'POST',
        'path': path, 'raw_path': path.encode(),
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


def _answer_with(mock_upstream, monkeypatch, code, body, content_type):
    """Make the mock answer the forwarded conversation turn with exactly this."""
    handler = mock_upstream.RequestHandlerClass
    original = handler._send

    def send(self, code_, payload, ctype='application/json', extra_headers=None):
        if self.path.split('?')[0] == '/backend-api/conversation':
            code_, payload, ctype = code, body, content_type
        return original(self, code_, payload, ctype, extra_headers)

    monkeypatch.setattr(handler, '_send', send)


# ---------------------------------------------------------------------------
# The one shape that is a successful turn
# ---------------------------------------------------------------------------

def test_completed_f_stream_reports_success(client, mock_upstream, account, outcomes):
    """A stream that ends cleanly *and* carries the upstream's terminal frame is
    the account doing real work -- the only evidence that resets its backoff."""
    response = _post(client)
    assert response.status_code == 200
    assert '[DONE]' in response.text
    assert mock_upstream.records, 'the mock upstream was never reached'

    # report_success records the pacing cooldown for this account...
    assert cooldown.get_next_available(account) > time.time()
    # ...and the slot came back, exactly once.
    assert _semaphore(account)._value == configs.account_max_concurrency
    assert circuit.get_circuit_stats() == {}
    assert outcomes == ['complete']


# ---------------------------------------------------------------------------
# The three shapes that are not
# ---------------------------------------------------------------------------

def test_mid_stream_failure_is_not_a_success(
        client, mock_upstream, account, bound_bucket, monkeypatch, outcomes):
    """The response committed 200 at its first event, so an upstream that dies
    mid-answer is invisible from outside. It must not be filed as a delivered
    turn -- and the transport failure must still reach the circuit."""
    from gateway import f_conversation_gateway

    async def failing_stream(response, token, history=True):
        yield b'data: {"message": {"author": {"role": "assistant"}}}\n\n'
        raise ConnectionResetError('connection reset mid-stream')

    monkeypatch.setattr(f_conversation_gateway, 'content_generator', failing_stream)

    with contextlib.suppress(BaseException):
        # A body cut off mid-stream surfaces as a client-side protocol error
        # rather than a response object; the contract under test is the
        # server-side one asserted below.
        _post(client)

    assert mock_upstream.records, 'the mock upstream was never reached'
    assert cooldown.get_next_available(account) == 0.0, 'a broken stream was reported as success'
    assert circuit.get_circuit_stats().get('network_error.reset') == 1
    assert _semaphore(account)._value == configs.account_max_concurrency
    assert outcomes == ['failed']


def test_stream_without_a_terminal_marker_is_not_a_success(
        client, mock_upstream, account, monkeypatch, outcomes):
    """A clean end is not a delivery. Without the upstream's terminal frame the
    answer may have been truncated by any hop in between, and reporting success
    would reset the backoff of an account that never finished a turn."""
    monkeypatch.setattr(mock_upstream, 'conversation_sse',
                        _without_terminal_marker(mock_upstream.conversation_sse))

    response = _post(client)
    assert response.status_code == 200
    assert '[DONE]' not in response.text
    assert cooldown.get_next_available(account) == 0.0, 'an undelivered turn was reported as success'
    assert _semaphore(account)._value == configs.account_max_concurrency
    # The iterator did end cleanly -- which is exactly why "ended" cannot be
    # the success criterion on its own.
    assert outcomes == ['complete']


def test_terminal_marker_less_stream_charges_neither_trial_nor_risk(
        client, mock_upstream, trial_account, monkeypatch):
    """Same shape for a trial account: a reply that ends without the terminal
    frame must not be charged, and must not be reported to the guard as a turn
    the account completed."""
    from utils import trials
    monkeypatch.setattr(mock_upstream, 'conversation_sse',
                        _without_terminal_marker(mock_upstream.conversation_sse))

    response = _post(client)
    assert response.status_code == 200
    assert '[DONE]' not in response.text
    assert cooldown.get_next_available(trial_account) == 0.0
    state = trials.trial_state(TRIAL_EMAIL, strict=True)
    assert state['used'] == 0
    assert state['reserved'] == 0


def test_completed_f_stream_charges_the_trial_exactly_once(
        client, mock_upstream, trial_account):
    """The positive control for the two tests above: with the terminal frame
    present the same fixture does charge the trial, so "used == 0" there means
    released rather than never reserved."""
    from utils import trials
    response = _post(client)
    assert response.status_code == 200
    assert '[DONE]' in response.text
    state = trials.trial_state(TRIAL_EMAIL, strict=True)
    assert state['used'] == 1
    assert state['reserved'] == 0


def test_client_disconnect_is_not_a_success(
        client, mock_upstream, account, bound_bucket, monkeypatch, outcomes):
    """The browser hanging up mid-answer is the user's decision, not delivered.

    A client disconnect cannot be driven through ``TestClient``: its transport
    only reports ``http.disconnect`` after the response has completed. The real
    ASGI app is driven directly instead, with a receive that hangs up once the
    first body chunk is on the wire.
    """
    from gateway import f_conversation_gateway

    async def stalled_stream(response, token, history=True):
        yield b'data: {"message": {"author": {"role": "assistant"}}}\n\n'
        # Park here: the hang-up below is what has to end this request.
        await asyncio.Event().wait()

    monkeypatch.setattr(f_conversation_gateway, 'content_generator', stalled_stream)
    # `client` is requested for its side effect: it repoints the upstream base
    # URL list at the mock. The request itself is driven through the app.
    assert mock_upstream.url in configs.chatgpt_base_url_list

    _drive_app()

    assert cooldown.get_next_available(account) == 0.0, 'a cancelled turn was reported as success'
    assert circuit.get_circuit_stats() == {}
    assert _semaphore(account)._value == configs.account_max_concurrency
    assert outcomes == ['cancelled']


# ---------------------------------------------------------------------------
# Trials are charged for what the client received, exactly as /v1 charges them
# ---------------------------------------------------------------------------

def _terminal_chunk(chunk):
    """The last thing the browser receives: the upstream's end-of-stream frame."""
    return b'[DONE]' in chunk


def test_disconnect_after_the_answer_releases_the_trial(
        client, mock_upstream, trial_account, monkeypatch):
    """The P1: the finished assistant turn and the terminal frame were both
    observed, but the browser hung up before the response finished.

    Nobody received a complete reply, so the ledger must not charge -- the rule
    ``utils.trials.TrialAttempt`` already applies on /v1 ("只有「本次生成确实完成」
    且「客户端确实收到了完整响应」同时成立才结算"; a disconnect after a completion
    signal still releases). The positive control below is the same stream
    delivered in full, so ``used == 0`` here means released, not never reserved.
    """
    from utils import trials
    assert mock_upstream.url in configs.chatgpt_base_url_list

    sent = _drive_app(cut_when=_terminal_chunk)

    assert _terminal_chunk(b''.join(sent)), (
        'the disconnect must land after the terminal frame was produced, '
        'otherwise this is just a mid-stream cancellation'
    )
    state = trials.trial_state(TRIAL_EMAIL, strict=True)
    assert (state['used'], state['remaining'], state['reserved']) == (0, 3, 0)
    assert _semaphore(trial_account)._value == configs.account_max_concurrency


def test_send_failure_releases_the_trial(
        client, mock_upstream, trial_account, monkeypatch):
    """A send that fails ends the response with nothing delivered, so the
    reservation goes back -- and the slot does too, since the response task dies
    inside Starlette's task group where no background task runs."""
    from utils import trials
    assert mock_upstream.url in configs.chatgpt_base_url_list

    _drive_app(fail_when=_terminal_chunk)

    state = trials.trial_state(TRIAL_EMAIL, strict=True)
    assert (state['used'], state['remaining'], state['reserved']) == (0, 3, 0)
    assert _semaphore(trial_account)._value == configs.account_max_concurrency


def test_duplicate_finalization_moves_the_ledger_once(monkeypatch):
    """A second finalization must not move the ledger again.

    The response wrapper's ``finally`` and the pre-response failure path both
    funnel into ``_Trial.finish``; a route that finalized a trial itself, or a
    lifetime that closed twice, must not hand out two slots or charge one
    reservation twice. Driven at the state machine because one HTTP request
    never reaches two finalizations -- exactly the property being pinned.
    """
    from gateway import generation

    moved = []
    monkeypatch.setattr(generation.trials, 'settle',
                        lambda reservation, seed: moved.append('settle'))
    monkeypatch.setattr(generation.trials, 'release',
                        lambda reservation, seed: moved.append('release'))
    terminal = [
        ('data: ' + json.dumps({'message': {
            'author': {'role': 'assistant'}, 'status': 'finished_successfully',
            'end_turn': True, 'content': {'parts': ['an answer']}}}) + '\n\n').encode(),
        b'data: [DONE]\n\n',
    ]

    delivered = generation._Trial('reservation-1', SEED)
    for event in terminal:
        delivered.observe(event)
    delivered.finish(True)        # the whole response reached the client
    delivered.finish(True)        # ...and the lifetime closed again
    delivered.finish(False)       # ...and a late release callback arrived

    released = generation._Trial('reservation-2', SEED)
    for event in terminal:
        released.observe(event)
    released.finish(False)        # hung up before the response finished
    released.finish(True)         # the completion callback arrives late

    assert moved == ['settle', 'release']


# ---------------------------------------------------------------------------
# The state a stream is in before anything was reported
# ---------------------------------------------------------------------------

def test_unclassifiable_2xx_is_not_credited_on_its_status(
        client, mock_upstream, account, monkeypatch):
    """A 2xx with no body iterator has no delivery evidence and is not credited.

    The record is left ``pending`` -- the state a request is in before anything
    was reported -- and a chat turn does not arrive as a non-SSE body, so there
    is nothing here to justify resetting the account's backoff. Pinned because
    the tempting "repair" (credit the status code) is exactly the false success
    this contract exists to stop.
    """
    _answer_with(mock_upstream, monkeypatch, 200, b'{"detail":"not a stream"}',
                 'application/json')

    response = _post(client)
    assert response.status_code == 200
    assert cooldown.get_next_available(account) == 0.0, 'a pending record was credited'
    assert circuit.get_circuit_stats() == {}
    assert _semaphore(account)._value == configs.account_max_concurrency
