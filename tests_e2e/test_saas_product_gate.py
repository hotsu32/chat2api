"""P0 product gate: what the launched product exposes, and what it refuses.

Covers the operator-facing boundary end to end, on the real FastAPI app:

  - Free is not a customer option: the demo/trial entrances that hand out seed
    aliases are development-gated (404 by default) and the public tier catalogue
    only lists the tiers that are actually for sale;
  - payment settlement verifies amount / currency / provider transaction identity
    against the stored order before any entitlement appears;
  - banning a user invalidates their live web sessions through the existing
    pw_version contract;
  - every sensitive action leaves an audit record with no email, token or cookie
    in it;
  - shared trial capacity stays fail-closed when it is not configured.
"""

import time

import pytest

import utils.audit as audit
import utils.configs as configs
import utils.payment as payment
import utils.store as store


# --------------------------------------------------------------------- helpers

def _register(client, email, password="password123"):
    client.get("/register")  # 下发 CSRF cookie
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post("/register", data={"email": email, "password": password, "csrf_token": csrf})


def _signin(client, email, password="password123"):
    """在**另一台设备**上登录同一账号（注册会因邮箱已存在而失败）。"""
    client.get("/signin")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post("/signin", data={"email": email, "password": password, "csrf_token": csrf})


def _post_form(client, path, data):
    client.get("/dashboard")  # 保证 CSRF cookie 在手
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post(path, data={**data, "csrf_token": csrf})


