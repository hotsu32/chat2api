"""ChatService 准入集成：被拒请求必须在任何上游动作之前终止。

与 tests/test_antiban_admission.py 的分工：
  - 那个文件证明 guard/concurrency 这一层的判定与槽位归属；
  - 本文件证明真实 ChatService 路径**真的**不发上游请求，并且失败/取消/重复关闭
    不会泄漏或偷走容量，同时准入日志不含任何凭据。

隔离要求（本文件自己保证，不依赖环境）：
  - configs.proxy_url_list / sentinel_proxy_url_list 清空，routing 绑定清空：
    否则 fp 会从本机真实配置里取到带 user:pass 的代理串并写进日志；
  - fp_map 用测试内构造的假指纹，不读真实 data/；
  - 所有出站出口（curl_cffi Client、get_dpl、verify_token）替换为间谍替身，
    任何一次真实网络调用都会让测试失败。
"""

import asyncio

import pytest
from fastapi import HTTPException

import utils.configs as configs
import utils.globals as globals
from utils.antiban import bucket, circuit, concurrency, cooldown, fingerprint, geo, guard

import chatgpt.ChatService as chat_service_mod
import chatgpt.fp as fp_mod
import chatgpt.services.auth_mixin as auth_mixin_mod
import utils.routing as routing_mod
from chatgpt.ChatService import ChatService

# 测试专用的假凭据/假代理。断言「它们不出现在日志里」，而不是断言真值。
FAKE_TOKEN = "eyJhbGciOiJIUzI1NitestonlyPAYLOAD123456"
FAKE_PROXY = "socks5h://fakeuser:fakesecret456@203.0.113.7:1080"


class _SpyClient:
    """curl_cffi Client 替身：记录构造，任何网络方法被调用即判定失败。"""

    constructed = []
    calls = []

    def __init__(self, *args, **kwargs):
        type(self).constructed.append(kwargs.get("impersonate"))

    async def _forbidden(self, *args, **kwargs):
        type(self).calls.append(args[:1])
        raise AssertionError("upstream request issued on a denied admission path")

    get = post = put = delete = request = _forbidden

    async def close(self):
        return None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    globals.antiban_dead_tokens.clear()
    globals.antiban_bucket = {"buckets": {}, "account_index": {}}
    _SpyClient.constructed = []
    _SpyClient.calls = []

    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)
    monkeypatch.setattr(geo, "get_geo", lambda proxy_url: None)
    monkeypatch.setattr(fingerprint, "ensure_extended", lambda token: {})
    monkeypatch.setattr(circuit, "_persist_dead", lambda: None)

    # 代理来源全部清空/固定：不得从本机真实配置里取到带凭据的代理串
    monkeypatch.setattr(configs, "proxy_url_list", [])
    monkeypatch.setattr(configs, "sentinel_proxy_url_list", [])
    monkeypatch.setattr(chat_service_mod, "sentinel_proxy_url_list", [])
    monkeypatch.setattr(fp_mod.configs, "proxy_url_list", [])
    monkeypatch.setattr(routing_mod, "get_bound_proxy", lambda req_token: None)
    monkeypatch.setattr(fp_mod, "get_bound_proxy", lambda req_token: None)
    monkeypatch.setattr(globals, "persist_fp_token", lambda token: None)
    monkeypatch.setattr(chat_service_mod, "get_fp", _fake_fp)

    # 所有上游出口替换为间谍/空转
    monkeypatch.setattr(chat_service_mod, "Client", _SpyClient)
    monkeypatch.setattr(chat_service_mod, "get_dpl", _noop_async)
    monkeypatch.setattr(ChatService, "validate_model_access", _noop_method)
    monkeypatch.setattr(auth_mixin_mod, "verify_token", _fake_verify)

    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "enable_limit", False)
    monkeypatch.setattr(configs, "account_max_concurrency", 2)
    monkeypatch.setattr(configs, "free_account_max_concurrency", 2)
    monkeypatch.setattr(configs, "account_concurrency_wait_seconds", 0.02)
    monkeypatch.setattr(configs, "account_max_wait_seconds", 1)
    yield
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()


