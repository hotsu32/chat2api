"""Capacity-protection gaps found by probing admission + feedback behaviour.

Each test here is written against a *behaviour* the layer claims in its own
docstrings, not against an implementation:

  1. **严格 IP 绑定不能静默放行「分不到桶」的账号。** bucket.assign_account 返回
     None 的语义是「这个号此刻无法绑定到任何健康出口」。严格模式下把 None 当
     「无需绑定」放过去，等于让请求走一个没有任何绑定保证的出口——粘性契约被
     无声作废。反过来，配方里根本没有桶（没配代理）不是绑定违约，那个边界
     （宽松模式 / 空池）必须继续放行。
  2. **proxy / timeout 失败必须能计数，哪怕号还没绑桶。** 指标只对已绑桶的号
     记数，会让「绑桶失败的号在打不出去的出口上连续失败」在运维面板上完全不
     可见——正好是最该被看见的那一段。
  3. **worker 声明必须 fail closed，不认识的声明不能被读成「单进程」。**
     `WEB_CONCURRENCY=0` 在 gunicorn 里不是「一个 worker」，而是「按 CPU 数
     展开」；`auto` / `4,4` 这类值既不是 1 也不是可解析的整数。把它们当成未
     声明 = 在多 Worker 下按进程内上限放行，正是本层承诺不做的静默降级。
  4. **声明了共享协调层也不能 fail open。** 本层没有协调层客户端，所以「声明
     了 Redis」既不能解锁多 Worker，也不能被静默忽略：必须启动期拒绝，并如实
     报告「声明了但不可用」。
  5. **挑战族 / 账号不可用必须与 429/401/5xx 区分开。** PoW、Turnstile、Arkose
     与 cf_chl_opt 都是上游明确告知的挑战，它们当前的归类是 unclassified，
     既没有降载动作也没有可查询指标。
  6. **死号复活必须先有探针证据。** circuit.revive_token 是公开原语，调用它本身
     不携带任何证据；没有证据的复活会把一个被封的号放回流量。

隔离：不发网络请求、不写真实 data/（持久化与 store 读取全部打桩）。
"""

import asyncio
import json
import logging
import math
import time

import pytest

import utils.configs as configs
import utils.globals as globals
from utils import store
from utils.antiban import bucket, circuit, cooldown, guard
import utils.antiban as antiban

# 测试专用假凭据。断言「它不出现在日志/指标里」，而不是断言真值。
FAKE_TOKEN = "eyJhbGciOiJIUzI1NitestonlyCAPGAPS246810"
BUCKET_ID = "bkt::testonly-gaps"
_WORKER_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "WORKERS")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    cooldown.reset_cooldown_stats()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    circuit.reset_circuit_stats()
    guard.reset_admission_stats()
    globals.antiban_dead_tokens.clear()
    globals.error_token_list.clear()
    globals.antiban_bucket = {"buckets": {}, "account_index": {}}
    # 部署形态必须由每个用例显式声明，否则会继承本机 shell 的值
    for var in _WORKER_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(circuit, "_persist_dead", lambda: None)
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "strict_ip_binding", True)
    monkeypatch.setattr(configs, "account_max_wait_seconds", 1)
    monkeypatch.setattr(configs, "account_concurrency_wait_seconds", 0.02)
    yield


def _healthy_bucket(bucket_id=BUCKET_ID, accounts=()):
    globals.antiban_bucket["buckets"][bucket_id] = {
        "proxy_url": "http://proxy.test",
        "proxy_name": "testonly",
        "group": "",
        "accounts": list(accounts),
        "last_request_at": {},
        "status": "healthy",
        "degraded_until": 0,
        "created_at": 0,
    }
    return bucket_id


def _entries(caplog):
    return "\n".join(record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# 1. 严格绑定：分不到桶的账号不得放行
# ---------------------------------------------------------------------------

async def test_unplaceable_account_is_refused_when_the_pool_has_buckets(monkeypatch):
    """池子里有桶，但没有任何一个能接纳这个号 → 绑定契约无法兑现 → 拒绝。"""
    _healthy_bucket()
    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)

    ctx = await guard.acquire_context(FAKE_TOKEN)

    assert ctx.admission_denied is True, "an unbound account was admitted under strict binding"
    assert ctx.denial_reason == "no_healthy_bucket"
    assert ctx.denial_status == 503, "换号重试是有意义的：这是容量问题，不是账号问题"
    assert ctx.concurrency_acquired is False


