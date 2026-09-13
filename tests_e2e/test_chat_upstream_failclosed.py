"""显式空 CHATGPT_BASE_URL 的网关链路：本地 503，零外呼。

覆盖三条 owned 路由面 + 一条 only-when-NO_SENTINEL 的路由面：

  * ``gateway.reverseProxy.chatgpt_reverse_proxy``（镜像 catch-all 的聊天/后端路径）；
  * ``gateway.f_conversation_gateway.f_conversation``（f/conversation 主路线）；
  * ``gateway.f_conversation_gateway.f_sentinel_prepare``（sentinel prepare）；
  * ``gateway.backend`` 的 ``if no_sentinel:`` 两条路由 —— 它们在常规套件里根本不会注册
    （``NO_SENTINEL=false``），所以只能在「全新进程 + 全新 env」里证明，见文件末尾的
    子进程驱动。

判据（只看外呼和响应，不看实现）：
  1. 显式空配置下这些路由返回有界 503，且**不构造任何上游客户端**（没有客户端就没有
     连接），mock 上游一条记录都没有；
  2. 任何非回环出口（socket 解析/连接 + curl_cffi 请求）在真实外发之前被拦下并记违规，
     所以「没有回落 chatgpt.com」是被观察到的，而不是靠假定；
  3. 配置了回环上游时同一条路由行为不变：路径与头部逐字送达（对照组）。

隔离：mock 上游只在回环；不读真实 ``data/``；不需要任何真实凭据。
"""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi import HTTPException

import utils.configs as configs

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 与 tests_e2e/test_f_conversation_upstream.py 同一套合成身份，便于对照。
SEED = "seed-f1"
ACCOUNT = "acc-f1"

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "", "none"}

_CHILD_TIMEOUT_SECONDS = 240


def _host_of(url) -> str:
    return (urlsplit(str(url)).hostname or "").lower()


