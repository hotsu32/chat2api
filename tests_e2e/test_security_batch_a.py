"""批次 A 的 4 项安全修复的回归锁。

每条都锁「正确行为」，并尽量顺带锁住「修复前的那个具体漏洞不会回来」：

A1  dev_link 只认显式开关，不再因为 SMTP 没配就把重置链接渲染到页面上（账号接管）
A2  改密 / 重置密码吊销此前签发的全部会话（改密救得回被盗的号）
A3  /signin 失败限流：IP 桶 + 账号桶，两个都要有
A4  X-Forwarded-For 只在 TCP 对端属于可信反代白名单时才采信
"""
import urllib.parse

import utils.configs as configs
import utils.ratelimit as ratelimit
import utils.store as store
from gateway import user as user_mod


def _csrf(client, path):
    client.get(path)
    return client.cookies.get(configs.user_csrf_cookie) or ""


def _register(client, email, password="password123"):
    csrf = _csrf(client, "/register")
    return client.post(
        "/register", data={"email": email, "password": password, "csrf_token": csrf}
    )


def _signin(client, email, password):
    csrf = _csrf(client, "/signin")
    return client.post(
        "/signin", data={"email": email, "password": password, "csrf_token": csrf}
    )


def _settings_status(client):
    """``/settings`` 的**原始**状态码。

    必须关掉自动跳转：会话失效时页面路由是 303 -> /signin，跟着跳最后拿到的是登录页的
    200，看起来像「还登着」，断言就永远不会失败。
    """
    return client.get("/settings", follow_redirects=False).status_code


def _assert_logged_out(client):
    assert _settings_status(client) in (302, 303, 401)


def _assert_logged_in(client):
    assert _settings_status(client) == 200


# ---------------------------------------------------------------------------
# A1 — dev_link 账号接管
# ---------------------------------------------------------------------------

def test_forgot_password_hides_reset_link_by_default(client):
    """SMTP 未配置（本套件即如此）时，页面**不得**出现重置链接。

    修复前的逻辑是「没配 SMTP 就把链接印在页面上」，于是任何人只要知道一个已注册邮箱，
    提交一次 /forgot-password 就能在返回的 HTML 里读到该账号的重置链接 —— 无需碰邮箱、
    无需偷 cookie，直接接管账号。而「SMTP 没配」在生产上也可能是配错或挂掉造成的。
    """
    assert configs.smtp_configured() is False   # 前提：正是老逻辑会触发的那个条件
    assert configs.user_debug_links is False
    _register(client, "a1@example.com")
    csrf = _csrf(client, "/forgot-password")
    resp = client.post("/forgot-password", data={"email": "a1@example.com", "csrf_token": csrf})
    assert resp.status_code == 200
    assert b"/reset-password?token=" not in resp.content


def test_forgot_password_shows_link_when_debug_flag_on(client, monkeypatch):
    """显式打开 USER_DEBUG_LINKS 时才给链接（本地开发的逃生口仍在）。"""
    _register(client, "a1b@example.com")
    monkeypatch.setattr(configs, "user_debug_links", True)
    csrf = _csrf(client, "/forgot-password")
    resp = client.post("/forgot-password", data={"email": "a1b@example.com", "csrf_token": csrf})
    assert b"/reset-password?token=" in resp.content


def test_register_hides_verify_link_by_default(client, monkeypatch):
    """注册验证信同理：开关关着就不给链接。"""
    monkeypatch.setattr(configs, "require_email_verification", True)
    resp = _register(client, "a1c@example.com")
    assert resp.status_code == 200
    assert b"/verify-email?token=" not in resp.content


# ---------------------------------------------------------------------------
# A2 — 改密吊销会话
# ---------------------------------------------------------------------------

