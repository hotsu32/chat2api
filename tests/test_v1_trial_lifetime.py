"""产品入口 ``/v1/chat/completions`` 的试用账与账号租约生命周期。

这个文件钉的是**接线**，不是 ``utils.trials`` 的余额语义（那是 ``test_trials.py``）：

  - 请求准入必须先过套餐/试用资格闸，再原子预留一次试用额度；
  - 只有「项目定义的成功完成信号 + 响应完整交付」才扣次数；
  - 上游报错、空流、生成器抛错、客户端断连、发送失败都不得扣次数；
  - 账号并发槽位（租约）与试用额度必须跟着**响应生命周期**结束，而不是跟着
    ``BackgroundTask``：Starlette 的流式响应在正文迭代器抛错或发送失败时会从任务组
    里直接抛出，背景任务根本不会执行 —— 那是租约泄漏的真实路径。

失效测试（写实现之前应当失败）：
  - 试用用户跑完一次成功生成后 ``used`` 仍为 0（未接线）；
  - 生成器抛错 / 非流式发送失败时 ``close_client`` 从未被调用（租约泄漏）。

ChatService 替身是刻意的：这里要证明的是入口层的生命周期接线，真实 ChatService
的准入行为由 ``test_antiban_chatservice_admission.py`` 覆盖。
"""

import asyncio
import json
import types

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

import app  # noqa: F401  -- app.py 先注册路由，api.chat2api 才能独立导入
import api.chat2api as chat_api
import utils.trials as trials
from utils.antiban.guard import AntibanContext

_PW = "pbkdf2_sha256$1$00$00"
TRIAL_EMAIL = "v1-lifetime@example.com"
TRIAL_SEED = "seed-v1-lifetime"


def _registered(db, email=TRIAL_EMAIL, seed=TRIAL_SEED, status="active"):
    """走真实注册 DAO：建 user_auth 行 + 发 3 次 Plus 试用。"""
    db.create_user_with_trial(email, password_hash=_PW, seed=seed, status=status,
                              trial_tier=trials.TRIAL_TIER,
                              trial_total=trials.SIGNUP_TRIAL_COUNT)
    return seed


class _FakeChatService:
    """ChatService 替身：只记录生命周期终点与流式回写字段。"""

    def __init__(self):
        self.closed = 0
        self.librechat_conv_id = None

    async def close_client(self):
        self.closed += 1


def _install_service(monkeypatch, result):
    """把上游链路替换成确定性替身，返回 (service, 调用记录)。"""
    service = _FakeChatService()
    calls = []

    async def fake_process(request_data, req_token):
        calls.append((request_data, req_token))
        return service, result

    monkeypatch.setattr(chat_api, "process", fake_process)
    return service, calls


def _install_no_retry(monkeypatch):
    """去掉重试：准入失败的重试等待不属于本文件的观测对象。"""
    async def _once(func, *args, **kwargs):
        return await func(*args, **kwargs)

    monkeypatch.setattr(chat_api, "async_retry", _once)


def _request(payload):
    body = json.dumps(payload).encode()
    state = {"sent": False}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()

    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
        "query_string": b"", "headers": [], "scheme": "http",
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        "asgi": {"version": "3.0", "spec_version": "2.3"},
    }
    return Request(scope, receive)


def _chat_payload(stream=False):
    return {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": stream}


class _Driver:
    """按 ASGI 语义驱动响应对象，可注入断连 / 发送失败。"""

    def __init__(self, outcome="complete"):
        self.outcome = outcome
        self.sent = []
        self.body_started = asyncio.Event()

    async def receive(self):
        if self.outcome == "disconnect":
            await self.body_started.wait()
            return {"type": "http.disconnect"}
        await asyncio.Future()

    async def send(self, message):
        if self.outcome == "send_error" and message["type"] == "http.response.body":
            raise OSError("synthetic send failure")
        if message["type"] == "http.response.body":
            self.body_started.set()
        self.sent.append(message["type"])

    async def run(self, response):
        scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
                 "headers": [], "asgi": {"version": "3.0"}}
        if self.outcome in ("send_error", "body_error"):
            # 流式响应在任务组里抛出（BaseExceptionGroup）；非流式响应直接冒泡。
            with pytest.raises((BaseExceptionGroup, OSError, HTTPException)):
                await response(scope, self.receive, self.send)
        else:
            await response(scope, self.receive, self.send)


