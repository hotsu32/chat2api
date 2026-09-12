# KIMI_REVIEW — Chat-Share SaaS 前端 8 页

复核方式：`~/.kimi-code/bin/kimi -p '<复核 brief>'`（非交互模式），独立读文件复核，session `b5887e71`。
复核日期：2026-09-11

## 采纳并已修复

| 来源 | 问题 | 修复 |
|---|---|---|
| P0.3 | 未登录访问 SaaS 页面返回 JSON 401，而非跳登录页（断链体验） | `saas.py` 新增 `_page_email`，页面路由未登录 → 303 `/signin` |
| P0.1 | `POST /api/checkout` 无幂等，双击/回退重提会重复建单 | 新增 `_recent_paid_order`：同 email+plan 10 秒内已有 paid 单则复用 |
| P0.2 | 支付方式 `method` 服务端完全不读 | `api_checkout` 读取并落注释说明（占位），前端加「演示环境」明示 |
| P1.6 | `_login_redirect` 只查 `paid` 不看有效期，过期用户仍进 Dashboard | 改判「存在未过期 paid 订单」，过期用户回 `/store` 复购 |
| P1.7 | `/signout` 无 CSRF 校验 | `base.html` 退出表单加 `csrf_token`，`user.py` 校验 |
| P1.9 | `days_left`/`healthy` 边界（到期当刻仍 healthy） | `healthy = expires > now`，days_left 同步归零 |
| P1.10 | 钱包页过期套餐「进入」按钮仍可点 | 过期套餐 CTA 换成「续费 →」链到 `/checkout?plan=<same>` |
| P2.11 | `store.html` 死代码 `if (typeof PRICES...)` | 删除 |
| 产品1 | 导航缺「使用记录」入口，层级不一致 | `base.html` 导航加「使用记录」 |
| 产品2 | 支付方式是「假选择」，伤信任 | checkout 加「演示环境，点击即视为支付成功」明示 |
| 产品3 | 注册密码不一致用 `alert()`，与 Swiss 风格断裂 | 改用站内 `.error` 组件 |
| 产品6 | Dashboard 空态价值低（新用户已被 redirect 挡住） | 未购买访问 Dashboard → 303 `/store` |
| 产品7 | 兑换码/邀请码占位占钱包黄金位 | 归入「即将上线」弱化处理 |
| 产品9 | 「超市」中英混排 | 导航统一中文命名 |

## 未采纳（记录理由）

| 来源 | 问题 | 理由 |
|---|---|---|
| P1.4/1.5 | CSRF token 不轮换、生命周期与 session 错位 | 占位阶段可接受；session 机制整体属底座，本轮不深改 |
| P1.8 | `tojson` 依赖 Jinja autoescape 隐式默认 | FastAPI Jinja2Templates 对 `.html` 默认 autoescape=True，当前安全；显式声明留待加固 |
| P2.13 | `used=0` 占位字段 | 已改为不渲染该列 |
| P2.14 | 进程内 session secret（重启失效） | 已知，dev 可接受；生产需配 `USER_SESSION_SECRET` |
| P2.15 | `invite_code` 用 email sha256 前 6 位可预测 | 占位用；返利上线前需换服务端随机码 |
| 产品8 | 移动端 nav 无折行 | 本轮先桌面；移动适配留下一轮 |

## 结论

Kimi 的 P0.3（未登录不跳登录）与 P1.6（过期用户分流）是真问题，已修。其余 P1/P2 多为占位阶段的已知取舍，已记录。产品侧「收敛占位暴露面」采纳，钱包/结算/用量三处的空转 UI 已降级为明示占位。

---

# 第二轮：P0 批次（支付-权益打通）

复核方式：`~/.kimi-code/bin/kimi -p '<brief>'`，两轮独立复核，Kimi 自行读代码并**实际跑了测试套件**。
复核日期：2026-09-11 / 2026-09-12
Session：round-1 `session_55fd1e4b`（改前查问题）、round-2 `session_2473ec78`（改后 review）
Brief：`KIMI_BRIEF_P0.md`、`/tmp/kimi_review_p0_round2.md`

本轮遵循用户定的流程：**改前让 Kimi 查问题 + 给方案，改后让 Kimi review**；Kimi 说「无重大问题」才算收工。

## Round 1（改后首审）：无重大问题 + 9 条改进