# ---------------------------------------------------------------------------
# 出口闸：非回环外呼必须在真实发出之前失败
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _loopback_only_egress(monkeypatch):
    """两道闸口，覆盖本应用的全部出口。

    curl_cffi 是 libcurl C 扩展，不走 Python socket（0.16 的 get/post 都收敛到
    ``AsyncSession.request``），所以只拦 socket 会让「无真实外发」只覆盖一半。
    """
    from curl_cffi.requests import AsyncSession

    violations = []
    real_request = AsyncSession.request

    async def _guarded_request(self, method, url, *args, **kwargs):
        host = _host_of(url)
        if host not in LOOPBACK_HOSTS:
            violations.append(f"curl:{host}")
            raise AssertionError(f"forbidden non-loopback curl request: {url}")
        return await real_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "request", _guarded_request)

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def _host_key(host):
        if isinstance(host, (bytes, bytearray)):
            host = host.decode("utf-8", "replace")
        return str(host).lower()

    def _guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if _host_key(host) not in LOOPBACK_HOSTS:
            violations.append(f"connect:{_host_key(host)}")
            raise AssertionError(f"forbidden non-loopback connect: {host}")
        return real_connect(self, address)

    def _guarded_getaddrinfo(host, *args, **kwargs):
        if _host_key(host) not in LOOPBACK_HOSTS:
            violations.append(f"resolve:{_host_key(host)}")
            raise AssertionError(f"forbidden non-loopback resolve: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    return violations


class _ConstructSpy:
    """记录构造；任何请求方法都判定失败。没有配置目标就不该有任何上游动作。"""

    constructed = []

    def __init__(self, *args, **kwargs):
        type(self).constructed.append(kwargs)

    async def _forbidden(self, *args, **kwargs):
        raise AssertionError("upstream request issued without a configured base URL")

    get = post = post_stream = put = request = _forbidden

    async def close(self):
        return None

    async def discard(self):
        return None


@pytest.fixture
def empty_upstream(monkeypatch):
    """显式空 CHATGPT_BASE_URL：in-place 清空，让直接 import 的模块也看到。"""
    _ConstructSpy.constructed = []
    from utils import resp_cache
    resp_cache.invalidate_all()
    configs.chatgpt_base_url_list[:] = []
    yield
    configs.chatgpt_base_url_list[:] = []


@pytest.fixture
def bound_account(monkeypatch, tmp_path, make_access_token, seed_user, seed_account):
    """把一个 seed 绑到一个官网会话已验证的账号（f/ 路由的前置条件）。"""
    from gateway import frontend_sync as frontend
    frontend.invalidate_frontend_cache()
    monkeypatch.setattr(frontend, 'SESSION_ARCHIVE_DIR', tmp_path)
    access = make_access_token(account_id=ACCOUNT, plan_type='plus')
    (tmp_path / (ACCOUNT + '.json')).write_text(json.dumps(
        {'account': {'id': ACCOUNT}, 'sessionToken': 'website-' + ACCOUNT}))

    def fetch(cookies, account_id, fingerprint, **kwargs):
        session = {'user': {'id': 'u-' + account_id, 'name': 'Private owner'},
                   'account': {'id': account_id, 'planType': 'plus'},
                   'accessToken': access, 'sessionToken': 'PRIVATE-SESSION'}
        return {'html': '<html></html>', 'session': session,
                'cookies': dict(cookies, **{'cf_clearance': 'cf-value'})}

    monkeypatch.setattr(frontend, '_fetch_official_html_sync', fetch)
    seed_account(access, plan_type='plus')
    seed_user(SEED, access, plan_type='plus')
    import asyncio
    asyncio.run(frontend.get_frontend_template(access, access, {}))
    yield access
    frontend.invalidate_frontend_cache()


def _f_body():
    return {
        "model": "gpt-5-6",
        "messages": [{"id": "msg-u1", "author": {"role": "user"},
                      "content": {"content_type": "text", "parts": ["hi"]}}],
    }


# ---------------------------------------------------------------------------
# 镜像 catch-all（reverseProxy）
# ---------------------------------------------------------------------------

def test_reverse_proxy_chat_path_fails_closed_without_upstream(
        client, mock_upstream, empty_upstream, monkeypatch):
    from gateway import reverseProxy

    monkeypatch.setattr(reverseProxy, "Client", _ConstructSpy)
    response = client.get("/backend-api/me", cookies={"token": SEED})

    assert response.status_code == 503
    assert response.json()["detail"] == "upstream not configured"
    # 没有构造客户端 → 不存在指向 chatgpt.com 的连接；也没有任何请求落到配置过的上游
    assert _ConstructSpy.constructed == []
    assert mock_upstream.records == []


def test_reverse_proxy_configured_upstream_still_receives_the_request(
        client, mock_upstream, bound_account):
    """对照：配置了上游时，路径与账号作用域头逐字送达。"""
    from utils import resp_cache
    resp_cache.invalidate_all()
    response = client.get("/backend-api/me", cookies={"token": SEED})
    assert response.status_code == 200
    forwarded = [r for r in mock_upstream.records if r["path"].startswith("/backend-api/me")]
    assert forwarded, "configured upstream must still receive the request"
    assert forwarded[-1]["headers"].get("host") == mock_upstream.url.replace("http://", "")


# ---------------------------------------------------------------------------
# f/conversation：主路线 + sentinel prepare
# ---------------------------------------------------------------------------

def test_f_conversation_fails_closed_without_upstream(
        client, mock_upstream, bound_account, empty_upstream, monkeypatch):
    from gateway import f_conversation_gateway

    monkeypatch.setattr(f_conversation_gateway, "Client", _ConstructSpy)
    response = client.post("/backend-api/f/conversation", cookies={"token": SEED},
                           json=_f_body())

    assert response.status_code == 503
    assert "chatgpt.com" not in response.text
    assert _ConstructSpy.constructed == []
    assert mock_upstream.records == []


def test_f_sentinel_prepare_fails_closed_without_upstream(
        client, mock_upstream, bound_account, empty_upstream, monkeypatch):
    from gateway import f_conversation_gateway

    monkeypatch.setattr(f_conversation_gateway, "Client", _ConstructSpy)
    response = client.post("/backend-api/sentinel/chat-requirements/prepare",
                           cookies={"token": SEED}, json={"p": "testonly"})

    assert response.status_code == 503
    assert _ConstructSpy.constructed == []
    assert mock_upstream.records == []


def test_f_conversation_configured_upstream_still_receives_the_turn(
        client, mock_upstream, bound_account):
    """对照：配置了上游时 f/conversation 照旧把这一轮送到 conversation 端点。"""
    from utils import resp_cache
    resp_cache.invalidate_all()
    response = client.post("/backend-api/f/conversation", cookies={"token": SEED},
                           json=_f_body())
    assert response.status_code == 200
    paths = [r["path"] for r in mock_upstream.records]
    assert "/backend-api/conversation" in paths


# ---------------------------------------------------------------------------
# gateway.backend 的 no_sentinel 路由：共享出口本身 + 进程级证据
# ---------------------------------------------------------------------------

def test_backend_upstream_guard_is_bounded_and_local(monkeypatch):
    from gateway import backend

    monkeypatch.setattr(configs, "chatgpt_base_url_list", [])
    with pytest.raises(HTTPException) as exc:
        backend._upstream_target_or_fail_closed()
    assert exc.value.status_code == 503
    assert "chatgpt.com" not in str(exc.value.detail)

    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["http://127.0.0.1:9"])
    assert backend._upstream_target_or_fail_closed() == "http://127.0.0.1:9"


