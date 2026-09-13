"""匿名指标边界、bucket 资格与单/多 Worker 协调可见性。

判据取自三条不变量，不看实现细节：
  1. **指标标签必须是有界枚举。** 调用方传入的任意文本（异常类名、reason 字符串）
     一旦直接进指标键或日志，既是凭据/上游原文的泄漏面，也是高基数炸弹——
     一个每请求都不同的键能让指标后端自己先垮。所以：未注册的值一律归一到
     枚举，且必须仍可诊断（认得 timeout/connect 等已知类别，未知进 other）。
  2. **degraded/dead 的桶不得继续接收新流量。** 代理已从 routing 移除的桶（dead）
     与临时降级的桶（degraded）语义不同、都不能接流量；degraded_until 缺失或畸形
     时必须 fail closed，不能因为读不到时间就当作已恢复。
  3. **不能在多 Worker 下假装安全。** 本层状态是进程内字典，多 Worker 时每号并发
     上限是「每 Worker 一份」，实际并发 = worker 数 × 上限。没有共享协调层就必须
     fail closed：启动期拒绝，而不是把「worker 数 × 上限」当成上限静默放行。

隔离：不发任何网络请求，不写真实 data/（持久化全部打桩）。
"""

import json
import logging
import math
import time

import pytest

import utils.configs as configs
import utils.globals as globals
from utils.antiban import bucket, circuit, concurrency, cooldown, fingerprint, guard

# 在 autouse fixture 打桩之前抓住真函数：`_isolate` 把 bucket.assign_account
# 换成 no-op，好让准入类测试不必真的分桶；日志卫生测试必须跑真实现。
_real_assign_account = bucket.assign_account

# 测试专用假凭据。断言「它不出现在指标/日志里」，而不是断言真值。
FAKE_TOKEN = "eyJhbGciOiJIUzI1NitestonlyMETRICS135790"

# 调用方可能误传的「文本型」标签：含账号线索与上游原文片段
LEAKY_LABEL = "sess-abc123user@example.com/upstream-warning-text"

BUCKET_ID = "bkt::testonly"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    cooldown._cooldown_events.clear()
    cooldown._extend_reasons.clear()
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    circuit._error_events.clear()
    guard._admission_denials.clear()
    globals.antiban_dead_tokens.clear()
    globals.error_token_list.clear()
    globals.antiban_bucket = {"buckets": {}, "account_index": {}}

    monkeypatch.setattr(circuit, "_persist_dead", lambda: None)
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "circuit_429_cooldown", 1800)
    monkeypatch.setattr(configs, "circuit_403_cooldown", 3600)
    monkeypatch.setattr(configs, "account_cooldown_jitter", 0.0)
    monkeypatch.setattr(configs, "account_max_concurrency", 2)
    # 不得从本机真实配置里取到带凭据的代理串
    monkeypatch.setattr(configs, "proxy_url_list", [])
    monkeypatch.setattr(configs, "sentinel_proxy_url_list", [])
    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)
    # 部署形态由环境声明，测试里必须显式清零，否则继承本机 shell 的值
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "WORKERS"):
        monkeypatch.delenv(var, raising=False)
    yield


def _blob(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


async def _noop_async(*args, **kwargs):
    return None


def _put_bucket(bucket_id, status="healthy", degraded_until=0, accounts=None):
    globals.antiban_bucket["buckets"][bucket_id] = {
        "proxy_url": "http://proxy.test",
        "status": status,
        "degraded_until": degraded_until,
        "accounts": accounts or [],
    }


# ---------------------------------------------------------------------------
# 1. 指标标签必须是有界枚举
# ---------------------------------------------------------------------------

# 指标标签的基数上界：枚举集合的大小，用字面量而不是引用被测属性，
# 这样 RED 跑的是行为断言失败而不是 AttributeError（那只能证明名字不存在）。
MAX_LABEL_CARDINALITY = 12


def test_network_error_kind_is_normalized_to_a_bounded_enum():
    """RED 形态：旧实现 `(error_kind or 'unknown')[:24]`，调用方给什么就记什么。"""
    circuit.reset_circuit_stats()

    for kind in (
        "TimeoutError", "ReadTimeout", "ConnectionError", "ConnectionResetError",
        "SSLError", "ProxyError", "gaierror", "SomeVendorSpecificError",
        LEAKY_LABEL, "", None,
    ):
        circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, kind)

    keys = {k.rsplit(".", 1)[-1] for k in circuit.get_circuit_stats() if k.startswith("network_error")}
    assert keys, "network errors must still be counted"
    # 旧实现会先截断到 24 字符再当标签，所以要比**截断后仍存活**的片段，
    # 只比完整字符串会漏掉这类泄漏
    assert "abc123user@example.com" not in repr(keys)
    assert "sess-abc123" not in repr(keys)
    assert len(keys) <= MAX_LABEL_CARDINALITY, f"labels are not a bounded enum: {keys}"


