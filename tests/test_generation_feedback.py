"""Antiban feedback out of the gateway generation lifetime.

The guard's cooldown / backoff / dead-account / bucket-degrade state is only as
true as what the generation path tells it. Before this contract existed the
gateway routes reported almost nothing: a completed turn never reached
``report_success``, and a transport failure was rewritten by the route into a
502/503 response that the guard never saw as a network failure at all.

These tests drive the real ASGI response lifetime and assert, for every way a
generation can end, exactly what the guard is told and that capacity comes back
exactly once. The assertions read the guard call production would make, so a
regression shows up as a wrong or missing report rather than as a private
structure changing shape.
"""

import asyncio
import json

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from gateway.generation import (admit_generation, generation_lifetime,
                                observe_generation_stream, track_generation_client)
from utils import trials
from utils.antiban import guard

TOKEN = 'synthetic-account'
SCOPE = {'type': 'http', 'method': 'POST', 'path': '/backend-api/conversation',
         'headers': [], 'asgi': {'version': '3.0'}}


class _GuardRecorder:
    """Records what the generation lifetime tells the guard."""

    def __init__(self):
        self.success = []
        self.errors = []
        self.network = []
        self.releases = []

    def install(self, monkeypatch):
        async def success(ctx):
            self.success.append(ctx.token)

        async def error(ctx, status_code, detail=None):
            self.errors.append((status_code, detail))

        async def network(ctx, kind=""):
            self.network.append(kind)

        def release(ctx):
            self.releases.append(ctx)

        monkeypatch.setattr(guard, 'report_success', success)
        monkeypatch.setattr(guard, 'report_error', error)
        monkeypatch.setattr(guard, 'report_network_error', network)
        monkeypatch.setattr(guard, 'release_context', release)


class _Upstream:
    def __init__(self):
        self.closed = []

    async def close(self):
        self.closed.append('closed')

    async def discard(self):
        self.closed.append('discarded')


async def _completed_events():
    message = {'message': {'author': {'role': 'assistant'},
                           'content': {'parts': ['synthetic reply']},
                           'status': 'finished_successfully', 'end_turn': True}}
    yield ('data: ' + json.dumps(message) + '\n\n').encode()
    yield b'data: [DONE]\n\n'


async def _error_frame_events():
    yield b'data: {"error": {"code": "synthetic_upstream_error"}}\n\n'
    yield b'data: [DONE]\n\n'


async def _failing_events():
    yield b'data: {"message": {"author": {"role": "assistant"}}}\n\n'
    raise ConnectionResetError('synthetic upstream reset')


async def _disconnecting_events():
    yield b'data: {"message": {"author": {"role": "assistant"}}}\n\n'
    await asyncio.Future()


# scenario -> (status, body, content_type). Streaming scenarios use a callable.
_SHAPES = {
    'status_401': (401, b'{"detail":"synthetic refusal"}', 'application/json'),
    'status_403': (403, b'{"detail":"synthetic refusal"}', 'application/json'),
    'status_403_cf': (403, b'<html>cf_chl_opt</html>', 'text/html'),
    'status_403_dead': (403, b'{"detail":"account_deactivated"}', 'application/json'),
    'status_429': (429, b'{"detail":"rate-limit"}', 'application/json'),
    'status_500': (500, b'{"detail":"synthetic upstream failure"}', 'application/json'),
    'status_503': (503, b'{"detail":"upstream unavailable"}', 'application/json'),
}

_RAISED = {
    'raised_transport': (502, ConnectionResetError('synthetic upstream reset')),
    'raised_404': (404, None),
    'raised_503': (503, None),
    'raised_internal': (500, ValueError('synthetic internal failure')),
}


