# REVIEW_PACKET — antiban 容量保护与准入/反馈闭环

分支：`codex/deepseek-antiban-capacity-20260913-retry`
工作树：`work/agent-team/four-lines-implementation-deepseek-20260913-retry/worktrees/antiban_capacity_coordination`
基线：`deaa701`（clean main 快照）
范围：`utils/antiban/**`、antiban/capacity 测试。**未**触碰账号池持久化、SaaS/支付、research、gateway 生成、凭据与无关文件。

---

## 1. 产品影响（1-2 句）

账号准入层原本有 4 条真实的 fail-open/不可见路径：分不到出口的号照常出网、多 Worker 声明读不出来时按单进程放行、声明了协调层却被静默忽略、proxy/timeout 失败在没有桶的号上完全不进指标；另外死号复活原语不携带任何证据、挑战类拒绝（PoW/Turnstile/Arkose/账号不可用）只能落到 `unclassified` 且没有任何降载动作。本次把这几条都改成「要么可验证地放行，要么明确拒绝」，并让每一类拒绝都能在匿名指标里查到。

## 2. 文件级改动

| 文件 | 改了什么 | 为什么 |
|---|---|---|
| `utils/antiban/guard.py` | 准入顺序：死号(403) → 桶资格 → **严格绑定分不到桶(503 no_healthy_bucket)** → 冷却 → 并发；死号检查提到分配之前；worker 声明解析区分 absent/declared/unusable；新增协调层声明探测与 `UnusableCoordinatorError` 启动拒绝 | 4 条 fail-open 中的 3 条 |
| `utils/antiban/circuit.py` | 新增 PoW/Turnstile/Arkose/account_unavailable 分类与账号退避动作；分类顺序改为「状态码硬证据优先于正文标记」；网络失败即使没有桶也计数；`revive_token` 增加「新鲜探针证据」闸门；桶降级事件进指标 | 分类与反馈闭环、死号复活证据 |
| `utils/antiban/bucket.py` | 新增 `has_buckets()` | 区分「池子是空的」与「池子有桶但接纳不了」 |
| `utils/antiban/cooldown.py` | 新枚举进入 `EXTEND_REASONS` 白名单 | 否则新原因会静默塌进 `other` |
| `utils/antiban/__init__.py` | 导出新异常 + 更新边界契约文档 | 契约可查询 |
| `tests/test_antiban_capacity_gaps.py`（新） | 63 条单元测试 | 每条行为一个可证伪断言 |
| `tests_e2e/test_antiban_capacity_gaps_e2e.py`（新） | 6 条真实 `/v1/chat/completions` 链路测试 | 替身证明不了「产品路径真的走到该判定」 |
| `tests_e2e/test_antiban_runtime_startup.py` | +5 条启动拒绝测试（子进程，loopback 护栏） | 启动期 fail closed 必须真起不来 |
| `tests_e2e/test_antiban_generation_feedback.py` | +1 条「降级出口不再放行本桶账号」 | 回归保护（基线已通过，非新行为） |

## 3. 逐条根因与判据

| # | 根因（基线行为） | 现在的判据 |
|---|---|---|
| A | `acquire_context` 忽略 `assign_account()` 返回的 `None`；池子满/全降级时请求仍带着「没有绑定保证的出口」出网 | 池中存在桶 且 `strict_ip_binding`（默认 True）→ 503 `no_healthy_bucket`；空池或宽松模式仍放行 |
| A' | 死号检查原本在分配之后，且新增加的分桶拒绝会把它降级成 503 | 死号先判 → 403，且不产生任何桶绑定副作用 |
| B | `handle_network_error` 在 `bucket_id` 为空时 `return`，proxy/timeout 失败连计数都没有 | 先按枚举计数；有桶才降级 |
| C | `WEB_CONCURRENCY=0`/`auto`/`4,4` 被 `except: continue` 吞掉 → `single_process_assumed` → 多 Worker 下按每进程上限放行 | 声明状态分 absent/declared/unusable；unusable 与 >1 一样启动期拒绝 |
| D | 协调层声明完全不被读取；`shared_coordinator` 恒为 `None`，进程照常起来 | 声明被脱敏地暴露为 `declared_shared_coordinator`，并在启动期抛 `UnusableCoordinatorError` |
| E | PoW/Turnstile/Arkose/账号不可用 → `unclassified`，既无动作也无指标 | 四个独立枚举 + 账号退避 + 指标键 + `known_error_classes` 表项 |
| F | `revive_token` 裸调用即可复活死号 | 复活要求 store 中「刚写入的 healthy 判定」（±60s 时钟容差，≤300s 新鲜度）；失败计数 `revive.refused_no_evidence` |
| G | 熔断动作本身（桶降级）只在日志里 | 指标新增 `bucket_degraded.<reason>` |

