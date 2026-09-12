"""健康探针的判据、隐私与扫描鲁棒性。

判据取自「探针是证据来源」这一前提，不看实现细节：
  1. HTTP 200 不等于账号可用：登录页 / HTML / 非 JSON / 空 body 都是**失败**，
     否则被挡在登录墙外的死号会被一路判成 healthy 并继续接流量；
  2. 成功必须是「JSON 且身份与已认证 JWT 的账号用户一致」——拿到的是别人的号、
     或拿到一个通用页面，都不构成这个账号可用的证据；
  3. 请求必须带 workspace 作用域头（chatgpt-account-id），否则 Plus/Pro 的工作区
     权益不归属，探针测的不是这个账号真实的可用性；
  4. 畸形 / 过期 token 与身份不符一律判 unhealthy，且**不发**上游请求；
  5. 代理只用既有的显式绑定，探针不得给账号新分配出口 IP；
  6. 失败/取消都要归还或丢弃客户端；单个账号的意外异常不能让整轮扫描中断；
  7. 日志只含匿名标识与状态/错误枚举，不含 token 前缀、代理串或异常原文；
  8. disabled 不被探针覆盖；熔断死号只有持续探针成功后才可复活。

隔离：不发任何网络请求（Client 全替身），不写真实 DB（store 读写全打桩）。
"""

import asyncio
import base64
import json
import logging
import time

import pytest
from fastapi import HTTPException

import utils.configs as configs
import utils.globals as globals
from utils import fleet_health
from utils.antiban import circuit as antiban_circuit
from utils.antiban.concurrency import anon_id

# 合成凭据。断言「它不出现在日志里」，而不是断言真值。
FAKE_TOKEN = "eyJhbGciOiJIUzI1NitestonlyHEALTH246813"
# RFC 5737 TEST-NET-3：路由不可达，即便有人误发也打不出去。
TEST_NET_PROXY = "http://probeuser:probesecret@203.0.113.9:8080"