def _build_handler(scenario):
    @generation_lifetime
    async def handler(request):
        await admit_generation(request, TOKEN)
        if scenario in _RAISED:
            status, cause = _RAISED[scenario]
            if cause is not None:
                try:
                    raise cause
                except type(cause):
                    # Mirrors how the gateway rewrites a transport failure: the
                    # original error stays reachable through the exception chain.
                    raise HTTPException(status_code=status, detail='Upstream request failed')
            raise HTTPException(status_code=status, detail='Gateway refusal')
        if scenario == 'raised_cancelled':
            raise asyncio.CancelledError()
        if scenario == 'complete':
            track_generation_client(request, _Upstream())
            return StreamingResponse(observe_generation_stream(request, _completed_events()),
                                     media_type='text/event-stream', status_code=200)
        if scenario == 'error_frame':
            return StreamingResponse(observe_generation_stream(request, _error_frame_events()),
                                     media_type='text/event-stream', status_code=200)
        if scenario == 'stream_error':
            return StreamingResponse(observe_generation_stream(request, _failing_events()),
                                     media_type='text/event-stream', status_code=200)
        if scenario == 'disconnect':
            return StreamingResponse(
                observe_generation_stream(request, _disconnecting_events()),
                media_type='text/event-stream', status_code=200)
        status, body, content_type = _SHAPES[scenario]
        return Response(content=body, status_code=status, media_type=content_type)

    return handler


async def _drive(scenario, monkeypatch, recorder, trial=False):
    """Run one admitted request end to end and return what the guard was told.

    ``recorder`` is installed before the request starts; pass None to install
    your own guard stand-ins instead.
    """
    accounting = []
    monkeypatch.setattr(trials, 'reserve', lambda seed: 'synthetic-reservation' if trial else None)
    monkeypatch.setattr(trials, 'settle', lambda rid, seed: accounting.append('settled'))
    monkeypatch.setattr(trials, 'release', lambda rid, seed: accounting.append('released'))

    async def acquire(token):
        return guard.AntibanContext(token=TOKEN, enabled=True, concurrency_acquired=True)

    monkeypatch.setattr(guard, 'acquire_context', acquire)
    if recorder is not None:
        recorder.install(monkeypatch)

    handler = _build_handler(scenario)
    try:
        response = await handler(Request(SCOPE))
    except BaseException:
        # Scenarios that raise never produce a response; the release and the
        # feedback have already happened on the way out.
        return accounting

    started = asyncio.Event()

    async def receive():
        if scenario == 'disconnect':
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return {'type': 'http.disconnect'}
        await asyncio.Future()

    async def send(message):
        if message['type'] == 'http.response.body':
            started.set()

    try:
        await response(SCOPE, receive, send)
    except BaseException:
        pass
    return accounting


# ---------------------------------------------------------------------------
# Exactly one release, whatever the exit looks like
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('scenario', [
    'complete', 'error_frame', 'stream_error', 'status_429', 'status_403_cf',
    'raised_transport', 'raised_404', 'raised_503', 'raised_internal',
])
def test_capacity_is_returned_exactly_once(monkeypatch, scenario):
    recorder = _GuardRecorder()

    async def run():
        return await _drive(scenario, monkeypatch, recorder)

    accounting = asyncio.run(run())
    assert len(recorder.releases) == 1
    assert accounting == []


def test_capacity_is_returned_exactly_once_when_the_handler_is_cancelled(monkeypatch):
    recorder = _GuardRecorder()

    async def run():
        return await _drive('raised_cancelled', monkeypatch, recorder)

    asyncio.run(run())
    assert len(recorder.releases) == 1
    assert recorder.success == []
    assert recorder.network == []
    assert recorder.errors == []


def test_trial_is_handed_back_once_when_the_response_never_starts(monkeypatch):
    recorder = _GuardRecorder()

    async def run():
        return await _drive('raised_transport', monkeypatch, recorder, trial=True)

    accounting = asyncio.run(run())
    assert len(recorder.releases) == 1
    assert accounting == ['released']


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------

def test_completed_generation_reports_success(monkeypatch):
    recorder = _GuardRecorder()

    async def run():
        return await _drive('complete', monkeypatch, recorder)

    asyncio.run(run())
    assert recorder.success == [TOKEN]
    assert recorder.errors == []
    assert recorder.network == []


def test_completed_generation_settles_the_trial_once(monkeypatch):
    recorder = _GuardRecorder()

    async def run():
        return await _drive('complete', monkeypatch, recorder, trial=True)

    assert asyncio.run(run()) == ['settled']


def test_stream_carrying_an_error_frame_is_not_a_success(monkeypatch):
    """200 + an error frame is a failed turn. Counting it as success would reset
    the account's backoff on the strength of a generation that never happened."""
    recorder = _GuardRecorder()

    async def run():
        return await _drive('error_frame', monkeypatch, recorder, trial=True)

    accounting = asyncio.run(run())
    assert recorder.success == []
    assert recorder.network == []
    assert accounting == ['released']


