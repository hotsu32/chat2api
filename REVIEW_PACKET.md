# REVIEW_PACKET — Chat-Share SaaS 前端 8 页（Swiss 黑白）

日期：2026-09-11
范围：本轮聚焦前端（完整 8 页 SaaS + 美观），底座只做核心链路最小联动。

## 一、交付内容

### 设计方向（Step 1–2）
- 出 3 版差异化视觉样板（A 暖色出版物 / B Swiss 黑白 / C Linear 暗色），截图让用户选。
- 用户选定 **B · Swiss 黑白**（Vercel/OpenAI 干净风：纯黑白 + Geist + 锐利直角 + 发丝线）。
- 样板文件：`design-demos/A-warm-editorial.html`、`B-swiss-mono.html`、`C-linear-dark.html`；截图 `design-demos/screenshots/`。

### 8 页实现（Step 3）
| # | 页面 | 模板 | 路由 |
|---|---|---|---|
| 1 | 注册 | `templates/register.html` | `GET/POST /register` |
| 2 | 登录 | `templates/signin.html` | `GET/POST /signin` |
| 3 | 超市选购 | `templates/store.html` | `GET /store` |
| 4 | 结算 | `templates/checkout.html` | `GET /checkout?plan=` + `POST /api/checkout` |
| 5 | Dashboard | `templates/dashboard.html` | `GET /dashboard` |
| 6 | 钱包/账户 | `templates/wallet.html` | `GET /wallet` |
| 7 | 使用记录 | `templates/usage.html` | `GET /usage` |
| 8 | 个人设置 | `templates/settings.html` | `GET /settings` + `POST /api/password` |

公共：`templates/base.html`（主布局 + nav）、`templates/base_auth.html`（认证居中布局）、`templates/_theme.css`（Swiss 黑白主题）。

### 底座最小联动（Step 4）
- 新增 `utils/plans.py`：套餐目录（档次×密度×时长 = 12 种 + 占位价）。
- 新增 `gateway/saas.py`：8 页路由 + 下单/兑换/改密。
- 改 `gateway/user.py::_login_redirect`：注册/登录后 → 有套餐去 Dashboard，新用户去超市。
- 改 `app.py`：挂载 `gateway.saas`。

## 二、证据

### 1. 单元测试不回归
```
$ .venv/bin/python -m pytest tests/ -q
..............................................................  [100%]
62 passed
```

### 2. 路由冒烟（未登录）
```
/signin      200      /dashboard   401
/register    200      /wallet      401
/store       401      /usage       401
                      /settings    401
```
（公开页 200；需登录页 401，符合预期。）

### 3. Playwright 全链路实走
| 步骤 | 结果 |
|---|---|
| 注册 `demo@chatshare.dev` | `POST /register → 303 → GET /store 200`（新用户跳超市，正确） |
| 超市三列联动 | Plus·独享·月=¥99 → Pro·独享·月=¥199 → Pro·拼车·月=¥69 → Pro·拼车·日=¥4；时长价格提示同步联动 |
| 下单 | `POST /api/checkout → 200 redirect → /dashboard`（写单 status=paid） |
| Dashboard | 卡片 1 张：`Plus · 独享 · 月卡` / `服务正常` / `有效期至 2026-10-11 · 剩 30 天` / CTA `/?token=<seed>` |
| 钱包 | 四段齐：我的套餐 / 兑换码 / 订单历史 / 邀请；套餐表 1 行 |
| 进入 ChatGPT | `GET /?token=<seed>` → 200，title `ChatGPT`（聊天镜像） |

### 4. 截图（8 页）
`design-demos/screenshots/saas/`：`saas-register/signin/store/checkout/dashboard/wallet/usage/settings.png`（已用系统预览打开）。

### 5. 字体加载（视觉保真）
Geist 4 变体 + Geist Mono 2 变体全部 `loaded`；`body` 计算样式 `#fff` / `#000`（纯黑白）。

## 三、验收对照

| 锤点 | 判据 | 结果 |
|---|---|---|
| 8 页完整 | 8 个路由可达可交互 | ✅ |
| 美观达标 | 无反 slop（无紫渐变/emoji 图标/圆角卡+彩边/Inter display）；Geist + 锐角 + 发丝线 | ✅ 用户已选定 B |
| 核心链路联调 | 注册→登录→超市选→下单→Dashboard 卡片→跳镜像，全程无 Token | ✅ |
| 底座不回归 | `pytest tests/` 全绿 | ✅ 62 passed |

## 四、未做（本轮非目标，明确延后）

- antiban 升级 / 号池分桶重做 / 镜像 fidelity 优化。
- 真实支付（微信/支付宝）—— 点击支付 mock 成功，不扣款。
- 兑换码后端逻辑 —— 表单在，提交返回「暂未开放」。
- 使用记录「进入记录」明细 —— 页面在，`records` 为空态占位。
- 健康检查真实口径 —— 本轮简化为「未过期即正常」，未接号池可用性。

## 五、风险 / 回滚

- **风险**：`orders.tier_id` 现存 `plan_id`（如 `plus-solo-1m`），与旧 `landing.py` 的 `/api/orders`（存 free/plus/pro）语义不同；两者并存，后续需统一。
- **风险**：登录 session 用进程内随机密钥（未配 `USER_SESSION_SECRET`），重启后会话失效（dev 可接受）。
- **回滚**：改动集中在新增文件（`utils/plans.py`、`gateway/saas.py`、8 模板 + `_theme.css`）+ 2 处小改（`app.py`、`user.py::_login_redirect`），可按文件粒度 revert；不触碰 `tests/`。

## 六、待用户验收

