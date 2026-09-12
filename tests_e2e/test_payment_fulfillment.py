"""支付后履约：结算事务、同档账号分配、失败恢复。

覆盖验收判据里的这一段：

  - 支付成功立即绑定正确档次账号（Plus 试用/购买都只能绑 Plus 号）；
  - 订单重复回调不会重复激活或延长有效期；
  - 绑定失败（容量未配置 / 无同档健康账号）不抹掉付款事实，且用户可恢复；
  - Dashboard 把「付过钱」和「能聊天」分开显示，不给进不去的入口。

全部跑在真实 FastAPI app + 真实 SQLite 上：结算走 main 的
``store.settle_order``，分配走 ``utils.seed_lifecycle.route_seed``，
不 mock 这两者本身（只在专门验证故障路径时替换它们）。
"""
import sqlite3
import time

import pytest

import utils.configs as configs
import utils.store as store


# ---------------------------------------------------------------------------
# Helpers（与 tests_e2e/test_user_saas.py 同口径）
# ---------------------------------------------------------------------------

def _register(client, email, password="password123"):
    client.get("/register")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post(
        "/register", data={"email": email, "password": password, "csrf_token": csrf}
    )


def _checkout(client, plan_id, follow_redirects=True):
    """走 mock 渠道下单 —— auto_settle 会立刻结算并尝试分配。"""
    client.get(f"/checkout?plan={plan_id}")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post(
        "/api/checkout", data={"plan": plan_id, "csrf_token": csrf},
        follow_redirects=follow_redirects,
    )


def _retry(client, follow_redirects=True):
    client.get("/dashboard")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post(
        "/api/fulfillment/retry", data={"csrf_token": csrf},
        follow_redirects=follow_redirects,
    )


def _install_fake_provider(monkeypatch):
    """装一个「非 mock」的支付渠道，用来走真实的异步回调路径。

    ``verify`` 返回完整的 ``payment.ProviderCallback``（订单号 + 流水号 + 金额 +
    币种）—— 结算侧只认这个结构，旧契约里那个裸 order_id 字符串现在会被判为无效回调。
    金额 / 币种缺省时按库里订单补齐，模拟「渠道报的金额与订单一致」的正常情形。
    """
    import utils.payment as payment

    class _FakeProvider:
        name = "fake"
        is_mock = False

        def begin(self, order):
            return {"provider": self.name, "order_id": order.get("order_id")}

        def verify(self, payload):
            payload = payload or {}
            order_id = payload.get("order_id")
            if not order_id:
                return None
            order = store.get_order(order_id) or {}
            return payment.ProviderCallback(
                order_id=order_id,
                transaction_id=str(payload.get("transaction_id") or f"txn-{order_id}"),
                amount=str(payload.get("amount", order.get("amount"))),
                currency=str(payload.get("currency") or payment.expected_currency()),
                provider=self.name,
            )

    monkeypatch.setattr(payment, "get_provider", lambda: _FakeProvider())
    return _FakeProvider


def _only_order(email):
    import utils.store as _store
    orders = _store.list_orders(email=email)
    assert len(orders) == 1, f"期望恰好一张订单，实际 {len(orders)}"
    return orders[0]


# ---------------------------------------------------------------------------
# 支付成功 → 立即绑定同档账号
# ---------------------------------------------------------------------------

def test_payment_immediately_binds_same_tier_account(client, seed_account, make_access_token):
    """付款成功后立刻绑定同档健康账号，且绝不绑到其它档位。"""
    tok_free = make_access_token(account_id="acc-free", plan_type="free")
    tok_plus = make_access_token(account_id="acc-plus", plan_type="plus")
    seed_account(tok_free, plan_type="free")
    seed_account(tok_plus, plan_type="plus")

    email = "fulfil-basic@example.com"
    _register(client, email)
    seed = store.get_user_auth(email)["seed"]

    resp = _checkout(client, "plus-solo-1m")
    assert resp.status_code == 200

    # 支付事实
    order = _only_order(email)
    assert order["status"] == "paid"
    assert order["expires_at"] > time.time()

    # 服务落地：绑定到 plus 号，而不是随手挑到的 free 号
    row = store.get_user(seed)
    assert row["status"] == "active", "付款后 Seed 必须被激活"
    assert row["current_account"] == tok_plus
    assert row["plan_type"] == "plus"

    # 用户可见状态：这时才该出现入口
    body = client.get("/dashboard").text
    assert "服务正常" in body
    assert "进入 ChatGPT" in body
    assert "重试分配" not in body