def _fake_fp(req_token):
    """固定假指纹：无代理，不读 data/，不触碰真实号池配置。"""
    return {
        "user-agent": "Mozilla/5.0 (Test) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
        "impersonate": "chrome130",
        "proxy_url": None,
    }


async def _noop_async(*args, **kwargs):
    return True


async def _noop_method(self, *args, **kwargs):
    return None


async def _fake_verify(token):
    return token


REQ = {"model": "gpt-5-5", "messages": [{"role": "user", "content": "hi"}]}


async def _free_slots(token, probe_limit=6):
    """非破坏性探针：数出当前可用槽位，数完原样归还。"""
    held = []
    for _ in range(probe_limit):
        lease = await concurrency.acquire_lease(token, max_wait=0.01)
        if lease is None:
            break
        held.append(lease)
    for lease in held:
        concurrency.release_lease(lease)
    return len(held)


# ---------------------------------------------------------------------------
# 被拒准入：零上游动作
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "setup,expected_status",
    [
        (lambda t: circuit.mark_dead(t, "deactivated"), 403),
        (lambda t: cooldown._account_next_available.__setitem__(t, _far_future()), 503),
    ],
    ids=["dead_account", "cooldown"],
)
async def test_denied_admission_issues_no_upstream_request(setup, expected_status):
    token = "tok-int"
    setup(token)
    svc = ChatService(token)

    with pytest.raises(HTTPException) as exc:
        await svc.set_dynamic_data(dict(REQ))

    assert exc.value.status_code == expected_status
    # 没有构造过 HTTP 客户端，更没有发出任何请求
    assert _SpyClient.constructed == []
    assert _SpyClient.calls == []
    # 被拒不得吃掉容量
    assert await _free_slots(token) == configs.account_max_concurrency

    await svc.close_client()


def _far_future():
    import time
    return time.time() + 9999


async def test_admitted_request_does_reach_client_construction():
    """对照组：健康号可以走到构造上游客户端这一步——证明上面的 0 是拒绝造成的。"""
    svc = ChatService("tok-ok")
    await svc.set_dynamic_data(dict(REQ))

    assert _SpyClient.constructed, "healthy admission should build the upstream client"
    assert svc.antiban_ctx.concurrency_acquired is True

    await svc.close_client()
    assert await _free_slots("tok-ok") == configs.account_max_concurrency


# ---------------------------------------------------------------------------
# 取消 / 重复清理：不泄漏、不偷容量
# ---------------------------------------------------------------------------

async def test_cancelled_request_releases_slot_through_close_client():
    """客户端断连（任务取消）后 close_client 必须归还槽位。"""
    svc = ChatService("tok-cancel")
    started = asyncio.Event()

    async def _run():
        await svc.set_dynamic_data(dict(REQ))
        started.set()
        await asyncio.sleep(3600)  # 模拟在飞 SSE

    task = asyncio.create_task(_run())
    await started.wait()
    assert await _free_slots("tok-cancel") == configs.account_max_concurrency - 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await svc.close_client()

    assert await _free_slots("tok-cancel") == configs.account_max_concurrency


async def test_duplicate_close_client_cannot_steal_inflight_capacity():
    """A 请求重复 close_client 不得放掉 B 请求仍持有的槽位。"""
    svc_a = ChatService("tok-shared")
    svc_b = ChatService("tok-shared")
    await svc_a.set_dynamic_data(dict(REQ))
    await svc_b.set_dynamic_data(dict(REQ))

    await svc_a.close_client()
    await svc_a.close_client()
    await svc_a.close_client()

    # B 仍在飞 → 上限 2 时只应剩 1
    assert await _free_slots("tok-shared") == 1
    await svc_b.close_client()


async def test_close_client_after_denied_admission_creates_no_capacity():
    token = "tok-deadclose"
    circuit.mark_dead(token, "banned")
    svc = ChatService(token)

    with pytest.raises(HTTPException):
        await svc.set_dynamic_data(dict(REQ))
    await svc.close_client()
    await svc.close_client()

    assert await _free_slots(token) == configs.account_max_concurrency