def _create_order(client, plan_id="plus-solo-1m"):
    client.get("/dashboard")
    resp = client.post("/api/orders", json={"tier_id": plan_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["order_id"]


def _install_fake_provider(monkeypatch):
    """非 mock 渠道替身：``verify`` 产出结构化的 ``payment.ProviderCallback``。"""
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


@pytest.fixture
def admin_headers(monkeypatch):
    from gateway import admin
    monkeypatch.setattr(admin, "admin_password", "test-admin-secret")
    return {"Authorization": "Bearer test-admin-secret"}


# ------------------------------------------------------- development-gated entry

@pytest.mark.parametrize("path", ["/try", "/demo"])
def test_alias_entrances_are_absent_unless_explicitly_enabled(client, monkeypatch, path):
    """未打开开发闸门时，发种子别名的入口按不存在处理。"""
    monkeypatch.setattr(configs, "dev_access_enabled", False)
    resp = client.get(path)
    assert resp.status_code == 404
    # 404 正文里不能顺带泄漏种子别名
    assert "frontend-proof" not in resp.text
    assert "demo-plus-pool" not in resp.text


@pytest.mark.parametrize("path", ["/try", "/demo"])
def test_alias_entrances_stay_available_for_developers_when_enabled(client, monkeypatch, path):
    """显式打开闸门 = 保留本地/运营验证能力，不因为收紧生产而拆掉开发入口。"""
    monkeypatch.setattr(configs, "dev_access_enabled", True)
    assert client.get(path).status_code == 200


def test_direct_alias_link_is_absent_when_development_gate_is_closed(client, monkeypatch):
    monkeypatch.setattr(configs, "dev_access_enabled", False)
    resp = client.get("/?token=frontend-proof-plus-2", follow_redirects=False)
    assert resp.status_code == 404


def test_public_tier_catalogue_hides_the_non_sellable_free_tier(client, monkeypatch):
    monkeypatch.setattr(configs, "dev_access_enabled", False)
    catalog = client.get("/api/tiers").json()
    assert set(catalog) == {"plus", "pro"}
    assert "free" not in catalog


def test_public_tier_catalogue_keeps_free_for_development(client, monkeypatch):
    monkeypatch.setattr(configs, "dev_access_enabled", True)
    catalog = client.get("/api/tiers").json()
    assert "free" in catalog


@pytest.mark.parametrize("path", ["/landing", "/try"])
def test_customer_facing_pages_do_not_advertise_a_free_plan(client, monkeypatch, path):
    """正式产品只卖 Plus / Pro：对外页面里不出现 Free 档位入口。"""
    monkeypatch.setattr(configs, "dev_access_enabled", True)  # /try 需要闸门才可达
    body = client.get(path).text
    assert "Plus" in body and "Pro" in body
    assert "购买 Free" not in body
    assert "Free 组" not in body
    assert "plus-solo" in body or "plus" in body.lower()


def test_store_describes_paid_plans_as_unlimited_during_the_term(client):
    assert _register(client, "store-copy@example.test").status_code == 200
    body = client.get("/store").text
    assert "套餐有效期内不限次数使用" in body
    assert "每日对话上限" not in body


# ------------------------------------------------------------- payment contract

def test_callback_amount_mismatch_never_grants_entitlement(client, monkeypatch):
    """金额不符是终局：400、订单仍 pending、一分权益都不产生。"""
    _install_fake_provider(monkeypatch)
    _register(client, "gate-amount@example.com")
    order_id = _create_order(client)

    resp = client.post("/api/payment/callback",
                       json={"order_id": order_id, "amount": "1"})
    assert resp.status_code == 400
    assert store.get_order(order_id)["status"] == "pending"

    from utils import entitlements
    seed = store.get_user_auth("gate-amount@example.com")["seed"]
    assert entitlements.active_orders("gate-amount@example.com") == []
    assert entitlements.effective_tier(seed) == "plus"  # 仍是注册时的试用档


def test_callback_currency_mismatch_is_rejected(client, monkeypatch):
    _install_fake_provider(monkeypatch)
    _register(client, "gate-currency@example.com")
    order_id = _create_order(client)

    resp = client.post("/api/payment/callback",
                       json={"order_id": order_id, "currency": "USD"})
    assert resp.status_code == 400
    assert store.get_order(order_id)["status"] == "pending"


def test_callback_cannot_replay_one_transaction_onto_another_users_order(client_factory, monkeypatch):
    """同一支付流水号只能结算它自己那张单 —— 换一张单（另一个用户）即拒绝。"""
    _install_fake_provider(monkeypatch)
    buyer_a = client_factory()
    buyer_b = client_factory()
    _register(buyer_a, "gate-a@example.com")
    _register(buyer_b, "gate-b@example.com")

    order_a = _create_order(buyer_a)
    order_b = _create_order(buyer_b)

    assert buyer_a.post("/api/payment/callback", json={
        "order_id": order_a, "transaction_id": "txn-shared-gate"}).status_code == 200
    assert store.get_order(order_a)["status"] == "paid"

    replay = buyer_b.post("/api/payment/callback", json={
        "order_id": order_b, "transaction_id": "txn-shared-gate"})
    assert replay.status_code == 400
    assert store.get_order(order_b)["status"] == "pending"


def test_callback_without_structured_provider_result_is_rejected(client, monkeypatch):
    """provider 返回裸 order_id（旧契约）不得被当作有效回调。"""
    class _LegacyProvider:
        name = "legacy"
        is_mock = False

        def begin(self, order):
            return {"provider": self.name, "order_id": order.get("order_id")}

        def verify(self, payload):
            return (payload or {}).get("order_id") or None

    monkeypatch.setattr(payment, "get_provider", lambda: _LegacyProvider())
    _register(client, "gate-legacy@example.com")
    order_id = _create_order(client)

    resp = client.post("/api/payment/callback", json={"order_id": order_id})
    assert resp.status_code == 400
    assert store.get_order(order_id)["status"] == "pending"


def test_settlement_records_an_audit_event_without_personal_data(client, monkeypatch):
    _install_fake_provider(monkeypatch)
    _register(client, "gate-audit-pay@example.com")
    order_id = _create_order(client)
    assert client.post("/api/payment/callback",
                       json={"order_id": order_id}).status_code == 200

    events = [e for e in audit.recent() if e["action"] == "payment.settled"]
    assert events, "结算必须留下审计记录"
    assert events[0]["subject"] == audit.subject_id("gate-audit-pay@example.com")
    blob = str(events[0])
    assert "gate-audit-pay@example.com" not in blob


# --------------------------------------------------------- ban + session policy

def test_banning_a_user_kills_the_live_web_session(client, client_factory, monkeypatch, admin_headers):
    """封禁走既有的会话吊销合同：别的设备上的 cookie 立刻作废。"""
    monkeypatch.setattr(configs, "dev_access_enabled", False)
    _register(client, "gate-ban@example.com")
    assert client.get("/dashboard").status_code == 200

    other_device = client_factory()
    _signin(other_device, "gate-ban@example.com")
    assert other_device.get("/dashboard").status_code == 200

    banned = client.post("/admin/users/status", headers=admin_headers,
                         json={"email": "gate-ban@example.com", "status": "banned"})
    assert banned.status_code == 200, banned.text
    assert banned.json()["sessions_revoked"] >= 1

    # 两台设备都被踢出登录态，并被明确告知「账号已停用」而不是「密码变了」
    for device in (client, other_device):
        resp = device.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin?reason=disabled"

    # 旧 cookie 不再被承认：每次访问都被推回登录页
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_banned_user_cannot_sign_in_again(client, admin_headers):
    _register(client, "gate-ban2@example.com", "password123")
    assert client.post("/admin/users/status", headers=admin_headers,
                       json={"email": "gate-ban2@example.com",
                             "status": "banned"}).status_code == 200

    client.get("/signin")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    resp = client.post("/signin", data={
        "email": "gate-ban2@example.com", "password": "password123", "csrf_token": csrf})
    assert resp.status_code == 403
    assert "停用" in resp.text


def test_unban_restores_sign_in_and_leaves_no_stale_session(client, admin_headers):
    """解封后可重新登录；此前被吊销的会话不会自己复活。"""
    old_device = client
    _register(old_device, "gate-unban@example.com", "password123")
    client.post("/admin/users/status", headers=admin_headers,
                json={"email": "gate-unban@example.com", "status": "banned"})
    client.post("/admin/users/status", headers=admin_headers,
                json={"email": "gate-unban@example.com", "status": "active"})

    old_device.get("/signin")
    csrf = old_device.cookies.get(configs.user_csrf_cookie) or ""
    resp = old_device.post("/signin", data={
        "email": "gate-unban@example.com", "password": "password123", "csrf_token": csrf},
        follow_redirects=False)
    assert resp.status_code == 303
    assert old_device.get("/dashboard").status_code == 200


def test_banned_account_cannot_resurrect_itself_through_password_reset(client, admin_headers):
    """封禁必须单向：控制着邮箱的被封用户不能靠「找回密码」把自己放回来。"""
    _register(client, "gate-reset@example.com", "password123")
    client.post("/admin/users/status", headers=admin_headers,
                json={"email": "gate-reset@example.com", "status": "banned"})

    # 走一遍找回密码，拿到真实的重置 token（自己控制邮箱 = 自己拿得到链接）
    client.get("/forgot-password")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    assert client.post("/forgot-password", data={
        "email": "gate-reset@example.com", "csrf_token": csrf}).status_code == 200
    token = None
    with store._connect() as conn:
        row = conn.execute(
            "SELECT token FROM email_tokens WHERE email=? AND kind='reset' "
            "ORDER BY rowid DESC LIMIT 1", ("gate-reset@example.com",)).fetchone()
        token = row[0] if row else None
    assert token, "找回密码应当签发了重置 token"

    client.get(f"/reset-password?token={token}")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    resp = client.post("/reset-password", data={
        "token": token, "password": "brand-new-password", "csrf_token": csrf})
    assert resp.status_code == 403
    assert "停用" in resp.text

    # 状态没被改写，新密码也没生效
    assert store.get_user_auth("gate-reset@example.com")["status"] == "banned"
    client.get("/signin")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    assert client.post("/signin", data={
        "email": "gate-reset@example.com", "password": "brand-new-password",
        "csrf_token": csrf}).status_code == 401
    # 即便用原密码，封禁同样挡在门外
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    assert client.post("/signin", data={
        "email": "gate-reset@example.com", "password": "password123",
        "csrf_token": csrf}).status_code == 403


def test_banned_account_does_not_become_active_by_verifying_email(client, admin_headers):
    """邮箱验证只该把 unverified 变 active，不该覆盖 banned。"""
    _register(client, "gate-verify@example.com", "password123")
    store.upsert_user_auth("gate-verify@example.com", status="banned")
    token = "verify-token-synthetic"
    store.create_email_token(token, "gate-verify@example.com", "verify",
                             int(time.time()) + 3600)

    assert client.get(f"/verify-email?token={token}").status_code == 200
    assert store.get_user_auth("gate-verify@example.com")["status"] == "banned"


def test_disabled_account_is_told_why_instead_of_being_told_the_password_changed(client, admin_headers):
    """登录页的提示必须区分「被停用」与「密码变更」：否则被封的人会去改密码白跑一趟。"""
    _register(client, "gate-notice@example.com")
    client.post("/admin/users/status", headers=admin_headers,
                json={"email": "gate-notice@example.com", "status": "banned"})

    page = client.get("/signin?reason=disabled")
    assert page.status_code == 200
    assert "停用" in page.text
    assert "密码已变更" not in page.text


def test_admin_user_status_requires_operator_auth(client):
    _register(client, "gate-noauth@example.com")
    for path, payload in (("/admin/users", None), ("/admin/audit", None),
                          ("/admin/users/status", {"email": "gate-noauth@example.com",
                                                   "status": "banned"})):
        resp = client.post(path, json=payload) if payload else client.get(path)
        assert resp.status_code in (401, 403, 503)


def test_unknown_user_and_unknown_status_are_rejected(client, admin_headers):
    assert client.post("/admin/users/status", headers=admin_headers,
                       json={"email": "nobody@example.com", "status": "banned"}).status_code == 404
    _register(client, "gate-badstatus@example.com")
    assert client.post("/admin/users/status", headers=admin_headers,
                       json={"email": "gate-badstatus@example.com",
                             "status": "deleted"}).status_code == 400


# ------------------------------------------------------------------- audit log

def test_audit_records_never_contain_emails_or_tokens(client, admin_headers, monkeypatch):
    """运营审计可被检索，但不能变成第二份用户/凭据数据库。"""
    _register(client, "gate-pii@example.com")
    row = store.get_user_auth("gate-pii@example.com")
    seed = row["seed"]

    client.post("/admin/users/status", headers=admin_headers,
                json={"email": "gate-pii@example.com", "status": "banned"})
    body = client.get("/admin/audit", headers=admin_headers).json()
    assert body["status"] == "success"

    blob = str(body["events"])
    assert "gate-pii@example.com" not in blob
    assert seed not in blob
    assert audit.subject_id("gate-pii@example.com") in blob


def test_admin_user_list_masks_addresses(client, admin_headers):
    _register(client, "gate-mask@example.com")
    users = client.get("/admin/users", headers=admin_headers).json()["users"]
    entry = next(u for u in users if u["subject"] == audit.subject_id("gate-mask@example.com"))
    assert entry["email"] == "ga***@example.com"
    assert entry["status"] == "active"


def test_pool_mutation_leaves_an_audit_trail(client, admin_headers):
    from utils import globals as state
    state.token_list.clear()

    client.post("/admin/routing/accounts/import", headers=admin_headers,
                json={"text": "synthetic-account-line"})
    client.post("/admin/routing/accounts/delete", headers=admin_headers,
                json={"token": "synthetic-account-line"})

    actions = [e["action"] for e in audit.recent()]
    assert "pool.accounts_imported" in actions
    assert "pool.account_deleted" in actions
    # 账号本身是凭据，审计里不能出现它的原文
    assert "synthetic-account-line" not in str(audit.recent())


# ------------------------------------------------------------ trial capacity

def test_dashboard_reports_unconfigured_trial_capacity_instead_of_a_broken_entry(client, monkeypatch):
    """共享容量未配置 = fail-closed：说清「容量未开放」，而不是给一个必然失败的入口。"""
    monkeypatch.setattr(configs, "max_shared_seeds_per_account", 0)
    _register(client, "gate-capacity@example.com")

    body = client.get("/dashboard").text
    assert "试用容量暂未开放" in body
    assert "开始试用" not in body
    # 不是「账号状态不支持」那种误导性文案
    assert "当前账号状态不支持使用试用额度" not in body


def test_configured_capacity_restores_the_trial_entry(client, monkeypatch):
    monkeypatch.setattr(configs, "max_shared_seeds_per_account", 2)
    _register(client, "gate-capacity-ok@example.com")

    body = client.get("/dashboard").text
    assert "开始试用" in body
    assert "试用容量暂未开放" not in body
