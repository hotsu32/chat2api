# REVIEW_PACKET — 超市页 plan 预选（Dashboard「购买 Pro」入口）

> 证据优先，非描述。判据见 `EVALUATOR_store_plan_cta.md`。
> 分支：`codex/deepseek-store-plan-cta`（隔离 worktree，未推送）。

## 1. 复现（改前，RED）

改前两条测试同时失败，失败点正是缺陷本身：

```bash
$ .venv/bin/python -m pytest tests_e2e/test_saas_full_journey_e2e.py
E       AssertionError: assert 'plus-solo-1m' == 'pro-solo-1m'
FAILED tests_e2e/test_saas_full_journey_e2e.py::test_single_user_full_saas_journey
FAILED tests_e2e/test_saas_full_journey_e2e.py::test_dashboard_pro_cta_preselects_and_purchases_pro
```

读法：入口把 `/store?plan=pro-solo-1m` 渲染出去了（href 断言先过），但超市页
**渲染/提交**的是 `plus-solo-1m` —— 查询串在接收端被丢掉。

静态复核同源：

| 位置 | 改前 |
|---|---|
| `gateway/saas.py:399` | `store_page(request, expired="")` —— 没有 `plan` 形参，FastAPI 不解析它 |
| `templates/store.html:15/18` | tier 轴写死 `plus` 带 `is-selected` |
| `templates/store.html:47/49` | 摘要写死 `Plus · 独享 · 1 个月` / `¥99` |
| `templates/store.html:51` | `value="plus-solo-1m"` |
| `templates/store.html:69` | `const state = { tier: "plus", ... }` —— 加载时 `render()` 把上面一切重写回 Plus |
| `templates/dashboard.html:69` | `href="/store?plan=pro-solo-1m"`（**这里本来就是对的**，未改动） |

## 2. 变更清单

| 文件 | 变更 | 状态 |
|---|---|---|
| `gateway/saas.py` | 新增 `_STORE_DEFAULT_PLAN`；`store_page` 接收 `plan`，经 `plans.plan_detail` 校验后透传 `selected` 给模板；非法/缺失退回默认档 | 修改 |
| `templates/store.html` | 三个轴的 `is-selected`、摘要名/价、hidden 值、脚本 `state` 全部改由 `selected` 驱动 | 修改 |
| `tests_e2e/test_saas_full_journey_e2e.py` | 新增 CTA 辅助 + 主链路步骤 4 跟随渲染出来的两个 CTA；新增 `test_dashboard_pro_cta_preselects_and_purchases_pro` | 修改 |
| `tests_e2e/test_store_plan_cta_browser.py` | 新增：真实 Chromium 打开真实渲染字节，读回 JS 跑完后的 DOM（3 例） | 新增 |
| `templates/dashboard.html` | **未改动** —— 入口 href 本来就是对的，缺陷在接收端 | — |

## 3. 通过证据（改后，GREEN）

```bash
$ .venv/bin/python -m pytest tests_e2e/test_saas_full_journey_e2e.py \
    tests_e2e/test_store_plan_cta_browser.py tests_e2e/test_user_saas.py \
    tests_e2e/test_saas_product_gate.py tests_e2e/test_payment_fulfillment.py \
    tests_e2e/test_trial_dashboard.py -o addopts=""
98 passed in 56.64s          # 含 3 例真实浏览器用例，0 skipped

$ .venv/bin/python -m pytest tests_e2e/          # 506 collected
E2E_EXIT=0                   # 无可收集的失败

$ .venv/bin/python -m pytest tests/              # 965 collected
UNIT_EXIT=0
```

## 4. 浏览器证据（不只是读字节）

`tests_e2e/test_store_plan_cta_browser.py` 用真实 FastAPI app 渲染登录态下的超市页，
把**那份字节**用本地静态服务器发出去，再用真实 Chromium 打开并读回 JS 执行后的 DOM：

| 打开的 URL | 浏览器里 `#plan-id` | 浏览器里 tier 轴选中 | 摘要 |
|---|---|---|---|
| `/store?plan=pro-solo-1m` | `pro-solo-1m` | `pro` | `Pro · 独享 · 1 个月` |
| `/store?plan=plus-solo-1m` | `plus-solo-1m` | `plus` | `Plus · 独享 · 1 个月` |
| `/store` / 非法 plan | `plus-solo-1m` | `plus` | `Plus · 独享 · 1 个月` |

这一层是必要的：服务端把 hidden 值渲染成 `pro-solo-1m` **不够**，因为 `render()`
在加载时会按脚本 `state` 重写它。第 4 节的变异测试证明这一层单独就能抓到该缺陷。

## 5. 变异测试（证明断言可证伪，不是恒真）

| 变异 | 结果 |
|---|---|
| A：`store_page` 重新丢掉 `plan`（`selected = plans.plan_detail(_STORE_DEFAULT_PLAN)`） | 主链路 + 新用例 **2 failed**；浏览器用例 **3 failed**（真实 Chromium 读到 `plus-solo-1m`）—— 是 fail 不是 skip |
| B：模板 HTML 正确，但脚本 `state.tier` 写回 `"plus"` | **2 failed** → 证明「脚本 state」断言独立有牙 |

变异后均已还原（`git diff` 复核见下）。

## 6. 安全性（注入面）

`?plan=` 只经 `utils.plans.plan_detail` 校验（与 `/checkout` 同源），未命中即退回
默认档。以下输入全部落到 `plus-solo-1m`，没有任何值被原样带进结算表单：

`""`、`free-solo-1m`、`pro-solo-1m-extra`、`pro-solo`、`  pro-solo-1m  `、
`<script>alert(1)</script>`、`../../etc/passwd`（TestClient 与 Chromium 两条路径都跑）。

Jinja 侧：HTML 上下文用默认转义，脚本上下文用 `| tojson`。

## 7. 未验证 / 残余风险

- **未验证**：真实部署环境下的多 worker 行为 —— 本改动无状态，理论上无关，但没在
  多进程下跑过。
- **未验证**：`base.html` 的外链静态资源在浏览器探针里 404（静态服务器只发超市页
  字节），因此**布局/样式**未在探针中验证；预选逻辑不依赖它们。
- **残余风险**：若 `utils/plans` 目录里 `plus-solo-1m` 消失，`selected` 为 `None`，
  页面渲染成空摘要（不 500、不注入任意值，但页面是坏的）。该分支当前不可达
  （12 组合是静态常量表），未额外加防御代码。
- **未做**：Kimi Code 二次评审 —— 本 worktree 为隔离 worker，未调用
  `~/.kimi-code/bin/kimi`；留给协调方按 `/kimi-collab` 决定是否需要。
