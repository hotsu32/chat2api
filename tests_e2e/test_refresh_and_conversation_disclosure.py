"""Two trust boundaries that were open on the frozen integration.

R1  POST /auth/refresh had no authorization dependency at all.  Any anonymous
    caller could post *any* string as ``access_token``; the value reached the
    pool allocator (``get_real_req_token`` -> ``_resolve_seed_account``), which
    bound a healthy pool account to that string and answered with the account's
    bearer token plus its raw upstream ``accounts_info`` (holder e-mail / name /
    phone / picture).  The route now takes the same operator authorization as
    every other endpoint in ``gateway/share.py``, and only a value that already
    *is* an upstream credential may reach the upstream exchange -- a mirror Seed
    or arbitrary junk is never a routing input here.

R2  GET /backend-api/conversations serialized the locally stored conversation
    entries verbatim, including the internal ``account`` field, which is the
    pool account token itself.  The list response is now a whitelist projection
    of the fields the frontend consumes, so an internal field can never ride
    out again.

Both are gateway disclosure facts; neither says anything about the upstream
product.  Every assertion below is deterministic and offline.
"""

import json

import pytest

import utils.globals as globals
from gateway import share as share_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OPERATOR_KEY = "operator-api-key"


@pytest.fixture
def operator_auth(monkeypatch):
    """Authorize exactly one operator key for the duration of a test.

    ``gateway.share`` imported the *list object* at module load, so the name has
    to be patched on that module (rebinding ``configs.authorization_list`` would
    leave the already-imported reference pointing at the old list).
    """
    monkeypatch.setattr(share_mod, "authorization_list", [OPERATOR_KEY])
    return {"Authorization": f"Bearer {OPERATOR_KEY}"}


def _post_refresh(client, data, headers=None, cookies=None):
    return client.post("/auth/refresh", data=data, headers=headers or {}, cookies=cookies or {})


# ---------------------------------------------------------------------------
# R1 - /auth/refresh trust boundary
# ---------------------------------------------------------------------------

def test_anonymous_refresh_is_rejected_and_touches_nothing(client, mock_upstream, seed_account,
                                                           make_access_token):
    """No operator credential, no work.

    Asserted at the strongest available layer: not just the status code, but
    that no pool account was bound and that nothing left the process.
    """
    pool_token = make_access_token(account_id="acc-pool")
    seed_account(pool_token, plan_type="plus", status="healthy")

    resp = _post_refresh(client, {"access_token": "attacker-supplied-value"})

    assert resp.status_code in (401, 403)
    assert pool_token not in resp.text
    assert globals.seed_map == {}
    assert mock_upstream.records == []


def test_wrong_operator_key_is_rejected(client, seed_account, make_access_token):
    seed_account(make_access_token(account_id="acc-pool"), plan_type="plus")

    resp = _post_refresh(client, {"access_token": "attacker-supplied-value"},
                         headers={"Authorization": "Bearer not-the-operator-key"})

    assert resp.status_code == 401
    assert globals.seed_map == {}


def test_arbitrary_input_never_allocates_a_pool_account(client, mock_upstream, operator_auth,
                                                        seed_account, make_access_token):
    """The core of R1: an authorized caller must not be able to spend pool
    capacity, or receive a pool credential, by posting a non-credential string.

    Before the fix this end-to-end path bound the healthy pool account to the
    posted string (``seed_map['attacker-supplied-value']['token']``), sent that
    account's bearer token to the upstream, and returned it in the response body
    together with the holder's real e-mail address.
    """
    pool_token = make_access_token(account_id="acc-pool", email="owner@example.com")
    seed_account(pool_token, plan_type="plus", status="healthy")
    make_access_token(account_id="acc-pool")

    resp = _post_refresh(client, {"access_token": "attacker-supplied-value"}, headers=operator_auth)

    assert resp.status_code == 401
    # No binding was invented for the posted string...
    assert "attacker-supplied-value" not in globals.seed_map
    assert globals.seed_map == {}
    # ...no pool credential was handed out...
    assert pool_token not in resp.text
    assert "owner@example.com" not in resp.text
    # ...and the pool account never went on the wire.
    assert mock_upstream.records == []