Kimi 独立跑出 `tests/` 98 passed、`tests_e2e/` 108 passed，判定无 P0。列出 1×P1 + 3×P2 + 5×P3，
建议「至少改 P1-1 和 P2-4」。实际**全部 9 条都改了**（每条都是分钟级成本）。

| # | 问题 | 修复 |
|---|---|---|
| P1-1 | mock 渠道回调零鉴权：任何注册用户「建单 + 自投回调」两步白拿套餐 | `saas.py::api_payment_callback` 在 `verify` 之前对 `is_mock` 直接 403；`landing.py::create_order` 未配渠道时 503，与 `/api/checkout` 口径对齐 |
| P2-2 | 权益层异常全链路 fail-open（且只打 debug 日志）：一次锁库 = 给全体过期用户开闸 | 新增 `store.StoreError` + `strict=True` 读模式，把「查询失败」与「查无此行」分开；`enforce_tier` 遇 StoreError → **503**（不放行也不 402） |
| P2-3 | 跨订单同档激活「读到期时间→加时长→写回」不原子，并发两笔会吞掉一笔 | `_SETTLE_LOCK` 串行化 `_settle_order` 全程 |
| P2-4 | 文案说「不限次数」，代码实为 Plus 500/日、Pro 2000/日 —— 直接的客诉源 | store/landing 改为明示每日上限 |
| P3-5 | `list_orders("")` 返回全表 = 把全站权益算到一个人头上 | 只有 `None` 才是「全部」，空串 = 一个 email 为空的用户 |
| P3-6 | 改价后复用旧 pending 单 = 按旧价付款拿新套餐 | 金额快照不匹配则不复用 |
| P3-7 | 回调对真实激活失败也返回 `ok:true`，付款静默沉没 | 失败且非已 paid → 500 让网关重投；重投（已 paid）仍 200 幂等 |
| P3-8 | legacy 裸档位订单权益层认、展示层不认（有权益但看不到卡片，也没有续费入口） | `_legacy_card` 渲染为「旧版套餐」，续费指向对应月卡 |
| P3-9 | checkout GET 不感知渠道状态，用户选完支付方式点确认才吃 503 | 传 `payable`，未开通时不渲染支付表单 |

## Round 2（复审）：**无重大问题**

Kimi 逐条读代码核验，结论「九条修复全部真实落地、修在正确的层，无『假修』」，并自行跑出
`tests/` 108 passed、`tests_e2e/` 117 passed（与声明一致）。三个专项问题的回答：

- **死锁/内存**：锁序单向无环。（Kimi 复审的 brief 描述的是 per-email 锁；实际落地前我已自查出
  「锁字典只增不减」的慢泄漏，改成了单把全局 `_SETTLE_LOCK` —— 结算是低频极短操作，全局串行的
  代价可忽略。Kimi 判定该泄漏「本轮不需要解决」，而实际上已不存在。）
- **StoreError 漏接**：授权链上无漏网，`enforce_tier` 两个调用点（`reverseProxy:483`、
  `f_conversation_gateway:290`）都能干净上抛 503。
- **`_entitled` 仍 fail-open 是否成立**：成立，且理由比注释更硬 —— `enforce_tier` 对**所有**
  backend-api 路径执行，故 DB 故障时放行的只是「一张不能干活的静态壳」，同时避免把运营者锁在门外。

### 复审附带的 P3，已就地修掉 3 条

| 来源 | 问题 | 修复 |
|---|---|---|
| 复审 P3-2 | 套餐下架后旧 pending 单永远激活不了，500 会被网关**无限重投** | `_unfulfillable()`：置 `failed` 终态 + `logger.error` 告警，回 `{"ok": false}` 让重投停下；新增用例 `test_delisted_plan_order_stops_retrying_instead_of_looping` |
| 复审 P3-1 | `user_usage_total` 吞一切异常返回 0，额度闸在 usage 子系统故障时静默失效 | 保留 fail-open（用户确实已持有效套餐，误伤更糟）但升为 `logger.warning`，不再查无实据 |
| 复审 P3-3 | `subscription_expiry` 无调用方且用非 strict 读，后来者易误用 | 对齐 `strict=True` 并写明原因 |

第 4 条（mock `auto_settle` 忽略 `_settle_order` 返回值）仅本地联调路径，按 Kimi 判断保留。

## 交接给号池 Agent（界外，不在本轮范围）