## 4. 证据

### 4.1 失败优先（RED）——在基线 `deaa701` 的临时工作树上跑本次新增测试

```text
$ git worktree add /tmp/af-base HEAD   # 复制新增测试文件后
$ /Users/Zhuanz/chat2api/.venv/bin/python -m pytest tests/test_antiban_capacity_gaps.py --tb=no -q
42 failed, 21 passed in 0.19s

$ /Users/Zhuanz/chat2api/.venv/bin/python -m pytest \
    tests_e2e/test_antiban_capacity_gaps_e2e.py tests_e2e/test_antiban_runtime_startup.py \
    tests_e2e/test_antiban_generation_feedback.py --tb=no -q
10 failed, 37 passed in 29.96s
```

失败点覆盖上表 A–F；通过的 21+37 条是刻意保留的「不得过度拦截」断言（空池放行、宽松模式放行、无证据时降级不发生、无桶时不降级、`unclassified` 仍可计数、antiban 关闭时零动作）——它们在基线与本次都通过，正是没有把闸门关过头的证据。

### 4.2 通过（GREEN）——本工作树，最终代码

```text
$ /Users/Zhuanz/chat2api/.venv/bin/python -m pytest tests/ -p no:cacheprovider
1073 passed in 16.76s

$ /Users/Zhuanz/chat2api/.venv/bin/python -m pytest tests_e2e/ -p no:cacheprovider
525 passed, 69 warnings in 297.26s

# 本次归属范围（分开跑，原因见 4.3）
$ /Users/Zhuanz/chat2api/.venv/bin/python -m pytest tests/test_antiban_capacity_gaps.py \
    tests/test_antiban_admission.py tests/test_antiban_chatservice_admission.py \
    tests/test_antiban_cooldown_limit.py tests/test_antiban_metrics_coordination.py \
    tests/test_antiban_version_check.py tests/test_proxy_health.py tests/test_ratelimit.py
173 passed in 3.48s

$ /Users/Zhuanz/chat2api/.venv/bin/python -m pytest tests_e2e/test_antiban.py \
    tests_e2e/test_antiban_admin_metrics.py tests_e2e/test_antiban_generation_feedback.py \
    tests_e2e/test_antiban_runtime_startup.py tests_e2e/test_antiban_capacity_gaps_e2e.py
74 passed in 38.01s
```

### 4.3 两条已确认的**既存**测试基础设施限制（非本次引入）

1. `pytest tests/ tests_e2e/` 同会话收集会因同名文件冲突中断：`tests/test_chat_upstream_failclosed.py` 与 `tests_e2e/test_chat_upstream_failclosed.py` 同名。基线 `deaa701` 同样报此错。
2. 单元与 e2e 套件在同一次 pytest 调用里互相干扰（e2e 大量失败）。基线同样复现：

```text
# 基线工作树上
$ pytest tests/test_ratelimit.py tests_e2e/test_antiban_generation_feedback.py --tb=no -q
FAILED tests_e2e/test_antiban_generation_feedback.py::test_delivered_completion_charges_the_trial_once
FAILED ... test_disconnect_after_the_answer_releases_the_trial
FAILED ... test_send_failure_releases_the_trial
FAILED ... test_second_turn_on_a_warmed_up_account_remains_available
```

