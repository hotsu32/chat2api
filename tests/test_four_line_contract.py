"""Cross-line product contracts, independent of worker implementation details."""

from utils import globals as state, store
from chatgpt import authorization
from utils import tiers
import pytest
from fastapi import HTTPException


def test_public_tiers_cannot_expand_to_another_account_tier(monkeypatch):
    monkeypatch.setattr(tiers, "_catalog", {
        "plus": {"account_plan_types": ["free", "plus", "pro"]},
        "pro": {"account_plan_types": ["plus", "pro"]},
    })
    assert tiers.tier_account_plan_types("plus") == ["plus"]
    assert tiers.tier_account_plan_types("pro") == ["pro"]


def test_public_tier_misconfiguration_does_not_grant_another_pool(monkeypatch):
    monkeypatch.setattr(tiers, "_catalog", {"pro": {"account_plan_types": ["plus"]}})
    assert tiers.tier_account_plan_types("pro") == []


def test_requested_pro_pool_never_falls_back_to_plus(db, monkeypatch):
    monkeypatch.setattr(state, "error_token_list", [])
    monkeypatch.setattr(state, "antiban_dead_tokens", {})
    store.upsert_account("test-plus-only", plan_type="plus", status="healthy")
    selected = authorization._pick_healthy_account(plan_types=["pro"])
    assert not selected, "An empty Pro pool must not select a Plus account"


def test_unexpected_entitlement_failure_cannot_become_operator_access(monkeypatch):
    from utils import entitlements

    def unavailable(_seed):
        raise ValueError("malformed entitlement state")

    monkeypatch.setattr(entitlements, "effective_tier", unavailable)
    with pytest.raises(HTTPException) as failure:
        tiers.enforce_tier("registered-user")
    assert failure.value.status_code == 503
