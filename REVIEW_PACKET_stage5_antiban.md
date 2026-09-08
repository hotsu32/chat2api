# REVIEW_PACKET — Stage 5 antiban 针对性升级

> 证据优先，非描述。判据见 `EVALUATOR_stage5_antiban.md`。

## 测试证据（实际命令输出，含子项①+④）

```bash
$ .venv/bin/pytest tests_e2e/test_antiban.py
33 passed in 0.29s        # B1–B5 (17) + B6 并发 (5) + B7 降智 (3) + B8 分池 (3) + B9 信誉 (5)

$ .venv/bin/pytest tests/
48 passed in 1.29s        # 无回归

$ .venv/bin/pytest tests_e2e/
63 passed in 15.95s       # 47 + 新增 16
```

## 子项 ① 每号并发上限 — 变更清单

| 文件 | 变更 | 状态 |
|---|---|---|
| `utils/antiban/concurrency.py` | 新增：每号 `asyncio.Semaphore` 并发槽位 + `_resolve_limit` 分层 | 新增 |
| `utils/configs.py` | `account_max_concurrency`/`free_account_max_concurrency`/`account_concurrency_wait_seconds` | 修改 |
| `utils/antiban/guard.py` | `AntibanContext.concurrency_acquired` + acquire 占位 + `release_context` | 修改 |
| `utils/antiban/__init__.py` | 导出 `release_context` | 修改 |
| `chatgpt/ChatService.py` | `set_dynamic_data` 503 检查 + `close_client` 释放槽位 | 修改 |
| `tests_e2e/test_antiban.py` | 新增 B6（5 例） | 修改 |

## 子项 ④ 降智联动 — 变更清单

| 文件 | 变更 | 状态 |
|---|---|---|
| `utils/antiban/account_risk.py` | Step B：`_escalate`（命中→冷却/熔断）+ `sniff` 联动调用 | 修改 |
| `utils/configs.py` | `account_degraded_link_enabled`/`account_degraded_cooldown`/`account_degraded_mark_dead_threshold` | 修改 |
| `tests_e2e/test_antiban.py` | 新增 B7（3 例） | 修改 |

## 子项 ⑥ 半专属分池 — 变更清单

| 文件 | 变更 | 状态 |
|---|---|---|
| `utils/antiban/bucket.py` | 新增 `_account_plan_type`；`_pick_least_loaded_healthy(plan_type)` 按档过滤；`assign_account` 解析档位 + 空桶定型；`_sync_from_routing` 反推 legacy 桶档位 | 修改 |
| `tests_e2e/test_antiban.py` | 新增 B8（3 例）：分池不串桶 / 跳过错档空桶 / 无匹配档拒绝 | 修改 |

## 子项 ⑤ IP 信誉 — 变更清单

| 文件 | 变更 | 状态 |
|---|---|---|
| `utils/antiban/iprep.py` | 新增：IPQS ip lookup（欺诈分/数据中心/代理/ASN）+ 缓存 + `is_blocked` fail-open | 新增 |
| `utils/globals.py` | `ANTIBAN_IPREP_FILE` + `antiban_iprep_cache` 内存/落盘 | 修改 |
| `utils/configs.py` | `ipqs_api_key`/`ipqs_fraud_threshold`/`ipqs_block_datacenter`/`ipqs_block_proxy`/`ipqs_timeout_seconds`/`ip_rep_cache_ttl_days` | 修改 |
| `utils/antiban/bucket.py` | 新增 `_ip_blocked`（惰性导入 iprep）；`_pick_least_loaded_healthy` 跳过判黑桶 | 修改 |
| `tests_e2e/conftest.py` | 每测试重置 `antiban_iprep_cache` | 修改 |
| `tests_e2e/test_antiban.py` | 新增 B9（5 例） | 修改 |

## 关键机制核实（代码追踪结论，非断言）

**分层限流 plan_type 能命中**：`accounts.token` 按原始形态存储（`persist_token_list` → `upsert_account(t)`），与 `globals.token_list` 一致；`sync_account_plan` 启动全量 + `verify_token` 交换惰性写入 `plan_type`。seed 路径 `_resolve_seed_account` 返回的正是 `accounts.token` 值 → `store.get_account(self.req_token)` 命中。