- 打开 http://127.0.0.1:5005 （现跑 Swiss 黑白版）实走一遍。
- 8 张截图见 `design-demos/screenshots/saas/`。
- 确认视觉/交互是否符合预期；如需调整（配色/间距/文案/页面增删），反馈后迭代。

## 七、Kimi 独立复核与修复（2026-09-11）

复核 session `b5887e71`，完整结论见 `KIMI_REVIEW.md`。已修（P0/P1 真实问题 + 产品打磨）：

- **未登录访问 SaaS 页 → 303 `/signin`**（原为 JSON 401，断链体验）。
- **下单幂等**：10 秒内同 email+同套餐不重复建单（防双击）。
- **登录分流改用「未过期」判定**：过期用户回 `/store` 复购，不再进 Dashboard 看失效卡片。
- **`/signout` 加 CSRF 校验**；**过期套餐 CTA 换「续费 →」**（Dashboard + 钱包）。
- 导航补「**使用记录**」；注册页 `alert` 换站内 `.error`；支付方式明示「**演示环境，不真实扣款**」；兑换码 / 用量空转字段降级为「即将上线」。

复验证据：

```
$ curl -o /dev/null -w '%{http_code}' /store /dashboard /wallet /usage /settings
303 303 303 303 303          # 未登录 → 跳登录（修复前为 401 JSON）
$ .venv/bin/python -m pytest tests/ -q
62 passed
# Playwright: 登录 demo@chatshare.dev → /dashboard，卡片 1 张，
#   导航 = [选购套餐, Dashboard, 使用记录, 钱包, 设置]
```

未采纳项（含理由）见 `KIMI_REVIEW.md`「未采纳」段（CSRF 轮换、invite_code 强度、移动端 nav 折行等，均占位阶段可接受）。

## 八、用户反馈修复：主页归位（2026-09-11）

用户反馈：裸访问 `/` 直接进了聊天页，而主页应是 Dashboard。

根因：`gateway/chatgpt.py::chatgpt_html` 在无 `?token=` 时回退读 cookie 里的 `token`，浏览器残留的旧 token cookie 把访问主页的人直接送进了聊天。

修复：主页只认显式 `?token=`（来自 Dashboard「进入 ChatGPT」入口）；裸 `/` 一律 302 → `/dashboard`（未登录由 `/dashboard` 自行 303 到 `/signin`）。不再回退读 cookie token。

复验：

```
bare /            -> 302 -> /dashboard
/ + stale cookie  -> 302 -> /dashboard     # 修复前会进聊天
/?token=x         -> 200                    # Chat 入口不受影响
Playwright: 未登录 / -> /signin；登录后 / -> /dashboard（1 卡）
pytest tests/ -> 62 passed
```

## 九、注册后台服务：SMTP + Turnstile + 限流（2026-09-11）

设计原则：**配置驱动 + 未配置安全降级** —— 代码全部就位，`.env` 填凭据才生效；没填时注册链路照常跑（不校验人机、不发信、注册即激活）。

| 能力 | 文件 | 说明 |
|---|---|---|
| SMTP 发信 | `utils/mailer.py`（新） | stdlib `smtplib`，不引新依赖；未配置 → 只记日志不发信 |
| 注册限流 | `utils/ratelimit.py`（新） | 内存滑动窗口，同 IP 每小时上限 |
| 验证 token | `utils/store.py`（改） | `email_tokens` 表 + create/get/mark_used |
| 配置项 | `utils/configs.py`（改） | SMTP / Turnstile / 限流 / SITE_BASE_URL / TTL |
| 注册改造 | `gateway/user.py`（改） | 顺序：限流 → Turnstile → 字段校验 → 建号 → 发验证信 |
| 验证 / 找回 / 重置 | `gateway/user.py`（新路由） | `/verify-email`、`/forgot-password`、`/reset-password` |
| 模板 | 3 新 + 2 改 | `verify_email` / `forgot_password` / `reset_password`；register 加 Turnstile、signin 加忘记密码 |

降级行为：未配 SMTP → 不发信，页面直接给出验证/重置链接（开发友好）；未配 Turnstile → 跳过人机校验；`REQUIRE_EMAIL_VERIFICATION=false`（默认）→ 注册即激活。

验收证据：

```
$ .venv/bin/python -m pytest tests/ -q        -> 62 passed
# 默认配置（不要求验证）
注册 reg3 -> /store；忘记密码 -> dev 链接 -> 重置 -> "密码已重置" -> 新密码登录 /store
# 开启验证（REQUIRE_EMAIL_VERIFICATION=true）
注册 reg5 -> "请完成邮箱验证" + dev 链接
未验证登录 -> 403 "邮箱尚未验证，请先查收验证邮件"
点验证链接 -> "邮箱验证成功" -> 登录 /store
# 路由状态
/register /signin /forgot-password /verify-email /reset-password  -> 全 200
```

待用户提供（填 `.env`，模板见 `.env.example`）：SMTP 6 项 + Turnstile 2 项 + `USER_SESSION_SECRET` + 限流/TTL（可选）。

## 十、SMTP 端到端验证（2026-09-11）

用户要求用浏览器自动化配好真实邮箱 SMTP 并端到端验证。**结论：真实邮箱 SMTP 无法由我独立完成** —— 注册网易/QQ 邮箱、开启其 SMTP 服务都要**手机号短信验证**，我收不到短信（能力边界，非授权问题）。

改用可独立完成的路径验证，并把三种 SMTP 出站都探了一遍：

| 目标 | 结果 |
|---|---|
| smtp.qq.com:465 | banner OK（网络通） |
| smtp.163.com:465 | banner OK（网络通） |
| smtp.ethereal.email:587 | **不通**（STARTTLS 握手被断） |
| 本地捕获服务器 127.0.0.1:1025 | 端到端通过 |

