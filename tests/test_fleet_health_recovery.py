"""Health-recovery semantics for the fleet probe (real credential-shape fixtures).

The deadlock this file pins down:

    verify_token() is the *routing* gate. It refuses any account whose persisted
    status is already unhealthy/degraded. The probe used that same call to obtain a
    credential, so an account marked unhealthy could never produce the evidence
    that would clear it — every sweep recorded auth_failed and the restriction
    became permanent.

Recovery therefore needs an explicit health-diagnosis path. That path must not
turn into a routing fail-open, so the contract under test is:

  1. a non-healthy account can be diagnosed, and the diagnosis reaches upstream;
  2. an account believed healthy keeps using the production gate (no bypass);
  3. a failed diagnosis is still unhealthy, and issues no upstream request;
  4. manual `disabled` is never probed and never auto-restored;
  5. circuit-dead accounts recover only through an authenticated probe + dwell;
  6. success alone never restores: the account must stay successful for the dwell
     window, and any failure restarts the clock;
  7. the probe never writes a proxy binding.

Isolation: no network (Client is a spy), no real DB (store reads/writes stubbed).
"""
from __future__ import annotations

import base64
import json
import logging
import time

import pytest
from fastapi import HTTPException

import utils.configs as configs
import utils.globals as globals
from utils import fleet_health
from utils.antiban.concurrency import anon_id

USER_ID = "user-testonly-recovery"
ACCOUNT_ID = "acc-testonly-recovery"

SESSION_TOKEN = "sess-testonly-recovery-0001"
# RFC 5737 TEST-NET-3: unroutable, so an accidental real request cannot escape.
TEST_NET_PROXY = "http://probeuser:probesecret@203.0.113.9:8080"

_ACCOUNTS: dict = {}
_UPSERTS: list = []
_PROBE_WRITES: list = []


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _access_token(user_id=USER_ID, account_id=ACCOUNT_ID, exp=None, plan="plus"):
    claims = {
        "sub": f"auth0|{user_id}",
        "iat": int(time.time()) - 60,
        "exp": exp if exp is not None else int(time.time()) + 3600,
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan,
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
        },
    }
    seg = lambda d: _b64url(json.dumps(d, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.testsig"


# A real AccessToken shape: the diagnosis path returns it verbatim, so it has to
# carry a decodable identity rather than just look like a token.
ACCESS_TOKEN = _access_token()


class _Resp:
    def __init__(self, status_code=200, body=None, content_type="application/json", text=None):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class _SpyClient:
    """Client stand-in: records construction, requests and how it was released."""

    instances: list = []
    next_response = None
    next_error = None

    def __init__(self, proxy=None, timeout=15, verify=True, impersonate="safari15_3"):
        self.proxy = proxy
        self.requests = []
        self.closed = 0
        self.discarded = 0
        type(self).instances.append(self)

    async def get(self, url, headers=None, timeout=None, **kwargs):
        self.requests.append({"url": url, "headers": dict(headers or {})})
        if type(self).next_error is not None:
            raise type(self).next_error
        return type(self).next_response

    async def close(self):
        self.closed += 1

    async def discard(self):
        self.discarded += 1


# --- stand-ins for the two credential paths ---------------------------------

async def _refuse(token):
    """The production routing gate refusing a non-healthy account."""
    raise HTTPException(status_code=401, detail="Account unavailable")


def _returns_access_token():
    async def _exchange(token, force_refresh=False):
        return _access_token()
    return _exchange


def _recording(seen, result=None, error=None):
    async def _call(token, *args, **kwargs):
        seen.append(token)
        if error is not None:
            raise error
        return result if result is not None else _access_token()
    return _call


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    globals.antiban_dead_tokens.clear()
    globals.error_token_list.clear()
    _ACCOUNTS.clear()
    _UPSERTS.clear()
    _PROBE_WRITES.clear()
    _SpyClient.instances = []
    _SpyClient.next_response = _Resp(200, {"id": USER_ID, "email": "owner@example.com"})
    _SpyClient.next_error = None
    fleet_health.reset_recovery_state()
    fleet_health.reset_health_stats()

    monkeypatch.setattr(fleet_health, "Client", _SpyClient)
    monkeypatch.setattr(fleet_health, "get_bound_proxy", lambda token: None)
    monkeypatch.setattr(configs, "proxy_url_list", [])
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["https://chatgpt.test"])

    monkeypatch.setattr(fleet_health.store, "get_account", lambda token: _ACCOUNTS.get(token))
    monkeypatch.setattr(
        fleet_health.store, "upsert_account",
        lambda token, **fields: _UPSERTS.append((token, fields)),
    )

    def _apply_probe(token, expected_status, status, checked_at):
        """Mirror store.apply_health_probe's conditional, stale-snapshot semantics."""
        acct = _ACCOUNTS.get(token)
        if acct is None or acct.get("status") != expected_status:
            return False
        if (acct.get("last_health_check") or 0) > checked_at:
            return False
        acct["status"] = status
        acct["last_health_check"] = checked_at
        _PROBE_WRITES.append((token, expected_status, status, checked_at))
        return True

    monkeypatch.setattr(fleet_health.store, "apply_health_probe", _apply_probe)
    yield
    _ACCOUNTS.clear()
    fleet_health.reset_recovery_state()


def _blob(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 1. The deadlock: a non-healthy account must be diagnosable
# ---------------------------------------------------------------------------

async def test_unhealthy_account_reaches_upstream_despite_the_routing_gate(monkeypatch):
    """RED on the old code: verify_token refuses unhealthy, so no evidence was ever
    collected and the account could never leave unhealthy."""
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "unhealthy"}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)

    status, reason = await fleet_health.check_account_detail(ACCESS_TOKEN)

    assert _SpyClient.instances, "the diagnosis must reach upstream to collect evidence"
    assert (status, reason) == ("unhealthy", fleet_health.REASON_RECOVERING)