def test_arbitrary_seed_like_input_is_not_a_routing_input(client, mock_upstream, operator_auth,
                                                          seed_account, make_access_token):
    """A value that could be mistaken for a mirror Seed must be refused outright.

    The allocator is reachable from this route only through a string that is not
    a credential; the guard has to be a property of the *input shape*, not of
    whether the string happens to resolve to something.
    """
    seed_account(make_access_token(account_id="acc-pool"), plan_type="plus", status="healthy")

    resp = _post_refresh(
        client,
        {"access_token": "seed-looking-value", "refresh_token": ""},
        headers=operator_auth,
    )

    assert resp.status_code == 401
    assert globals.seed_map == {}
    assert mock_upstream.records == []


def test_authorized_direct_credential_still_works_and_is_scrubbed(
        client, mock_upstream, operator_auth, make_access_token):
    """The legitimate operator path survives, and its account objects are
    anonymized with the same contract as /backend-api/accounts/check.

    This is the compatibility lock: an operator holding an upstream AccessToken
    still gets ``models`` + ``accounts_info`` + ``accountCheckInfo``; what it no
    longer gets is the pool holder's identity fields.
    """
    access_token = make_access_token(account_id="acc-real-1", email="owner@example.com",
                                     name="Owner Real")

    resp = _post_refresh(client, {"access_token": access_token}, headers=operator_auth)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["accessToken"] == access_token
    assert [m["slug"] for m in payload["models"]] == ["gpt-5-5", "gpt-4o"]
    assert "accounts_info" in payload
    assert payload["accountCheckInfo"] == {
        "is_deactivated": True, "plan_type": None, "team_ids": [],
    }

    account = payload["accounts_info"]["accounts"]["default"]["account"]
    # Capability metadata the frontend keys on is preserved...
    assert account["account_id"] == "acc-real-1"
    # ...while every holder-identifying field is replaced.
    assert account["email"] == ""
    assert account["account_email"] == ""
    assert account["name"] == "ChatGPT"
    assert account["account_name"] == "ChatGPT"
    assert account["phone_number"] == ""
    assert account["picture"] == ""
    assert account["account_user_id"] == "user-chatgpt__acc-real-1"

    for secret in ("owner@example.com", "Owner Real", "+15551234567",
                   "cdn.example.com/avatar.png", "user-owner"):
        assert secret not in resp.text
    # The legitimate exchange really did reach the upstream (not a silently
    # empty 200): the mock recorded the models + accounts/check pair.
    assert [r["path"].split("?")[0] for r in mock_upstream.records] == [
        "/backend-api/models",
        "/backend-api/accounts/check/v4-2023-04-27",
    ]


# ---------------------------------------------------------------------------
# R1 - the accepted input shape is pinned to the pool's own branch
# ---------------------------------------------------------------------------

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.sig"

# Shapes the pool's own branch does *not* pass through -- offering any of them as
# `access_token` would have reached the allocator before the fix.
_REJECTED_SHAPES = [
    "",
    "attacker-supplied-value",
    "seed-looking-value",
    "sess-" + "a" * 50,          # SessionToken
    "fk-" + "a" * 50,            # AccessToken variant the pool's branch does not cover
    "rt_" + "a" * 60,            # belongs to refresh_token, not access_token
    "a" * 44,
]


@pytest.mark.parametrize("shape", _REJECTED_SHAPES)
def test_no_non_credential_shape_is_ever_routed(client, mock_upstream, operator_auth,
                                                seed_account, make_access_token, shape):
    """Every non-credential shape ends at 401 with an untouched pool.

    Parametrized over the whole rejected set rather than one sample, because the
    defect was "any string works" -- a single-value guard would not have shown it.
    """
    seed_account(make_access_token(account_id="acc-pool"), plan_type="plus", status="healthy")

    resp = _post_refresh(client, {"access_token": shape}, headers=operator_auth)

    assert resp.status_code == 401
    assert globals.seed_map == {}
    assert mock_upstream.records == []