# 子进程侧护栏：非回环的解析 / 连接 / curl 请求都被记为违规（而不是静默通过）。
_GUARDS = r'''
import json, os, socket
from urllib.parse import urlsplit

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_VIOLATIONS = []


def _host_of(value):
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value)


_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect


def _guard_getaddrinfo(host, *args, **kwargs):
    name = _host_of(host)
    if name not in _LOOPBACK_HOSTS:
        _VIOLATIONS.append("resolve:" + name)
        raise socket.gaierror(-2, "blocked non-loopback resolve: " + name)
    return _real_getaddrinfo(host, *args, **kwargs)


def _guard_connect(self, address):
    name = _host_of(address[0]) if isinstance(address, (tuple, list)) and address else ""
    if name not in _LOOPBACK_HOSTS:
        _VIOLATIONS.append("connect:" + name)
        raise OSError("blocked non-loopback connect: " + name)
    return _real_connect(self, address)


socket.getaddrinfo = _guard_getaddrinfo
socket.socket.connect = _guard_connect

try:
    from curl_cffi import requests as _cffi_requests
except Exception:  # pragma: no cover - curl_cffi 缺失时只剩 socket 那道
    _cffi_requests = None

if _cffi_requests is not None:
    for _name in ("Session", "AsyncSession"):
        _cls = getattr(_cffi_requests, _name, None)
        if _cls is None:
            continue
        _original = _cls.request

        def _guard_request(original):
            def _wrapped(self, method, url, *args, **kwargs):
                host = urlsplit(str(url)).hostname or ""
                if host not in _LOOPBACK_HOSTS:
                    _VIOLATIONS.append("curl:" + host)
                    raise OSError("blocked non-loopback curl request: " + host)
                return original(self, method, url, *args, **kwargs)
            return _wrapped

        _cls.request = _guard_request(_original)
'''

_DRIVER = _GUARDS + r'''
import sys

sys.path.insert(0, os.environ["CHAT2API_PROJECT_ROOT"])

import utils.configs as configs
from starlette.testclient import TestClient

import app as app_module

# 不进入 with 块：路由在 import 期注册，启动期任务与本次判据无关。
client = TestClient(app_module.app)
TOKEN = "eyJhbGciOiJIUzI1NitestonlyCHILDUPSTREAM1234567"

statuses = {}
for name, path, body in (
    ("conversation", "/backend-api/conversation", {"model": "gpt-5-5", "messages": []}),
    ("sentinel", "/backend-api/sentinel/chat-requirements", {"p": "testonly"}),
):
    resp = client.post(path, headers={"Authorization": "Bearer " + TOKEN}, json=body)
    statuses[name] = resp.status_code

print("MARKER_BASE_URLS=" + json.dumps(configs.chatgpt_base_url_list), flush=True)
print("MARKER_STATUS=" + json.dumps(statuses), flush=True)
print("MARKER_VIOLATIONS=" + json.dumps(_VIOLATIONS), flush=True)
print("MARKER_DONE=1", flush=True)
'''


def _markers(stdout: str) -> dict:
    out = {}
    for line in stdout.splitlines():
        if line.startswith("MARKER_") and "=" in line:
            key, value = line.split("=", 1)
            out[key] = value
    return out


@pytest.mark.skipif(not os.path.isdir(str(_PROJECT_ROOT / "templates")),
                    reason="需要 templates/ 才能 import app")
def test_no_sentinel_backend_routes_fail_closed_in_fresh_process(tmp_path):
    """``NO_SENTINEL=true`` 才会注册的两条 backend 路由：显式空配置下必须 503。

    常规套件里这两条路由不存在（``NO_SENTINEL=false``），而它们恰恰是直接
    ``chatgpt_base_url_list or ["https://chatgpt.com"]`` 的旧形态所在地，所以只能
    在全新进程里证明：env → configs → 路由注册 → 响应状态。
    """
    work_dir = tmp_path / "runtime"
    (work_dir / "data").mkdir(parents=True)
    shutil.copytree(_PROJECT_ROOT / "templates", work_dir / "templates")
    (work_dir / "version.txt").write_text("1.8.8-test\n", encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "CHAT2API_PROJECT_ROOT": str(_PROJECT_ROOT),
        "CHATGPT_BASE_URL": "",
        "ENABLE_GATEWAY": "true",
        "ENABLE_ANTIBAN": "false",
        "NO_SENTINEL": "true",
        "PROXY_URL": "",
        "SENTINEL_PROXY_URL": "",
        "ADMIN_PASSWORD": "test-admin-password",
        "FLEET_DB_PATH": str(work_dir / "data" / "chat2api.db"),
        "SESSION_DB_PATH": str(work_dir / "data" / "sessions.db"),
        "SCHEDULED_REFRESH": "false",
        "AUTO_SEED": "true",
        "RANDOM_TOKEN": "false",
    })
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER], cwd=str(work_dir), env=env,
        capture_output=True, text=True, timeout=_CHILD_TIMEOUT_SECONDS,
    )
    markers = _markers(result.stdout)
    assert markers.get("MARKER_DONE") == "1", (
        f"child did not finish: rc={result.returncode}\n"
        f"stdout tail: {result.stdout[-2000:]}\nstderr tail: {result.stderr[-2000:]}"
    )
    assert json.loads(markers["MARKER_BASE_URLS"]) == []
    statuses = json.loads(markers["MARKER_STATUS"])
    assert statuses == {"conversation": 503, "sentinel": 503}, statuses
    # 没有任何一次非回环的解析 / 连接 / curl 请求被尝试
    assert json.loads(markers["MARKER_VIOLATIONS"]) == []
