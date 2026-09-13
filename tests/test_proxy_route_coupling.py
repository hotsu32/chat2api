"""Proxy health and route choice are one decision, not two.

An account reaches the Seed router only through the canonical health gate, and
that gate probes over the account's **existing** egress binding. So a transport
failure on the bound proxy has to make the account non-routable, and recovery
has to require a successful authenticated probe sustained for the dwell window —
otherwise a flapping node would put a broken account straight back in front of
live traffic.

These tests drive a real SQLite account row through the real probe path with a
synthetic client (no network), then ask the Seed router for an account.
"""
import base64
import json
import time

import pytest

from utils import configs, fleet_health, globals, store
from utils.seed_lifecycle import LifecycleDenied, route_seed

USER_ID = "user-testonly-proxy"
ACCOUNT_ID = "acc-testonly-proxy"
ACCOUNT = "synthetic-account"
SEED = "synthetic-seed"
# RFC 5737 TEST-NET-3: unroutable, so a stray request cannot leave the host.
PROXY = "socks5h://203.0.113.9:1080"


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _access_token():
    claims = {
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": "plus",
            "chatgpt_account_id": ACCOUNT_ID,
            "chatgpt_user_id": USER_ID,
        },
    }
    seg = lambda d: _b64url(json.dumps(d, separators=(",", ":")).encode("utf-8"))
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.testsig"


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class _ProxyAwareClient:
    """Records the egress it was built for; fails while ``broken`` is set."""

    instances = []
    broken = False

    def __init__(self, proxy=None, timeout=15, verify=True, impersonate="safari15_3"):
        self.proxy = proxy
        self.closed = 0
        self.discarded = 0
        type(self).instances.append(self)

    async def get(self, url, headers=None, timeout=None, **kwargs):
        if type(self).broken:
            raise ConnectionResetError("connection reset by peer")
        return _Resp(200, {"id": USER_ID})

    async def close(self):
        self.closed += 1

    async def discard(self):
        self.discarded += 1


@pytest.fixture(autouse=True)
def isolated(db, monkeypatch):
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.antiban_dead_tokens.clear()
    monkeypatch.setattr(globals, "seed_map", {})
    monkeypatch.setattr(globals, "error_token_list", [])
    monkeypatch.setattr(globals, "antiban_dead_tokens", {})
    monkeypatch.setattr(fleet_health, "_PACING_SECONDS", 0)
    monkeypatch.setattr(fleet_health, "Client", _ProxyAwareClient)
    monkeypatch.setattr(fleet_health, "get_bound_proxy", lambda token: PROXY)
    async def _verify(token):
        return _access_token()

    monkeypatch.setattr(fleet_health, "verify_token", _verify)
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["https://chatgpt.test"])
    fleet_health.reset_recovery_state()
    _ProxyAwareClient.instances = []
    _ProxyAwareClient.broken = False
    yield
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.antiban_dead_tokens.clear()


def _seed_with_entitlement():
    email = f"{SEED}@example.test"
    store.upsert_account(ACCOUNT, plan_type="plus", status="healthy")
    globals.token_list.append(ACCOUNT)
    store.create_user_with_trial(email, password_hash="synthetic", seed=SEED,
                                 status="active", trial_tier="plus", trial_total=3)
    store.upsert_user(SEED, current_account="", plan_type="plus", status="frozen")
    store.create_order(f"order-{SEED}", email, "plus-shared-1m", "1", status="pending")
    store.activate_order(f"order-{SEED}", int(time.time()) + 86400)
    globals.seed_map[SEED] = {"token": "", "plan_type": "plus", "status": "frozen",
                              "conversations": []}


async def test_probe_uses_the_accounts_bound_egress():
    _seed_with_entitlement()
    _ProxyAwareClient.broken = True
    store.upsert_account(ACCOUNT, proxy_url=PROXY)
    await fleet_health.check_all_accounts()
    assert _ProxyAwareClient.instances[0].proxy == PROXY


async def test_bound_proxy_failure_makes_the_account_unroutable():
    _seed_with_entitlement()
    _ProxyAwareClient.broken = True
    await fleet_health.check_all_accounts()

    assert store.get_account(ACCOUNT)["status"] == "unhealthy"
    with pytest.raises(LifecycleDenied) as denial:
        route_seed(SEED, 2)
    assert denial.value.reason == "no_healthy_candidate"
    assert store.get_user(SEED)["current_account"] == ""


async def test_recovery_is_gated_by_dwell_before_the_seed_can_use_it_again():
    _seed_with_entitlement()
    _ProxyAwareClient.broken = True
    await fleet_health.check_all_accounts()
    assert store.get_account(ACCOUNT)["status"] == "unhealthy"

    # The egress recovers, but one lucky probe is not evidence of recovery.
    _ProxyAwareClient.broken = False
    now = int(time.time())
    status, reason = await fleet_health.check_account_detail(ACCOUNT, now=now)
    assert (status, reason) == ("unhealthy", fleet_health.REASON_RECOVERING)
    with pytest.raises(LifecycleDenied):
        route_seed(SEED, 2)

    # Sustained success past the dwell window is the evidence the gate requires.
    later = now + fleet_health._RECOVERY_DWELL_SECONDS
    status, reason = await fleet_health.check_account_detail(ACCOUNT, now=later)
    assert (status, reason) == ("healthy", fleet_health.REASON_OK)
    store.apply_health_probe(ACCOUNT, "unhealthy", status, later)
    assert route_seed(SEED, 2) == ACCOUNT
    assert store.get_user(SEED)["status"] == "active"
