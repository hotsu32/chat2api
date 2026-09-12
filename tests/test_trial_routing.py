"""Tier trial aliases must never reuse a healthy account from another tier."""
from types import SimpleNamespace


def test_core_trial_bindings_repair_stale_free_aliases(monkeypatch):
    import gateway.landing as landing
    import utils.globals as g

    g.seed_map.clear()
    g.seed_map.update({
        "frontend-proof-free-1": {"token": "free", "plan_type": "free", "conversations": []},
        "frontend-proof-plus-2": {"token": "free", "plan_type": "free", "conversations": []},
        "frontend-proof-plus-3": {"token": "free", "plan_type": "free", "conversations": []},
        "frontend-proof-pro-1": {"token": "pro", "plan_type": "pro", "conversations": []},
    })
    rows = {
        "free": [{"token": "free", "plan_type": "free"}],
        "plus": [{"token": "plus-a", "plan_type": "plus"}, {"token": "plus-b", "plan_type": "plus"}],
        "pro": [{"token": "pro", "plan_type": "pro"}],
    }
    monkeypatch.setattr(landing.store, "get_account", lambda token: next((r for r in sum(rows.values(), []) if r["token"] == token), None))
    monkeypatch.setattr(landing.store, "get_account_by_plan", lambda tier, status="healthy": rows[tier])
    monkeypatch.setattr(landing.globals, "persist_seed_map", lambda: None)
    landing.ensure_core_trial_bindings([
        ("Free", "frontend-proof-free-1", "free"),
        ("Plus 一", "frontend-proof-plus-2", "plus"),
        ("Plus 二", "frontend-proof-plus-3", "plus"),
        ("Pro 一", "frontend-proof-pro-1", "pro"),
    ])
    assert g.seed_map["frontend-proof-plus-2"]["token"] == "plus-a"
    assert g.seed_map["frontend-proof-plus-3"]["token"] == "plus-b"
    assert g.seed_map["frontend-proof-free-1"]["token"] == "free"
    assert g.seed_map["frontend-proof-pro-1"]["token"] == "pro"
