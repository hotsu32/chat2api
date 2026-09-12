"""P0: an unknown Seed must never reach the account fleet.

Reproduced defect (production defaults, ``AUTO_SEED=true``): an arbitrary cookie
string with no ``user_auth`` row reached ``_resolve_seed_account`` ->
``_pick_healthy_account``, which fell back from the Free pool to *any* healthy
account, then persisted that binding into ``seed_map``/``users``. ``enforce_tier``
returned early for the same "no row = operator" reason and ``trials.reserve``
returned ``None``, so nothing else charged the caller. Net effect: anonymous,
unlimited, persisted access to the paid Plus/Pro pool.

Contract under test: allocation (and reuse of a binding) requires a server-side
authorized state, in exactly one of these forms:

  1. ``user_auth`` row -> paid entitlement or Plus trial with remaining allowance
     (``utils.entitlements`` / ``utils.trials`` stay the truth source);
  2. an explicit operator/import grant persisted server-side (the ``POST
     /seedtoken`` import path, the one-shot ``seed_map.json`` migration, or a
     previously authorized allocation), read from SQLite -- not from memory;
  3. a public development alias, and only while ``DEV_ACCESS_ENABLED`` is on: the
     alias name is public and guessable, so the name alone is never authorization
     and a binding left behind by an earlier development run must fail closed.

Everything else fails closed: no account, no ``seed_map`` entry, no ``users`` row.
"""

import time

import pytest
from fastapi import HTTPException

import utils.configs as configs
import utils.globals as globals
import utils.plans as plans
import utils.store as store
from chatgpt import authorization as auth

_PW = "pbkdf2_sha256$1$00$00"

# An arbitrary client-chosen string; never imported, registered or migrated.
UNKNOWN = "unknown-cookie-2f8a1c"
PUBLIC_ALIAS = "frontend-proof-plus-2"


@pytest.fixture(autouse=True)
def _reset(db, monkeypatch):
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.antiban_dead_tokens.clear()
    globals._plan_synced.clear()
    monkeypatch.setattr(configs, "max_shared_seeds_per_account", 2)
    yield
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.antiban_dead_tokens.clear()
    globals._plan_synced.clear()


def _account(token, plan_type, status="healthy"):
    store.upsert_account(token, plan_type=plan_type, status=status)
    return token


def _registered(seed, email, trial=True):
    """Registered SaaS user: ``user_auth`` row + users row (+ registration trial)."""
    if trial:
        store.create_user_with_trial(email, password_hash=_PW, seed=seed,
                                     status="active", trial_tier="plus", trial_total=3)
    else:
        store.upsert_user_auth(email, password_hash=_PW, seed=seed, status="active")
        store.upsert_user(seed, status="active")
    return seed


def _paid(email, plan_id, days_left=30):
    detail = plans.plan_detail(plan_id)
    order_id = f"ord-{email}-{plan_id}"
    store.create_order(order_id, email, detail["id"], str(detail["price"]), status="pending")
    store.activate_order(order_id, int(time.time()) + days_left * 86400)
    return order_id


def _operator_import(seed, token, tier):
    """Mirror ``POST /seedtoken`` (AUTHORIZATION-gated): binding + explicit grant marker.

    Like the endpoint, an existing binding keeps its declared tier, conversation
    history and status; only the token is rewritten. The marker is what authorizes
    a seed that has no ``user_auth`` row — a legacy ``users`` row written by the
    pre-fix anonymous allocation path is not enough.
    """
    entry = globals.seed_map.get(seed)
    if isinstance(entry, dict):
        entry["token"] = token
    else:
        globals.seed_map[seed] = {"token": token, "plan_type": tier, "conversations": []}
    globals.persist_seed(seed)
    store.upsert_user(seed, status=auth.OPERATOR_SEED_STATUS)
    return seed


def _pick_max(seq):
    return max(seq, key=lambda d: d["token"])