def test_network_error_kind_never_carries_caller_text(caplog):
    """任意文本不得进指标键，也不得进日志（含桶降级那条 warning）。"""
    caplog.set_level(logging.DEBUG)
    circuit.reset_circuit_stats()

    circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, LEAKY_LABEL)
    circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, FAKE_TOKEN)
    # 打满阈值以触发降级日志——那条日志里旧实现回显了 error_kind
    circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, LEAKY_LABEL)

    blob = repr(circuit.get_circuit_stats()) + "\n" + _blob(caplog)
    assert LEAKY_LABEL not in blob
    assert "abc123user@example.com" not in blob
    assert FAKE_TOKEN not in blob
    assert FAKE_TOKEN[:8] not in blob


def test_network_error_kind_keeps_known_classes_distinguishable():
    """归一不等于糊成一团：已知类别必须仍可区分，否则诊断价值归零。"""
    circuit.reset_circuit_stats()

    circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, "TimeoutError")
    circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, "ConnectionResetError")
    circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, "gaierror")
    circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, "SomethingBrandNew")

    keys = circuit.get_circuit_stats()
    assert keys.get("network_error.timeout") == 1
    assert keys.get("network_error.reset") == 1
    assert keys.get("network_error.dns") == 1
    assert keys.get("network_error.other") == 1


def test_network_error_cardinality_is_bounded_under_adversarial_input():
    """500 个各不相同的标签不得产生 500 个指标键（高基数拒绝）。"""
    circuit.reset_circuit_stats()

    for i in range(500):
        circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, f"CustomError{i}-{i * 7919}")

    series = [k for k in circuit.get_circuit_stats() if k.startswith("network_error")]
    assert len(series) <= MAX_LABEL_CARDINALITY, f"{len(series)} distinct series from 500 labels"


def test_extend_cooldown_reason_is_normalized_and_diagnosable():
    """RED 形态：旧实现直接把 reason 当指标键与日志值，调用方文本原样进入二者。"""
    cooldown.reset_cooldown_stats()

    cooldown.extend_cooldown(FAKE_TOKEN, 60, reason="rate_limit")
    cooldown.extend_cooldown(FAKE_TOKEN, 60, reason="chat_limit")
    cooldown.extend_cooldown(FAKE_TOKEN, 60, reason=LEAKY_LABEL)
    cooldown.extend_cooldown(FAKE_TOKEN, 60, reason=None)

    reasons = cooldown.get_cooldown_stats()["extend_reasons"]
    assert reasons.get("rate_limit") == 1
    assert reasons.get("chat_limit") == 1
    assert LEAKY_LABEL not in repr(reasons)
    assert len(reasons) <= MAX_LABEL_CARDINALITY, f"reason is not a bounded enum: {set(reasons)}"


def test_extend_cooldown_logs_carry_no_caller_text(caplog):
    caplog.set_level(logging.DEBUG)

    cooldown.extend_cooldown(FAKE_TOKEN, 60, reason=LEAKY_LABEL)

    blob = _blob(caplog)
    assert LEAKY_LABEL not in blob
    assert "abc123user@example.com" not in blob
    assert FAKE_TOKEN not in blob
    assert FAKE_TOKEN[:8] not in blob
    assert concurrency.anon_id(FAKE_TOKEN) in blob
    assert "reason=" in blob


# ---------------------------------------------------------------------------
# 2. degraded / dead 的桶不得接收新流量
# ---------------------------------------------------------------------------

def test_dead_bucket_refuses_new_traffic():
    """RED 形态：旧 is_bucket_allowed 只认 'degraded'，代理已被移除的 dead 桶照样放行。"""
    _put_bucket(BUCKET_ID, status="dead")
    assert circuit.is_bucket_allowed(BUCKET_ID) is False


def test_degraded_bucket_still_refuses_new_traffic():
    _put_bucket(BUCKET_ID, status="degraded", degraded_until=int(time.time()) + 300)
    assert circuit.is_bucket_allowed(BUCKET_ID) is False


def test_healed_bucket_accepts_traffic_again():
    _put_bucket(BUCKET_ID, status="degraded", degraded_until=int(time.time()) - 1)
    assert circuit.is_bucket_allowed(BUCKET_ID) is True


