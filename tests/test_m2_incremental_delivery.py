"""M2: the gateway must deliver SSE events as they arrive, not at the terminal.

The reported symptom is that the browser shows nothing until the reply is
finished.  Parser unit tests cannot detect that: they consume an iterable that
is already complete, so a gateway that buffered the whole body would still pass
every one of them.

These tests therefore time *arrival*.  A slow upstream emits assistant deltas
with a real gap between them, and the assertion is that the response body
reaches the ASGI layer spread out over time — at least two separate deliveries
strictly before the terminal event.  A buffering regression collapses every
delivery to the end and fails here.

What these tests do and do not prove:

* They prove the gateway (``content_generator`` -> filter -> StreamingResponse)
  does not withhold events, and that nothing in the chain waits for EOF.
* They do NOT prove the browser paints them.  DOM rendering is covered by the
  Playwright evidence in tmp/agent-team/account-frontend/m2-streaming/, not
  here.  A pass here plus an empty DOM would localise the defect to the client.
"""
import asyncio
import socket
import threading
import time

from starlette.responses import StreamingResponse

from gateway.reverseProxy import content_generator
from gateway.sse_parser import extract_data_json
from utils.Client import Client


class _SlowSSEUpstream:
    """An upstream that emits one SSE event at a time with a real delay."""

    def __init__(self, events, delay=0.15):
        self._events = events
        self._delay = delay
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
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
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


def _delta(text):
    return ('data: {"v": "%s"}\n\n' % text).encode()


_TERMINAL = b'data: {"type": "message_stream_complete", "conversation_id": "c1"}\n\n'
_DONE = b"data: [DONE]\n\n"


def _drive(events, delay=0.15):
    """Run the real gateway streaming stack; return (timeline, body).

    ``timeline`` is a list of (seconds_since_first_byte, payload) for every
    ASGI ``http.response.body`` message, i.e. what the server actually handed
    to the transport for the browser.
    """
    upstream = _SlowSSEUpstream(events, delay=delay)
    timeline = []

    async def run():
        client = Client(timeout=30)
        response = await client.post_stream(
            f"http://127.0.0.1:{upstream.port}/",
            headers={}, cookies={}, data=b"", stream=True,
        )

        # The production filter from f_conversation, kept in sync with it:
        # drop resume tokens and user/system echoes, forward everything else.
        async def body():
            try:
                async for event in content_generator(response, "seed-token", False):
                    parsed = extract_data_json(event)
                    if parsed is not None:
                        kind = parsed.get("type", "message")
                        if kind == "resume_conversation_token":
                            continue
                        if kind == "message":
                            role = (parsed.get("message") or {}).get(
                                "author", {}).get("role")
                            if role in ("user", "system"):
                                continue
                    yield event
            finally:
                await client.discard()

        started = None
        streaming = StreamingResponse(body(), media_type="text/event-stream")

        async def send(message):
            nonlocal started
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if not chunk:
                    return
                now = time.monotonic()
                if started is None:
                    started = now
                timeline.append((now - started, chunk))

        async def receive():
            # Never disconnect: this test is about delivery, not cancellation.
            await asyncio.Event().wait()

        await streaming({"type": "http"}, receive, send)

    try:
        asyncio.run(asyncio.wait_for(run(), timeout=60))
    finally:
        upstream.close()
    return timeline, b"".join(chunk for _, chunk in timeline)


def test_assistant_deltas_reach_the_client_before_the_terminal_event():
    """At least two deltas must be delivered strictly before the terminal.

    This is the M2 acceptance criterion ("终态前至少两次可观测增量") measured at
    the gateway boundary.

    Ordering alone is not enough to assert: a gateway that buffers everything
    and flushes at EOF still emits the deltas *before* the terminal in list
    order, and an earlier draft of this test passed against exactly that
    mutant.  So the assertion is on arrival time — the deltas must land while
    the upstream is still generating, i.e. measurably earlier than the
    terminal.
    """
    delay = 0.15
    events = [_delta("one"), _delta("two"), _delta("three"), _TERMINAL, _DONE]
    timeline, body = _drive(events, delay=delay)

    terminal_at = next(
        at for at, chunk in timeline if b"message_stream_complete" in chunk
    )
    deltas_before = [
        at for at, chunk in timeline if b'"v"' in chunk and at < terminal_at
    ]
    assert len(deltas_before) >= 2, (
        f"only {len(deltas_before)} delta deliveries before the terminal event; "
        f"the gateway is batching the stream ({len(timeline)} total deliveries)"
    )
    # The second delta must arrive a real interval before the terminal, which
    # is what "the user sees text while it is still being written" means.
    assert terminal_at - deltas_before[1] >= delay * 0.5, (
        f"deltas landed {terminal_at - deltas_before[1]:.3f}s before the "
        f"terminal despite a {delay}s upstream gap — the stream was buffered "
        f"and only appears incremental in ordering"
    )
    # Nothing may be lost or reordered on the way through.
    assert body == b"".join(events)


def test_deliveries_are_spread_over_time_not_flushed_at_the_end():
    """Delivery timing must track the upstream, not collapse to the end.

    With a 0.15s upstream gap, a non-buffering gateway shows a comparable gap
    between deliveries.  A gateway that accumulates and flushes at EOF emits
    everything within a few milliseconds of the last event.
    """
    delay = 0.15
    events = [_delta("one"), _delta("two"), _delta("three"), _TERMINAL, _DONE]
    timeline, _ = _drive(events, delay=delay)

    assert len(timeline) >= 3, (
        f"expected one delivery per event, got {len(timeline)}: the stream was "
        f"coalesced"
    )
    span = timeline[-1][0] - timeline[0][0]
    assert span > delay, (
        f"all deliveries landed within {span:.3f}s of each other despite a "
        f"{delay}s upstream gap — the gateway buffered the stream"
    )


def test_partial_event_is_not_delivered_until_it_is_complete():
    """An event split across transport writes must not leak a half event.

    Forwarding a partial event would make the browser's EventSource parse a
    truncated JSON payload and drop the delta.  Note this test is about
    *boundaries*, not timing — a buffering gateway also passes it; the timing
    tests above are what catch buffering.
    """
    raw = _delta("split-across-writes")
    upstream_events = [raw[:12], raw[12:], _TERMINAL, _DONE]
    timeline, body = _drive(upstream_events, delay=0.15)

    for _, chunk in timeline:
        assert chunk.endswith(b"\n\n"), (
            f"delivered a chunk that is not a whole event: {chunk[:40]!r}"
        )
        # A half event would also fail to parse; assert the payload is intact.
        assert chunk.count(b"data:") >= 1
    assert body == b"".join(upstream_events)


def test_utf8_multibyte_split_across_writes_is_delivered_intact():
    """A multi-byte character cut in half by the transport must survive.

    The gateway must never decode-and-re-encode a partial character; doing so
    substitutes U+FFFD and corrupts the visible reply.
    """
    text = "中文流式输出"
    raw = ('data: {"v": "%s"}\n\n' % text).encode("utf-8")
    cut = raw.index(b"\xe6") + 1          # mid-character split
    timeline, body = _drive([raw[:cut], raw[cut:], _TERMINAL, _DONE], delay=0.15)

    assert body.startswith(raw), "multibyte payload was altered in transit"
    assert "�" not in body.decode("utf-8"), "replacement char introduced"
