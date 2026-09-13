"""显式空 CHATGPT_BASE_URL：规范化聊天链路必须本地失败，不回落到任何公网主机。

判据（只看外呼与结果，不看实现细节）：
  1. 上游目标只有一个出口。显式留空（``chatgpt_base_url_list`` 为空，或整串都是空白）
     = 没有目标：取目标这一步就失败，异常里不含任何主机名；
  2. 已配置的目标（含回环 / 自定义路径前缀）逐字返回，不被改写、不被裁剪；
  3. ``ChatService.initialize_request_context`` 在没有目标时于**构造任何 HTTP 客户端之前**
     本地 503——没有客户端就没有连接，凭据与对话体也就无从外发；
  4. 归属文件里不存在「三元表达式兜底到字面 http(s) 主机名」这种写法：这是本次缺陷的
     形状（``chatgpt_base_url_list or ["https://chatgpt.com"]`` 的等价物），
     静态扫描比逐个断言更能挡住未来新增的同类回落。

隔离：所有出站出口被替换（curl_cffi Client 替身 / 构造即失败），socket 侧只放行回环。
"""

import ast
import contextlib
import socket
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

import utils.configs as configs
import utils.globals as globals

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 归属文件：本轮改动范围。任一文件出现兜底主机名都算回归。
OWNED_FILES = (
    "utils/configs.py",
    "chatgpt/ChatService.py",
    "gateway/backend.py",
    "gateway/reverseProxy.py",
    "gateway/f_conversation_gateway.py",
)

# 测试专用假凭据（JWT 形状，足以走完 token 归一化；值本身无意义）。
FAKE_TOKEN = "eyJhbGciOiJIUzI1NitestonlyUPSTREAM1234567890"

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::", "0.0.0.0", "", "none"}


class _NoClient:
    """一构造就失败：用来证明「没有目标时不构造客户端」。"""

    constructed = []

    def __init__(self, *args, **kwargs):
        type(self).constructed.append(kwargs)
        raise AssertionError("no HTTP client may be constructed without a configured base URL")


class _SpyClient:
    """记录构造的 Client 替身（只用于「已配置目标」的对照路径）。"""

    constructed = []

    def __init__(self, *args, **kwargs):
        type(self).constructed.append(kwargs)

    async def close(self):
        return None

    async def discard(self):
        return None

    async def post(self, *args, **kwargs):
        raise AssertionError("no upstream request in this test")

    async def get(self, *args, **kwargs):
        raise AssertionError("no upstream request in this test")


def _fake_fp(req_token):
    return {
        "user-agent": "Mozilla/5.0 (Test) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
        "impersonate": "chrome130",
        "proxy_url": None,
    }


@pytest.fixture(autouse=True)
def _loopback_only_egress(monkeypatch):
    """任何非回环的解析/连接都必须在真实 socket 之前被拦下并让测试失败。"""
    attempts = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def _host_key(host):
        if isinstance(host, (bytes, bytearray)):
            host = host.decode("utf-8", "replace")
        return str(host).lower()

    def _guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        attempts.append(f"connect:{_host_key(host)}")
        if _host_key(host) not in LOOPBACK_HOSTS:
            raise AssertionError(f"forbidden non-loopback connect: {host}")
        return real_connect(self, address)

    def _guarded_getaddrinfo(host, *args, **kwargs):
        attempts.append(f"resolve:{_host_key(host)}")
        if _host_key(host) not in LOOPBACK_HOSTS:
            raise AssertionError(f"forbidden non-loopback resolve: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    return attempts


@pytest.fixture(autouse=True)
def _isolate_chatservice(monkeypatch):
    """ChatService 的真实出口全部打桩；代理与指纹不读本机真实配置。"""
    import chatgpt.ChatService as chat_service_mod
    import chatgpt.fp as fp_mod
    import utils.routing as routing_mod
    from utils.antiban import bucket, circuit, concurrency, cooldown, fingerprint, geo

    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    globals.antiban_dead_tokens.clear()
    globals.antiban_bucket = {"buckets": {}, "account_index": {}}
    _NoClient.constructed = []
    _SpyClient.constructed = []

    monkeypatch.setattr(bucket, "assign_account", lambda token: None)
    monkeypatch.setattr(bucket, "get_bucket_proxy", lambda token: None)
    monkeypatch.setattr(geo, "get_geo", lambda proxy_url: None)
    monkeypatch.setattr(fingerprint, "ensure_extended", lambda token: {})
    monkeypatch.setattr(circuit, "_persist_dead", lambda: None)
    monkeypatch.setattr(configs, "proxy_url_list", [])
    monkeypatch.setattr(configs, "sentinel_proxy_url_list", [])
    monkeypatch.setattr(chat_service_mod, "sentinel_proxy_url_list", [])
    monkeypatch.setattr(chat_service_mod, "get_fp", _fake_fp)
    monkeypatch.setattr(fp_mod, "get_bound_proxy", lambda req_token: None)
    monkeypatch.setattr(routing_mod, "get_bound_proxy", lambda req_token: None)
    monkeypatch.setattr(configs, "enable_antiban", False)
    monkeypatch.setattr(configs, "enable_limit", False)
    yield


# ---------------------------------------------------------------------------
# 上游目标出口：显式留空 = 没有目标
# ---------------------------------------------------------------------------

def test_pick_base_url_fails_closed_when_explicitly_empty(monkeypatch):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [])
    with pytest.raises(configs.UpstreamNotConfigured) as exc:
        configs.pick_chatgpt_base_url()
    # 有界：异常里不得出现任何主机名（它会被写进日志/响应）
    assert "chatgpt.com" not in str(exc.value)


