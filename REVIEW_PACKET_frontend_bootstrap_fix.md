# REVIEW_PACKET — 前端裸页修复（client-bootstrap 外科手术重写 + 静态资源凭据绕过）

日期：2026-09-08
分支：main
范围：`gateway/chatgpt.py`、`gateway/reverseProxy.py`、`tests_e2e/test_gateway_golden_path.py`

## 1. 产品 / 用户影响

镜像网页此前打开只剩一个无样式的简陋裸壳，完全无法使用。修复后浏览器打开镜像即得到
与官方一致的完整 ChatGPT UI（侧栏、模型切换、输入框、流式样式齐全），且页面里嵌入的
账号身份仍是脱敏后的镜像用户身份，不泄漏号池持有者（owner）身份。

## 2. 根本原因（两个独立缺陷，均有证据）

前端是 React Router v7 SSR 应用，整页 90%（约 510KB / 568KB）是内嵌在
`<script id="client-bootstrap">` 里的「完整启动状态」——其中 `statsigPayload`
（Statsig 特性开关，约 477KB）是前端启动时 `JSON.parse(r.statsigPayload)` 的必需输入。

- **缺陷 1（启动崩溃）**：`gateway/chatgpt.py::_rewrite_client_bootstrap` 把整段 510KB
  bootstrap 整体替换成约 2.6KB 的 `{authStatus, session}`，丢掉了 `statsigPayload`。
  前端启动 `JSON.parse(undefined)` 抛 `SyntaxError`，boot 崩溃，页面只剩裸壳。
  证据：官网实测 568,390B vs 镜像下发 60,764B；Playwright console 抓到
  `SyntaxError: "undefined" is not valid JSON`；root chunk 源码确认 `Ane(r.statsigPayload)`。
- **缺陷 2（样式 403）**：`gateway/reverseProxy.py` 对**所有**路径（含 `/cdn/assets/*`）
  注入 `Authorization: Bearer <access_token>` + owner session cookie。CDN 对携带凭据的
  静态请求直接 403 → CSS 加载失败 → 页面无样式。
  证据：curl header 矩阵复现——匿名 200，带 token cookie 403。

## 3. 修复内容（File-Level）

- `gateway/chatgpt.py`
  - 新增 `_stable_device_id()`：按种子用户 uuid5 派生稳定设备指纹，替换 statsig 里 owner 的设备 ID。
  - 新增 `_sanitize_statsig_payload()`：把 statsigPayload 内嵌的 owner user 对象
    （userID / email / customIDs.account_id / 设备 ID / custom.account_id 等）替换为镜像用户身份，
    **特性开关评估值原样保留**（服务端已预评估）。
  - 重写 `_rewrite_client_bootstrap()`：由「整体替换」改为「外科手术式合并」——
    仅覆盖 `authStatus` / `session` / `user` / `statsigPayload` 四个身份字段，
    其余 40+ 启动配置键（sessionId、entryContext、cluster、locale、feature gates …）全部保留。
    解析失败回退整体替换（宁可降级也不下发 owner 凭据）。
- `gateway/reverseProxy.py`
  - 新增 `is_static_asset = "cdn/" in path or "assets/" in path`。
  - 静态资源：跳过 owner session cookie 注入、跳过 seed 解析与 `Authorization` 注入，匿名直连 CDN。
- `tests_e2e/test_gateway_golden_path.py`
  - 扩展 E1 用例：canned 模板嵌入 owner 身份的 session + statsigPayload + 非身份键 sessionId，
    字段级断言「启动配置保留 + owner 身份全脱敏 + 原文无任何 owner 字串」。

## 4. 验证（证据，非描述）

### 4.1 单元 / E2E 回归 —— Verified
```
$ PYTHONPATH=. .venv/bin/python -m pytest tests/
48 passed in 1.33s
$ PYTHONPATH=. .venv/bin/python -m pytest tests_e2e/
77 passed in 21.65s
```
（含扩展后的 E1：`test_seed_visit_rewrites_client_bootstrap_identity` 单跑 1 passed）

### 4.2 页面体积与身份脱敏 —— Verified
```
$ curl -s "http://127.0.0.1:5005/?token=<seed>" -o served.html
HTTP 200  size 564421            # 修复前 60,764B → 恢复全量
```
解析下发页面的 client-bootstrap（实测，非推断）：
```
authStatus         = logged_in
session.user.name  = ChatGPT     # 匿名
session.user.email = ''          # 匿名
session.account.planType = free
statsigPayload     = 430,644 B 保留（feature_gates 完整）
bootstrap keys     = 42          # 40+ 启动键全保留，未被整体替换
owner email 探针   = 未出现（@gmail.com/@qq.com/@outlook.com 均 absent）
```

### 4.3 静态资源不再 403 —— Verified
```
$ curl /cdn/assets/root-bf3xaoxt.css            匿名        -> 200
$ curl /cdn/assets/root-bf3xaoxt.css            带 token    -> 200   # 修复前 403
$ curl /cdn/assets/manifest-dc355b04.js         匿名        -> 200
$ curl /cdn/assets/manifest-dc355b04.js         带 token    -> 200
```

### 4.4 浏览器实测（Playwright）—— Verified
- Page Title: `ChatGPT`（真实 App 已 boot）。
- live DOM 中 client-bootstrap 实测：`authStatus=logged_in`、`user.name=ChatGPT`、
  `user.email=''`、`account.planType=free`、`statsigPayload=430,644B`、`bootstrapKeys=42`。
- 截图 `~/fixed-page.png`：完整 ChatGPT UI（侧栏 + 模型切换 + 输入框 + 流式样式）。

## 5. 遗留 / 未验证（如实标注）

- [ ] Unverified（无害，环境性）：console 仍有错误，均不影响功能——
  - Datadog「non-allowed domain」：镜像域名固有，良性。
  - `/ces/v1/rgstr` SSL 错误：Statsig 遥测 beacon 硬编码 `https://`，本地 `http://127.0.0.1`
    下 SSL 失败；仅遥测，真实 HTTPS 部署后消失。
  - React #418 hydration warning：recoverable，App 正常 boot。
- [ ] Unverified（设计权衡）：「User should have either email or phone number」——
  某 profile/settings 组件期望 contact 字段，而匿名 session `email=""`。不影响聊天主链路
  （侧栏/输入框/模型切换正常）。这是「完全匿名化」的固有副作用，如需消除可后续给匿名用户
  合成占位 email（如 `<seed>@mirror.local`），本轮不处理。
- [ ] Needs User：真实 HTTPS 域名部署后复测 `/ces` beacon 与 Datadog 是否归零。

## 6. 风险与回滚

- 风险：外科手术重写依赖 `client-bootstrap` 是合法 JSON。官网结构稳定，且解析失败有
  「整体替换」兜底（降级但不泄漏凭据），风险可控。
- 回滚：`git revert <本 commit>` 即可整体回退三文件改动；E1 旧断言随之恢复。
- 不涉及 schema / 认证 / 支付 / 部署变更。

## 7. 安全说明

- 全程未回显任何真实 token 值；测试 seed 为合成身份。owner 身份在 session 与
  statsigPayload 两处均已脱敏，页面原文无任何 owner 字串（探针验证）。
