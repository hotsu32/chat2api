"""The Plus trial ledger settles on the research turn's own terminal evidence.

One answer frame charges the reservation exactly once, and no other frame of the
recorded stream can do it.  The stream is the real capture
(``tests/fixtures/deep_research_plus_shapes.json``, 2026-09-12, 23 events, field
shapes only), rebuilt frame by frame the same way ``test_research_projection``
rebuilds it: every *field name* comes from the capture, and every *value* is set
by the test next to the observation it rests on.

Why this file exists
--------------------
The gateway ledger read ``content.parts`` and nothing else, while the capture
shows the turn's own answer -- fixture index 12 -- carrying ``content.text`` with
no ``parts`` at all.  The frame that really settles a research turn therefore
never charged one, and an interim assistant frame that merely carried
``end_turn`` (and no ``metadata.is_complete``, which the capture records on the
answer frame only) charged one instead.

Both ledgers must not disagree about the same turn, so the evidence is now the
research projection's own; the tests here drive the shapes the projection tests
already drive, and one test replays the whole capture's message frames under the
most permissive reading an interim frame can have and asserts that only the
answer frame charges.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.responses import StreamingResponse

from gateway.generation import (admit_generation, generation_lifetime,
                                observe_generation_stream)
from utils import trials
from utils.antiban import guard

FIXTURE = Path(__file__).parent / "fixtures" / "deep_research_plus_shapes.json"

TOKEN = 'synthetic-account'
SCOPE = {'type': 'http', 'method': 'POST', 'path': '/backend-api/conversation',
         'headers': [], 'asgi': {'version': '3.0'}}

ANSWER_FRAME = 12          # the only captured frame carrying metadata.is_complete
INTERIM_FRAME = 4          # an assistant frame with parts, end_turn and no is_complete
TOOL_FRAME = 17            # an assistant frame whose metadata names a tool invocation
HIDDEN_FRAME = 11          # an assistant frame upstream marks hidden from the conversation

# The message-bearing frames of the capture (the ones that carry a message object
# with an author and a body).  Title/marker/status frames carry no message.
MESSAGE_FRAMES = (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 17, 18)


# ---------------------------------------------------------------------------
# Rebuilding one frame from the recorded shape
# ---------------------------------------------------------------------------

def _build(node):
    """Type-correct placeholder for one recorded shape node (see the projection tests)."""
    if node == "str":
        return "x"
    if node == "int":
        return 1
    if node == "float":
        return 1.0
    if node == "bool":
        return True
    if node == "null":
        return None
    if node == "{}":
        return {}
    if isinstance(node, dict) and node.get("type") == "array":
        items = node.get("items")
        if items in (None, "empty"):
            return []
        return [_build(items)]
    if isinstance(node, dict):
        return {key: _build(value) for key, value in node.items()}
    return None


def _frame(index):
    """One payload rebuilt from the capture's shape for that event."""
    for entry in json.loads(FIXTURE.read_text())["events"]:
        if entry["index"] == index:
            payload = _build(entry["shape"])
            if entry.get("type") and isinstance(payload, dict):
                payload["type"] = entry["type"]
            return payload
    raise AssertionError(f"the fixture has no event {index}")