`chatgpt/authorization.py::_seed_plan_types` 的裸 `except Exception` 会把 `StoreError` 吞成
fail-open。与改动前行为等价（**无回归**），但意味着 DB 故障时「请求闸 503、选号 fail-open」姿态不一致。
同文件 `:44` 的 `if not tier_id: return None` 用真值判断混淆了 `""`（无权益）与 `None`（运营者），
过期用户仍会在 `/api/auth/session` 被绑到号池账号。

## 本轮测试

- `tests/` **109 passed**（P0 批次前 98）
- `tests_e2e/` **119 passed**（P0 批次前 108）
- 两套件因 conftest 在 import 时各自 `chdir`，**必须分开跑**（`tests_e2e/conftest.py:17-19` 已写明）。
- 并发结算用例经**变异验证**：把 `with _SETTLE_LOCK:` 换成 `if True:` 后该用例失败，证明它不是空跑。

## 结论

Kimi 明确给出「**无重大问题**」——本轮收工判据满足。

---

# 第三轮：批次 A（安全 4 项）

改前 brief `KIMI_BRIEF_A_ROUND2.md`；Round 1 session `0d8c64ba`，Round 2 session `3eebc81d`。
日期：2026-09-12。审阅期间工作区冻结（Round 1 中途改文件导致 Kimi 读到半截文件，已引以为戒）。

## 本批次做了什么

| 项 | 问题 | 修复 |
|---|---|---|
| A1 | `dev_link` 在「SMTP 未配置」时把重置链接印到页面上 = 无条件账号接管 | 只认显式 `USER_DEBUG_LINKS`，不再拿 SMTP 状态当开关 |
| A2 | session payload 只有 `{email, exp}`，改密码救不回被盗账号 | 新增 `user_auth.pw_version`（**schema 变更**），token 带 `pwv`，改密 / 重置即 bump |
| A3 | `/signin` 完全无限流 | IP 桶（挡横扫）+ 账号桶（挡撞库，换 IP 无效），仅失败计数 |
| A4 | `_client_ip` 无条件采信 XFF = 一行 header 清零所有限流 | 只在 TCP 对端属于 `USER_TRUSTED_PROXIES` 时采信，且**从右往左**逐跳剥 |

超出原定 4 项、评审过程中补的：`/api/password` 的 old 字段复用账号桶（它是个密码预言机）、
ratelimit 内存有界化、注册 / 重置写库失败显式报错、查无此人也跑 PBKDF2（关时序预言机）、
forgot-password 账号级冷却、被踢设备的解释文案。

## Round 1（改后首审）：3 条真问题

| 级别 | 问题 | 处理 |
|---|---|---|
| P1 | **实测证伪了我自己的测试**：`test_flood_does_not_erase_an_active_bucket` 只灌了 45 个 key < `_MAX_KEYS=50`，淘汰路径根本没跑，断言恒真；且当时按「最久未命中」淘汰，受害者的桶**会**被挤掉 | 独立复现确认 → 淘汰键改为「命中数最少」+ 测试改成真灌 200 个 → 变异验证 |
| P1 | 账号桶连正确密码也挡 = 可锁死任意用户（判定引入新 DoS），建议「密码对就放行」 | **未采纳**，见下 |
| P1 | XFF 取最左段，在 CF / Nginx 的**追加**语义下仍可伪造 | 已改为从右往左剥 |
| P2 | 注册写库静默失败、ratelimit 无内存上限、`_max_window` 写死 3600 | 全部已修 |

### 关于「密码对就放行」——我不采纳，Round 2 Kimi 收回了该建议

「密码对就放行」等于**取消账号桶**：攻击者可无限次提交，撞对的那次照样放行，
桶只是把失败的错误码从 401 换成 429，而撞库防护的全部价值就在于让他到不了那一次。
保留「连正确密码一起挡」，代价用两条逃生口化解：登录成功清桶；「忘记密码」不受该桶
约束且重置成功清桶（攻击者控制不了受害者邮箱，这条路他关不掉）；429 文案明确指路。

Round 2 原话：「**你的核心论证是对的，而且比 Round 1 我的建议更正确……收回我 Round 1 的
建议方向，你现在的『挡 + 双逃生口』是正确结构。**」

## Round 2（复审）：**可以放行的状态**

我提了 5 个「请攻击这里」，逐条结果：