def test_dashboard_shows_no_entry_before_allocation(client, seed_account, make_access_token):
    """池里没有同档健康账号时，订单仍为 paid，但页面不给「进入」入口。

    「付了钱」和「能聊天」是两件事：把 paid 直接当成服务可用，用户点进去必然失败。
    """
    tok_pro = make_access_token(account_id="acc-pro", plan_type="pro")
    seed_account(tok_pro, plan_type="pro")

    email = "fulfil-no-plus@example.com"
    _register(client, email)
    seed = store.get_user_auth(email)["seed"]

    assert _checkout(client, "plus-solo-1m").status_code == 200

    # 付款事实没有被抹掉
    order = _only_order(email)
    assert order["status"] == "paid"
    assert order["expires_at"] > time.time()

    # 但没有绑定任何账号 —— 池里只有 pro 号，plus 档无号可用
    row = store.get_user(seed)
    assert row["current_account"] in (None, "")
    assert row["status"] != "active"

    body = client.get("/dashboard").text
    assert "进入 ChatGPT" not in body
    assert "待分配账号" in body
    assert "重试分配" in body
    # 不能把绑定失败说成服务正常，也不能因此把用户降级去用试用
    assert "服务正常" not in body
    assert "开始试用" not in body


# ---------------------------------------------------------------------------
# 重复回调：不重复激活、不延长有效期、不重复绑定
# ---------------------------------------------------------------------------

def test_duplicate_callback_does_not_extend_or_rebind(client, monkeypatch, seed_account, make_access_token):
    """支付网关重投回调：有效期不延长，绑定不被重写。"""
    tok_plus = make_access_token(account_id="acc-plus", plan_type="plus")
    seed_account(tok_plus, plan_type="plus")
    _install_fake_provider(monkeypatch)

    email = "fulfil-dup@example.com"
    _register(client, email)
    seed = store.get_user_auth(email)["seed"]

    order_id = client.post("/api/orders", json={"tier_id": "plus-solo-1m"}).json()["order_id"]

    assert client.post("/api/payment/callback", json={"order_id": order_id}).status_code == 200
    first = store.get_order(order_id)
    assert first["status"] == "paid"
    assert store.get_user(seed)["current_account"] == tok_plus

    for _ in range(3):
        assert client.post("/api/payment/callback", json={"order_id": order_id}).status_code == 200

    again = store.get_order(order_id)
    assert again["expires_at"] == first["expires_at"], "重复回调延长了有效期"
    assert store.get_user(seed)["current_account"] == tok_plus
    assert len(store.list_orders(email=email)) == 1


def test_retry_after_success_does_not_duplicate_payment(client, seed_account, make_access_token):
    """已在服务中的订单再点重试：不新增订单、不改变到期时间、不重复扣费。"""
    tok_plus = make_access_token(account_id="acc-plus", plan_type="plus")
    seed_account(tok_plus, plan_type="plus")

    email = "fulfil-retry-idem@example.com"
    _register(client, email)
    seed = store.get_user_auth(email)["seed"]
    assert _checkout(client, "plus-solo-1m").status_code == 200

    before = _only_order(email)
    assert store.get_user(seed)["current_account"] == tok_plus

    resp = _retry(client)
    assert resp.status_code == 200
    assert "账号分配已完成" in resp.text

    after = _only_order(email)
    assert after["expires_at"] == before["expires_at"]
    assert after["status"] == "paid"
    assert store.get_user(seed)["current_account"] == tok_plus


# ---------------------------------------------------------------------------
# 绑定失败 → 诚实待分配 + 可重试恢复
# ---------------------------------------------------------------------------

