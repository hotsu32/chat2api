"""M2: client-cancel must release the upstream streaming connection.

These tests exercise the *real* release chain, not the SSE parser:

    browser disconnects
      -> Starlette cancels the StreamingResponse body task
      -> the gateway generator is closed
      -> the upstream curl_cffi stream must be torn down
      -> the pooled session must NOT be reused while it is mid-stream

Measured facts that motivate each test (see
tmp/agent-team/account-frontend/m2-streaming/REPORT.md, section "取消/释放"):

* Abandoning ``r.aiter_content()`` without closing leaves the upstream sending:
  a server that would emit 60 events still emitted 29 more over 3 s after the
  "cancel", and never observed a disconnect.
* ``Client.close()`` returns that mid-stream session to the shared pool.  The
  next turn on the same pool key then blocks ~4.15 s draining the abandoned
  response before its own first byte, versus 0.21 s with a fresh connection.
* ``Client.discard()`` hard-closes: the upstream observes ConnectionResetError
  and stops generating.

The tests use a local socket server rather than a mock so they assert on what
the peer actually saw on the wire.
"""
import asyncio
import socket
import threading
import time

import pytest

from utils.Client import Client

# Large events force real kernel-buffer backpressure.  With small events the
# whole response fits in the socket buffer, the server never blocks, and a
# leaked stream looks identical to a released one.
_PAYLOAD = b"x" * 200_000


class _SSEServer:
    """Local chunked text/event-stream server that records peer disconnects."""

    def __init__(self, n_events=60, delay=0.05):
        self._n_events = n_events
        self._delay = delay
        self.sent = 0
        self.connections = 0
        self.peer_disconnect = None
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        # Serve every connection: a test that opens a second turn must not be
        # blocked by the accept queue, or a fast fresh connection would look
        # identical to a stalled pooled one.
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.recv(65536)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            for _ in range(self._n_events):
                body = b'data: {"v":"' + _PAYLOAD + b'"}\n\n'
                conn.sendall(b"%x\r\n" % len(body) + body + b"\r\n")
                self.sent += 1
                time.sleep(self._delay)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self.peer_disconnect = type(exc).__name__
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


async def _open_stream(client, port):
    return await client.post_stream(
        f"http://127.0.0.1:{port}/", headers={}, cookies={}, data=b"", stream=True
    )


async def _consume(response, n):
    got = 0
    async for _ in response.aiter_content():
        got += 1
        if got >= n:
            break
    return got


def test_discard_stops_upstream_generation_after_cancel():
    """A cancelled turn must stop costing upstream tokens.

    Regression guard for the leak: the gateway used to return the client to the
    pool (``close()``), which leaves the upstream generating into a socket
    nobody reads.
    """
    server = _SSEServer(n_events=60, delay=0.05)

    async def run():
        client = Client(timeout=30)
        response = await _open_stream(client, server.port)
        await _consume(response, 2)
        sent_at_cancel = server.sent
        await client.discard()
        await asyncio.sleep(2.0)
        return sent_at_cancel, server.sent, server.peer_disconnect

    try:
        sent_at_cancel, sent_after, disconnect = asyncio.run(run())
    finally:
        server.close()

    assert disconnect is not None, (
        "upstream never observed a disconnect: the cancelled stream was leaked"
    )
    assert sent_after - sent_at_cancel <= 2, (
        f"upstream kept generating after cancel: {sent_at_cancel} -> {sent_after}"
    )


def test_cancelled_stream_is_not_returned_to_the_session_pool():
    """A mid-stream session must never be handed to the next turn.

    ``Client.close()`` returns the session to the shared pool *with the
    abandoned response still attached*, so the next turn for the same account
    checks out a session whose upstream is still streaming a reply nobody will
    read.  ``discard()`` hard-closes instead and pools nothing.

    Asserted on session identity rather than on wall-clock latency: an earlier
    draft timed the next turn, but that signal only appeared against a
    single-threaded test server.  Against a server that accepts concurrently
    (as chatgpt.com does) curl just opens a second connection and the timing
    difference vanishes — the leak is still there, so timing was measuring the
    harness, not the defect.
    """
    import utils.Client as client_module

    def reuse_after(release):
        server = _SSEServer(n_events=60, delay=0.05)

        async def run():
            client_module._session_pool.clear()
            first = Client(timeout=30)
            response = await _open_stream(first, server.port)
            await _consume(response, 2)
            abandoned_session = first.session
            await release(first)

            pooled = sum(len(v) for v in client_module._session_pool.values())
            second = Client(timeout=30)
            try:
                return pooled, second.session is abandoned_session
            finally:
                await second.discard()

        try:
            return asyncio.run(run())
        finally:
            server.close()

    pooled_after_close, reused_after_close = reuse_after(lambda c: c.close())
    pooled_after_discard, reused_after_discard = reuse_after(lambda c: c.discard())

    # Documents the defect being fixed: close() pools the mid-stream session.
    assert (pooled_after_close, reused_after_close) == (1, True), (
        "expected close() to pool the abandoned mid-stream session; if this no "
        "longer reproduces the release path changed and this test must be "
        "re-derived"
    )
    assert pooled_after_discard == 0, (
        "discard() must not pool a session whose stream was abandoned"
    )
    assert not reused_after_discard, (
        "the next turn must not check out the session that held the cancelled "
        "stream"
    )


def test_generator_close_propagates_to_upstream_teardown():
    """Closing the response generator must tear the upstream down.

    This is the shape Starlette uses: on client disconnect it cancels the body
    task, which closes the async generator.  The gateway's generator therefore
    has to release the upstream in a ``finally`` — a background task that only
    runs on *normal* completion is not enough.
    """
    server = _SSEServer(n_events=60, delay=0.05)
    released = []

    async def run():
        client = Client(timeout=30)
        response = await _open_stream(client, server.port)

        async def body():
            try:
                async for chunk in response.aiter_content():
                    yield chunk
            finally:
                released.append(True)
                await client.discard()

        gen = body()
        got = 0
        async for _ in gen:
            got += 1
            if got >= 2:
                break
        await gen.aclose()          # what Starlette's cancellation does
        sent_at_cancel = server.sent
        await asyncio.sleep(2.0)
        return sent_at_cancel, server.sent

    try:
        sent_at_cancel, sent_after = asyncio.run(run())
    finally:
        server.close()

    assert released, "generator finally-block did not run on aclose()"
    assert sent_after - sent_at_cancel <= 2, (
        f"upstream kept generating after generator close: "
        f"{sent_at_cancel} -> {sent_after}"
    )


def test_f_conversation_releases_upstream_on_client_disconnect():
    """The production f_conversation generator must release on cancel.

    Asserts the contract structurally against the real module: the streaming
    body must release the upstream from a ``finally``, because
    ``BackgroundTask`` only runs after a *completed* response.
    """
    import inspect

    from gateway import f_conversation_gateway

    source = inspect.getsource(f_conversation_gateway.f_conversation)
    body = source.split("async def _filter_gen", 1)[1].split("if \"stream\" in", 1)[0]
    assert "finally" in body, (
        "_filter_gen must release the upstream in a finally block; a "
        "BackgroundTask does not run when the client disconnects"
    )