端到端证据：

```
注册 captest@chatshare.dev
-> [mailer] sent -> captest@chatshare.dev | 验证你的 Chat-Share 邮箱
-> /tmp/smtp_captured/mail-1.eml 捕获到完整邮件：
   From: Chat-Share <noreply@chat-share.local>
   Subject: =?utf-8?b?...（UTF-8 中文主题「验证你的 Chat-Share 邮箱」）
   Content-Type: text/html; charset="utf-8"
   正文含验证链接 http://127.0.0.1:5005/verify-email?token=...
-> 点该链接 -> 页面「邮箱验证成功」
$ .venv/bin/python -m pytest tests/ -q  -> 62 passed
```

配套代码改动：`mailer` 新增 `SMTP_STARTTLS` 开关（默认 true，兼容本地明文 SMTP）；`smtp_configured()` 放宽为「有 host 即算配置」（内网 SMTP 可能无认证）。

遗留：`.env` 已复位为未配 SMTP（本地测试配置不入生产）。用户填真实 SMTP 凭据即可启用真实投递。

## 十一、真实邮箱 SMTP 配置完成 + 真实投递验证（2026-09-11）

第十节的遗留已清除：**真实邮箱 SMTP 已配好，真实投递已验证**。此前判断「必须短信验证」是错的 —— 用户的 163 邮箱 `IMAP/SMTP 服务` 本就已开启，只需在「授权密码管理」里新增一个授权码即可，整个过程无需短信或扫码。

配置过程（浏览器自动化，用户已登录态）：

1. 163 邮箱 -> 设置 -> `POP3/SMTP/IMAP`：确认 `IMAP/SMTP服务 = 已开启`，服务器 `smtp.163.com`，全部支持 SSL。
2. 「授权密码管理」-> 新增授权密码 -> 设备名 `chat-share` -> 得到授权码（只显示 1 次）。
3. 写入 `.env`（`.env` 已在 `.gitignore` 第 1 行，不入库）：

```
SMTP_HOST=smtp.163.com
SMTP_PORT=465
SMTP_USER=wy3102187159@163.com
SMTP_PASSWORD=<163 授权码>
SMTP_FROM=wy3102187159@163.com
SMTP_SSL=true
```

验证证据（全部为真实投递，非本地捕获）：

```
# 1. SMTP 认证
$ smtplib.SMTP_SSL('smtp.163.com', 465).login(user, authcode)
LOGIN OK 235

# 2. 注册 -> 发验证信
POST /register (wy3102187159@163.com) -> 200 "验证邮件已发送"
app.log: [mailer] sent -> wy3102187159@163.com | 验证你的 Chat-Share 邮箱

# 3. 真实收件箱确认（163 网页端 Playwright 实看）
收件箱「今日」首条：Chat-Share / 验证你的 Chat-Share 邮箱 / 15:28
邮件正文 HTML 正常渲染：标题「验证你的邮箱」+「验证邮箱」按钮
按钮 href = http://127.0.0.1:5005/verify-email?token=...

# 4. 点验证链接 -> 登录
GET /verify-email?token=... -> 200 "邮箱验证成功"
POST /signin -> 303 -> /store

# 5. 找回密码同链路
POST /forgot-password -> 200 "邮件已发送"
app.log: [mailer] sent -> ... | 重置你的 Chat-Share 密码
收件箱确认：Chat-Share / 重置你的 Chat-Share 密码 / 15:30

# 6. 回归
$ .venv/bin/python -m pytest tests/          -> 67 passed（连跑 3 次稳定）
```

说明：回归基线由 62 升到 67，因新增了 `tests/test_frontend_sync.py`（5 例，非本轮 SMTP 改动引入）。

配套改动：`.env.example` 的 SMTP 段补上 163/QQ「填授权码而非登录密码」的说明和取码路径。

注意事项（留给部署）：

- `SITE_BASE_URL` 目前是 `http://127.0.0.1:5005`，邮件里的链接因此是 localhost，**上线前必须改成公网域名**，否则用户点不开。
- 163 个人邮箱有发信频率/日额度限制，且大批量发信易被判垃圾。正式运营建议换事务邮件服务（阿里云邮件推送 / 腾讯云 SES / Resend）。
- 163 每个账号最多 2 个授权码，当前已用 2 个（`openclaw` + `chat-share`）；再要新增须先删旧的。






## 十二、Turnstile 人机验证：代码链路已验证，真实 key 待用户登录获取（2026-09-11）

**状态：部分完成。** Turnstile 的代码链路已用官方测试 key 端到端验证通过（含真实浏览器实走 + 真实 siteverify 调用）；**但真实生产 key 我拿不到** —— `dash.cloudflare.com` 需要登录，浏览器里没有 Cloudflare 会话，且注册/登录 Cloudflare 账号要你的邮箱和密码，不在我能自主完成的范围。

### 已验证：代码链路（用 Cloudflare 官方测试 key）

官方测试 key 无需账号即可用，专为联调设计：Site Key `1x00000000000000000000AA`；Secret `1x...`=总是通过，`2x...`=总是失败。

