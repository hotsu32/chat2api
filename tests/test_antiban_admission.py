"""Antiban 准入（admission）正确性：冷却拒绝、熔断拒绝、槽位归属与释放。

本文件只测 `utils.antiban.guard.acquire_context / release_context` 与
`utils.antiban.concurrency` 的准入契约，不发任何真实上游请求：
bucket / geo / fingerprint / iprep 全部以 fixture 桩替换。

判据取自「容量保护」不变量，而非实现细节：
  1. 冷却拒绝必须在占并发槽位**之前**返回（占了再拒绝 = 白占别人的容量）；
  2. 死号 / degraded 桶必须拒绝准入并给出明确 HTTP 状态，请求不得继续；
  3. 占槽位之后的任何失败/取消都必须把槽位还回去（否则容量单调泄漏到 0）；
  4. release 必须按「本请求的租约」幂等——重复释放不得偷走别的在飞请求的槽位。

第 4 条是 BoundedSemaphore 单独证明不了的：Bounded 只能防止总量超过上限，
不能防止「A 请求重复 release 把 B 请求的槽位放掉」。所以这里按 ctx 生命周期断言。
"""

import asyncio

import pytest

import utils.configs as configs
import utils.globals as globals
from utils.antiban import bucket, circuit, concurrency, cooldown, fingerprint, geo, guard


@pytest.fixture(autouse=True)
def _isolate_antiban(monkeypatch):
    """清空模块级状态 + 掐断所有外部副作用（网络 / 盘 / SQLite）。"""
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    globals.antiban_dead_tokens.clear()
    globals.antiban_bucket = {"buckets": {}, "account_index": {}}

    # 默认桩：无桶、无 geo、无指纹扩展。需要桶的用例自行覆盖。
    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)
    monkeypatch.setattr(geo, "get_geo", lambda proxy_url: None)
    monkeypatch.setattr(fingerprint, "ensure_extended", lambda token: {})
    # mark_dead 默认不落盘
    monkeypatch.setattr(circuit, "_persist_dead", lambda: None)

    # 代理配置清空：本文件不触碰真实代理，也不让真实代理串有机会进日志
    monkeypatch.setattr(configs, "proxy_url_list", [])
    monkeypatch.setattr(configs, "sentinel_proxy_url_list", [])

    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "account_max_concurrency", 2)
    monkeypatch.setattr(configs, "free_account_max_concurrency", 2)
    monkeypatch.setattr(configs, "account_concurrency_wait_seconds", 0.02)
    monkeypatch.setattr(configs, "account_max_wait_seconds", 1)
    yield
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()


async def _drain_slots(token, probe_limit=6):
    """行为探针：返回该 token 当前还能拿到多少个并发槽位（不看内部字段）。"""
    got = 0
    for _ in range(probe_limit):
        if await concurrency.acquire(token, max_wait=0.01):
            got += 1
        else:
            break
    return got


# ---------------------------------------------------------------------------
# 1. 冷却拒绝：必须在占槽位之前拒绝
# ---------------------------------------------------------------------------

async def test_cooldown_refusal_denies_admission():
    """冷却剩余时间 > max_wait → 准入被拒，请求不得继续。"""
    cooldown._account_next_available["tok-cold"] = asyncio.get_running_loop().time() + 0
    # 用真实时钟：wait_or_skip 读 time.time()
    import time
    cooldown._account_next_available["tok-cold"] = time.time() + 9999

    ctx = await guard.acquire_context("tok-cold")

    assert ctx.admission_denied is True
    assert ctx.denial_reason == "cooldown"
    # 明确 HTTP 状态：账号暂时不可用（可 failover），不是 200 继续发请求
    assert ctx.denial_status == 503
    # 既有 ChatService 契约：未占槽位即拒绝请求，冷却拒绝不得伪装成"已准入"
    assert ctx.concurrency_acquired is False


async def test_cooldown_refusal_does_not_consume_a_slot():
    """冷却拒绝必须在 acquire 之前返回：被拒后该号的并发容量必须仍是满的。

    RED 形态：旧实现丢弃 wait_or_skip 的返回值并继续 acquire，
    于是一个「本该被冷却挡住」的请求白占一个槽位，探针只能拿到 limit-1 个。
    """
    import time
    cooldown._account_next_available["tok-cold"] = time.time() + 9999

    await guard.acquire_context("tok-cold")

    assert await _drain_slots("tok-cold") == configs.account_max_concurrency


