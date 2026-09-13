"""浏览器验收：运营台的「SaaS 账号状态」面板在真实 Chromium 里渲染出什么。

其他测试读的是字节（HTML / 脚本文本）。字节对只能证明「服务端写对了」，
证明不了浏览器**执行**之后运营者看到什么 —— 而这块面板的全部内容都是 JS 在
``GET /admin/users`` 之后画上去的，服务端渲染的只是一个空 shell。
所以这里让真实 Chromium 打开**真实渲染出来的页面字节**，喂给它**真实接口返回的
JSON**，读回 JS 跑完之后的 DOM。

探针以退出码区分两种失败：``42`` = 本机没有可用浏览器（skip），其余非零 = 探针
真的挂了（fail）—— 否则「浏览器断言」会在出错时静默变成「跳过」。
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time

import pytest

import utils.configs as configs
import utils.store as store
import utils.trials as trials

# 与 ``test_store_plan_cta_browser.py`` 同一套 playwright 查找顺序（含可覆盖项）。
PLAYWRIGHT_MODULE = os.environ.get(
    "PLAYWRIGHT_MODULE",
    "/Users/Zhuanz/.codex/skills/playwright-skill/node_modules/playwright",
)

# 退出码 42 = 无浏览器可用；其余非零 = 探针自身出错（必须失败，不能跳过）
_NO_BROWSER = 42

_LAUNCH = r"""
function loadChromium() {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    '__PW__',
    'playwright',
    'playwright-core',
  ];
  for (const c of candidates) {
    if (!c) continue;
    try { return require(c).chromium; } catch (e) {}
  }
  return null;
}
const chromium = loadChromium();
if (!chromium) { process.exit(42); }
async function withPage(url, fn) {
  let browser;
  try { browser = await chromium.launch(); }
  catch (e) { process.exit(42); }
  try {
    const page = await browser.newPage();
    // 等 DOMContentLoaded 而不是 load：这个页面从 CDN 拉 tailwind / chart.js，
    // 等 load 会把「CDN 通不通」变成这块面板的验收前提。面板自己的渲染由各探针
    // 用 waitForFunction 等（chart.js 不参与 SaaS 面板，模板里另有 typeof 守卫）。
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    return await fn(page);
  } finally { await browser.close(); }
}
""".replace("__PW__", PLAYWRIGHT_MODULE)

_TABLE_JS = _LAUNCH + r"""
withPage(process.argv[process.argv.length - 1], async (page) => {
  // 面板是异步 fetch 之后画上去的：等行出现，而不是等 load 事件。
  await page.waitForFunction(
    () => document.querySelectorAll('#saasUsersTable tr td').length > 0,
    { timeout: 15000 }
  );
  return page.evaluate(() => ({
    count: (document.getElementById('saasUsersCount') || {}).textContent.trim(),
    rows: Array.from(document.querySelectorAll('#saasUsersTable tr')).map((tr) =>
      Array.from(tr.querySelectorAll('td')).map((td) =>
        td.textContent.replace(/\s+/g, ' ').trim()
      )
    ),
  }));
}).then((r) => process.stdout.write(JSON.stringify(r)))
  .catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


# --------------------------------------------------------------- scenario setup

def _register(client, email, password="Browser-pw-123!"):
    client.get("/register")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    resp = client.post("/register", data={"email": email, "password": password,
                                          "csrf_token": csrf})
    assert resp.status_code in (200, 303), f"注册失败：{resp.status_code}"


def _checkout(client, plan_id):
    client.get(f"/checkout?plan={plan_id}")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post("/api/checkout",
                       data={"plan": plan_id, "csrf_token": csrf},
                       follow_redirects=False)


def _seed_lifecycle_scenario(client):
    """一个「到期冻结且分配失败」的用户 —— 面板上每一列都有话说。

    刻意不播任何号池账号：付款会成功、分配必然失败，``payment.awaiting_allocation``
    因此会带上真实的失败原因，运营台上那一列才不是空的。
    """
    email = "browser-lifecycle@example.test"
    _register(client, email)
    seed = store.get_user_auth(email)["seed"]
    for _ in range(trials.trial_state(email)["remaining"]):
        assert trials.settle(trials.reserve(seed), seed) is True
    assert _checkout(client, "plus-shared-1m").status_code == 303

    order_id = store.list_orders(email=email)[0]["order_id"]
    assert store.get_order(order_id)["status"] == "paid"
    with store._connect() as conn:
        conn.execute("UPDATE orders SET expires_at=? WHERE order_id=?",
                     (int(time.time()) - 60, order_id))
    from utils import seed_lifecycle
    assert seed_lifecycle.freeze_if_expired(seed) == "frozen"
    return email


def _fresh_trial_user(client):
    # 本地部分刻意与另一个场景不同：运营接口只回脱敏邮箱（``ab***@domain``），
    # 两个 ``browser-*`` 会被脱敏成同一个 ``br***@...``，测试就分不清行了。
    email = "alpha-fresh@example.test"
    _register(client, email)
    return email


# ------------------------------------------------------------------ probe runner

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_handler(pages, api):
    """静态页面 + 真实接口响应。页面里的 fetch 走同源相对路径，所以共用一个 handler。"""
    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # 静音：探针 stdout 只留 JSON
            pass

        def _send(self, body, content_type):
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802 (http.server 的接口名)
            path = self.path.split("?")[0]
            if path in pages:
                self._send(pages[path], "text/html; charset=utf-8")
                return
            if path in api:
                self._send(json.dumps(api[path]), "application/json")
                return
            self.send_error(404)
    return _Handler


