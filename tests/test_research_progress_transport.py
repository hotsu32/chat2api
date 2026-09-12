"""Deep Research progress transport: the gateway must not lose the long tail.

A Deep Research turn's value is the intermediate progress it streams over
minutes -- the research plan, the activity history, the sources.  Losing the
final answer is visible and gets reported; losing everything after event three
of ninety looks, from the browser, like a turn that just went quiet.

Scope of this file, deliberately narrow: it covers the *transport* the gateway
owns -- which upstream events reach the browser and whether the stream survives
to its terminal event.  It asserts nothing about upstream event names, stage
labels or percentages, because none of those have been observed on a real
Plus/Pro research run; see the "no real upstream capture" note at the bottom.
So a pass here proves the gateway forwards whatever the upstream sends; it does
NOT prove Deep Research works end to end.

The defect these tests were written against (measured, see REPORT below):

    f_conversation's stream filter destructured upstream-controlled fields
    without type-checking them:

        (_d.get("message") or {}).get("author", {}).get("role")

    Any frame shaped {"type": "message", "message": {"author": null}} -- or
    "message" as a string -- raised AttributeError *inside the streaming body*.
    The response had already committed 200 at its first event, so the browser
    could not see an error: the stream simply stopped.  Measured on the real
    chain: an 8-event stream delivered 3 events, no terminal event, no [DONE].

Introduced in 5b3d53d, which rewrote the filter to parse whole SSE events and
dropped the ``try/except Exception: pass`` that a0dba46 had wrapped it in.
"""
import asyncio
import socket
import threading
import time

import pytest
from starlette.responses import StreamingResponse

from gateway.f_conversation_gateway import is_legacy_echo_event
from gateway.reverseProxy import content_generator
from gateway.sse_parser import extract_data_json
from utils.Client import Client


# ---------------------------------------------------------------------------
# Unit level: classification must be total over legal JSON objects
# ---------------------------------------------------------------------------

def _echo(role):
    return {"type": "message", "message": {"author": {"role": role}}}


def test_legacy_user_and_system_echoes_are_dropped():
    """The legacy endpoint echoes the prompt back; the new frontend renders it
    as a duplicate user turn.  This is the filter's whole reason to exist and
    must keep working -- the fix must not turn into "forward everything"."""
    assert is_legacy_echo_event(_echo("user"))
    assert is_legacy_echo_event(_echo("system"))
    assert is_legacy_echo_event({"type": "resume_conversation_token", "v": "t"})


def test_assistant_and_progress_events_are_forwarded():
    """Everything that is not a positively-identified legacy echo is the
    payload the user is waiting for."""
    assert not is_legacy_echo_event(_echo("assistant"))
    assert not is_legacy_echo_event(_echo("tool"))
    assert not is_legacy_echo_event({"v": "delta text"})
    assert not is_legacy_echo_event({"p": "", "o": "append", "v": "more"})
    assert not is_legacy_echo_event({"type": "message_stream_complete"})


@pytest.mark.parametrize("payload", [
    {"type": "message", "message": None},
    {"type": "message", "message": {"author": None}},
    {"type": "message", "message": "a string"},
    {"type": "message", "message": []},
    {"type": "message", "message": {"author": "assistant"}},
    {"type": "message", "message": {"author": []}},
    {"type": None},
    {"type": [], "message": {"author": {"role": "user"}}},
    {},
], ids=["message-null", "author-null", "message-string", "message-list",
        "author-string", "author-list", "type-null", "type-list", "empty"])
def test_unexpected_frame_shapes_do_not_raise(payload):
    """RED before the fix: each of these raised AttributeError.

    These are not hypothetical JSON: every one is a legal object that the
    upstream is free to send, and the gateway does not control its schema.  The
    events the parser hands over are upstream-shaped, so classification has to
    be total over them or the stream dies mid-flight.
    """
    assert is_legacy_echo_event(payload) is False


def test_unrecognised_shapes_are_forwarded_not_dropped():
    """Fail-open, not fail-closed.

    An unknown event must reach the browser: the project rule is that unknown
    events are never silently discarded.  Dropping on doubt would also make the
    filter a silent data-loss path the moment upstream adds a field -- the
    failure mode this file exists to prevent, just quieter.
    """
    assert is_legacy_echo_event({"type": "message", "message": {}}) is False
    assert is_legacy_echo_event({"type": "some_future_event"}) is False


# ---------------------------------------------------------------------------
# Transport level: the same defect, measured through the real streaming chain
# ---------------------------------------------------------------------------

