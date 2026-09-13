"""浏览器验收：真实 Chromium 里，超市页的预选到底落在哪个档上。

其他测试读的是渲染出来的字节（HTML/脚本文本）。字节对只能证明「服务端写对了」，
证明不了浏览器**执行**之后用户看到什么 —— 而这条缺陷的第二半恰好就在浏览器里：
``render()`` 在加载时按 ``state`` 重写 hidden 值与摘要，服务端渲染得再对，脚本一跑
就可能被打回默认档。所以这里让真实 Chromium 打开**真实渲染出来的字节**，读回 JS
跑完之后的 DOM。

静态服务器只替代资产分发（base.html 的外链样式在探针里 404，与选择逻辑无关）；
页面的脚本、DOM 与 CSS 都是产物本身。

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

import pytest

import utils.configs as configs

# 与 `browser/research_panel_probe.js` 同一套 playwright 查找顺序（含可覆盖项），
# 免得两处探针各认一个路径。
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
    await page.goto(url, { waitUntil: 'load' });
    return await fn(page);
  } finally { await browser.close(); }
}
""".replace("__PW__", PLAYWRIGHT_MODULE)

_PROBE_JS = _LAUNCH + r"""
withPage(process.argv[process.argv.length - 1], (page) => page.evaluate(() => {
  const selected = {};
  document.querySelectorAll('[data-axis]').forEach((axis) => {
    const on = axis.querySelector('.opt.is-selected');
    selected[axis.dataset.axis] = on ? on.dataset.value : null;
  });
  return {
    plan: document.getElementById('plan-id').value,
    selected: selected,
    summary: document.getElementById('sel-name').textContent.trim(),
  };
})).then((r) => process.stdout.write(JSON.stringify(r)))
  .catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""

_CLICK_JS = _LAUNCH + r"""
withPage(process.argv[process.argv.length - 1], async (page) => {
  await page.click('[data-axis="tier"] .opt[data-value="pro"]');
  return page.evaluate(() => ({
    plan: document.getElementById('plan-id').value,
    summary: document.getElementById('sel-name').textContent.trim(),
  }));
}).then((r) => process.stdout.write(JSON.stringify(r)))
  .catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 静音：探针 stdout 只留 JSON
        pass


def _serve(directory):
    port = _free_port()
    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(directory), **kw)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _run_probe(script, tmp_path, html, path="/store.html"):
    """把渲染好的 HTML 发到本地静态服务器，交给真实 Chromium 打开并取回结果。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("no node available for the browser probe")
    # 注意：``tmp_path / "/store.html"`` 会被绝对路径截断成 ``/store.html``，
    # 所以这里必须先去掉前导斜杠再拼接。
    rel = path.lstrip("/") or "store.html"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    httpd, port = _serve(tmp_path)
    try:
        out = subprocess.run(
            [node, "-e", script, "--", f"http://127.0.0.1:{port}/{rel}"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ),
        )
    finally:
        httpd.shutdown()
    if out.returncode == _NO_BROWSER:
        pytest.skip("no browser available for the probe")
    assert out.returncode == 0, f"浏览器探针失败：{out.stderr.strip()[:600]}"
    return json.loads(out.stdout)


def _sign_in(client, email):
    """注册一个账号并保持登录态（页面侧要登录才能看超市页）。

    TestClient 默认跟随 303 → /dashboard，所以成功时看到的是 200 而不是 303。
    """
    client.get("/register")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    resp = client.post("/register", data={"email": email, "password": "Browser-pw-123!",
                                          "csrf_token": csrf})
    assert resp.status_code in (200, 303), f"注册失败：{resp.status_code}"


@pytest.fixture
def browser_store(client, tmp_path, monkeypatch):
    """返回 ``probe(path, script)``：渲染登录态页面 → 浏览器执行 → 读回 DOM。"""
    import utils.store  # noqa: F401  （触发建表 / 目录准备）

    monkeypatch.setattr(configs, "require_email_verification", False)
    _sign_in(client, "browser-cta@example.test")

    def _probe(path, script=_PROBE_JS):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
        return _run_probe(script, tmp_path, resp.text)
    return _probe


def test_browser_store_preselection_matches_the_cta(browser_store):
    """CTA 带的档位必须一路活到浏览器里的提交值。"""
    pro = browser_store("/store?plan=pro-solo-1m")
    assert pro == {"plan": "pro-solo-1m", "summary": "Pro · 独享 · 1 个月",
                   "selected": {"tier": "pro", "density": "solo", "duration": "1m"}}

    plus = browser_store("/store?plan=plus-solo-1m")
    assert plus["plan"] == "plus-solo-1m" and plus["selected"]["tier"] == "plus"
    assert plus["summary"] == "Plus · 独享 · 1 个月"


def test_browser_default_and_invalid_plans_fall_back_to_plus(browser_store):
    """无查询串与非法查询串都退回默认档，且不能把任意值带进表单。"""
    for path in ("/store", "/store?plan=pro-solo-1m-evil", "/store?plan=free-solo-1m",
                 "/store?plan="):
        got = browser_store(path)
        assert got["plan"] == "plus-solo-1m", f"{path} 浏览器提交值 {got['plan']!r}"
        assert got["selected"]["tier"] == "plus"
        assert got["summary"] == "Plus · 独享 · 1 个月"


def test_browser_clicking_an_axis_still_rewrites_the_plan(browser_store):
    """预选不是把脚本钉死：用户点 Pro 之后提交值仍要跟着变。"""
    got = browser_store("/store", script=_CLICK_JS)
    assert got == {"plan": "pro-solo-1m", "summary": "Pro · 独享 · 1 个月"}