# --------------------------------------------------------------- 1 unknown seeds

def test_unknown_seed_cannot_take_a_paid_account(monkeypatch):
    """The sharpest form of the defect: with no Free account in the fleet, the old
    fallback handed an anonymous caller a Plus/Pro account."""
    monkeypatch.setattr(configs, "auto_seed", True)
    _account("plus-1", "plus")
    _account("pro-1", "pro")
    assert auth._resolve_seed_account(UNKNOWN) == ""
    assert UNKNOWN not in globals.seed_map
    assert store.get_user(UNKNOWN) is None


def test_unknown_seed_gets_no_account_through_the_public_path(monkeypatch):
    monkeypatch.setattr(configs, "auto_seed", True)
    _account("plus-1", "plus")
    assert auth.get_req_token(UNKNOWN, seed=UNKNOWN) == ""
    assert UNKNOWN not in globals.seed_map


def test_unknown_seeds_never_grow_the_map_or_the_users_table(monkeypatch):
    monkeypatch.setattr(configs, "auto_seed", True)
    _account("free-1", "free")
    _account("plus-1", "plus")
    before_map = dict(globals.seed_map)
    for i in range(5):
        assert auth._resolve_seed_account(f"unknown-{i}") == ""
        assert auth.get_req_token(f"unknown-{i}", seed=f"unknown-{i}") == ""
    assert globals.seed_map == before_map
    assert store.list_users() == []


@pytest.mark.parametrize("auto_seed", [True, False])
def test_unknown_seed_is_denied_in_both_seed_modes(monkeypatch, auto_seed):
    monkeypatch.setattr(configs, "auto_seed", auto_seed)
    _account("plus-1", "plus")
    if auto_seed:
        assert auth.get_req_token(UNKNOWN, seed=UNKNOWN) == ""
    else:
        with pytest.raises(HTTPException) as failure:
            auth.get_req_token(UNKNOWN)
        assert failure.value.status_code == 401
    assert UNKNOWN not in globals.seed_map
    assert store.get_user(UNKNOWN) is None


def test_unknown_seed_cannot_use_switch_account_either(monkeypatch):
    """Switch is the other allocation entry point; it must share the contract."""
    monkeypatch.setattr(configs, "auto_seed", True)
    _account("plus-1", "plus")
    assert auth.switch_seed_account(UNKNOWN) == ""
    assert UNKNOWN not in globals.seed_map
    assert store.get_user(UNKNOWN) is None


def test_memory_only_binding_is_not_authorization(monkeypatch):
    """A ``seed_map`` entry that no durable grant backs is cache, not permission."""
    _account("plus-1", "plus")
    globals.seed_map["memory-only"] = {
        "token": "plus-1", "plan_type": "plus", "conversations": [],
    }
    assert auth._resolve_seed_account("memory-only") == ""
    assert auth.switch_seed_account("memory-only") == ""


# ------------------------------------------------- 2 public development aliases

def test_stale_public_alias_fails_closed_when_the_development_gate_is_shut(monkeypatch):
    """A binding an earlier dev run left behind must not be repaired into a fresh
    paid allocation just because someone typed the public alias name."""
    monkeypatch.setattr(configs, "auto_seed", True)
    monkeypatch.setattr(configs, "dev_access_enabled", False)
    _account("free-1", "free")
    _account("plus-1", "plus")
    globals.seed_map[PUBLIC_ALIAS] = {
        "token": "free-1", "plan_type": "plus", "conversations": ["old"],
    }
    store.upsert_user(PUBLIC_ALIAS, current_account="free-1", plan_type="plus")
    with pytest.raises(HTTPException) as exc:
        auth._resolve_seed_account(PUBLIC_ALIAS)
    assert exc.value.status_code == 404
    # Neither escalated to the Plus pool nor rewritten:
    assert globals.seed_map[PUBLIC_ALIAS]["token"] == "free-1"
    with pytest.raises(HTTPException) as exc:
        auth.get_req_token(PUBLIC_ALIAS, seed=PUBLIC_ALIAS)
    assert exc.value.status_code == 404
    assert store.get_user(PUBLIC_ALIAS)["current_account"] == "free-1"


