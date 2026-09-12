# Kimi 复审 brief — 批次 A（安全）Round 2

你在 Round 1 审过这批改动，提了 P1/P2/P3。这一轮是**针对你的意见做完修改后的复审**。
请重点看：我的修法对不对、有没有引入新问题、有没有哪条我判断错了。

工作目录 `/Users/Zhuanz/chatgpt-mirror/chat2api`。**我在你审阅期间不改任何文件。**

---

## 一、Round 1 你提的问题 → 我怎么处理的

### P1-1 账号桶连正确密码也挡 = 可被用来锁死任意用户（你判定引入新 DoS）

**你的建议**：先验密码，密码正确就放行，只对失败计数。

**我没有采纳，理由如下，请你重点复核这个判断：**

「密码对就放行」等于**取消账号桶**。攻击者可以无限次提交，撞对的那一次照样放行，
桶只是把失败的错误码从 401 换成 429 —— 而撞库防护的全部价值恰恰在于让他到不了
那一次。这不是加固，是把锁换成了门铃。

我做的是保留「连正确密码一起挡」，但补两条逃生口，把 DoS 代价降到可接受：

1. 登录成功立刻 `ratelimit.reset(_signin_email_key(email))` —— 本人笔误不累积到下一次；
2. 「忘记密码」走邮箱令牌，**不受该桶约束**，且重置成功也清桶 ——
   攻击者控制不了受害者的邮箱，所以这条回家路他关不掉；
3. 429 文案 `_THROTTLE_MSG` 明确指路「忘记密码」，否则被锁的人以为自己永久失联。

代码：`gateway/user.py` 的 `/signin` POST；`reset_password` 尾部的 `ratelimit.reset`。
测试：`tests_e2e/test_security_batch_a.py::test_locked_out_user_can_still_recover_by_email`

**请你判断**：这个取舍成立吗？还有没有我没看到的锁死路径（比如攻击者能不能同时
把「忘记密码」也堵死）？`_FORGOT_EMAIL_LIMIT=3/小时` 的账号级冷却会不会反而成了
攻击者堵住逃生口的工具 —— 他先把受害者的重置信配额灌满，受害者就同时进不去也重置不了？
这条我自己也不确定，是本次复审我最想要你意见的地方。

### P1-2 XFF 取最左段，在 Cloudflare / Nginx 追加语义下可被伪造

**已按你说的改**：`_client_ip` 现在从右往左逐跳剥掉可信条目，取剩下第一个。
`.env.example` 补了「每一级反代出口 IP 都要写」的说明。
测试：`test_xff_walks_right_to_left_not_leftmost`、`test_xff_peels_every_trusted_tier`。

### P1-3 你实测证伪了我的 `test_flood_does_not_erase_an_active_bucket`

**你是对的，我独立复现确认了**：45 个 key < `_MAX_KEYS=50`，淘汰路径根本没跑，
断言恒真；且当时按「最久未命中」淘汰时，受害者的桶**会**被洪水挤掉。

改了两处：淘汰键换成「命中次数最少，同数再看最久未命中」（噪声 key 各 1 次会先自相残杀）；
测试改成真的灌 200 个 key 过上限。并做了变异测试（把淘汰键改回去 → 新测试 FAIL）。

### P2 其余
- `upsert_user_auth` 加 `strict=True`，注册写库失败 → 503，不再静默发一张指向不存在账号的 cookie
- `ratelimit` 读路径不建条目 + `_MAX_KEYS` 上限 + `_max_window` 跟随实际配置（不写死 3600）
- 查无此人也跑一次 PBKDF2（`_DUMMY_HASH`），关掉账号枚举的时序预言机
- forgot-password 加账号级冷却，且超限**静默回成功页**而非 429（回 429 等于确认该邮箱存在）

### P3 未处理（请确认是否接受延后）
- `_is_https` 仍无条件信任 `x-forwarded-proto`，与 XFF 的白名单模型不一致
- `/verify-email` GET 有副作用（邮件客户端预取会消耗 token）
- `_invite_code = sha256(email)[:6]` 可预测（返利上线前必须换随机码）

---

## 二、请你复审的范围

改动文件：
- `gateway/user.py`（核心：`_client_ip` / `/signin` / `register` / `reset_password` / 限流 helper）
- `gateway/saas.py`（`_page_auth` / `api_password`）
- `utils/ratelimit.py`（内存有界 + 淘汰策略）
- `utils/store.py`（`upsert_user_auth(strict=)`、`bump_pw_version`、`pw_version` 列）
- `tests/test_ratelimit.py`、`tests_e2e/test_security_batch_a.py`
- `templates/signin.html`、`templates/_theme.css`、`.env.example`

## 三、我特别想让你攻击的点

1. **上面那条逃生口互锁问题**（攻击者同时灌满登录桶 + 重置信桶 → 受害者彻底出不去？）
2. **淘汰策略仍可被绕过吗**：攻击者若给每个噪声 key 都刷到 6 次（超过受害者的 5 次），
   是不是又能把受害者挤掉？`_MAX_KEYS=50_000` 下这个成本是多少？值不值得防？
3. **`ratelimit.reset` 在改密成功后清桶**（`saas.py::api_password`）：
   拿到一张偷来的 cookie 的人，能不能用「改密成功」来清掉自己刷出来的限流？
   （我的理解是不能，因为他得先知道旧密码 —— 但请你确认这个推理。）
4. `pw_version` 是 schema 变更，`_verify_session` 对缺 pwv 的老 token 按 1 认。
   这个 fallback 会不会被用来绕过吊销（伪造一个不带 pwv 的 payload）？
   （签名是 HMAC，我认为伪造不了，但这是安全关键路径，请独立确认。）
5. 任何「测试通过但其实没验到东西」的地方 —— 你上一轮抓到一个，我想知道还有没有。

## 四、测试现状

两套必须分开跑（两个 conftest 都在 import 时 chdir）：

    .venv/bin/python -m pytest tests/      →  124 passed
    .venv/bin/python -m pytest tests_e2e/  →  146 passed

批次 A 专项 27 例全绿，并对 7 个关键性质做了变异测试（逐个把修复改回去，
确认对应测试变红）：XFF 取最左 / 注册静默失败 / 重置不清桶 / 无 dummy hash /
无邮件冷却 / 密码对就放行 / 成功不清桶 —— 7 个变异全部被捕获。

## 五、边界（不要审这些）

镜像 / 号池 / antiban / `chatgpt/authorization.py` / `reverseProxy` 调度 /
`globals.seed_map` 语义 —— 由另一个 Agent 负责，不在我职责内。
seed 轮换已转为跨界交接单，本轮不实现。