async def test_degraded_account_is_diagnosable_too(monkeypatch):
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "degraded"}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)

    status, reason = await fleet_health.check_account_detail(ACCESS_TOKEN)

    assert _SpyClient.instances
    assert (status, reason) == ("unhealthy", fleet_health.REASON_RECOVERING)


async def test_session_token_is_exchanged_on_the_diagnosis_path(monkeypatch):
    """Non-AccessToken credentials need the exchange; the diagnosis path owns it."""
    _ACCOUNTS[SESSION_TOKEN] = {"status": "unhealthy"}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    seen: list = []
    monkeypatch.setattr(fleet_health, "sess2ac", _recording(seen))

    await fleet_health.check_account_detail(SESSION_TOKEN)

    assert seen == [SESSION_TOKEN], "the session token must be exchanged, not sent raw"
    headers = _SpyClient.instances[0].requests[-1]["headers"]
    assert headers["Authorization"].startswith("Bearer ")


# ---------------------------------------------------------------------------
# 2. The diagnosis path must not become a routing fail-open
# ---------------------------------------------------------------------------

async def test_healthy_account_keeps_using_the_production_gate(monkeypatch):
    """An account believed healthy must NOT be re-diagnosed around the gate."""
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "healthy"}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    seen: list = []
    monkeypatch.setattr(fleet_health, "diagnose_access_token", _recording(seen))

    status, reason = await fleet_health.check_account_detail(ACCESS_TOKEN)

    assert (status, reason) == ("unhealthy", fleet_health.REASON_AUTH_FAILED)
    assert seen == [], "no bypass is attempted for an account that is not restricted"
    assert _SpyClient.instances == []


async def test_unregistered_account_is_not_diagnosed_around_the_gate(monkeypatch):
    """No persisted row means no restriction to explain the refusal."""
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    seen: list = []
    monkeypatch.setattr(fleet_health, "diagnose_access_token", _recording(seen))

    assert (await fleet_health.check_account_detail(ACCESS_TOKEN)) == (
        "unhealthy", fleet_health.REASON_AUTH_FAILED
    )
    assert seen == []


async def test_failed_diagnosis_is_still_unhealthy_and_sends_no_request(monkeypatch):
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "unhealthy"}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    monkeypatch.setattr(fleet_health, "diagnose_access_token", _refuse)

    assert (await fleet_health.check_account_detail(ACCESS_TOKEN)) == (
        "unhealthy", fleet_health.REASON_AUTH_FAILED
    )
    assert _SpyClient.instances == []