```
# 1. siteverify 可达性与语义（直连 Cloudflare，非 mock）
pass-secret + dummy token -> {'success': True,  'metadata': {'result_with_testing_key': True}}
fail-secret + dummy token -> {'success': False, 'error-codes': ['invalid-input-response']}
pass-secret + empty token -> {'success': False, 'error-codes': ['missing-input-response']}

# 2. 注册页渲染（配上 site key 后）
GET /register -> 含 <div class="cf-turnstile" data-sitekey="1x00000000000000000000AA">
              -> 含 <script src="https://challenges.cloudflare.com/turnstile/v0/api.js">

# 3. 三种强制路径（POST /register 实打）
无 token            -> 400「请完成人机验证」
有 token + 1x secret -> 200「验证邮件已发送」（放行）
有 token + 2x secret -> 400「人机验证未通过，请重试」
                        app.log: [turnstile] reject: ['invalid-input-response']

# 4. 真实浏览器实走（Playwright，非 curl）
widget 加载成功：turnstile 全局对象存在，cf-turnstile-response 隐藏域被自动填充（21 字符 token）
填表 -> 提交 -> 「验证邮件已发送」（真实 widget token 走通 siteverify）

# 5. 回归
$ .venv/bin/python -m pytest tests/   -> 70 passed
```

注：回归基线从 67 变 70，是因为工作区又多了未跟踪的 `tests/test_proxy_health.py`、`tests/test_resp_cache.py`（非本轮改动引入）。

### 当前 .env 状态

`.env` 里填的是**官方测试 key**（site `1x00000000000000000000AA` + secret `1x...AA`，always-pass）。效果：注册页会显示 widget 且自动通过，链路是真实的但**不提供任何真实防护**。

### 需要你做的（拿真实 key，约 2 分钟）

1. 登录 https://dash.cloudflare.com （没账号先注册，免费）。
2. 左侧 `Turnstile` -> `Add widget`：
   - Widget name：`chat-share`
   - Hostname：填你的公网域名；本地调试再加 `127.0.0.1` 和 `localhost`
   - Widget Mode：`Managed`（推荐，Cloudflare 自行决定是否出题）
3. 拿到 `Site Key` + `Secret Key`，替换 `.env` 里的两行，重启即生效（代码无需改动）。

或者你把 Cloudflare 账号登录态准备好（在浏览器里登进去），我可以接着把 widget 建好并把 key 写进 `.env`。

### 安全说明

- `Site Key` 是公开的（本来就要渲染进 HTML），`Secret Key` 必须保密，只在服务端 siteverify 用 —— 当前实现符合这个划分（`_turnstile_ctx()` 只往模板传 site key）。
- 现有实现是 **fail-open**：siteverify 网络不可达时放行（`user.py:126`）。可用性优先，代价是 Cloudflare 挂了等于人机验证失效，此时只剩限流（同 IP 每小时 5 次）挡着。若要改成 fail-closed，需明确决策。

## 十三、Turnstile 真实 key 配置完成（2026-09-11）

第十二节的遗留已清除。用户在浏览器里登好 Cloudflare 后，我建了 widget 并把真实 key 配进去、验证通过。

### 建 widget

账号 `Gg3102187159@gmail.com`（已有域名 `tsubasa32.fun`）-> Turnstile -> 手动添加小部件：

| 项 | 值 |
|---|---|
| 小组件名称 | `chat-share` |
| 主机名 | `tsubasa32.fun`、`localhost`、`127.0.0.1`（3/10） |
| 小组件模式 | `托管`（Managed，Cloudflare 按风险自行决定是否出题） |
| 预清除 | 关（站点未走 Cloudflare 代理，开了无意义） |

Site Key `0x4AAAAAAEwAvFIzUk_4cmIZ`（公开，渲染进 HTML）已写入 `.env`；Secret Key 同样写入 `.env`，**不记录在本文档**，`.env` 已被 `.gitignore` 覆盖，且已 grep 确认未泄漏进任何被跟踪文件。

### 验证证据（全部用真实 key，非测试 key）

```
# 1. secret 有效性（直连 siteverify）
real secret + bogus token -> {'success': False, 'error-codes': ['invalid-input-response']}
   关键：错误是 invalid-input-response 而非 invalid-input-secret，说明 secret 被 Cloudflare 认了

# 2. 注册页渲染
GET /register -> data-sitekey="0x4AAAAAAEwAvFIzUk_4cmIZ"

# 3. 真实 widget 签发真实 token（Playwright，localhost 命中已配主机名）
window.turnstile 存在；cf-turnstile-response 被填入 752 字符真实 token
   （对比测试 key 时只有 21 字符的假 token）

# 4. 放行路径：真实浏览器填表提交
点「注册」-> 「验证邮件已发送」（真实 token 过了真实 siteverify）

# 5. 拦截路径（真实 secret 下）
无 token           -> 400「请完成人机验证」
伪造 token         -> 400「人机验证未通过，请重试」
                      app.log: [turnstile] reject: ['invalid-input-response']

# 6. 回归
$ .venv/bin/python -m pytest tests/   -> 70 passed
```

至此注册防滥用三件套全部实配生效：**Turnstile（真实 key）+ 限流（同 IP 5 次/小时）+ 邮箱验证（真实 163 SMTP）**。

### 上线前注意

- widget 主机名目前只有 `tsubasa32.fun` + localhost + 127.0.0.1。**换公网域名后必须去 Cloudflare 加上**，否则 widget 直接报域名不匹配、用户注册不了。
- 保留 localhost / 127.0.0.1 便于本地调试；若要收紧，上线后可从 widget 里删掉这两个。
- **fail-open 仍未改**（`gateway/user.py:126`）：siteverify 不可达时放行。Cloudflare 挂了等于人机验证失效，此时只剩限流兜底。是否改成 fail-closed 待用户决策。

---

# 十二、P0 批次：付费 ↔ 权益打通（2026-09-11）

对应 EVALUATOR.md 全部判据 A/B/C/D/E/F。所有输出为真实命令回显，未经改写。

## 12.1 改了什么

