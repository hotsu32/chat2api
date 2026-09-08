# REVIEW_PACKET — 新前端会话未注册导致「一直加载/网络错误」修复（证据包）

> 记录实际命令输出与可复现证据，区分 Verified / Unverified / Needs User。

## 1. 问题（用户反馈）

刷新浏览器后发消息，页面「一直在加载，也没有回复」，最终前端循环报
`RequestError: Forbidden … source: paginated_conversation_initial` 与
`404 Not Found @ /backend-api/conversation/{id}`。

## 2. 根因（两点，均已修复）

| # | 文件:行 | 根因 | 修复 |
|---|---------|------|------|
| 1 | `gateway/reverseProxy.py:262` | Python 运算优先级：`not conversation_id or not title and chat_chunk.startswith(...)` 被求值为 `not conversation_id or (not title and startswith(...))`。`conversation_id` 初始为 None，恒真，导致对**新 2026 流格式** `data: {` 块错误走了旧格式分支（`chat_chunk[19:]`），JSON 损坏、`json.loads` 永远抛异常 → `save_conversation` 从不触发 → `conversations` 表为空 → `/conversation/{id}` 404。 | 加括号：`(not conversation_id or not title) and chat_chunk.startswith(...)` |
| 2 | `gateway/backend.py:35` | `banned_paths` 误封 `conversations/{uuid}`，前端 `paginated_conversation_initial` 加载历史被 403。 | 移除该条 ban。 |

## 3. 变更清单

| 文件 | 变更 |
|------|------|
| `gateway/reverseProxy.py` | 修复 #1 运算优先级；`except Exception as e:` → `except Exception:`（清理诊断期死代码） |
| `gateway/backend.py` | 修复 #2 移除 `conversations/{uuid}` ban |
| `gateway/f_conversation_gateway.py` | 还原诊断期 `[fc-debug]` 计数日志（净变更 = HEAD 一致） |

## 4. 测试证据（实际输出）

### 4.1 现有单元套件（不回归）

```
$ .venv/bin/pytest tests/
................................................                         [100%]
48 passed in 1.32s
```

### 4.2 E2E 套件（不回归）

```
$ .venv/bin/pytest tests_e2e/
........................................................................ [ 93%]
.....                                                                    [100%]
77 passed in 21.66s
```

## 5. 浏览器端到端证据（真实 loopback 上游）

Playwright 打开 `http://127.0.0.1:5005/`，发消息 `"reply with exactly: hello world"`：

- **回复正确渲染**：快照显示 `ChatGPT said: hello world`（此前为「A network error occurred」死循环）。
- **会话已注册**：侧边栏 `Recents` 出现标题为 `Hello World` 的会话，URL `/c/6aa03e6a-6e64-83ea-b272-0f3928df7690`；页面 `<title>` 同步为 `Hello World`。
- **`save_conversation` 已触发**（诊断日志，现已移除）：
  ```
  [cg-debug] got conversation_id='6aa03e6a-6e64-83ea-b272-0f3928df7690' from chunk=b'data: {"type":"resume_conversation_token",...'
  ```

## 6. 残余控制台报错（均非本缺陷）

剩余 87 条 console error 全部为**外部遥测/匿名化副作用**，与对话主链路无关：

- `ERR_SSL_PROTOCOL_ERROR` → `https://127.0.0.1:5005/ces/v1/rgstr`（Statsig/CES 用 HTTPS 打 HTTP 服务）。
- `Datadog Browser SDK … non-allowed domain`（遥测域名校验）。
- `Access to fetch at https://bzr.openai.com/v1/obi/sync … CORS`（第三方 bazaar cookie 同步，非本域）。
- `Minified React error #418`（bootstrap 注入导致的 hydration mismatch）。
- `User should have either email or phone number`（镜像 `/me` 匿名化后的预期现象）。

## 7. 证据可信度分级

| 结论 | 等级 | 依据 |
|------|------|------|
| 修复 #1 / #2 正确落地 | Verified | `git diff` 逐条核对 + 浏览器真实回复渲染 |
| 会话注册恢复 | Verified | 诊断日志 `got conversation_id=…` + 侧边栏会话落列 |
| `tests/` 不回归 | Verified | 48 passed（实际输出） |
| `tests_e2e/` 不回归 | Verified | 77 passed（实际输出） |
| 零真实凭据泄漏 | Verified | `git status` 仅 3 个 gateway 文件；无 `data/*`、无 token 值 |

## 8. 手动验收步骤

```bash
.venv/bin/pytest tests/          # 期望 48 passed
.venv/bin/pytest tests_e2e/      # 期望 77 passed
```

浏览器打开 `http://127.0.0.1:5005/` → 登录 → 发一条消息 → 确认回复正常渲染、
侧边栏出现该会话标题、URL 从 `WEB:<guid>` 落到 UUID 会话页。
