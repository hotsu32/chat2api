"""网络回落加固：显式空 base URL 不得回落到真实 chatgpt.com；生产代码不得依赖 dev-only httpx。

判据（只看外呼与结果，不看实现）：
  1. ``CHATGPT_BASE_URL`` 显式为空（``chatgpt_base_url_list`` 为空）＝ 没有上游目标：
     探针必须给出一个有界的非成功原因，并且**不构造**客户端——没有客户端就没有连接，
     也就不存在对真实站点的解析与回落；
  2. 配置了 base URL（含回环地址）时行为不变：目标逐字使用，仍带 workspace 作用域头，
     JSON 且身份与已认证 JWT 一致才算 healthy；
  3. 启动期版本自检走生产客户端（``utils.Client``，curl_cffi），不得 import dev-only 的
     httpx（它只在 requirements-dev.txt，生产环境没有）；
  4. 失败日志只有有界类别（状态码 / 原因枚举 / 异常类名），不含上游地址与异常原文；
  5. 取消与失败都要归还或丢弃客户端。

隔离：真实 Client 的出站在发出前被拦下，只允许回环地址；其余用替身客户端。
"""

import asyncio
import base64
import contextlib
import http.server
import json
import logging
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

import pytest

import utils.configs as configs
import utils.globals as globals
from utils import fleet_health
from utils.antiban import version_check

# 回环地址白名单：本文件里唯一允许真实连接的出口。空/通配目标不是外呼（本地被动
# 绑定与 getaddrinfo(None) 会走到这里），放行不削弱判据。
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::", "0.0.0.0", "", "none"}

# 合成凭据。断言「它不出现在日志里」，而不是断言真值。
FAKE_TOKEN = "eyJhbGciOiJIUzI1NitestonlyNETWORK246813"

USER_ID = "u-1"
ACCOUNT_ID = "acc-testonly-net"


def _host_of(url: str) -> str:
    return (urlsplit(str(url)).hostname or "").lower()