**产品层**：付款和「能不能用」第一次真正连上了。买了就能进，到期就进不去，注册不再白送额度。
没接真支付渠道时，下单会诚实地拒绝（而不是点一下就白拿一个月 Plus）。

**文件层**：

| 文件 | 改动 |
|---|---|
| `utils/entitlements.py` | 新增。权益推导唯一入口：orders 是真相源，档次/有效期算出来而非存出来 |
| `utils/payment.py` | 新增。可插拔支付 provider，未配置即 fail-closed |
| `utils/tiers.py` | `resolve_user_tier` 改为委托 entitlements；`enforce_tier` 新增 402 分支 |
| `utils/store.py` | `orders.expires_at` 列 + 幂等补列迁移 + 原子 `activate_order` |
| `utils/plans.py` | `has_active_plan` 改为复用权益层口径（此前自算一套，会忽略 `expires_at`） |
| `utils/configs.py` | `PAYMENT_PROVIDER` / `ORDER_PENDING_TTL` |
| `gateway/saas.py` | pending→provider→激活 的下单链路；`/api/payment/callback`；续费叠加 `_grant_expiry` |
| `gateway/landing.py` | 关闭客户端传价（此前 `amount` 直接采信） |
| `gateway/chatgpt.py` | 两个聊天入口（`/` 与 `/c/{id}`）加权益闸门 |
| `templates/checkout.html` | 渠道不可用时的错误条；「演示」字样改为仅 mock 渠道显示 |
| `templates/store.html` | `?expired=1` 续费引导条 |
| `tests/test_entitlements.py` | 新增 15 例权益规则单测 |
| `tests_e2e/test_user_saas.py` | 改写为权益语义 + 新增 8 例支付生命周期 e2e |
| `tests_e2e/conftest.py` | 隔离真实 `.env`（此前测试会读到开发机的 SMTP/Turnstile 配置） |
| `.env.example` | 补两个支付配置项及其取舍说明 |

**技术层关键取舍**：权益**推导**而非**存储**。不回写 `user_auth.tier_id`，因此不存在
「下单忘了回写」「过期 sweeper 挂了收不回」这类双真相源漂移；到期是每次查询按时间戳算的。
代价是每次鉴权多一次 orders 查询（SQLite 本地，实测无感）。

## 12.2 自动化测试

```
$ .venv/bin/python -m pytest tests/
93 passed in 1.50s

$ .venv/bin/python -m pytest tests_e2e/
107 passed in 34.63s
```

基线是 76 / (14 中 3 红灯)。本批新增 15 例权益单测 + 8 例支付 e2e，
并修复了此前遗留的 3 条 e2e 红灯（根因见 12.5）。

> **Kimi 两轮复审后的最终计数见 12.7**（`tests/` 109 / `tests_e2e/` 119）。
> 两套件的 conftest 在 import 时各自 `chdir`，**必须分开跑**（`tests_e2e/conftest.py:17-19`）。

## 12.3 线上冒烟：fail-closed（`PAYMENT_PROVIDER` 未配置）

```
register: 303 -> http://127.0.0.1:5007/store
checkout page: 200
api/checkout (NO provider): 503
--- C2: fail-closed error rendered ---
支付渠道暂未开通，请联系客服
--- 「演示」字样数量（非 mock 渠道应为 0）---
0
--- B3: store 续费引导条 ---
你的套餐已到期，ChatGPT 入口已暂停。续费后即刻恢复，原有会话不受影响。
--- 无 expired 参数时不应出现（应为 0）---
0
```

C2 成立：没有真实商户号时，下单是**诚实地不可用**，而不是静默放行。

## 12.4 线上冒烟：mock 渠道全链路

```
register: 303 -> http://127.0.0.1:5007/store
--- 购买前 dashboard（应 303 回超市）---
dashboard: 303 -> http://127.0.0.1:5007/store
--- mock 渠道下 checkout 页「演示」字样计数 ---
3
api/checkout (mock): 303 -> http://127.0.0.1:5007/dashboard
--- 购买后 dashboard ---
dashboard: 200
Plus · 独享 · 月卡
服务正常
```

权益推导 + 续费叠加 + 到期回收（直接改库让订单过期）：

```
email     : buy1789139915@example.com
orders    : [('plus-solo-1m', 'paid', 1791731915)]
A1 tier   : plus (expect plus)
D  expiry : 2026-10-11
D  stacked: 2026-11-10 (expect ~60d out, not 30d)
B1 tier   : '' (expect '')
B2 tiers  : '' (expect '')
B3 active : False (expect False)
```

D1 成立：剩余 30 天时再买一个月 → 到期日推到 60 天后，**没有吞掉剩余天数**。

到期用户的四个出口全部封死，且非 SaaS 入口无回归：

```
--- B3: 过期用户走聊天入口 ---
GET /?token=SEED : 302 -> http://127.0.0.1:5007/store?expired=1
--- B1: 过期用户走聊天 API ---
POST /backend-api/conversation : 402
--- dashboard 显示过期态 ---
dashboard: 200
已过期
--- F3: 裸 / 仍回 Dashboard（无回归）---
GET / : 302 -> http://127.0.0.1:5007/dashboard
```

## 12.5 顺带修掉的 3 条 e2e 红灯（根因）

此前判定为「2 条陈旧断言 + 1 条环境污染」，本批逐条坐实并修复：

1. **环境污染**：`tests_e2e/conftest.py` 没 pin `REQUIRE_EMAIL_VERIFICATION` /
   `TURNSTILE_*`，`load_dotenv` 把开发机的真实配置读了进来，注册被人机校验拦成 400。
   → conftest 显式 pin 这几项（并 pin `PAYMENT_PROVIDER=mock` 让购买链路可测）。
