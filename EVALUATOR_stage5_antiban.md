# EVALUATOR — Stage 5 antiban 针对性升级

> 判据先于代码。本文件按完成顺序锁定各子项的可证伪完成标准。

## 子项 ① 每号并发上限（DONE）

### C1 — 并发槽位机制
- [x] `concurrency.acquire(token)` 槽位未满时立即返回 `True`。
- [x] 槽位满时等到超时（`account_concurrency_wait_seconds`）仍无槽位 → `False`。
- [x] `release(token)` 释放后可再次成功。
- [x] `release` 幂等：未占用/已释放不再多放、不报错。

### C2 — 分层限流（free vs paid）
- [x] `_resolve_limit` 对 `persona=="chatgpt-freeaccount"` 或 `plan_type=="free"` 返回 `free_account_max_concurrency`。
- [x] 其余返回 `account_max_concurrency`。

### C3 — 请求流集成
- [x] `acquire_context` 占槽位并置 `concurrency_acquired=True`；超限超时置 `False`（不抛）。
- [x] `set_dynamic_data` 在 `concurrency_acquired=False` 时抛 503。
- [x] `close_client` 经 `release_context` 释放；未占用时不释放。

## 子项 ④ 降智联动（DONE）

### C4 — 命中联动冷却/熔断
- [x] `account_degraded_link_enabled=False` 时 `sniff` 只记录、不联动（向后兼容）。
- [x] 开启后，累计命中 < `account_degraded_mark_dead_threshold` → `extend_cooldown`（软退避），`is_token_dead` 仍 False。
- [x] 命中 >= 阈值 → `mark_dead`（硬熔断），`is_token_dead` True。

## 子项 ⑥ 半专属分池（DONE）

### C5 — free/plus 不串池（桶按 plan_type 定型）
- [x] `assign_account` 解析账号 `plan_type`，选桶时跳过已定型为其他档的桶。
- [x] 空桶（未定型）首次被某档账号占用时定型为那档（`bucket["plan_type"]`）。
- [x] 只有其他档的桶时，绝不跨档分配 → 返回 `None`（拒绝）。
- [x] `_sync_from_routing` 对 legacy 未定型桶按首个账号反推档位，防止历史混档桶继续混入新号。

## 子项 ⑤ IP 信誉（IPQS 欺诈分 + ASN，DONE）

### C6 — IP 前置过滤（fail-open + 判黑）
- [x] `iprep.get_reputation` 无 `IPQS_API_KEY` 时不发任何网络请求（fail-open，返回 None）。
- [x] `iprep.is_blocked` 对 `fraud_score >= ipqs_fraud_threshold` 判黑。
- [x] 数据中心 / 代理 IP 判黑受 `ipqs_block_datacenter` / `ipqs_block_proxy` 开关控制（默认关）。
- [x] `bucket._ip_blocked` 前置过滤：判黑桶被 `_pick_least_loaded_healthy` 跳过，不参与分配。

## 主动放下 / 暂缓（非确有必要，经讨论）

- ② 指纹粒度（每号 N 套）：计划里「最锋利判据」的正面对应，但①并发上限已先框住并发、且为跨 9 处访问器的高风险重构，规模化后才咬人 → 暂缓，规模化实测需要时再评估。
- ③ IP 松绑（自洽+低频切换）：「弱信号 = IP」，自洽属过度设计；「松绑」是可用性便利而非风控必需 → 放下。

## 已知边界（如实标注）

- 「503 → 切到另一个号」的 failover 布线是后续子项；子项① 只发信号 + 保护账号不过载。
- 并发上限是进程内状态（与 `cooldown` 一致），多 worker 时为「每 worker 上限」。
- 分层限流 plan_type 主路径（seed → 号池）命中；直传 access token 且池内以 sess-/rt_ 存储时 miss，安全回落 paid 默认。
- 降智 escalation 计数为「生命周期累计」（非滑动窗口），校准后可改为窗口计数。