async def test_diagnosis_network_failure_is_reported_as_a_network_error(monkeypatch):
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "unhealthy"}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    monkeypatch.setattr(
        fleet_health, "diagnose_access_token",
        _recording([], error=ConnectionResetError("synthetic secret detail")),
    )

    assert (await fleet_health.check_account_detail(ACCESS_TOKEN)) == (
        "unhealthy", fleet_health.REASON_NETWORK_ERROR
    )
    assert _SpyClient.instances == []


# ---------------------------------------------------------------------------
# 3. Manual override and hard verdicts are never probed
# ---------------------------------------------------------------------------

async def test_disabled_account_is_never_probed_nor_auto_restored(monkeypatch):
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "disabled"}
    seen: list = []
    monkeypatch.setattr(fleet_health, "verify_token", _recording(seen))
    monkeypatch.setattr(fleet_health, "diagnose_access_token", _recording(seen))

    status, reason = await fleet_health.check_account_detail(ACCESS_TOKEN)

    assert reason == fleet_health.REASON_DISABLED
    assert status == "unhealthy", "probe vocabulary: a manual disable is not serving"
    assert fleet_health.resolve_account_status(ACCESS_TOKEN) == "disabled"
    assert seen == [], "a manual override is not a health question"
    assert _SpyClient.instances == []


async def test_circuit_dead_account_requires_probe_success_and_dwell(monkeypatch):
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "dead"}
    globals.antiban_dead_tokens[ACCESS_TOKEN] = {"reason": "account_deactivated", "dead_at": 1}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    monkeypatch.setattr(fleet_health, "diagnose_access_token", _returns_access_token())
    monkeypatch.setattr(fleet_health, "_RECOVERY_DWELL_SECONDS", 10)

    first = await fleet_health.check_account_detail(ACCESS_TOKEN, now=100)
    second = await fleet_health.check_account_detail(ACCESS_TOKEN, now=109)
    recovered = await fleet_health.check_account_detail(ACCESS_TOKEN, now=110)

    assert first == second == ("unhealthy", fleet_health.REASON_RECOVERING)
    assert recovered == ("healthy", fleet_health.REASON_OK)
    assert len(_SpyClient.instances) == 3


async def test_error_list_alone_blocks_the_probe(monkeypatch):
    """With no persisted restriction to re-evaluate, the list stays authoritative."""
    globals.error_token_list.append(ACCESS_TOKEN)
    monkeypatch.setattr(fleet_health, "verify_token", _returns_access_token())

    assert (await fleet_health.check_account_detail(ACCESS_TOKEN)) == (
        "unhealthy", fleet_health.REASON_ERROR_LIST
    )
    assert _SpyClient.instances == [], "nothing to re-evaluate: do not spend a request"


async def test_error_list_does_not_block_a_persisted_unhealthy_account(monkeypatch):
    """RED: load_all rebuilds the list from persisted unhealthy, so membership must not
    stop the probe for an account that still carries a restriction to re-evaluate."""
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "unhealthy"}
    globals.error_token_list.append(ACCESS_TOKEN)
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)

    status, reason = await fleet_health.check_account_detail(ACCESS_TOKEN)

    assert _SpyClient.instances, "the list must not stand as its own justification"
    assert (status, reason) == ("unhealthy", fleet_health.REASON_RECOVERING)


# ---------------------------------------------------------------------------
# 4. Success alone must not restore: dwell is required
# ---------------------------------------------------------------------------

async def test_dwell_is_required_before_the_status_flips(monkeypatch):
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "unhealthy"}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    monkeypatch.setattr(fleet_health, "_RECOVERY_DWELL_SECONDS", 300)

    assert (await fleet_health.check_account_detail(ACCESS_TOKEN, now=1000))[1] == (
        fleet_health.REASON_RECOVERING
    )
    assert (await fleet_health.check_account_detail(ACCESS_TOKEN, now=1299))[1] == (
        fleet_health.REASON_RECOVERING
    )
    assert await fleet_health.check_account_detail(ACCESS_TOKEN, now=1301) == (
        "healthy", fleet_health.REASON_OK
    )