def test_binding_failure_is_recoverable_from_dashboard(client, seed_account, make_access_token):
    """购买时无可用账号 → 付款保留、页面诚实待分配；补上账号后重试即恢复。"""
    email = "fulfil-recover@example.com"
    _register(client, email)
    seed = store.get_user_auth(email)["seed"]

    # 池里还没有 plus 号 —— 绑定必然失败
    assert _checkout(client, "plus-solo-1m").status_code == 200
    order = _only_order(email)
    assert order["status"] == "paid"
    assert store.get_user(seed)["status"] != "active"

    body = client.get("/dashboard").text
    assert "待分配账号" in body
    assert "重试分配" in body
    assert "进入 ChatGPT" not in body

    # 账号到货后重试分配
    tok_plus = make_access_token(account_id="acc-plus-late", plan_type="plus")
    seed_account(tok_plus, plan_type="plus")

    resp = _retry(client)
    assert resp.status_code == 200
    assert "账号分配已完成" in resp.text

    row = store.get_user(seed)
    assert row["status"] == "active"
    assert row["current_account"] == tok_plus
    # 重试不产生新的付款事实
    assert _only_order(email)["expires_at"] == order["expires_at"]

    body = client.get("/dashboard").text
    assert "进入 ChatGPT" in body
    assert "重试分配" not in body


def test_unconfigured_shared_capacity_is_reported_not_silently_broken(
    client, monkeypatch, seed_account, make_access_token
):
    """共享容量未配置（默认 0）时拒绝分配，并把原因如实告诉用户。

    运营没配置容量就静默绑上一个共享账号，等于把「不知道能挂几个人」的账号
    直接放到线上；这里要求 fail-closed，同时不能把用户的付款弄丢。
    """
    monkeypatch.setattr(configs, "max_shared_seeds_per_account", 0)

    tok_plus = make_access_token(account_id="acc-plus-shared", plan_type="plus")
    seed_account(tok_plus, plan_type="plus")

    email = "fulfil-capacity@example.com"
    _register(client, email)
    seed = store.get_user_auth(email)["seed"]

    assert _checkout(client, "plus-shared-1m").status_code == 200

    # 付款保留，但没有绑定
    order = _only_order(email)
    assert order["status"] == "paid"
    assert store.get_user(seed)["current_account"] in (None, "")

    body = client.get("/dashboard").text
    assert "待分配账号" in body
    assert "重试分配" in body
    assert "进入 ChatGPT" not in body

    # 重试同样失败，但这一次会把具体原因带回来；付款仍然一分不少
    resp = _retry(client)
    assert resp.status_code == 200
    assert "容量尚未配置" in resp.text
    assert _only_order(email)["status"] == "paid"


def test_retry_requires_csrf(client, seed_account, make_access_token):
    """分配重试是用户动作，必须和其它动作一样带 CSRF。"""
    tok_plus = make_access_token(account_id="acc-plus-csrf", plan_type="plus")
    seed_account(tok_plus, plan_type="plus")
    email = "fulfil-csrf@example.com"
    _register(client, email)
    assert _checkout(client, "plus-solo-1m").status_code == 200

    resp = client.post("/api/fulfillment/retry", data={"csrf_token": "forged"})
    assert resp.status_code == 403


def test_retry_requires_login(client):
    """未登录不能触发分配重试 —— 这个接口只操作自己的 Seed，但入口仍须鉴权。

    同时断言路由确实注册在 app 上：``gateway/backend.py`` 有一个 catch-all，
    未知路径也会走到鉴权并回 401，只看状态码会在路由被删掉时静默通过。
    """
    import app as _app
    paths = {getattr(r, "path", "") for r in _app.app.routes}
    assert "/api/fulfillment/retry" in paths, "重试路由未注册"

    resp = client.post("/api/fulfillment/retry", follow_redirects=False)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 结算故障：不得伪装成成功
# ---------------------------------------------------------------------------

