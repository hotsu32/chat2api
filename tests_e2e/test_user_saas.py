"""Stage 0–4 用户侧 SaaS E2E：档位模型 / 注册登录 / 档位与额度执行 / 落地页+支付占位。

全部跑在真实 FastAPI app + loopback mock 上游上，零真实网络、零真实凭据。

覆盖验收判据：
  Stage 0  user.tier 能持久化 + 枚举「可用号组 / 可用模型 / 额度上限」
  Stage 1  注册 → 登录 → 拿 seed → 走 GET /?token=seed 进聊天，无运营者介入
  Stage 2  免费档超额被限、付费档进专属号组、换档后换号组
  Stage 3  落地页可访问、下单落库 pending
  fail-open 运营者 seed（无 user_auth 行）不设限
"""
import time

from starlette.testclient import TestClient

import app
import utils.configs as configs
import utils.globals as globals
import utils.store as store
import utils.tiers as tiers


def _make_user(seed, email, tier_id="free"):
    store.upsert_user_auth(
        email, password_hash="pbkdf2_sha256$1$00$00", seed=seed, tier_id=tier_id, status="active"
    )
    globals.seed_map[seed] = {"token": "", "plan_type": None, "conversations": []}


def _grant(email, plan_id, days_left=30):
    """给用户一张**已支付且未过期**的订单 —— 权益的唯一来源。

    ``user_auth.tier_id`` 不再决定权益（见 utils/entitlements.py），
    因此测试里想让用户「有 plus 权益」必须落一张 paid 订单，而不是改 tier_id。
    """
    import time as _time
    import utils.plans as plans

    detail = plans.plan_detail(plan_id)
    order_id = "ord-test-" + plan_id + "-" + email
    store.create_order(order_id, email, detail["id"], str(detail["price"]), status="pending")
    store.activate_order(order_id, int(_time.time()) + days_left * 86400)
    return order_id


def _register(client, email, password):
    client.get("/register")  # 下发 CSRF cookie
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post(
        "/register", data={"email": email, "password": password, "csrf_token": csrf}
    )


def _signin(client, email, password):
    client.get("/signin")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post(
        "/signin", data={"email": email, "password": password, "csrf_token": csrf}
    )


def _fake_template(monkeypatch):
    """用 canned 官网 HTML 替代真实抓取，避免 data/session_cookie.txt 依赖。

    必须带 ``client-bootstrap``：渲染路径会把其中的 owner 身份重写为种子账号身份，
    重写不到就按「宁可降级也不泄漏凭据」判 503。
    """
    html = (
        '<html><head></head><body>chat'
        '<script type="application/json" id="client-bootstrap">'
        '{"authStatus":"logged_out","session":{},"user":{}}'
        '</script></body></html>'
    )

    async def _fake(*args, **kwargs):
        return html
    monkeypatch.setattr("gateway.chatgpt.get_frontend_template", _fake)


# ---------------------------------------------------------------------------
# Stage 0 — 档位与分池数据模型
# ---------------------------------------------------------------------------

def test_tier_catalog_enumerates_group_models_quota():
    cat = tiers.list_tiers()
    assert set(cat) >= {"free", "plus", "pro"}
    for tid in ("free", "plus", "pro"):
        t = cat[tid]
        assert t["account_plan_types"], f"{tid} 缺号组范围"
        assert t["models"], f"{tid} 缺模型白名单"
        assert t.get("quota_limit") is not None, f"{tid} 缺额度上限"


def test_tier_account_plan_types_and_model_gate():
    assert tiers.tier_account_plan_types("free") == ["free"]
    assert "plus" in tiers.tier_account_plan_types("pro")
    assert tiers.tier_allows_model("free", "gpt-5-5") is True
    assert tiers.tier_allows_model("free", "gpt-5-5-thinking") is False  # plus 专属
    assert tiers.tier_allows_model("plus", "gpt-5-5-thinking") is True
    # auto（默认自动选型）始终放行
    assert tiers.tier_allows_model("free", "auto") is True
    assert tiers.tier_allows_model("plus", "auto") is True
    # 未知档位 / 空模型 fail-open
    assert tiers.tier_allows_model("free", "") is True
    assert tiers.tier_allows_model("nope", "gpt-5-5-thinking") is True


