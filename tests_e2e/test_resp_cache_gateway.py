"""E2E: per-account TTL response cache engages on the real gateway route.

Proves the page-load fix (the "conversation history / action buttons load slowly"
symptom) at the route level, network-free:

  - the first GET to a cacheable endpoint (models / accounts/check / conversation/{id})
    hits the upstream once,
  - a repeat GET within TTL is served from cache (upstream NOT contacted again),
  - posting a new message invalidates the conversation-detail cache so the next
    GET refetches (fresh history).
"""
import utils.globals as globals
import utils.store as store
import utils.resp_cache as resp_cache


def _model_gets(records):
    return [r for r in records if r["method"] == "GET" and r["path"].split("?")[0] == "/backend-api/models"]


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


def test_conversation_detail_cache_invalidated_on_new_message(client, mock_upstream, seed_user,
                                                              seed_account, make_access_token):
    resp_cache.invalidate_all()
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
    hits = [r for r in mock_upstream.records if r["method"] == "GET" and r["path"] == detail]
    assert len(hits) == 1

    # repeat within TTL -> cached, upstream NOT contacted
    r2 = client.get(detail, cookies={"token": "seed-conv"})
    assert r2.status_code == 200
    assert r2.content == r1.content
    hits = [r for r in mock_upstream.records if r["method"] == "GET" and r["path"] == detail]
    assert len(hits) == 1

    # a new message records the conversation -> invalidates the detail cache -> refetch
    from gateway.reverseProxy import save_conversation
    save_conversation("seed-conv", "conv-1", "New Title")
    r3 = client.get(detail, cookies={"token": "seed-conv"})
    assert r3.status_code == 200
    hits = [r for r in mock_upstream.records if r["method"] == "GET" and r["path"] == detail]
    assert len(hits) == 2, "conversation detail must refetch after a new message"


def test_patch_conversation_invalidates_detail_cache(client, mock_upstream, seed_user, seed_account,
                                                     make_access_token):
    resp_cache.invalidate_all()
    tok = make_access_token(account_id="acc-patch", plan_type="plus")
    seed_account(tok)
    seed_user("seed-patch", tok, plan_type="plus", conversations=["conv-1"])
    globals.conversation_map["conv-1"] = {
        "id": "conv-1", "title": "Old", "create_time": 1, "update_time": 1, "account": tok,
    }

    detail = "/backend-api/conversation/conv-1"
    hits = lambda: [r for r in mock_upstream.records if r["method"] == "GET" and r["path"].split("?")[0] == detail]

    r1 = client.get(detail, cookies={"token": "seed-patch"})
    assert r1.status_code == 200
    assert len(hits()) == 1

    r2 = client.get(detail, cookies={"token": "seed-patch"})
    assert r2.status_code == 200
    assert len(hits()) == 1, "repeat GET must hit cache"

    # PATCH (rename/archive) mutates the conversation -> must invalidate the detail cache
    rp = client.patch(detail, json={"title": "Renamed", "is_visible": True}, cookies={"token": "seed-patch"})
    assert rp.status_code == 200

    r3 = client.get(detail, cookies={"token": "seed-patch"})
    assert r3.status_code == 200
    assert len(hits()) == 2, "PATCH must invalidate the conversation detail cache"


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
