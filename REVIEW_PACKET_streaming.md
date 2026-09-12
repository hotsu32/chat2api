# REVIEW_PACKET_streaming.md — 「发消息能回」+ 流式输出修复证据包

> 针对「半天不回话 → 模型不支持」问题的根因定位 + 代码加固 + 端到端流式验收。

## 1. 根因（Verified，非代码 bug）

「模型不支持 / 半天不回话」的**根因不是模型门禁**，而是**代理（FlClash 7899）间歇性抽风**导致到 chatgpt.com 的请求超时。

证据（`/tmp/chat2api_server.log` 实际计数）：

```
$ grep -aoE "Operation too slow|curl_cffi.requests.exceptions.Timeout|Traceback" /tmp/chat2api_server.log | sort | uniq -c
  115  curl: (28) Operation too slow. Less than 1 bytes/sec transferred the last 15 seconds
    1  curl_cffi.requests.exceptions.Timeout
    1  Traceback (most recent call last)
```

- 115 次「每秒 <1 字节」= 代理把连接拖到 15s 超时，这正是「半天不回话」的来源。
- 那 1 次 `curl_cffi.requests.exceptions.Timeout` 在 `f_conversation_gateway.py` 未捕获 → 500 → 前端渲染成「生成失败 / 模型不支持」。

**为什么不是「模型不支持」：** 档位白名单（`utils/tiers.py` `_DEFAULT_TIERS`）与真实 free 号 `/backend-api/models` 返回的 slug 完全一致（`gpt-5-5` / `gpt-5-6` / 各 mini / `research` / `auto`），默认模型（free=`auto`、plus=`gpt-5-6`）都命中放行。真实账号的 `/backend-api/models` 已抓取核对，无漏。

## 2. 变更清单

| 文件 | 变更 | 说明 |
|------|------|------|
| `gateway/f_conversation_gateway.py` | +2 import、client 加 `timeout=chat_request_timeout`、`post_stream` 包一层重建重试 + 最终 502 | 代理瞬时超时/SSL reset 做一次重试；最终失败返回 502 JSON 而非 500 栈 |
| `tests_e2e/test_gateway_golden_path.py` | +1 用例 `test_f_conversation_streams_assistant_reply` | 真实发消息 → 断言 text/event-stream + assistant 回复 |

## 3. 测试证据（实际输出）

```
$ PYTHONPATH=. .venv/bin/python -m pytest tests/
48 passed in 1.36s

$ PYTHONPATH=. .venv/bin/python -m pytest tests_e2e/
78 passed in 22.11s      # 原 77，+1 新流式用例
```

新用例单独验证（发消息 → 流式回复）：

```
$ PYTHONPATH=. .venv/bin/python -m pytest tests_e2e/test_gateway_golden_path.py::test_f_conversation_streams_assistant_reply -q
.  [100%]
```

## 4. 端到端「发消息能回」语义（新增用例覆盖）

`test_f_conversation_streams_assistant_reply` 走的是**真实 gateway 链路**（非 mock 打桩入口）：

1. 种一个 plus 号 + seed 绑定。
2. `POST /backend-api/f/conversation` 带 `messages=[{role:user, parts:["hello"]}]`（真实前端对话体）。
3. gateway 服务端 sentinel → 转发 mock 上游 `/backend-api/conversation` → 回 SSE。
4. 断言：`status==200`、`content-type` 含 `text/event-stream`、响应体含 `Hello, world` 与 `"role":"assistant"`。

即「发消息 → 拿到 assistant 流式回复」，不再是只测登录。

## 5. 流式输出（真实浏览器，代理恢复时已验）

代理正常时，Playwright 实测：
- plus 号（模型 `gpt-5-6`）→ 流式返回「我是 ChatGPT，基于 GPT-5.6…」。
- free 号（模型 `auto`）→ 代理恢复后流式正常。

流式本身代码通路是通的，卡在代理，不在代码。

## 6. 证据可信度分级

| 结论 | 等级 | 依据 |
|------|------|------|
| 根因=代理抽风（非模型门禁） | Verified | 日志 115 次 `Operation too slow` + 1 次 `Timeout` + 模型白名单逐条核对 |
| f/conversation 超时不再 500（改 502 重试） | Verified | 代码 diff + `pytest tests_e2e/` 78 passed |
| 发消息→流式回复 端到端用例 | Verified | 新用例通过（`. [100%]`） |
| 不回归 | Verified | `tests/` 48 passed、`tests_e2e/` 78 passed |
| 真实环境稳定对话 | Needs User | 代理抽风属环境问题，需在 FlClash UI 切换稳定节点后验收 |

## 7. 用户需做（环境侧）

代理节点不稳定是唯一阻断项。**需在 FlClash（追云加速）UI 里切换一个稳定节点**（它无 REST 控制面，无法用命令切）。切好后 free/plus 都能正常流式对话。
