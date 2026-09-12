"""E2E: per-account TTL response cache engages on the real gateway route.

Proves the page-load fix (the "conversation history / action buttons load slowly"
symptom) at the route level, network-free:

  - the first GET to a cacheable endpoint (models / accounts/check) hits upstream once,
  - a repeat GET within TTL is served from cache (upstream NOT contacted again),
  - conversation/ GETs are NEVER cached: every repeat reaches upstream and returns the
    current upstream body, because that prefix carries live state (stream_status /
    async-status polling, details that grow as a turn streams).
"""
import json

import utils.globals as globals
import utils.store as store
import utils.resp_cache as resp_cache


def _model_gets(records):
    return [r for r in records if r["method"] == "GET" and r["path"].split("?")[0] == "/backend-api/models"]


def _conversation_gets(records, path):
    return [r for r in records if r["method"] == "GET" and r["path"].split("?")[0] == path]


def _changing_conversation_upstream(monkeypatch, mock_upstream):
    """Make every conversation/ GET return a *different* body.

    A stale cache is only observable when the upstream answer actually changes —
    which is the real situation these paths are in (a streaming turn's status and
    its growing detail change between two polls seconds apart).
    """
    handler_cls = mock_upstream.RequestHandlerClass
    original = handler_cls.do_GET
    state = {"tick": 0}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/backend-api/conversation/"):
            self._record()
            state["tick"] += 1
            self._json(200, {"path": path, "tick": state["tick"]})
            return
        original(self)

    monkeypatch.setattr(handler_cls, "do_GET", do_GET)
    return state


def test_models_get_is_cached_across_requests(client, mock_upstream, seed_user, seed_account,
                                              make_access_token):
    resp_cache.invalidate_all()
    tok = make_access_token(account_id="acc-cache", plan_type="plus")
    seed_account(tok)
    seed_user("seed-cache", tok, plan_type="plus")

    r1 = client.get("/backend-api/models", cookies={"token": "seed-cache"})
    assert r1.status_code == 200
    assert len(_model_gets(mock_upstream.records)) == 1, "first GET must hit upstream"

    r2 = client.get("/backend-api/models", cookies={"token": "seed-cache"})
    assert r2.status_code == 200
    assert r2.content == r1.content, "cache hit must return identical (scrubbed) body"
    assert len(_model_gets(mock_upstream.records)) == 1, "second GET must be served from cache, not upstream"


def test_models_cache_is_isolated_per_account(client_factory, mock_upstream, seed_user,
                                              seed_account, make_access_token):
    """Cross-account isolation: one account's cached body must never serve another's."""
    resp_cache.invalidate_all()
    tok_a = make_access_token(account_id="acc-iso-a", plan_type="plus")
    tok_b = make_access_token(account_id="acc-iso-b", plan_type="plus")
    seed_account(tok_a)
    seed_account(tok_b)
    seed_user("seed-iso-a", tok_a, plan_type="plus")
    seed_user("seed-iso-b", tok_b, plan_type="plus")

    client_a, client_b = client_factory(), client_factory()
    assert client_a.get("/backend-api/models", cookies={"token": "seed-iso-a"}).status_code == 200
    assert len(_model_gets(mock_upstream.records)) == 1

    # account B must NOT be served account A's cache entry -> its own upstream fetch
    assert client_b.get("/backend-api/models", cookies={"token": "seed-iso-b"}).status_code == 200
    gets = _model_gets(mock_upstream.records)
    assert len(gets) == 2, "a second account must fetch its own copy, not reuse account A's cache"
    assert gets[0]["authorization"] != gets[1]["authorization"], "each fetch carries its own account token"


def test_accounts_check_get_is_cached(client, mock_upstream, seed_user, seed_account,
                                      make_access_token):
    resp_cache.invalidate_all()
    tok = make_access_token(account_id="acc-check", plan_type="plus")
    seed_account(tok)
    seed_user("seed-check", tok, plan_type="plus")

    path = "/backend-api/accounts/check/v4-2023-04-27"
    r1 = client.get(path, cookies={"token": "seed-check"})
    assert r1.status_code == 200
    hits = [r for r in mock_upstream.records if r["method"] == "GET" and r["path"] == path]
    assert len(hits) == 1

    r2 = client.get(path, cookies={"token": "seed-check"})
    assert r2.status_code == 200
    assert r2.content == r1.content
    hits = [r for r in mock_upstream.records if r["method"] == "GET" and r["path"] == path]
    assert len(hits) == 1, "accounts/check must be cached"


def test_conversation_detail_get_is_never_cached(client, mock_upstream, seed_user, seed_account,
                                                 make_access_token, monkeypatch):
    """An active conversation's detail grows while a turn streams — every GET must be fresh."""
    resp_cache.invalidate_all()
    _changing_conversation_upstream(monkeypatch, mock_upstream)
    tok = make_access_token(account_id="acc-conv", plan_type="plus")
    seed_account(tok)
    seed_user("seed-conv", tok, plan_type="plus", conversations=["conv-1"])
    globals.conversation_map["conv-1"] = {
        "id": "conv-1", "title": "Old Title", "create_time": 1, "update_time": 1,
        "account": tok,
    }

    detail = "/backend-api/conversation/conv-1"
    r1 = client.get(detail, cookies={"token": "seed-conv"})
    assert r1.status_code == 200
    assert len(_conversation_gets(mock_upstream.records, detail)) == 1

    r2 = client.get(detail, cookies={"token": "seed-conv"})
    assert r2.status_code == 200
    assert len(_conversation_gets(mock_upstream.records, detail)) == 2, \
        "repeat detail GET must reach upstream, not a 30s-stale cache"
    assert json.loads(r2.content)["tick"] > json.loads(r1.content)["tick"], \
        "the changed upstream body must be returned, not the first one"