**释放点必达**：`close_client` 在流式/非流式均经 `BackgroundTask` 或 `except` 路径调用，acquire/release 平衡。

**降智联动惰性导入**：`_escalate` 内 `from utils.antiban import circuit, cooldown` 避免与 `guard` 的循环依赖（`circuit` 自身 `from utils.antiban import bucket/cooldown`）。

## 已知边界（如实标注）

- 「503 → 切到另一个号」failover 布线未做（后续子项）。
- 并发上限进程内状态（多 worker 为每 worker 上限）。
- 直传 access token 且池内以 sess-/rt_ 存储时 `get_account` miss → 回落 paid 默认（欠租方向）。
- 降智 escalation 计数为生命周期累计（非滑动窗口）。
- Step B 默认关闭（`account_degraded_link_enabled=False`），校准 WARNING_PATTERNS 后再开，避免误杀。
- **半专属分池的档位来源是 `account.plan_type`（号本身的档），不是 `user.tier`（用户订阅档）**：本子项只保证 free/plus 号不落同一 IP 桶；`user.tier → 号组` 映射仍属 Stage 2。
- 分池拒绝跨档时返回 `None`，与既有「桶满拒绝」一致，由上游走默认（不主动 failover）。
- `_account_plan_type` 惰性查 store；账号不在 store 时回落 `plan_type=None` → 不设档位过滤（向后兼容，不误拒分配）。
- **IP 信誉依赖外部 IPQS API key**：当前未配置 → fail-open 不过滤；占位已就绪，接 `IPQS_API_KEY` 即生效（`ipqs_enabled` 由 key 存在与否推导）。
- IPQS 首查在分配路径同步 HTTP（超时 3s），按 proxy host 缓存后走本地；`bulk_assign` 启动时为每 proxy 一次（有 key 时）。
- 数据中心默认不判黑（`ipqs_block_datacenter=False`），避免误伤数据中心代理存量部署；`fraud_score >= 阈值` 才是默认判黑条件。
- ASN / ISP 仅落缓存供后续后台展示，本子项未接管理后台 UI。

## 真号冒烟（Stage 5 收口，本地无网络依赖）

用 3 个真实账号（1 free + 2 plus）跑 `smoke_real_accounts.py`（plan_type 全读自 SQLite 真相源，
落盘副作用全部 stub 为 no-op，可重复、不污染库/文件）。真实 OpenAI 往返探活因代理
`127.0.0.1:7899` 死节点被阻塞（见「未验证」）。

```bash
$ .venv/bin/python smoke_real_accounts.py
SUMMARY: total=18 pass=18 fail=0
# ① 导入落库 1 free + 2 plus  ② free/plus 分池不串桶 + 桶定型
# ③ 模拟 plus 封禁 failover（60 采样命中死号 0 次）  ④ 并发分层 free=10/plus=5  ⑤ 指纹稳定不漂移
```

**冒烟揪出一个真 bug（已修）**：`_pick_healthy_account` 只按 `accounts.status="healthy"` 过滤，
而 `mark_dead` 只写 `antiban_dead_tokens`（JSON）不改 `accounts.status`，导致封号后 failover
会把死号重选回来（修前 60 采样命中死号 31 次，非「无感」）。

修复：`chatgpt/authorization.py::_pick_healthy_account` 候选统一过 `_account_is_usable`
（剔除熔断 dead / error / disabled），三处分支（plan_types / tier / 全局回退）全部覆盖。
回归测试 `tests_e2e/test_user_saas.py::test_failover_skips_marked_dead_account` 锁定该行为。

## 验证状态

- [x] Verified：B6 5 例 + B7 3 例 + B8 3 例 + B9 5 例全绿 + 48 单测 + 63 e2e 无回归
- [x] Verified：真号冒烟 18/18（导入落库 + plan_type 正确 + free/plus 不串池 + 模拟封禁 failover + 并发分层 + 指纹稳定）
- [ ] Unverified：真实 OpenAI 往返探活（代理 `127.0.0.1:7899` 节点死，SSL_ERROR_SYSCALL；需用户修复 VPN 后重跑）
- [ ] Needs User：并发默认值（5/10）、降智阈值（3）/冷却（1800s）、IPQS 阈值（80）是否合意