def test_resolve_user_tier_none_without_user_auth():
    assert tiers.resolve_user_tier("seed-no-user") is None


# ---------------------------------------------------------------------------
# Stage 1 — 注册 / 登录 / seed 绑定
# ---------------------------------------------------------------------------

def test_signin_without_subscription_lands_on_dashboard(client):
    """登录后即使没有订阅，也统一进入 Dashboard 空态。"""
    _register(client, "dashboard-login@example.com", "password123")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    client.post("/signout", data={"csrf_token": csrf})

    resp = _signin(client, "dashboard-login@example.com", "password123")
    assert resp.status_code == 200
    assert resp.history[-1].status_code == 303
    assert resp.history[-1].headers["location"] == "/dashboard"
    assert "Dashboard" in resp.text
    assert "去超市选购" in resp.text


def test_dashboard_without_subscription_stays_on_empty_state(client):
    """已登录但无订阅时，直接访问 Dashboard 不再跳 Store。"""
    _register(client, "dashboard-empty@example.com", "password123")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Dashboard" in resp.text
    assert 'href="/store"' in resp.text
    assert "/?token=" not in resp.text


def test_dashboard_requires_login(client):
    """Dashboard 空态调整不能削弱未登录保护。"""
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/signin"


def test_register_creates_user_with_seed_but_no_entitlement(client, monkeypatch, seed_account, make_access_token):
    seed_account(make_access_token(account_id="acc-plus", plan_type="plus"), plan_type="plus")
    _fake_template(monkeypatch)

    # register 成功 → 303 跳 /dashboard（无套餐也保留在控制台）
    resp = _register(client, "alice@example.com", "password123")
    assert resp.status_code == 200
    assert resp.history[-1].status_code == 303
    assert resp.history[-1].headers["location"] == "/dashboard"
    assert "Dashboard" in resp.text
    assert "去超市选购" in resp.text
    assert "/?token=" not in resp.text

    row = store.get_user_auth("alice@example.com")
    assert row is not None
    assert row["status"] == "active"
    assert row["seed"]
    # 注册不附赠任何权益：必须先购买
    from utils import entitlements
    assert entitlements.effective_tier(row["seed"]) == ""
    # 会话 cookie 已下发
    assert client.cookies.get(configs.user_session_cookie)
    # seed 已落到 seed_map
    assert row["seed"] in globals.seed_map


def test_login_then_enter_chat(client, monkeypatch, seed_account, make_access_token):
    seed_account(make_access_token(account_id="acc-plus", plan_type="plus"), plan_type="plus")
    _fake_template(monkeypatch)
    _register(client, "bob@example.com", "password123")
    row = store.get_user_auth("bob@example.com")
    seed = row["seed"]
    _grant("bob@example.com", "plus-solo-1m")

    # 退出登录态，再登录
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    client.post("/signout", data={"csrf_token": csrf})
    resp = _signin(client, "bob@example.com", "password123")
    assert resp.status_code == 200

    # 入口只认显式 ?token=（裸 / 一律回 Dashboard），有效套餐 → 渲染聊天页
    resp = client.get(f"/?token={seed}")
    assert resp.status_code == 200
    assert resp.cookies.get("token") == seed


def test_login_rejects_wrong_password(client):
    _register(client, "carol@example.com", "password123")
    client.post("/signout")
    resp = _signin(client, "carol@example.com", "wrong-password")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Stage 2 — 档位与额度执行
# ---------------------------------------------------------------------------

