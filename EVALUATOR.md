# EVALUATOR — SaaS P0 批次（付费 ↔ 权益打通）

日期：2026-09-11
范围：SaaS 层。镜像号池调度（`chatgpt/authorization.py`）归后端 Agent，本轮不改。

## 产品决策（用户 2026-09-11 拍板）

1. **没有 Free 档**。Free 只是测试期产物，正式运营只有 Plus / Pro。
2. **到期即不可用**，不降级。7 天 Plus 过期 → 直接用不了 → 引导续费。
3. **注册不送额度**，必须购买才能用。
4. **solo 保留**。价格是占位，将来会真独享（后端分池落地后），不构成虚假宣传。

## 核心设计

**权益从 orders 实时推导，不回写 `user_auth.tier_id`**（Kimi 评审选定方案 A）。
orders 是唯一真相源，过期是算出来的，不需要 sweeper，不存在双写漂移。

三态语义（`enforce_tier` 的判定）：

| seed 情况 | 含义 | 行为 |
|---|---|---|
| 无 `user_auth` 行 | 运营者 seed / 直传 token | fail-open，不设限（**保持现状不变**） |
| 有 `user_auth` 行 + 有未过期 paid 单 | 正常付费用户 | 按该档执行模型白名单 + 额度 |
| 有 `user_auth` 行 + 无未过期 paid 单 | 未购买 / 已过期 | **402 拒绝**，引导续费 |

> 状态码取 402 Payment Required 而非原计划的 403：403 已被「模型不在白名单」占用，
> 两者混用会让前端无法区分「该续费」和「该换模型」，续费引导也就无从触发。

## 验收判据（falsifiable）

图例：`[x]` 已验证并附证据（见 REVIEW_PACKET.md「P0 批次」）。

### A. 付费 → 权益打通（P0-1）

- [x] A1 用户下单并支付成功后，`resolve_user_tier(seed)` 返回所购档次（`plus`/`pro`），不再是 `free`
- [x] A2 `tier_account_plan_types()` 随之返回对应号组（plus → `['plus']`，pro → `['plus','pro']`）
- [x] A3 推导用 `plans.parse_plan_id()` 取档次，**不用 `normalize_tier_id`**
      （反证：`normalize_tier_id("plus-solo-1m")` 必须仍返回 `"free"`，证明误用会导致付费用户被降级）
- [x] A4 用户持有多个未过期订单时，取**最高档**（pro > plus）

### B. 到期回收（P0-2）

- [x] B1 订单过期后 `enforce_tier(seed)` 抛 402，不再放行
- [x] B2 从未购买的注册用户 `enforce_tier(seed)` 抛 402
- [x] B3 `GET /?token=<seed>` 对无有效套餐的用户**不渲染聊天页**，302 跳 `/store?expired=1`
- [x] B4 运营者 seed（无 user_auth 行）不受影响，仍 fail-open
- [x] B5 非 active 账号（封禁 / 未验证）即便订单有效也无权益

### C. 支付生命周期（P0-4）

- [x] C1 下单先写 `status="pending"`，权益只认 `paid`
- [x] C2 `PAYMENT_PROVIDER` 未配置 / 未知值 → **拒绝下单**（fail-closed），不静默 mock
- [x] C3 订单激活是幂等的：重复激活同一订单不延长有效期
- [x] C4 pending 单可复用：连点下单不产生多张并行订单
- [x] C5 `landing.py POST /api/orders` 的金额由服务端定价，客户端传的 amount 被忽略
- [x] C6 前端无法直接把订单置为 paid，只能经 provider 激活路径

### D. 续费叠加（P1-5，本批附带）

- [x] D1 同档提前续费 → 有效期从**当前到期时间**起算叠加，不是从 now
- [x] D2 跨档购买 → 高档立即生效，低档时钟并行（不做折算）
- [x] D3 `expires_at` 在支付激活时物化落库，推导优先读它

### E. 注册不送额度（用户决策 3）

