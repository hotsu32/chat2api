"""Targeted hardening of the Seed revocation surface (three residual findings).

  1. ``DELETE /seedtoken`` with ``seed="clear"`` delegated the revocation of the
     durable operator grants to ``persist_seed_map``'s ``list_users`` sweep. That
     read swallows storage failures and returns an empty list, so a single failed
     read turned "revoke every grant" into "revoke none" while the endpoint still
     answered ``success`` — the grants, not the bindings, are what authorize the
     paid pool.
  2. A named durable grant (``users.status='operator'``) whose in-memory
     ``seed_map`` entry is gone (failed persist, restart, direct DB edit) could not
     be revoked at all: the route answered 404 before touching the grant.
  3. ``/c/{conversation_id}`` keyed conversation ownership off the raw ``token``
     cookie instead of the canonical identity resolution, so a public development
     alias left behind by an earlier development run still worked as an ownership
     key with ``DEV_ACCESS_ENABLED=false``.

Contract under test: revocation is explicit, atomic and fail-closed — a storage
failure is an error, never "nothing to revoke" — and the conversation route reads
the caller identity through the same gate every other route uses.

See ``tests/test_unknown_seed_authorization.py`` for the allocation contract itself.
"""

import json

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

import utils.configs as configs
import utils.globals as globals
import utils.store as store
from chatgpt.authorization import OPERATOR_SEED_STATUS

OPERATOR_KEY = "operator-key"
ALIAS = "frontend-proof-plus-2"


@pytest.fixture(autouse=True)
def _reset(db, monkeypatch):
    import gateway.share as share

    globals.seed_map.clear()
    monkeypatch.setattr(share, "authorization_list", [OPERATOR_KEY])
    monkeypatch.setattr(configs, "dev_access_enabled", False)
    yield
    globals.seed_map.clear()


def _credentials(key=OPERATOR_KEY):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)


def _request(payload=None, cookies=None, method="DELETE", path="/seedtoken"):
    body = json.dumps(payload).encode() if payload is not None else b""
    state = {"sent": False}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        import asyncio
        await asyncio.Future()

    headers = []
    if payload is not None:
        headers.append((b"content-type", b"application/json"))
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    scope = {
        "type": "http", "http_version": "1.1", "method": method,
        "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": headers, "scheme": "http",
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        "asgi": {"version": "3.0", "spec_version": "2.2"},
    }
    return Request(scope, receive)


def _grant(seed, account="plus-1", conversations=()):
    """A durable operator grant plus (optionally) an in-memory binding."""
    store.upsert_user(seed, status=OPERATOR_SEED_STATUS, current_account=account)
    for conv_id in conversations:
        store.upsert_conversation(conv_id, seed, account, "t", 1, 2)
    return seed


# ------------------------------------------------------------- 1 revoke all

async def test_clear_revokes_grants_even_when_the_user_list_cannot_be_read(monkeypatch):
    """The old sweep was a no-op whenever ``list_users`` failed; the response said
    success anyway. Revocation must not be a side effect of a best-effort read."""
    from gateway import share

    _grant("op-a", "plus-1", conversations=["conv-a"])
    _grant("op-b", "plus-2")
    globals.seed_map["op-a"] = {"token": "plus-1", "plan_type": "plus", "conversations": ["conv-a"]}
    monkeypatch.setattr(store, "list_users", lambda: [])

    response = await share.delete_seedtoken(_request({"seed": "clear"}), _credentials())

    assert response["status"] == "success"
    assert globals.seed_map == {}
    assert store.get_user("op-a") is None
    assert store.get_user("op-b") is None


