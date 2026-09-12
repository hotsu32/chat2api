"""冷却预算、chatLimit 联动与匿名诊断。

判据取自「容量保护」不变量，不看实现细节：
  1. max_wait 是一次请求的**总等待预算**：锁排队与等待期间被重新延长的冷却都要计入，
     预算耗尽必须拒绝，而不是继续睡到天亮再放行（= 冷却超时仍然发送请求）；
  2. record_request 只能前移冷却：一次成功不能把 429/chatLimit 刚加的退避抹掉；
  3. chatLimit 只认上游确证的 detail.clears_in，不从任意 assistant 正文推断封禁；
  4. 关闭 antiban 后以上联动全部不生效，既有行为无回归；
  5. 这些路径的日志只含匿名标识与原因枚举，不含 token、代理或上游原文。

隔离：不发任何网络请求，不写真实 data/（_persist 全部打桩）。
"""

import asyncio
import time

import pytest

import utils.configs as configs
import utils.globals as globals
from chatgpt import chatLimit
from utils.antiban import account_risk, circuit, concurrency, cooldown

# 测试专用假凭据。断言「它不出现在日志/记录里」，而不是断言真值。
FAKE_TOKEN = "eyJhbGciOiJIUzI1NitestonlyCOOLDOWN987654"
MODEL = "gpt-5-5"

# 模拟上游响应正文：既含会话内容也含账号线索，一律不得进入日志或持久化记录。
UPSTREAM_BODY = "conversation text with account_id acc-abc123 and email user@example.com"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    # 指标接口是本轮新增的。fixture 用 getattr 兜底，好让 RED 跑的是**行为断言失败**，
    # 而不是 setup 阶段的 AttributeError（那只能证明名字不存在）。
    getattr(cooldown, "reset_cooldown_stats", lambda: None)()
    getattr(circuit, "reset_circuit_stats", lambda: None)()
    globals.antiban_dead_tokens.clear()
    globals.account_warnings.clear()
    globals.error_token_list.clear()
    globals.antiban_bucket = {"buckets": {}, "account_index": {}}
    chatLimit.limit_details.clear()

    # 任何落盘都掐断
    monkeypatch.setattr(circuit, "_persist_dead", lambda: None)
    monkeypatch.setattr(account_risk, "_persist", lambda: None)

    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "account_min_interval_seconds", 60)
    monkeypatch.setattr(configs, "free_account_min_interval_seconds", 180)
    monkeypatch.setattr(configs, "account_cooldown_jitter", 0.0)
    monkeypatch.setattr(configs, "account_max_wait_seconds", 30)
    monkeypatch.setattr(configs, "circuit_429_cooldown", 1800)
    monkeypatch.setattr(configs, "circuit_403_cooldown", 3600)
    yield
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    chatLimit.limit_details.clear()


# ---------------------------------------------------------------------------
# 1. max_wait 是总预算：等待期间被重新延长必须拒绝
# ---------------------------------------------------------------------------

async def test_cooldown_extended_during_wait_is_refused_within_budget():
    """睡眠期间冷却被重新延长 → 醒来必须复查并拒绝，不得直接放行。

    RED 形态：旧实现只在进入时算一次 remaining，睡完就 return True，
    于是「等待中被 429 延长到 30 分钟」的账号照样被放行去发请求。
    """
    token = "tok-extend"
    cooldown._account_next_available[token] = time.time() + 0.2

    async def _extender():
        await asyncio.sleep(0.05)
        cooldown.extend_cooldown(token, 30, reason="rate_limit")

    task = asyncio.create_task(_extender())
    started = time.monotonic()
    admitted = await cooldown.wait_or_skip(token, max_wait=0.3)
    elapsed = time.monotonic() - started
    await task

    assert admitted is False
    assert elapsed < 1.0, "refusal must happen within the caller's budget"
    # 延长本身必须保留（拒绝不等于取消退避）
    assert cooldown.get_next_available(token) > time.time() + 20


