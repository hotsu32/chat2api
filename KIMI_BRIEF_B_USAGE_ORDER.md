# Kimi 改前审阅 brief：SaaS B 批次（用量记录 + 订单状态）

日期：2026-09-12。请在我修改前审阅当前代码，给出问题、方案和边界。工作区位于 `/Users/Zhuanz/chatgpt-mirror/chat2api`，审阅期间我不改文件。

## 背景

SaaS 的核心付费链路和批次 A 安全项已经完成。下一轮从已延期事项中选两项：

1. **使用记录接线**：反向代理成功路径已经把事件写入 `utils.usage` 的内存队列，定期 flush 到 `usage_events`；但 `gateway/saas.py::usage_page` 仍把 `records` 固定传成空列表，用户看不到自己的使用轨迹。
2. **订单状态可见性**：`orders` 已有 `pending/paid/failed`、`created_at`、`updated_at`、`expires_at`；钱包只把状态显示成一张静态表。支付 provider 非 mock 时，`/api/checkout` 跳到带 `order=` 的 checkout URL，但 checkout 页面没有读取订单或显示明确的待支付状态，也没有用户可访问的订单详情路径。

## 当前代码位置

- `gateway/saas.py`：`usage_page`（约 297 行）、`api_checkout`（约 328 行）、`_order_rows`（约 174 行）
- `utils/usage.py`：`record_usage`、`flush_usage`、`user_usage`、`user_usage_total`
- `utils/store.py`：`query_usage(since)`、`query_usage_count`、`list_orders(email)`、`get_order(order_id)`
- `templates/usage.html`、`templates/checkout.html`、`templates/wallet.html`
- 现有测试：`tests/test_usage.py`、`tests_e2e/test_gateway_golden_path.py`、`tests_e2e/test_user_saas.py`

## 希望你重点攻击的问题

### A. 使用记录

- 应该按什么粒度展示：每条事件、按日期聚合，还是按套餐/类型聚合？当前表字段只有 `seed/account/kind/created_at`，不能暴露 seed、account 等内部凭据。
- 内存 pending 尚未 flush 时，页面是否要包含？如果包含，如何与 SQLite 查询去重；如果不包含，用户刚进入聊天后看不到记录是否可接受？
- 时间范围是否应限制（例如最近 30 天），避免 `query_usage` 无上限读表；是否需要分页？
- 事件 `kind` 来自反代路径分类，如何映射为用户可读文案，未知 kind 如何降级？
- 一个 SaaS 用户可能有多个套餐，但当前记录只带 seed，不直接带 tier/order；应按用户总记录展示，还是用当前有效套餐标注？请避免伪造无法从数据推导的套餐归属。
- `utils.usage.user_usage()` 只查已 flush 的库，`user_usage_total()` 才合并 pending；最佳实现应复用现有抽象而不是绕过锁。

### B. 订单状态

- `order_id` 是用户可控查询参数时，必须先绑定当前登录 email，不能 IDOR 读取别人的订单。
- pending/paid/failed 的页面状态和刷新行为如何设计，避免伪装成已支付或重复触发结算？
- 现有 mock 下单自动结算，真实 provider 需要保留 pending；不要把页面刷新变成支付回调或激活操作。
- 订单状态页是否应支持 `order=` 查询参数复用 checkout，还是新增 `/order` 路由？请按现有路由风格给最小方案。
- 失败/不存在/过期订单的错误语义要统一，不能泄露其他用户订单是否存在。
- 是否需要为订单保存 provider/payment reference？如果当前 schema 不足，请明确是否应延期而不是在本轮扩大数据库变更。

## 约束

- 只改 SaaS 层；不要修改 `chatgpt/authorization.py`、antiban、reverseProxy 调度、`globals.seed_map`。
- 不接入真实支付、不新增生产依赖、不触碰 secrets。
- 两套 pytest 必须分开运行：`.venv/bin/python -m pytest tests/` 和 `.venv/bin/python -m pytest tests_e2e/`。
- 必须补行为测试，不只测试模板字符串；重点覆盖 pending 订单不能看成 paid、跨账号订单不可读、usage 不泄露 seed/account、pending 事件是否按约定显示。
- 输出方案时请区分 P0/P1/P2/P3，并指出任何需要我在实现前重新确认的高风险项。