async def _invoke(seed, payload, outcome="complete"):
    driver = _Driver(outcome)
    response = await chat_api.send_conversation(
        _request(payload), HTTPAuthorizationCredentials(scheme="Bearer", credentials=seed))
    await driver.run(response)
    return driver


def _sse(delta, finish_reason=None):
    return "data: " + json.dumps({
        "id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 0,
        "model": "gpt-4o",
        "choices": [{"index": 0, "delta": delta, "logprobs": None,
                     "finish_reason": finish_reason}],
    }) + "\n\n"


def _completion(content="Hello, world"):
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "gpt-4o",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _stream(*, text="Hello, world", terminal=True, duplicates=False, fail_after=None,
            error_frame=False, pause=False, pause_after_terminal=False):
    """与 chatgpt/chatFormat.py 真实形状一致的 OpenAI 兼容分片序列。"""
    async def _gen():
        yield _sse({"role": "assistant", "content": ""})
        if pause:
            # 上游还在生成（真实流在这里会阻塞等下一个分片），
            # 断连/取消必须能在这一点打断它。
            await asyncio.Future()
        if fail_after == "open":
            raise RuntimeError("synthetic upstream failure")
        if error_frame:
            yield "data: " + json.dumps({"error": "synthetic upstream error"}) + "\n\n"
            return
        if text:
            yield _sse({"content": text})
        if fail_after == "text":
            raise RuntimeError("synthetic upstream failure")
        if terminal:
            yield _sse({"content": ""}, "stop")
            if pause_after_terminal:
                # 完成信号已发出，但 [DONE] 还没发：客户端此时断连。
                await asyncio.Future()
            if duplicates:
                yield _sse({"content": ""}, "stop")
                yield _sse({"content": ""}, "stop")
        yield "data: [DONE]\n\n"

    return _gen()


def _assert_is_streaming(res):
    assert isinstance(res, types.AsyncGeneratorType)


# ------------------------------------------------------------ 流式：成功

@pytest.mark.parametrize("duplicates", [False, True])
async def test_stream_success_settles_exactly_once(db, monkeypatch, duplicates):
    """成功流：扣一次；重复的完成信号不得扣第二次。"""
    seed = _registered(db)
    service, calls = _install_service(monkeypatch, _stream(duplicates=duplicates))
    _assert_is_streaming(_stream())

    await _invoke(seed, _chat_payload(stream=True))

    state = trials.trial_state(TRIAL_EMAIL)
    assert (state["used"], state["remaining"], state["reserved"]) == (1, 2, 0)
    assert service.closed == 1
    assert len(calls) == 1


# ------------------------------------------------------------ 流式：失败

@pytest.mark.parametrize("outcome,expected", [
    ("stream_error", {"text": "partial", "terminal": False}),      # 上游报错后 [DONE] 收尾
    ("error_frame", {"error_frame": True}),                        # 上游直接下发 error 分片
    ("empty", {"text": ""}),                                       # 空流
])
async def test_stream_without_completion_signal_is_not_charged(db, monkeypatch, outcome, expected):
    """没有项目定义的完成信号（终止分片 + 正文）就不是一次成功生成。"""
    seed = _registered(db)
    _install_service(monkeypatch, _stream(**expected))

    await _invoke(seed, _chat_payload(stream=True))

    state = trials.trial_state(TRIAL_EMAIL)
    assert (state["used"], state["remaining"], state["reserved"]) == (0, 3, 0)


async def test_generator_exception_releases_trial_and_account_lease(db, monkeypatch):
    """正文迭代器抛错：额度退回，租约也必须释放（Starlette 会跳过背景任务）。"""
    seed = _registered(db)
    service, _calls = _install_service(monkeypatch, _stream(fail_after="text"))

    await _invoke(seed, _chat_payload(stream=True), outcome="body_error")

    assert service.closed == 1
    state = trials.trial_state(TRIAL_EMAIL)
    assert (state["used"], state["remaining"], state["reserved"]) == (0, 3, 0)


async def test_client_disconnect_releases_trial_and_account_lease(db, monkeypatch):
    """客户端断连：不扣次数，且租约必须归还（否则容量单调泄漏）。"""
    seed = _registered(db)
    service, _calls = _install_service(monkeypatch, _stream(pause=True))

    await _invoke(seed, _chat_payload(stream=True), outcome="disconnect")

    assert service.closed == 1
    state = trials.trial_state(TRIAL_EMAIL)
    assert (state["used"], state["remaining"], state["reserved"]) == (0, 3, 0)


