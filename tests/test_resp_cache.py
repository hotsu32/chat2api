"""Unit tests for utils.resp_cache (per-account TTL response cache)."""
import utils.resp_cache as resp_cache


def test_cacheable_paths_and_methods():
    assert resp_cache.cacheable("backend-api/models", "GET") == 3600
    assert resp_cache.cacheable("backend-api/accounts/check/xyz", "GET") == 300
    # non-GET never cached
    assert resp_cache.cacheable("backend-api/models", "POST") is None
    assert resp_cache.cacheable("backend-api/models", "PUT") is None
    # non-cacheable paths
    assert resp_cache.cacheable("backend-api/me", "GET") is None
    assert resp_cache.cacheable("backend-api/conversation", "GET") is None  # POST-only list endpoint


def test_conversation_paths_are_never_cached():
    """conversation/ 前缀下没有一条「短期内不变」的契约，整体不缓存。

    stream_status / async-status 是活跃会话的进行中状态（真实浏览器基线里反复轮询），
    会话详情在同一会话内也会随消息增长而变；旧的 30s TTL 会让这些动态响应变陈旧，
    而按 conversation/{id} 的定向失效既覆盖不到全局状态路径，也挡不住活跃会话变更。
    """
    for path in (
        "backend-api/conversation/abc",
        "backend-api/conversation/abc/stream_status",
        "backend-api/conversation/abc/async-status",
        "backend-api/conversation/abc/textdocs",
        "backend-api/conversation/init",
    ):
        assert resp_cache.cacheable(path, "GET") is None, path


def test_cacheable_static_assets():
    # CDN 静态资源（cache-busted 不可变）长 TTL 缓存，让多用户冷启动也命中
    assert resp_cache.cacheable("assets/app-abc123.js", "GET") == 86400
    assert resp_cache.cacheable("assets/css/main.css", "GET") == 86400
    assert resp_cache.cacheable("assets/app-abc123.js", "POST") is None
    assert resp_cache.cacheable("assets/app-abc123.js", "PUT") is None


def test_set_get_roundtrip():
    resp_cache.invalidate_all()
    key = ("tok-a", "backend-api/models", ())
    entry = {"content": b'{"models": []}', "status": 200, "rheaders": {"content-type": "application/json"}}
    resp_cache.set(key, entry, 3600)
    got = resp_cache.get(key)
    assert got is not None
    assert got["content"] == entry["content"]
    assert got["status"] == 200
    assert "at" in got and "ttl" in got


def test_get_expires_after_ttl(monkeypatch):
    resp_cache.invalidate_all()
    clock = {"now": 1000.0}
    monkeypatch.setattr(resp_cache.time, "time", lambda: clock["now"])

    key = ("tok-a", "backend-api/accounts/check/", ())
    resp_cache.set(key, {"content": b"{}", "status": 200, "rheaders": {}}, 300)
    assert resp_cache.get(key) is not None

    # advance past TTL
    clock["now"] = 1000.0 + 301.0
    assert resp_cache.get(key) is None


def test_invalidate_path_prefix():
    resp_cache.invalidate_all()
    k1 = ("tok-a", "backend-api/conversation/conv-1", ())
    k2 = ("tok-a", "backend-api/conversation/conv-2", ())
    k3 = ("tok-a", "backend-api/models", ())
    for k in (k1, k2, k3):
        resp_cache.set(k, {"content": b"{}", "status": 200, "rheaders": {}}, 30)
    assert resp_cache.size() == 3

    resp_cache.invalidate_path_prefix("backend-api/conversation/conv-1")
    assert resp_cache.get(k1) is None
    assert resp_cache.get(k2) is not None  # different conversation untouched
    assert resp_cache.get(k3) is not None  # models untouched


def test_invalidate_all():
    resp_cache.invalidate_all()
    resp_cache.set(("tok-a", "backend-api/models", ()), {"content": b"{}", "status": 200, "rheaders": {}}, 3600)
    resp_cache.set(("tok-b", "backend-api/me", ()), {"content": b"{}", "status": 200, "rheaders": {}}, 60)
    assert resp_cache.size() == 2
    resp_cache.invalidate_all()
    assert resp_cache.size() == 0


def test_max_entries_evicts_oldest(monkeypatch):
    resp_cache.invalidate_all()
    monkeypatch.setattr(resp_cache, "_MAX_ENTRIES", 3)

    clock = {"now": 1000.0}
    monkeypatch.setattr(resp_cache.time, "time", lambda: clock["now"])
    for i in range(5):
        clock["now"] += 1.0
        resp_cache.set((f"tok-{i}", "backend-api/models", ()),
                       {"content": b"{}", "status": 200, "rheaders": {}}, 3600)

    assert resp_cache.size() == 3
    # 最旧的 tok-0/tok-1 被淘汰，保留最新 3 个
    assert resp_cache.get(("tok-0", "backend-api/models", ())) is None
    assert resp_cache.get(("tok-1", "backend-api/models", ())) is None
    assert resp_cache.get(("tok-2", "backend-api/models", ())) is not None
    assert resp_cache.get(("tok-4", "backend-api/models", ())) is not None
