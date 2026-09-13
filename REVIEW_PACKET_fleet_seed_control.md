# REVIEW_PACKET — 账号状态机与 Seed 路由闸门统一

工作树：`work/agent-team/four-lines-implementation-deepseek-20260913-retry/worktrees/fleet_seed_control`
分支：`codex/deepseek-fleet-seed-control-20260913-retry`
基线：`deaa701`（main 干净快照）
范围：`utils/routing.py`、`utils/seed_lifecycle.py`、`docs/FLEET_ECOSYSTEM_SPEC.md` 及对应测试。

## 1. 根因

系统里只有一个账号状态机 `utils.fleet_health.resolve_account_status`（人工停用 > dead（熔断标记或账号行）> 持久化 degraded > 错误列表 > 已被证实的 healthy），但**只有探针在用**：

- `utils/routing._status_label` 自带一套三态映射，只读 `accounts.status` 与前端的 `error_token_list`。`utils.antiban.circuit.mark_dead` 会写 `accounts.status='dead'`（`utils/antiban/circuit.py:321`）以及 `globals.antiban_dead_tokens`，两者都不在旧映射的识别范围内 —— 落进兜底分支后，只要该 token 不在 `error_token_list` 里就返回「正常」。
- 结果：一个已被熔断、`seed_lifecycle` 明确拒绝绑定的账号，在同一行数据里 `antiban_status='dead'`、`status='正常'`（面板渲染为绿色 Healthy）。面板同时把 dead/disabled 账号计入 `accounts_ok`（"可用账号/健康运行"），`accounts_bad` 只统计 `error_token_list` 的大小。
- `utils/seed_lifecycle._candidate_denial` 另有一份等价但不共享的实现（`row[1] != 'healthy' or ...`）。当前行为与状态机一致，但它是第二处真相源：状态机一旦新增限制来源，路由闸门会静默分叉。
- 路由候选池耗尽时 `route_seed` 复用 `account_unknown`（"账号未知"）这一编码，运维无法区分「号池里没有这个档的健康号」与「绑定指向了一个不存在的账号」。

## 2. 改动

| 文件 | 改动 |
|------|------|
| `utils/routing.py` | `_status_label` → `project_account_status`，委托 `resolve_account_status`，返回 `(面板标签, 规范状态, 规范标签)`；账号行新增 `account_status` / `status_label`（附加字段，模板不改）；汇总与告警按规范状态聚合，新增 `accounts_disabled`；代理卡的 ok/bad 计数同样按规范状态 |
| `utils/seed_lifecycle.py` | `_candidate_denial` 改用同一状态机判定；`_transition` 在取写锁**之前**预热 `fleet_health` 导入（`_WRITE_LOCK` 不可重入）；候选池为空时以 `no_healthy_candidate` 拒绝 |
| `docs/FLEET_ECOSYSTEM_SPEC.md` | 记录唯一状态机、面板契约、已验证的失败路径与仍待验收项 |

契约变化（附加，未破坏）：账号行新增两个字段；`accounts_bad` 语义由「错误列表长度」改为「degraded/unhealthy/dead 计数」（不含运营主动停用），新增 `accounts_disabled`；`route_seed` 新增一个拒绝编码。面板模板 `templates/account_proxy_bindings.html` 未改动，`status` 仍是原来的三档词表（停用/异常/正常），受限状态不再被渲染成「正常」。

## 3. 证据（命令与输出）

失败优先（实现前）：

```
$ python -m pytest tests/test_routing_status_view.py
15 failed, 4 passed
```

```
$ python -m pytest tests/test_seed_route_failclosed.py        # 实现前
1 failed, 15 passed        # test_active_peer_consumes_the_only_shared_slot 期望的编码
```

实现后：

```
$ python -m pytest tests/test_seed_route_failclosed.py tests/test_routing_status_view.py tests/test_proxy_route_coupling.py
40 passed in 0.34s

$ python -m pytest tests/           # 全部单元测试
1050 passed in 17.04s

$ python -m pytest tests_e2e/       # 端到端
513 passed, 69 warnings in 280.74s

$ python -m pytest <owned set：seed/routing/fleet_health/pool/store/entitlements/authorization/audit 共 19 个文件>
313 passed in 1.80s
```

新增回归覆盖（`tests/`，全部确定性、无网络、无真实凭据）：

- `test_routing_status_view.py`（19）：五种持久化状态逐一投影；受限状态一律不投影为「正常」；熔断标记压过陈旧的 healthy 行；错误列表使 healthy 行受限；无账号行不投影为健康；`disabled` 保留专属标签；面板与 `antiban_status` 对同一死号不再自相矛盾；汇总/告警/代理卡按规范状态聚合。
- `test_seed_route_failclosed.py`（18）：dead / disabled / degraded / unhealthy 候选一律拒绝且绑定与内存不变；熔断标记与错误列表同样拒绝；强切跳过受限原号；冻结绑定不占容量、active/trial 占容量；跨档候选与跨档路由拒绝；强切无候选以 `no_healthy_candidate` 拒绝；重启后沿用持久化绑定、不复活冻结 Seed、号池全受限仍拒绝；并发强切后库与内存一致；拒绝原因是固定匿名词汇且可原样进入审计 sink（不含 seed/email/token）。
- `test_proxy_route_coupling.py`（3，真实 SQLite + 探针路径，Client 为替身）：探针走账号绑定出口；绑定出口传输失败 → `status=unhealthy` → Seed 路由拒绝；恢复需持续成功满 dwell 窗口后才重新可路由。

## 4. 已验证 / 未验证

已验证（有上述命令输出）：
- 面板与路由闸门读同一个状态机判定。
- 受限账号（dead/disabled/degraded/unhealthy/错误列表/熔断标记）不可路由，且拒绝不改动已有绑定与会话历史。
- 冻结绑定不占共享容量，active/trial 占容量；跨档不回退。
- 并发强切在 SQLite 事务内串行化，库与内存一致。
- 重启（`globals.reload_account_cache`）后沿用持久化绑定，冻结 Seed 不被复活。
- 代理出口故障经探针传导为不可路由；恢复受 dwell 约束。

未验证 / 残余风险：
- **候选排序仍不看逐节点代理健康。** `utils/proxy_health` 只暴露 `record` 与随机化的 `weighted_choice`，没有单节点健康读取接口；该模块属于 antiban 工作线，本次不改。当前代理健康对路由的影响是"经探针传导"，不是候选权重。
- **运营者路径的全量 `seed_map` 写入收敛未做**（`globals.persist_seed_map`），SaaS 路径通过不代表这条路径并发安全。
- **真实账号容量未校准。** 测试里的容量是合成数据；`FLEET_MAX_SHARED_SEEDS_PER_ACCOUNT` 必须在部署侧实测后设置正整数。
- **面板模板未改。** `status` 仍是三档；精确状态在新增的 `account_status` / `status_label` 字段里，模板要展示更多档位需另一处改动。
- **无真实上游验证。** 本文所有结论来自替身客户端与合成凭据，不代表任何真实账号可用。
- 审计：本次只验证了拒绝编码可匿名入账；自动化的 Seed 生命周期流转（冻结/激活）本身未接审计写入（`route_seed` 在分配热路径上，接入会新增每次都写的旁路库写入），需要时另行设计。

## 5. 回滚

三个源文件各自独立可回滚；无 schema 变更、无数据迁移、无新依赖。回滚 `utils/routing.py` 与 `utils/seed_lifecycle.py` 即恢复旧行为（面板重新显示死号为「正常」，即回滚后的已知缺陷）。文档为描述性，不参与运行。数据库无需处理。