async def test_disconnect_after_completion_still_releases(db, monkeypatch):
    """完成信号已出现但响应没能发完：不算「用户收到了一次完整回复」。"""
    seed = _registered(db)
    service, _calls = _install_service(monkeypatch, _stream(pause_after_terminal=True))

    await _invoke(seed, _chat_payload(stream=True), outcome="disconnect")

    # 终止分片已经发到客户端，但整段响应没有发完（[DONE] 还没写出去）。
    assert service.closed == 1
    assert trials.trial_state(TRIAL_EMAIL)["used"] == 0


async def test_send_failure_releases_trial_and_account_lease(db, monkeypatch):
    """发送失败：租约必须释放，背景任务在这条路径上不会执行。"""
    seed = _registered(db)
    service, _calls = _install_service(monkeypatch, _stream())

    await _invoke(seed, _chat_payload(stream=True), outcome="send_error")

    assert service.closed == 1
    state = trials.trial_state(TRIAL_EMAIL)
    assert (state["used"], state["remaining"], state["reserved"]) == (0, 3, 0)


# ------------------------------------------------------------ 非流式

async def test_non_stream_success_settles_once(db, monkeypatch):
    seed = _registered(db)
    service, _calls = _install_service(monkeypatch, _completion())

    await _invoke(seed, _chat_payload())

    state = trials.trial_state(TRIAL_EMAIL)
    assert (state["used"], state["remaining"]) == (1, 2)
    assert service.closed == 1


async def test_non_stream_empty_body_is_not_charged(db, monkeypatch):
    """空正文不是一次成功生成（format_not_stream_response 亦以 403 拒绝）。"""
    seed = _registered(db)
    _install_service(monkeypatch, _completion(content=""))

    await _invoke(seed, _chat_payload())

    assert trials.trial_state(TRIAL_EMAIL)["used"] == 0


async def test_non_stream_send_failure_releases_trial_and_lease(db, monkeypatch):
    seed = _registered(db)
    service, _calls = _install_service(monkeypatch, _completion())

    await _invoke(seed, _chat_payload(), outcome="send_error")

    assert service.closed == 1
    assert trials.trial_state(TRIAL_EMAIL)["used"] == 0


async def test_upstream_failure_before_response_releases_trial(db, monkeypatch):
    """上游拒绝（403）发生在响应之前：额度必须立刻退回，不能挂成在途预留。"""
    seed = _registered(db)

    async def failing_process(request_data, req_token):
        raise HTTPException(status_code=403, detail="No content in the message.")

    monkeypatch.setattr(chat_api, "process", failing_process)

    with pytest.raises(HTTPException) as failure:
        await chat_api.send_conversation(
            _request(_chat_payload()),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=seed))
    assert failure.value.status_code == 403

    state = trials.trial_state(TRIAL_EMAIL)
    assert (state["used"], state["remaining"], state["reserved"]) == (0, 3, 0)


async def test_capacity_denial_releases_trial_and_lease(db, monkeypatch):
    """共享容量分不到号（503）时：不扣次数、不留租约。"""
    seed = _registered(db)
    _install_no_retry(monkeypatch)
    service = _FakeChatService()

    async def denied_process(request_data, req_token):
        raise HTTPException(status_code=503, detail="Account capacity unavailable")

    monkeypatch.setattr(chat_api, "process", denied_process)

    with pytest.raises(HTTPException) as failure:
        await chat_api.send_conversation(
            _request(_chat_payload()),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=seed))
    assert failure.value.status_code == 503
    assert service.closed == 0  # 从未构造出服务，也就没有可归还的租约

    state = trials.trial_state(TRIAL_EMAIL)
    assert (state["used"], state["remaining"], state["reserved"]) == (0, 3, 0)


# ------------------------------------------------------------ 资格闸

async def test_exhausted_trial_is_denied_before_upstream(db, monkeypatch):
    """三次用完后第 4 次必须在打上游之前被拒，且不做任何预留。"""
    seed = _registered(db)
    for _ in range(trials.SIGNUP_TRIAL_COUNT):
        assert trials.settle(trials.reserve(seed), seed) is True
    service, calls = _install_service(monkeypatch, _stream())

    with pytest.raises(HTTPException) as failure:
        await chat_api.send_conversation(
            _request(_chat_payload(stream=True)),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=seed))

    assert failure.value.status_code == 402
    assert calls == []
    assert service.closed == 0
    assert trials.trial_state(TRIAL_EMAIL)["used"] == trials.SIGNUP_TRIAL_COUNT