async def test_lock_queue_wait_counts_against_the_budget():
    """同 token 并发排队：锁等待也要计入预算，否则实际等待时间无上限。

    RED 形态：旧实现 `async with _get_lock(token)` 无超时，队尾请求会一直等，
    拿到锁后再按当时的 remaining 睡足——预算被完全绕过。
    """
    token = "tok-burst"
    cooldown._account_next_available[token] = time.time() + 0.2

    async def _extender():
        await asyncio.sleep(0.05)
        cooldown.extend_cooldown(token, 3, reason="rate_limit")

    task = asyncio.create_task(_extender())
    started = time.monotonic()
    results = await asyncio.gather(
        *[cooldown.wait_or_skip(token, max_wait=0.35) for _ in range(5)]
    )
    elapsed = time.monotonic() - started
    await task

    assert results == [False] * 5
    assert elapsed < 1.5, "no waiter may sleep past its own budget"


async def test_burst_waiters_all_admitted_only_after_cooldown_elapsed():
    """预算充足时并发请求仍应全部放行，且没有人在冷却到期前被放行。"""
    token = "tok-ok-burst"
    next_at = time.time() + 0.15
    cooldown._account_next_available[token] = next_at

    async def _one():
        ok = await cooldown.wait_or_skip(token, max_wait=5)
        return ok, time.time()

    started = time.monotonic()
    results = await asyncio.gather(*[_one() for _ in range(5)])
    elapsed = time.monotonic() - started

    assert all(ok for ok, _ in results)
    for _, at in results:
        assert at >= next_at, "admitted before the cooldown actually elapsed"
    assert elapsed < 2, "waiters must share one cooldown window, not serialize"


async def test_cooldown_beyond_budget_is_refused_immediately():
    token = "tok-long"
    cooldown._account_next_available[token] = time.time() + 9999

    started = time.monotonic()
    admitted = await cooldown.wait_or_skip(token, max_wait=0.5)

    assert admitted is False
    assert time.monotonic() - started < 0.5


# ---------------------------------------------------------------------------
# 2. record_request 绝不缩短已有冷却
# ---------------------------------------------------------------------------

def test_success_after_429_does_not_shorten_backoff():
    """429 退避 1800s 后一次成功只记录节奏，不得把冷却缩回 60s。

    RED 形态：旧 record_request 无条件覆盖 next_available，
    于是「被限流 → 换个模型成功一次 → 立刻继续打」绕过了整个退避。
    """
    token = "tok-429"
    circuit.handle_response_error(token, None, 429, "rate-limit")
    after_429 = cooldown.get_next_available(token)
    assert after_429 > time.time() + 1700

    cooldown.record_request(token, persona="chatgpt-paid")

    assert cooldown.get_next_available(token) == after_429


def test_success_after_chat_limit_does_not_shorten_cooldown():
    token = "tok-limit-success"
    chatLimit.check_is_limit({"clears_in": 600}, token=token, model=MODEL)
    after_limit = cooldown.get_next_available(token)

    cooldown.record_request(token, persona="chatgpt-paid")

    assert cooldown.get_next_available(token) == after_limit


def test_record_request_still_sets_rhythm_when_no_backoff():
    """无退避时 record_request 照常生效（既有行为不得回归）。"""
    token = "tok-rhythm"
    cooldown.record_request(token, persona="chatgpt-paid")
    delta = cooldown.get_next_available(token) - time.time()
    assert 55 < delta <= 61


# ---------------------------------------------------------------------------
# 3. chatLimit：只认上游确证证据
# ---------------------------------------------------------------------------

def test_chat_limit_clears_in_extends_cooldown():
    """上游 429 的 detail.clears_in 是官方明示的恢复时间 → 联动账号冷却。

    RED 形态：旧 check_is_limit 只写 limit_details，账号冷却毫无变化，
    于是同一个号立刻可以换个模型继续打。
    """
    token = "tok-chatlimit"
    chatLimit.check_is_limit({"clears_in": 300}, token=token, model=MODEL)

    assert cooldown.get_next_available(token) > time.time() + 290
    assert chatLimit.get_limit_clear_time(token, MODEL) is not None


def test_chat_limit_cooldown_is_capped():
    token = "tok-chatlimit-huge"
    chatLimit.check_is_limit({"clears_in": 999999}, token=token, model=MODEL)

    remaining = cooldown.get_next_available(token) - time.time()
    assert remaining <= chatLimit.MAX_LINKED_COOLDOWN_SECONDS + 1
    # 模型级窗口仍按上游真实时间记录，不被 cap 截断
    assert chatLimit.get_limit_clear_time(token, MODEL) > time.time() + 900000