@pytest.mark.parametrize("bad_until", [None, "soon", float("nan"), float("inf")])
def test_degraded_bucket_with_unusable_deadline_fails_closed(bad_until):
    """读不到恢复时间 ≠ 已恢复。缺时间就当作还在降级里，不能默认放行。"""
    _put_bucket(BUCKET_ID, status="degraded", degraded_until=bad_until)
    assert circuit.is_bucket_allowed(BUCKET_ID) is False


def test_unknown_bucket_status_fails_closed():
    """未知状态不静默放行：bucket.py 新增状态时不能让流量悄悄通过。"""
    _put_bucket(BUCKET_ID, status="quarantined")
    assert circuit.is_bucket_allowed(BUCKET_ID) is False


def test_missing_bucket_metadata_is_a_counted_anomaly_not_an_outage():
    """桶记录整条缺失 = 状态文件损坏/部分写入，不是「桶已知不合格」。

    刻意不放行成静默：必须留下可查询的异常计数。但也不硬拒绝——bucket.py 在代理
    被移除时是保留记录并改标 dead 的，所以这条路径只可能是坏状态；在坏状态上全局
    拒绝会把一次簿记故障放大成整个号池 503。取舍已作为接口建议提交主控。
    """
    circuit.reset_circuit_stats()

    assert circuit.is_bucket_allowed("bkt::not-registered") is True
    assert circuit.get_circuit_stats().get("bucket_unknown") == 1


def test_unassigned_account_is_not_blocked_by_bucket_rules():
    """没有桶（未分配）不是桶不合格，不能因此拒绝。"""
    assert circuit.is_bucket_allowed(None) is True


def test_bucket_denial_reason_distinguishes_dead_from_degraded():
    """dead 与 degraded 的业务含义不同，运维需要能区分。"""
    _put_bucket("bkt::dead", status="dead")
    _put_bucket("bkt::degraded", status="degraded", degraded_until=int(time.time()) + 60)

    assert circuit.bucket_denial_reason("bkt::dead") == "bucket_dead"
    assert circuit.bucket_denial_reason("bkt::degraded") == "bucket_degraded"


async def test_admission_refuses_traffic_for_a_dead_bucket(monkeypatch):
    """端到端：绑定到 dead 桶的账号不得进入上游，且不吃掉容量。"""
    _put_bucket(BUCKET_ID, status="dead")
    monkeypatch.setattr(bucket, "assign_account", lambda token: BUCKET_ID)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: "http://proxy.test")

    ctx = await guard.acquire_context("tok-deadbucket")

    assert ctx.admission_denied is True
    assert ctx.denial_reason == "bucket_dead"
    assert ctx.denial_status == 503
    assert ctx.concurrency_acquired is False
    assert await concurrency.acquire_lease("tok-deadbucket", max_wait=0.01) is not None


async def test_disabled_antiban_does_not_block_on_bucket_state(monkeypatch):
    """关闭 antiban 后既有行为无回归：dead 桶也不拦。"""
    monkeypatch.setattr(configs, "enable_antiban", False)
    _put_bucket(BUCKET_ID, status="dead")

    assert circuit.is_bucket_allowed(BUCKET_ID) is True
    ctx = await guard.acquire_context("tok-off")
    assert ctx.admission_denied is False
    guard.release_context(ctx)


# ---------------------------------------------------------------------------
# 3. 可查询快照（JSON / Prometheus）
# ---------------------------------------------------------------------------

def test_metrics_snapshot_is_json_serializable_and_anonymous():
    from utils.antiban import metrics_snapshot

    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")
    _put_bucket(BUCKET_ID, status="degraded", degraded_until=int(time.time()) + 60)
    circuit.handle_response_error(FAKE_TOKEN, BUCKET_ID, 429, LEAKY_LABEL)
    circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, LEAKY_LABEL)
    cooldown.extend_cooldown(FAKE_TOKEN, 60, reason=LEAKY_LABEL)

    snap = metrics_snapshot()
    blob = json.dumps(snap, ensure_ascii=False)  # 必须可序列化，否则无法暴露给 JSON/Prometheus

    assert FAKE_TOKEN not in blob
    assert FAKE_TOKEN[:8] not in blob
    assert BUCKET_ID not in blob
    assert LEAKY_LABEL not in blob
    assert "abc123user@example.com" not in blob


def test_metrics_snapshot_reports_counts_not_identifiers():
    from utils.antiban import metrics_snapshot

    for i in range(3):
        circuit.mark_dead(f"synthetic-token-{i}", "degraded_quality")
    _put_bucket(BUCKET_ID, status="dead")

    snap = metrics_snapshot()
    dead = snap["counts"]["dead_accounts"]
    assert dead == 3
    assert isinstance(dead, int)