# ---------------------------------------------------------------------------
# 2. 死号 / degraded 桶：明确状态拒绝，不占槽位
# ---------------------------------------------------------------------------

async def test_dead_account_denied_with_explicit_status():
    """已熔断的死号不得准入；403 表示「这个号别再重试了」，与可重试的 503 区分。"""
    circuit.mark_dead("tok-dead", "account_deactivated")

    ctx = await guard.acquire_context("tok-dead")

    assert ctx.admission_denied is True
    assert ctx.denial_reason == "account_dead"
    assert ctx.denial_status == 403
    assert ctx.concurrency_acquired is False


async def test_dead_account_does_not_consume_a_slot():
    circuit.mark_dead("tok-dead", "banned")

    await guard.acquire_context("tok-dead")

    assert await _drain_slots("tok-dead") == configs.account_max_concurrency


async def test_degraded_bucket_denied_with_explicit_status(monkeypatch):
    """degraded 桶（CF 挑战 / 代理连续失败）不得继续接收新流量。"""
    monkeypatch.setattr(bucket, "assign_account", lambda token: "bkt::p1")
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: "http://p1")
    monkeypatch.setattr(circuit, "is_bucket_allowed", lambda bucket_id: False)

    ctx = await guard.acquire_context("tok-bkt")

    assert ctx.admission_denied is True
    assert ctx.denial_reason == "bucket_degraded"
    assert ctx.denial_status == 503
    assert ctx.concurrency_acquired is False
    assert await _drain_slots("tok-bkt") == configs.account_max_concurrency


# ---------------------------------------------------------------------------
# 3. 并发超限：拒绝时的 reason/status 必须可区分（匿名诊断）
# ---------------------------------------------------------------------------

async def test_concurrency_exhausted_denied_with_explicit_status():
    assert await concurrency.acquire("tok-busy") is True
    assert await concurrency.acquire("tok-busy") is True

    ctx = await guard.acquire_context("tok-busy")

    assert ctx.admission_denied is True
    assert ctx.denial_reason == "concurrency"
    assert ctx.denial_status == 503
    assert ctx.concurrency_acquired is False


# ---------------------------------------------------------------------------
# 4. 占槽位之后失败 / 取消 → 槽位必须还回去
# ---------------------------------------------------------------------------

async def test_slot_released_when_post_acquire_step_raises(monkeypatch):
    """acquire 之后的步骤抛异常时，槽位必须释放（否则容量单调泄漏）。"""
    def _boom(token):
        raise RuntimeError("fingerprint backend down")

    monkeypatch.setattr(fingerprint, "ensure_extended", _boom)

    with pytest.raises(RuntimeError):
        await guard.acquire_context("tok-leak")

    assert await _drain_slots("tok-leak") == configs.account_max_concurrency