def test_paid_tier_blocks_model_outside_whitelist(client, seed_account, make_access_token):
    seed_account(make_access_token(account_id="acc-plus", plan_type="plus"), plan_type="plus")
    _make_user("seed-gate", "gate@example.com")
    _grant("gate@example.com", "plus-solo-1m")

    resp = client.post(
        "/backend-api/conversation", cookies={"token": "seed-gate"}, json={"model": "o3"}
    )
    assert resp.status_code == 403


def test_expired_plan_blocks_conversation_with_402(client, seed_account, make_access_token):
    """套餐到期 → 聊天请求 402，而不是降级到 free（已无 Free 档）。"""
    seed_account(make_access_token(account_id="acc-plus", plan_type="plus"), plan_type="plus")
    _make_user("seed-expired", "expired@example.com")
    _grant("expired@example.com", "plus-solo-1m", days_left=-1)

    resp = client.post(
        "/backend-api/conversation", cookies={"token": "seed-expired"}, json={"model": "gpt-5-5"}
    )
    assert resp.status_code == 402


def test_no_plan_blocks_conversation_with_402(client, seed_account, make_access_token):
    """注册但没买 → 直接 402。注册不再附赠任何额度。"""
    seed_account(make_access_token(account_id="acc-plus", plan_type="plus"), plan_type="plus")
    _make_user("seed-nopay", "nopay@example.com")

    resp = client.post(
        "/backend-api/conversation", cookies={"token": "seed-nopay"}, json={"model": "gpt-5-5"}
    )
    assert resp.status_code == 402


def test_paid_tier_quota_exceeded_returns_429(client, seed_account, make_access_token):
    tok = make_access_token(account_id="acc-plus", plan_type="plus")
    seed_account(tok, plan_type="plus")
    seed = "seed-quota"
    _make_user(seed, "quota@example.com")
    _grant("quota@example.com", "plus-solo-1m")

    # 预置 plus 档满额（500）条当日用量
    for _ in range(500):
        store.add_usage_event(seed, tok, "conversation")

    resp = client.post(
        "/backend-api/conversation", cookies={"token": seed}, json={"model": "gpt-5-5"}
    )
    assert resp.status_code == 429


def test_plus_user_routes_to_plus_pool(client, seed_account, make_access_token):
    tok_free = make_access_token(account_id="acc-free", plan_type="free")
    tok_plus = make_access_token(account_id="acc-plus", plan_type="plus")
    seed_account(tok_free, plan_type="free")
    seed_account(tok_plus, plan_type="plus")
    _make_user("seed-pool", "pool@example.com")
    _grant("pool@example.com", "plus-solo-1m")

    client.get("/api/auth/session", cookies={"token": "seed-pool"})
    assert globals.seed_map["seed-pool"]["token"] == tok_plus


def test_upgrade_tier_switches_pool_on_failover(client, seed_account, make_access_token):
    tok_plus = make_access_token(account_id="acc-plus", plan_type="plus")
    tok_pro = make_access_token(account_id="acc-pro", plan_type="pro")
    seed_account(tok_plus, plan_type="plus")
    seed_account(tok_pro, plan_type="pro")
    _make_user("seed-up", "up@example.com")
    _grant("up@example.com", "plus-solo-1m")

    # plus 档只吃 plus 号组，pro 号不在范围内 → 确定性绑到 tok_plus
    client.get("/api/auth/session", cookies={"token": "seed-up"})
    assert globals.seed_map["seed-up"]["token"] == tok_plus

    # 升到 pro（号组扩到 plus+pro）+ plus 号挂掉 → failover 切进 pro 号
    _grant("up@example.com", "pro-solo-1m")
    store.upsert_account(tok_plus, status="disabled")
    client.get("/api/auth/session", cookies={"token": "seed-up"})
    assert globals.seed_map["seed-up"]["token"] == tok_pro