async def test_clear_revokes_grants_before_the_binding_sync_can_fail(monkeypatch):
    """Revocation must not be a side effect of the binding sync: it happens first,
    and a broken sync is reported as an error instead of a successful clear."""
    from gateway import share

    _grant("op-a", "plus-1", conversations=["conv-a"])
    globals.seed_map["op-a"] = {"token": "plus-1", "plan_type": "plus", "conversations": ["conv-a"]}

    def broken_persist():
        raise RuntimeError("seed map persistence unavailable")

    monkeypatch.setattr(globals, "persist_seed_map", broken_persist)

    with pytest.raises(HTTPException) as exc:
        await share.delete_seedtoken(_request({"seed": "clear"}), _credentials())

    assert exc.value.status_code == 500
    assert store.get_user("op-a") is None


async def test_clear_fails_closed_when_the_grant_store_is_unavailable(monkeypatch):
    """A storage failure while revoking must not be reported as a successful clear."""
    from gateway import share

    _grant("op-a")
    globals.seed_map["op-a"] = {"token": "plus-1", "plan_type": "plus", "conversations": []}

    def unavailable(status):
        raise store.StoreError("grant store unavailable")

    monkeypatch.setattr(store, "delete_operator_grants", unavailable)

    with pytest.raises(HTTPException) as exc:
        await share.delete_seedtoken(_request({"seed": "clear"}), _credentials())

    assert exc.value.status_code == 503
    assert store.get_user("op-a") is not None          # untouched, but the caller was told
    assert "op-a" in globals.seed_map                  # bindings survive a failed revoke


# ------------------------------------------------------------ 2 single revoke

async def test_named_durable_grant_is_revocable_without_an_in_memory_binding():
    """A grant can outlive its binding (failed persist / restart / manual edit).
    Revoking by name must still work instead of 404-ing on the missing binding."""
    from gateway import share

    _grant("orphan-op", "plus-1", conversations=["conv-orphan"])
    assert "orphan-op" not in globals.seed_map

    response = await share.delete_seedtoken(_request({"seed": "orphan-op"}), _credentials())

    assert response["status"] == "success"
    assert store.get_user("orphan-op") is None


async def test_revoke_with_a_binding_still_removes_binding_grant_and_history():
    from gateway import share

    _grant("op-a", "plus-1", conversations=["conv-a"])
    globals.seed_map["op-a"] = {"token": "plus-1", "plan_type": "plus", "conversations": ["conv-a"]}

    response = await share.delete_seedtoken(_request({"seed": "op-a"}), _credentials())

    assert response["status"] == "success"
    assert "op-a" not in globals.seed_map
    assert store.get_user("op-a") is None
    assert store.list_seed_conversations("op-a") == []


async def test_named_revoke_fails_closed_and_leaves_the_binding_alone(monkeypatch):
    """When the grant store is unreachable, nothing is half-done: the binding stays
    and the caller is told, instead of a 200 on a revocation that did not happen."""
    from gateway import share

    _grant("op-a", "plus-1")
    globals.seed_map["op-a"] = {"token": "plus-1", "plan_type": "plus", "conversations": []}

    def unavailable(seed, status):
        raise store.StoreError("grant store unavailable")

    monkeypatch.setattr(store, "delete_operator_grant", unavailable)

    with pytest.raises(HTTPException) as exc:
        await share.delete_seedtoken(_request({"seed": "op-a"}), _credentials())

    assert exc.value.status_code == 503
    assert "op-a" in globals.seed_map
    assert store.get_user("op-a") is not None


async def test_unknown_seed_without_any_grant_is_still_not_found():
    from gateway import share

    with pytest.raises(HTTPException) as exc:
        await share.delete_seedtoken(_request({"seed": "never-granted"}), _credentials())

    assert exc.value.status_code == 404


# ----------------------------------------------------- 3 conversation ownership

def _owned_alias(conversations=("conv-1",)):
    globals.seed_map[ALIAS] = {
        "token": "plus-1", "plan_type": "plus", "conversations": list(conversations),
    }
    return ALIAS


