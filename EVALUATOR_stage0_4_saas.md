# EVALUATOR — Stage 0–4 用户侧 SaaS

> 判据先于代码。本文件按完成顺序锁定各子项的可证伪完成标准。
> 对应计划文件 `clever-spinning-bird.md` 线程 2（SaaS 用户侧）。

## Stage 0 — 档位与分池数据模型（DONE）

### C1 — user.tier 可持久化
- [x] `store.user_auth` 表存在（email PK、password_hash、seed、tier_id、status、created_at/updated_at）+ seed 索引。
- [x] `upsert_user_auth` 幂等写；`get_user_auth(email)` / `get_user_auth_by_seed(seed)` 读回。
- [x] `tier_id` 默认 `free`（`default_tier_id()`）。

### C2 — 档位枚举「号组 / 模型 / 额度」
- [x] `list_tiers()` 返回 free/plus/pro 三档，每档含 `account_plan_types`（号组）、`models`（模型白名单）、`quota_limit`（额度上限）。
- [x] `tier_account_plan_types(tier_id)`、`tier_allows_model(tier_id, model)`、`tier_quota(tier_id)` 可独立查询。
- [x] 未知档位 / 空模型 fail-open（不误杀）。

### C3 — user.tier 与 account.plan_type 严格分列
- [x] 档位映射用 `tiers.json`（`_DEFAULT_TIERS` 兜底），与 `models_by_plan.json`（管 account.plan_type）并列、不混用。
- [x] 无 `user_auth` 行的 seed → `resolve_user_tier` 返回 `None`（fail-open，运营者 seed 不设限）。

## Stage 1 — 用户注册/登录 + seed 绑定（DONE）

### C4 — 注册即拿 seed
- [x] `POST /register` 校验邮箱/密码（≥8）/唯一；成功生成 32-hex seed。
- [x] seed 落 `store.user_auth` + `globals.seed_map`（persist）。
- [x] 默认档位 `free`，`require_email_verification=False` → `status=active`。

### C5 — 登录/退出 + 会话隔离
- [x] `POST /signin` 校验 PBKDF2 哈希；错密码 401 不泄露存在性。
- [x] 会话用 stdlib HMAC 签名 token（无 itsdangerous 依赖），8h 过期。
- [x] `POST /signout` 清会话/CSRF cookie。

### C6 — 无运营者介入进聊天
- [x] 注册/登录后 303 → `/?token=<seed>`，seed 首请求由 `_resolve_seed_account` 分配号。

## Stage 2 — 档位与额度执行（DONE）

### C7 — 模型门禁
- [x] `enforce_tier(seed, model)`：免费档请求 `o3` → 403；白名单内放行。
- [x] 接入 `reverseProxy.py` + `f_conversation_gateway.py` 两处 conversation 路径。

### C8 — 额度执行
- [x] `user_usage_total(seed, tier_id)` = DB 计数 + 内存 pending 缓冲（准实时）。
- [x] 免费档满 50/日 → 429。

### C9 — 半专属分池
- [x] `_seed_plan_types(seed)` 把 user.tier 解析为 account.plan_types 列表。
- [x] `_pick_healthy_account(plan_types=...)` 从匹配号组内挑号；换档后换号组。
- [x] failover 时按 `_seed_plan_types` 在专属号组内切换。

### C10 — fail-open
- [x] 运营者 seed（无 user_auth 行）走旧主链路，不设档位/额度限制。

## Stage 3 — 引流落地页 + 支付占位（DONE）

### C11 — 落地页
- [x] `GET /landing` 渲染档位对比 + 免费注册入口，审美向 OpenAI/Anthropic。
- [x] `GET /api/tiers` 返回档位 JSON。

### C12 — 支付占位
- [x] `orders` 表 + `POST /api/orders`（需登录）落库 `pending`，无真实扣款。
- [x] `get_order` / `update_order_status` DAO 就绪，微信/支付宝回调后补。

## Stage 4 — 验收收口（DONE）

### C13 — 端到端可复现
- [x] `tests_e2e/test_user_saas.py` 13 例覆盖注册→登录→进聊天→模型门禁→额度→分池→换档→落地页→下单。
- [x] `pytest tests/` = 48 passed（无回归）。
- [x] `pytest tests_e2e/` = 76 passed（63 + 新增 13）。

## 已知边界（如实标注）

- 邮箱验证占位：`require_email_verification=False`，SMTP 资源后补（计划「资源缺口先占位」）。
- 支付模块只占位：`orders` 表 + pending 落库，无真实微信/支付宝。
- 定价金额未定（计划「运行后再定」），订单 `amount` 为字符串透传。
- `user.tier → 号组` 的最终并发等级（独享/少数共享/多数共享）仍由 Stage 5 的 `concurrency._resolve_limit` 按 account.plan_type 分层，user.tier 未单独加并发档位（规模化后再细化）。
