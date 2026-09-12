"""M3: the button-latency fault-injection suite.

What M3 must prove is *causal*, not observational: a browser timeline showing
"buttons appeared 2s after the text" does not say whether the buttons waited on
a request, and a cache unit test showing "conversation/ is not cached" does not
say whether the buttons are fast. These tests sit in between — they make one
upstream dependency slow / broken / dead at a time and assert what the turn's
own response does in each case.

The mechanism they pin down (measured in a real Chrome, evidence under
tmp/task-evidence/m3/):

  * The terminal action bar (copy / rate / more / share) is rendered by the
    official bundle from the turn's own SSE stream. It does NOT wait for
    conversation detail, stream_status, async-status or textdocs.
  * Therefore: making those endpoints slow or failing must NOT delay or block
    the terminal frame of the turn response. If a future change made the turn
    response wait on them, the buttons would inherit that latency, and these
    tests fail.

Timing assertions here are deliberately generous (they assert "did not inherit
the injected 2s", not "under 500ms"): a TestClient measurement is not a browser
measurement, and the 500ms acceptance number is only claimed from real-browser
evidence. What these tests guarantee is the *absence of a serialising
dependency* — the property that makes the browser number achievable.
"""
import json
import socket
import threading
import time

import utils.globals as globals
import utils.resp_cache as resp_cache


import pytest

import app as app_module
import utils.configs as configs