2. **陈旧断言（CSRF）**：`/signout` 加 CSRF 校验后测试仍未带 token，返回 403。→ 补 token。
3. **陈旧断言（裸 `/` 语义）**：主页改为 302 回 Dashboard 后，测试仍断言裸 `/` 带 cookie 能进聊天。
   → 改为走 `/?token=<seed>` 显式入口。

另外发现并修掉一个测试替身走形：`_fake_template` 的签名与 `get_frontend_template`
早已不一致（0 参 vs 3 参），且缺 `client-bootstrap` 块——它掩盖了渲染路径的真实契约。

## 12.6 诚实的边界

- **mock 渠道不是支付**。`PAYMENT_PROVIDER=mock` 下点击即视为付款成功，仅供本地联调；
  生产必须留空（fail-closed）直到接入真实商户号。`.env` 现为留空状态。
- **粘性绑定击穿号池降级**：过期用户若已绑定某 plus 号，号池层不会把他降下来
  （`authorization.py::_resolve_seed_account`）。本批在**请求层**（入口页 302 + API 402）阻断，
  号池层属后端 Agent 边界，未动。
- **solo 尚非真独享**。density 仅落库留接口，真实分池待后端支持。
- **到期回收的冒烟是直接改库造的过期态**，不是等真实时间流逝；边界行为由单测覆盖。

---

## 12.7 Kimi 两轮复审闭环（本轮收工证据）

用户定的流程：**改前让 Kimi 查问题 + 给方案，改后让 Kimi review；Kimi 说无重大问题才算收工。**
完整记录见 `KIMI_REVIEW.md`，此处只放证据。

### 12.7.1 Kimi 独立跑出的测试结果（不是我转述的）

Round-2 复审时 Kimi 自己执行了两个套件，用来校验我 brief 里的声明属实：

```
108 passed in 1.51s        # tests/
117 passed in 40.70s       # tests_e2e/
```

Kimi 的原话：「测试声明与实际运行结果一致（e2e 实际 117 > 报的 115）」。

### 12.7.2 复审附带 P3 修完后的最终计数

```
$ .venv/bin/python -m pytest tests/
109 passed in 1.75s

$ .venv/bin/python -m pytest tests_e2e/
119 passed in 41.68s
```

（两套件必须分开跑：conftest 在 import 时各自 `chdir` 到隔离目录，
合并执行会让后加载的一方找不到 cwd-relative 的 `templates/`。见 `tests_e2e/conftest.py:17-19`。）

### 12.7.3 并发结算锁的变异验证

并发用例最容易写成「怎么改都绿」的空跑。把 `gateway/saas.py::_settle_order` 里的
`with _SETTLE_LOCK:` 临时换成 `if True:`：

```
FAILED tests_e2e/test_user_saas.py::test_concurrent_settlement_does_not_swallow_paid_time
```

去掉锁则该用例失败 → 它确实在测锁，不是摆设。随后已还原。

用例本身用 `threading.Barrier` 把竞态窗口从几微秒放大到可复现：barrier 在**修好之后必定超时**
（两个线程被锁串行化了，第二个进不到临界区），所以「barrier 能凑齐」本身就是锁失效的证据。

### 12.7.4 Round-2 复审后就地修掉的 3 条 P3

| 文件 | 改动 |
|---|---|
| `gateway/saas.py` | 新增 `_unfulfillable()`：套餐下架导致永远激活不了的 pending 单置 `failed` + `logger.error` 告警，回调返回 `{"ok": false}` 而非 500 —— 否则支付网关会对一笔谁也救不回的单无限重投 |
| `utils/tiers.py` | `user_usage_total` 的异常吞没升为 `logger.warning`（保留 fail-open，但不再查无实据） |
| `utils/entitlements.py` | `subscription_expiry` 对齐 `strict=True`，避免后来者误用非 strict 读做「还能用到几号」的判断 |

新增用例 `test_delisted_plan_order_stops_retrying_instead_of_looping` 钉死第一条。

### 12.7.5 交接给号池 Agent（界外，本轮未动）

Kimi 复审确认这是唯一的灰色地带，且**与改动前行为等价、无回归**：

- `chatgpt/authorization.py::_seed_plan_types` 裸 `except Exception` 会把 `StoreError` 吞成
  fail-open → DB 故障时「请求闸 503、选号 fail-open」姿态不一致。
- `chatgpt/authorization.py:44` `if not tier_id: return None` 用真值判断混淆了
  `""`（无权益，应拒）与 `None`（运营者，不设限），过期用户仍会在 `/api/auth/session` 被绑到号池账号。

### 12.7.6 Kimi 的最终裁定

> **无重大问题。** 九条修复全部真实落地、修在正确的层，测试声明与实际运行结果一致。
> 本轮收工判据满足。

---


# 13. 批次 A（安全 4 项）证据

评审闭环记于 `KIMI_REVIEW.md` 第三轮；验收判据记于 `EVALUATOR.md` 批次 A 段。

## 13.1 测试实跑输出

```
$ .venv/bin/python -m pytest tests/
124 passed

$ .venv/bin/python -m pytest tests_e2e/
148 passed

$ .venv/bin/python -m pytest tests_e2e/test_security_batch_a.py
29 passed
```

两套件**必须分开跑**（两个 conftest 在 import 时各自 `chdir`）。
批次 A 前的基线：`tests/` 109、`tests_e2e/` 119。

## 13.2 变异验证（9 项，逐个把修复改回去看测试是否变红）

这一节是本批次证据的核心：Round 1 中 Kimi **实测证伪**了我自己写的一条测试
（灌 45 个 key < `_MAX_KEYS=50`，淘汰路径根本没跑，断言恒真），所以此后每条
安全性质都做了反向验证 —— 不能只看「测试绿」。