def _host_key(host) -> str:
    """socket 侧的主机名归一化：bytes / None 都要能安全比较。"""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return str(host).lower()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _access_token(user_id=USER_ID, account_id=ACCOUNT_ID, exp=None):
    claims = {
        "sub": f"auth0|{user_id}",
        "iat": int(time.time()) - 60,
        "exp": exp if exp is not None else int(time.time()) + 3600,
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": "plus",
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
        },
    }
    seg = lambda d: _b64url(json.dumps(d, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.testsig"


@pytest.fixture(autouse=True)
def _loopback_only_egress(monkeypatch):
    """任何非回环外呼都必须在真实 socket / 真实 Client 之前被拦下。

    两道闸口：
      - 真实 Client.get：URL 进入 curl_cffi 之前检查（curl 的连接不经 Python socket，
        所以这一层才是 curl_cffi 的有效拦截点）；
      - Python socket.connect / getaddrinfo：覆盖标准库路径与名称解析。
    """
    from utils.Client import Client as _RealClient

    attempted = []
    real_get = _RealClient.get

    async def _guarded_get(self, url, *args, **kwargs):
        attempted.append(str(url))
        if _host_of(url) not in LOOPBACK_HOSTS:
            raise AssertionError(f"forbidden non-loopback egress: {url}")
        return await real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(_RealClient, "get", _guarded_get)

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def _guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        attempted.append(f"connect:{_host_key(host)}")
        if _host_key(host) not in LOOPBACK_HOSTS:
            raise AssertionError(f"forbidden non-loopback connect: {host}")
        return real_connect(self, address)

    def _guarded_getaddrinfo(host, *args, **kwargs):
        attempted.append(f"resolve:{_host_key(host)}")
        if _host_key(host) not in LOOPBACK_HOSTS:
            raise AssertionError(f"forbidden non-loopback resolve: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    return attempted


@contextlib.contextmanager
def _httpx_unavailable():
    """让 httpx 在本进程内不可导入（等价于只装了生产依赖的运行环境）。"""
    saved_top = sys.modules.pop("httpx", None)
    saved_sub = {k: v for k, v in list(sys.modules.items()) if k.startswith("httpx.")}
    for k in saved_sub:
        sys.modules.pop(k, None)

    class _Blocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "httpx" or fullname.startswith("httpx."):
                raise ImportError("No module named 'httpx' (dev-only dependency)")
            return None

    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(saved_sub)
        if saved_top is not None:
            sys.modules["httpx"] = saved_top


class _Resp:
    def __init__(self, status_code=200, body=None, content_type="application/json", text=None):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self._body = body
        self.text = text if text is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class _HealthSpyClient:
    """fleet_health 的 Client 替身：记录构造与请求，记录归还方式。"""

    instances = []
    next_response = _Resp(200, {"id": USER_ID})

    def __init__(self, proxy=None, timeout=15, verify=True, impersonate="safari15_3"):
        self.proxy = proxy
        self.timeout = timeout
        self.requests = []
        self.closed = 0
        self.discarded = 0
        type(self).instances.append(self)

    async def get(self, url, headers=None, timeout=None, **kwargs):
        self.requests.append({"url": url, "headers": dict(headers or {}), "timeout": timeout,
                              "kwargs": dict(kwargs)})
        return type(self).next_response

    async def close(self):
        self.closed += 1

    async def discard(self):
        self.discarded += 1


class _VersionSpyClient:
    """version_check 的 Client 替身。"""

    instances = []
    next_response = None
    next_error = None

    def __init__(self, proxy=None, timeout=15, verify=True, impersonate="safari15_3"):
        self.proxy = proxy
        self.timeout = timeout
        self.verify = verify
        self.impersonate = impersonate
        self.calls = []
        self.closed = 0
        self.discarded = 0
        type(self).instances.append(self)

    async def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout,
                           "kwargs": dict(kwargs)})
        err = type(self).next_error
        if err is not None:
            raise err
        return type(self).next_response

    async def close(self):
        self.closed += 1

    async def discard(self):
        self.discarded += 1


class _BoomClient:
    """一构造就失败：用来证明「没有目标时不构造客户端」。"""

    def __init__(self, *args, **kwargs):
        raise AssertionError("no HTTP client may be constructed without a configured base URL")


_VERSION_PAGE = (
    '<!DOCTYPE html><html data-build="prod-f501fe933b3edf57aea882da888e1a544df99840">'
    "<head><script>window.__NEXT_DATA__ = {\"buildNumber\": 1234567}</script></head>"
    "<body>ChatGPT</body></html>"
)


class _VersionPageHandler(http.server.BaseHTTPRequestHandler):
    html = _VERSION_PAGE
    paths = []
    status_code = 200

    def do_GET(self):
        type(self).paths.append(self.path)
        body = type(self).html.encode("utf-8")
        self.send_response(type(self).status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def version_page():
    """回环上的官网替身：返回带 data-build 的 HTML。"""
    _VersionPageHandler.html = _VERSION_PAGE
    _VersionPageHandler.paths = []
    _VersionPageHandler.status_code = 200
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _VersionPageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


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
def _isolate(monkeypatch):
    """fleet_health 的持久层与凭据全部打桩；默认 base URL 指向回环合成域名。"""
    globals.antiban_dead_tokens.clear()
    globals.error_token_list.clear()
    globals.token_list = []
    _ACCOUNTS.clear()
    _VERIFY_MAP.clear()
    _HealthSpyClient.instances = []
    _HealthSpyClient.next_response = _Resp(200, {"id": USER_ID})
    _VersionSpyClient.instances = []
    _VersionSpyClient.next_response = _Resp(200, None, content_type="text/html", text=_VERSION_PAGE)
    _VersionSpyClient.next_error = None

    monkeypatch.setattr(fleet_health, "Client", _HealthSpyClient)
    monkeypatch.setattr(fleet_health, "verify_token", _fake_verify)
    monkeypatch.setattr(configs, "proxy_url_list", [])
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["http://127.0.0.1:1"])
    monkeypatch.setattr(fleet_health, "get_bound_proxy", lambda token: None)
    monkeypatch.setattr(fleet_health.store, "get_account", lambda token: _ACCOUNTS.get(token))
    yield
    _ACCOUNTS.clear()
    _VERIFY_MAP.clear()


def _blob(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 1. 显式空 base URL：没有目标，不回落真实站点
# ---------------------------------------------------------------------------

async def test_empty_base_url_is_not_a_probe_target(monkeypatch):
    """RED 形态：旧实现回落 https://chatgpt.com 并照常构造客户端外呼。"""
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [])

    status, reason = await fleet_health.check_account_detail(FAKE_TOKEN)

    assert status == "unhealthy"
    assert reason == fleet_health.REASON_NO_BASE_URL
    assert _HealthSpyClient.instances == [], (
        "an explicitly empty CHATGPT_BASE_URL must not construct a client at all"
    )


async def test_empty_base_url_reason_is_bounded_and_logged_anonymously(monkeypatch, caplog):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [])

    assert await fleet_health.check_account(FAKE_TOKEN) == "unhealthy"

    blob = _blob(caplog)
    assert fleet_health.REASON_NO_BASE_URL in blob
    assert "chatgpt.com" not in blob
    assert FAKE_TOKEN not in blob