def _run_probe(script, pages, api, path="/admin/routing"):
    node = shutil.which("node")
    if not node:
        pytest.skip("no node available for the browser probe")
    port = _free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _make_handler(pages, api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = subprocess.run(
            [node, "-e", script, "--", f"http://127.0.0.1:{port}{path}"],
            capture_output=True, text=True, timeout=120, env=dict(os.environ),
        )
    finally:
        httpd.shutdown()
    if out.returncode == _NO_BROWSER:
        pytest.skip("no browser available for the probe")
    assert out.returncode == 0, f"浏览器探针失败：{out.stderr.strip()[:600]}"
    return json.loads(out.stdout)


@pytest.fixture
def admin_headers(monkeypatch):
    """与 ``test_saas_product_gate.py`` 同一套运营者凭据（两个文件各自持有）。"""
    from gateway import admin
    monkeypatch.setattr(admin, "admin_password", "test-admin-secret")
    return {"Authorization": "Bearer test-admin-secret"}


@pytest.fixture
def admin_browser(client, admin_headers):
    """返回 ``probe(script)``：真实页面 + 真实接口 JSON → Chromium → 读回 DOM。"""
    _fresh_trial_user(client)
    _seed_lifecycle_scenario(client)

    page = client.get("/admin/routing", headers=admin_headers)
    assert page.status_code == 200, f"运营台页面不可达：{page.status_code}"
    users = client.get("/admin/users", headers=admin_headers).json()
    assert [u for u in users["users"] if u.get("failure", {}).get("reason")], \
        "场景没有制造出分配失败，面板的失败原因列会是空的"

    # 运营台首屏还会拉一次 routing/data —— 给一份最小的合法载荷，
    # 免得主面板的脚本先抛错。这块断言的对象是 SaaS 面板，不是它。
    routing_data = {
        "summary": {}, "accounts": [], "users": [], "ip_cards": [],
        "rules": [], "alerts": [], "proxy_options": [], "routing_config": {},
        "updated_at": "test",
    }

    def _probe(script=_TABLE_JS):
        return _run_probe(
            script,
            pages={"/admin/routing": page.text},
            api={"/admin/users": users, "/admin/routing/data": routing_data},
        )
    return _probe


def test_browser_admin_panel_renders_trial_expiry_frozen_and_failure(admin_browser):
    """面板必须把接口里的试用 / 到期 / 冻结 / 失败原因真的画到运营者眼前。"""
    got = admin_browser()
    assert got["count"] == "2 个账号", got["count"]
    assert len(got["rows"]) == 2, got["rows"]

    # 第一列是脱敏邮箱，用它定位两行（顺序无关）
    expired = next(r for r in got["rows"] if r[0].startswith("br***"))
    fresh = next(r for r in got["rows"] if r[0].startswith("al***"))

    # 到期冻结 + 待续费 + 分配失败原因，三件事都要在同一行里看得到
    expired_row = " | ".join(expired)
    assert "已冻结" in expired_row, expired_row
    assert "续费后恢复" in expired_row, expired_row
    assert "待续费" in expired_row, expired_row
    assert "未订阅" not in expired_row, expired_row
    # 试用余额仍然记账（3 次都结算过），但原因是「套餐已到期」而不是「已用完」——
    # 这一列存在的意义就是别把到期的用户说成账号有问题。
    assert "3/3" in expired_row, expired_row
    assert "套餐已到期" in expired_row, expired_row
    assert "已用完" not in expired_row, expired_row
    # 失败原因列是匿名代码，不是空白
    assert expired[-1] and expired[-1] != "-", expired_row

    # 新注册用户：试用可用 + 未订阅 + 未冻结
    fresh_row = " | ".join(fresh)
    assert "3/3" not in fresh_row, fresh_row
    assert "0/3" in fresh_row and "可用" in fresh_row, fresh_row
    assert "未订阅" in fresh_row, fresh_row
    assert "已冻结" not in fresh_row, fresh_row
    assert fresh[-1] == "-", fresh_row


def test_browser_admin_panel_shows_a_load_failure_instead_of_an_empty_table(client, admin_headers):
    """接口挂了要显示「加载失败」，而不是一张看起来「没有用户」的空表。"""
    page = client.get("/admin/routing", headers=admin_headers)
    assert page.status_code == 200
    routing_data = {
        "summary": {}, "accounts": [], "users": [], "ip_cards": [],
        "rules": [], "alerts": [], "proxy_options": [], "routing_config": {},
        "updated_at": "test",
    }
    probe = _LAUNCH + r"""
withPage(process.argv[process.argv.length - 1], async (page) => {
  await page.waitForFunction(
    () => document.querySelector('#saasUsersTable') &&
          document.querySelector('#saasUsersTable').textContent.trim().length > 0,
    { timeout: 15000 }
  );
  return page.evaluate(() => ({
    table: document.getElementById('saasUsersTable').textContent.replace(/\s+/g, ' ').trim(),
    count: (document.getElementById('saasUsersCount') || {}).textContent.trim(),
  }));
}).then((r) => process.stdout.write(JSON.stringify(r)))
  .catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""
    # 只登记 routing/data，不登记 /admin/users —— 该请求会 404
    got = _run_probe(probe, pages={"/admin/routing": page.text},
                     api={"/admin/routing/data": routing_data})
    assert "加载失败" in got["table"], got
    assert got["count"] == "--", got