def test_metrics_snapshot_exposes_known_error_classes_for_diagnosis():
    """已知错误类别要能被查询：哪类错误映射到什么动作，不能只存在于代码里。"""
    from utils.antiban import metrics_snapshot

    classes = metrics_snapshot()["known_error_classes"]
    by_reason = {c["reason"]: c for c in classes}

    assert circuit.REASON_CF_CHALLENGE in by_reason
    assert circuit.REASON_RATE_LIMIT in by_reason
    assert circuit.REASON_AUTH_INVALID in by_reason
    assert circuit.REASON_ACCOUNT_DEAD in by_reason

    assert by_reason[circuit.REASON_RATE_LIMIT]["action"] == "extend_cooldown"
    assert by_reason[circuit.REASON_ACCOUNT_DEAD]["action"] == "mark_dead"
    for entry in classes:
        assert set(entry) == {"reason", "signal", "action"}


def test_metrics_snapshot_reports_zeroed_counters_when_disabled(monkeypatch):
    from utils.antiban import metrics_snapshot

    monkeypatch.setattr(configs, "enable_antiban", False)
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")  # 关闭时不动作

    snap = metrics_snapshot()
    assert snap["enabled"] is False
    assert snap["counts"]["dead_accounts"] == 0
    assert all(v == 0 for v in snap["counters"]["circuit_errors"].values())


# ---------------------------------------------------------------------------
# 4. 单/多 Worker：不能默认声称多 Worker 安全
# ---------------------------------------------------------------------------

def test_coordination_reports_single_process_when_one_worker_declared(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    status = guard.coordination_status()
    assert status["mode"] == guard.COORDINATION_SINGLE_PROCESS
    assert status["capacity_is_global"] is False


def test_coordination_marks_multi_worker_as_uncoordinated(monkeypatch):
    """RED 形态：旧实现完全没有这个概念，多 Worker 下静默按每 Worker 上限放行。"""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    status = guard.coordination_status()
    assert status["mode"] == guard.COORDINATION_MULTI_PROCESS_UNCOORDINATED
    assert status["declared_workers"] == 4
    assert status["capacity_is_global"] is False


def test_coordination_never_claims_global_capacity_without_a_coordinator(monkeypatch):
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "WORKERS"):
        monkeypatch.delenv(var, raising=False)
    assert guard.coordination_status()["capacity_is_global"] is False


