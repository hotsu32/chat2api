"""ENABLE_ANTIBAN 环境开关的进程级运行时证据。

为什么必须是独立进程：本套件其余测试都在 pytest 进程里 monkeypatch ``configs``，
证明的是「开关为真时代码怎么走」，而不是「开关本身真的被读到了」。
``utils.configs`` 在导入时只读一次 env，所以只有「全新进程 + 全新 cwd」才能证明：

  * ``ENABLE_ANTIBAN=true`` 真的从环境进入 ``configs.enable_antiban``；
  * 启动期真的跑了 ``bulk_assign``、注册了 ``antiban_heal``、调度了版本自检；
  * 版本自检真的打到了 ``CHATGPT_BASE_URL`` 指向的 loopback；
  * 全程没有任何非 loopback 的解析/连接尝试（含 libcurl 那条不经过 Python socket 的路）。

隔离：临时 cwd + 临时 SQLite + loopback 假上游；``PROXY_URL`` / ``SENTINEL_PROXY_URL`` /
``IPQS_API_KEY`` 置空。不读不写真实 ``data/``，不打印任何凭据——报告里只有计数与枚举。

多 Worker 契约在同文件的 ``REFUSE`` 形态里验证：声明多 Worker 且无共享协调层时，
启动必须失败，且失败发生在任何外发之前。
"""

