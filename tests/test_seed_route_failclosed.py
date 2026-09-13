"""Fail-closed gates on Seed account selection, driven through real SQLite.

Each case is a transition that must be *refused*: the account is restricted, the
capacity is already consumed by active peers only, the tier does not match, or
the process restarted and lost its in-memory cache. A refusal must leave the
persisted binding and the published memory entry exactly as they were.
"""
import threading
import time

import pytest

from utils import globals, store
from utils.seed_lifecycle import LifecycleDenied, activate_seed, freeze_if_expired, route_seed


@pytest.fixture(autouse=True)
def isolated(db, monkeypatch):
    monkeypatch.setattr(globals, "seed_map", {})
    monkeypatch.setattr(globals, "error_token_list", [])
    monkeypatch.setattr(globals, "antiban_dead_tokens", {})


def user(seed, account="", tier="plus", density="shared", status="frozen"):
    email = f"{seed}@example.test"
    store.create_user_with_trial(email, password_hash="synthetic", seed=seed,
                                 status="active", trial_tier="plus", trial_total=3)
    store.upsert_user(seed, current_account=account, plan_type=tier, status=status)
    if density != "trial":
        store.create_order(f"order-{seed}", email, f"{tier}-{density}-1m", "1", status="pending")
        store.activate_order(f"order-{seed}", int(time.time()) + 86400)
    globals.seed_map[seed] = dict(token=account, plan_type=tier, status=status, conversations=[])


# ---------------------------------------------------------------------------
# A restricted account is never a candidate, whatever restricts it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("restriction", ["dead", "disabled", "degraded", "unhealthy"])
def test_restricted_account_is_refused_and_the_binding_is_untouched(restriction):
    store.upsert_account("sick", plan_type="plus", status=restriction)
    user("seed", "sick", status="active")
    with pytest.raises(LifecycleDenied) as denial:
        route_seed("seed", 2)
    assert denial.value.reason == "account_not_healthy"
    assert store.get_user("seed")["current_account"] == "sick"
    assert globals.seed_map["seed"]["token"] == "sick"


def test_circuit_dead_marker_restricts_a_healthy_row():
    """``mark_dead`` records the marker before the row write is known to land."""
    store.upsert_account("sick", plan_type="plus", status="healthy")
    globals.antiban_dead_tokens["sick"] = {"reason": "account_deactivated", "dead_at": 1}
    user("seed", "sick", status="active")
    with pytest.raises(LifecycleDenied) as denial:
        route_seed("seed", 2)
    assert denial.value.reason == "account_not_healthy"


def test_error_list_membership_restricts_a_healthy_row():
    store.upsert_account("sick", plan_type="plus", status="healthy")
    globals.error_token_list.append("sick")
    user("seed", "sick", status="active")
    with pytest.raises(LifecycleDenied) as denial:
        route_seed("seed", 2)
    assert denial.value.reason == "account_not_healthy"


def test_forced_switch_skips_the_restricted_original_and_uses_a_healthy_peer():
    store.upsert_account("original", plan_type="plus", status="dead")
    store.upsert_account("alternative", plan_type="plus", status="healthy")
    user("seed", "original", status="active")
    assert route_seed("seed", 2, force_switch=True) == "alternative"
    assert store.get_user("seed")["current_account"] == "alternative"


# ---------------------------------------------------------------------------
# Capacity counts only bindings that can receive work
# ---------------------------------------------------------------------------

def test_frozen_peer_does_not_consume_shared_capacity():
    store.upsert_account("plus", plan_type="plus", status="healthy")
    user("frozen-peer", "plus", status="frozen")
    user("incoming")
    assert route_seed("incoming", 1) == "plus"
    assert store.get_user("incoming")["status"] == "active"
    # The frozen peer keeps its binding and history; it was not evicted.
    assert store.get_user("frozen-peer")["current_account"] == "plus"


def test_active_peer_consumes_the_only_shared_slot():
    store.upsert_account("plus", plan_type="plus", status="healthy")
    user("active-peer", "plus", status="active")
    user("incoming")
    with pytest.raises(LifecycleDenied) as denial:
        route_seed("incoming", 1)
    assert denial.value.reason == "capacity_exceeded"
    assert store.get_user("incoming")["current_account"] == ""


def test_trial_peer_consumes_shared_capacity():
    store.upsert_account("plus", plan_type="plus", status="healthy")
    user("trial-peer", "plus", status="trial", density="trial")
    user("incoming")
    with pytest.raises(LifecycleDenied):
        route_seed("incoming", 1)


# ---------------------------------------------------------------------------
# Tier is a hard boundary, never a preference
# ---------------------------------------------------------------------------

def test_cross_tier_candidate_is_refused():
    store.upsert_account("pro-pool", plan_type="pro", status="healthy")
    user("plus-seed", density="solo")
    with pytest.raises(LifecycleDenied) as denial:
        activate_seed("plus-seed", "pro-pool", max_active_seeds=5)
    assert denial.value.reason == "cross_tier"
    assert store.get_user("plus-seed")["current_account"] == ""