def test_coordination_reports_disabled_when_antiban_off(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", False)
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert guard.coordination_status()["mode"] == guard.COORDINATION_DISABLED


async def test_init_refuses_declared_multi_worker_without_a_coordinator(monkeypatch, caplog, _isolate):
    """多 Worker 无协调层必须启动期拒绝（fail closed），不能降级放行。

    RED 形态：旧实现只打一行 ERROR 然后照常启用——真实每号并发会是
    worker 数 × 上限，但对外仍宣称受保护。
    """
    import asyncio

    from utils.antiban import version_check

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setattr(bucket, "bulk_assign", lambda tokens: {"assigned": 0, "skipped": 0})
    # 不得真的发版本探测请求（拒绝发生在探测之前，这里只是兜底）
    monkeypatch.setattr(version_check, "probe_and_compare", _noop_async)

    with pytest.raises(guard.UncoordinatedMultiWorkerError):
        await guard.init()
    await asyncio.sleep(0)

    blob = _blob(caplog)
    assert "multi_process_uncoordinated" in blob
    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert records, "拒绝启动前必须留下 ERROR 级证据，说明为什么拒绝"


async def test_init_proceeds_when_exactly_one_worker_is_declared(monkeypatch, caplog, _isolate):
    """单 Worker 是当前shipped 形态，必须照常启用。"""
    from utils.antiban import version_check

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    assigned = []
    monkeypatch.setattr(
        bucket, "bulk_assign", lambda tokens: assigned.append(list(tokens)) or {"assigned": 0, "skipped": 0}
    )
    monkeypatch.setattr(version_check, "probe_and_compare", _noop_async)

    await guard.init()

    assert assigned, "单 Worker 下 bulk_assign 必须照常执行"
    assert "coordination=single_process " in _blob(caplog)


async def test_init_proceeds_when_no_worker_count_is_declared(monkeypatch, caplog, _isolate):
    """未声明 worker 数沿用 shipped 默认（单进程），不得因此拒绝启动。"""
    from utils.antiban import version_check

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(bucket, "bulk_assign", lambda tokens: {"assigned": 0, "skipped": 0})
    monkeypatch.setattr(version_check, "probe_and_compare", _noop_async)

    await guard.init()

    assert "coordination=single_process_assumed " in _blob(caplog)


def test_metrics_snapshot_exposes_coordination_status(monkeypatch):
    from utils.antiban import metrics_snapshot

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert metrics_snapshot()["coordination"]["mode"] == guard.COORDINATION_MULTI_PROCESS_UNCOORDINATED


# ---------------------------------------------------------------------------
# 5. 死号不被本层绕过（恢复走号池契约，不在此复活）
# ---------------------------------------------------------------------------

def test_no_success_or_error_path_clears_the_dead_list():
    """成功 / 网络错误 / 冷却路径都不得把 dead 记录抹掉。"""
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")
    assert circuit.is_token_dead(FAKE_TOKEN) is True

    circuit.handle_response_success(FAKE_TOKEN)
    circuit.handle_network_error(FAKE_TOKEN, BUCKET_ID, "TimeoutError")
    circuit.handle_response_error(FAKE_TOKEN, BUCKET_ID, 200, "ok")
    cooldown.record_request(FAKE_TOKEN)
    cooldown.extend_cooldown(FAKE_TOKEN, 60, reason="rate_limit")

    assert circuit.is_token_dead(FAKE_TOKEN) is True
    assert globals.antiban_dead_tokens[FAKE_TOKEN]["reason"] == "account_deactivated"


async def test_scheduled_heal_never_revives_dead_accounts():
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")
    _put_bucket(BUCKET_ID, status="degraded", degraded_until=int(time.time()) - 1)

    await circuit.scheduled_heal()

    assert circuit.is_token_dead(FAKE_TOKEN) is True


def test_metrics_snapshot_does_not_expose_dead_token_identity():
    from utils.antiban import metrics_snapshot

    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")
    snap = metrics_snapshot()
    assert FAKE_TOKEN not in json.dumps(snap, ensure_ascii=False)
    assert snap["counts"]["dead_accounts"] == 1


# ---------------------------------------------------------------------------
# 6. 日志卫生：账号标识一律匿名摘要，token 前缀不算脱敏
# ---------------------------------------------------------------------------

def test_bucket_assignment_logs_carry_no_token_prefix(monkeypatch, caplog, _isolate):
    """批量分配发生在启动期，正是冒烟抓日志的时刻。

    RED 形态：旧实现打 `token[:12]`。前缀可直接比对/关联账号，属于凭据泄漏面。
    """
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(configs, "strict_ip_binding", True)
    monkeypatch.setattr(bucket, "_persist", lambda: None)
    monkeypatch.setattr(bucket, "update_single_binding", lambda *a, **kw: None)
    monkeypatch.setattr(bucket, "assign_account", _real_assign_account)
    _put_bucket(BUCKET_ID, status="healthy")

    assert bucket.assign_account(FAKE_TOKEN) == BUCKET_ID

    blob = _blob(caplog)
    assert FAKE_TOKEN not in blob
    for n in (6, 8, 10, 12, 16):
        assert FAKE_TOKEN[:n] not in blob, f"token prefix of length {n} leaked into bucket logs"
    assert concurrency.anon_id(FAKE_TOKEN) in blob


def test_bucket_refusal_log_carries_no_token_prefix(monkeypatch, caplog, _isolate):
    """所有桶都不可用时的拒绝日志同样只能有匿名标识。"""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(configs, "strict_ip_binding", True)
    monkeypatch.setattr(bucket, "_persist", lambda: None)
    monkeypatch.setattr(bucket, "assign_account", _real_assign_account)
    monkeypatch.setattr(bucket, "_pick_least_loaded_healthy", lambda plan_type: None)

    assert bucket.assign_account(FAKE_TOKEN) is None

    blob = _blob(caplog)
    for n in (6, 8, 10, 12, 16):
        assert FAKE_TOKEN[:n] not in blob, f"token prefix of length {n} leaked into bucket logs"
    assert concurrency.anon_id(FAKE_TOKEN) in blob


def test_fingerprint_extension_log_carries_no_token_prefix(monkeypatch, caplog, _isolate):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(fingerprint, "_persist_fp", lambda: None)

    fingerprint.ensure_extended(FAKE_TOKEN)

    blob = _blob(caplog)
    assert FAKE_TOKEN not in blob
    for n in (6, 8, 10, 12, 16):
        assert FAKE_TOKEN[:n] not in blob, f"token prefix of length {n} leaked into fingerprint logs"
    assert concurrency.anon_id(FAKE_TOKEN) in blob