1. **逃生口互锁**（我最不确定的一条）—— 不成立。关键机制：`forgot:email:` 的配额消耗
   **必然伴随真实邮件投递**，攻击者灌满 3 格 = 给受害者送达 3 封有效重置链接，而他读不到。
   **灌得越狠，逃生口开得越大。** 但 Kimi 指出一个真实时序窗：token TTL=1800s 而槽位占 3600s，
   两者之间受害者会拿到「已发送」却没有可用链接。**已修**：冷却窗口改为跟随 `email_token_ttl`
   （`_forgot_email_window()`，下限 5 分钟），槽位释放不晚于最后一封信失效。
2. **淘汰策略可绕过** —— 实测确认能：噪声 key 各刷 6 次（>受害者封顶的 5 次）即可挤掉。
   但成本 = 30 万次 `/signin` POST × PBKDF2 ≈ 4-8 CPU 小时 + 约 15,000 IP·小时肉鸡，
   收益仅「对一个账号每 15 分钟多猜 5 次」。**结论：ROI 极差，本轮不防。**
   存档一条反直觉结论：随机淘汰反而更便宜（期望 ~50k 次单 hit），现在的计数式已是更优解。
3. **偷 cookie 者能否用改密清桶** —— 推理成立：他得先过 `verify_password(old)`，
   而知道旧密码的人直接 `/signin` 成功同样清桶，没有授予任何新能力。
4. **老 token 无 `pwv` 的 fallback 能否绕过吊销** —— 独立确认安全：HMAC 先验签后解析，
   伪造需要 `_session_secret`；且「老 token + 已改密」仍会被吊销（fallback 1 ≠ pwv 2）。
5. **还有没有第二处空转测试** —— 抓到一处**注释描述失真**（断言本身有效）：
   `test_forgot_password_has_a_per_email_cooldown` 写「换设备绕开 IP 桶」，
   但 TestClient 对端恒为 `testclient`，且 e2e 的 IP 桶本就关着。**已修**：显式
   `monkeypatch` 关 IP 桶把前提写出来，并改掉误导性注释。

### Round 2 新发现（1 条 P3，已修）

`reset_password` 的 `upsert_user_auth` 没加 `strict=True`：bump 成功 + token 已消耗 +
upsert 静默失败 → 页面显示「已完成」但密码没改。不是锁死（旧密码仍可用），但正是
注册那条 P2 消灭的同一类「假成功」。**已修** + 补测试 + 变异验证。

## P3 延后（Kimi 确认可延后）

| 问题 | 备注 |
|---|---|
| `_is_https` 无条件信 `x-forwarded-proto`，与 XFF 的白名单模型不一致 | 危害路径：注入 `http` → secure flag 缺失 → 站点若同时听 http 则有明文泄露面。全站 HTTPS-only 后自动失效 |
| `/verify-email` GET 有副作用 | 真实危害不是「用户被坑」（账号其实已激活），而是「点了链接=真人」这个语义被扫描器替代 |
| `_invite_code = sha256(email)[:6]` 可预测 | **返利上线前必须换随机码**，否则是「输入别人邮箱冒领返利」的接口。已在 `saas.py::_invite_code` 留 TODO 锚点，不只留在评审记录里 |

## 本轮测试

- `tests/` **124 passed**（批次 A 前 109）
- `tests_e2e/` **148 passed**（批次 A 前 119），其中 `test_security_batch_a.py` **29 passed**
- 两套件必须**分开跑**（两个 conftest 在 import 时各自 chdir）
- **9 项变异验证**（把修复逐个改回去，确认对应测试变红）：XFF 取最左 / 注册静默失败 /
  重置不清桶 / 无 dummy hash / 无邮件冷却 / 密码对就放行 / 成功不清桶 /
  重置 upsert 非 strict / 冷却窗口写死 3600 —— 全部被捕获。
  其中「重置 upsert 非 strict」首次变异时**没有**被捕获（当时无对应测试），补测试后再验通过。

## 交接（界外）

seed 轮换转为 `handoff.md` **线路 F**：`user_auth.seed` 终身不变且泄露后无补救，
但轮换需要号池线提供 `rebind_seed` / `drop_seed`（`globals.seed_map` 归号池线），
SaaS 线单独做不成。已写明双方职责与交付标准。

## 结论

Kimi 判定「**可以放行的状态**」。Round 2 指出的 2 处（时序窗、测试注释失真）与 1 条新
P3（reset 非 strict）均已修复并补了变异验证过的测试。本轮收工判据满足。