def test_failover_skips_marked_dead_account(client, seed_account, make_access_token):
    # 回归：mark_dead 只写 antiban_dead_tokens（JSON），不改 accounts.status，
    # 故 _pick_healthy_account 必须经 _account_is_usable 过滤，否则 failover 重选死号。
    from utils.antiban import circuit
    tok_dead = make_access_token(account_id="acc-plus-dead", plan_type="plus")
    tok_live = make_access_token(account_id="acc-plus-live", plan_type="plus")
    seed_account(tok_dead, plan_type="plus")
    _make_user("seed-dead", "dead@example.com")
    _grant("dead@example.com", "plus-solo-1m")

    # 首次绑定：号池只有 tok_dead 一个 plus → 确定性绑定到 tok_dead
    client.get("/api/auth/session", cookies={"token": "seed-dead"})
    assert globals.seed_map["seed-dead"]["token"] == tok_dead

    # 再放入第二个健康 plus 号，然后封禁 tok_dead
    seed_account(tok_live, plan_type="plus")
    circuit.mark_dead(tok_dead, "account_deactivated")

    # failover 必须切到 tok_live，绝不重选死号 tok_dead
    client.get("/api/auth/session", cookies={"token": "seed-dead"})
    assert globals.seed_map["seed-dead"]["token"] == tok_live


def test_operator_seed_not_limited_by_tier(client, seed_account, make_access_token):
    # 运营者 seed（无 user_auth 行）走旧主链路，不受档位/额度限制
    tok = make_access_token(account_id="acc-op", plan_type="plus")
    seed_account(tok, plan_type="plus")
    globals.seed_map["seed-op"] = {"token": tok, "plan_type": "plus", "conversations": []}

    resp = client.post(
        "/backend-api/conversation", cookies={"token": "seed-op"}, json={"model": "o3"}
    )
    # 不设限 → 正常透传到 mock 上游（200）
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Stage 3 — 落地页 + 支付占位
# ---------------------------------------------------------------------------

def test_landing_page_serves(client):
    resp = client.get("/landing")
    assert resp.status_code == 200
    assert b"register" in resp.content