def _message_of(payload):
    """The message object inside a rebuilt frame, whichever envelope carries it."""
    for key in ("message", "input_message"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    value = payload.get("v")
    if isinstance(value, dict) and isinstance(value.get("message"), dict):
        return value["message"]
    raise AssertionError("this frame carries no message object")


def _assistant_frame(index):
    """A fixture frame read as an assistant frame that could end the turn.

    Values the capture does not record are set to the reading its *role* implies
    -- an assistant frame is ``finished_successfully``, and ``end_turn`` is a
    boolean on several captured frames, so an interim frame is given the
    permissive ``True`` and must still not charge.
    """
    payload = _frame(index)
    message = _message_of(payload)
    message["author"]["role"] = "assistant"
    message["status"] = "finished_successfully"
    message["end_turn"] = True
    # The capture records *which* body field a frame carries (the answer carries
    # ``text``, every other frame carries ``parts``); the body itself is
    # synthetic, so a frame is never rejected merely for having an empty one.
    content = message["content"]
    if "text" in content:
        content["text"] = "synthetic answer"
    if "parts" in content:
        content["parts"] = ["synthetic research step"]
    return payload


def _chat_completion():
    """The plain chat completion frame the gateway has always settled on."""
    return {'message': {'author': {'role': 'assistant'},
                        'content': {'content_type': 'text', 'parts': ['synthetic reply']},
                        'status': 'finished_successfully',
                        'end_turn': True}}


def _events(frames, terminal=True):
    for frame in frames:
        yield ('data: ' + json.dumps(frame) + '\n\n').encode()
    if terminal:
        yield b'data: [DONE]\n\n'


# ---------------------------------------------------------------------------
# Driving one admitted generation through the real ASGI lifetime
# ---------------------------------------------------------------------------

def _run(monkeypatch, frames, terminal=True):
    """Run one admitted generation and return what the trial ledger was told.

    Everything below is the production path: admission, the response wrapper,
    the event observer and the tee that feeds it.  Only the ledger's own writes
    are recorded, so a regression shows up as a wrong verdict rather than as a
    private structure changing shape.
    """
    accounting = []
    monkeypatch.setattr(trials, 'reserve', lambda seed: 'synthetic-reservation')
    monkeypatch.setattr(trials, 'settle', lambda rid, seed: accounting.append('settled'))
    monkeypatch.setattr(trials, 'release', lambda rid, seed: accounting.append('released'))

    async def acquire(token):
        return guard.AntibanContext(token=TOKEN, enabled=True, concurrency_acquired=True)

    async def report(*args, **kwargs):
        return None

    def release(ctx):
        return None

    monkeypatch.setattr(guard, 'acquire_context', acquire)
    monkeypatch.setattr(guard, 'release_context', release)
    monkeypatch.setattr(guard, 'report_success', report)
    monkeypatch.setattr(guard, 'report_error', report)
    monkeypatch.setattr(guard, 'report_network_error', report)

    @generation_lifetime
    async def handler(request):
        await admit_generation(request, TOKEN)
        return StreamingResponse(
            observe_generation_stream(request, _stream(frames, terminal)),
            media_type='text/event-stream')

    async def drive():
        response = await handler(Request(SCOPE))

        async def receive():
            await asyncio.Future()

        async def send(message):
            pass

        await response(SCOPE, receive, send)

    asyncio.run(drive())
    return accounting


async def _stream(frames, terminal):
    for event in _events(frames, terminal):
        yield event


# ---------------------------------------------------------------------------
# The captured answer frame is the one that charges
# ---------------------------------------------------------------------------

def test_the_captured_answer_frame_settles_the_trial_once(monkeypatch):
    """Index 12: the answer is ``content.text``, and there is no ``parts`` at all.

    This is the frame the real research turn ends on; a ledger that reads only
    ``parts`` charges nothing for the one generation the user actually got.
    """
    answer = _assistant_frame(ANSWER_FRAME)
    assert 'parts' not in _message_of(answer)['content'], \
        "the capture records the answer as content.text, with no parts"
    assert _message_of(answer)['metadata']['is_complete'] is True, \
        "the capture records metadata.is_complete on this frame"
    assert _run(monkeypatch, [answer]) == ['settled']


def test_a_streamed_answer_is_settled_from_patch_frames(monkeypatch):
    """The same body, delivered as patches instead of one whole message object."""
    opening = {'message': {'author': {'role': 'assistant'},
                           'content': {'content_type': 'text', 'parts': ['']},
                           'status': 'in_progress', 'end_turn': False,
                           'metadata': {'is_complete': False}}}
    patches = [
        {'p': '/message/status', 'o': 'replace', 'v': 'finished_successfully'},
        {'p': '/message/end_turn', 'o': 'replace', 'v': True},
        {'p': '/message/metadata/is_complete', 'o': 'replace', 'v': True},
        {'p': '/message/content/text', 'o': 'replace', 'v': 'synthetic answer'},
    ]
    assert _run(monkeypatch, [opening] + patches) == ['settled']


def test_the_trial_is_charged_once_however_often_the_answer_arrives(monkeypatch):
    """A repeated answer frame or terminal marker must not charge twice."""
    answer = _assistant_frame(ANSWER_FRAME)
    assert _run(monkeypatch, [answer, answer, answer]) == ['settled']


def test_an_ordinary_chat_parts_completion_still_settles(monkeypatch):
    """The frame shape every existing gateway test pins keeps its verdict."""
    assert _run(monkeypatch, [_chat_completion()]) == ['settled']


# ---------------------------------------------------------------------------
# No other frame in the stream may charge
# ---------------------------------------------------------------------------

def test_an_interim_frame_carrying_end_turn_does_not_settle(monkeypatch):
    """Index 4: parts, an assistant author, ``end_turn`` -- and no ``is_complete``.

    ``end_turn`` alone is not the answer: the capture records the field on the
    walk of frames that lead up to the answer, while ``metadata.is_complete``
    appears on the answer frame only.
    """
    interim = _assistant_frame(INTERIM_FRAME)
    assert 'is_complete' not in _message_of(interim)['metadata'], \
        "the capture never records is_complete on this frame"
    assert _run(monkeypatch, [interim]) == ['released']


def test_a_tool_frame_carrying_end_turn_does_not_settle(monkeypatch):
    """Index 17: the capture's own tool evidence is ``metadata.invoked_resource``.

    The frame is given the answer's completion flag so that the tool evidence is
    the only thing left to reject it -- a tool node is not the user's answer.
    """
    tool = _assistant_frame(TOOL_FRAME)
    _message_of(tool)['metadata']['is_complete'] = True
    _message_of(tool)['metadata'].pop('is_visually_hidden_from_conversation', None)
    assert _message_of(tool)['metadata'].get('invoked_resource'), \
        "the capture records invoked_resource on this frame"
    assert _run(monkeypatch, [tool]) == ['released']


def test_a_hidden_frame_carrying_end_turn_does_not_settle(monkeypatch):
    """Index 11: upstream itself marks this frame hidden from the conversation."""
    hidden = _assistant_frame(HIDDEN_FRAME)
    _message_of(hidden)['metadata']['is_complete'] = True
    assert _message_of(hidden)['metadata']['is_visually_hidden_from_conversation'] is True
    assert _run(monkeypatch, [hidden]) == ['released']


def test_an_answer_frame_that_does_not_declare_itself_complete_does_not_settle(monkeypatch):
    """The same answer frame with the completion flag turned off is not an answer."""
    answer = _assistant_frame(ANSWER_FRAME)
    _message_of(answer)['metadata']['is_complete'] = False
    assert _run(monkeypatch, [answer]) == ['released']


def test_only_the_captures_answer_frame_can_settle_the_trial(monkeypatch):
    """Replay every message frame of the capture under the permissive reading.

    Each frame is built as an assistant frame that finished and ended the turn --
    the reading most favourable to an interim frame -- and only the answer frame,
    the one the capture gives ``metadata.is_complete``, may charge.
    """
    settled, released = [], []
    for index in MESSAGE_FRAMES:
        verdict = _run(monkeypatch, [_assistant_frame(index)])
        assert verdict in (['settled'], ['released']), index
        (settled if verdict == ['settled'] else released).append(index)
    assert settled == [ANSWER_FRAME], \
        f"only the answer frame may charge, but {settled} charged"
    assert ANSWER_FRAME not in released


# ---------------------------------------------------------------------------
# Nothing but a delivered answer charges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('status', ['failed', 'cancelled', 'incomplete'])
def test_a_failed_cancelled_or_incomplete_turn_releases(monkeypatch, status):
    answer = _assistant_frame(ANSWER_FRAME)
    _message_of(answer)['status'] = status
    assert _run(monkeypatch, [answer]) == ['released']


def test_the_upstream_terminal_marker_is_still_required(monkeypatch):
    """An answer that upstream never declared finished is not a delivered turn."""
    assert _run(monkeypatch, [_assistant_frame(ANSWER_FRAME)], terminal=False) \
        == ['released']


def test_an_error_frame_never_charges(monkeypatch):
    error = {'error': {'code': 'synthetic_upstream_error'}}
    assert _run(monkeypatch, [error]) == ['released']


def test_a_research_turn_that_delivers_nothing_releases(monkeypatch):
    """The degenerate case: the stream closes without ever producing an answer."""
    assert _run(monkeypatch, []) == ['released']
