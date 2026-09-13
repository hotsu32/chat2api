"""The admin panel must project the one canonical account state machine.

``fleet_health.resolve_account_status`` folds every source that can restrict an
account (manual disable, circuit/persisted dead, degraded, the error list, an
unproven row) into healthy / degraded / unhealthy / dead / disabled. The
dashboard used to run its own three-state mapping that consulted only
``accounts.status`` and the in-memory error list, so a circuit-dead account --
whose row ``mark_dead`` writes as ``status='dead'`` and whose ``antiban_status``
already reads "dead" -- was rendered as 正常 (green, healthy) in the operator
view. An operator looking at that panel would see an account as available that
``seed_lifecycle`` refuses to route to.

These tests are the regression gate: no restricted state may be projected as
healthy, and the machine-readable status must be part of the payload.
"""
import pytest

from utils import fleet_health, globals, store
from utils import routing


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
    yield
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.conversation_map.clear()
    globals.refresh_map.clear()
    globals.fp_map.clear()
    globals.routing_config.clear()
    globals.antiban_dead_tokens.clear()


def _row(token):
    for row in routing.get_dashboard_payload()["accounts"]:
        if row["token"] == token:
            return row
    raise AssertionError("account missing from the dashboard payload")


def _register(token, **fields):
    store.upsert_account(token, **fields)
    globals.token_list.append(token)


# ---------------------------------------------------------------------------
# Per-account projection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("persisted,expected", [
    ("disabled", fleet_health.STATUS_DISABLED),
    ("dead", fleet_health.STATUS_DEAD),
    ("degraded", fleet_health.STATUS_DEGRADED),
    ("unhealthy", fleet_health.STATUS_UNHEALTHY),
    ("healthy", fleet_health.STATUS_HEALTHY),
])
def test_persisted_status_is_projected_verbatim(persisted, expected):
    _register("acct", plan_type="plus", status=persisted)
    row = _row("acct")
    assert row["account_status"] == expected
    assert row["status_label"] == fleet_health.status_label(expected)


@pytest.mark.parametrize("persisted", ["dead", "degraded", "unhealthy", "disabled"])
def test_no_restricted_state_is_reported_as_the_panel_healthy_label(persisted):
    _register("acct", plan_type="plus", status=persisted)
    assert _row("acct")["status"] != "正常"


def test_circuit_dead_marker_outranks_a_stale_healthy_row():
    """``mark_dead`` writes both the marker and the row; either one alone restricts."""
    _register("acct", plan_type="plus", status="healthy")
    globals.antiban_dead_tokens["acct"] = {"reason": "account_deactivated", "dead_at": 1}
    row = _row("acct")
    assert row["account_status"] == fleet_health.STATUS_DEAD
    assert row["status"] != "正常"


def test_error_list_membership_restricts_a_healthy_row():
    _register("acct", plan_type="plus", status="healthy")
    globals.error_token_list.append("acct")
    row = _row("acct")
    assert row["account_status"] == fleet_health.STATUS_UNHEALTHY
    assert row["status"] != "正常"


def test_unproven_account_row_is_not_projected_as_healthy():
    """No row is not evidence of health: the panel must not invent one."""
    globals.token_list.append("ghost")
    row = _row("ghost")
    assert row["account_status"] == fleet_health.STATUS_UNHEALTHY
    assert row["status"] != "正常"


def test_disabled_row_keeps_the_dedicated_disabled_label():
    _register("acct", plan_type="plus", status="disabled")
    assert _row("acct")["status"] == "停用"


def test_healthy_row_is_still_projected_as_healthy():
    _register("acct", plan_type="plus", status="healthy")
    row = _row("acct")
    assert row["status"] == "正常"
    assert row["account_status"] == fleet_health.STATUS_HEALTHY


def test_panel_label_agrees_with_antiban_view_for_a_dead_account():
    """The row carried two contradicting views: status=正常 while antiban said dead."""
    _register("acct", plan_type="plus", status="healthy")
    globals.antiban_dead_tokens["acct"] = {"reason": "account_deactivated", "dead_at": 1}
    row = _row("acct")
    assert row["antiban_status"] == "dead"
    assert row["status"] == "异常"


# ---------------------------------------------------------------------------
# Aggregate projection
# ---------------------------------------------------------------------------

def test_summary_counts_canonical_health_not_just_the_error_list():
    _register("ok", plan_type="plus", status="healthy")
    _register("dead", plan_type="plus", status="dead")
    _register("disabled", plan_type="plus", status="disabled")
    summary = routing.get_dashboard_payload()["summary"]
    assert summary["accounts_total"] == 3
    assert summary["accounts_ok"] == 1
    assert summary["accounts_bad"] == 1
    assert summary["accounts_disabled"] == 1


def test_summary_reports_a_circuit_dead_marker_as_impaired():
    _register("ok", plan_type="plus", status="healthy")
    _register("dead", plan_type="plus", status="healthy")
    globals.antiban_dead_tokens["dead"] = {"reason": "account_deactivated", "dead_at": 1}
    summary = routing.get_dashboard_payload()["summary"]
    assert summary["accounts_ok"] == 1
    assert summary["accounts_bad"] == 1


def test_alerts_are_raised_from_canonical_impairment():
    _register("ok", plan_type="plus", status="healthy")
    _register("dead", plan_type="plus", status="healthy")
    globals.antiban_dead_tokens["dead"] = {"reason": "account_deactivated", "dead_at": 1}
    alerts = " ".join(routing.get_dashboard_payload()["alerts"])
    assert "异常账号" in alerts


def test_proxy_card_counts_use_canonical_health():
    _register("ok", plan_type="plus", status="healthy")
    _register("dead", plan_type="plus", status="dead")
    routing.save_routing_config({
        "proxies": [{"id": "proxy-1", "name": "ip1", "proxy_url": "socks5h://node.invalid:1080"}],
        "groups": [],
        "bindings": {
            "ok": {"group": "Group A", "proxy_name": "ip1",
                   "proxy_url": "socks5h://node.invalid:1080"},
            "dead": {"group": "Group A", "proxy_name": "ip1",
                     "proxy_url": "socks5h://node.invalid:1080"},
        },
        "account_meta": {},
    })
    card = routing.get_dashboard_payload()["ip_cards"][0]
    assert card["accounts"] == 2
    assert card["ok"] == 1
    assert card["bad"] == 1