def test_development_alias_still_allocates_while_the_gate_is_open(monkeypatch):
    """The explicit development switch keeps local acceptance working."""
    monkeypatch.setattr(configs, "auto_seed", True)
    monkeypatch.setattr(configs, "dev_access_enabled", True)
    _account("plus-1", "plus")
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    globals.seed_map[PUBLIC_ALIAS] = {"token": "", "plan_type": "plus", "conversations": []}
    store.upsert_user(PUBLIC_ALIAS, current_account="", plan_type="plus")
    assert auth._resolve_seed_account(PUBLIC_ALIAS) == "plus-1"


@pytest.mark.parametrize("handle,tier,account", [
    ("frontend-proof-pro-1", "pro", "pro-1"),
    ("demo-plus-pool", "plus", "plus-1"),
    ("demo-free-pool", "free", "free-1"),
])
def test_stale_public_handles_are_inert_without_the_gate(monkeypatch, handle, tier, account):
    """Every publicly named development handle (aliases and /demo pool seeds) that an
    earlier development run left persisted must be inert in production defaults."""
    monkeypatch.setattr(configs, "auto_seed", True)
    monkeypatch.setattr(configs, "dev_access_enabled", False)
    _account("free-1", "free")
    _account("plus-1", "plus")
    _account("pro-1", "pro")
    globals.seed_map[handle] = {"token": "", "plan_type": tier, "conversations": []}
    store.upsert_user(handle, current_account="", plan_type=tier)
    with pytest.raises(HTTPException) as exc:
        auth._resolve_seed_account(handle)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        auth.get_req_token(handle, seed=handle)
    assert exc.value.status_code == 404
    assert globals.seed_map[handle]["token"] == ""


@pytest.mark.parametrize("handle,tier,account", [
    ("demo-plus-pool", "plus", "plus-1"),
    ("demo-free-pool", "free", "free-1"),
])
def test_demo_handles_still_allocate_while_the_gate_is_open(monkeypatch, handle, tier, account):
    monkeypatch.setattr(configs, "auto_seed", True)
    monkeypatch.setattr(configs, "dev_access_enabled", True)
    _account("free-1", "free")
    _account("plus-1", "plus")
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    globals.seed_map[handle] = {"token": "", "plan_type": tier, "conversations": []}
    assert auth._resolve_seed_account(handle) == account


# ----------------------------------------------------------- 3 paid entitlement

def test_paid_plus_user_still_allocates_from_the_plus_pool(monkeypatch):
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    _account("free-1", "free")
    _account("plus-1", "plus")
    seed = _registered("seed-paid-plus", "paid-plus@example.com", trial=False)
    _paid("paid-plus@example.com", "plus-solo-1m")
    assert auth._resolve_seed_account(seed) == "plus-1"
    assert globals.seed_map[seed]["token"] == "plus-1"


def test_paid_pro_user_still_allocates_from_the_pro_pool(monkeypatch):
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    _account("plus-1", "plus")
    _account("pro-1", "pro")
    seed = _registered("seed-paid-pro", "paid-pro@example.com", trial=False)
    _paid("paid-pro@example.com", "pro-solo-1m")
    assert auth._resolve_seed_account(seed) == "pro-1"
    assert globals.seed_map[seed]["plan_type"] == "pro"


def test_paid_user_without_a_same_tier_account_still_fails_closed():
    _account("free-1", "free")
    seed = _registered("seed-paid-empty", "paid-empty@example.com", trial=False)
    _paid("paid-empty@example.com", "plus-solo-1m")
    assert auth._resolve_seed_account(seed) == ""
    assert seed not in globals.seed_map


