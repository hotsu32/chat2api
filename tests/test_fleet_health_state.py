"""Health sweeps must not undo concurrent operator decisions (real SQLite)."""
import pytest

from utils import fleet_health, globals, store


@pytest.mark.parametrize("decision", ["disabled", "dead", "degraded", "deleted"])
async def test_sweep_preserves_decision_during_probe(db, monkeypatch, decision):
    store.upsert_account("synthetic-account", status="healthy")
    monkeypatch.setattr(globals, "token_list", ["synthetic-account"])
    monkeypatch.setattr(fleet_health, "_PACING_SECONDS", 0)

    async def probe(token):
        if decision == "deleted":
            store.delete_account(token)
        else:
            store.upsert_account(token, status=decision)
        return "healthy"

    monkeypatch.setattr(fleet_health, "check_account", probe)
    summary = await fleet_health.check_all_accounts()
    actual = store.get_account("synthetic-account")
    assert actual is None if decision == "deleted" else actual["status"] == decision
    assert summary["healthy"] == 0


async def test_sweep_reports_storage_failure_and_continues(db, monkeypatch):
    for token in ("synthetic-a", "synthetic-b"):
        store.upsert_account(token, status="unhealthy")
    monkeypatch.setattr(globals, "token_list", ["synthetic-a", "synthetic-b"])
    monkeypatch.setattr(fleet_health, "_PACING_SECONDS", 0)
    original = store._connect

    async def probe(token):
        if token == "synthetic-a":
            def broken():
                monkeypatch.setattr(store, "_connect", original)
                raise OSError("synthetic secret connection detail")
            monkeypatch.setattr(store, "_connect", broken)
        return "healthy"

    monkeypatch.setattr(fleet_health, "check_account", probe)
    summary = await fleet_health.check_all_accounts()
    assert summary["errors"] == 1
    assert summary["healthy"] == 1
    assert store.get_account("synthetic-a")["status"] == "unhealthy"
    assert store.get_account("synthetic-b")["status"] == "healthy"


async def test_verified_dead_recovery_updates_sqlite_and_clears_legacy_marker(
        db, monkeypatch):
    token = "synthetic-dead-account"
    store.upsert_account(token, status="dead")
    monkeypatch.setattr(globals, "token_list", [token])
    globals.antiban_dead_tokens[token] = {"reason": "account_deactivated", "dead_at": 1}
    monkeypatch.setattr(fleet_health, "_PACING_SECONDS", 0)

    async def recovered(_token):
        return "healthy"

    monkeypatch.setattr(fleet_health, "check_account", recovered)
    summary = await fleet_health.check_all_accounts()

    assert summary["healthy"] == 1
    assert store.get_account(token)["status"] == "healthy"
    assert token not in globals.antiban_dead_tokens