USER_ID = "user-testonly-1"
ACCOUNT_ID = "acc-testonly-1"


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
        "https://api.openai.com/profile": {"email": "owner@example.com"},
    }
    seg = lambda d: _b64url(json.dumps(d, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.testsig"


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
    """Client 替身：记录构造/请求，并记录归还方式（close / discard）。"""

    instances = []

    def __init__(self, proxy=None, timeout=15, verify=True, impersonate="safari15_3"):
        self.proxy = proxy
        self.requests = []
        self.closed = 0
        self.discarded = 0
        type(self).instances.append(self)

    # 每个用例通过 _SpyClient.next_response / next_error 决定行为
    next_response = None
    next_error = None

    async def get(self, url, headers=None, timeout=None, **kwargs):
        self.requests.append({"url": url, "headers": dict(headers or {})})
        err = type(self).next_error
        if err is not None:
            raise err
        return type(self).next_response

    async def close(self):
        self.closed += 1

    async def discard(self):
        self.discarded += 1


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    globals.antiban_dead_tokens.clear()
    globals.error_token_list.clear()
    globals.token_list = []
    _SpyClient.instances = []
    _SpyClient.next_response = _Resp(200, {"id": USER_ID, "email": "owner@example.com"})
    _SpyClient.next_error = None

    monkeypatch.setattr(fleet_health, "Client", _SpyClient)
    monkeypatch.setattr(fleet_health, "verify_token", _fake_verify)
    # 代理来源全部清空：探针不得从本机真实配置里取到带凭据的代理串
    monkeypatch.setattr(configs, "proxy_url_list", [])
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["https://chatgpt.test"])
    monkeypatch.setattr(fleet_health, "get_bound_proxy", lambda token: None)

    # 持久化全部打桩，绝不碰真实 SQLite
    writes = []
    monkeypatch.setattr(fleet_health.store, "get_account", lambda token: _ACCOUNTS.get(token))
    monkeypatch.setattr(
        fleet_health.store, "upsert_account",
        lambda token, **fields: writes.append((token, fields)),
    )
    def apply_probe(token, expected_status, status, checked_at):
        if _ACCOUNTS.get(token, {}).get("status") != expected_status:
            return False
        writes.append((token, {"status": status, "last_health_check": checked_at}))
        return True
    monkeypatch.setattr(fleet_health.store, "apply_health_probe", apply_probe)
    _ACCOUNTS.clear()
    yield writes
    _ACCOUNTS.clear()


_ACCOUNTS: dict = {}
_VERIFY_MAP: dict = {}


async def _fake_verify(token):
    if token in _VERIFY_MAP:
        result = _VERIFY_MAP[token]
        if isinstance(result, Exception):
            raise result
        return result
    return _access_token()


@pytest.fixture(autouse=True)
def _clear_verify_map():
    _VERIFY_MAP.clear()
    yield
    _VERIFY_MAP.clear()


def _blob(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 1. HTTP 200 不等于成功
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("resp,ids", [
    (_Resp(200, None, content_type="text/html; charset=utf-8",
           text="<!DOCTYPE html><html><body>Log in to ChatGPT</body></html>"), "login_html"),
    (_Resp(200, None, content_type="application/json", text=""), "empty_body"),
    (_Resp(200, ["not", "an", "object"]), "json_not_object"),
    (_Resp(200, {"detail": "Unauthorized"}), "json_without_identity"),
])
async def test_http_200_without_json_identity_is_not_healthy(resp, ids):
    """RED 形态：旧实现只看 status_code == 200，登录墙 HTML 也算 healthy。"""
    _SpyClient.next_response = resp
    assert await fleet_health.check_account(FAKE_TOKEN) == "unhealthy"


async def test_identity_mismatch_is_not_healthy():
    """拿到的是**别的**账号的身份，不构成本账号可用的证据。"""
    _SpyClient.next_response = _Resp(200, {"id": "user-someone-else"})
    assert await fleet_health.check_account(FAKE_TOKEN) == "unhealthy"


async def test_matching_identity_is_healthy():
    """对照组：JSON 且 body.id 与 JWT 账号用户一致 → healthy。"""
    _SpyClient.next_response = _Resp(200, {"id": USER_ID, "email": "owner@example.com"})
    assert await fleet_health.check_account(FAKE_TOKEN) == "healthy"


# ---------------------------------------------------------------------------
# 2. workspace 作用域头
# ---------------------------------------------------------------------------

async def test_probe_sends_workspace_scoped_header():
    """RED 形态：旧实现只发 Authorization，Plus/Pro 的工作区权益不归属。"""
    await fleet_health.check_account(FAKE_TOKEN)

    assert len(_SpyClient.instances) == 1
    req = _SpyClient.instances[0].requests[-1]
    headers = {k.lower(): v for k, v in req["headers"].items()}
    assert headers.get("chatgpt-account-id") == ACCOUNT_ID
    assert headers.get("authorization", "").startswith("Bearer ")
    assert headers.get("accept") == "application/json"


async def test_probe_without_account_id_claim_still_probes_without_the_header():
    """JWT 无 account_id（个人号历史格式）时不得凭空造一个，也不得直接判死。"""
    _VERIFY_MAP[FAKE_TOKEN] = _access_token(account_id="")
    assert await fleet_health.check_account(FAKE_TOKEN) == "healthy"

    headers = {k.lower(): v for k, v in _SpyClient.instances[0].requests[-1]["headers"].items()}
    assert "chatgpt-account-id" not in headers


# ---------------------------------------------------------------------------
# 3. 畸形 / 过期 token 与不可验证身份：不发上游请求
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("access_token", [
    "not-a-jwt",
    "",
    None,
])
async def test_malformed_access_token_is_rejected_without_a_request(access_token):
    _VERIFY_MAP[FAKE_TOKEN] = access_token
    assert await fleet_health.check_account(FAKE_TOKEN) == "unhealthy"
    assert _SpyClient.instances == [], "no upstream request may be issued for a bad token"


async def test_expired_access_token_is_rejected_without_a_request():
    _VERIFY_MAP[FAKE_TOKEN] = _access_token(exp=int(time.time()) - 10)
    assert await fleet_health.check_account(FAKE_TOKEN) == "unhealthy"
    assert _SpyClient.instances == []


async def test_unverifiable_identity_is_rejected_without_a_request():
    """JWT 没有账号用户标识 → 无法判断响应是不是这个账号的，不发请求也不判 healthy。"""
    _VERIFY_MAP[FAKE_TOKEN] = _access_token(user_id="")
    assert await fleet_health.check_account(FAKE_TOKEN) == "unhealthy"
    assert _SpyClient.instances == []


async def test_auth_failure_is_rejected_without_a_request():
    _VERIFY_MAP[FAKE_TOKEN] = HTTPException(status_code=401, detail="Account unavailable")
    assert await fleet_health.check_account(FAKE_TOKEN) == "unhealthy"
    assert _SpyClient.instances == []


# ---------------------------------------------------------------------------
# 4. 代理：只用既有显式绑定，不新分配
# ---------------------------------------------------------------------------

async def test_probe_uses_the_existing_explicit_binding(monkeypatch):
    monkeypatch.setattr(fleet_health, "get_bound_proxy", lambda token: TEST_NET_PROXY)
    await fleet_health.check_account(FAKE_TOKEN)
    assert _SpyClient.instances[0].proxy == TEST_NET_PROXY


async def test_probe_uses_pool_proxy_when_unbound(monkeypatch):
    """RED 形态：旧实现在无绑定时 random.choice(proxy_url_list)，等于给账号
    临时换了出口 IP——既破坏粘性绑定，也是一次未经授权的新分配。"""
    monkeypatch.setattr(configs, "proxy_url_list", [TEST_NET_PROXY, "http://203.0.113.10:8080"])
    monkeypatch.setattr(fleet_health, "get_bound_proxy", lambda token: None)

    await fleet_health.check_account(FAKE_TOKEN)

    assert _SpyClient.instances[0].proxy in {
        TEST_NET_PROXY,
        "http://203.0.113.10:8080",
    }


async def test_account_row_binding_is_honoured_when_routing_has_none(monkeypatch):
    """账号行里已有的显式出口也是既有绑定，读取而不是重新分配。"""
    _ACCOUNTS[FAKE_TOKEN] = {"status": "healthy", "proxy_url": TEST_NET_PROXY}
    await fleet_health.check_account(FAKE_TOKEN)
    assert _SpyClient.instances[0].proxy == TEST_NET_PROXY


# ---------------------------------------------------------------------------
# 5. 客户端生命周期：失败 / 取消都要归还或丢弃
# ---------------------------------------------------------------------------

async def test_client_is_released_on_success():
    await fleet_health.check_account(FAKE_TOKEN)
    c = _SpyClient.instances[0]
    assert c.closed + c.discarded == 1


async def test_client_is_discarded_on_network_error():
    _SpyClient.next_error = ConnectionResetError("connection reset by peer")
    assert await fleet_health.check_account(FAKE_TOKEN) == "unhealthy"
    c = _SpyClient.instances[0]
    assert c.discarded == 1, "a broken connection must not be returned to the pool"


async def test_client_is_discarded_on_cancellation():
    """取消必须向上传播（不能吞成 unhealthy），但连接不能泄漏。"""
    _SpyClient.next_error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await fleet_health.check_account(FAKE_TOKEN)

    c = _SpyClient.instances[0]
    assert c.discarded == 1


# ---------------------------------------------------------------------------
# 6. 整轮扫描：单账号异常不得中断
# ---------------------------------------------------------------------------

async def test_one_unexpected_account_error_does_not_abort_the_sweep(monkeypatch, _isolate):
    writes = _isolate
    # raising=False：节奏常量是本轮新增的，缺它应当让**行为断言**失败，
    # 而不是让 setup 报 AttributeError（那只证明名字不存在）。
    monkeypatch.setattr(fleet_health, "_PACING_SECONDS", 0, raising=False)
    globals.token_list = ["tok-a", "tok-boom", "tok-c"]
    _ACCOUNTS.update({t: {"status": "healthy"} for t in globals.token_list})

    original = fleet_health.check_account

    async def _maybe_boom(token):
        if token == "tok-boom":
            raise RuntimeError("unexpected internal failure")
        return await original(token)

    monkeypatch.setattr(fleet_health, "check_account", _maybe_boom)

    summary = await fleet_health.check_all_accounts()

    assert summary["healthy"] == 2
    assert summary.get("errors") == 1
    written = {t for t, _ in writes}
    assert written == {"tok-a", "tok-c"}, "an internal error is not evidence about the account"


async def test_disabled_accounts_are_never_probed_or_overwritten(monkeypatch, _isolate):
    writes = _isolate
    monkeypatch.setattr(fleet_health, "_PACING_SECONDS", 0, raising=False)
    globals.token_list = ["tok-disabled"]
    _ACCOUNTS["tok-disabled"] = {"status": "disabled"}

    summary = await fleet_health.check_all_accounts()

    assert summary["skipped_disabled"] == 1
    assert writes == []
    assert _SpyClient.instances == []


async def test_circuit_dead_account_stays_ineligible_during_recovery_probe():
    """一次成功探针只进入 dwell，不能立即把死号放回流量。"""
    antiban_circuit.globals.antiban_dead_tokens[FAKE_TOKEN] = {
        "reason": "account_deactivated", "dead_at": int(time.time()),
    }
    assert await fleet_health.check_account(FAKE_TOKEN) == "unhealthy"
    assert len(_SpyClient.instances) == 1


async def test_error_list_account_stays_ineligible_and_is_not_probed():
    globals.error_token_list.append(FAKE_TOKEN)
    assert await fleet_health.check_account(FAKE_TOKEN) == "unhealthy"
    assert _SpyClient.instances == []


# ---------------------------------------------------------------------------
# 7. 匿名诊断：状态/错误枚举保留，凭据与原文不保留
# ---------------------------------------------------------------------------

async def test_probe_logs_contain_no_token_proxy_or_exception_text(caplog, monkeypatch):
    """RED 形态：旧实现打 token[:12] 与 str(e)[:120]。"""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(fleet_health, "get_bound_proxy", lambda token: TEST_NET_PROXY)

    _SpyClient.next_response = _Resp(403, None, content_type="text/html",
                                     text="<html>cf_chl_opt secret-ray-id-abc</html>")
    await fleet_health.check_account(FAKE_TOKEN)

    _SpyClient.next_error = ConnectionResetError("connect to 203.0.113.9 failed: secret detail")
    await fleet_health.check_account(FAKE_TOKEN)

    blob = _blob(caplog)
    assert FAKE_TOKEN not in blob
    for n in (8, 10, 12, 16):
        assert FAKE_TOKEN[:n] not in blob, f"token prefix of length {n} leaked into logs"
    assert "probeuser" not in blob and "probesecret" not in blob and "203.0.113.9" not in blob
    assert "secret-ray-id-abc" not in blob
    assert "secret detail" not in blob
    assert "ConnectionResetError" in blob, "the error class stays: it is the diagnosis"
    assert anon_id(FAKE_TOKEN) in blob


async def test_check_account_detail_reports_anonymous_reason_enums():
    """状态之外还要给出原因枚举，否则运维只能回去看原文日志。"""
    _SpyClient.next_response = _Resp(200, None, content_type="text/html", text="<html>login</html>")
    status, reason = await fleet_health.check_account_detail(FAKE_TOKEN)
    assert (status, reason) == ("unhealthy", fleet_health.REASON_NOT_JSON)

    _SpyClient.next_response = _Resp(200, {"id": "user-someone-else"})
    assert (await fleet_health.check_account_detail(FAKE_TOKEN))[1] == fleet_health.REASON_IDENTITY_MISMATCH

    _VERIFY_MAP[FAKE_TOKEN] = _access_token(exp=int(time.time()) - 10)
    assert (await fleet_health.check_account_detail(FAKE_TOKEN))[1] == fleet_health.REASON_TOKEN_EXPIRED

    _VERIFY_MAP.pop(FAKE_TOKEN)
    _SpyClient.next_response = _Resp(200, {"id": USER_ID})
    assert await fleet_health.check_account_detail(FAKE_TOKEN) == ("healthy", fleet_health.REASON_OK)


async def test_health_stats_are_anonymous_reason_counters():
    fleet_health.reset_health_stats()
    _SpyClient.next_response = _Resp(200, {"id": "user-someone-else"})
    await fleet_health.check_account(FAKE_TOKEN)
    _SpyClient.next_response = _Resp(200, {"id": USER_ID})
    await fleet_health.check_account(FAKE_TOKEN)

    stats = fleet_health.get_health_stats()
    assert stats[fleet_health.REASON_IDENTITY_MISMATCH] == 1
    assert stats[fleet_health.REASON_OK] == 1
    assert FAKE_TOKEN not in repr(stats)
    assert FAKE_TOKEN[:8] not in repr(stats)


@pytest.mark.parametrize('field,value', [('exp', None), ('exp', 'invalid'),
                                        ('exp', float('inf')),
                                        ('https://api.openai.com/auth', ['invalid'])])
async def test_unverifiable_claim_shapes_never_reach_probe(field, value):
    claims = fleet_health._decode_jwt_payload(_access_token())
    claims[field] = value
    _VERIFY_MAP[FAKE_TOKEN] = 'header.' + _b64url(json.dumps(claims).encode()) + '.testsig'
    assert await fleet_health.check_account(FAKE_TOKEN) == 'unhealthy'
    assert not _SpyClient.instances


async def test_saved_fingerprint_proxy_binding_is_preserved():
    _ACCOUNTS[FAKE_TOKEN] = {'fingerprint': json.dumps({'proxy_url': TEST_NET_PROXY})}
    assert await fleet_health.check_account(FAKE_TOKEN) == 'healthy'
    assert _SpyClient.instances[0].proxy == TEST_NET_PROXY