async def test_paid_user_is_not_charged_to_trial(db, monkeypatch):
    """有效付费订单：不占试用账（付费按有效期用，试用不参与也不被消耗）。"""
    seed = _registered(db)
    db.create_order("ord-paid", TRIAL_EMAIL, "plus-shared-1m", "39", status="pending")
    db.activate_order("ord-paid", 2_000_000_000)
    service, _calls = _install_service(monkeypatch, _completion())

    await _invoke(seed, _chat_payload())

    state = trials.trial_state(TRIAL_EMAIL)
    assert (state["used"], state["remaining"]) == (0, 3)
    assert service.closed == 1


async def test_inactive_account_is_denied_and_never_charged(db, monkeypatch):
    """未验证 / 被封禁的账号不能用试用额度（402，不上游、不预留）。"""
    seed = _registered(db, status="banned")
    service, calls = _install_service(monkeypatch, _completion())

    with pytest.raises(HTTPException) as failure:
        await chat_api.send_conversation(
            _request(_chat_payload()),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=seed))

    assert failure.value.status_code == 402
    assert calls == []
    assert service.closed == 0


async def test_operator_token_stays_ungated(db, monkeypatch):
    """无 user_auth 行的运营者 seed / 直传 token：不设限、不占试用账。"""
    service, calls = _install_service(monkeypatch, _completion())

    await _invoke("operator-raw-token", _chat_payload())

    assert len(calls) == 1
    assert service.closed == 1
    assert trials.trial_state(TRIAL_EMAIL)["granted"] is False
    assert db.count_open_trial_reservations(TRIAL_EMAIL) == 0


# ------------------------------------------------------------ Plus 分池

def test_trial_seed_is_routed_only_to_plus_accounts(db, monkeypatch):
    """试用的档位就是 Plus：号组范围是硬边界，不借 Free 号，也不吃 Plus 以外的池子。

    这一条不重测选号算法（见 test_pool_tier_contract.py），只钉「试用身份 → Plus 号组」
    这层推导：试用的 ticket 是给新用户的 Plus 入场券，不是「随便哪个号都行」。
    """
    import utils.configs as configs
    import utils.globals as globals
    from chatgpt import authorization as auth

    monkeypatch.setattr(globals, "seed_map", {})
    monkeypatch.setattr(globals, "error_token_list", [])
    monkeypatch.setattr(globals, "antiban_dead_tokens", {})
    monkeypatch.setattr(configs, "max_shared_seeds_per_account", 2, raising=False)
    monkeypatch.setattr(configs, "auto_seed", True, raising=False)

    seed = _registered(db)
    db.upsert_user(seed, status="active")
    db.upsert_account("free-acct", plan_type="free", status="healthy")

    # 只有 Free 号可用：分不到号（fail-closed），而不是静默降级去吃 Free 池
    assert auth.get_req_token(seed) == ""
    assert db.get_user(seed)["current_account"] in ("", None)

    db.upsert_account("plus-acct", plan_type="plus", status="healthy")
    assert auth.get_req_token(seed) == "plus-acct"
    assert db.get_user(seed)["plan_type"] == "plus"


# ------------------------------------------------------------ 试用账状态机

def test_attempt_is_terminal_once(db):
    """终态一次：重复/迟到的完成或失败回调既不多扣也不退款。"""
    seed = _registered(db)
    attempt = trials.TrialAttempt(trials.reserve(seed), seed)

    assert attempt.finish(delivered=False) is False         # 交付失败 → 退回
    assert attempt.finish(delivered=True) is False          # 迟到（但更"成功"）的结论不生效
    assert attempt.settle() is False
    assert attempt.release() is False
    assert trials.trial_state(TRIAL_EMAIL)["used"] == 0