# --------------------------------------------------------------- 4 Plus trial

def test_plus_trial_user_still_allocates_while_allowance_remains(monkeypatch):
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    _account("free-1", "free")
    _account("plus-1", "plus")
    seed = _registered("seed-trial", "trial@example.com")
    assert auth._resolve_seed_account(seed) == "plus-1"
    assert globals.seed_map[seed]["plan_type"] == "plus"
    # Trial Seeds are activated as ``trial`` (see seeds_lifecycle._entitlement).
    assert store.get_user(seed)["status"] == "trial"


def test_registered_user_without_entitlement_still_gets_nothing():
    _account("plus-1", "plus")
    store.upsert_user_auth("noent@example.com", password_hash=_PW, seed="seed-noent", status="active")
    store.upsert_user("seed-noent", status="active")
    assert auth._resolve_seed_account("seed-noent") == ""
    # Freezing publishes the (empty) binding; it must never carry an account.
    assert globals.seed_map.get("seed-noent", {}).get("token", "") == ""


# ------------------------------------------------- 5 explicit operator/import

def test_imported_operator_seed_still_allocates(monkeypatch):
    """``POST /seedtoken`` grants stay honored: durable row + declared tier."""
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    _account("plus-1", "plus")
    _account("pro-1", "pro")
    _operator_import("operator-seed", "plus-1", "plus")
    assert auth._resolve_seed_account("operator-seed") == "plus-1"


def test_imported_operator_seed_rebinds_inside_its_declared_tier(monkeypatch):
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    _account("plus-dead", "plus", status="disabled")
    _account("plus-live", "plus")
    _account("pro-1", "pro")
    _operator_import("operator-seed", "plus-dead", "plus")
    assert auth._resolve_seed_account("operator-seed") == "plus-live"
    assert globals.seed_map["operator-seed"]["plan_type"] == "plus"
    # The allocation write-back must not clobber the grant marker: the next request
    # has to stay authorized (``persist_seed`` only sends the fields the entry has).
    assert store.get_user("operator-seed")["status"] == auth.OPERATOR_SEED_STATUS
    assert auth.get_req_token("operator-seed", seed="operator-seed") == "plus-live"


def test_imported_operator_seed_survives_without_a_user_auth_row(monkeypatch):
    """The operator path has no ``user_auth`` row by design: ``enforce_tier`` keeps
    its documented fail-open behavior for it."""
    from utils.tiers import enforce_tier
    _account("plus-1", "plus")
    _operator_import("operator-seed", "plus-1", "plus")
    enforce_tier("operator-seed", "gpt-5-5")


def test_legacy_binding_is_not_authorization_and_reimport_regrants_it(monkeypatch):
    """Upgrade regression.

    A ``users`` row + ``seed_map`` binding created without an explicit grant (the
    pre-fix anonymous allocation path, or the one-shot ``seed_map.json``
    migration) must NOT authorize the seed: whoever held that string before the
    upgrade could otherwise keep paid fleet access forever. Re-importing through
    the authenticated operator path grants it again.
    """
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    _account("plus-1", "plus")
    legacy = {"token": "plus-1", "plan_type": "plus", "conversations": ["legacy-history"]}
    store.migrate([], [], {"legacy-operator": legacy}, {}, {}, {}, {})
    globals.seed_map.update({"legacy-operator": legacy})

    # Row exists, but nothing explicitly authorized it:
    assert store.get_user("legacy-operator") is not None
    assert auth._resolve_seed_account("legacy-operator") == ""
    assert auth.get_req_token("legacy-operator", seed="legacy-operator") == ""

    # The authenticated operator path re-grants it, idempotently.
    _operator_import("legacy-operator", "plus-1", "plus")
    assert auth._resolve_seed_account("legacy-operator") == "plus-1"
    assert globals.seed_map["legacy-operator"]["conversations"] == ["legacy-history"]