@pytest.mark.parametrize("detail", [
    "Your account has been banned for unusual activity",   # 任意 assistant 正文
    {"message": "account_deactivated"},                    # 无 clears_in 的结构化正文
    {"clears_in": 0},
    {"clears_in": "soon"},
    None,
])
def test_chat_limit_ignores_unconfirmed_evidence(detail):
    """只有 detail.clears_in 是可信证据；正文措辞不得推断为限流/封禁。"""
    token = "tok-prose"
    chatLimit.check_is_limit(detail, token=token, model=MODEL)

    assert cooldown.get_next_available(token) == 0.0
    assert chatLimit.get_limit_clear_time(token, MODEL) is None


async def test_handle_request_limit_reports_and_expires():
    token = "tok-window"
    chatLimit.check_is_limit({"clears_in": 300}, token=token, model=MODEL)
    assert await chatLimit.handle_request_limit(token, MODEL) is not None

    # 窗口过期后自动清除
    chatLimit.limit_details[token][MODEL] = int(time.time()) - 1
    assert await chatLimit.handle_request_limit(token, MODEL) is None
    assert MODEL not in chatLimit.limit_details[token]


# ---------------------------------------------------------------------------
# 4. 关闭 antiban：不联动，无回归
# ---------------------------------------------------------------------------