async def test_slot_released_on_cancellation(monkeypatch):
    """请求被取消（客户端断连）时槽位必须释放，且 CancelledError 不得被吞掉。"""
    def _cancel(proxy_url):
        raise asyncio.CancelledError()

    monkeypatch.setattr(geo, "get_geo", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await guard.acquire_context("tok-cancel")

    assert await _drain_slots("tok-cancel") == configs.account_max_concurrency


# ---------------------------------------------------------------------------
# 5. release_context 幂等 + 按本请求租约释放
# ---------------------------------------------------------------------------

async def test_release_context_idempotent_does_not_inflate_capacity():
    """重复 release 不得把容量放大到上限之上。"""
    ctx = await guard.acquire_context("tok-rel")
    assert ctx.concurrency_acquired is True

    guard.release_context(ctx)
    guard.release_context(ctx)
    guard.release_context(ctx)

    assert await _drain_slots("tok-rel") == configs.account_max_concurrency


async def test_release_context_does_not_steal_another_requests_slot():
    """A 请求重复 release 不得释放 B 请求仍持有的槽位。

    这是 BoundedSemaphore 单独证明不了的部分：上限为 2 时，A 已释放、B 仍在飞，
    容量应当只剩 1。若 release 不按租约归属，A 的第二次 release 会把 B 的槽位放掉，
    探针就能拿到 2 个。
    """
    ctx_a = await guard.acquire_context("tok-shared")
    ctx_b = await guard.acquire_context("tok-shared")
    assert ctx_a.concurrency_acquired is True
    assert ctx_b.concurrency_acquired is True

    guard.release_context(ctx_a)
    guard.release_context(ctx_a)  # 重复释放（close_client 被调用两次）

    # B 仍持有一个槽位 → 只应剩 1 个可用
    assert await _drain_slots("tok-shared") == 1


async def test_release_context_after_denied_admission_is_noop():
    """被拒绝的请求也会走 close_client → release_context，不得凭空造出槽位。"""
    import time
    cooldown._account_next_available["tok-cold"] = time.time() + 9999

    ctx = await guard.acquire_context("tok-cold")
    guard.release_context(ctx)
    guard.release_context(ctx)

    assert await _drain_slots("tok-cold") == configs.account_max_concurrency


async def test_lease_releases_the_semaphore_it_acquired():
    """租约必须归还它当初占的那个信号量，而不是「释放时 map 里恰好是谁」。

    重建场景：账号改档 / 重新加载配置会给同一 token 换一个新信号量对象。
    若释放按 token 查表，旧租约就会把新信号量的容量凭空 +1。
    """
    ctx = await guard.acquire_context("tok-swap")
    assert ctx.concurrency_acquired is True

    # 同一 token 换上一个全新的信号量（模拟限额重算/热重载）
    concurrency._account_semaphores.pop("tok-swap", None)
    concurrency._account_limits.pop("tok-swap", None)
    assert await _drain_slots("tok-swap") == configs.account_max_concurrency  # 新信号量已满

    guard.release_context(ctx)  # 归还的是旧信号量

    # 新信号量仍应是满的：旧租约不得把容量灌进新信号量
    assert await _drain_slots("tok-swap") == 0


def test_legacy_over_release_cannot_inflate_capacity():
    """直接 concurrency.release 的历史调用路径也不得放大容量。"""
    async def _run():
        assert await concurrency.acquire("tok-over") is True
        for _ in range(5):
            concurrency.release("tok-over")
        return await _drain_slots("tok-over")

    assert asyncio.run(_run()) == configs.account_max_concurrency


# ---------------------------------------------------------------------------
# 6. 等待期间状态变化：必须复查资格
# ---------------------------------------------------------------------------

async def test_account_marked_dead_during_cooldown_wait_is_rejected(monkeypatch):
    """在 cooldown sleep 期间号被判死 → 醒来必须复查并拒绝，不得放行。"""
    async def _slow_wait(token, persona=None, max_wait=None):
        circuit.mark_dead(token, "deactivated_during_wait")
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(cooldown, "wait_or_skip", _slow_wait)

    ctx = await guard.acquire_context("tok-wait")

    assert ctx.admission_denied is True
    assert ctx.denial_reason == "account_dead"
    assert ctx.denial_status == 403
    assert await _drain_slots("tok-wait") == configs.account_max_concurrency


async def test_bucket_degraded_during_concurrency_wait_is_rejected(monkeypatch):
    """排队等槽位期间桶被降级 → 拿到槽位后必须复查、拒绝并归还槽位。"""
    monkeypatch.setattr(bucket, "assign_account", lambda token: "bkt::p1")
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: "http://p1")

    allowed = {"value": True}
    monkeypatch.setattr(circuit, "is_bucket_allowed", lambda bucket_id: allowed["value"])

    real_acquire = concurrency.acquire_lease

    async def _degrade_then_acquire(token, persona=None, max_wait=None):
        lease = await real_acquire(token, persona, max_wait)
        allowed["value"] = False  # 等待期间桶降级
        return lease

    monkeypatch.setattr(concurrency, "acquire_lease", _degrade_then_acquire)

    ctx = await guard.acquire_context("tok-degrade")

    assert ctx.admission_denied is True
    assert ctx.denial_reason == "bucket_degraded"
    assert ctx.concurrency_acquired is False
    # 复查失败时占用的槽位必须已归还
    assert await _drain_slots("tok-degrade") == configs.account_max_concurrency


# ---------------------------------------------------------------------------
# 7. 日志脱敏：token 前缀不是脱敏
# ---------------------------------------------------------------------------

