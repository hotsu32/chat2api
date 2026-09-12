"""Admission lasts until the ASGI send task exits, including failed starts."""
import asyncio
import json

import pytest
from fastapi import Request
from fastapi.responses import StreamingResponse

from gateway.generation import admit_generation, generation_lifetime, track_generation_client, observe_generation_stream
from utils import trials
from utils.antiban import guard


@pytest.mark.parametrize('trial', [False, True])
@pytest.mark.parametrize('outcome', ['complete', 'disconnect', 'send_error', 'body_error',
                                     'truncated_done', 'multiline_done', 'duplicate_done'])
def test_capacity_is_held_until_response_exit(monkeypatch, outcome, trial):
    released = []
    cleanup = []
    accounting = []
    monkeypatch.setattr(trials, 'reserve', lambda seed: 'synthetic-reservation' if trial else None)
    monkeypatch.setattr(trials, 'settle', lambda rid, seed: accounting.append('settled'))
    monkeypatch.setattr(trials, 'release', lambda rid, seed: accounting.append('released'))

    class UpstreamClient:
        async def close(self):
            assert not released
            cleanup.append("pooled")

        async def discard(self):
            assert not released
            cleanup.append("discarded")
    ctx = guard.AntibanContext(token='synthetic-account', enabled=True)

    async def acquire(token):
        return ctx

    monkeypatch.setattr(guard, 'acquire_context', acquire)
    monkeypatch.setattr(guard, 'release_context', released.append)

    async def exercise():
        sent_body = asyncio.Event()
        scope = {'type': 'http', 'method': 'POST', 'path': '/conversation',
                 'headers': [], 'asgi': {'version': '3.0'}}

        async def body():
            message = {'message': {'author': {'role': 'assistant'},
                'content': {'parts': ['synthetic reply']},
                'status': 'finished_successfully', 'end_turn': True}}
            yield ('data: ' + json.dumps(message) + '\n\n').encode()
            if outcome == 'body_error':
                raise RuntimeError('synthetic stream failure')
            if outcome == 'disconnect':
                await asyncio.Future()
            if outcome == 'truncated_done':
                yield b'data: [DONE]\n'
            elif outcome == 'multiline_done':
                yield b'data: [DONE]\ndata: extra\n\n'
            else:
                yield b'data: [DONE]\n\n'
                if outcome == 'duplicate_done':
                    yield b'data: [DONE]\n\n'

        @generation_lifetime
        async def handler(request):
            await admit_generation(request, 'synthetic-account')
            upstream = UpstreamClient()
            track_generation_client(request, upstream)
            track_generation_client(request, upstream)
            return StreamingResponse(observe_generation_stream(request, body()), media_type='text/event-stream')

        async def receive():
            await sent_body.wait()
            if outcome == 'disconnect':
                return {'type': 'http.disconnect'}
            await asyncio.Future()

        async def send(message):
            assert not released, 'capacity released while response still sending'
            if outcome == 'send_error':
                raise OSError('synthetic send failure')
            if message['type'] == 'http.response.body':
                sent_body.set()

        response = await handler(Request(scope))
        assert not released
        if outcome in ('send_error', 'body_error'):
            with pytest.raises(BaseExceptionGroup):
                await response(scope, receive, send)
        else:
            await response(scope, receive, send)
        assert released == [ctx]
        assert accounting == (["settled" if outcome in ("complete", "duplicate_done") else "released"] if trial else [])
        assert cleanup == ["discarded" if outcome in ("disconnect", "send_error", "body_error") else "pooled"]

    asyncio.run(exercise())