async def test_a_failed_probe_restarts_the_dwell_clock(monkeypatch):
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "unhealthy"}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    monkeypatch.setattr(fleet_health, "_RECOVERY_DWELL_SECONDS", 300)

    await fleet_health.check_account_detail(ACCESS_TOKEN, now=1000)
    _SpyClient.next_response = _Resp(500, None, content_type="text/html", text="<html>oops</html>")
    assert (await fleet_health.check_account_detail(ACCESS_TOKEN, now=1100))[0] == "unhealthy"

    _SpyClient.next_response = _Resp(200, {"id": USER_ID})
    assert (await fleet_health.check_account_detail(ACCESS_TOKEN, now=1200))[1] == (
        fleet_health.REASON_RECOVERING
    )
    assert (await fleet_health.check_account_detail(ACCESS_TOKEN, now=1400))[1] == (
        fleet_health.REASON_RECOVERING
    ), "the clock restarted at 1200, so 1400 is not yet a full dwell"
    assert await fleet_health.check_account_detail(ACCESS_TOKEN, now=1501) == (
        "healthy", fleet_health.REASON_OK
    )


async def test_a_healthy_account_is_not_slowed_down_by_the_dwell(monkeypatch):
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "healthy"}
    monkeypatch.setattr(fleet_health, "_RECOVERY_DWELL_SECONDS", 300)
    assert await fleet_health.check_account_detail(ACCESS_TOKEN, now=1000) == (
        "healthy", fleet_health.REASON_OK
    )


async def test_sweep_does_not_publish_healthy_while_recovering(monkeypatch):
    """The sweep writes the probe's verdict; a recovering account stays non-routable."""
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "unhealthy"}
    globals.token_list = [ACCESS_TOKEN]
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    monkeypatch.setattr(fleet_health, "_PACING_SECONDS", 0, raising=False)

    summary = await fleet_health.check_all_accounts()

    assert _ACCOUNTS[ACCESS_TOKEN]["status"] == "unhealthy"
    assert summary["healthy"] == 0
    assert summary["recovering"] == 1
    assert _PROBE_WRITES and _PROBE_WRITES[-1][2] == "unhealthy"


# ---------------------------------------------------------------------------
# 5. The probe never re-binds a proxy
# ---------------------------------------------------------------------------

async def test_probe_never_writes_a_proxy_binding(monkeypatch):
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "unhealthy", "proxy_url": TEST_NET_PROXY}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    monkeypatch.setattr(fleet_health, "get_bound_proxy", lambda token: TEST_NET_PROXY)

    await fleet_health.check_account_detail(ACCESS_TOKEN)

    assert _UPSERTS == [], "the health probe must not rewrite account rows"
    assert _ACCOUNTS[ACCESS_TOKEN]["proxy_url"] == TEST_NET_PROXY
    assert _SpyClient.instances[0].proxy == TEST_NET_PROXY


async def test_unbound_account_probe_uses_pool_outlet(monkeypatch):
    """An unbound account follows the same configured pool outlet as chat."""
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "unhealthy"}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    monkeypatch.setattr(configs, "proxy_url_list", [TEST_NET_PROXY])

    await fleet_health.check_account_detail(ACCESS_TOKEN)

    assert _SpyClient.instances[0].proxy == TEST_NET_PROXY
    assert _UPSERTS == []


# ---------------------------------------------------------------------------
# 6. The diagnosis path stays anonymous
# ---------------------------------------------------------------------------

async def test_diagnosis_logs_carry_no_credential_or_upstream_text(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    _ACCOUNTS[ACCESS_TOKEN] = {"status": "unhealthy"}
    monkeypatch.setattr(fleet_health, "verify_token", _refuse)
    monkeypatch.setattr(
        fleet_health, "diagnose_access_token",
        _recording([], error=RuntimeError(f"exchange failed via {TEST_NET_PROXY}")),
    )

    await fleet_health.check_account_detail(ACCESS_TOKEN)

    blob = _blob(caplog)
    assert ACCESS_TOKEN not in blob
    for n in (8, 10, 12, 16):
        assert ACCESS_TOKEN[:n] not in blob, f"token prefix of length {n} leaked"
    assert "probeuser" not in blob and "probesecret" not in blob and "203.0.113.9" not in blob
    assert "exchange failed via" not in blob
    assert "RuntimeError" in blob, "the error class is the diagnosis and stays"
    assert anon_id(ACCESS_TOKEN) in blob