class _SSEUpstream:
    """A local upstream that emits pre-baked SSE events over a real socket."""

    def __init__(self, events, delay=0.02):
        self._events, self._delay = events, delay
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\n"
                         b"Content-Type: text/event-stream\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n")
            for raw in self._events:
                conn.sendall(b"%x\r\n" % len(raw) + raw + b"\r\n")
                time.sleep(self._delay)
            conn.sendall(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


def _drive(events):
    """Run the production filter through Starlette; return the client's view.

    Mirrors ``f_conversation._filter_gen`` -- same content_generator, same
    classification call, same StreamingResponse -- so a regression in the real
    filter shows up here.  Returns (delivered_bytes, status, error).
    """
    upstream = _SSEUpstream(events)
    delivered, state = [], {"status": None, "error": None}

    async def run():
        client = Client(timeout=30)
        response = await client.post_stream(
            f"http://127.0.0.1:{upstream.port}/",
            headers={}, cookies={}, data=b"", stream=True)

        async def body():
            try:
                async for event in content_generator(response, "seed-research", False):
                    parsed = extract_data_json(event)
                    if parsed is not None and is_legacy_echo_event(parsed):
                        continue
                    yield event
            finally:
                await client.discard()

        async def send(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            elif message["type"] == "http.response.body" and message.get("body"):
                delivered.append(message["body"])

        async def receive():
            await asyncio.Event().wait()      # never disconnect

        try:
            await StreamingResponse(body(), media_type="text/event-stream")(
                {"type": "http"}, receive, send)
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"

    try:
        asyncio.run(asyncio.wait_for(run(), timeout=60))
    finally:
        upstream.close()
    return b"".join(delivered), state["status"], state["error"]


_TERMINAL = b'data: {"type":"message_stream_complete","conversation_id":"c-res"}\n\n'
_DONE = b"data: [DONE]\n\n"


def _progress(n):
    """A progress-carrying frame.

    Shaped as a generic delta on purpose: no invented event name, no stage
    label, no percentage.  The claim under test is "the gateway forwards the
    upstream's intermediate events", which does not depend on their schema.
    """
    return b'data: {"p":"","o":"append","v":"step-%d"}\n\n' % n


def test_progress_events_survive_an_unparseable_frame_mid_stream():
    """RED before the fix: delivery stopped at the bad frame.

    This is the acceptance-relevant case.  A long research turn streams many
    intermediate events; one frame the filter cannot destructure used to kill
    the response body, silently discarding every later event.  Measured before
    the fix: 3 of 8 events delivered, terminal event never sent.
    """
    events = ([_progress(i) for i in range(3)]
              + [b'data: {"type":"message","message":{"author":null}}\n\n']
              + [_progress(i) for i in range(3, 6)]
              + [_TERMINAL, _DONE])
    body, status, error = _drive(events)

    assert error is None, f"the streaming body raised mid-turn: {error}"
    assert status == 200
    for i in range(6):
        assert b"step-%d" % i in body, (
            f"progress event step-{i} never reached the client; the stream was "
            f"truncated mid-turn (received {len(body)} bytes)")
    assert b"message_stream_complete" in body, (
        "the terminal event was lost: the client cannot tell a finished "
        "research turn from a stalled one")
    assert b"[DONE]" in body


def test_every_upstream_event_except_legacy_echoes_is_forwarded_verbatim():
    """No reordering, no rewriting, no dropping beyond the two echo classes.

    The gateway is a transport here.  Asserting on the exact byte stream is
    what keeps a future "helpful" transformation (a synthesised progress frame,
    a rewritten payload) from passing.
    """
    echo = b'data: {"type":"message","message":{"author":{"role":"user"}}}\n\n'
    resume = b'data: {"type":"resume_conversation_token","v":"tok"}\n\n'
    kept = [_progress(0), _progress(1), _TERMINAL, _DONE]
    body, _, error = _drive([resume, echo] + kept)

    assert error is None
    assert body == b"".join(kept), "forwarded stream is not byte-identical"
    assert b"resume_conversation_token" not in body
    assert b'"role":"user"' not in body


def test_a_stream_of_only_unknown_events_still_reaches_the_client():
    """Fail-open under total novelty.

    If upstream renames every event tomorrow, the correct gateway behaviour is
    to forward all of it, not to filter the turn down to nothing.
    """
    unknown = [b'data: {"type":"future_event_%d","v":{"any":"shape"}}\n\n' % i
               for i in range(4)]
    body, _, error = _drive(unknown + [_TERMINAL, _DONE])

    assert error is None
    assert body == b"".join(unknown + [_TERMINAL, _DONE])


# ---------------------------------------------------------------------------
# What this file does not establish
# ---------------------------------------------------------------------------
#
# No real Plus/Pro Deep Research SSE capture exists in this repository, so
# every frame above is synthetic and proves protocol *handling* only.  Still
# outstanding for the "官网级 Deep Research" acceptance, none of it inferable
# from these tests:
#
#   * The real upstream event names, ordering and payloads for a research run
#     (research plan, activity history, sources).  Nothing here should be read
#     as evidence of what those look like.
#   * Whether the browser renders intermediate progress once it is forwarded --
#     a gateway that forwards correctly and a frontend that ignores the events
#     are indistinguishable from this side.
#   * Whether register-websocket / backend-api/tasks are load-bearing for the
#     research UI under ordinary seed auth (backend.py returns an empty task
#     list for seed-authenticated users; backend.py is outside this worker's
#     ownership and the question is unresolved).
#   * Reconnect and refresh recovery, cancellation of the upstream async
#     research task, and any event-cache bound -- none are implemented, and a
#     client-side disconnect does not by itself cancel an upstream research
#     task that continues server-side.
