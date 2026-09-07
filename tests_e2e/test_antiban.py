"""Stage 4 — antiban B1–B5.

B1–B4 are pure unit tests against the antiban modules (enable_antiban toggled on
via monkeypatch). B5 exercises guard.acquire_context / report_* with geo stubbed.
"""

import time

import pytest

import utils.configs as configs
import utils.globals as globals
from utils.antiban import bucket, circuit, cooldown, geo, guard


@pytest.fixture(autouse=True)
def _reset_antiban_state():
    # conftest._reset_state clears globals.antiban_* / error_token_list / fp_map,
    # but not these module-private dicts.
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    yield


def _bucket(proxy_url="http://p1", status="healthy", accounts=None, degraded_until=0):
    return {
        "proxy_url": proxy_url,
        "proxy_name": proxy_url,
        "group": "",
        "accounts": accounts or [],
        "last_request_at": {},
        "status": status,
        "degraded_until": degraded_until,
        "created_at": 0,
    }


# ---------------------------------------------------------------------------
# B1 circuit: mark_dead / 429 backoff / 403 bucket / 401 / deactivated / reset
# ---------------------------------------------------------------------------

def test_mark_dead_sets_dead_token(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    circuit.mark_dead("tok-dead", "account_deactivated")
    assert circuit.is_token_dead("tok-dead") is True
    assert globals.antiban_dead_tokens["tok-dead"]["reason"] == "account_deactivated"
    assert "dead_at" in globals.antiban_dead_tokens["tok-dead"]


def test_429_backoff_exponential_with_cap(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    t0 = time.time()
    circuit.handle_response_error("tok", None, 429, "rate-limit")   # level 1 -> 1800
    l1 = cooldown.get_next_available("tok") - t0
    circuit.handle_response_error("tok", None, 429, "rate-limit")   # level 2 -> 3600
    l2 = cooldown.get_next_available("tok") - t0
    circuit.handle_response_error("tok", None, 429, "rate-limit")   # level 3 -> 7200
    l3 = cooldown.get_next_available("tok") - t0
    circuit.handle_response_error("tok", None, 429, "rate-limit")   # level 4 -> capped 7200
    l4 = cooldown.get_next_available("tok") - t0

    assert circuit._account_backoff_level["tok"] == 4
    assert 1750 < l1 < 1850
    assert 3550 < l2 < 3650
    assert 7150 < l3 < 7250
    assert 7150 < l4 < 7250  # cap at 7200


def test_403_cf_chl_degrades_bucket(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    globals.antiban_bucket["buckets"]["bkt::p1"] = _bucket()
    globals.antiban_bucket["account_index"] = {}

    circuit.handle_response_error("tok", "bkt::p1", 403, "cf_chl_opt")

    meta = bucket.get_bucket_meta("bkt::p1")
    assert meta["status"] == "degraded"
    assert meta["degraded_until"] > int(time.time())


def test_401_invalid_grant_goes_error_list_not_dead(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    circuit.handle_response_error("tok", None, 401, "invalid_grant")
    assert "tok" in globals.error_token_list
    assert circuit.is_token_dead("tok") is False


def test_account_deactivated_marks_dead(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    circuit.handle_response_error("tok", None, 401, "account_deactivated")
    assert circuit.is_token_dead("tok") is True


def test_success_resets_backoff(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    circuit.handle_response_error("tok", None, 429, "rate-limit")
    assert circuit._account_backoff_level.get("tok") == 1
    circuit.handle_response_success("tok")
    assert circuit._account_backoff_level.get("tok") is None


# ---------------------------------------------------------------------------
# B2 cooldown: record_request intervals / wait_or_skip three states / extend
# ---------------------------------------------------------------------------

def test_record_request_sets_interval(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    cooldown.record_request("tok-plus", persona="chatgpt-paid")
    delta = cooldown.get_next_available("tok-plus") - time.time()
    assert 40 < delta <= 80  # account_min_interval_seconds=60 ± 0.3 jitter

    cooldown.record_request("tok-free", persona="chatgpt-freeaccount")
    delta_free = cooldown.get_next_available("tok-free") - time.time()
    assert 120 < delta_free <= 240  # free_account_min_interval_seconds=180 ± 0.3


async def test_wait_or_skip_three_states(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    # no cooldown -> pass immediately
    assert await cooldown.wait_or_skip("tok") is True
    # within max_wait -> sleep then pass
    cooldown._account_next_available["tok"] = time.time() + 0.1
    assert await cooldown.wait_or_skip("tok", max_wait=30) is True
    # beyond max_wait -> skip
    cooldown._account_next_available["tok"] = time.time() + 1000
    assert await cooldown.wait_or_skip("tok", max_wait=30) is False


def test_extend_cooldown_monotonic(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    cooldown._account_next_available["tok"] = time.time() + 100
    cooldown.extend_cooldown("tok", 50)
    first = cooldown.get_next_available("tok")
    cooldown.extend_cooldown("tok", 200)
    second = cooldown.get_next_available("tok")
    assert second > first  # never shorten an existing cooldown


# ---------------------------------------------------------------------------
# B3 self-heal: degraded bucket -> healthy; dead account NOT auto-revived
# ---------------------------------------------------------------------------

def test_heal_buckets_restores_degraded(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    globals.antiban_bucket["buckets"]["bkt::p1"] = _bucket(
        status="degraded", degraded_until=int(time.time()) - 10,
    )
    restored = bucket.heal_buckets()
    assert restored == 1
    assert globals.antiban_bucket["buckets"]["bkt::p1"]["status"] == "healthy"


def test_dead_account_not_auto_revived(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    circuit.mark_dead("tok", "banned")
    bucket.heal_buckets()
    assert circuit.is_token_dead("tok") is True


# ---------------------------------------------------------------------------
# B4 bucket: sticky assignment / least-load / capacity cap
# ---------------------------------------------------------------------------

def test_assign_account_sticky_and_least_loaded(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    globals.antiban_bucket["buckets"] = {
        "bkt::p1": _bucket("http://p1"),
        "bkt::p2": _bucket("http://p2"),
    }
    globals.antiban_bucket["account_index"] = {}

    b1 = bucket.assign_account("tok-1")
    assert b1 is not None
    # 粘性：同一 token 再次分配返回同一桶，绝不漂移
    assert bucket.assign_account("tok-1") == b1
    # 最少负载：tok-2 落到另一个空桶
    b2 = bucket.assign_account("tok-2")
    assert b2 is not None and b2 != b1


def test_bucket_capacity_limits_assignment(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "bucket_max_accounts_per_ip", 2)
    globals.antiban_bucket["buckets"] = {
        "bkt::p1": _bucket("http://p1", accounts=["a", "b"]),
    }
    globals.antiban_bucket["account_index"] = {"a": "bkt::p1", "b": "bkt::p1"}

    assert bucket.assign_account("tok-new") is None  # 满桶，拒绝分配


def test_degrade_bucket_meta(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    globals.antiban_bucket["buckets"]["bkt::p1"] = _bucket()
    bucket.degrade_bucket("bkt::p1", 300)
    meta = bucket.get_bucket_meta("bkt::p1")
    assert meta["status"] == "degraded"
    assert meta["degraded_until"] == 0 or meta["degraded_until"] > int(time.time())


# ---------------------------------------------------------------------------
# B5 guard: acquire_context off/on + report_success/error routing
# ---------------------------------------------------------------------------

async def test_acquire_context_disabled_no_side_effect(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", False)
    ctx = await guard.acquire_context("tok-1")
    assert ctx.enabled is False
    assert ctx.bucket_id is None
    assert ctx.proxy_url is None
    assert ctx.header_overrides == {}
    assert ctx.fp_overrides == {}


async def test_acquire_context_enabled_populates(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(bucket, "assign_account", lambda token: "bkt::p1")
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: "http://127.0.0.1:1080")
    monkeypatch.setattr(geo, "get_geo", lambda proxy_url: {
        "country": "JP",
        "accept_language": "ja-JP,ja;q=0.9",
        "oai_language": "ja-JP",
        "tz_offset_min": 540,
        "timezone": "Asia/Tokyo",
    })

    ctx = await guard.acquire_context("tok-1")
    assert ctx.enabled is True
    assert ctx.bucket_id == "bkt::p1"
    assert ctx.proxy_url == "http://127.0.0.1:1080"
    assert ctx.header_overrides["accept-language"] == "ja-JP,ja;q=0.9"
    assert ctx.header_overrides["oai-language"] == "ja-JP"
    assert ctx.header_overrides["_timezone_name"] == "Asia/Tokyo"
    assert ctx.tz_offset_min == 540
    assert ctx.fp_overrides  # ensure_extended returned non-empty fp


async def test_report_error_and_success_route(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    ctx = guard.AntibanContext(token="tok", enabled=True)
    await guard.report_error(ctx, 429, "rate-limit")
    assert circuit._account_backoff_level.get("tok") == 1
    await guard.report_success(ctx)
    assert circuit._account_backoff_level.get("tok") is None