def test_password_change_revokes_other_devices(client_factory):
    """B 设备改密后，A 设备手里的旧 cookie 立刻失效。

    修复前：session token 的 payload 只有 {email, exp}，没有任何可失效的凭据，
    号被盗后改密码也救不回来 —— 攻击者的 cookie 在有效期（默认 8h）内照常能用。
    """
    device_a = client_factory()
    _register(device_a, "a2@example.com")
    _assert_logged_in(device_a)

    device_b = client_factory()
    _signin(device_b, "a2@example.com", "password123")
    csrf = device_b.cookies.get(configs.user_csrf_cookie) or ""
    resp = device_b.post(
        "/api/password",
        data={"old": "password123", "new": "newpassword456", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # A 设备（旧 cookie）被踢下线
    _assert_logged_out(device_a)
    # B 设备（操作者本人）仍在线 —— 它刚证明了自己知道旧密码，没理由被自己踢掉
    _assert_logged_in(device_b)


def test_revoked_device_is_told_why(client_factory):
    """被踢的设备要能看到原因，而不只是莫名跳回登录页。

    没有这句解释，用户在另一台设备上看到的就是「好端端地被登出了」—— 像 bug，
    会当故障报上来；而这恰恰是安全措施生效的时刻。
    """
    device_a = client_factory()
    _register(device_a, "a2c@example.com")

    device_b = client_factory()
    _signin(device_b, "a2c@example.com", "password123")
    csrf = device_b.cookies.get(configs.user_csrf_cookie) or ""
    device_b.post(
        "/api/password",
        data={"old": "password123", "new": "newpassword456", "csrf_token": csrf},
        follow_redirects=False,
    )

    resp = device_a.get("/settings", follow_redirects=False)
    assert resp.status_code == 303
    assert "reason=pw_changed" in resp.headers["location"]
    # 登录页据此渲染解释文案
    page = device_a.get("/signin?reason=pw_changed")
    assert "密码已变更" in page.text


def test_plain_logged_out_user_sees_no_revocation_notice(client):
    """没登录过的人不该看到「你的登录状态已失效」—— 那会让人以为账号出过事。"""
    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 303
    assert "reason=" not in resp.headers["location"]
    assert "密码已变更" not in client.get("/signin").text


def test_password_change_bumps_version(client):
    _register(client, "a2b@example.com")
    assert store.get_user_auth("a2b@example.com")["pw_version"] == 1
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    client.post(
        "/api/password",
        data={"old": "password123", "new": "newpassword456", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert store.get_user_auth("a2b@example.com")["pw_version"] == 2


def test_failed_password_change_does_not_revoke(client):
    """旧密码填错 → 不改密码，也就不该吊销任何会话（否则成了免费的登出炸弹）。"""
    _register(client, "a2c@example.com")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    client.post(
        "/api/password",
        data={"old": "wrong-password", "new": "newpassword456", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert store.get_user_auth("a2c@example.com")["pw_version"] == 1
    _assert_logged_in(client)


def test_reset_password_revokes_sessions(client_factory, monkeypatch):
    """「忘记密码」的常见起因就是号被盗 —— 重置后攻击者手里的会话必须失效。"""
    monkeypatch.setattr(configs, "user_debug_links", True)
    victim = client_factory()
    _register(victim, "a2d@example.com")
    attacker = client_factory()          # 模拟被盗的会话
    _signin(attacker, "a2d@example.com", "password123")
    _assert_logged_in(attacker)

    csrf = _csrf(victim, "/forgot-password")
    resp = victim.post("/forgot-password", data={"email": "a2d@example.com", "csrf_token": csrf})
    token = resp.content.decode().split("/reset-password?token=")[1].split('"')[0]

    csrf = _csrf(victim, f"/reset-password?token={token}")
    victim.post(
        "/reset-password",
        data={"token": token, "password": "brandnewpass789", "csrf_token": csrf},
    )
    assert store.get_user_auth("a2d@example.com")["pw_version"] == 2
    _assert_logged_out(attacker)


def test_legacy_token_without_pwv_still_accepted(client):
    """升级前签发的 token（payload 无 pwv）按版本 1 认，避免上线瞬间把所有人踢下线。

    它们最长 user_session_max_age（默认 8h）内自然消亡，窗口有界。
    """
    import base64
    import json
    import time

    _register(client, "a2e@example.com")
    payload = {"email": "a2e@example.com", "exp": int(time.time()) + 3600}  # 无 pwv
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    legacy = f"{data}.{user_mod._sign(data)}"
    assert user_mod._verify_session(legacy) == ("a2e@example.com", 1)

    client.cookies.set(configs.user_session_cookie, legacy)
    _assert_logged_in(client)


# ---------------------------------------------------------------------------
# A3 — /signin 限流
# ---------------------------------------------------------------------------

def test_signin_email_bucket_blocks_credential_stuffing(client_factory, monkeypatch):
    """同一账号连续试错，换 IP 也绕不过 —— 账号桶不看 IP。"""
    monkeypatch.setattr(configs, "signin_email_rate_limit", 3)
    monkeypatch.setattr(configs, "signin_ip_rate_limit", 0)   # 只验账号桶
    ratelimit.reset()
    c = client_factory()
    _register(c, "a3@example.com")

    for _ in range(3):
        assert _signin(c, "a3@example.com", "wrong").status_code == 401
    # 第 4 次：账号桶已满
    assert _signin(c, "a3@example.com", "wrong").status_code == 429
    # 换一台设备（不同 client）仍然被挡：账号桶与 IP 无关
    assert _signin(client_factory(), "a3@example.com", "wrong").status_code == 429
    # 密码正确也照样挡住 —— 否则攻击者无限次提交、撞对的那次照样放行，
    # 账号桶就只是把错误码从 401 换成 429，撞库防护等于取消。
    # 代价（正主也被挡）由下面两条逃生口化解。
    assert _signin(client_factory(), "a3@example.com", "password123").status_code == 429


def test_locked_out_user_can_still_recover_by_email(client_factory, monkeypatch):
    """账号桶被灌满的人必须有一条攻击者关不掉的回家路。

    账号桶连正确密码也挡，于是「知道你邮箱的人每 15 分钟发 5 次错密码」就能把你
    永久锁在门外 —— 除非重置密码这条路不受该桶约束，且重置成功会清掉计数。
    攻击者控制不了受害者的邮箱，所以这条路他关不掉。
    """
    monkeypatch.setattr(configs, "signin_email_rate_limit", 3)
    monkeypatch.setattr(configs, "signin_ip_rate_limit", 0)
    monkeypatch.setattr(configs, "user_debug_links", True)    # 取重置链接
    ratelimit.reset()
    victim = "a3lock@example.com"
    _register(client_factory(), victim)

    attacker = client_factory()
    for _ in range(4):
        _signin(attacker, victim, "wrong")
    assert _signin(client_factory(), victim, "password123").status_code == 429

    # 受害者走邮箱重置：该路径不受账号桶约束
    c = client_factory()
    csrf = _csrf(c, "/forgot-password")
    page = c.post("/forgot-password", data={"email": victim, "csrf_token": csrf}).text
    token = page.split("/reset-password?token=")[1].split('"')[0]
    csrf = _csrf(c, f"/reset-password?token={token}")
    resp = c.post(
        "/reset-password",
        data={"token": token, "password": "brandnewpass1", "csrf_token": csrf},
    )
    assert resp.status_code == 200

    # 重置成功 → 计数已清 → 立刻就能用新密码登录，不用等 15 分钟
    assert _signin(client_factory(), victim, "brandnewpass1").status_code == 200


def test_successful_signin_clears_the_account_bucket(client_factory, monkeypatch):
    """本人几次笔误不该累积：登录成功即清桶。

    不清的话，自己打错 4 次 + 攻击者补 1 次就被锁 15 分钟，太脆。
    """
    monkeypatch.setattr(configs, "signin_email_rate_limit", 3)
    monkeypatch.setattr(configs, "signin_ip_rate_limit", 0)
    ratelimit.reset()
    email = "a3f@example.com"
    _register(client_factory(), email)

    for _ in range(2):
        assert _signin(client_factory(), email, "typo").status_code == 401
    assert _signin(client_factory(), email, "password123").status_code == 200
    # 桶已清零：又能再错 3 次才触发限流，而不是只剩 1 次
    for _ in range(3):
        assert _signin(client_factory(), email, "typo").status_code == 401
    assert _signin(client_factory(), email, "typo").status_code == 429


def test_unknown_email_still_runs_a_hash(client, monkeypatch):
    """账号不存在也要跑一次 PBKDF2，否则耗时差本身就是账号枚举接口。"""
    calls = []
    real = user_mod.verify_password
    monkeypatch.setattr(user_mod, "verify_password",
                        lambda pw, stored: calls.append(stored) or real(pw, stored))
    _signin(client, "nobody-here@example.com", "whatever")
    assert len(calls) == 1, "查无此人时直接返回了，没有跑哈希"


def test_forgot_password_has_a_per_email_cooldown(client_factory, monkeypatch):
    """只有 IP 桶的话，攻击者换一批 IP 就能对着受害者的邮箱狂发重置信。

    前提显式写出：这里量到的**纯粹是账号桶**，forgot 的 IP 桶（复用
    ``register_rate_limit``）被主动关掉了。**不要**以为换 ``client_factory()``
    就换了 IP —— TestClient 的对端恒为 ``testclient``，换的只是 cookie jar。
    """
    monkeypatch.setattr(configs, "register_rate_limit", 0)    # 关掉 IP 桶，别依赖 conftest 的默认值
    monkeypatch.setattr(configs, "user_debug_links", True)
    ratelimit.reset()
    victim = "a3mail@example.com"
    _register(client_factory(), victim)

    seen = []
    for _ in range(user_mod._FORGOT_EMAIL_LIMIT + 2):
        c = client_factory()
        csrf = _csrf(c, "/forgot-password")
        resp = c.post("/forgot-password", data={"email": victim, "csrf_token": csrf})
        seen.append(b"/reset-password?token=" in resp.content)
    assert sum(seen) == user_mod._FORGOT_EMAIL_LIMIT
    # 超限后仍回「已发送」页而不是 429：回 429 等于确认该邮箱存在且刚被请求过，
    # 把「不暴露邮箱是否注册」的口径拆穿了
    assert resp.status_code == 200


def test_forgot_cooldown_never_outlives_the_token(monkeypatch):
    """冷却窗口不得长于 token 有效期，否则开出一个「静默空窗」。

    脱钩时：攻击者 t=0 灌满 3 格 → token 在 TTL 后全部过期 → 但槽位还占着；
    此后受害者自己点「忘记密码」会看到「已发送」，收件箱里却没有可用链接 ——
    而这条路正是他被登录限流挡住时唯一的出口。
    """
    for ttl in (600, 1800, 7200):
        monkeypatch.setattr(configs, "email_token_ttl", ttl)
        assert user_mod._forgot_email_window() <= max(ttl, 300)
    # 反向：TTL 配得极短也不能退化成「几乎不限流」，那就成了邮件轰炸口
    monkeypatch.setattr(configs, "email_token_ttl", 10)
    assert user_mod._forgot_email_window() >= 300


def test_signin_ip_bucket_blocks_account_sweeping(client, monkeypatch):
    """同一来源横扫多个账号 —— 账号桶各自独立，只有 IP 桶能挡。"""
    monkeypatch.setattr(configs, "signin_ip_rate_limit", 3)
    monkeypatch.setattr(configs, "signin_email_rate_limit", 0)  # 只验 IP 桶
    ratelimit.reset()
    for i in range(3):
        assert _signin(client, f"victim{i}@example.com", "wrong").status_code == 401
    assert _signin(client, "victim9@example.com", "wrong").status_code == 429


def test_successful_signin_does_not_consume_quota(client_factory, monkeypatch):
    """登录成功不计数 —— 正常用户反复登录不该把自己锁在门外。"""
    monkeypatch.setattr(configs, "signin_email_rate_limit", 2)
    monkeypatch.setattr(configs, "signin_ip_rate_limit", 0)
    ratelimit.reset()
    c = client_factory()
    _register(c, "a3c@example.com")
    for _ in range(5):
        assert _signin(client_factory(), "a3c@example.com", "password123").status_code == 200


def test_signin_rate_limit_disabled_by_zero(client, monkeypatch):
    monkeypatch.setattr(configs, "signin_ip_rate_limit", 0)
    monkeypatch.setattr(configs, "signin_email_rate_limit", 0)
    ratelimit.reset()
    _register(client, "a3d@example.com")
    for _ in range(8):
        assert _signin(client, "a3d@example.com", "wrong").status_code == 401


def test_change_password_old_field_is_throttled(client, monkeypatch):
    """/api/password 的 old 字段是个密码预言机 —— 也必须限流。

    它虽然要求已登录，但猜中原密码就能改密，而改密会吊销所有会话：
    一张借来的 cookie 就此升级成账号所有权，真主人被锁在门外。
    """
    monkeypatch.setattr(configs, "signin_email_rate_limit", 3)
    ratelimit.reset()
    _register(client, "a3e@example.com")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""

    def attempt(old):
        loc = client.post(
            "/api/password",
            data={"old": old, "new": "newpassword456", "csrf_token": csrf},
            follow_redirects=False,
        ).headers.get("location", "")
        return urllib.parse.unquote(loc)   # pw_msg 是 URL 编码的中文

    for _ in range(3):
        assert "当前密码不正确" in attempt("guess")
    assert "尝试过于频繁" in attempt("guess")
    # 桶满之后，即使猜对了也改不成 —— 否则限流对「最后一次刚好猜中」毫无作用
    assert "尝试过于频繁" in attempt("password123")
    assert store.get_user_auth("a3e@example.com")["pw_version"] == 1


# ---------------------------------------------------------------------------
# A4 — XFF 信任白名单
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, peer, xff=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}


def test_xff_ignored_when_no_trusted_proxy_configured(monkeypatch):
    """默认（白名单为空）：谁的 XFF 都不信。

    修复前 ``_client_ip`` 无条件取 XFF 首段，于是攻击者每次请求换一个假 IP，
    注册 / 找回密码的限流就等于不存在 —— 一行 header 即可清零。
    """
    monkeypatch.setattr(configs, "user_trusted_proxies", [])
    assert user_mod._client_ip(_FakeRequest("203.0.113.9", "1.2.3.4")) == "203.0.113.9"


def test_xff_honored_only_from_trusted_peer(monkeypatch):
    monkeypatch.setattr(configs, "user_trusted_proxies", ["10.0.0.5"])
    # 反代本人转发 → 采信
    assert user_mod._client_ip(_FakeRequest("10.0.0.5", "1.2.3.4")) == "1.2.3.4"
    # 任意外部 IP 伪造同样的头 → 不采信
    assert user_mod._client_ip(_FakeRequest("203.0.113.9", "1.2.3.4")) == "203.0.113.9"


def test_xff_trusted_proxy_supports_cidr(monkeypatch):
    monkeypatch.setattr(configs, "user_trusted_proxies", ["10.0.0.0/8"])
    assert user_mod._client_ip(_FakeRequest("10.1.2.3", "1.2.3.4")) == "1.2.3.4"
    assert user_mod._client_ip(_FakeRequest("11.1.2.3", "1.2.3.4")) == "11.1.2.3"


def test_xff_walks_right_to_left_not_leftmost(monkeypatch):
    """采信 XFF 时必须从右往左剥可信跳，**不能**直接取最左段。

    Cloudflare 和 Nginx 的 ``$proxy_add_x_forwarded_for`` 都是**追加**而非覆盖：
    客户端自带 ``XFF: 1.2.3.4`` 时 app 看到的是 ``1.2.3.4, <真实IP>``，
    取最左拿到的正好是攻击者手写的那一段 —— 白名单配好了，限流照样能被一行 header 清零。
    右侧是各级反代自己追加的、伪造不了的部分。
    """
    monkeypatch.setattr(configs, "user_trusted_proxies", ["10.0.0.5"])
    # 攻击者伪造左侧 + 反代在右侧追加真实 IP → 必须拿到右边那个
    assert user_mod._client_ip(
        _FakeRequest("10.0.0.5", "1.2.3.4, 203.0.113.77")) == "203.0.113.77"
    # 干净的单跳链路（反代未透传客户端伪造值）
    assert user_mod._client_ip(_FakeRequest("10.0.0.5", "203.0.113.77")) == "203.0.113.77"


def test_xff_peels_every_trusted_tier(monkeypatch):
    """多级反代（CF -> Nginx -> app）：每一级出口都在白名单里时逐跳剥到真实客户端。"""
    monkeypatch.setattr(configs, "user_trusted_proxies", ["10.0.0.5", "172.16.0.0/12"])
    assert user_mod._client_ip(
        _FakeRequest("10.0.0.5", "203.0.113.77, 172.16.3.9")) == "203.0.113.77"
    # 整条链都是可信反代 → 没有比对端更靠谱的信息，退回对端而不是返回可信反代的 IP
    assert user_mod._client_ip(
        _FakeRequest("10.0.0.5", "172.16.3.9, 10.0.0.5")) == "10.0.0.5"


def test_xff_tolerates_junk(monkeypatch):
    monkeypatch.setattr(configs, "user_trusted_proxies", ["10.0.0.5"])
    # 空 XFF → 退回对端，不返回空串（空串会让所有人共用同一个限流桶）
    assert user_mod._client_ip(_FakeRequest("10.0.0.5", "   ")) == "10.0.0.5"
    # 规则本身写错不应炸，只是不匹配
    monkeypatch.setattr(configs, "user_trusted_proxies", ["not-an-ip"])
    assert user_mod._client_ip(_FakeRequest("10.0.0.5", "1.2.3.4")) == "10.0.0.5"


def test_register_rate_limit_not_bypassable_via_xff(client, monkeypatch):
    """端到端：伪造 XFF 不能重置注册限流。"""
    monkeypatch.setattr(configs, "user_trusted_proxies", [])
    monkeypatch.setattr(configs, "register_rate_limit", 2)
    ratelimit.reset()
    for i in range(2):
        assert _register(client, f"a4{i}@example.com").status_code in (200, 303)
    csrf = _csrf(client, "/register")
    resp = client.post(
        "/register",
        data={"email": "a4x@example.com", "password": "password123", "csrf_token": csrf},
        headers={"X-Forwarded-For": "9.9.9.9"},
    )
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# 注册写库失败必须显式失败（Kimi 复审 P2）
# ---------------------------------------------------------------------------

def test_register_reports_db_failure_instead_of_faking_success(client, monkeypatch):
    """写库失败不能静默放过。

    静默失败时用户会拿到一张「查无此人」的会话 cookie：注册页看起来成功了，
    之后每个页面都 401 且没有任何解释，用户完全无从判断自己到底有没有注册成功。
    """
    def boom(email, **fields):
        raise store.StoreError("disk full")

    monkeypatch.setattr(store, "create_user_with_trial", boom)
    resp = _register(client, "a5@example.com")
    assert resp.status_code == 503
    assert "稍后重试" in resp.text
    # 没有发出会话 cookie —— 否则就是「拿着一张指向不存在账号的票」
    assert not client.cookies.get(configs.user_session_cookie)


def test_reset_password_reports_db_failure_instead_of_faking_success(client, monkeypatch):
    """重置页说「已完成」就必须真的改了密码。

    静默失败时：页面显示成功 → 用户拿新密码登录 401、旧密码却还能用。
    比直接报错难排查得多，而且发生在刚证明了邮箱所有权的人身上。
    """
    monkeypatch.setattr(configs, "user_debug_links", True)
    _register(client, "a5b@example.com")
    csrf = _csrf(client, "/forgot-password")
    page = client.post(
        "/forgot-password", data={"email": "a5b@example.com", "csrf_token": csrf}).text
    token = page.split("/reset-password?token=")[1].split('"')[0]

    def boom(email, **fields):
        raise store.StoreError("disk full")

    monkeypatch.setattr(store, "upsert_user_auth", boom)   # 注册之后才打桩
    csrf = _csrf(client, f"/reset-password?token={token}")
    resp = client.post(
        "/reset-password",
        data={"token": token, "password": "brandnewpass2", "csrf_token": csrf},
    )
    assert resp.status_code == 500
    assert "密码未能保存" in resp.text
    monkeypatch.undo()
    # 新密码没生效（本来就没写进去），旧密码仍然可用 —— 用户没被锁死，但页面绝不能说「成功」
    assert _signin(client, "a5b@example.com", "brandnewpass2").status_code == 401
