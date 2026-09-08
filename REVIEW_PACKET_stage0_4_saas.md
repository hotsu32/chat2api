# REVIEW_PACKET — Stage 0–4 用户侧 SaaS

> 证据优先，非描述。判据见 `EVALUATOR_stage0_4_saas.md`。

## 测试证据（实际命令输出）

```bash
$ .venv/bin/python -m pytest tests_e2e/test_user_saas.py
13 passed in 1.57s

$ .venv/bin/python -m pytest tests/
48 passed in 1.34s        # 无回归

$ .venv/bin/python -m pytest tests_e2e/
76 passed in 21.16s       # 63（既有）+ 13（新增用户侧）
```

## 变更清单

| 文件 | 变更 | 状态 |
|---|---|---|
| `utils/tiers.py` | 新增：档位目录 + 解析 + 执行（config-driven，`_DEFAULT_TIERS` 兜底） | 新增 |
| `utils/store.py` | 新增 `user_auth` / `orders` 表 + DAO（`get_user_auth`/`upsert_user_auth`/`create_order`/`get_order`/`update_order_status`/`list_orders`） | 修改 |
| `utils/usage.py` | 新增 `user_usage_total(seed, tier_id)`（DB + 内存 pending 缓冲） | 修改 |
| `utils/configs.py` | 新增 `user_session_secret`/`user_session_max_age`/`user_session_cookie`/`user_csrf_cookie`/`require_email_verification` | 修改 |
| `chatgpt/authorization.py` | 新增 `_seed_plan_types`；`_pick_healthy_account(plan_types=...)` 按档位过滤；`_resolve_seed_account`/`switch_seed_account` 传参 | 修改 |
| `gateway/user.py` | 新增：注册/登录/退出/账号 + session + CSRF（stdlib HMAC，无 itsdangerous） | 新增 |
| `gateway/landing.py` | 新增：`/landing` + `/api/tiers` + `/api/orders`（pending 占位） | 新增 |
| `gateway/reverseProxy.py` | 注入 `enforce_tier(seed, model)`（conversation 路径） | 修改 |
| `gateway/f_conversation_gateway.py` | 注入 `enforce_tier(token, model)`（f/conversation 路径） | 修改 |
| `app.py` | import 顺序修正：`gateway.user`/`gateway.landing` 在 `gateway.backend`（catch-all）之前注册 | 修改 |
| `templates/register.html` `signin.html` `account.html` `landing.html` | 新增：OpenAI/Anthropic 风干净留白页 | 新增 |
| `tests_e2e/test_user_saas.py` | 新增 13 例（Stage 0–4 全覆盖） | 新增 |

## 关键机制核实（代码追踪结论，非断言）

**档位分列不混用**：`account.plan_type`（号本身的档，JWT `chatgpt_plan_type`）与 `user.tier`（用户订阅档，`store.user_auth.tier_id`）在两条独立链路上。`models_by_plan.json` 继续管前者；`tiers.json`（`_DEFAULT_TIERS`）管后者。`_seed_plan_types(seed)` 只读 `user_auth.tier_id` → `tier_account_plan_types`，返回的是「该档可用号组」，交给 `_pick_healthy_account(plan_types=...)` 做池内过滤。

**fail-open 双保险**：`resolve_user_tier` 无 `user_auth` 行返回 `None`；`_seed_plan_types` 同样返回 `None`；`_pick_healthy_account(plan_types=None)` 走旧 `tier` 分支。因此运营者 seed（无 user_auth 行）完全不受档位/额度/号组约束。

**额度准实时**：`user_usage_total` 合并 `usage.py` 的内存缓冲（已计入未 flush 的 pending 计数），避免「刚写完就查询」少算。

**注册无运营者介入**：`register` 生成 seed 后直接 `seed_map[seed]` + `persist_seed_map`，`_login_redirect` 303 → `/?token=seed`。seed 首请求经 `_resolve_seed_account` 分配号，无需运营者手动发 seed。

## 已知边界（如实标注）

- 邮箱验证占位（`require_email_verification=False`），SMTP 后补。
- 支付仅占位（`orders` 表 + pending），无真实微信/支付宝。
- 定价金额未定，订单 `amount` 字符串透传。
- `user.tier` 未单独加并发档位；并发分层当前由 Stage 5 的 `concurrency._resolve_limit` 按 `account.plan_type` 处理（规模化后如需按用户档位限并发再细化）。
- 会话密钥默认进程内随机（未配 `USER_SESSION_SECRET` 时重启失效），生产需显式配置。

## 验证状态

- [x] Verified：13 新增用例全绿 + 48 单测 + 76 e2e 无回归
- [ ] Unverified：真号冒烟（1 free + 2 plus 导入→plan_type 落库→分池→failover，属 Stage 5 收口项，未在本线程执行）
- [ ] Needs User：档位定价（free/plus/pro 金额）、额度默认值（50/500/2000 每日）、模型白名单是否合意