- [x] E1 注册后 `user_auth` 行建立，但无任何有效权益
- [x] E2 新注册用户 `enforce_tier` 即 402

### F. 不回归

- [x] F1 `pytest tests/` 全绿（**109 passed**；P0 批次落地时 93，Kimi 两轮复审后补至 109）
- [x] F2 `pytest tests_e2e/` 全绿（**119 passed**；P0 批次落地时 107，原有 3 红灯一并修复）
- [x] F3 后端契约不变：`resolve_user_tier` 对非 SaaS seed 仍返回 `None`

> 两套件的 conftest 在 import 时各自 `chdir` 到隔离目录，**必须分开跑**；
> 合并成一条 `pytest tests/ tests_e2e/` 会让后加载的一方找不到 `templates/`。
> 见 `tests_e2e/conftest.py:17-19`。

### G. Kimi 复审闭环（用户定的收工判据）

- [x] G1 改前 Kimi 查问题 + 给方案（`KIMI_BRIEF_P0.md`，session `55fd1e4b`）
- [x] G2 改后 Kimi review，且 Kimi **自行跑测试**验证声明属实（session `2473ec78`）
- [x] G3 Kimi 明确给出「无重大问题」→ 本轮收工。全过程记于 `KIMI_REVIEW.md`
- [x] G4 复审列的 9 条（1×P1 + 3×P2 + 5×P3）全部修复，非只修它建议的两条
- [x] G5 并发结算用例经变异验证（去掉 `_SETTLE_LOCK` 则该用例失败），非空跑

## 跨边界（本轮不做，必须在交付说明中写明）

- **粘性绑定击穿号池降级**：`authorization.py::_resolve_seed_account:83` 中 seed 一旦绑定某号，
  只在该号失效时才重选，`_seed_plan_types` 不会被重新咨询。
  影响：过期用户若已绑 plus 号，号池层面不会降级。
  本轮通过 `enforce_tier` + 入口页拦截在**请求层**阻断，号池层遗留给后端 Agent。
- **号池空时全池回退**：`_pick_healthy_account:75` 在档位池无可用号时回退任意健康号。
- **真实分池（solo 独享）**：需后端支持，本轮 density 仅落库留接口。

## 已知不在本批范围（下批）

XFF 限流绕过、会话不可吊销、seed 不可轮换、合规页面、使用记录接线。

（原列的「e2e 3 红灯」已在本批一并修复：2 条是产品语义变更导致的陈旧断言，
1 条是 `tests_e2e/conftest.py` 未隔离真实 `.env`，见 REVIEW_PACKET.md。）

---

# 批次 A（安全 4 项）验收判据

冻结于实现前；每条都是可证伪的判据，勾选即已由自动化测试锁住。
用例集中在 `tests_e2e/test_security_batch_a.py`（29 例）与 `tests/test_ratelimit.py`（9 例）。

### A1. dev_link 只认显式开关

- [x] A1-1 SMTP 未配置时 `/forgot-password` 的响应体**不含** `/reset-password?token=`
- [x] A1-2 显式 `USER_DEBUG_LINKS=true` 时才给链接（本地开发逃生口仍在）
- [x] A1-3 注册验证信同理

### A2. 改密吊销既有会话

- [x] A2-1 B 设备改密后，A 设备的旧 cookie **立刻**失效（断言必须 `follow_redirects=False`）
- [x] A2-2 操作者本人（B 设备）不被自己踢下线
- [x] A2-3 旧密码填错 → `pw_version` 不变（不给免费登出炸弹）
- [x] A2-4 「忘记密码」重置同样吊销（这条路的起因通常就是号被盗）
- [x] A2-5 升级前签发的老 token（payload 无 `pwv`）按 1 认，上线瞬间不踢所有人；窗口 ≤ 8h
- [x] A2-6 被踢的设备跳到 `/signin?reason=pw_changed` 并看到解释；**没登录过的人看不到**该文案

### A3. /signin 限流（双桶）