async def test_conversation_route_refuses_a_disabled_development_alias(monkeypatch):
    """The alias name is public and guessable, so with the gate shut a leftover
    binding must not act as an ownership key for /c/{id}."""
    from gateway import chatgpt

    _owned_alias()

    with pytest.raises(HTTPException) as exc:
        await chatgpt.conversation_page(_request(cookies={"token": ALIAS}, method="GET",
                                                  path="/c/conv-1"), "conv-1")

    assert exc.value.status_code == 404


async def test_conversation_route_still_serves_an_owned_conversation(monkeypatch):
    """No regression for a normal seed: the cookie is still the ownership key."""
    from gateway import chatgpt

    globals.seed_map["operator-seed"] = {
        "token": "plus-1", "plan_type": "plus", "conversations": ["conv-1"],
    }
    monkeypatch.setattr(chatgpt, "_entitled", lambda seed: True)
    async def _rendered(request, token):
        return f"rendered:{token}"

    monkeypatch.setattr(chatgpt, "_render_account_page", _rendered)

    result = await chatgpt.conversation_page(
        _request(cookies={"token": "operator-seed"}, method="GET", path="/c/conv-1"), "conv-1")

    assert result == "rendered:operator-seed"


async def test_conversation_route_still_serves_the_alias_while_the_gate_is_open(monkeypatch):
    from gateway import chatgpt

    _owned_alias()
    monkeypatch.setattr(configs, "dev_access_enabled", True)
    monkeypatch.setattr(chatgpt, "_entitled", lambda seed: True)

    async def _rendered(request, token):
        return f"rendered:{token}"

    monkeypatch.setattr(chatgpt, "_render_account_page", _rendered)

    result = await chatgpt.conversation_page(
        _request(cookies={"token": ALIAS}, method="GET", path="/c/conv-1"), "conv-1")

    assert result == f"rendered:{ALIAS}"


async def test_conversation_route_still_refuses_a_conversation_the_seed_does_not_own():
    """Ownership is still checked: the identity gate must not loosen this route."""
    from gateway import chatgpt

    globals.seed_map["operator-seed"] = {
        "token": "plus-1", "plan_type": "plus", "conversations": [],
    }

    with pytest.raises(HTTPException) as exc:
        await chatgpt.conversation_page(
            _request(cookies={"token": "operator-seed"}, method="GET", path="/c/conv-1"),
            "conv-1")

    assert exc.value.status_code == 404


# ------------------------------------------------------------ store primitives

def test_store_revokes_only_operator_grants_and_cascades_conversations():
    _grant("op-a", conversations=["conv-a"])
    _grant("op-b")
    store.upsert_user("saas-user", status="active")

    revoked = store.delete_operator_grants(OPERATOR_SEED_STATUS)

    assert revoked == 2
    assert store.get_user("op-a") is None
    assert store.get_user("op-b") is None
    assert store.get_user("saas-user") is not None
    assert store.list_seed_conversations("op-a") == []
    assert store.delete_operator_grants(OPERATOR_SEED_STATUS) == 0  # idempotent


def test_store_single_grant_revocation_reports_whether_a_row_was_removed():
    _grant("op-a", conversations=["conv-a"])
    store.upsert_user("saas-user", status="active")

    assert store.delete_operator_grant("saas-user", OPERATOR_SEED_STATUS) is False
    assert store.get_user("saas-user") is not None

    assert store.delete_operator_grant("op-a", OPERATOR_SEED_STATUS) is True
    assert store.get_user("op-a") is None
    assert store.list_seed_conversations("op-a") == []
    assert store.delete_operator_grant("op-a", OPERATOR_SEED_STATUS) is False


def _unavailable_connect():
    raise __import__("sqlite3").OperationalError("database is locked")


def test_store_revocation_fails_closed_instead_of_reporting_nothing_to_revoke(monkeypatch):
    _grant("op-a")
    monkeypatch.setattr(store, "_connect", _unavailable_connect)

    with pytest.raises(store.StoreError):
        store.delete_operator_grants(OPERATOR_SEED_STATUS)
    with pytest.raises(store.StoreError):
        store.delete_operator_grant("op-a", OPERATOR_SEED_STATUS)
