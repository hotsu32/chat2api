# REVIEW_PACKET_demo.md — 演示 demo（free/plus 分组入口）证据包

> 记录实际命令输出，区分 Verified / Unverified / Needs User。

## 1. 变更清单

| 文件 | 变更 | 说明 |
|------|------|------|
| `gateway/demo.py` | 新增 | `/demo` 路由，下发 `demo.html`，注入两个演示 seed |
| `templates/demo.html` | 新增 | 分组选择页（Free 组 / Plus 组），OpenAI 风格，预填 token + 进入按钮 |
| `app.py` | +1 | `import gateway.demo`（`enable_gateway` 分支内） |
| `gateway/chatgpt.py` | +5 −2 | `/` 无 token 时 `302 → /demo`（原 `login_html`）；import 补 `RedirectResponse` |
| `tests_e2e/test_gateway_golden_path.py` | +4 −2 | 测试改名 `test_root_without_seed_redirects_to_demo`，断言 302 + demo 内容 |

**两个演示 token（自生成 seed，非 OpenAI 凭据）：**

| 组 | token | 绑定档位 |
|----|-------|----------|
| Free 组 | `demo-free-pool` | free |
| Plus 组 | `demo-plus-pool` | plus |

## 2. 测试证据（实际输出）

```
$ PYTHONPATH=. .venv/bin/python -m pytest tests/
................................................                         [100%]
48 passed in 1.35s

$ PYTHONPATH=. .venv/bin/python -m pytest tests_e2e/
........................................................................ [ 93%]
.....                                                                    [100%]
77 passed in 21.79s
```

## 3. 服务端行为证据（实际 curl 输出）

```
$ curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" http://127.0.0.1:5005/
302 http://127.0.0.1:5005/demo

$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5005/demo
200
```

## 4. Playwright 端到端证据

| 步骤 | 结果 |
|------|------|
| `/demo` 渲染 | 标题「选择分组 · ChatGPT 镜像 Demo」，两个卡片 + 预填 token 可见（快照确认） |
| 点「进入 Free 组」 | 进入官网反代，账号显示 **ChatGPT Free** |
| 点「进入 Plus 组」 | 进入官网反代，账号显示 **ChatGPT Plus** |
| 分组隔离 | free seed 绑定 free 号池账号、plus seed 绑定 plus 号池账号，互不串池 |

## 5. 安全证据

- 两个 demo token 为**自生成 seed**（组标识），非 OpenAI 凭据，不含 `WARNING_BANNER` 保护的真实 access_token / sessionToken。
- 真实凭据仍只经 secrets 机制进入运行环境，未写入任何 demo 文件、未出现在聊天/日志。
- `git status --short` 仅 3 个修改文件 + 2 个新增文件，无 `data/*`、无 `.env`、无 `.pyc`。

## 6. 证据可信度分级

| 结论 | 等级 | 依据 |
|------|------|------|
| demo 路由 + 页面 + 根重定向落地 | Verified | `git diff` 逐条核对 + curl 302/200 实际输出 |
| `tests/` 不回归 | Verified | 48 passed（实际输出） |
| `tests_e2e/` 全绿 | Verified | 77 passed（实际输出） |
| free/plus 分组隔离 | Verified | Playwright 快照 + account-status 对比 |
| 真实环境 E2E | Needs User | Playwright 走 loopback mock 上游，真实 chatgpt.com 链路需用户在真实环境验收 |