async def test_empty_base_url_reason_is_a_declared_enum(monkeypatch):
    """原因必须是枚举值：面板与指标只能拿到有界的类别，不是自由文本。"""
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [])
    _, reason = await fleet_health.check_account_detail(FAKE_TOKEN)

    declared = {v for k, v in vars(fleet_health).items() if k.startswith("REASON_")}
    assert reason in declared


async def test_empty_base_url_does_not_touch_the_recovery_dwell(monkeypatch):
    """没有目标不是账号的错：不得因此累计恢复连击。"""
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [])
    fleet_health.reset_recovery_state()

    await fleet_health.check_account(FAKE_TOKEN)

    assert fleet_health._recovery_state == {}


# ---------------------------------------------------------------------------
# 2. 配置了 base URL（含回环）：行为不变
# ---------------------------------------------------------------------------

async def test_configured_base_url_is_used_verbatim(monkeypatch):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["http://127.0.0.1:7/base"])

    await fleet_health.check_account(FAKE_TOKEN)

    assert _HealthSpyClient.instances[0].requests[-1]["url"] == \
        "http://127.0.0.1:7/base/backend-api/me"


async def test_configured_loopback_base_still_probes_end_to_end(monkeypatch, mock_upstream):
    """真实 Client + 回环官网替身：配置了 base URL 时探针照常取证。"""
    from utils.Client import Client as RealClient

    monkeypatch.setattr(fleet_health, "Client", RealClient)
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [mock_upstream])
    _VERIFY_MAP[FAKE_TOKEN] = _access_token(user_id=USER_ID)

    assert await fleet_health.check_account(FAKE_TOKEN) == "healthy"


async def test_workspace_header_is_still_sent_to_a_configured_loopback(monkeypatch):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["http://127.0.0.1:7"])
    _VERIFY_MAP[FAKE_TOKEN] = _access_token(user_id=USER_ID, account_id=ACCOUNT_ID)

    assert await fleet_health.check_account(FAKE_TOKEN) == "healthy"

    headers = {k.lower(): v for k, v in _HealthSpyClient.instances[0].requests[-1]["headers"].items()}
    assert headers.get("chatgpt-account-id") == ACCOUNT_ID
    assert headers.get("accept") == "application/json"


# ---------------------------------------------------------------------------
# 3. 版本自检：生产客户端 + 有界失败类别
# ---------------------------------------------------------------------------