def test_attempt_requires_both_completion_and_delivery(db):
    """只有「上游完成」+「客户端确实收到」同时成立才扣次数。"""
    seed = _registered(db)
    attempt = trials.TrialAttempt(trials.reserve(seed), seed)

    assert attempt.finish(delivered=True) is False          # 没有完成信号 → 退回
    assert attempt.finish(delivered=True) is False          # 已是终态
    assert trials.trial_state(TRIAL_EMAIL)["used"] == 0

    second = trials.TrialAttempt(trials.reserve(seed), seed)
    second.mark_completed()
    assert second.finish(delivered=False) is False          # 完成但没交付 → 退回
    assert trials.trial_state(TRIAL_EMAIL)["used"] == 0

    third = trials.TrialAttempt(trials.reserve(seed), seed)
    third.mark_completed()
    assert third.finish(delivered=True) is True
    assert trials.trial_state(TRIAL_EMAIL)["used"] == 1


def test_attempt_settle_then_release_is_a_noop(db):
    seed = _registered(db)
    attempt = trials.TrialAttempt(trials.reserve(seed), seed)
    attempt.mark_completed()

    assert attempt.settle() is True
    assert attempt.release() is False
    assert attempt.settle() is False
    assert trials.trial_state(TRIAL_EMAIL)["used"] == 1


def test_attempt_survives_ledger_failure_without_breaking_cleanup(db, monkeypatch):
    """台账不可用：不伪装成已结算，也不让清理路径抛出去打断响应收尾。"""
    import sqlite3

    import utils.store as store

    seed = _registered(db)
    attempt = trials.TrialAttempt(trials.reserve(seed), seed)
    attempt.mark_completed()

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_connect", _boom)

    assert attempt.finish(delivered=True) is False
    assert attempt.resolved is False


async def test_stream_terminal_frame_on_a_secondary_choice_still_charges(db, monkeypatch):
    """终止分片落在非 0 号 choice 时也不能漏判 —— 漏判等于成功不扣次数。"""
    seed = _registered(db)

    async def _gen():
        yield "data: " + json.dumps({"choices": [
            {"index": 0, "delta": {"content": "Hello, world"}},
            {"index": 1, "delta": {}, "finish_reason": "stop"},
        ]}) + "\n\n"
        yield "data: [DONE]\n\n"

    _install_service(monkeypatch, _gen())
    await _invoke(seed, _chat_payload(stream=True))

    assert trials.trial_state(TRIAL_EMAIL)["used"] == 1


async def test_ledger_failure_does_not_skip_account_lease_release(db):
    """结账抛意外异常时槽位仍必须归还：归还槽位是硬保证，结账是尽力而为。"""
    service = _FakeChatService()

    class _BrokenAttempt:
        def finish(self, delivered):
            raise RuntimeError("synthetic ledger failure")

    lifetime = chat_api._GenerationLifetime(service, _BrokenAttempt())

    with pytest.raises(RuntimeError):
        await lifetime.close(delivered=True)

    assert service.closed == 1


async def test_compact_payload_failure_still_releases_the_account_lease(db, monkeypatch):
    """/v1/responses* 在准入之后、响应构造之前失败：租约不能跟着漏掉。"""
    service, _calls = _install_service(monkeypatch, _completion())

    def _boom(_data):
        raise RuntimeError("synthetic shaping failure")

    monkeypatch.setattr(chat_api, "_compact_responses_payload", _boom)

    with pytest.raises(HTTPException) as failure:
        await chat_api.send_responses_compact(
            _request({"model": "gpt-4o", "input": "hi"}),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="operator-raw-token"))

    assert failure.value.status_code == 500
    assert service.closed == 1


# ------------------------------------------------------------ ChatService 租约

class _FakeUpstreamResponse:
    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def atext(self):
        return "".join(self._lines)


class _FakeUpstreamClient:
    """记录「归还连接池」与「硬关闭」的区别（见 test_m2_stream_cancel.py）。"""

    def __init__(self, response):
        self._response = response
        self.closed = 0
        self.discarded = 0

    async def post_stream(self, *args, **kwargs):
        return self._response

    async def close(self):
        self.closed += 1

    async def discard(self):
        self.discarded += 1


def _chat_stream_lines():
    # aiter_lines() 返回 bytes（chatFormat.stream_response 直接 .decode("utf-8")）。
    raw = [
        json.dumps({
            "conversation_id": "conv-1",
            "message": {"id": "msg-1", "author": {"role": "assistant"}, "status": "in_progress",
                        "content": {"content_type": "text", "parts": [""]}},
        }),
        json.dumps({
            "conversation_id": "conv-1",
            "message": {"id": "msg-1", "author": {"role": "assistant"},
                        "status": "finished_successfully", "end_turn": True,
                        "content": {"content_type": "text", "parts": ["Hello, world"]},
                        "metadata": {"model_slug": "gpt-5-5"}},
        }),
    ]
    return [("data: " + line).encode("utf-8") for line in raw] + [b"data: [DONE]"]