async def test_accepted_shape_matches_the_pool_allocator_branch(monkeypatch):
    """`/auth/refresh` may only accept what the pool already treats as a credential.

    The predicate is a deliberate copy of ``get_real_req_token``'s branch (that
    module is outside this change's blast radius), so it is pinned here: if the
    branch ever widens or narrows, this test fails instead of the route quietly
    re-opening the allocation hole.
    """
    from gateway import reverseProxy
    from gateway.backend import has_direct_access_token

    calls = []

    def spy(req_token, seed=None):
        calls.append((req_token, seed))
        if seed:
            raise AssertionError("pool allocator branch reached")
        return req_token

    monkeypatch.setattr(reverseProxy, "get_req_token", spy)

    # A legacy 45-char token and a modern JWT: both are passed through untouched.
    for accepted in (_JWT, "a" * 45):
        assert share_mod.is_direct_upstream_credential(accepted) is True
        assert has_direct_access_token(accepted) is True
        assert await reverseProxy.get_real_req_token(accepted) == accepted

    for rejected in _REJECTED_SHAPES:
        if len(rejected) == 45:
            continue        # length alone is the pool's own branch condition
        # Both the route guard and the pool's own pass-through predicate say no.
        assert share_mod.is_direct_upstream_credential(rejected) is False
        assert has_direct_access_token(rejected) is False

    # Only the two accepted values were ever looked at, and always without a Seed
    # argument -- i.e. the allocator branch was never a possible outcome.
    assert calls == [(_JWT, None), ("a" * 45, None)]


async def test_account_check_refuses_a_non_credential_from_any_leg(monkeypatch):
    """Defense in depth on the second leg.

    The OAuth exchange hands its result straight back into the account check; if
    that value is not credential-shaped, the check must refuse rather than fall
    back to allocating a pool account. Guarding the function (not just the route)
    is what makes "never allocate for arbitrary input" structural.
    """
    from gateway import reverseProxy

    calls = []

    def spy(req_token, seed=None):
        calls.append((req_token, seed))
        if seed:
            raise AssertionError("pool allocator branch reached")
        return req_token

    monkeypatch.setattr(reverseProxy, "get_req_token", spy)

    for value in ("not-a-credential", "sess-" + "a" * 50, ""):
        assert await share_mod.chatgpt_account_check(value) == {}

    assert calls == []


# ---------------------------------------------------------------------------
# R2 - conversation list non-disclosure
# ---------------------------------------------------------------------------

def _seed_conversation(seed, pool_token, **fields):
    entry = {
        "id": "conv-1",
        "title": "My Chat",
        "create_time": 1700000000,
        "update_time": 1700000100,
        "account": pool_token,
        "is_archived": False,
        "conversation_template_id": "tmpl-1",
        "gizmo_id": "gizmo-1",
        "async_status": None,
    }
    entry.update(fields)
    globals.seed_map[seed]["conversations"] = ["conv-1"]
    globals.conversation_map["conv-1"] = entry
    return entry


def test_conversation_list_never_returns_the_pool_account_token(
        client, seed_account, seed_user, make_access_token):
    """The list is the mirror user's own view; the account token that serves it
    is an internal routing fact and must not appear in the payload."""
    pool_token = make_access_token(account_id="acc-pool", email="owner@example.com")
    seed_account(pool_token, plan_type="plus")
    seed_user("seed-r2", pool_token, plan_type="plus")
    _seed_conversation("seed-r2", pool_token)

    resp = client.get("/backend-api/conversations", cookies={"token": "seed-r2"})

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["id"] for item in items] == ["conv-1"]
    assert pool_token not in resp.text
    assert "account" not in items[0]