def test_pick_base_url_treats_blank_entries_as_no_target(monkeypatch):
    """``CHATGPT_BASE_URL=','`` 之类的空白项不是目标，不能变成相对 URL 的起点。"""
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["", "   "])
    with pytest.raises(configs.UpstreamNotConfigured):
        configs.pick_chatgpt_base_url()


@pytest.mark.parametrize("configured", [
    "http://127.0.0.1:9",
    "http://127.0.0.1:9/base/",
    "https://upstream.example",
])
def test_pick_base_url_returns_configured_target_verbatim(monkeypatch, configured):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [configured])
    assert configs.pick_chatgpt_base_url() == configured


def test_pick_base_url_chooses_among_configured_targets(monkeypatch):
    targets = ["http://127.0.0.1:9", "http://127.0.0.1:8"]
    monkeypatch.setattr(configs, "chatgpt_base_url_list", targets)
    assert configs.pick_chatgpt_base_url() in targets


# ---------------------------------------------------------------------------
# 规范化 ChatService：无目标 = 本地 503，且不构造客户端
# ---------------------------------------------------------------------------

async def test_chatservice_fails_closed_before_constructing_any_client(monkeypatch):
    import chatgpt.ChatService as chat_service_mod
    from chatgpt.ChatService import ChatService

    monkeypatch.setattr(configs, "chatgpt_base_url_list", [])
    monkeypatch.setattr(chat_service_mod, "Client", _NoClient)

    svc = ChatService(FAKE_TOKEN)
    with pytest.raises(HTTPException) as exc:
        await svc.initialize_request_context()

    assert exc.value.status_code == 503
    assert "chatgpt.com" not in str(exc.value.detail)
    assert _NoClient.constructed == []
    # 没有客户端挂上，也就没有任何连接可复用/泄漏
    assert svc.s is None and svc.ss is None
    await svc.close_client()


async def test_chatservice_uses_configured_target_unchanged(monkeypatch):
    """对照：配置了自定义上游时，目标逐字使用，客户端照旧构造。"""
    import chatgpt.ChatService as chat_service_mod
    from chatgpt.ChatService import ChatService

    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["http://127.0.0.1:9/base/"])
    monkeypatch.setattr(chat_service_mod, "Client", _SpyClient)

    svc = ChatService(FAKE_TOKEN)
    # resolve_auth_context 的产物（生产路径由 set_dynamic_data 先行填充）
    svc.access_token = None
    await svc.initialize_request_context()

    assert svc.host_url == "http://127.0.0.1:9/base/"
    assert svc.base_headers["origin"] == "http://127.0.0.1:9/base/"
    assert svc.base_headers["referer"] == "http://127.0.0.1:9/base//"
    assert _SpyClient.constructed, "configured upstream must still construct a client"
    await svc.close_client()


# ---------------------------------------------------------------------------
# 静态判据：归属文件里不得再有「三元兜底到字面主机名」
# ---------------------------------------------------------------------------

def _hardcoded_public_fallbacks(source: str):
    """找出 ``X if cond else "https://..."`` 形状的兜底。返回 [(行号, 字面量)]。"""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.IfExp):
            continue
        orelse = node.orelse
        if isinstance(orelse, ast.Constant) and isinstance(orelse.value, str) \
                and orelse.value.startswith(("http://", "https://")):
            hits.append((node.lineno, orelse.value))
    return hits


@pytest.mark.parametrize("relpath", OWNED_FILES)
def test_owned_files_have_no_hardcoded_public_fallback(relpath):
    source = (PROJECT_ROOT / relpath).read_text(encoding="utf-8")
    hits = _hardcoded_public_fallbacks(source)
    assert hits == [], (
        f"{relpath} 仍有兜底到字面主机名的写法（显式空配置时会外呼未配置的站点）: {hits}"
    )


@pytest.mark.parametrize("relpath", OWNED_FILES)
def test_owned_upstream_call_sites_use_the_shared_accessor(relpath):
    """上游目标只允许从共享出口取：归属文件里不得直接 random.choice(base_url_list)。"""
    source = (PROJECT_ROOT / relpath).read_text(encoding="utf-8")
    if relpath == "utils/configs.py":
        assert "def pick_chatgpt_base_url" in source
        return
    assert "pick_chatgpt_base_url" in source, f"{relpath} 没有走共享上游出口"
    assert "random.choice(chatgpt_base_url_list)" not in source, (
        f"{relpath} 仍在本地直接挑选上游目标，绕过了失败闭合"
    )