import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 探针超时给足：子进程要导入整个 app（FastAPI + curl_cffi + SQLite）。
_CHILD_TIMEOUT_SECONDS = 180
# 版本自检是 fire-and-forget 的 create_task。不赌固定 sleep 够长：
# 主动等探测落到假上游（或子进程退出）再收尾，避免慢机器上的假失败。
_PROBE_WAIT_SECONDS = 30
_PROBE_SETTLE_SECONDS = 2.0


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
    """只回一个能通过 version_check 解析的 HTML，并记录路径。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.records.append(self.path)
        body = f'<html data-build="{self.server.data_build}"><body>ok</body></html>'.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_upstream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    server.records = []
    server.data_build = "prod-testonly-runtime-smoke"
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# 子进程侧护栏：任何非 loopback 的解析 / 连接 / 应用层请求都被拒绝并记录。
#
# 两道而不是一道，因为应用自己的 HTTP 客户端是 curl_cffi（libcurl C 扩展），
# 它不走 Python 的 socket 模块——只拦 socket 会让「无真实外发」只覆盖一半。
# ---------------------------------------------------------------------------

_GUARDS = r'''
import json, os, socket, sys
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
    def _guard_request(original):
        def _wrapped(self, method, url, *args, **kwargs):
            host = urlsplit(str(url)).hostname or ""
            if host not in _LOOPBACK_HOSTS:
                _VIOLATIONS.append("curl:" + host)
                raise OSError("blocked non-loopback curl request: " + host)
            return original(self, method, url, *args, **kwargs)
        return _wrapped

    for _name in ("Session", "AsyncSession"):
        _cls = getattr(_cffi_requests, _name, None)
        if _cls is not None:
            _cls.request = _guard_request(_cls.request)
'''

_DRIVER_STARTUP = _GUARDS + r'''
sys.path.insert(0, os.environ["ANTIBAN_PROJECT_ROOT"])

import asyncio
import time

import utils.configs as configs
import utils.globals as globals
from starlette.testclient import TestClient

import app as app_module

# TestClient 的 with 块跑的就是 uvicorn 会跑的那条 lifespan / app_start 路径。
with TestClient(app_module.app):
    time.sleep(float(os.environ["ANTIBAN_PROBE_SETTLE_SECONDS"]))

from api.chat2api import scheduler

jobs = sorted(job.id for job in scheduler.get_jobs())
heal_job = scheduler.get_job("antiban_heal")

# 直接调用调度器上注册的可调用对象：证明注册的确实是自愈入口，
# 而不只是任务名对得上。预置的 degraded 桶在调用后应变为 healthy。
healthy_after_heal = None
if heal_job is not None:
    asyncio.run(heal_job.func())
    healthy_after_heal = sum(
        1 for b in globals.antiban_bucket["buckets"].values() if b.get("status") == "healthy"
    )

print("MARKER_ENABLED=" + str(configs.enable_antiban), flush=True)
print("MARKER_BASE_URLS=" + json.dumps(configs.chatgpt_base_url_list), flush=True)
print("MARKER_PROXY_COUNT=" + str(len(configs.proxy_url_list) + len(configs.sentinel_proxy_url_list)), flush=True)
print("MARKER_JOBS=" + json.dumps(jobs), flush=True)
print("MARKER_HEAL_FUNC=" + (heal_job.func.__name__ if heal_job is not None else ""), flush=True)
print("MARKER_HEAL_HEALTHY=" + str(healthy_after_heal), flush=True)
print("MARKER_VIOLATIONS=" + json.dumps(_VIOLATIONS), flush=True)
print("MARKER_DONE=1", flush=True)
'''

_DRIVER_REFUSE = _GUARDS + r'''
sys.path.insert(0, os.environ["ANTIBAN_PROJECT_ROOT"])

import utils.configs as configs
from starlette.testclient import TestClient

import app as app_module

failure = None
try:
    with TestClient(app_module.app):
        pass
except BaseException as exc:
    failure = type(exc).__name__ + "|" + str(exc)

print("MARKER_ENABLED=" + str(configs.enable_antiban), flush=True)
print("MARKER_STARTUP_FAILURE=" + str(failure), flush=True)
print("MARKER_VIOLATIONS=" + json.dumps(_VIOLATIONS), flush=True)
print("MARKER_DONE=1", flush=True)
'''


def _seed_degraded_bucket(work_dir: Path) -> None:
    """放一个已过冷却期的 degraded 桶，用来证明自愈任务真的会自愈。"""
    data_dir = work_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "antiban_bucket.json").write_text(
        json.dumps({
            "buckets": {
                "bkt::http://proxy.test": {
                    "proxy_url": "http://proxy.test",
                    "proxy_name": "loopback-test-proxy",
                    "status": "degraded",
                    "degraded_until": 1,  # 早就过期
                    "accounts": [],
                    "last_request_at": {},
                }
            },
            "account_index": {},
        }),
        encoding="utf-8",
    )


def _run_child(
    driver: str,
    base_url: str,
    tmp_path: Path,
    extra_env: dict,
    wait_for_probe=None,
) -> subprocess.CompletedProcess:
    """在全新 cwd + 全新 SQLite + loopback 假上游下跑一个全新的 python 进程。

    ``wait_for_probe`` 传入假上游时，先等探测真的落到它上面（或子进程退出）再收尾，
    这样「探测打到了 loopback」是主动观察到的，而不是靠一个固定 sleep 赌出来的。
    """
    work_dir = tmp_path / "runtime"
    (work_dir / "data").mkdir(parents=True)
    # app.py 用 cwd 相对的 Jinja 目录；version_check 用 cwd 相对的 version.txt。
    shutil.copytree(_PROJECT_ROOT / "templates", work_dir / "templates")
    (work_dir / "version.txt").write_text("1.8.8-test\n", encoding="utf-8")
    _seed_degraded_bucket(work_dir)

    env = dict(os.environ)
    env.update({
        "ANTIBAN_PROJECT_ROOT": str(_PROJECT_ROOT),
        "ANTIBAN_PROBE_SETTLE_SECONDS": str(_PROBE_SETTLE_SECONDS),
        "ENABLE_ANTIBAN": "true",
        "CHATGPT_BASE_URL": base_url,
        # 没有出口就没有真实外发；IPQS 无 key 时 iprep fail-open 且不发请求。
        "PROXY_URL": "",
        "SENTINEL_PROXY_URL": "",
        "IPQS_API_KEY": "",
        "FLEET_DB_PATH": str(work_dir / "data" / "chat2api.db"),
        "SESSION_DB_PATH": str(work_dir / "data" / "sessions.db"),
        "ADMIN_PASSWORD": "test-admin-password",
        "ENABLE_GATEWAY": "true",
        "ENABLE_SESSION_STICKY": "false",
        "SCHEDULED_REFRESH": "false",
        "AUTO_SEED": "true",
        "RANDOM_TOKEN": "false",
        # fleet 探活默认 24h 一次；把它推到几乎不触发，避免任何真实上游探活
        "CIRCUIT_DEAD_ACCOUNT_RECHECK_HOURS": "8760",
        "SMTP_HOST": "",
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
        "REQUIRE_EMAIL_VERIFICATION": "false",
        "TURNSTILE_SITE_KEY": "",
        "TURNSTILE_SECRET_KEY": "",
    })
    # 部署形态必须由本测试显式声明，否则会继承本机 shell 的值。
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "WORKERS"):
        env.pop(var, None)
    env.update(extra_env)

    process = subprocess.Popen(
        [sys.executable, "-c", driver],
        cwd=str(work_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if wait_for_probe is not None:
            deadline = time.monotonic() + _PROBE_WAIT_SECONDS
            while not wait_for_probe.records and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
        stdout, stderr = process.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise AssertionError(f"child did not finish within {_CHILD_TIMEOUT_SECONDS}s")

    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _markers(stdout: str) -> dict:
    return {
        key: value
        for key, value in re.findall(r"^MARKER_([A-Z_]+)=(.*)$", stdout, flags=re.MULTILINE)
    }


# ---------------------------------------------------------------------------
# 形态一：单 Worker（当前 shipped 形态）——env 开关必须真的把 antiban 跑起来
# ---------------------------------------------------------------------------

def test_enable_antiban_env_starts_runtime_jobs_against_loopback_only(fake_upstream, tmp_path):
    result = _run_child(_DRIVER_STARTUP, fake_upstream.url, tmp_path, {}, wait_for_probe=fake_upstream)
    markers = _markers(result.stdout)

    assert result.returncode == 0, f"child failed:\n{result.stdout}\n{result.stderr}"
    assert markers.get("DONE") == "1", f"child did not finish:\n{result.stdout}\n{result.stderr}"

    # 1. 开关来自环境，而不是被 monkeypatch。
    assert markers["ENABLED"] == "True", "ENABLE_ANTIBAN=true must reach configs.enable_antiban"

    # 2. 上游基址是 loopback，且没有配置任何出口代理：
    #    真实外发既没有目标也没有通道。
    assert json.loads(markers["BASE_URLS"]) == [fake_upstream.url]
    assert markers["PROXY_COUNT"] == "0"

    # 3. 启动期任务真的注册在调度器上，且自愈入口就是 circuit.scheduled_heal。
    jobs = json.loads(markers["JOBS"])
    assert "antiban_heal" in jobs, f"antiban_heal job missing: {jobs}"
    assert "fleet_health_check" in jobs, f"fleet_health_check job missing: {jobs}"
    assert markers["HEAL_FUNC"] == "scheduled_heal"

    # 4. 自愈任务真的能自愈：预置的 degraded 桶在调用后变 healthy。
    assert markers["HEAL_HEALTHY"] == "1"

    # 5. 版本自检真的打出去了——打到 loopback，且是它自己发起的。
    assert fake_upstream.records == ["/"], f"unexpected upstream calls: {fake_upstream.records}"

    # 6. 全程没有任何非 loopback 的解析 / 连接 / 应用层请求。
    assert json.loads(markers["VIOLATIONS"]) == [], "non-loopback outbound was attempted"

    # 启动期四条匿名标记逐条核对（冒烟抓日志时看到的就是这几行）。
    log = result.stderr
    assert "[antiban] coordination=single_process_assumed capacity_scope=process" in log
    assert "[antiban] bulk_assign result: assigned=" in log
    assert "[antiban] enabled | buckets=1 accounts=0 healthy=0 degraded=1" in log
    assert "[antiban] version_check OK" in log


def test_startup_logs_carry_no_credential_like_identifiers(fake_upstream, tmp_path):
    """启动日志只应有计数与枚举：不出现 token、代理串、上游原文。"""
    result = _run_child(_DRIVER_STARTUP, fake_upstream.url, tmp_path, {}, wait_for_probe=fake_upstream)

    assert result.returncode == 0, f"child failed:\n{result.stdout}\n{result.stderr}"
    log = result.stdout + result.stderr
    for needle in ("chatgpt.com", "proxy.test:secret", "Bearer ", "eyJhbGciOi"):
        assert needle not in log, f"startup output leaked {needle!r}"


# ---------------------------------------------------------------------------
# 形态一之补：显式留空 CHATGPT_BASE_URL —— 不得回落到真实主机
# ---------------------------------------------------------------------------

def test_empty_base_url_does_not_fall_back_to_the_real_host(fake_upstream, tmp_path):
    """``CHATGPT_BASE_URL=""`` 的意图就是「不要碰真实主机」。

    RED 形态：旧实现 `chatgpt_base_url_list or ["https://chatgpt.com"]`，
    于是启用了 antiban 的部署会在启动期向真实 chatgpt.com 发一次 HTTPS 探测。
    假上游必须一个请求都收不到，且子进程护栏一次外发尝试都没记录到。
    """
    result = _run_child(_DRIVER_STARTUP, "", tmp_path, {})
    markers = _markers(result.stdout)

    assert result.returncode == 0, f"child failed:\n{result.stdout}\n{result.stderr}"
    assert markers.get("DONE") == "1", f"child did not finish:\n{result.stdout}\n{result.stderr}"
    assert json.loads(markers["BASE_URLS"]) == []
    assert json.loads(markers["VIOLATIONS"]) == [], "an outbound attempt was made with no base URL configured"
    assert fake_upstream.records == []
    assert "[antiban] version_check skipped (no CHATGPT_BASE_URL configured)" in result.stderr
    # 其余启动路径不受影响：开关仍然是开的，任务照样注册
    assert markers["ENABLED"] == "True"
    assert "antiban_heal" in json.loads(markers["JOBS"])


# ---------------------------------------------------------------------------
# 形态二：声明多 Worker 且无共享协调层——必须启动期拒绝，且拒绝在任何外发之前
# ---------------------------------------------------------------------------

def test_declared_multi_worker_refuses_startup_before_any_outbound(fake_upstream, tmp_path):
    result = _run_child(_DRIVER_REFUSE, fake_upstream.url, tmp_path, {"WEB_CONCURRENCY": "4"})
    markers = _markers(result.stdout)

    assert markers.get("DONE") == "1", f"child did not finish:\n{result.stdout}\n{result.stderr}"
    failure = markers.get("STARTUP_FAILURE", "")
    assert "UncoordinatedMultiWorkerError" in failure, f"startup did not fail closed: {failure!r}"
    assert "workers x cap" in failure, "the refusal must state the real dilution it is preventing"

    # 拒绝发生在任何外发之前：版本自检根本没被调度。
    assert fake_upstream.records == []
    assert json.loads(markers["VIOLATIONS"]) == []

    log = result.stderr
    assert "multi_process_uncoordinated" in log
    assert "ENABLE_ANTIBAN" in log


def test_single_worker_declaration_is_accepted(fake_upstream, tmp_path):
    """WEB_CONCURRENCY=1 是 shipped 形态，必须照常启动。"""
    result = _run_child(_DRIVER_STARTUP, fake_upstream.url, tmp_path, {"WEB_CONCURRENCY": "1"},
                        wait_for_probe=fake_upstream)
    markers = _markers(result.stdout)

    assert result.returncode == 0, f"child failed:\n{result.stdout}\n{result.stderr}"
    assert markers["ENABLED"] == "True"
    assert "[antiban] coordination=single_process capacity_scope=process" in result.stderr
    assert fake_upstream.records == ["/"]


# ---------------------------------------------------------------------------
# 形态二之补：worker 声明读不出正整数 / 声明了用不上的协调层
#
# 这两类都不能被读成「单进程」：
#   * WEB_CONCURRENCY=0 在 gunicorn 里是「按 CPU 数展开」，不是「一个 worker」；
#   * auto / 4,4 这类值既不是 1 也不是可解析的整数。
# 声明了共享协调层同理：本层没有协调客户端，静默按进程内状态跑就是 fail open。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("declaration", ["0", "auto", "4,4"])
def test_unusable_worker_declaration_refuses_startup(fake_upstream, tmp_path, declaration):
    result = _run_child(_DRIVER_REFUSE, fake_upstream.url, tmp_path,
                        {"WEB_CONCURRENCY": declaration})
    markers = _markers(result.stdout)

    assert markers.get("DONE") == "1", f"child did not finish:\n{result.stdout}\n{result.stderr}"
    failure = markers.get("STARTUP_FAILURE", "")
    assert "UncoordinatedMultiWorkerError" in failure, f"startup did not fail closed: {failure!r}"
    assert "WEB_CONCURRENCY" in failure, "the refusal must name the variable to fix"

    assert fake_upstream.records == []
    assert json.loads(markers["VIOLATIONS"]) == []
    assert "multi_process_uncoordinated" in result.stderr


def test_declared_coordinator_refuses_startup_without_leaking_its_credentials(fake_upstream, tmp_path):
    """声明了协调层但本层没有客户端：拒绝启动，且不得把端点（含密码）写进日志。"""
    result = _run_child(
        _DRIVER_REFUSE, fake_upstream.url, tmp_path,
        {"ANTIBAN_COORDINATOR_URL": "redis://coordinator-user:coordinator-secret@"
                                    "coordinator.test:6379/0"},
    )
    markers = _markers(result.stdout)

    assert markers.get("DONE") == "1", f"child did not finish:\n{result.stdout}\n{result.stderr}"
    failure = markers.get("STARTUP_FAILURE", "")
    assert "UnusableCoordinatorError" in failure, f"startup did not fail closed: {failure!r}"
    assert "ANTIBAN_COORDINATOR_URL" in failure, "the refusal must name the variable to remove"

    log = result.stdout + result.stderr
    for needle in ("coordinator-secret", "coordinator-user", "coordinator.test", "6379"):
        assert needle not in log, f"the coordinator endpoint leaked {needle!r}"

    # 拒绝发生在任何外发之前
    assert fake_upstream.records == []
    assert json.loads(markers["VIOLATIONS"]) == []


def test_declared_coordinator_never_starts_a_multi_worker_deployment(fake_upstream, tmp_path):
    """声明协调层不能解锁多 Worker：两件事都不成立就必须起不来。"""
    result = _run_child(
        _DRIVER_REFUSE, fake_upstream.url, tmp_path,
        {"WEB_CONCURRENCY": "4",
         "ANTIBAN_COORDINATOR_URL": "redis://coordinator.test:6379/0"},
    )
    markers = _markers(result.stdout)

    assert markers.get("DONE") == "1"
    failure = markers.get("STARTUP_FAILURE", "")
    assert "UnusableCoordinatorError" in failure, (
        f"a declared coordinator must not unlock multi-worker startup: {failure!r}"
    )
    assert fake_upstream.records == []
