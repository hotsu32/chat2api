# M5 验收发现（阶段性）

本文件记录验收工具已经测到的事实。**这不是最终验收结论**——按分工，最终 fresh
review 由 Codex 在代码冻结后另开进行。此处每条结论都指向 `evidence/` 下的具体
记录文件，可独立复核。

生成次数：10/24（含 1 次已作废证据的重跑）。

---

## 1. 核心发现：缺陷只发生在新建会话，与 tier、机型、缓存都无关

这是目前最有价值的一条线索，也是唯一一条被 2x2 对照实验隔离出来的变量。

| | cold cache | warm cache |
|---|---|---|
| **新建会话** | FAIL：0 次增量，按钮 4451 ms | FAIL：0 次增量，按钮 4646 ms |
| **续聊** | PASS：9 次增量，按钮提前 1476 ms | PASS：8 次增量，按钮提前 383 ms |

同一账号（plus-2）、同一机型（desktop）、同一探针版本（v3），四格只差
"新建/续聊" 和 "冷/热缓存" 两个维度。缓存翻转不改变结果，线程类型翻转则完全
改变结果。

三个 tier 在续聊下全部通过：

| tier | 终态前 DOM 增量 | 按钮相对终帧 | 证据 |
|---|---|---|---|
| free | 12 | 提前 432 ms | `turn-free-desktop-continue-warm.json` |
| plus | 8 | 提前 383 ms | `turn-plus-desktop-continue-warm.json` |
| pro | 14 | 提前 425 ms | `turn-pro-desktop-continue-warm.json` |

**给修复者的含义**：流式渲染管线本身是好的。问题出在新建会话这条路径上——
会话尚未落库时的那段渲染逻辑。不必去查 SSE 解析或按钮组件。

## 2. 问题 2（增量渲染）：新建会话确认失败，且归因明确

新建会话下，SSE 确实送达了内容，DOM 却没有随之增长：

| 单元 | 内容帧 | 字符数 | 终态前增量 | 证据 |
|---|---|---|---|---|
| plus / mobile / new / cold | 5 | 2062 | 0 | `turn-plus-mobile-new-cold.json` |
| pro / desktop / new / cold | 5 | 2025 | 0 | `turn-pro-desktop-new-cold.json` |
| plus / desktop / new / warm | 6 | 2839 | 0 | `turn-plus-desktop-new-warm.json` |

答案最终在终帧**之后**一次性提交到 DOM。另有一次零成本 reload 验证：失败轮次的
739 字答案在服务端完整存在，reload 后正常渲染。

**归因**：渲染缺陷，不是上游、不是取流失败。

free 和 plus 的 desktop/new/cold 两格由 v1 探针采集，仅记录到 "0 次增量" 这一
事实；当时没有内容帧计数，因此报告中标注为"未归因"。增量数本身有效（直接读
DOM，后续修复未改变其含义）。

## 3. 问题 3（终态按钮 <=500 ms）：新建失败，续聊通过

新建会话：四个控件在终帧后约 4.4-4.7 秒才挂载，全部超出 500 ms 预算
（free 4584 / plus 4451 / pro 4707 / plus-warm 4646 ms）。

续聊：四个控件在终帧**之前**就已挂载并可点击（提前 383-1476 ms）。

按照第 1 节的结论，按钮延迟很可能是新建会话渲染缺陷的下游表现，而不是独立
问题——内容一次性提交完成后才轮到控件挂载。修复问题 2 后应重测本项。

## 4. 已作废的早期结论

以下内容曾被报告过，现已确认不可信，**不可作为依据**：

- **"8-9 个 error frame"**（free / plus desktop-new-cold）：v1/v2 探针用
  `body.includes('"error"')` 计数，而协议在正常帧上带 `error: null`。健康的流
  被计成了错误。已改为真正解析 JSON。
- **"selector scope wrong"**（plus continue/warm 按钮格）：v1 的
  `suspect_pre_terminal_mount` 对任何早于终帧挂载的控件报警，而这正是快速轮次
  的正常情况。v3 重测后该格为 PASS。
