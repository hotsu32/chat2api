"""Stage 4 — antiban B1–B5.

B1–B4 are pure unit tests against the antiban modules (enable_antiban toggled on
via monkeypatch). B5 exercises guard.acquire_context / report_* with geo stubbed.
"""

import time

import pytest

import utils.configs as configs
import utils.globals as globals
from utils.antiban import account_risk, bucket, circuit, concurrency, cooldown, geo, guard, iprep


@pytest.fixture(autouse=True)
def _reset_antiban_state():
    # conftest._reset_state clears globals.antiban_* / error_token_list / fp_map,
    # but not these module-private dicts.
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()
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


# ---------------------------------------------------------------------------
# B6 concurrency: per-account in-flight cap (acquire/release/tier/failover)
# ---------------------------------------------------------------------------

async def test_concurrency_acquire_release_and_cap(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "account_max_concurrency", 2)
    monkeypatch.setattr(configs, "account_concurrency_wait_seconds", 0.05)

    assert await concurrency.acquire("tok") is True
    assert await concurrency.acquire("tok") is True
    # 满：等 0.05s 仍无槽位 → False（failover 信号）
    assert await concurrency.acquire("tok") is False

    concurrency.release("tok")
    assert await concurrency.acquire("tok") is True


async def test_concurrency_release_idempotent(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    # 未占用就 release：不报错、不产生槽位泄漏
    concurrency.release("tok-nonexistent")
    assert await concurrency.acquire("tok-nonexistent") is True


def test_concurrency_resolve_limit_persona(monkeypatch):
    monkeypatch.setattr(configs, "account_max_concurrency", 5)
    monkeypatch.setattr(configs, "free_account_max_concurrency", 10)
    assert concurrency._resolve_limit("tok", persona="chatgpt-freeaccount") == 10
    assert concurrency._resolve_limit("tok", persona="chatgpt-paid") == 5
    assert concurrency._resolve_limit("tok") == 5


def test_concurrency_resolve_limit_from_store_plan_type(monkeypatch):
    monkeypatch.setattr(configs, "account_max_concurrency", 5)
    monkeypatch.setattr(configs, "free_account_max_concurrency", 10)
    import utils.store as store
    monkeypatch.setattr(store, "get_account", lambda token: {"plan_type": "free"})
    assert concurrency._resolve_limit("tok-free") == 10
    monkeypatch.setattr(store, "get_account", lambda token: {"plan_type": "plus"})
    assert concurrency._resolve_limit("tok-plus") == 5


async def test_acquire_context_sets_concurrency_flag(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)
    monkeypatch.setattr(geo, "get_geo", lambda proxy_url: None)

    ctx = await guard.acquire_context("tok-1")
    assert ctx.concurrency_acquired is True
    guard.release_context(ctx)
    assert ctx.concurrency_acquired is False


# ---------------------------------------------------------------------------
# B7 降智联动：sniff 命中 → 冷却（软退避）/ 熔断（硬）
# ---------------------------------------------------------------------------

def _warning_message(text="unusual activity detected"):
    return {"content": {"parts": [text]}, "author": {"role": "assistant"}}


def test_sniff_hit_extends_cooldown(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "account_degraded_link_enabled", True)
    monkeypatch.setattr(configs, "account_degraded_cooldown", 1800)
    monkeypatch.setattr(configs, "account_degraded_mark_dead_threshold", 3)
    monkeypatch.setattr(account_risk, "_persist", lambda: None)

    account_risk.sniff("tok", _warning_message())
    # 命中后：冷却被延长（软退避），未 mark_dead
    assert cooldown.get_next_available("tok") > time.time()
    assert circuit.is_token_dead("tok") is False


def test_sniff_hit_threshold_marks_dead(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "account_degraded_link_enabled", True)
    monkeypatch.setattr(configs, "account_degraded_mark_dead_threshold", 2)
    monkeypatch.setattr(account_risk, "_persist", lambda: None)

    account_risk.sniff("tok2", _warning_message())   # hit 1 → 冷却
    assert circuit.is_token_dead("tok2") is False
    account_risk.sniff("tok2", _warning_message())   # hit 2 >= 2 → mark_dead
    assert circuit.is_token_dead("tok2") is True


def test_sniff_link_disabled_only_records(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "account_degraded_link_enabled", False)
    monkeypatch.setattr(account_risk, "_persist", lambda: None)

    account_risk.sniff("tok3", _warning_message())
    # Step B 关闭：只记录，不联动
    assert "tok3" in globals.account_warnings
    assert circuit.is_token_dead("tok3") is False
    assert cooldown.get_next_available("tok3") == 0.0


# ---------------------------------------------------------------------------
# B8 半专属分池：free/plus 不串池（桶按 plan_type 定型，跨档绝不混桶）
# ---------------------------------------------------------------------------

def test_assign_account_separates_free_plus(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "bucket_max_accounts_per_ip", 5)
    globals.antiban_bucket["buckets"] = {
        "bkt::p1": _bucket("http://p1"),
        "bkt::p2": _bucket("http://p2"),
    }
    globals.antiban_bucket["account_index"] = {}
    import utils.store as store
    plan = {"tok-free": "free", "tok-plus": "plus"}
    monkeypatch.setattr(store, "get_account", lambda token: {"plan_type": plan.get(token)})

    b_free = bucket.assign_account("tok-free")
    b_plus = bucket.assign_account("tok-plus")
    assert b_free is not None and b_plus is not None
    assert b_free != b_plus  # 半专属分池：不同档不落同一桶
    # 桶各自定型为对应档
    assert globals.antiban_bucket["buckets"][b_free]["plan_type"] == "free"
    assert globals.antiban_bucket["buckets"][b_plus]["plan_type"] == "plus"


def test_assign_account_skips_mismatched_empty_bucket(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "bucket_max_accounts_per_ip", 5)
    globals.antiban_bucket["buckets"] = {
        "bkt::p1": _bucket("http://p1"),
        "bkt::p2": _bucket("http://p2"),
    }
    # p1 已定型 free（空但不可用于 plus），p2 未定型
    globals.antiban_bucket["buckets"]["bkt::p1"]["plan_type"] = "free"
    globals.antiban_bucket["account_index"] = {}
    import utils.store as store
    monkeypatch.setattr(store, "get_account", lambda token: {"plan_type": "plus"})

    result = bucket.assign_account("tok-plus")
    # 若无档位过滤，least-loaded 会选 p1（同 size 0 且排序靠前）；档位过滤强制落到 p2
    assert result == "bkt::p2"
    assert globals.antiban_bucket["buckets"]["bkt::p2"]["plan_type"] == "plus"


def test_assign_account_rejects_cross_tier_when_no_matching(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "bucket_max_accounts_per_ip", 5)
    globals.antiban_bucket["buckets"] = {
        "bkt::p1": _bucket("http://p1"),
    }
    globals.antiban_bucket["buckets"]["bkt::p1"]["plan_type"] = "free"
    globals.antiban_bucket["account_index"] = {}
    import utils.store as store
    monkeypatch.setattr(store, "get_account", lambda token: {"plan_type": "plus"})

    # 只有 free 桶（未满），plus 号绝不跨档 → 拒绝分配
    assert bucket.assign_account("tok-plus") is None


# ---------------------------------------------------------------------------
# B9 IP 信誉（IPQS 欺诈分 + ASN）：fail-open / 高欺诈判黑 / 数据中心开关 / 桶前置过滤
# ---------------------------------------------------------------------------

def test_iprep_fail_open_without_key(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "ipqs_api_key", "")
    # 无 key：不做任何网络调用，直接放行（fail-open）
    assert iprep.is_blocked("http://proxy.example:8080") is False


def test_iprep_blocks_high_fraud(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "ipqs_api_key", "test-key")
    monkeypatch.setattr(configs, "ipqs_fraud_threshold", 80)
    monkeypatch.setattr(iprep, "_resolve_host", lambda host: "1.2.3.4")
    monkeypatch.setattr(iprep, "_query_ipqs", lambda ip: {
        "fraud_score": 90, "is_datacenter": False, "is_proxy": False,
        "asn": 123, "isp": "x", "organization": "y",
    })
    monkeypatch.setattr(iprep, "_persist", lambda: None)
    assert iprep.is_blocked("http://proxy.example:8080") is True


def test_iprep_allows_clean(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "ipqs_api_key", "test-key")
    monkeypatch.setattr(configs, "ipqs_fraud_threshold", 80)
    monkeypatch.setattr(iprep, "_resolve_host", lambda host: "1.2.3.4")
    monkeypatch.setattr(iprep, "_query_ipqs", lambda ip: {
        "fraud_score": 10, "is_datacenter": False, "is_proxy": False,
        "asn": 123, "isp": "x", "organization": "y",
    })
    monkeypatch.setattr(iprep, "_persist", lambda: None)
    assert iprep.is_blocked("http://proxy.example:8080") is False


def test_iprep_datacenter_blocked_only_when_enabled(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "ipqs_api_key", "test-key")
    monkeypatch.setattr(configs, "ipqs_fraud_threshold", 80)
    monkeypatch.setattr(iprep, "_resolve_host", lambda host: "1.2.3.4")
    rep = {"fraud_score": 10, "is_datacenter": True, "is_proxy": False,
           "asn": 123, "isp": "x", "organization": "y"}
    monkeypatch.setattr(iprep, "_query_ipqs", lambda ip: dict(rep))
    monkeypatch.setattr(iprep, "_persist", lambda: None)

    monkeypatch.setattr(configs, "ipqs_block_datacenter", False)
    assert iprep.is_blocked("http://dc.example:8080") is False
    # 开关打开后（命中缓存、verdict 实时重算）→ 判黑
    monkeypatch.setattr(configs, "ipqs_block_datacenter", True)
    assert iprep.is_blocked("http://dc.example:8080") is True


def test_assign_account_skips_ip_blocked_bucket(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "bucket_max_accounts_per_ip", 5)
    globals.antiban_bucket["buckets"] = {
        "bkt::p1": _bucket("http://blocked"),
        "bkt::p2": _bucket("http://clean"),
    }
    globals.antiban_bucket["account_index"] = {}
    # 只把 "http://blocked" 判黑，隔离桶层与 iprep 内部
    monkeypatch.setattr(bucket, "_ip_blocked", lambda proxy_url: proxy_url == "http://blocked")

    result = bucket.assign_account("tok-1")
    assert result == "bkt::p2"