def test_route_never_borrows_from_another_tier():
    store.upsert_account("pro-pool", plan_type="pro", status="healthy")
    user("plus-seed")
    with pytest.raises(LifecycleDenied) as denial:
        route_seed("plus-seed", 2)
    assert denial.value.reason == "no_healthy_candidate"


def test_forced_switch_without_a_peer_is_denied_as_an_empty_pool():
    store.upsert_account("original", plan_type="plus", status="healthy")
    user("seed", "original", status="active")
    with pytest.raises(LifecycleDenied) as denial:
        route_seed("seed", 2, force_switch=True)
    assert denial.value.reason == "no_healthy_candidate"
    assert store.get_user("seed")["current_account"] == "original"


# ---------------------------------------------------------------------------
# Restart: the durable row wins over whatever memory happens to hold
# ---------------------------------------------------------------------------

def test_restart_reuses_the_persisted_binding_instead_of_reallocating():
    store.upsert_account("first", plan_type="plus", status="healthy")
    store.upsert_account("second", plan_type="plus", status="healthy")
    user("seed", "first", status="active")
    store.upsert_conversation("history", "seed", "first", "Example", "1", "2")

    # Restart: memory is rebuilt from SQLite, nothing else survives.
    globals.reload_account_cache()
    assert globals.seed_map["seed"]["token"] == "first"

    assert route_seed("seed", 2) == "first"
    assert store.list_seed_conversations("seed")[0]["account"] == "first"


def test_restart_does_not_resurrect_a_frozen_seed():
    store.upsert_account("plus", plan_type="plus", status="healthy")
    user("seed", "plus", status="frozen")
    globals.reload_account_cache()
    assert "seed" in globals.seed_map
    assert freeze_if_expired("seed") == "frozen"
    assert store.get_user("seed")["status"] == "frozen"
    assert store.get_user("seed")["current_account"] == "plus"


def test_restart_with_all_accounts_restricted_still_refuses():
    store.upsert_account("plus", plan_type="plus", status="dead")
    user("seed", "plus", status="frozen")
    globals.reload_account_cache()
    with pytest.raises(LifecycleDenied):
        route_seed("seed", 2)


# ---------------------------------------------------------------------------
# Concurrency: two switches of one Seed cannot leave a torn binding
# ---------------------------------------------------------------------------

def test_concurrent_forced_switches_leave_one_consistent_binding():
    for token in ("original", "a", "b"):
        store.upsert_account(token, plan_type="plus", status="healthy")
    user("seed", "original", status="active")
    barrier = threading.Barrier(2)

    def switch():
        barrier.wait(timeout=5)
        try:
            return route_seed("seed", 2, force_switch=True)
        except LifecycleDenied as denied:
            return f"denied:{denied.reason}"

    results = []
    threads = [threading.Thread(target=lambda: results.append(switch())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    persisted = store.get_user("seed")
    assert persisted["status"] == "active"
    assert persisted["current_account"] in {"a", "b"}
    # Memory and SQLite agree: the last committed transition is the visible one,
    # and no thread observed a binding that was never persisted.
    assert globals.seed_map["seed"]["token"] == persisted["current_account"]
    for outcome in results:
        assert outcome in {"a", "b", "denied:no_healthy_candidate"}


# ---------------------------------------------------------------------------
# Audit compatibility: a transition outcome is recordable without credentials
# ---------------------------------------------------------------------------

DENIAL_REASONS = {
    "invalid_args", "capacity_unconfigured", "operator_seed", "auth_not_active",
    "no_entitlement", "account_unknown", "account_not_healthy", "cross_tier",
    "capacity_exceeded", "exclusive_conflict", "no_healthy_candidate",
}


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    from utils import audit, configs
    monkeypatch.setattr(configs, "audit_db_path", str(tmp_path / "audit.db"))
    monkeypatch.setattr(audit, "_initialized_paths", set())
    return audit


def test_denial_reason_is_an_anonymous_fixed_code(audit_db):
    """The operator audit sink must be able to carry the outcome verbatim.

    A denial reason is the only per-transition value the code emits, so it has to
    be a fixed vocabulary entry -- never the token, the seed or the email that was
    being routed. ``audit.record`` further filters through a value whitelist; the
    check here is that nothing sensitive is in the reason to begin with.
    """
    from utils import audit

    store.upsert_account("sick-account-token", plan_type="plus", status="dead")
    user("seed-audit@example.test", "sick-account-token", status="active")
    with pytest.raises(LifecycleDenied) as denial:
        route_seed("seed-audit@example.test", 2)

    reason = denial.value.reason
    assert reason in DENIAL_REASONS
    for secret in ("sick-account-token", "seed-audit@example.test"):
        assert secret not in reason

    assert audit.record("seed.transition_denied",
                        subject=audit.subject_id("seed-audit@example.test"),
                        ok=False, detail={"reason": reason, "status": "frozen"}) is True
    event = audit.recent()[0]
    assert event["detail"]["reason"] == reason
    assert "seed-audit@example.test" not in str(event)