- [x] A3-1 账号桶：同账号连错 N 次后被挡，**换 IP 无效**
- [x] A3-2 账号桶**连正确密码一起挡**（否则撞库防护等于取消）
- [x] A3-3 逃生口一：登录成功清桶（本人笔误不累积）
- [x] A3-4 逃生口二：邮箱重置不受该桶约束，且重置成功清桶 → 被锁的人立刻能登
- [x] A3-5 IP 桶：同来源横扫多账号被挡
- [x] A3-6 登录成功不消耗配额
- [x] A3-7 `limit=0` 关闭对应桶
- [x] A3-8 `/api/password` 的 `old` 字段复用账号桶，桶满后**猜对也改不成**
- [x] A3-9 查无此人也跑一次 PBKDF2（关掉账号枚举的时序预言机）
- [x] A3-10 forgot-password 有账号级冷却，且超限**静默回成功页**（回 429 会暴露邮箱是否注册）
- [x] A3-11 冷却窗口不长于 `email_token_ttl`（否则开出「已发送但无可用链接」的静默空窗）

### A4. XFF 只在可信对端时采信

- [x] A4-1 白名单为空（默认）→ 一律用 TCP 对端，谁的 XFF 都不信
- [x] A4-2 仅当对端在白名单内才采信；支持 CIDR
- [x] A4-3 **从右往左**剥可信跳，而非取最左（CF / Nginx 是追加语义，最左段攻击者可写）
- [x] A4-4 多级反代逐跳剥；整条链都可信时退回对端
- [x] A4-5 空 XFF / 规则写错不炸、不返回空串（空串会让所有人共用一个桶）
- [x] A4-6 端到端：伪造 XFF 不能重置注册限流

### B. 限流器自身不是新的攻击面

- [x] B1 读路径（`over_limit` / `remaining`）不建条目（key 含用户提交的 email）
- [x] B2 `_MAX_KEYS` 上限，灌 1000 个 key 后条目数仍有界
- [x] B3 洪水**不能**挤掉受害者已累计的失败计数（淘汰按「命中数最少」而非「最久未命中」）
- [x] B4 `_prune` 跟随「见过的最长窗口」，配长窗口时不被提前回收

### C. 写库失败不得伪装成功

- [x] C1 注册写库失败 → 503 + 不发会话 cookie（否则用户拿着指向不存在账号的票）
- [x] C2 重置密码写库失败 → 500 + 不显示「已完成」

### D. 不回归

- [x] D1 `pytest tests/` 全绿（**124 passed**；批次 A 前 109）
- [x] D2 `pytest tests_e2e/` 全绿（**148 passed**；批次 A 前 119）
- [x] D3 两套件分开跑（conftest 在 import 时各自 chdir）

### E. Kimi 复审闭环（用户定的收工判据）

- [x] E1 改前 Kimi 查问题 + 给方案
- [x] E2 改后 Kimi review（Round 1，session `0d8c64ba`）→ 提出 3 条真问题
- [x] E3 修复后再审（Round 2，session `3eebc81d`）→ 判定「**可以放行的状态**」
- [x] E4 Round 2 新提的 2 处 + 1 条 P3 全部已修
- [x] E5 **9 项变异验证**：逐个把修复改回去，确认对应测试变红。
      其中「重置 upsert 非 strict」首次变异**未被捕获**（当时无对应测试）→ 补测试后再验通过

### F. 高风险项（须在交付说明中显式告知）

- [x] F1 `user_auth.pw_version` 是 **schema 变更**（`CREATE TABLE` + `_ADDED_COLUMNS` 双写，老库自动补列）
- [x] F2 A4 改变了生产环境的 IP 判定行为：**走反代必须配 `USER_TRUSTED_PROXIES`**，
      且多级反代要把**每一级**出口 IP 都写上，否则全员挤同一个桶（表现为集体 429）
- [x] F3 seed 轮换未实现，转为跨界交接单 `handoff.md` 线路 F