# ---------------------------------------------------------------------------
# Network / transport failures
# ---------------------------------------------------------------------------

def test_mid_stream_transport_failure_reports_a_network_reason(monkeypatch):
    recorder = _GuardRecorder()

    async def run():
        return await _drive('stream_error', monkeypatch, recorder)

    asyncio.run(run())
    # ConnectionResetError normalises to the circuit enum, not to an exception name.
    assert recorder.network == ['reset']
    assert recorder.success == []


def test_raised_transport_failure_is_classified_from_the_exception_chain(monkeypatch):
    """The route rewrites a transport failure into HTTPException(502). The
    original error survives in the chain, and that is what gets classified."""
    recorder = _GuardRecorder()

    async def run():
        return await _drive('raised_transport', monkeypatch, recorder)

    asyncio.run(run())
    assert recorder.network == ['reset']
    assert recorder.errors == []


@pytest.mark.parametrize('status,kind', [(502, 'protocol'), (504, 'timeout')])
def test_gateway_hop_failure_statuses_report_network_not_account_backoff(monkeypatch, status, kind):
    """502/504 are how this gateway says "the upstream hop did not complete";
    they must not be charged to the account as an upstream 5xx."""
    recorder = _GuardRecorder()

    @generation_lifetime
    async def handler(request):
        await admit_generation(request, TOKEN)
        raise HTTPException(status_code=status, detail='Upstream request failed')

    async def run():
        recorder.install(monkeypatch)
        try:
            await handler(Request(SCOPE))
        except HTTPException:
            pass

    asyncio.run(run())
    assert recorder.network == [kind]
    assert recorder.errors == []


# ---------------------------------------------------------------------------
# Upstream refusals stay distinguishable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('scenario,expected', [
    ('status_401', (401, 'unauthorized')),
    ('status_403', (403, None)),
    ('status_403_cf', (403, 'cf_chl_opt')),
    ('status_403_dead', (403, 'account_deactivated')),
    ('status_429', (429, None)),
    ('status_500', (500, None)),
    ('status_503', (503, None)),
])
def test_upstream_refusal_is_reported_with_its_own_signal(monkeypatch, scenario, expected):
    recorder = _GuardRecorder()

    async def run():
        return await _drive(scenario, monkeypatch, recorder)

    asyncio.run(run())
    assert recorder.errors == [expected]
    assert recorder.success == []


def test_gateway_own_rejections_are_not_charged_to_the_account(monkeypatch):
    """404 and raised 500/503 are this layer's own conclusions, not evidence
    about the account. Reporting them would extend the cooldown of an account
    that did nothing wrong."""
    recorder = _GuardRecorder()

    async def run():
        for scenario in ('raised_404', 'raised_503', 'raised_internal'):
            await _drive(scenario, monkeypatch, recorder)

    asyncio.run(run())
    assert recorder.errors == []
    assert recorder.network == []
    assert recorder.success == []


# ---------------------------------------------------------------------------
# Disconnects and denied admission
# ---------------------------------------------------------------------------

def test_client_disconnect_is_neither_success_nor_failure(monkeypatch):
    recorder = _GuardRecorder()

    async def run():
        return await _drive('disconnect', monkeypatch, recorder, trial=True)

    accounting = asyncio.run(run())
    assert recorder.success == []
    assert recorder.network == []
    assert recorder.errors == []
    assert len(recorder.releases) == 1
    assert accounting == ['released']


def test_denied_admission_tells_the_guard_nothing_about_the_upstream(monkeypatch):
    """A capacity denial is the guard's own verdict. Feeding it back as an
    upstream 5xx would extend the very cooldown that caused the denial."""
    recorder = _GuardRecorder()
    denied = guard.AntibanContext(token=TOKEN, enabled=True, admission_denied=True,
                                  denial_reason='cooldown', denial_status=503)

    async def acquire(token):
        return denied

    monkeypatch.setattr(guard, 'acquire_context', acquire)

    @generation_lifetime
    async def handler(request):
        await admit_generation(request, TOKEN)

    async def run():
        recorder.install(monkeypatch)
        with pytest.raises(HTTPException) as raised:
            await handler(Request(SCOPE))
        assert raised.value.status_code == 503

    asyncio.run(run())
    assert recorder.errors == []
    assert recorder.network == []
    assert recorder.success == []
    assert len(recorder.releases) == 1