def _streaming_service(monkeypatch, response):
    from chatgpt.ChatService import ChatService

    client = _FakeUpstreamClient(response)
    # 走真实构造函数（seed 未注册 → 原样作为账号 token），随后只替换出站替身。
    service = ChatService("account-token")
    service.antiban_ctx = AntibanContext(token="account-token", enabled=False)
    service.s = client
    service.ss = None
    service.ws = None
    service.base_url = "https://upstream.test/backend-api"
    service.chat_headers = {}
    service.chat_request = {}
    service.history_disabled = True
    service.resp_model = "gpt-5-5"
    service.max_tokens = 1024
    service.prompt_tokens = 1
    service.librechat_conv_id = None
    service.data = {"stream": True}
    return service, client


async def test_aborted_stream_session_is_discarded_not_pooled(db, monkeypatch):
    """流被中断（断连/取消/异常）时，半途的上游连接不能还给连接池。"""
    service, client = _streaming_service(monkeypatch, _FakeUpstreamResponse(_chat_stream_lines()))
    generator = await service.send_conversation()
    assert isinstance(generator, types.AsyncGeneratorType)

    await service.close_client()  # 生成器从未被消费

    assert (client.closed, client.discarded) == (0, 1)


async def test_completed_stream_session_is_pooled(db, monkeypatch):
    """正常结束的流仍然归还连接池（保留 curl 的 TCP/TLS 复用）。"""
    service, client = _streaming_service(monkeypatch, _FakeUpstreamResponse(_chat_stream_lines()))
    generator = await service.send_conversation()
    async for _chunk in generator:
        pass

    await service.close_client()

    assert (client.closed, client.discarded) == (1, 0)


async def test_stream_consumed_until_done_marker_is_pooled(db, monkeypatch):
    """消费方读到 ``data: [DONE]`` 就 break（非流式格式化就是这么做的）：连接是干净的。"""
    service, client = _streaming_service(monkeypatch, _FakeUpstreamResponse(_chat_stream_lines()))
    generator = await service.send_conversation()
    async for chunk in generator:
        if chunk.startswith("data: [DONE]"):
            break

    await service.close_client()

    assert (client.closed, client.discarded) == (1, 0)


async def test_chatservice_refuses_request_without_allocated_account(db, monkeypatch):
    """分不到号（容量为 0 / 池空）不得静默回落成匿名请求 —— 那就是白送生成。"""
    from chatgpt.ChatService import ChatService
    import chatgpt.ChatService as chat_service_mod

    monkeypatch.setattr(chat_service_mod, "get_req_token", lambda *a, **kw: "")
    service = ChatService("seed-without-capacity")
    seen = []

    async def _no_upstream(*args, **kwargs):
        seen.append(args)

    monkeypatch.setattr(service, "resolve_auth_context", _no_upstream)

    with pytest.raises(HTTPException) as failure:
        await service.set_dynamic_data(_chat_payload())

    assert failure.value.status_code == 503
    assert seen == []


async def test_cancellation_before_response_returns_the_account_lease(db, monkeypatch):
    """响应建立之前被取消（客户端断连 / 停机）：槽位必须归还，取消原样上抛。"""
    service = _FakeChatService()

    async def _cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    service.set_dynamic_data = _cancelled
    monkeypatch.setattr(chat_api, "ChatService", lambda token: service)

    with pytest.raises(asyncio.CancelledError):
        await chat_api.to_send_conversation(_chat_payload(), "tok-cancel")

    assert service.closed == 1


async def test_cancelled_upstream_work_returns_the_account_lease(db, monkeypatch):
    """上游工作中途被取消：process 也必须归还槽位（旧写法只捕获 Exception）。"""
    service = _FakeChatService()

    async def _cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    service.prepare_send_conversation = _cancelled

    async def fake_to_send(request_data, req_token):
        return service

    monkeypatch.setattr(chat_api, "to_send_conversation", fake_to_send)

    with pytest.raises(asyncio.CancelledError):
        await chat_api.process(_chat_payload(), "tok-cancel")

    assert service.closed == 1