| # | 把什么改回去 | 对应测试 | 结果 |
|---|---|---|---|
| M1 | `_client_ip` 改回取最左段 | `test_xff_walks_right_to_left_not_leftmost`、`test_xff_peels_every_trusted_tier` | FAILED（2 例） |
| M2 | 注册的 `upsert_user_auth` 去掉 `strict` | `test_register_reports_db_failure_...` | FAILED |
| M3 | 重置密码后不清账号桶 | `test_locked_out_user_can_still_recover_by_email` | FAILED |
| M4 | 查无此人时直接返回，不跑 dummy hash | `test_unknown_email_still_runs_a_hash` | FAILED |
| M5 | 去掉 forgot-password 的账号级冷却 | `test_forgot_password_has_a_per_email_cooldown` | FAILED |
| M6 | 采纳「密码对就放行」 | `test_signin_email_bucket_blocks_credential_stuffing` | FAILED |
| M7 | 登录成功不清桶 | `test_successful_signin_clears_the_account_bucket` | FAILED |
| M8 | 重置密码的 upsert 去掉 `strict` | `test_reset_password_reports_db_failure_...` | FAILED |
| M9 | 冷却窗口写死 3600（与 token TTL 脱钩） | `test_forgot_cooldown_never_outlives_the_token` | FAILED |

另有 `tests/test_ratelimit.py` 的淘汰键变异（改回「最久未命中」→
`test_flood_does_not_erase_an_active_bucket` FAILED）。

**M8 值得单独记一笔**：首次跑这条变异时**没有**任何测试变红 —— 说明当时那条修复
是「无人看守」的。补了 `test_reset_password_reports_db_failure_instead_of_faking_success`
之后重跑变异才捕获。这正是变异验证的用处：绿灯不代表被锁住。

每次变异后都从备份还原并 `diff -q` 确认文件与改前完全一致，再继续下一项。

## 13.3 高风险变更告知

### 13.3.1 数据库 schema 变更（CLAUDE.md 高风险项）

新增列 `user_auth.pw_version INTEGER NOT NULL DEFAULT 1`：

- `CREATE TABLE`（`utils/store.py:148`）与 `_ADDED_COLUMNS`（`:192`）**双写**，老库启动时自动补列，无需手工迁移。
- 该列**故意不在** `_USER_AUTH_COLUMNS` 白名单里 —— 只能经 `bump_pw_version()` 递增，
  不能被 `upsert_user_auth` 的任意字段路径写到，避免哪天有人不小心把它写回 1 而使所有吊销失效。
- 回滚：删列即可（旧代码不读该列）；回滚后 A2 的吊销能力随之消失。

### 13.3.2 生产部署行为变更（必须配合改配置）

A4 之后，`X-Forwarded-For` **只在 TCP 对端属于 `USER_TRUSTED_PROXIES` 时才被采信**。

- 走 Nginx / Cloudflare 却**没配**这个白名单 → 所有用户会共用反代出口 IP 的同一个限流桶，
  表现为**集体 429**。这是刻意选的失败方向：吵闹但五分钟内就会被发现并修好，
  远好过「限流静默失效」那种无声无息的失败。
- 多级反代（CF → Nginx → app）要把**每一级**的出口 IP / 网段都写进去，
  漏写中间某一级会让逐跳剥离提前停下。
- `.env.example` 已写明该规则与理由。

### 13.3.3 未实现、已转交接

seed 轮换（`user_auth.seed` 终身不变、泄露后无补救）需要号池线提供 `rebind_seed` /
`drop_seed`（`globals.seed_map` 的语义与生命周期归号池线），SaaS 线单独做不成。
已登记为 `handoff.md` **线路 F**，含双方职责划分与交付标准。

## 13.4 超出原定 4 项的改动（评审过程中补的）

| 文件 | 改动 | 起因 |
|---|---|---|
| `gateway/saas.py::api_password` | `old` 字段复用登录账号桶 | 它是个密码预言机：借来的 cookie + 猜中原密码 → 改密吊销所有会话 → 真主人被锁在门外 |
| `utils/ratelimit.py` | 读路径不建条目、`_MAX_KEYS` 上限、按命中数淘汰、`_max_window` 自适应 | 限流 key 含用户提交的 email = 攻击者可控，限流器本身不能变成内存耗尽入口 |
| `utils/store.py::upsert_user_auth` | 新增 `strict=` | 静默失败会让用户拿到指向不存在账号的 cookie |
| `gateway/user.py` | `_DUMMY_HASH` | 「查无此人立刻返回」与「算 10 万轮」的耗时差就是免费的账号枚举接口 |

## 14. 批次 B：使用记录与订单状态证据

### 14.1 交付范围

- `utils/store.py` 新增 `query_seed_usage_daily`，在 SQLite 侧按本地日期与 kind 聚合，避免页面读取 500 条明细后静默少算。
- `utils/usage.py` 新增 `user_daily_usage`，在锁内快照未 flush 的 pending 并与落库聚合合并。
- `gateway/saas.py::_usage_rows` 将原始 kind 映射为公开标签，并按日期与标签再次折叠；`/usage` 保持最近 30 天窗口。
- `templates/usage.html` 使用记录表头改为「时间 / 类型 / 会话数」，订阅表的「套餐」表头保持不变。
- `/checkout` 展示本人订单的 pending、paid、expired、failed 状态；订单归属不匹配时页面与无订单页面一致。
- `tests_e2e/test_user_saas.py` 修正原始 303 与跟随后页面的测试契约，并新增 501 条聚合、未知类型折叠测试。

