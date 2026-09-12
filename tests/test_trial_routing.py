"""Tier trial aliases must never reuse a healthy account from another tier.

Alias repair is a development-gate feature (``DEV_ACCESS_ENABLED``): with the gate
closed the repair is a no-op, so production never mints a fresh alias binding to a
real account just because someone hit ``/?token=frontend-proof-*``.
"""


def _enable_dev_access(monkeypatch):
    import utils.configs as configs
    monkeypatch.setattr(configs, "dev_access_enabled", True)


def _seed_map():
    return {
        "frontend-proof-free-1": {"token": "free", "plan_type": "free", "conversations": []},
        "frontend-proof-plus-2": {"token": "free", "plan_type": "free", "conversations": []},
        "frontend-proof-plus-3": {"token": "free", "plan_type": "free", "conversations": []},
        "frontend-proof-pro-1": {"token": "pro", "plan_type": "pro", "conversations": []},
    }


_ENTRIES = [
    ("Free", "frontend-proof-free-1", "free"),
    ("Plus 一", "frontend-proof-plus-2", "plus"),
    ("Plus 二", "frontend-proof-plus-3", "plus"),
    ("Pro 一", "frontend-proof-pro-1", "pro"),
]


def test_core_trial_bindings_repair_stale_free_aliases(monkeypatch):
    import gateway.landing as landing
    import utils.globals as g
    from types import SimpleNamespace  # noqa: F401  (kept for parity with the original imports)

    _enable_dev_access(monkeypatch)
    g.seed_map.clear()
    g.seed_map.update(_seed_map())
    rows = {
        "free": [{"token": "free", "plan_type": "free"}],
        "plus": [{"token": "plus-a", "plan_type": "plus"}, {"token": "plus-b", "plan_type": "plus"}],
        "pro": [{"token": "pro", "plan_type": "pro"}],
    }
    monkeypatch.setattr(landing.store, "get_account", lambda token: next((r for r in sum(rows.values(), []) if r["token"] == token), None))
    monkeypatch.setattr(landing.store, "get_account_by_plan", lambda tier, status="healthy": rows[tier])
    monkeypatch.setattr(landing.globals, "persist_seed_map", lambda: None)
    landing.ensure_core_trial_bindings(_ENTRIES)
    assert g.seed_map["frontend-proof-plus-2"]["token"] == "plus-a"
    assert g.seed_map["frontend-proof-plus-3"]["token"] == "plus-b"
    assert g.seed_map["frontend-proof-free-1"]["token"] == "free"
    assert g.seed_map["frontend-proof-pro-1"]["token"] == "pro"


def test_alias_repair_does_nothing_while_the_dev_gate_is_closed(monkeypatch):
    """闸门关闭 = 别名修复是 no-op：生产不因一次访问就新造出真实账号入口。"""
    import gateway.landing as landing
    import utils.configs as configs
    import utils.globals as g

    monkeypatch.setattr(configs, "dev_access_enabled", False)
    original = _seed_map()
    g.seed_map.clear()
    g.seed_map.update({k: dict(v) for k, v in original.items()})
    monkeypatch.setattr(landing.globals, "persist_seed_map", lambda: None)

    landing.ensure_core_trial_bindings(_ENTRIES)

    assert g.seed_map == original


# ---------------------------------------------------------------------------
# The API boundary, not just the HTML entrance
# ---------------------------------------------------------------------------
# `/?token=frontend-proof-*` is already gated in the chat page.  That gate only
# covers a *browser*: a persisted alias is a seed like any other, so anything
# that resolves a seed into an upstream credential (curl with the alias cookie,
# the OpenAI-compatible API, /api/switch-account) would still hand out the real
# account bound to it.  These pin the admission boundary.

_ALIAS = "frontend-proof-pro-1"
_BOUND_ACCOUNT = "pro-account"          # the real account the alias is bound to


def _bound_alias_env(monkeypatch, *, dev_access):
    """A persisted alias bound to a healthy real account, as production has it."""
    import chatgpt.authorization as auth
    import utils.configs as configs
    import utils.globals as g

    monkeypatch.setattr(configs, "dev_access_enabled", dev_access)
    g.seed_map.clear()
    g.seed_map.update(_seed_map())
    g.seed_map[_ALIAS] = {"token": _BOUND_ACCOUNT, "plan_type": "pro", "conversations": []}
    monkeypatch.setattr(
        auth.store, "get_account",
        lambda token: {"token": token, "plan_type": "pro", "status": "healthy"},
    )
    monkeypatch.setattr(auth.globals, "persist_seed", lambda *a, **kw: None)
    return auth