def test_conversation_list_keeps_the_fields_the_frontend_consumes(
        client, seed_account, seed_user, make_access_token):
    """Dropping an internal field must not turn into dropping the list."""
    pool_token = make_access_token(account_id="acc-pool")
    seed_account(pool_token, plan_type="plus")
    seed_user("seed-r2b", pool_token, plan_type="plus")
    _seed_conversation("seed-r2b", pool_token)

    resp = client.get("/backend-api/conversations", cookies={"token": "seed-r2b"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["limit"] == 28 and body["offset"] == 0
    assert body["has_missing_conversations"] is False
    assert body["items"] == [{
        "id": "conv-1",
        "title": "My Chat",
        "create_time": 1700000000,
        "update_time": 1700000100,
        "is_archived": False,
        "conversation_template_id": "tmpl-1",
        "gizmo_id": "gizmo-1",
        "async_status": None,
    }]


def test_conversation_list_is_a_projection_not_a_dump(
        client, seed_account, seed_user, make_access_token):
    """Whitelist, not blacklist: a field nobody declared cannot ride out.

    ``PATCH /backend-api/conversation/{id}`` merges the client body straight
    into the stored entry, so "whatever happens to be in the map" is not a
    bounded set.
    """
    pool_token = make_access_token(account_id="acc-pool")
    seed_account(pool_token, plan_type="plus")
    seed_user("seed-r2c", pool_token, plan_type="plus")
    _seed_conversation("seed-r2c", pool_token,
                       proxy_url="socks5h://internal-proxy:1080",
                       session_token="sess-internal-secret",
                       refresh_token="rt_internal_secret")

    resp = client.get("/backend-api/conversations", cookies={"token": "seed-r2c"})

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert set(item) == {"id", "title", "create_time", "update_time", "is_archived",
                         "conversation_template_id", "gizmo_id", "async_status"}
    for leaked in ("socks5h://internal-proxy:1080", "sess-internal-secret", "rt_internal_secret"):
        assert leaked not in resp.text


def test_patch_injected_fields_cannot_ride_out_in_the_list(
        client, seed_account, seed_user, make_access_token):
    """The whitelist is exercised through the real injection path.

    ``PATCH /backend-api/conversation/{id}`` merges the request body into the
    stored entry, so a client can name any key it likes. The junk really does
    land in the store -- reading the list back is what must not return it.
    """
    pool_token = make_access_token(account_id="acc-pool")
    seed_account(pool_token, plan_type="plus")
    seed_user("seed-r2e", pool_token, plan_type="plus")
    _seed_conversation("seed-r2e", pool_token)

    injected = {"session_token": "sess-injected", "mapping": {"secret": "injected"},
                "proxy_url": "socks5h://injected:1080", "internal_note": "injected"}
    patched = client.patch(
        "/backend-api/conversation/conv-1",
        cookies={"token": "seed-r2e"},
        json={"is_visible": True, **injected},
    )
    assert patched.status_code == 200
    for key, value in injected.items():
        assert globals.conversation_map["conv-1"][key] == value

    resp = client.get("/backend-api/conversations", cookies={"token": "seed-r2e"})

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert set(item) == {"id", "title", "create_time", "update_time", "is_archived",
                         "conversation_template_id", "gizmo_id", "async_status"}
    assert "injected" not in resp.text
    assert pool_token not in resp.text


def test_conversation_list_still_filters_to_the_bound_account(
        client, seed_account, seed_user, make_access_token):
    """The account filter is the list's authorization decision; it reads the
    stored field server-side and must keep working after the projection."""
    tok_a = make_access_token(account_id="acc-a")
    tok_b = make_access_token(account_id="acc-b")
    seed_account(tok_a, plan_type="plus")
    seed_account(tok_b, plan_type="plus")
    seed_user("seed-r2d", tok_a, plan_type="plus", conversations=["conv-1", "conv-2"])
    globals.conversation_map["conv-1"] = {
        "id": "conv-1", "title": "A", "account": tok_a, "is_archived": False,
    }
    globals.conversation_map["conv-2"] = {
        "id": "conv-2", "title": "B", "account": tok_b, "is_archived": False,
    }

    resp = client.get("/backend-api/conversations", cookies={"token": "seed-r2d"})

    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()["items"]] == ["conv-1"]
    assert json.dumps(resp.json()).count(tok_b) == 0