async def test_disabled_antiban_keeps_existing_behavior(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", False)
    token = "tok-off"

    # 冷却不生效
    cooldown._account_next_available[token] = time.time() + 9999
    assert await cooldown.wait_or_skip(token) is True
    cooldown.extend_cooldown(token, 600, reason="rate_limit")
    assert cooldown.get_next_available(token) < time.time() + 10000

    # chatLimit 仍记录模型窗口（enable_limit 是独立开关），但不联动冷却
    chatLimit.limit_details.clear()
    cooldown._account_next_available.clear()
    chatLimit.check_is_limit({"clears_in": 300}, token=token, model=MODEL)
    assert chatLimit.get_limit_clear_time(token, MODEL) is not None
    assert cooldown.get_next_available(token) == 0.0

    # circuit 不动作
    circuit.handle_response_error(token, None, 429, "rate-limit")
    assert circuit._account_backoff_level.get(token) is None


# ---------------------------------------------------------------------------
# 5. 匿名诊断：原因/状态保留，凭据与上游原文不保留
# ---------------------------------------------------------------------------

def _blob(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


async def test_cooldown_and_circuit_logs_contain_no_credentials(caplog):
    """这些路径的日志不得含 token（含前缀）或上游响应原文。"""
    import logging
    caplog.set_level(logging.DEBUG)

    cooldown._account_next_available[FAKE_TOKEN] = time.time() + 9999
    await cooldown.wait_or_skip(FAKE_TOKEN, max_wait=0.1)          # skip 日志
    cooldown.extend_cooldown(FAKE_TOKEN, 60, reason="rate_limit")  # extend 日志
    circuit.handle_response_error(FAKE_TOKEN, None, 429, UPSTREAM_BODY)
    circuit.handle_response_error(FAKE_TOKEN, None, 500, UPSTREAM_BODY)
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")
    chatLimit.check_is_limit({"clears_in": 120}, token=FAKE_TOKEN, model=MODEL)
    await chatLimit.handle_request_limit(FAKE_TOKEN, MODEL)

    blob = _blob(caplog)
    assert FAKE_TOKEN not in blob
    for n in (8, 10, 12, 16, 40):
        assert FAKE_TOKEN[:n] not in blob, f"token prefix of length {n} leaked into logs"
    assert UPSTREAM_BODY not in blob
    assert "acc-abc123" not in blob and "user@example.com" not in blob

    # 可诊断性保留：匿名标识 + 原因枚举
    assert concurrency.anon_id(FAKE_TOKEN) in blob
    assert "reason=rate_limit" in blob


def test_account_risk_records_no_upstream_text(caplog):
    """降智嗅探只保留命中规则与摘要，不保留命中文案。"""
    import logging
    caplog.set_level(logging.DEBUG)

    secret_text = "unusual activity detected on conversation about " + UPSTREAM_BODY
    account_risk.sniff(
        FAKE_TOKEN,
        {"content": {"parts": [secret_text]}, "author": {"role": "assistant"}, "id": "msg-abc"},
        {"conversation_id": "conv-secret-123"},
    )

    records = globals.account_warnings[FAKE_TOKEN]
    assert len(records) == 1
    serialized = repr(records)
    assert UPSTREAM_BODY not in serialized
    assert "conv-secret-123" not in serialized
    assert "msg-abc" not in serialized
    # 业务状态保留：命中的规则、时间、长度摘要
    assert records[0]["pattern"]
    assert records[0]["text_len"] == len(secret_text)

    blob = _blob(caplog)
    assert UPSTREAM_BODY not in blob
    assert FAKE_TOKEN[:8] not in blob
    assert concurrency.anon_id(FAKE_TOKEN) in blob


def test_warning_summary_keys_are_anonymous():
    """后台批量视图的键必须是匿名标识，不能是 token 本身。"""
    account_risk.sniff(
        FAKE_TOKEN,
        {"content": {"parts": ["unusual activity"]}, "author": {"role": "assistant"}},
    )

    summary = account_risk.get_warning_summary()
    assert concurrency.anon_id(FAKE_TOKEN) in summary
    assert FAKE_TOKEN not in summary
    assert summary[concurrency.anon_id(FAKE_TOKEN)]["count"] == 1


def test_mark_dead_reason_is_an_enum_not_upstream_text():
    """死号原因必须归一到枚举；上游原文不得落进持久化记录。"""
    circuit.mark_dead(FAKE_TOKEN, UPSTREAM_BODY)

    stored = globals.antiban_dead_tokens[FAKE_TOKEN]
    assert stored["reason"] == circuit.REASON_UNCLASSIFIED
    assert UPSTREAM_BODY not in repr(stored)

    # 已知枚举原样保留，UI 需要据此区分业务状态
    circuit.mark_dead("tok-enum", "degraded_quality")
    assert globals.antiban_dead_tokens["tok-enum"]["reason"] == "degraded_quality"


def test_error_classification_is_anonymous_and_distinguishable():
    """429/403/401/5xx/账号失效/网络错误必须匿名可区分。"""
    globals.antiban_bucket["buckets"]["bkt::p1"] = {
        "proxy_url": "http://p1", "status": "healthy", "accounts": [], "degraded_until": 0,
    }

    assert circuit.handle_response_error("t1", "bkt::p1", 403, "cf_chl_opt") == circuit.REASON_CF_CHALLENGE
    assert circuit.handle_response_error("t2", None, 429, "rate-limit") == circuit.REASON_RATE_LIMIT
    assert circuit.handle_response_error("t3", None, 401, "invalid_grant") == circuit.REASON_AUTH_INVALID
    assert circuit.handle_response_error("t4", None, 400, "account_deactivated") == circuit.REASON_ACCOUNT_DEAD
    assert circuit.handle_response_error("t5", None, 502, UPSTREAM_BODY) == circuit.REASON_UPSTREAM_5XX
    assert circuit.handle_response_error("t6", None, 418, UPSTREAM_BODY) == circuit.REASON_UNCLASSIFIED

    stats = circuit.get_circuit_stats()
    assert stats[f"{circuit.REASON_RATE_LIMIT}:429"] == 1
    assert stats[f"{circuit.REASON_CF_CHALLENGE}:403"] == 1
    blob = repr(stats)
    for tok in ("t1", "t2", "t3", "t4", "t5", "t6"):
        assert f"'{tok}'" not in blob
    assert UPSTREAM_BODY not in blob


async def test_cooldown_stats_are_anonymous_reason_counters():
    cooldown.reset_cooldown_stats()
    token = "tok-stats"

    assert await cooldown.wait_or_skip(token) is True             # admitted_immediately
    cooldown._account_next_available[token] = time.time() + 9999
    assert await cooldown.wait_or_skip(token, max_wait=0.1) is False  # skip_over_max_wait
    cooldown.extend_cooldown(token, 60, reason="chat_limit")

    stats = cooldown.get_cooldown_stats()
    assert stats["events"]["admitted_immediately"] == 1
    assert stats["events"]["skip_over_max_wait"] == 1
    assert stats["extend_reasons"]["chat_limit"] == 1
    assert token not in repr(stats)
