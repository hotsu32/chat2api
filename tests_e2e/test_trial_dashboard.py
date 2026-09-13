"""Dashboard 试用区块渲染测试。

覆盖：
  - 新用户（有试用赠额，全额可用）
  - 三次全在途（有余额但全被占住，busy 态）
  - 额度耗尽（3/3 已结算，exhausted 态 + CTA）
  - 有效付费套餐（订阅卡片，试用区块隐藏）
  - 付费到期 + 未用完试用（不回落试用，不展示试用入口）
  - 账号未验证 / 被封禁（不展示试用入口，不把「不可用」标为 banned）
  - DB 故障（渲染明确的不可用信息，不展示零余额）

全部跑在真实 FastAPI app + TestClient 上，无 mock 余额硬编码于模板。
"""
import time

import pytest
from starlette.testclient import TestClient

import app as _app_module
import utils.configs as configs
import utils.globals as globals
import utils.store as store
import utils.trials as trials


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register(client, email, password="Password123"):
    client.get("/register")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    resp = client.post("/register", data={"email": email, "password": password, "csrf_token": csrf})
    assert resp.status_code == 200, f"register failed: {resp.status_code}"
    return resp


def _signin(client, email, password="Password123"):
    client.get("/signin")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    resp = client.post("/signin", data={"email": email, "password": password, "csrf_token": csrf})
    assert resp.status_code == 200, f"signin failed: {resp.status_code}"
    return resp


def _make_paid(email, plan_id="plus-solo-1m", days_left=30):
    import utils.plans as plans
    detail = plans.plan_detail(plan_id)
    order_id = f"ord-trial-dash-{email}-{plan_id}"
    store.create_order(order_id, email, detail["id"], str(detail["price"]), status="pending")
    store.activate_order(order_id, int(time.time()) + days_left * 86400)
    return order_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fresh_trial_shows_entry_and_balance(client):
    """新注册用户：显示试用入口、余额 3/0/3（total/used/remaining）。"""
    _register(client, "trial-fresh@example.com")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text

    assert "Plus 免费试用" in body
    assert "开始试用" in body
    # 余额数字来自真实 DB，不是硬编码
    assert "共 3 次" in body
    assert "已用 0 次" in body
    assert "剩余 3 次" in body
    # 绝不展示 Free 档
    assert "Free" not in body
    assert "free" not in body.lower().replace("trial-fresh", "")


def test_three_in_flight_shows_busy_state(client):
    """3 次全被在途预留占住：显示忙碌态，不显示试用入口。"""
    _register(client, "trial-busy@example.com")
    email = "trial-busy@example.com"
    row = store.get_user_auth(email)
    seed = row["seed"]

    # 全部 3 次都预留但不结算（模拟并发长任务）
    rids = [trials.reserve(seed) for _ in range(3)]
    assert len(rids) == 3

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text

    assert "Plus 免费试用" in body
    # 进行中计数展示
    assert "进行中 3 次" in body
    # 忙碌提示，不是「耗尽」
    assert "等待完成" in body or "正在生成" in body
    # 无试用入口链接（进行中全占时不能再发起）
    assert "开始试用" not in body

    # 清理：释放预留，不影响其他测试
    for rid in rids:
        trials.release(rid, seed)


def test_exhausted_shows_cta_no_entry(client):
    """3 次全部结算完：显示耗尽提示和购买 CTA，不显示试用入口。"""
    _register(client, "trial-exhausted@example.com")
    row = store.get_user_auth("trial-exhausted@example.com")
    seed = row["seed"]

    for _ in range(3):
        rid = trials.reserve(seed)
        trials.settle(rid, seed)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text

    assert "Plus 免费试用" in body
    assert "3 次试用已用完" in body or "已用完" in body
    # 购买 CTA 出现
    assert "购买 Plus" in body
    assert "购买 Pro" in body
    # 无试用入口
    assert "开始试用" not in body
    # 不展示 Free 档
    assert "Free" not in body


def test_paid_user_shows_subscription_not_trial(client):
    """有效付费用户：显示订阅卡片，试用区块隐藏。"""
    _register(client, "trial-paid@example.com")
    _make_paid("trial-paid@example.com", "plus-solo-1m", days_left=20)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text

    # 订阅卡片存在
    assert "服务正常" in body or "天" in body
    # 试用入口不出现（付费用户无需试用入口）
    assert "开始试用" not in body
    assert "3 次试用已用完" not in body


def test_expired_paid_with_unused_trial_no_trial_entry(client):
    """付费到期 + 试用未用：不回落试用，不显示试用入口。"""
    _register(client, "trial-expired-paid@example.com")
    _make_paid("trial-expired-paid@example.com", "plus-solo-1m", days_left=-1)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text

    # 试用余额还在（3 次未用），但不能作为权益入口
    assert "开始试用" not in body
    # 过期续费引导
    assert "过期" in body or "续费" in body or "购买" in body