SECRET_TOKEN = "eyJhbGciOiJIUzI1NisecretpayloadABCDEF"


async def test_admission_logs_contain_no_token_material(caplog, monkeypatch):
    """准入路径的所有日志都不得包含 token 的任何子串（含前缀）。"""
    import logging
    caplog.set_level(logging.DEBUG)

    # 走遍：正常准入 / 并发打满 / 死号
    ctx = await guard.acquire_context(SECRET_TOKEN)
    guard.release_context(ctx)

    held = [await concurrency.acquire_lease(SECRET_TOKEN) for _ in range(configs.account_max_concurrency)]
    denied = await guard.acquire_context(SECRET_TOKEN)
    assert denied.denial_reason == "concurrency"
    for lease in held:
        concurrency.release_lease(lease)

    concurrency.release(SECRET_TOKEN)  # 触发 over-release 日志

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_TOKEN not in blob
    for n in (8, 10, 12, 16):
        assert SECRET_TOKEN[:n] not in blob, f"token prefix of length {n} leaked into logs"


def test_anon_id_is_stable_and_not_reversible():
    """匿名账号标识：同 token 稳定、不同 token 不同、且不含 token 任何前缀。"""
    a = concurrency.anon_id(SECRET_TOKEN)
    b = concurrency.anon_id(SECRET_TOKEN)
    c = concurrency.anon_id(SECRET_TOKEN + "x")

    assert a == b
    assert a != c
    assert SECRET_TOKEN[:8] not in a


# ---------------------------------------------------------------------------
# 8. 放行路径与关闭开关的向后兼容
# ---------------------------------------------------------------------------

async def test_healthy_account_admitted():
    ctx = await guard.acquire_context("tok-ok")

    assert ctx.admission_denied is False
    assert ctx.denial_reason == ""
    assert ctx.denial_status == 0
    assert ctx.concurrency_acquired is True


async def test_antiban_disabled_never_denies(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", False)
    import time
    cooldown._account_next_available["tok-off"] = time.time() + 9999
    circuit.mark_dead("tok-off", "banned")

    ctx = await guard.acquire_context("tok-off")

    assert ctx.enabled is False
    assert ctx.admission_denied is False
    assert ctx.denial_status == 0
    guard.release_context(ctx)  # 不得抛


async def test_admission_error_maps_denial_to_http_exception():
    """调用方（ChatService）据此抛出明确状态码，而不是统统 503。"""
    from fastapi import HTTPException

    circuit.mark_dead("tok-dead", "banned")
    ctx = await guard.acquire_context("tok-dead")

    err = guard.admission_error(ctx)
    assert isinstance(err, HTTPException)
    assert err.status_code == 403

    ok_ctx = await guard.acquire_context("tok-ok")
    assert guard.admission_error(ok_ctx) is None


async def test_admission_denials_are_counted_anonymously():
    """匿名诊断：按原因计数，绝不记录 token。"""
    import time

    guard.reset_admission_stats()
    cooldown._account_next_available["tok-cold"] = time.time() + 9999
    circuit.mark_dead("tok-dead", "banned")

    await guard.acquire_context("tok-cold")
    await guard.acquire_context("tok-dead")

    stats = guard.get_admission_stats()
    assert stats["cooldown"] == 1
    assert stats["account_dead"] == 1
    blob = repr(stats)
    assert "tok-cold" not in blob and "tok-dead" not in blob


async def test_cooldown_extended_while_waiting_for_slot_denies_new_work(monkeypatch):
    """An existing stream can receive 429 while another request queues for it."""
    monkeypatch.setattr(configs, 'account_max_concurrency', 1)
    monkeypatch.setattr(configs, 'account_concurrency_wait_seconds', 1)
    first = await concurrency.acquire_lease('tok-queued')
    pending = asyncio.create_task(guard.acquire_context('tok-queued'))
    await asyncio.sleep(0.01)
    cooldown.extend_cooldown('tok-queued', 60)
    concurrency.release_lease(first)
    ctx = await pending
    try:
        assert ctx.admission_denied
        assert ctx.denial_reason == 'cooldown'
    finally:
        guard.release_context(ctx)
    assert await _drain_slots('tok-queued') == 1