def test_settlement_db_failure_is_not_reported_as_success(client, monkeypatch):
    """结算写库失败 → 下单侧明确报错，不假装支付成功把人送回控制台。"""
    def _boom(*_a, **_kw):
        raise store.StoreError("database is locked")

    monkeypatch.setattr(store, "settle_order", _boom)

    email = "fulfil-dberr@example.com"
    _register(client, email)

    resp = _checkout(client, "plus-solo-1m", follow_redirects=False)
    assert resp.status_code == 503

    # 一分钱都没变成权益
    order = _only_order(email)
    assert order["status"] == "pending"
    assert order["expires_at"] in (None, 0, "")


def test_callback_reports_settlement_failure_so_gateway_retries(client, monkeypatch):
    """回调侧结算失败必须回 5xx，让支付网关重投，而不是静默丢单。"""
    _install_fake_provider(monkeypatch)

    def _boom(*_a, **_kw):
        raise store.StoreError("database is locked")

    monkeypatch.setattr(store, "settle_order", _boom)

    email = "fulfil-cb-dberr@example.com"
    _register(client, email)
    order_id = client.post("/api/orders", json={"tier_id": "plus-solo-1m"}).json()["order_id"]

    resp = client.post("/api/payment/callback", json={"order_id": order_id})
    assert resp.status_code == 500
    assert store.get_order(order_id)["status"] == "pending"


class _MetaUnavailable:
    """Connection proxy where only the ``meta`` KV table is unavailable.

    The order lookup keeps working, so the callback gets all the way to the
    provider-transaction binding step before storage fails — which is the step whose
    failure must not be mistaken for "this transaction was never used".
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *params):
        if "meta" in sql.lower():
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *params)

    # Context-manager dunders are looked up on the type, not via __getattr__.
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._conn.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_callback_binding_failure_is_retried_never_settled(client, monkeypatch):
    """认领支付流水号时存储层故障 → 回调整体失败（5xx 让网关重投），绝不静默结算。

    「查不到绑定」和「查不了绑定」是两件事。把后者当成前者，就等于在数据库打嗝时
    放行一笔可能的重放 —— 同一个流水号可以拿来结算第二张单。这里要求整条回调失败：
    订单保持 pending、不产生任何权益。旧的 get_meta/set_meta 会吞掉故障照常放行。
    """
    _install_fake_provider(monkeypatch)
    real_connect = store._connect
    monkeypatch.setattr(store, "_connect", lambda: _MetaUnavailable(real_connect()))

    email = "fulfil-bind-dberr@example.com"
    _register(client, email)
    seed = store.get_user_auth(email)["seed"]
    order_id = client.post("/api/orders", json={"tier_id": "plus-solo-1m"}).json()["order_id"]

    resp = client.post("/api/payment/callback", json={"order_id": order_id})
    assert resp.status_code == 500

    order = store.get_order(order_id)
    assert order["status"] == "pending", "绑定失败却把订单置成了已支付"
    assert order["expires_at"] in (None, 0)
    assert store.get_user(seed)["current_account"] in (None, "")
    assert store.get_user(seed)["status"] != "active"


def test_allocation_failure_still_settles_and_acknowledges(client, monkeypatch):
    """分配故障不影响结算：回调仍确认收款，且明确告知「还没分配好」。"""
    _install_fake_provider(monkeypatch)

    def _boom(*_a, **_kw):
        raise store.StoreError("seed lifecycle unavailable")

    monkeypatch.setattr("gateway.saas.seed_lifecycle.route_seed", _boom)

    email = "fulfil-alloc-dberr@example.com"
    _register(client, email)
    order_id = client.post("/api/orders", json={"tier_id": "plus-solo-1m"}).json()["order_id"]

    resp = client.post("/api/payment/callback", json={"order_id": order_id})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "fulfilled": False}

    # 钱收到了，没有被分配故障吞掉
    assert store.get_order(order_id)["status"] == "paid"

    body = client.get("/dashboard").text
    assert "待分配账号" in body
    assert "重试分配" in body

    # 重试路径把具体原因带回来，付款仍然保留
    resp = _retry(client)
    assert resp.status_code == 200
    assert "系统繁忙" in resp.text
    assert store.get_order(order_id)["status"] == "paid"