def test_expired_paid_with_reservations_does_not_offer_waiting_for_trial(client):
    email = "trial-expired-reserved@example.com"
    _register(client, email)
    seed = store.get_user_auth(email)["seed"]
    reservations = [trials.reserve(seed) for _ in range(3)]
    _make_paid(email, days_left=-1)
    body = client.get("/dashboard").text
    assert "等待完成" not in body
    assert "开始试用" not in body
    assert "续费" in body
    for rid in reservations:
        trials.release(rid, seed)


def test_expired_paid_with_unused_trial_blames_the_subscription_not_the_account(client):
    """付费到期 + 试用未用：必须说「套餐已到期」，不能赖到账号状态上。

    试用区块只有一个兜底分支时，「买过但到期」和「未验证 / 被封禁」会渲染成同一句
    「当前账号状态不支持使用试用额度」—— 到期用户的 user_auth.status 明明是 active，
    他会去找客服解封一个根本没被封的账号，而真正的恢复动作是页面上方的续费。
    """
    _register(client, "trial-expired-msg@example.com")
    _make_paid("trial-expired-msg@example.com", "plus-solo-1m", days_left=-1)

    body = client.get("/dashboard").text
    # 试用区块必须给出「到期」这个真实原因（而不是账号状态）
    assert "注册赠额不用于续费" in body, "试用区块没有说明到期才是原因"
    assert "当前账号状态不支持使用试用额度" not in body
    # 恢复路径仍在页面上：续费入口
    assert "续费" in body


def test_unverified_account_keeps_the_account_state_reason(client):
    """未验证账号的原因**不能**被上一条改动带偏：它确实是账号状态问题。"""
    email = "trial-unverified-msg@example.com"
    store.create_user_with_trial(
        email, password_hash="pbkdf2_sha256$1$00$00", seed="seed-unverified-msg",
        status="unverified", trial_tier=trials.TRIAL_TIER,
        trial_total=trials.SIGNUP_TRIAL_COUNT,
    )
    globals.seed_map["seed-unverified-msg"] = {"token": "", "plan_type": None,
                                               "conversations": []}
    from gateway.user import _issue_session
    client.cookies.set(configs.user_session_cookie, _issue_session(email, pw_version=1))

    body = client.get("/dashboard").text
    assert "当前账号状态不支持使用试用额度" in body
    assert "注册赠额不用于续费" not in body


@pytest.mark.parametrize("status,slug", [
    ("unverified", "unverified"),
    # email suffix must NOT contain "banned" — the nav renders the email address
    # and the assertion checks the full body for that word.
    ("banned", "blocked"),
])
def test_non_active_account_no_trial_entry_no_banned_label(client, status, slug):
    """未验证 / 被封禁账号：不显示试用入口，不把状态标为 banned。"""
    email = f"trial-{slug}@example.com"
    # 绕过注册流程直接建账号（注册强制 active/unverified，需直接写库）
    store.create_user_with_trial(
        email,
        password_hash="pbkdf2_sha256$1$00$00",
        seed=f"seed-{slug}",
        status=status,
        trial_tier=trials.TRIAL_TIER,
        trial_total=trials.SIGNUP_TRIAL_COUNT,
    )
    globals.seed_map[f"seed-{slug}"] = {"token": "", "plan_type": None, "conversations": []}

    # 注入会话 cookie（绕过密码登录）：使用与 gateway.user 相同的 HMAC 签名逻辑。
    # app 已在模块顶部导入，gateway.user 已随之加载，无需重新 import。
    from gateway.user import _issue_session
    session_token = _issue_session(email, pw_version=1)
    client.cookies.set(configs.user_session_cookie, session_token)

    resp = client.get("/dashboard")
    # 可能跳转回登录或展示不可用态；关键是不出现试用入口和 banned 标签
    if resp.status_code == 200:
        body = resp.text
        assert "开始试用" not in body
        assert "banned" not in body
        assert "封禁" not in body


def test_db_failure_renders_unavailable_not_zero(client, monkeypatch):
    """DB 故障：渲染明确的不可用信息，不展示虚假的零余额。"""
    _register(client, "trial-dberr@example.com")

    original_trial_state = trials.trial_state

    def _boom(email, strict=False):
        raise store.StoreError("database is locked")

    monkeypatch.setattr(trials, "trial_state", _boom)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text

    # 显示明确的不可用提示
    assert "暂时无法查询" in body or "请稍后刷新" in body or "unavailable" in body.lower()
    # 绝不展示「剩余 0 次」假余额
    assert "剩余 0 次" not in body
    assert "共 0 次" not in body