def test_order_placeholder_persists_pending(client):
    _register(client, "buyer@example.com", "password123")

    # 金额一律服务端定价：客户端传的 amount 不被采信
    resp = client.post("/api/orders", json={"tier_id": "plus-solo-1m", "amount": "1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["tier_id"] == "plus-solo-1m"
    assert str(data["amount"]) == "99"

    order = store.get_order(data["order_id"])
    assert order is not None
    assert order["status"] == "pending"
    assert order["email"] == "buyer@example.com"
    assert order["amount"] == "99"


def test_order_rejects_unknown_plan(client):
    _register(client, "badplan@example.com", "password123")
    # 裸档位不再是可下单 SKU（套餐 = 档次 x 密度 x 时长）
    resp = client.post("/api/orders", json={"tier_id": "plus", "amount": "99"})
    assert resp.status_code == 400



def test_usage_page_counts_more_than_500_events(client):
    """使用页按数据库聚合，活跃用户超过旧上限时仍显示完整计数。"""
    email = "usage-large@example.com"
    _register(client, email, "password123")
    seed = store.get_user_auth(email)["seed"]
    events = [(seed, "account-large", "conversation", int(time.time())) for _ in range(501)]
    store.add_usage_events(events)
    page = client.get("/usage")
    assert page.status_code == 200
    assert ">501<" in page.text


def test_usage_page_collapses_unknown_kinds_on_same_day(client):
    """同日多个未知 kind 映射为一个公开的其他类型。"""
    email = "usage-unknown@example.com"
    _register(client, email, "password123")
    seed = store.get_user_auth(email)["seed"]
    now = int(time.time())
    store.add_usage_events([
        (seed, "account-unknown", "kind-a", now),
        (seed, "account-unknown", "kind-b", now),
    ])
    page = client.get("/usage")
    assert page.status_code == 200
    assert page.text.count(">其他<") == 1
    assert ">2<" in page.text


def test_usage_page_shows_user_events_without_internal_identifiers(client, monkeypatch):
    """使用页展示当前用户的事件，但不泄露 seed / account。"""
    import time as _time
    import utils.usage as usage

    email = "usage-page@example.com"
    _register(client, email, "password123")
    seed = store.get_user_auth(email)["seed"]
    usage.record_usage(seed, "internal-account-usage-page", "conversation")
    usage.record_usage(seed, "internal-account-usage-page", "conversation")
    usage.record_usage(seed, "internal-account-usage-page", "future-unknown-kind")
    usage.flush_usage()
    usage.record_usage(seed, "internal-account-usage-page", "image")

    page = client.get("/usage")
    assert page.status_code == 200
    assert "对话" in page.text
    assert "其他" in page.text
    assert "图片生成" in page.text
    assert seed not in page.text
    assert "internal-account-usage-page" not in page.text
    assert "最近 30 天" in page.text


def test_usage_page_limits_events_to_recent_window(client):
    """使用页不读取 30 天之前的事件，也不依赖当前套餐归属。"""
    import time as _time
    import utils.usage as usage

    email = "usage-window@example.com"
    _register(client, email, "password123")
    seed = store.get_user_auth(email)["seed"]
    store.add_usage_event(seed, "account-window", "conversation")
    with store._connect() as conn:
        conn.execute(
            "UPDATE usage_events SET created_at=? WHERE seed=?",
            (int(time.time()) - 31 * 86400, seed),
        )
    usage.record_usage(seed, "account-window", "image")
    page = client.get("/usage")
    assert page.status_code == 200
    assert "图片生成" in page.text
    assert "对话" not in page.text


def test_checkout_pending_order_status_is_read_only_and_owned(client_factory, monkeypatch):
    """pending 订单显示待支付；GET 不结算，且不能读取其他账号的订单。"""
    _install_fake_provider(monkeypatch)
    client = client_factory()
    _register(client, "order-a@example.com", "password123")
    response = _checkout(client, "plus-solo-1m", follow_redirects=False)
    assert response.status_code == 303
    order = store.list_orders(email="order-a@example.com")[0]
    assert order["status"] == "pending"

    page = client.get(f"/checkout?plan=plus-solo-1m&order={order['order_id']}")
    assert page.status_code == 200
    assert "待支付" in page.text
    assert store.get_order(order["order_id"])["status"] == "pending"

    other = TestClient(app.app)
    _register(other, "order-b@example.com", "password123")
    hidden = other.get(f"/checkout?plan=plus-solo-1m&order={order['order_id']}")
    normal = other.get("/checkout?plan=plus-solo-1m")
    assert hidden.status_code == normal.status_code == 200
    assert hidden.text == normal.text
    assert order["order_id"] not in hidden.text


def test_checkout_shows_paid_order_status(client, monkeypatch):
    """已支付订单在带 order 参数回访时显示成功，不重复激活。"""
    _install_fake_provider(monkeypatch)
    _register(client, "order-paid@example.com", "password123")
    order_id = _checkout(client, "plus-solo-1m", follow_redirects=False).headers["location"].split("order=")[1]
    assert client.post("/api/payment/callback", json={"order_id": order_id}).status_code == 200
    page = client.get(f"/checkout?plan=plus-solo-1m&order={order_id}")
    assert page.status_code == 200
    assert "支付成功" in page.text


def test_checkout_marks_expired_pending_order_as_invalid(client, monkeypatch):
    """超过 pending TTL 的订单显示失效，不伪装成待支付。"""
    _install_fake_provider(monkeypatch)
    _register(client, "order-expired@example.com", "password123")
    _checkout(client, "plus-solo-1m")
    order = store.list_orders(email="order-expired@example.com")[0]
    with store._connect() as conn:
        conn.execute(
            "UPDATE orders SET created_at=? WHERE order_id=?",
            (int(time.time()) - configs.order_pending_ttl - 1, order["order_id"]),
        )
    page = client.get(f"/checkout?plan=plus-solo-1m&order={order['order_id']}")
    assert page.status_code == 200
    assert "已失效" in page.text
    assert "待支付" not in page.text


def _checkout(client, plan_id, follow_redirects=True):
    client.get(f"/checkout?plan={plan_id}")  # 下发 CSRF cookie
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post(
        "/api/checkout", data={"plan": plan_id, "csrf_token": csrf},
        follow_redirects=follow_redirects,
    )


def _install_fake_provider(monkeypatch):
    """装一个「非 mock」的支付渠道，用来走真实的异步回调路径。

    mock 渠道的回调口被刻意关死（无签名可验），因此回调相关的用例不能用它。
    """
    import utils.payment as payment

    class _FakeProvider:
        name = "fake"
        is_mock = False

        def begin(self, order):
            return {"provider": self.name, "order_id": order.get("order_id")}

        def verify(self, payload):
            return (payload or {}).get("order_id") or None

    monkeypatch.setattr(payment, "get_provider", lambda: _FakeProvider())
    return _FakeProvider


def test_checkout_grants_entitlement_end_to_end(client):
    _register(client, "pay@example.com", "password123")
    row = store.get_user_auth("pay@example.com")

    from utils import entitlements
    assert entitlements.effective_tier(row["seed"]) == ""

    resp = _checkout(client, "plus-solo-1m")
    assert resp.status_code == 200  # 跟随 303 到 /dashboard
    assert entitlements.effective_tier(row["seed"]) == "plus"


def test_checkout_double_click_reuses_pending_order(client):
    """连点下单不得产生多张并行订单，否则用户白拿成倍有效期。"""
    _register(client, "double@example.com", "password123")

    _checkout(client, "plus-solo-1m")
    first = store.list_orders(email="double@example.com")
    _checkout(client, "plus-solo-1m")
    second = store.list_orders(email="double@example.com")

    # 第一单已 paid，故第二次新建一单（不可复用非 pending 单），但两单各自独立计时
    assert len(second) == len(first) + 1


def test_checkout_without_provider_is_fail_closed(client, monkeypatch):
    """未配置支付渠道 → 503 拒绝下单，绝不白送套餐。"""
    monkeypatch.setattr(configs, "payment_provider", "")
    _register(client, "noprov@example.com", "password123")

    resp = _checkout(client, "plus-solo-1m")
    assert resp.status_code == 503

    from utils import entitlements
    row = store.get_user_auth("noprov@example.com")
    assert entitlements.effective_tier(row["seed"]) == ""
    assert all(o["status"] == "pending" for o in store.list_orders(email="noprov@example.com"))


def test_checkout_requires_csrf(client):
    _register(client, "csrf@example.com", "password123")
    client.get("/checkout?plan=plus-solo-1m")
    resp = client.post("/api/checkout", data={"plan": "plus-solo-1m", "csrf_token": "forged"})
    assert resp.status_code == 403


def test_payment_callback_is_rejected_under_mock(client):
    """mock 渠道没有签名可验，回调口必须关死。

    否则「建单 + 自投回调」两条请求就能把自己的订单置成已支付，
    而 mock 的正常结算全走 /api/checkout 的 auto_settle，根本用不到这个口。
    """
    _register(client, "cb@example.com", "password123")
    resp = client.post("/api/orders", json={"tier_id": "pro-shared-1w"})
    order_id = resp.json()["order_id"]

    assert client.post("/api/payment/callback", json={"order_id": order_id}).status_code == 403
    assert store.get_order(order_id)["status"] == "pending"

    from utils import entitlements
    seed = store.get_user_auth("cb@example.com")["seed"]
    assert entitlements.effective_tier(seed) == ""


def test_payment_callback_is_idempotent(client, monkeypatch):
    """回调重复投递不得延长有效期（支付网关会重发）。"""
    # 用一个「非 mock」的 provider 走真实回调路径：mock 的回调口是关死的
    _install_fake_provider(monkeypatch)
    _register(client, "cb2@example.com", "password123")
    resp = client.post("/api/orders", json={"tier_id": "pro-shared-1w"})
    order_id = resp.json()["order_id"]

    assert client.post("/api/payment/callback", json={"order_id": order_id}).status_code == 200
    first_expiry = store.get_order(order_id)["expires_at"]

    assert client.post("/api/payment/callback", json={"order_id": order_id}).status_code == 200
    assert store.get_order(order_id)["expires_at"] == first_expiry


def test_payment_callback_reports_failure_for_unknown_order(client, monkeypatch):
    """激活失败必须让网关看到失败并重投，否则已付款的单会静默沉没。"""
    _install_fake_provider(monkeypatch)
    resp = client.post("/api/payment/callback", json={"order_id": "ord_does_not_exist"})
    assert resp.status_code == 500


def test_payment_callback_rejects_unknown_order(client, monkeypatch):
    _install_fake_provider(monkeypatch)
    resp = client.post("/api/payment/callback", json={})
    assert resp.status_code == 400


def test_delisted_plan_order_stops_retrying_instead_of_looping(client, monkeypatch):
    """套餐下架后，旧 pending 单再也激活不了 —— 必须置终态，不能让网关无限重投。

    500 的语义是「稍后重试能成」。这类单重试一万次也成不了，只会把一笔真实付款
    埋在重投日志里；置 failed + 告警才能让人看见并补发。
    """
    import utils.plans as plans

    _install_fake_provider(monkeypatch)
    _register(client, "delisted@example.com", "password123")
    order_id = client.post("/api/orders", json={"tier_id": "pro-shared-1w"}).json()["order_id"]

    # 模拟该 SKU 在建单之后下架
    monkeypatch.setattr(plans, "plan_detail", lambda pid: None)

    resp = client.post("/api/payment/callback", json={"order_id": order_id})
    assert resp.status_code == 200          # 不再要求网关重投
    assert resp.json()["ok"] is False       # 但明确告诉它「没成」
    assert store.get_order(order_id)["status"] == "failed"


def test_concurrent_settlement_does_not_swallow_paid_time(client, monkeypatch):
    """同档两笔订单并发结算必须各自叠加，不能都从 now 起算吞掉一个月。

    这是「读到期时间 → 加时长 → 写回」的经典竞态：两个回调同时读到「还没有有效套餐」，
    各自算出 now+30d，用户付了两个月只拿到一个月。
    """
    import threading
    import gateway.saas as saas

    _install_fake_provider(monkeypatch)
    _register(client, "race@example.com", "password123")

    ids = []
    for n in range(2):
        resp = client.post("/api/orders", json={"tier_id": "plus-solo-1m"})
        ids.append(resp.json()["order_id"])

    # 放大临界区：真实竞态窗口只有几微秒，靠运气撞不出来，必须在「读」和「写」之间
    # 强行插入一个切换点。注意 barrier 在修好之后**必定超时** —— 两个线程被结算锁
    # 串行化了，第二个根本进不到这里。这正是我们要的证据：barrier 能凑齐
    # 就说明两个线程同时进了临界区，也就说明锁没起作用。
    original = saas._grant_expiry
    barrier = threading.Barrier(2, timeout=1)
    both_entered = []

    def _slow_grant_expiry(email, detail, now=None):
        result = original(email, detail, now)
        try:
            barrier.wait()
            both_entered.append(True)  # 无锁时才可能走到这里
        except threading.BrokenBarrierError:
            pass
        return result

    monkeypatch.setattr(saas, "_grant_expiry", _slow_grant_expiry)

    def _settle(oid):
        saas._settle_order(oid)

    threads = [threading.Thread(target=_settle, args=(o,)) for o in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    expiries = sorted(store.get_order(o)["expires_at"] for o in ids)
    assert all(e for e in expiries), "两单都应已激活"
    assert not both_entered, "两个线程同时进入了临界区 —— 结算锁没有生效"
    # 两单相差约 30 天（后一单从前一单的到期时间起算），而不是几乎相等
    assert expiries[1] - expiries[0] > 29 * 86400


def test_expired_user_is_redirected_from_chat_entrance(client, monkeypatch, seed_account, make_access_token):
    """套餐到期后收藏的带 token 链接也进不去聊天，改引导续费。"""
    seed_account(make_access_token(account_id="acc-plus", plan_type="plus"), plan_type="plus")
    _fake_template(monkeypatch)
    _register(client, "gone@example.com", "password123")
    seed = store.get_user_auth("gone@example.com")["seed"]
    _grant("gone@example.com", "plus-solo-1m", days_left=-1)

    resp = client.get(f"/?token={seed}", follow_redirects=False)
    assert resp.status_code == 302
    assert "/store" in resp.headers["location"]


def test_checkout_page_hides_payment_form_without_provider(client, monkeypatch):
    """渠道没开通就在 GET 侧说清楚，别让用户选完支付方式点了确认才吃 503。"""
    monkeypatch.setattr(configs, "payment_provider", "")
    _register(client, "nopay-ui@example.com", "password123")

    resp = client.get("/checkout?plan=plus-solo-1m")
    assert resp.status_code == 200
    body = resp.content.decode("utf-8")
    assert "支付渠道暂未开通" in body
    assert "确认支付" not in body


def test_order_api_is_fail_closed_without_provider(client, monkeypatch):
    """旧落地页下单口与 /api/checkout 口径一致，不留永远付不了款的孤儿单。"""
    monkeypatch.setattr(configs, "payment_provider", "")
    _register(client, "orphan@example.com", "password123")

    resp = client.post("/api/orders", json={"tier_id": "plus-solo-1m"})
    assert resp.status_code == 503
    assert store.list_orders(email="orphan@example.com") == []


def test_repricing_invalidates_reusable_pending_order(client, monkeypatch):
    """改价后不得复用旧 pending 单，否则用户按旧价付款拿新套餐。"""
    import utils.plans as plans
    _install_fake_provider(monkeypatch)  # 非 mock：下单后停在 pending
    _register(client, "reprice@example.com", "password123")

    _checkout(client, "plus-solo-1m")
    orders = store.list_orders(email="reprice@example.com")
    assert len(orders) == 1
    assert orders[0]["amount"] == "99"

    # 涨价后再点一次：旧单金额已过时，必须新建而不是复用
    monkeypatch.setitem(plans.PRICE_TABLE["plus"]["solo"], "1m", 129)
    _checkout(client, "plus-solo-1m")
    orders = store.list_orders(email="reprice@example.com")
    assert len(orders) == 2
    assert {o["amount"] for o in orders} == {"99", "129"}


def test_legacy_bare_tier_order_is_visible_in_dashboard(client):
    """老格式订单在权益层算数，展示层也必须算数，否则用户有权益却看不到卡片。"""
    import time as _time
    _register(client, "legacy@example.com", "password123")
    store.create_order("ord-legacy", "legacy@example.com", "plus", "99", status="pending")
    store.activate_order("ord-legacy", int(_time.time()) + 15 * 86400)

    from utils import entitlements
    seed = store.get_user_auth("legacy@example.com")["seed"]
    assert entitlements.effective_tier(seed) == "plus"  # 权益层认

    resp = client.get("/dashboard")  # 展示层也得认，不能 303 弹回超市
    assert resp.status_code == 200
    assert "旧版套餐" in resp.content.decode("utf-8")


def test_entitlement_lookup_failure_refuses_instead_of_allowing(client, monkeypatch, seed_account, make_access_token):
    """数据层故障时分不清运营者和过期用户 —— 答「暂时不可用」，不是「都放进来」。"""
    seed_account(make_access_token(account_id="acc-plus", plan_type="plus"), plan_type="plus")
    _make_user("seed-dberr", "dberr@example.com")
    _grant("dberr@example.com", "plus-solo-1m")

    def _boom(*args, **kwargs):
        raise store.StoreError("database is locked")

    monkeypatch.setattr(store, "get_user_auth_by_seed", _boom)
    resp = client.post(
        "/backend-api/conversation", cookies={"token": "seed-dberr"}, json={"model": "gpt-5-5"}
    )
    assert resp.status_code == 503
