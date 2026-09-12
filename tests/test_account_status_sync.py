"""Credential inventory updates are not evidence of health recovery."""
import pytest

from utils import globals, store


@pytest.mark.parametrize("sync", ["persist_token_list", "persist_error_tokens"])
@pytest.mark.parametrize("status", ["disabled", "dead", "degraded", "unhealthy"])
@pytest.mark.parametrize("errored", [False, True])
def test_inventory_sync_preserves_restrictions(db, monkeypatch, sync, status, errored):
    token = "synthetic-account"
    store.upsert_account(token, status=status, proxy_url="http://proxy.test", plan_type="plus")
    monkeypatch.setattr(globals, "token_list", [token])
    monkeypatch.setattr(globals, "error_token_list", [token] if errored else [])
    monkeypatch.setattr(globals, "antiban_dead_tokens", {})
    getattr(globals, sync)()
    actual = store.get_account(token)
    assert actual["status"] == status
    assert actual["proxy_url"] == "http://proxy.test"
    assert actual["plan_type"] == "plus"


def test_inventory_sync_never_implicitly_deletes_and_still_imports(db, monkeypatch):
    store.upsert_account("removed", status="disabled")
    monkeypatch.setattr(globals, "token_list", ["new"])
    monkeypatch.setattr(globals, "error_token_list", ["failed"])
    monkeypatch.setattr(globals, "antiban_dead_tokens", {})
    globals.persist_token_list()
    assert store.get_account("removed")["status"] == "disabled"
    assert store.get_account("new")["status"] == "healthy"
    assert store.get_account("failed")["status"] == "unhealthy"

    store.delete_account("removed")
    assert store.get_account("removed") is None


def test_error_signal_restricts_healthy_account(db, monkeypatch):
    store.upsert_account("failed", status="healthy")
    monkeypatch.setattr(globals, "token_list", ["failed"])
    monkeypatch.setattr(globals, "error_token_list", ["failed"])
    monkeypatch.setattr(globals, "antiban_dead_tokens", {})
    globals.persist_error_tokens()
    assert store.get_account("failed")["status"] == "unhealthy"