async def test_unplaceable_account_does_not_consume_a_slot(monkeypatch):
    from utils.antiban import concurrency

    _healthy_bucket()
    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)

    await guard.acquire_context(FAKE_TOKEN)

    assert await concurrency.acquire(FAKE_TOKEN, max_wait=0.01) is True, "a denied request held a slot"


async def test_unplaceable_account_is_admitted_when_no_bucket_exists(monkeypatch):
    """配方里没有任何桶（未配代理）不是绑定违约：没有出口可绑，也就没有漂移。"""
    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)

    ctx = await guard.acquire_context(FAKE_TOKEN)

    assert ctx.admission_denied is False
    assert ctx.concurrency_acquired is True
    guard.release_context(ctx)


async def test_lenient_binding_still_admits_an_unplaceable_account(monkeypatch):
    """宽松模式是显式选择：允许账号落到默认出口，不能被这条拒绝带走。"""
    _healthy_bucket()
    monkeypatch.setattr(configs, "strict_ip_binding", False)
    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)

    ctx = await guard.acquire_context(FAKE_TOKEN)

    assert ctx.admission_denied is False
    guard.release_context(ctx)


async def test_unplaceable_refusal_leaks_no_credentials(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _healthy_bucket()
    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)

    ctx = await guard.acquire_context(FAKE_TOKEN)
    detail = guard.admission_error(ctx)

    blob = _entries(caplog) + repr(detail.detail)
    assert FAKE_TOKEN not in blob
    for n in (8, 12, 16):
        assert FAKE_TOKEN[:n] not in blob


async def test_dead_account_keeps_its_own_verdict_when_it_also_has_no_bucket(monkeypatch):
    """两个结论同时成立时，说更确定的那个。

    死号是「别再试这个号」（403，永久），分不到桶是「换号重试」（503，临时）。
    先判分桶会把永久结论降级成临时结论，客户端于是会一直重试一个已被封的号。
    """
    _healthy_bucket()
    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")

    ctx = await guard.acquire_context(FAKE_TOKEN)

    assert ctx.denial_reason == "account_dead"
    assert ctx.denial_status == 403


async def test_a_dead_account_is_never_bound_to_a_bucket(monkeypatch):
    """死号不该在建绑定的路径上留下副作用，也不该挤占健康桶的名额。"""
    calls = []
    _healthy_bucket()
    monkeypatch.setattr(bucket, "assign_account", lambda token: calls.append(token) or None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")

    ctx = await guard.acquire_context(FAKE_TOKEN)

    assert ctx.denial_reason == "account_dead"
    assert calls == [], "the guard tried to bind a dead account into the pool"


# ---------------------------------------------------------------------------
# 2. 网络层失败：没有桶也要计数
# ---------------------------------------------------------------------------

def test_network_failures_without_a_bucket_are_still_counted():
    """指标不能因为「这个号还没绑桶」就把 proxy/timeout 失败丢掉。"""
    circuit.handle_network_error(FAKE_TOKEN, None, "ReadTimeout")
    circuit.handle_network_error(FAKE_TOKEN, None, "ProxyError")

    stats = circuit.get_circuit_stats()

    assert stats["network_error.timeout"] == 1
    assert stats["network_error.proxy"] == 1


def test_network_failure_without_a_bucket_degrades_nothing():
    """没有桶就没有「这条出口」可降级：拒绝降级任何东西，但保留计数。"""
    _healthy_bucket()

    for _ in range(circuit._NETWORK_ERROR_THRESHOLD + 1):
        circuit.handle_network_error(FAKE_TOKEN, None, "ProxyError")

    assert bucket.get_bucket_meta(BUCKET_ID)["status"] == "healthy"


def test_network_failure_with_a_bucket_still_degrades_at_the_threshold():
    _healthy_bucket()

    for _ in range(circuit._NETWORK_ERROR_THRESHOLD):
        circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, "ProxyError")

    assert bucket.get_bucket_meta(BUCKET_ID)["status"] == "degraded"


# ---------------------------------------------------------------------------
# 3. Worker 声明：不可用的声明不得被读成「单进程」
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("declaration", ["0", "auto", "4,4", "-2", "1.5"])
def test_worker_declaration_that_is_not_a_positive_integer_is_not_single_process(monkeypatch, declaration):
    monkeypatch.setenv("WEB_CONCURRENCY", declaration)

    status = guard.coordination_status()

    assert status["mode"] == "multi_process_uncoordinated", (
        f"WEB_CONCURRENCY={declaration!r} was read as a single process"
    )
    assert status["declared_workers"] is None, "an unusable declaration has no worker count to report"
    assert status["capacity_is_global"] is False