### 14.2 验证命令与实际结果

```text
.venv/bin/python -m pytest tests -o addopts='' -q
125 passed in 4.03s

.venv/bin/python -m pytest tests_e2e -o addopts='' -q
166 passed, 10 warnings in 62.75s

.venv/bin/python -m pytest tests_e2e/test_user_saas.py -o addopts='' -q
40 passed in 20.12s
```

10 条 warning 来自既有 `curl_cffi` 对 `__Secure-` cookie 的 `secure` 属性提示，不影响测试通过。

### 14.3 Kimi 改后复审

Kimi session：`session_28cdc7b7-1dd1-427b-a1ec-e611bd5c7f8b`。复审覆盖技术正确性、并发、权限、回归、边界，以及产品统计准确性、展示语义、订单流程。

结论：**PASS，无 P0/P1/P2**。

复审确认：

- `strftime(..., 'unixepoch', 'localtime')` 与 pending 侧 `time.localtime()` 时区一致。
- SQL 聚合没有 500 条明细限制，且同日未知 kind 会合并为一个「其他」行。
- pending 读取持锁，订单与使用记录均按当前用户身份隔离。
- 既有结算锁、mock 回调拒绝、下架套餐终态处理和回调幂等测试未回归。

### 14.4 遗留项与回滚

- P3：保留旧 `user_events` / `query_seed_usage` 接口；字段名 `plan` / `sessions` 与实际类型/次数语义略有遗留；flush 换出与落库之间有极短暂展示少算窗口。
- P3：`query_seed_usage_daily(since=0)` 会聚合全表，当前调用方固定传最近 30 天窗口。
- 待下一轮高风险确认：`checkout.html` 对 `failed` 订单仍与 `expired` 共用「重新下单」提示。该状态可能对应已付款但未发放的套餐，涉及支付补发流程，本轮未修改。
- 回滚方式：恢复 `gateway/saas.py::_usage_rows` 使用旧 `usage.user_events`，删除新调用方即可；新增 store/usage 接口可保留，不涉及 schema 迁移。

| `gateway/user.py` | forgot-password 账号级冷却 + 窗口跟随 token TTL | 只有 IP 桶时换 IP 即可邮件轰炸；窗口长于 TTL 会开出「已发送但无可用链接」的静默空窗 |
| `templates/signin.html`、`_theme.css` | 被踢设备的 `notice` 文案 | 莫名跳回登录页会被当 bug 报上来，而那正是安全措施生效的时刻 |
| `gateway/saas.py::_invite_code` | TODO 锚点 | 可预测邀请码在返利上线后 = 冒领接口；锚点留在代码里而非只在评审记录里 |

## 15. 统一登录后进入 Dashboard

### 15.1 产品决策与路由契约

- 注册成功和登录成功不再按订阅状态分流，统一以 303 进入 `/dashboard`。
- 已登录但从未购买套餐的用户停留在 Dashboard 空态；空态保留明确的 `/store` 入口。
- 有效 paid 订单继续逐订单生成独立镜像入口，并展示套餐与到期信息。
- 空态不渲染 `/?token=` 聊天入口；聊天能力仍由权益闸控制。
- 匿名访问 Dashboard 仍以 303 跳转 `/signin`。

### 15.2 实现范围

| 文件 | 改动 | 原因 |
|---|---|---|
| `gateway/user.py::_login_redirect` | 无条件 303 到 `/dashboard`，移除不再使用的 `plans` import | 登录/注册后的用户旅程保持单一入口 |
| `gateway/saas.py::dashboard_page` | 删除无订阅时 303 到 `/store` 的分支 | Dashboard 承载套餐卡片和未购买用户的空态 |
| `gateway/saas.py` 模块说明 | 更新 Dashboard 的职责描述 | 让路由行为与维护文档一致 |
| `tests_e2e/test_user_saas.py` | 新增登录无订阅、空态直访、匿名保护测试；收紧注册跳转断言 | 锁定统一落点、空态入口和 token 隔离契约 |

### 15.3 验证命令与实际结果

```text
.venv/bin/python -m pytest tests -o addopts='' -q
125 passed in 4.03s

.venv/bin/python -m pytest tests_e2e -o addopts='' -q
169 passed, 10 warnings in 64.20s

.venv/bin/python -m pytest tests_e2e/test_user_saas.py -q
43 passed
```

10 条 warning 为既有 `curl_cffi` 对 `__Secure-` cookie 的 `secure` 属性提示，不影响断言结果。

### 15.4 Kimi 独立复审

改前 brief：`KIMI_BRIEF_DASHBOARD_FLOW.md`；改后复审 session：`session_cdde6e7b-bdd1-482e-b721-8440625a0700`。

结论：**PASS，无 P0/P1/P2**。复审确认以下行为同时成立：

- 注册和登录的最终页面均为 `/dashboard`；无订阅用户直接访问该页返回 200 空态。
- 空态存在 `/store` 入口且不存在 `/?token=`；匿名访问仍跳转 `/signin`。
- 多个 paid 订单仍逐订单生成独立入口，既有聊天入口和权益闸未受影响。

### 15.5 产品观察与回滚

- 仅持有过期订单的用户仍看到续费卡而非从未购买的空态。这符合续费转化目标，但后续需明确产品文案中“订阅”是指历史订单还是当前有效权益。
- 本版空态只提供 Store 入口。公告、推荐、用量摘要、余额和新手引导暂不加入，待用户研究或数据验证后逐项决策。
- 回滚：恢复 `gateway/user.py::_login_redirect` 的订阅状态分流，并在 `dashboard_page` 恢复空订阅跳转 `/store`；本轮不涉及 schema、支付结算或权益闸变更。
