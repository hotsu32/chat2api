# EVALUATOR — 超市页 plan 预选（Dashboard「购买 Pro」入口）

> 先定判据再改代码。每条都必须是可证伪的：给出命令 / 断言，跑一遍就能判真假。

## 缺陷陈述（改前事实）

`gateway/saas.store_page` 只接 `expired` 一个查询参数，丢掉 `plan`；`templates/store.html`
把预选档、摘要与 hidden 值**三处写死**为 `plus-solo-1m`。于是 Dashboard 上
`/store?plan=pro-solo-1m`（`templates/dashboard.html:69`）这个入口，落到的是 Plus 档，
提交的也是 Plus —— 用户点「购买 Pro」买到的是 Plus。

## 判据

| # | 判据 | 怎么验（可证伪） | 期望 |
|---|---|---|---|
| E1 | Pro 入口预选 Pro | 登录态 `GET /store?plan=pro-solo-1m`，正则取 hidden 值 / 三个轴的 `is-selected` / 摘要 | hidden = `pro-solo-1m`；tier 轴选中 `pro` 且 `plus` 未选中；摘要 `Pro · 独享 · 1 个月`、`¥199` |
| E2 | **浏览器里**也预选 Pro | 真实 Chromium 打开 E1 渲染出的字节，读 `#plan-id` 与各轴 `.opt.is-selected` | `#plan-id` = `pro-solo-1m`；tier 轴 = `pro` |
| E3 | Pro 被真的提交 | 把 E1 页面渲染出来的 hidden 值 POST 到 `/api/checkout` | 200，且新增已支付订单 `tier_id = pro-solo-1m` |
| E4 | Plus 入口未被带偏 | 同样走 `/store?plan=plus-solo-1m` | 预选 `plus-solo-1m`，摘要 `Plus · 独享 · 1 个月` |
| E5 | 非法 / 缺失 plan 不能注入 | `""`、`free-solo-1m`、`pro-solo-1m-extra`、`pro-solo`、`  pro-solo-1m  `、`<script>alert(1)</script>`、`../../etc/passwd` | 一律退回默认档 `plus-solo-1m`；表单里只有一个目录内的 plan 值 |
| E6 | 默认落地不变 | `GET /store`（无查询串） | 仍为 `plus-solo-1m`，`Plus`+`独享`+`1 个月` 选中，摘要 `¥99` |
| E7 | 预选不冻结选择器 | 浏览器里点 tier 轴的 Pro | hidden 值变为 `pro-solo-1m`，摘要跟着变 |
| E8 | 无回归 | `tests/` 与 `tests_e2e/` 全量 | 全绿 |

## 非目标（刻意不做）

- 不改 `templates/dashboard.html`：两个入口渲染出来的 href 本来就是对的
  （`?plan=plus-solo-1m` / `?plan=pro-solo-1m`），缺陷在接收端。
- 不改套餐目录、价格、结算页语义、`/checkout` 的校验路径。
- 不引入新的默认档常量模块：默认值留在 `gateway/saas.py`（本次改动范围内）。
- 不碰 Seed 生命周期、生成、antiban、网络路由。

## 残余风险

- `_STORE_DEFAULT_PLAN` 若在目录里消失（`utils/plans` 是静态表），`selected` 会是
  `None`，页面渲染成空摘要、表单值为空 —— **不会 500、也不会带任意值**，但页面是坏的。
  该分支当前不可达（12 个组合是常量表）；已在 `REVIEW_PACKET_store_plan_cta.md` 登记。
- 浏览器用例在无 node/无 Chromium 的机器上 skip（退出码 42），此时 E2/E7 由 E1 的
  「脚本 state 断言」兜底（见 REVIEW_PACKET 的变异测试记录）。