@pytest.fixture
def live_server(mock_upstream):
    """A real uvicorn server on a loopback port, for tests that must observe
    streaming as it happens.

    TestClient buffers the whole response body, so it reports every frame of a
    StreamingResponse at the time the *last* one arrives. Any test asserting
    "this frame arrived before that delay elapsed" is meaningless against it and
    needs a real socket.
    """
    import uvicorn

    configs.chatgpt_base_url_list[:] = [mock_upstream.url]
    config = uvicorn.Config(app_module.app, host="127.0.0.1", port=0,
                            log_level="critical", lifespan="off", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server.started and server.servers and server.servers[0].sockets:
            break
        time.sleep(0.02)
    else:                                        # pragma: no cover - startup failure
        raise RuntimeError("uvicorn did not start")
    host, port = server.servers[0].sockets[0].getsockname()[:2]
    try:
        yield (host, port)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


TURN_PATH = "/backend-api/conversation"
DETAIL = "/backend-api/conversation/conv-1"
STATUS_PATHS = ("stream_status", "async-status", "textdocs")

# Injected upstream stall. Must be far larger than any plausible local overhead
# so "inherited the stall" and "did not" cannot be confused with jitter.
INJECTED_DELAY_S = 2.0


def _seed_turn(seed_user, seed_account, make_access_token, name):
    tok = make_access_token(account_id=f"acc-{name}", plan_type="plus")
    seed_account(tok)
    seed_user(f"seed-{name}", tok, plan_type="plus", conversations=["conv-1"])
    globals.conversation_map["conv-1"] = {
        "id": "conv-1", "title": "T", "create_time": 1, "update_time": 1, "account": tok,
    }
    return tok


def _install(monkeypatch, mock_upstream, *, get_hook=None):
    """Replace the mock upstream's GET handling for conversation/* paths."""
    handler_cls = mock_upstream.RequestHandlerClass
    original = handler_cls.do_GET

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/backend-api/conversation/") and get_hook is not None:
            get_hook(self, path)
            return
        original(self)

    monkeypatch.setattr(handler_cls, "do_GET", do_GET)


def _turn(client, seed):
    """Run one full turn, returning (elapsed_seconds, response)."""
    started = time.monotonic()
    r = client.post(TURN_PATH, json={
        "action": "next",
        "messages": [{"id": "u-1", "author": {"role": "user"},
                      "content": {"content_type": "text", "parts": ["hi"]}}],
        "model": "gpt-5-5",
        "conversation_mode": {"kind": "primary_assistant"},
    }, cookies={"token": seed})
    return time.monotonic() - started, r


def _has_terminal(body: str) -> bool:
    """Did the turn response actually carry a terminal frame?

    The action bar mounts off the terminal message state, so "the stream ended"
    is not enough — a stream truncated mid-turn also ends.
    """
    return "finished_successfully" in body or "[DONE]" in body


# --------------------------------------------------------------------------
# 1. a slow conversation-detail / status endpoint must not delay the turn
# --------------------------------------------------------------------------

def test_turn_terminal_does_not_wait_on_slow_conversation_detail(
        client, mock_upstream, seed_user, seed_account, make_access_token, monkeypatch):
    """Inject a 2s stall into every conversation/* GET.

    If the turn's terminal frame were serialised behind detail/status (the
    "buttons wait for the detail request" hypothesis), the turn would inherit
    the stall. It must not.
    """
    resp_cache.invalidate_all()
    seed = "seed-slowdetail"
    _seed_turn(seed_user, seed_account, make_access_token, "slowdetail")

    def slow(handler, path):
        handler._record()
        time.sleep(INJECTED_DELAY_S)
        handler._json(200, {"path": path, "slow": True})

    _install(monkeypatch, mock_upstream, get_hook=slow)

    elapsed, r = _turn(client, seed)
    assert r.status_code == 200
    assert _has_terminal(r.text), "turn must still carry a terminal frame"
    assert elapsed < INJECTED_DELAY_S, (
        f"turn took {elapsed:.2f}s with a {INJECTED_DELAY_S}s stall injected into "
        "conversation/* GETs — the terminal frame is serialised behind them, so the "
        "action bar would inherit that latency"
    )


# --------------------------------------------------------------------------
# 2. a broken (403) status endpoint must not block the turn
# --------------------------------------------------------------------------

def test_turn_terminal_survives_403_on_status_paths(
        client, mock_upstream, seed_user, seed_account, make_access_token, monkeypatch):
    """403 on detail/status must leave the turn — and so the buttons — intact."""
    resp_cache.invalidate_all()
    seed = "seed-403"
    _seed_turn(seed_user, seed_account, make_access_token, "403")

    def forbidden(handler, path):
        handler._record()
        handler._json(403, {"detail": "forbidden"})

    _install(monkeypatch, mock_upstream, get_hook=forbidden)

    elapsed, r = _turn(client, seed)
    assert r.status_code == 200
    assert _has_terminal(r.text), (
        "a 403 on conversation status must not strip the turn's terminal frame"
    )
    assert elapsed < INJECTED_DELAY_S


def test_status_403_is_not_cached_and_not_masked(
        client, mock_upstream, seed_user, seed_account, make_access_token, monkeypatch):
    """A failing status must surface as a failure every time, never be cached.

    Caching an error would pin a transiently-broken status into the UI, and
    "buttons hidden because status said so" would outlive the actual fault.
    """
    resp_cache.invalidate_all()
    seed = "seed-403c"
    _seed_turn(seed_user, seed_account, make_access_token, "403c")

    calls = {"n": 0}

    def forbidden(handler, path):
        handler._record()
        calls["n"] += 1
        handler._json(403, {"detail": "forbidden"})

    _install(monkeypatch, mock_upstream, get_hook=forbidden)

    path = f"{DETAIL}/stream_status"
    for _ in range(3):
        r = client.get(path, cookies={"token": seed})
        assert r.status_code == 403, "the upstream failure must reach the caller, not be masked as 200"
    assert calls["n"] == 3, "a 403 status must never be served from cache"


# --------------------------------------------------------------------------
# 3. a stream that dies mid-turn must be distinguishable from a finished one
# --------------------------------------------------------------------------

def test_truncated_stream_does_not_fake_a_terminal(
        client, mock_upstream, seed_user, seed_account, make_access_token):
    """A turn cut off mid-stream must NOT present as finished.

    This is the anti-fake assertion for M3: the action bar mounting on a
    terminal state is only correct if a terminal state means the turn really
    finished. If a truncated stream still yielded finished_successfully, the
    buttons would look fast by lying.
    """
    resp_cache.invalidate_all()
    seed = "seed-trunc"
    _seed_turn(seed_user, seed_account, make_access_token, "trunc")

    # in_progress frame only: upstream dies before any terminal frame
    mock_upstream.conversation_sse = (
        "data: " + json.dumps({
            "message": {
                "id": "msg-1", "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["partial"]},
                "status": "in_progress", "metadata": {},
            },
            "conversation_id": "conv-1",
        }) + "\n\n"
    ).encode("utf-8")

    _, r = _turn(client, seed)
    assert "finished_successfully" not in r.text, (
        "a truncated stream must not be rewritten into a successful terminal state"
    )


# --------------------------------------------------------------------------
# 4. cold vs warm cache must not change what a turn or a status call returns
# --------------------------------------------------------------------------

def test_cold_and_warm_cache_agree_on_status(
        client, mock_upstream, seed_user, seed_account, make_access_token, monkeypatch):
    """Warm the cache with cacheable traffic, then assert dynamic state is still live.

    The failure this guards: a cache warmed by models/accounts-check traffic
    starts serving conversation status too, and the action bar reads a status
    from a previous turn.
    """
    resp_cache.invalidate_all()
    seed = "seed-coldwarm"
    _seed_turn(seed_user, seed_account, make_access_token, "coldwarm")

    tick = {"n": 0}

    def changing(handler, path):
        handler._record()
        tick["n"] += 1
        handler._json(200, {"path": path, "tick": tick["n"]})

    _install(monkeypatch, mock_upstream, get_hook=changing)

    # cold: first read of each dynamic path
    cold = {}
    for suffix in STATUS_PATHS:
        r = client.get(f"{DETAIL}/{suffix}", cookies={"token": seed})
        assert r.status_code == 200
        cold[suffix] = json.loads(r.content)["tick"]

    # warm the cache hard with a genuinely cacheable endpoint
    for _ in range(3):
        assert client.get("/backend-api/models", cookies={"token": seed}).status_code == 200
    assert resp_cache.size() > 0, "models must actually be cached (else this proves nothing)"

    # warm: the same dynamic paths must still be live
    for suffix in STATUS_PATHS:
        r = client.get(f"{DETAIL}/{suffix}", cookies={"token": seed})
        assert r.status_code == 200
        warm = json.loads(r.content)["tick"]
        assert warm > cold[suffix], (
            f"{suffix} returned a stale body ({warm} <= {cold[suffix]}) once the cache was warm"
        )


def test_account_switch_does_not_leak_status_across_seeds(
        client_factory, mock_upstream, seed_user, seed_account, make_access_token, monkeypatch):
    """Two accounts, same conversation-shaped path: neither may see the other's state."""
    resp_cache.invalidate_all()
    tok_a = make_access_token(account_id="acc-sw-a", plan_type="plus")
    tok_b = make_access_token(account_id="acc-sw-b", plan_type="plus")
    seed_account(tok_a)
    seed_account(tok_b)
    seed_user("seed-sw-a", tok_a, plan_type="plus", conversations=["conv-1"])
    seed_user("seed-sw-b", tok_b, plan_type="plus", conversations=["conv-1"])
    globals.conversation_map["conv-1"] = {
        "id": "conv-1", "title": "T", "create_time": 1, "update_time": 1, "account": tok_a,
    }

    seen = []

    def per_account(handler, path):
        handler._record()
        seen.append(handler.headers.get("Authorization", ""))
        handler._json(200, {"path": path, "n": len(seen)})

    _install(monkeypatch, mock_upstream, get_hook=per_account)

    client_a, client_b = client_factory(), client_factory()
    path = f"{DETAIL}/stream_status"
    ra = client_a.get(path, cookies={"token": "seed-sw-a"})
    assert ra.status_code == 200

    globals.conversation_map["conv-1"]["account"] = tok_b
    rb = client_b.get(path, cookies={"token": "seed-sw-b"})
    assert rb.status_code == 200

    assert len(seen) == 2, "the second account must reach upstream, not reuse account A's entry"
    assert seen[0] != seen[1], "each account's status fetch must carry its own credential"
    assert json.loads(rb.content)["n"] != json.loads(ra.content)["n"], \
        "account B must not receive account A's status body"


# --------------------------------------------------------------------------
# 5. concurrent status polling must not serialise behind one slow call
# --------------------------------------------------------------------------

def test_slow_status_poll_does_not_block_a_concurrent_turn(
        client_factory, mock_upstream, seed_user, seed_account, make_access_token, monkeypatch):
    """A stuck status poll must not hold up a turn running next to it.

    If a shared lock (e.g. around the response cache) serialised these, one slow
    poll would delay every other user's terminal frame — a latency source that a
    single-request timeline can never reveal.
    """
    resp_cache.invalidate_all()
    seed = "seed-conc"
    _seed_turn(seed_user, seed_account, make_access_token, "conc")

    def slow(handler, path):
        handler._record()
        time.sleep(INJECTED_DELAY_S)
        handler._json(200, {"path": path})

    _install(monkeypatch, mock_upstream, get_hook=slow)

    poller = client_factory()
    runner = client_factory()
    done = threading.Event()

    def poll():
        try:
            poller.get(f"{DETAIL}/stream_status", cookies={"token": seed})
        finally:
            done.set()

    t = threading.Thread(target=poll, daemon=True)
    t.start()
    time.sleep(0.2)                      # ensure the slow poll is in flight

    elapsed, r = _turn(runner, seed)
    assert r.status_code == 200
    assert _has_terminal(r.text)
    assert elapsed < INJECTED_DELAY_S, (
        f"a turn took {elapsed:.2f}s while one status poll was stalled for "
        f"{INJECTED_DELAY_S}s — turns are serialised behind status polling"
    )
    done.wait(timeout=INJECTED_DELAY_S * 3)
    t.join(timeout=1)


# --------------------------------------------------------------------------
# 8. the gateway must not buffer the terminal frame behind the stream's tail
# --------------------------------------------------------------------------

def test_gateway_forwards_terminal_before_upstream_closes(
        mock_upstream, seed_user, seed_account, make_access_token, live_server):
    """The terminal frame must reach the client while upstream is still holding
    the connection open.

    Real-browser evidence (tmp/task-evidence/m3/m3-sse-*.json) shows a turn's SSE
    connection can stay open ~2s after its terminal frame. That tail is why a
    finished reply can still look like it is generating: the composer clears its
    "generating" state on stream events, not on the text. This test isolates the
    half we own — the mock upstream deliberately lingers after sending the
    terminal frame, and the gateway must have already forwarded it.

    Uses a real uvicorn server and a raw socket rather than TestClient, because
    TestClient buffers the entire response body before returning it (verified:
    a StreamingResponse with a 1.5s gap yields both frames at t=1.5s), so it
    cannot distinguish streaming from buffering at all.
    """
    resp_cache.invalidate_all()
    seed = "seed-tail"
    _seed_turn(seed_user, seed_account, make_access_token, "tail")

    hold_s = 1.5
    terminal = json.dumps({
        "message": {
            "id": "msg-1", "author": {"role": "assistant"},
            "content": {"content_type": "text", "parts": ["done"]},
            "status": "finished_successfully", "end_turn": True, "metadata": {},
        },
        "conversation_id": "conv-1",
    })

    handler_cls = mock_upstream.RequestHandlerClass
    original_post = handler_cls.do_POST

    def do_POST(self):
        if self.path.split("?")[0] != TURN_PATH:
            original_post(self)
            return
        self._record(self._read_body())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(payload):
            raw = f"data: {payload}\n\n".encode("utf-8")
            self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            self.wfile.flush()

        chunk(terminal)
        time.sleep(hold_s)          # upstream lingers before closing
        chunk("[DONE]")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    handler_cls.do_POST = do_POST
    try:
        body = json.dumps({
            "action": "next",
            "messages": [{"id": "u-1", "author": {"role": "user"},
                          "content": {"content_type": "text", "parts": ["hi"]}}],
            "model": "gpt-5-5",
            "conversation_mode": {"kind": "primary_assistant"},
        }).encode("utf-8")
        request = (
            f"POST {TURN_PATH} HTTP/1.1\r\n"
            f"Host: {live_server[0]}:{live_server[1]}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Cookie: token={seed}\r\n"
            f"Accept: text/event-stream\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body

        sock = socket.create_connection(live_server, timeout=30)
        started = time.monotonic()
        seen_terminal_at = None
        try:
            sock.sendall(request)
            buf = b""
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                buf += data
                if seen_terminal_at is None and b"finished_successfully" in buf:
                    seen_terminal_at = time.monotonic() - started
                    break
        finally:
            sock.close()
    finally:
        handler_cls.do_POST = original_post

    assert seen_terminal_at is not None, "terminal frame never reached the client"
    assert seen_terminal_at < hold_s, (
        f"terminal frame surfaced after {seen_terminal_at:.2f}s while upstream held "
        f"the stream open for {hold_s}s after sending it — the gateway buffers the "
        "stream and manufactures the tail itself"
    )