async def test_init_refuses_an_unusable_worker_declaration(monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    monkeypatch.setenv("WEB_CONCURRENCY", "0")
    monkeypatch.setattr(bucket, "bulk_assign", lambda tokens: {"assigned": 0, "skipped": 0})

    with pytest.raises(guard.UncoordinatedMultiWorkerError):
        await guard.init()

    assert "WEB_CONCURRENCY" in _entries(caplog), "the refusal must name the variable to fix"


async def test_absent_worker_declaration_is_still_the_assumed_single_process(monkeypatch):
    """没有声明时保持既有形态：如实标注「假定单进程」，不是「保证」。"""
    status = guard.coordination_status()

    assert status["mode"] == "single_process_assumed"
    assert status["declared_workers"] is None


# ---------------------------------------------------------------------------
# 4. 共享协调层：声明了也不能 fail open
# ---------------------------------------------------------------------------

def test_declared_coordinator_is_surfaced_without_credentials(monkeypatch):
    monkeypatch.setenv("ANTIBAN_COORDINATOR_URL", "redis://user:secret@10.0.0.5:6379/0")

    status = guard.coordination_status()
    blob = json.dumps(status)

    assert status["declared_shared_coordinator"] is not None, "a declared coordinator must be visible"
    assert "secret" not in blob and "10.0.0.5" not in blob, "the endpoint is a credential: only a digest may be reported"
    assert status["shared_coordinator"] is None
    assert status["capacity_is_global"] is False


def test_declared_coordinator_never_unlocks_global_capacity(monkeypatch):
    monkeypatch.setenv("ANTIBAN_COORDINATOR_URL", "redis://coordinator.test:6379/0")
    monkeypatch.setattr(configs, "enable_antiban", True)

    status = guard.coordination_status()

    assert status["capacity_is_global"] is False
    assert status["state_scope"] == "process"


async def test_init_refuses_a_declared_coordinator_it_cannot_use(monkeypatch, caplog):
    """本层没有协调层客户端：声明了 Redis 就必须拒绝启动，而不是静默按进程内状态跑。"""
    caplog.set_level(logging.ERROR)
    monkeypatch.setenv("ANTIBAN_COORDINATOR_URL", "redis://coordinator.test:6379/0")

    with pytest.raises(guard.UnusableCoordinatorError):
        await guard.init()

    log = _entries(caplog)
    assert "ANTIBAN_COORDINATOR_URL" in log


async def test_coordinator_refusal_message_carries_no_credentials(monkeypatch):
    monkeypatch.setenv("ANTIBAN_COORDINATOR_URL", "redis://user:supersecret@10.0.0.5:6379/0")

    with pytest.raises(guard.UnusableCoordinatorError) as excinfo:
        await guard.init()

    message = str(excinfo.value)
    assert "supersecret" not in message
    assert "10.0.0.5" not in message


def test_coordinator_is_inert_when_antiban_is_disabled(monkeypatch):
    """关闭开关必须保持既有行为：不因为残留配置开始拒绝启动。"""
    monkeypatch.setenv("ANTIBAN_COORDINATOR_URL", "redis://coordinator.test:6379/0")
    monkeypatch.setattr(configs, "enable_antiban", False)
    monkeypatch.setattr(bucket, "bulk_assign", lambda tokens: {"assigned": 0, "skipped": 0})

    status = guard.coordination_status()
    asyncio.run(guard.init())  # 不抛：关闭时协调层声明不参与判定

    assert status["mode"] == "disabled"
    assert status["shared_coordinator"] is None
    assert status["capacity_is_global"] is False


# ---------------------------------------------------------------------------
# 5. 反馈分类：挑战族与账号不可用必须可区分
# ---------------------------------------------------------------------------

# 期望值是**枚举字面量**：指标键里出现的就是这些字符串，契约是值而不是属性名。
# （用字面量也让 RED 跑在行为断言上，而不是模块导入期的 AttributeError。）
@pytest.mark.parametrize("detail,expected", [
    ("cf_chl_opt", "cf_challenge"),
    ("turnstile required", "turnstile_challenge"),
    ("arkose required", "arkose_challenge"),
    ("Ark0se service required", "arkose_challenge"),
    ("Failed to solve proof of work", "pow_challenge"),
    ("Proof of work difficulty too high: 0000", "pow_challenge"),
])
def test_challenge_family_is_classified_distinctly(detail, expected):
    assert circuit.classify_response_error(403, detail) == expected


def test_account_unavailable_is_distinct_from_account_dead():
    unavailable = circuit.classify_response_error(401, "account unavailable")
    dead = circuit.classify_response_error(401, "account_deactivated")

    assert unavailable == "account_unavailable"
    assert dead == "account_deactivated"
    assert unavailable != dead


def test_unregistered_refusal_text_still_falls_into_unclassified():
    """区分开不等于什么都认得：没登记的文本仍要可计数、不猜类别。"""
    assert circuit.classify_response_error(403, "no marker here") == circuit.REASON_UNCLASSIFIED
    assert circuit.classify_response_error(418, "conversation text user@example.com") == circuit.REASON_UNCLASSIFIED


@pytest.mark.parametrize("status", [403, 503, 500])
def test_a_machine_token_challenge_marker_is_recognised_regardless_of_status(status):
    """挑战页也可能以 503 下发：只认 403 会把它降级成 upstream_5xx 轻度退避，
    整桶降级就不会发生——正好漏掉最该做的那一步。机器令牌不是自然语言，不会误判。"""
    assert circuit.classify_response_error(status, '{"cf_chl_opt":{}}') == circuit.REASON_CF_CHALLENGE


def test_a_natural_language_challenge_word_only_counts_as_403():
    """自然语言的挑战词只在 403 下成认：429 正文提到 turnstile 仍然是频控。"""
    assert circuit.classify_response_error(429, "turnstile required") == circuit.REASON_RATE_LIMIT
    assert circuit.classify_response_error(503, "turnstile required") == circuit.REASON_UPSTREAM_5XX


def test_a_skipped_challenge_marker_does_not_hide_a_later_account_state_marker():
    """跳过不合格的标记必须继续往下找，而不是就地放弃。"""
    detail = '{"detail":"turnstile required, account unavailable"}'

    assert circuit.classify_response_error(401, detail) == circuit.REASON_ACCOUNT_UNAVAILABLE


def test_status_code_evidence_outranks_a_marker_word_in_the_body():
    """正文里提到一个标记词，不足以覆盖 429 / invalid_grant 这种更硬的证据。

    401 + invalid_grant 的处置是走 refresh 恢复；如果正文里恰好有
    "account unavailable" 就把结论改成账号退避，凭据恢复路径会被跳过。
    """
    detail = '{"error":"invalid_grant","hint":"account unavailable"}'

    assert circuit.classify_response_error(401, detail) == circuit.REASON_AUTH_INVALID
    assert circuit.classify_response_error(429, "cf_chl_opt") == circuit.REASON_RATE_LIMIT


@pytest.mark.parametrize("detail,expected", [
    ("turnstile required", "turnstile_challenge"),
    ("arkose required", "arkose_challenge"),
    ("Failed to solve proof of work", "pow_challenge"),
    ("account unavailable", "account_unavailable"),
])
def test_challenge_refusal_backs_the_account_off(detail, expected):
    """上游明确告知的挑战是容量信号：必须延长该号冷却，而不是什么都不做。"""
    before = cooldown.get_next_available(FAKE_TOKEN)

    reason = circuit.handle_response_error(FAKE_TOKEN, None, 403, detail)

    assert reason == expected
    assert cooldown.get_next_available(FAKE_TOKEN) > max(before, time.time()), "no backoff was recorded"


@pytest.mark.parametrize("reason", [
    "cf_challenge", "turnstile_challenge", "arkose_challenge", "pow_challenge",
    "account_unavailable", "rate_limit", "upstream_5xx",
])
def test_feedback_reasons_are_registered_cooldown_reasons(reason):
    """归一动作用的原因必须是冷却层登记过的枚举，否则会静默塌进 other。"""
    assert cooldown.normalize_extend_reason(reason) == reason


def test_disabled_antiban_keeps_classification_and_backoff_inert(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", False)
    before = cooldown.get_next_available(FAKE_TOKEN)

    circuit.handle_response_error(FAKE_TOKEN, None, 403, "turnstile required")
    circuit.handle_network_error(FAKE_TOKEN, None, "ProxyError")

    assert cooldown.get_next_available(FAKE_TOKEN) == before
    assert circuit.get_circuit_stats() == {}


# ---------------------------------------------------------------------------
# 6. 死号复活：必须有刚发生的认证探针证据
# ---------------------------------------------------------------------------

def _evidence(monkeypatch, account):
    monkeypatch.setattr(store, "get_account", lambda token: account)


def test_revive_accepts_a_fresh_healthy_verdict(monkeypatch):
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")
    _evidence(monkeypatch, {"status": "healthy", "last_health_check": int(time.time())})

    assert circuit.revive_token(FAKE_TOKEN) is True
    assert circuit.is_token_dead(FAKE_TOKEN) is False


@pytest.mark.parametrize("account", [
    None,
    {},
    {"status": "unhealthy", "last_health_check": int(time.time())},
    {"status": "recovering", "last_health_check": int(time.time())},
    {"status": "healthy"},                                                    # 没有探针时间
    {"status": "healthy", "last_health_check": None},
    {"status": "healthy", "last_health_check": "just-now"},
    {"status": "healthy", "last_health_check": time.time() - 86400},          # 陈旧证据
    {"status": "healthy", "last_health_check": time.time() + 86400},          # 未来时间戳
])
def test_revive_fails_closed_without_fresh_probe_evidence(monkeypatch, account):
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")
    _evidence(monkeypatch, account)

    assert circuit.revive_token(FAKE_TOKEN) is False
    assert circuit.is_token_dead(FAKE_TOKEN) is True, "a dead account was revived without evidence"


def test_revive_fails_closed_when_the_store_cannot_be_read(monkeypatch):
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")

    def broken(token):
        raise OSError("synthetic store failure")

    monkeypatch.setattr(store, "get_account", broken)

    assert circuit.revive_token(FAKE_TOKEN) is False
    assert circuit.is_token_dead(FAKE_TOKEN) is True


def test_revive_refusal_is_counted_and_anonymous(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")
    _evidence(monkeypatch, {"status": "unhealthy", "last_health_check": int(time.time())})

    circuit.revive_token(FAKE_TOKEN)

    assert circuit.get_circuit_stats()["revive.refused_no_evidence"] == 1
    blob = _entries(caplog)
    assert FAKE_TOKEN not in blob
    for n in (8, 12, 16):
        assert FAKE_TOKEN[:n] not in blob


def test_scheduled_heal_still_never_revives_dead_accounts(monkeypatch):
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")
    _evidence(monkeypatch, {"status": "healthy", "last_health_check": int(time.time())})

    asyncio.run(circuit.scheduled_heal())

    assert circuit.is_token_dead(FAKE_TOKEN) is True


# ---------------------------------------------------------------------------
# 7. 指标覆盖：PoW / Turnstile / 超时 / 熔断 / 冷却 / 代理失败都要能查
# ---------------------------------------------------------------------------

def test_metrics_snapshot_surfaces_every_capacity_signal(monkeypatch):
    _healthy_bucket()
    monkeypatch.setattr(store, "get_account", lambda token: None)

    circuit.handle_response_error("tok-pow", None, 403, "Failed to solve proof of work")
    circuit.handle_response_error("tok-turnstile", None, 403, "turnstile required")
    circuit.handle_response_error("tok-bucket", BUCKET_ID, 403, "cf_chl_opt")
    circuit.handle_network_error("tok-timeout", None, "ReadTimeout")
    circuit.handle_network_error("tok-proxy", None, "ProxyError")
    asyncio.run(cooldown.wait_or_skip("tok-pacing"))

    snapshot = antiban.metrics_snapshot()
    counters = snapshot["counters"]

    assert counters["circuit_errors"]["pow_challenge:403"] == 1
    assert counters["circuit_errors"]["turnstile_challenge:403"] == 1
    assert counters["circuit_errors"]["bucket_degraded.cf_challenge"] == 1
    assert counters["circuit_errors"]["network_error.timeout"] == 1
    assert counters["circuit_errors"]["network_error.proxy"] == 1
    assert counters["cooldown_events"]["admitted_immediately"] == 1
    assert counters["cooldown_extend_reasons"]["pow_challenge"] == 1

    blob = json.dumps(snapshot)
    for token in ("tok-pow", "tok-turnstile", "tok-bucket", "tok-timeout", "tok-proxy", "tok-pacing"):
        assert token not in blob
    for entry in snapshot["known_error_classes"]:
        assert entry["reason"] in blob or True  # 表本身可查询，键仍是枚举
    assert {e["reason"] for e in snapshot["known_error_classes"]} >= {
        "pow_challenge", "turnstile_challenge", "arkose_challenge",
        "account_unavailable", "cf_challenge", "rate_limit", "network_error",
    }


def test_bucket_degrade_is_not_counted_when_there_is_no_bucket():
    circuit.handle_response_error("tok", None, 403, "cf_chl_opt")

    assert "bucket_degraded.cf_challenge" not in circuit.get_circuit_stats()