# ---------------------------------------------------------------------------
# Streams that were never admitted
# ---------------------------------------------------------------------------

async def _plain_events():
    yield b'data: {"conversation_id":"synthetic"}\n\n'
    yield b'data: [DONE]\n\n'


async def _plain_failing_events():
    yield b'data: {"conversation_id":"synthetic"}\n\n'
    raise ConnectionResetError('synthetic upstream reset')


async def _collect(request, events):
    return [event async for event in observe_generation_stream(request, events)]


def test_unadmitted_stream_passes_through(monkeypatch):
    """Polling and status streams share this wrapper without being admitted.
    They must keep working, and they have nothing to report."""
    recorder = _GuardRecorder()
    recorder.install(monkeypatch)

    async def run():
        request = Request(SCOPE)
        request.state.generation_admission = None
        request.state.generation_trial = None
        request.state.generation_feedback = None
        return await _collect(request, _plain_events())

    assert asyncio.run(run()) == [b'data: {"conversation_id":"synthetic"}\n\n', b'data: [DONE]\n\n']
    assert recorder.success == []
    assert recorder.errors == []
    assert recorder.network == []


def test_unadmitted_stream_failure_still_propagates(monkeypatch):
    """The wrapper must not swallow an upstream failure it cannot report on."""
    recorder = _GuardRecorder()
    recorder.install(monkeypatch)

    async def run():
        request = Request(SCOPE)
        request.state.generation_admission = None
        request.state.generation_trial = None
        request.state.generation_feedback = None
        return await _collect(request, _plain_failing_events())

    with pytest.raises(ConnectionResetError):
        asyncio.run(run())
    assert recorder.network == []


def test_request_state_that_was_never_initialised_still_streams(monkeypatch):
    """A route outside ``generation_lifetime`` may reuse the wrapper; absent
    state must read as "nothing to report", not as a crash."""
    async def run():
        return await _collect(Request(SCOPE), _plain_events())

    assert asyncio.run(run()) == [b'data: {"conversation_id":"synthetic"}\n\n', b'data: [DONE]\n\n']


# ---------------------------------------------------------------------------
# Ordering and log hygiene
# ---------------------------------------------------------------------------

def test_feedback_reaches_the_guard_before_capacity_is_returned(monkeypatch):
    """The slot may be reused the instant it is free, so the verdict about this
    request has to be with the guard by then."""
    order = []

    async def success(ctx):
        order.append('report')

    def release(ctx):
        order.append('release')

    monkeypatch.setattr(guard, 'report_success', success)
    monkeypatch.setattr(guard, 'release_context', release)

    async def run():
        return await _drive('complete', monkeypatch, recorder=None)

    asyncio.run(run())
    assert order == ['report', 'release']


def test_sniffed_body_and_credentials_never_reach_the_logs(monkeypatch, caplog):
    import logging
    secret = 'SECRET-UPSTREAM-BODY-AND-PROXY-CREDENTIALS'

    @generation_lifetime
    async def handler(request):
        await admit_generation(request, TOKEN)
        return Response(content=('cf_chl_opt ' + secret).encode(), status_code=403)

    async def never_receives():
        await asyncio.Future()

    async def ignore_send(message):
        return None

    async def run():
        response = await handler(Request(SCOPE))
        await response(SCOPE, never_receives, ignore_send)

    recorder = _GuardRecorder()
    recorder.install(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert recorder.errors == [(403, 'cf_chl_opt')]
    assert secret not in caplog.text
    assert TOKEN not in caplog.text


def test_transport_exception_text_never_reaches_the_logs(monkeypatch, caplog):
    import logging
    secret = 'SECRET-PROXY-CREDENTIALS-IN-EXCEPTION'

    @generation_lifetime
    async def handler(request):
        await admit_generation(request, TOKEN)
        try:
            raise ConnectionResetError('connection reset ' + secret)
        except ConnectionResetError:
            raise HTTPException(status_code=502, detail='Upstream request failed')

    async def run():
        try:
            await handler(Request(SCOPE))
        except HTTPException:
            pass

    recorder = _GuardRecorder()
    recorder.install(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert recorder.network == ['reset']
    assert secret not in caplog.text