- **经 pro-4 alias 取得的任何早期 "Pro 证据"**：该 alias 曾回退到 plus-2 的
  账号（见第 5 节）。

现在每条记录都带 `instrumentVersion`，验收规则声明每个字段从哪一版起可信，早期
记录不会被按新规则重新解读。

## 5. P1：alias 绑定会被运行中的服务静默回退

`frontend-proof-pro-4` 再次出现 `mismatch`，与 plus-2 共用账号。直接查 SQLite
证明数据库本身是对的（pro-4 指向 `acct#d60c2b8be8`，`plan_type=pro`），是运行中
服务的内存 `seed_map` 覆盖了它。

处理顺序：停服 -> 重跑 fixture -> 重启。之后四个 alias 全部 `consistent`，
`bindings_distinct: true`。

**给 Codex 的提醒**：重启应用会静默撤销 DB 层的重新绑定。任何依赖 tier 隔离的
验收之前都要重跑 `alias_check`。

## 6. P1：聊天页 client-bootstrap 内嵌池账号真实 AccessToken

摘要 `0a7f5dd5b147`。`gateway/reverseProxy.py:437` 在服务端覆写 authorization
头，因此该 token 对前端没有用途，属于不必要暴露。尚未复测。

## 7. 四工具：可用性已盘点，功能尚未驱动

用 `tool_probe.js` 零成本盘点了 composer 实际提供的工具（不发消息，记录中
`conversationPostSeen: false` 可验证）。三个 alias 结果一致：

| 工具 | 状态 | composer 中的入口 |
|---|---|---|
| web search | offered | "Search the web" |
| file analysis | offered | "Add files and more" |
| image gen | offered | "Create an image or sticker" |
| deep research | **not_offered** | 三个 tier 均无入口 |

矩阵中的处理是有意不对称的：

- `offered` 记为 **NOT_MEASURED**，注明"入口存在，尚未驱动"。计划要求的是四个
  真实工具结果，按钮存在不是结果，记 PASS 会虚报覆盖率。
- `not_offered` 记为 **BLOCKED**。本 build 不提供的工具无法驱动，记 FAIL 等于
  指控镜像弄坏了一个它根本没提供的功能。

**deep research 缺失是否符合预期，需要产品侧确认**——"本 build 没有"与"坏了"
是两回事，此处只报告不下判断。

盘点探针本身曾四次误报"工具缺失"，均已修复并在文件内逐条注明（侧边栏菜单被
误当作 composer 菜单、Playwright `.click()` 打不开该菜单、菜单无 ARIA role、
`aria-expanded` 早于子节点挂载）。最后一条一度让 pro 和 free 看起来比 plus
工具更少——纯属竞态，不是 tier 权限差异。

## 8. 尚未测量

当前覆盖率 29.7%（78 格未测），报告中逐格显式标出，不做任何推断：

- 三个 offered 工具的真实生命周期（发起 -> 进行中 -> 结果落地）全部未驱动。
- 故障注入：403 / 429 / proxy_fail / stream_break / reload / cancel /
  switch_account 全部未测。
- free 和 pro 的 mobile 格、pro 的 mobile 续聊格等未测。
- 官网/镜像逐账号对比表未做。

## 附：复核方式

```bash
# 全部单元测试（纯函数，无需服务和浏览器）
.venv/bin/python -m pytest tests_e2e/test_m5_verdicts.py tests_e2e/test_m5_harness.py -q

# 从证据重建矩阵（幂等，不跑浏览器、不消耗生成次数）
.venv/bin/python -m scripts.mirror_acceptance.runner ingest

# 零成本盘点某 tier 的工具入口（不发消息）
node scripts/mirror_acceptance/tool_probe.js frontend-proof-plus-2 plus-desktop desktop
```

矩阵永远可以从 `evidence/` 下的记录重建，任何一格的状态背后都必须有文件。