async def test_denial_detail_contains_no_token_material():
    """对外错误信息只说原因，不带任何账号凭据。"""
    circuit.mark_dead(FAKE_TOKEN, "banned")
    svc = ChatService(FAKE_TOKEN)

    with pytest.raises(HTTPException) as exc:
        await svc.set_dynamic_data(dict(REQ))

    detail = str(exc.value.detail)
    assert FAKE_TOKEN not in detail
    assert FAKE_TOKEN[:8] not in detail
    await svc.close_client()


# ---------------------------------------------------------------------------
# 凭据不入日志：token 与代理串（含内嵌 user:pass）
# ---------------------------------------------------------------------------

async def test_request_logs_contain_no_token_or_proxy_secrets(caplog, monkeypatch):
    """走通完整准入路径，断言日志既无 token（含前缀）也无代理凭据。"""
    import logging
    caplog.set_level(logging.DEBUG)

    # 让 antiban 下发一个带账号密码的代理，逼出「代理串进日志」这条路径
    monkeypatch.setattr(bucket, "assign_account", lambda token: "bkt::test")
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: FAKE_PROXY)

    svc = ChatService(FAKE_TOKEN)
    await svc.set_dynamic_data(dict(REQ))
    await svc.close_client()

    blob = "\n".join(r.getMessage() for r in caplog.records)

    assert FAKE_TOKEN not in blob
    for n in (8, 10, 12, 16):
        assert FAKE_TOKEN[:n] not in blob, f"token prefix of length {n} leaked into logs"

    # 代理串整体及其凭据片段都不得出现
    assert FAKE_PROXY not in blob
    assert "fakeuser" not in blob
    assert "fakesecret456" not in blob
    assert "203.0.113.7" not in blob

    # 但仍要保留可诊断性：匿名账号标识与代理摘要应当在
    assert guard.anon_id(FAKE_TOKEN) in blob
    assert guard.redact_proxy(FAKE_PROXY) in blob


async def test_denied_path_logs_contain_no_credentials(caplog):
    """被拒路径同样不得泄露凭据。

    此前 utils/antiban/circuit.py 的 mark_dead 会打 token 前缀，该文件当时不属本
    worker 所有，故登记过一条豁免。circuit.py 现已归本轮所有并改为匿名标识 +
    原因枚举，豁免随之取消：任何一条含 token 材料的日志都判失败。
    """
    import logging
    caplog.set_level(logging.DEBUG)

    circuit.mark_dead(FAKE_TOKEN, "banned")
    svc = ChatService(FAKE_TOKEN)
    with pytest.raises(HTTPException):
        await svc.set_dynamic_data(dict(REQ))
    await svc.close_client()

    messages = [r.getMessage() for r in caplog.records]
    leaks = [m for m in messages if FAKE_TOKEN[:8] in m or FAKE_TOKEN in m]

    assert leaks == [], f"credential leak in logs: {len(leaks)} record(s)"


def test_redact_proxy_keeps_scheme_and_drops_credentials():
    red = guard.redact_proxy(FAKE_PROXY)

    assert red.startswith("socks5h://")
    assert "fakeuser" not in red and "fakesecret456" not in red and "203.0.113.7" not in red
    # 稳定且可区分：同值同摘要，不同值不同摘要
    assert red == guard.redact_proxy(FAKE_PROXY)
    assert red != guard.redact_proxy(FAKE_PROXY.replace("1080", "1081"))
    assert guard.redact_proxy(None) == "proxy:none"


async def test_antiban_disabled_keeps_existing_behavior(monkeypatch):
    """关闭 antiban 后准入不拦截，既有行为无回归。"""
    monkeypatch.setattr(configs, "enable_antiban", False)
    token = "tok-off"
    circuit.mark_dead(token, "banned")
    cooldown._account_next_available[token] = _far_future()

    svc = ChatService(token)
    await svc.set_dynamic_data(dict(REQ))  # 不得抛

    assert svc.antiban_ctx.enabled is False
    await svc.close_client()
