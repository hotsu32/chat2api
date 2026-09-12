"""Public-path P0: an unknown cookie must not reach the account fleet.

Companion to the unit contract in ``tests/test_unknown_seed_authorization.py``.
This file walks the real HTTP surface (FastAPI app + loopback mock upstream, no
real network, no real credentials) because that is where the defect was observed:

    POST /backend-api/conversation with any unknown ``token`` cookie

previously resolved an arbitrary fleet account (falling back from the Free pool to
any healthy Plus/Pro account), persisted the binding, and answered 200. The same
surface must now fail closed while the legitimate registered/trial/import paths
keep serving traffic.
"""

import time

import utils.configs as configs
import utils.globals as globals
import utils.plans as plans
import utils.store as store
from chatgpt.authorization import OPERATOR_SEED_STATUS

UNKNOWN = "unknown-cookie-7c31ba"


def _account(seed_account, token, plan_type):
    return seed_account(token, plan_type=plan_type)


def _registered(seed, email, trial=True):
    if trial:
        store.create_user_with_trial(email, password_hash="pbkdf2_sha256$1$00$00", seed=seed,
                                     status="active", trial_tier="plus", trial_total=3)
    else:
        store.upsert_user_auth(email, password_hash="pbkdf2_sha256$1$00$00", seed=seed,
                               status="active")
        store.upsert_user(seed, status="active")
    return seed


def _grant(email, plan_id, days_left=30):
    detail = plans.plan_detail(plan_id)
    order_id = "ord-e2e-" + plan_id + "-" + email
    store.create_order(order_id, email, detail["id"], str(detail["price"]), status="pending")
    store.activate_order(order_id, int(time.time()) + days_left * 86400)
    return order_id


def _converse(client, seed, model="gpt-5-5"):
    return client.post("/backend-api/conversation", cookies={"token": seed}, json={"model": model})


def test_unknown_cookie_cannot_reach_the_fleet(client, seed_account, make_access_token):
    """The reproduced defect, end to end: anonymous cookie in, paid account out."""
    _account(seed_account, make_access_token(account_id="acc-paid", plan_type="plus"), "plus")

    response = _converse(client, UNKNOWN)

    assert response.status_code == 401
    assert UNKNOWN not in globals.seed_map
    assert store.get_user(UNKNOWN) is None
    # Nothing was written on behalf of the anonymous caller.
    assert store.list_users() == []


def test_unknown_cookie_leaves_the_fleet_unbound_for_real_users(
        client, seed_account, make_access_token):
    """Denial must not consume or re-point the capacity a paying user needs."""
    token = _account(seed_account, make_access_token(account_id="acc-paid", plan_type="plus"), "plus")
    seed = _registered("seed-paid", "paid@example.com", trial=False)
    _grant("paid@example.com", "plus-solo-1m")

    assert _converse(client, UNKNOWN).status_code == 401
    assert _converse(client, seed).status_code == 200
    assert globals.seed_map[seed]["token"] == token


def test_registered_plus_trial_still_reaches_the_upstream(client, seed_account, make_access_token):
    token = _account(seed_account, make_access_token(account_id="acc-trial", plan_type="plus"), "plus")
    seed = _registered("seed-trial", "trial@example.com")

    response = _converse(client, seed)

    assert response.status_code == 200
    assert "[DONE]" in response.text
    assert globals.seed_map[seed]["token"] == token


def test_imported_operator_seed_still_reaches_the_upstream(client, seed_account, make_access_token):
    token = _account(seed_account, make_access_token(account_id="acc-op", plan_type="plus"), "plus")
    globals.seed_map["operator-seed"] = {"token": token, "plan_type": "plus", "conversations": []}
    globals.persist_seed("operator-seed")
    store.upsert_user("operator-seed", status=OPERATOR_SEED_STATUS)

    response = _converse(client, "operator-seed")

    assert response.status_code == 200


def test_legacy_binding_needs_reimport_and_delete_revokes(
        client, seed_account, make_access_token, monkeypatch):
    """Upgrade regression on the real HTTP surface.

    A binding that predates the fix (row + seed_map entry, no explicit grant) is
    denied; the authenticated operator import re-grants it; deleting it revokes it
    again. Nothing here may silently grandfather the ambiguous legacy row.
    """
    from gateway import share
    monkeypatch.setattr(share, "authorization_list", ["operator-key"])
    headers = {"Authorization": "Bearer operator-key"}
    token = _account(seed_account, make_access_token(account_id="acc-legacy", plan_type="plus"), "plus")

    # Legacy state: persisted by the pre-fix allocation path, never imported.
    globals.seed_map["legacy-seed"] = {"token": token, "plan_type": "plus", "conversations": []}
    store.upsert_user("legacy-seed", current_account=token, plan_type="plus")
    assert _converse(client, "legacy-seed").status_code == 401

    # Authenticated import re-grants it.
    assert client.post("/seedtoken", json={"seed": "legacy-seed", "token": token},
                       headers=headers).status_code == 200
    assert store.get_user("legacy-seed")["status"] == OPERATOR_SEED_STATUS
    assert _converse(client, "legacy-seed").status_code == 200

    # Deletion revokes it.
    assert client.request("DELETE", "/seedtoken", json={"seed": "legacy-seed"},
                          headers=headers).status_code == 200
    assert store.get_user("legacy-seed") is None
    assert _converse(client, "legacy-seed").status_code == 401


def test_operator_import_requires_the_authorization_key(
        client, seed_account, make_access_token, monkeypatch):
    from gateway import share
    monkeypatch.setattr(share, "authorization_list", ["operator-key"])
    token = _account(seed_account, make_access_token(account_id="acc-op2", plan_type="plus"), "plus")

    denied = client.post("/seedtoken", json={"seed": "unauthenticated-seed", "token": token},
                         headers={"Authorization": "Bearer wrong-key"})
    missing = client.post("/seedtoken", json={"seed": "unauthenticated-seed", "token": token})

    assert denied.status_code == 401
    assert missing.status_code in (401, 403)  # HTTPBearer rejects a missing header first
    assert store.get_user("unauthenticated-seed") is None
    assert "unauthenticated-seed" not in globals.seed_map


def test_stale_public_alias_is_inert_while_the_development_gate_is_shut(
        client, seed_account, make_access_token, monkeypatch):
    """A public, guessable alias name must not be a key: with the gate shut even a
    binding left behind by an earlier development run stays denied."""
    monkeypatch.setattr(configs, "dev_access_enabled", False)
    _account(seed_account, make_access_token(account_id="acc-free", plan_type="free"), "free")
    plus = _account(seed_account, make_access_token(account_id="acc-plus", plan_type="plus"), "plus")

    response = _converse(client, "frontend-proof-plus-2")

    assert response.status_code == 404
    assert "frontend-proof-plus-2" not in globals.seed_map
    assert store.get_user("frontend-proof-plus-2") is None
    assert store.get_account(plus)["plan_type"] == "plus"  # pool untouched by the attempt