def test_alias_seed_is_refused_by_every_api_resolution_path_when_the_gate_is_closed(monkeypatch):
    """别名必须**在校准入边界**被拒，而不是只在 HTML 入口被拒。"""
    import asyncio

    import pytest
    from fastapi import HTTPException

    from gateway.reverseProxy import get_real_req_token

    auth = _bound_alias_env(monkeypatch, dev_access=False)

    calls = {
        "get_req_token(alias)": lambda: auth.get_req_token(_ALIAS),
        "get_req_token(seed=alias)": lambda: auth.get_req_token("", _ALIAS),
        "resolve_seed_account": lambda: auth._resolve_seed_account(_ALIAS),
        "switch_seed_account": lambda: auth.switch_seed_account(_ALIAS),
        "get_real_req_token": lambda: asyncio.run(get_real_req_token(_ALIAS)),
    }
    for name, call in calls.items():
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404, name


def test_a_closed_gate_never_hands_out_the_account_bound_to_an_alias(monkeypatch):
    """回归：闸门关闭时别名不得再换出任何真实账号 token。"""
    auth = _bound_alias_env(monkeypatch, dev_access=False)

    resolved = []
    for call in (auth._resolve_seed_account, auth.switch_seed_account, auth.get_req_token):
        try:
            resolved.append(str(call(_ALIAS)))
        except Exception as exc:            # HTTPException(404) is the expected path
            resolved.append(type(exc).__name__)
    assert _BOUND_ACCOUNT not in resolved


def test_alias_seed_still_resolves_for_an_explicitly_enabled_developer(monkeypatch):
    """显式打开闸门 = 别名照常可用：收紧生产不能拆掉开发入口。"""
    auth = _bound_alias_env(monkeypatch, dev_access=True)

    assert auth._resolve_seed_account(_ALIAS) == _BOUND_ACCOUNT
    assert auth.get_req_token("", _ALIAS) == _BOUND_ACCOUNT


def _cookie_request(token):
    from starlette.requests import Request

    return Request({
        "type": "http", "http_version": "1.1", "method": "GET",
        "path": "/api/account-status", "raw_path": b"/api/account-status",
        "query_string": b"", "scheme": "http",
        "headers": [(b"cookie", f"token={token}".encode())],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        "asgi": {"version": "3.0", "spec_version": "2.2"},
    })


def test_alias_cookie_is_refused_where_every_route_reads_the_caller_identity(monkeypatch):
    """身份解析入口也拒绝别名：/api/account-status 这类只读路由不能泄漏绑定账号。"""
    import pytest
    from fastapi import HTTPException

    from gateway.reverseProxy import resolve_seed_token

    _bound_alias_env(monkeypatch, dev_access=False)

    with pytest.raises(HTTPException) as exc:
        resolve_seed_token(_cookie_request(_ALIAS))
    assert exc.value.status_code == 404


def test_alias_cookie_still_resolves_for_an_explicitly_enabled_developer(monkeypatch):
    from gateway.reverseProxy import resolve_seed_token

    _bound_alias_env(monkeypatch, dev_access=True)

    assert resolve_seed_token(_cookie_request(_ALIAS)) == _ALIAS


def test_non_alias_seeds_are_untouched_by_the_alias_gate(monkeypatch):
    """闸门只认别名前缀：运营者 / 普通 seed 的解析路径不变。"""
    import chatgpt.authorization as auth
    import utils.configs as configs
    import utils.globals as g

    monkeypatch.setattr(configs, "dev_access_enabled", False)
    monkeypatch.setattr(auth.store, "get_account",
                        lambda token: {"token": token, "plan_type": "pro", "status": "healthy"})
    monkeypatch.setattr(auth, "_has_durable_seed_grant", lambda seed: seed == "operator-seed")
    g.seed_map.clear()
    g.seed_map["operator-seed"] = {"token": _BOUND_ACCOUNT, "plan_type": "pro", "conversations": []}

    assert auth._resolve_seed_account("operator-seed") == _BOUND_ACCOUNT
