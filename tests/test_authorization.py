"""fleet core: tier pool, sticky routing, switch, verify_token (AccessToken path)."""
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
    # A token with no account row is not *explicitly* unusable (default-usable contract).
    assert auth._account_is_usable("missing") is True


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
    # tier pool empty -> fallback to any healthy account
    assert auth._pick_healthy_account("team") in {"plus-1", "free-1"}


def test_pick_healthy_account_empty(db):
    assert auth._pick_healthy_account("plus") == ""


def test_resolve_seed_account_sticky(db):
    store.upsert_account("plus-1", plan_type="plus", status="healthy")
    globals.seed_map["seed-a"] = {"token": "plus-1", "plan_type": "plus", "conversations": []}
    assert auth._resolve_seed_account("seed-a") == "plus-1"


def test_resolve_seed_account_switch_on_disabled(db):
    store.upsert_account("plus-1", plan_type="plus", status="disabled")
    store.upsert_account("plus-2", plan_type="plus", status="healthy")
    globals.seed_map["seed-a"] = {"token": "plus-1", "plan_type": "plus", "conversations": []}
    assert auth._resolve_seed_account("seed-a") == "plus-2"
    assert globals.seed_map["seed-a"]["token"] == "plus-2"


def test_resolve_seed_account_new_user(db, monkeypatch):
    store.upsert_account("plus-1", plan_type="plus", status="healthy")
    monkeypatch.setattr(auth.random, "choice", _pick_max_token)
    token = auth._resolve_seed_account("seed-new")
    assert token == "plus-1"
    assert globals.seed_map["seed-new"]["token"] == "plus-1"


def test_switch_seed_account(db, monkeypatch):
    store.upsert_account("plus-1", plan_type="plus", status="healthy")
    store.upsert_account("plus-2", plan_type="plus", status="healthy")
    globals.seed_map["seed-a"] = {"token": "plus-1", "plan_type": "plus", "conversations": []}
    monkeypatch.setattr(auth.random, "choice", _pick_max_token)
    assert auth.switch_seed_account("seed-a") == "plus-2"
    assert globals.seed_map["seed-a"]["token"] == "plus-2"


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
