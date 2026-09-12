"""fleet core: tier pool, sticky routing, switch, verify_token (AccessToken path).

Operator Seeds (no ``user_auth`` row) are an explicit import path: the binding has
to exist server-side before it can be routed to. See
``tests/test_unknown_seed_authorization.py`` for the fail-closed contract itself.
"""
import pytest

import utils.globals as globals
import utils.store as store
from chatgpt import authorization as auth


@pytest.fixture(autouse=True)
def _reset_globals(db):
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.conversation_map.clear()
    globals.refresh_map.clear()
    globals.fp_map.clear()
    globals.routing_config.clear()
    globals.antiban_dead_tokens.clear()
    globals._plan_synced.clear()
    yield
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.conversation_map.clear()
    globals.refresh_map.clear()
    globals.fp_map.clear()
    globals.routing_config.clear()
    globals.antiban_dead_tokens.clear()
    globals._plan_synced.clear()


def _pick_max_token(seq):
    # Deterministic stand-in for random.choice over account-row dicts.
    return max(seq, key=lambda d: d["token"])


def test_account_is_usable(db):
    store.upsert_account("healthy-1", plan_type="plus", status="healthy")
    store.upsert_account("disabled-1", plan_type="plus", status="disabled")
    assert auth._account_is_usable("healthy-1") is True
    assert auth._account_is_usable("disabled-1") is False
    # A token with no account row is unusable (default-unusable: a dangling binding must fail over).
    assert auth._account_is_usable("missing") is False


def test_account_tier(db):
    store.upsert_account("pro-1", plan_type="pro", status="healthy")
    assert auth._account_tier("pro-1") == "pro"
    assert auth._account_tier("missing") is None


def test_pick_healthy_account(db, monkeypatch):
    store.upsert_account("plus-1", plan_type="plus", status="healthy")
    store.upsert_account("free-1", plan_type="free", status="healthy")
    monkeypatch.setattr(auth.random, "choice", _pick_max_token)
    assert auth._pick_healthy_account("plus") == "plus-1"
    assert auth._pick_healthy_account("free") == "free-1"
    # 显式档位是硬边界：该档号池空了就空手而归，不跨档借号（详见 test_pool_tier_contract.py）
    assert auth._pick_healthy_account("team") == ""
    # 无档位请求（运营者 / 直传 token 老链路）才回退：先 free 池，再任意健康号
    assert auth._pick_healthy_account() == "free-1"


def test_pick_healthy_account_empty(db):
    assert auth._pick_healthy_account("plus") == ""


def _import_operator_seed(seed, token, plan_type):
    """Mirror the operator import (``POST /seedtoken``): binding + grant marker."""
    globals.seed_map[seed] = {"token": token, "plan_type": plan_type, "conversations": []}
    globals.persist_seed(seed)
    store.upsert_user(seed, status=auth.OPERATOR_SEED_STATUS)


def test_resolve_seed_account_sticky(db):
    store.upsert_account("plus-1", plan_type="plus", status="healthy")
    _import_operator_seed("seed-a", "plus-1", "plus")
    assert auth._resolve_seed_account("seed-a") == "plus-1"


def test_resolve_seed_account_switch_on_disabled(db):
    store.upsert_account("plus-1", plan_type="plus", status="disabled")
    store.upsert_account("plus-2", plan_type="plus", status="healthy")
    _import_operator_seed("seed-a", "plus-1", "plus")
    assert auth._resolve_seed_account("seed-a") == "plus-2"
    assert globals.seed_map["seed-a"]["token"] == "plus-2"


def test_resolve_seed_account_requires_a_persisted_operator_grant(db, monkeypatch):
    """A seed nobody imported is not an operator: it gets no account at all."""
    store.upsert_account("plus-1", plan_type="plus", status="healthy")
    monkeypatch.setattr(auth.random, "choice", _pick_max_token)
    assert auth._resolve_seed_account("seed-new") == ""
    assert "seed-new" not in globals.seed_map

    # Once the operator import persists the binding, the declared tier routes it.
    _import_operator_seed("seed-new", "", "plus")
    assert auth._resolve_seed_account("seed-new") == "plus-1"
    assert globals.seed_map["seed-new"]["plan_type"] == "plus"


def test_switch_seed_account(db, monkeypatch):
    store.upsert_account("plus-1", plan_type="plus", status="healthy")
    store.upsert_account("plus-2", plan_type="plus", status="healthy")
    _import_operator_seed("seed-a", "plus-1", "plus")
    monkeypatch.setattr(auth.random, "choice", _pick_max_token)
    assert auth.switch_seed_account("seed-a") == "plus-2"
    assert globals.seed_map["seed-a"]["token"] == "plus-2"


def test_switch_seed_account_requires_a_persisted_operator_grant(db):
    store.upsert_account("plus-1", plan_type="plus", status="healthy")
    globals.seed_map["seed-new"] = {"token": "", "plan_type": "plus", "conversations": []}
    assert auth.switch_seed_account("seed-new") == ""
    assert globals.seed_map["seed-new"]["token"] == ""


@pytest.mark.asyncio
async def test_verify_token_access(db, make_access_token):
    token = make_access_token(plan_type="plus", account_id="acc-9")
    result = await auth.verify_token(token)
    assert result == token
    # plan_type lazily synced to accounts on the access-token path
    assert store.get_account(token)["plan_type"] == "plus"


@pytest.mark.asyncio
async def test_verify_token_empty(db):
    # AUTHORIZATION env unset -> empty authorization_list -> None (not a 401)
    assert await auth.verify_token("") is None