def test_version_probe_uses_the_production_http_client():
    """生产用 utils.Client（curl_cffi），不是 dev-only 的 httpx。"""
    from utils.Client import Client as ProductionClient

    assert version_check.Client is ProductionClient


async def test_version_probe_is_importable_without_httpx(monkeypatch, version_page):
    """RED 形态：旧实现在函数内 ``import httpx``，生产环境直接 ModuleNotFoundError。"""
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [version_page])
    monkeypatch.setattr(configs, "oai_client_version", "prod-f501fe933b3edf57aea882da888e1a544df99840")
    monkeypatch.setattr(configs, "oai_client_build_number", 1234500)

    with _httpx_unavailable():
        assert await version_check.probe_and_compare() == (False, "in-sync")


async def test_version_probe_skips_without_a_client_when_base_url_is_empty(monkeypatch):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [])
    # raising=False：旧实现没有模块级 Client（它用的是 httpx），让 RED 形态表现为
    # 行为失败（回落真实站点）而不是 setup 报 AttributeError。模块级 Client 本身由
    # test_version_probe_uses_the_production_http_client 断言。
    monkeypatch.setattr(version_check, "Client", _BoomClient, raising=False)

    assert await version_check.probe_and_compare() == (False, version_check.REASON_NO_BASE_URL)


async def test_version_probe_reads_the_configured_loopback_page(monkeypatch, version_page):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [version_page])
    monkeypatch.setattr(configs, "oai_client_version", "prod-f501fe933b3edf57aea882da888e1a544df99840")
    monkeypatch.setattr(configs, "oai_client_build_number", 1234500)

    assert await version_check.probe_and_compare() == (False, "in-sync")
    assert _VersionPageHandler.paths[-1] == "/"


async def test_version_probe_reports_drift_on_a_prefix_change(monkeypatch, version_page):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [version_page])
    monkeypatch.setattr(configs, "oai_client_version", "dev-0000000000000000000000000000000000000000")

    is_drift, msg = await version_check.probe_and_compare()

    assert is_drift is True
    assert "prefix mismatch" in msg


async def test_version_probe_keeps_timeout_and_no_redirect_policy(monkeypatch):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["http://127.0.0.1:9/"])
    monkeypatch.setattr(version_check, "Client", _VersionSpyClient)

    await version_check.probe_and_compare()

    client = _VersionSpyClient.instances[0]
    call = client.calls[-1]
    assert client.timeout == 5
    assert call["url"] == "http://127.0.0.1:9/"
    assert call["kwargs"].get("allow_redirects") is False
    assert client.closed + client.discarded == 1


async def test_version_probe_failure_log_has_no_upstream_address_or_exception_text(monkeypatch, caplog):
    """失败只记有界类别：异常类名，不记原文（原文会带回环地址与本机端口）。"""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["http://127.0.0.1:9/"])
    monkeypatch.setattr(version_check, "Client", _VersionSpyClient)
    _VersionSpyClient.next_error = ConnectionResetError("connect to 127.0.0.1:9 failed: secret detail")

    assert await version_check.probe_and_compare() == (False, "skipped")

    blob = _blob(caplog)
    assert "ConnectionResetError" in blob
    assert "secret detail" not in blob
    assert "127.0.0.1" not in blob
    assert _VersionSpyClient.instances[0].discarded == 1


async def test_version_probe_cancellation_propagates_and_discards_the_client(monkeypatch):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["http://127.0.0.1:9/"])
    monkeypatch.setattr(version_check, "Client", _VersionSpyClient)
    _VersionSpyClient.next_error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await version_check.probe_and_compare()

    client = _VersionSpyClient.instances[0]
    assert client.discarded == 1
    assert client.closed == 0


async def test_version_probe_cancellation_during_the_empty_base_skip_is_a_noop(monkeypatch):
    """空 base 提前返回：没有客户端可泄漏。"""
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [])

    assert await version_check.probe_and_compare() == (False, version_check.REASON_NO_BASE_URL)
    assert _VersionSpyClient.instances == []