def test_conversation_status_paths_are_never_cached(client, mock_upstream, seed_user, seed_account,
                                                    make_access_token, monkeypatch):
    """stream_status / async-status / textdocs are polled repeatedly on a live turn.

    The real-browser baseline shows GET conversation/{id}/stream_status called over and
    over during one turn; caching it for 30s pins the UI to a status that is already gone.
    """
    resp_cache.invalidate_all()
    _changing_conversation_upstream(monkeypatch, mock_upstream)
    tok = make_access_token(account_id="acc-status", plan_type="plus")
    seed_account(tok)
    seed_user("seed-status", tok, plan_type="plus", conversations=["conv-1"])
    globals.conversation_map["conv-1"] = {
        "id": "conv-1", "title": "T", "create_time": 1, "update_time": 1, "account": tok,
    }

    for suffix in ("stream_status", "async-status", "textdocs"):
        path = f"/backend-api/conversation/conv-1/{suffix}"
        bodies = []
        for _ in range(3):
            r = client.get(path, cookies={"token": "seed-status"})
            assert r.status_code == 200
            bodies.append(json.loads(r.content)["tick"])
        assert len(_conversation_gets(mock_upstream.records, path)) == 3, \
            f"every GET to {suffix} must reach upstream"
        assert bodies == sorted(set(bodies)), \
            f"{suffix} must return each fresh upstream body, never a repeated cached one"


def test_conversation_init_is_never_cached(client, mock_upstream, seed_user, seed_account,
                                           make_access_token, monkeypatch):
    """conversation/init is a global (not per-conversation) path the old prefix also caught.

    Per-conversation invalidation could never clear it, so it could go stale for 30s with
    no way to flush it. It reaches the proxy only for a direct-access-token client (the
    mirror route treats "init" as a conversation id and 404s it on ownership), so that is
    the caller exercised here.
    """
    resp_cache.invalidate_all()
    _changing_conversation_upstream(monkeypatch, mock_upstream)
    tok = make_access_token(account_id="acc-init", plan_type="plus")
    seed_account(tok)
    store.upsert_account(tok, plan_type="plus", status="healthy")

    path = "/backend-api/conversation/init"
    headers = {"Authorization": f"Bearer {tok}"}
    r1 = client.get(path, headers=headers)
    r2 = client.get(path, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(_conversation_gets(mock_upstream.records, path)) == 2
    assert json.loads(r2.content)["tick"] > json.loads(r1.content)["tick"]


def test_patch_conversation_still_serves_fresh_detail(client, mock_upstream, seed_user, seed_account,
                                                      make_access_token, monkeypatch):
    """A rename must be visible on the next GET — trivially true with no detail cache."""
    resp_cache.invalidate_all()
    _changing_conversation_upstream(monkeypatch, mock_upstream)
    tok = make_access_token(account_id="acc-patch", plan_type="plus")
    seed_account(tok)
    seed_user("seed-patch", tok, plan_type="plus", conversations=["conv-1"])
    globals.conversation_map["conv-1"] = {
        "id": "conv-1", "title": "Old", "create_time": 1, "update_time": 1, "account": tok,
    }

    detail = "/backend-api/conversation/conv-1"
    r1 = client.get(detail, cookies={"token": "seed-patch"})
    assert r1.status_code == 200
    assert len(_conversation_gets(mock_upstream.records, detail)) == 1

    rp = client.patch(detail, json={"title": "Renamed", "is_visible": True}, cookies={"token": "seed-patch"})
    assert rp.status_code == 200

    r3 = client.get(detail, cookies={"token": "seed-patch"})
    assert r3.status_code == 200
    assert len(_conversation_gets(mock_upstream.records, detail)) == 2
    assert json.loads(r3.content)["tick"] > json.loads(r1.content)["tick"], \
        "post-PATCH GET must reflect the current upstream state"


def test_is_textual_content():
    from gateway.reverseProxy import _is_textual_content
    assert _is_textual_content("application/json") is True
    assert _is_textual_content("text/javascript") is True
    assert _is_textual_content("text/css") is True
    assert _is_textual_content("application/json; charset=utf-8") is True
    # binary must be excluded from the text cache
    assert _is_textual_content("font/woff2") is False
    assert _is_textual_content("application/octet-stream") is False
    assert _is_textual_content("application/wasm") is False


def test_static_fp_is_stable():
    from chatgpt.fp import get_fp
    a = get_fp("")
    b = get_fp("")
    assert a["user-agent"] == b["user-agent"]
    assert a["impersonate"] == b["impersonate"]
    assert a["oai-device-id"] == b["oai-device-id"]
    # returned dict must be a copy: mutating it must not corrupt the shared cache
    b.pop("proxy_url", None)
    c = get_fp("")
    assert c["user-agent"] == a["user-agent"]
    assert c["impersonate"] == a["impersonate"]


def test_static_fp_refreshes_after_ttl(monkeypatch):
    import chatgpt.fp as fp
    monkeypatch.setattr(fp, "_static_fp_cache", None)
    monkeypatch.setattr(fp, "_static_fp_cache_at", 0.0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(fp.time, "time", lambda: clock["now"])

    a = fp.get_fp("")
    b = fp.get_fp("")
    assert a["oai-device-id"] == b["oai-device-id"], "within TTL the fingerprint is reused"

    # advance past the TTL -> re-rolled, oai-device-id (uuid4) must change
    clock["now"] += fp._STATIC_FP_TTL + 1.0
    c = fp.get_fp("")
    assert c["oai-device-id"] != a["oai-device-id"], "static fingerprint must re-roll after TTL"