因此两套必须分开跑（本仓库既有 review packet 也是分开跑的）。本次新增的 e2e 文件已特意取了不冲突的文件名，未让限制 1 变差。

## 5. 独立评审（Kimi Code）

两轮，均为只读评审（Kimi 未改任何文件）。逐字结论见下：

- **第 1 轮**提出 5 点，其中 3 点成立并已修：
  - `no_healthy_bucket` 排在死号检查之前 → 死号被降级成 503（**已修**，见 A'）
  - 分类循环 `break` 会跳过后续更明确的账号状态标记（**已修**，改为逐标记跳过）
  - CF 挑战页也可能以 503 下发，只认 403 会降级成 `upstream_5xx`（**已修**，机器令牌标记与状态码解耦）
  - 另 2 点判定为「设计取舍，接受」：多 Worker 声明取各变量最大值（无法确知服务器读哪个变量 → 取最坏情况）；声明了协调层即拒绝启动（命名空间是 `ANTIBAN_*`，不会被无关服务误设；错误信息给出「去掉哪个变量」）
- **第 2 轮**验证修复后提出 2 点，均成立并已修：
  - 标记循环会先于 429/401 状态规则返回（401+invalid_grant 的 refresh 恢复路径被跳过）→ **已修**：状态码硬证据排在正文标记之前
  - 死号仍会走一遍分配（产生绑定副作用）→ **已修**：死号检查提到分配之前

Kimi 第 2 轮的残余风险排序（第 3 项：平台注入 worker 变量导致启动拒绝且无覆盖开关）——按本层既有教条接受：宁可起不来，也不假装受保护；异常信息指明「unset 或固定为 1」。第 2 项（`account_deactivated` 任意状态码即判死）为**基线既有行为**，本次未改变，也未借机收紧（避免在没有真实证据的情况下改动既有分类契约），列为残余风险 R2。

## 6. 残余风险与边界

| # | 风险 | 现状 / 处置 |
|---|---|---|
| R1 | 多 Worker 且平台**未注入**任何 worker 变量：本层无法证明是单进程，只标注 `single_process_assumed`，不拒绝启动 | 与基线契约一致（"假定"不是"保证"），`capacity_is_global` 恒为 False，可查询 |
| R2 | `account_deactivated` 在任意状态码都判死（持久化），理论上正文回显该字符串会误判 | 基线既有行为；机器令牌不是自然语言，gateway 路径的正文嗅探白名单本就排除自然语言词 |
| R3 | 协调层声明即拒绝启动，没有运行期覆盖开关 | 有意为之：覆盖开关等于 fail-open 后门；恢复方式是改正环境变量 |
| R4 | 挑战页若只出现自然语言描述（如 `just a moment`）且非 403，仍归 `unclassified` | 词汇表边界，不猜类别；`unclassified:403` 仍可计数可查 |
| R5 | 本次未做任何真实账号验证（全部为替身/mock） | 见第 7 节 |

## 7. Verified / Unverified

- **Verified**：上述所有 pytest 命令的实际输出；基线 RED；在本工作树可复现。
- **Unverified**：真实账号 / 真实代理下的行为（无凭据，且 brief 明确禁止从 mock 声称真账号成功）；线上是否真的会出现 `WEB_CONCURRENCY=0`、真实 Cloudflare 挑战状态码分布。
- **Cannot Verify**：多 Worker 真实部署下的容量稀释（需要共享协调层，本层未实现）。
- **Needs User**：是否接受 R3（声明协调层即拒绝启动）与 R1（未声明 worker 数时只标注不拒绝）这两个部署策略。

## 8. 回滚

单 commit，纯 `utils/antiban/**` + 测试；`git revert <sha>` 即可完全回滚，无数据迁移、无持久化格式变更（`antiban_dead.json` / `antiban_bucket.json` 结构未变）。
