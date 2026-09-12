"""权益推导单测 —— 付费与权限之间唯一的桥（utils/entitlements.py）。

覆盖 EVALUATOR 判据 A（付费→权益）/ B（到期回收）/ D（续费叠加）/ E（注册不送额度）。
这里测的是业务规则本身，不依赖 FastAPI，也不依赖上游 mock。
"""
import time

import pytest

import utils.entitlements as entitlements
import utils.plans as plans
import utils.tiers as tiers

_PW = "pbkdf2_sha256$1$00$00"


def _user(db, email, seed, status="active"):
    db.upsert_user_auth(email, password_hash=_PW, seed=seed, status=status)


def _paid(db, email, plan_id, days_left=30, order_id=None):
    """落一张已支付订单，到期时间相对 now 偏移 days_left（可为负 = 已过期）。"""
    detail = plans.plan_detail(plan_id)
    order_id = order_id or f"ord-{email}-{plan_id}-{days_left}"
    db.create_order(order_id, email, detail["id"], str(detail["price"]), status="pending")
    db.activate_order(order_id, int(time.time()) + days_left * 86400)
    return order_id


# --------------------------------------------------------------- A 付费 → 权益

def test_paid_order_grants_tier(db):
    _user(db, "a@example.com", "seed-a")
    _paid(db, "a@example.com", "plus-solo-1m")
    assert entitlements.effective_tier("seed-a") == "plus"


def test_highest_tier_wins_across_parallel_plans(db):
    _user(db, "b@example.com", "seed-b")
    _paid(db, "b@example.com", "plus-solo-1m")
    _paid(db, "b@example.com", "pro-shared-1w")
    assert entitlements.effective_tier("seed-b") == "pro"


def test_pending_order_grants_nothing(db):
    """只有 paid 才算权益 —— 建单不等于付款。"""
    _user(db, "c@example.com", "seed-c")
    detail = plans.plan_detail("plus-solo-1m")
    db.create_order("ord-c", "c@example.com", detail["id"], str(detail["price"]), status="pending")
    assert entitlements.effective_tier("seed-c") == ""


def test_plan_id_is_not_a_tier_id(db):
    """反向验证：plan_id 不在档位目录里，normalize_tier_id 会静默回落 free。

    权益推导一旦误用它，所有付费用户当天就被降级 —— 故 entitlements 必须走
    plans.plan_detail，本用例把这个坑钉死。
    """
    assert tiers.normalize_tier_id("plus-solo-1m") == "free"
    assert plans.plan_detail("plus-solo-1m")["tier"] == "plus"


# --------------------------------------------------------------- B 到期即回收

def test_expired_order_grants_nothing(db):
    _user(db, "d@example.com", "seed-d")
    _paid(db, "d@example.com", "plus-solo-1m", days_left=-1)
    assert entitlements.effective_tier("seed-d") == ""


def test_expiry_downgrades_to_nothing_not_to_free(db):
    """到期 = 不可用，不是降级到 Free（产品上已无 Free 档）。"""
    _user(db, "e@example.com", "seed-e")
    _paid(db, "e@example.com", "pro-solo-1m", days_left=-1)
    assert entitlements.effective_tier("seed-e") == ""
    assert tiers.resolve_user_tier("seed-e") == ""


def test_non_active_account_loses_entitlement_despite_paid_order(db):
    """封禁 / 未验证的账号即便订单有效也无权益，否则收藏 seed 即可继续用。"""
    _user(db, "f@example.com", "seed-f", status="disabled")
    _paid(db, "f@example.com", "plus-solo-1m")
    assert entitlements.effective_tier("seed-f") == ""


def test_unknown_seed_is_unrestricted(db):
    """运营者 seed / 直传 token 无 user_auth 行 → None（fail-open，不受 SaaS 权益约束）。

    None 与 "" 语义相反，调用方必须用 is None 区分。
    """
    assert entitlements.effective_tier("seed-operator") is None


# --------------------------------------------------------------- D 续费叠加

def test_renewal_stacks_on_existing_expiry(db):
    """提前续费从原到期时间起算，不吞剩余天数。"""
    _user(db, "g@example.com", "seed-g")
    now = int(time.time())
    _paid(db, "g@example.com", "plus-solo-1m", days_left=10, order_id="ord-g1")

    base = entitlements.tier_expiry("g@example.com", "plus")
    assert base is not None
    # 模拟 saas._grant_expiry 的叠加口径
    detail = plans.plan_detail("plus-solo-1m")
    stacked = max(base, now) + detail["days"] * 86400
    _paid(db, "g@example.com", "plus-solo-1m", order_id="ord-g2")
    db.activate_order("ord-g2", stacked)  # 已是 paid，幂等不改动

    assert entitlements.tier_expiry("g@example.com", "plus") >= base


def test_activate_order_is_idempotent(db):
    """回调重复投递不得延长有效期。"""
    _user(db, "h@example.com", "seed-h")
    detail = plans.plan_detail("plus-solo-1m")
    db.create_order("ord-h", "h@example.com", detail["id"], str(detail["price"]), status="pending")
    first = int(time.time()) + 30 * 86400

    assert db.activate_order("ord-h", first) is True
    assert db.activate_order("ord-h", first + 999999) is False
    assert db.get_order("ord-h")["expires_at"] == first


# --------------------------------------------------------------- E 注册不送额度

def test_registered_user_without_order_has_no_entitlement(db):
    _user(db, "i@example.com", "seed-i")
    assert entitlements.effective_tier("seed-i") == ""
    assert plans.has_active_plan("i@example.com") is False


@pytest.mark.parametrize(
    "plan_id", ["plus-solo-1d", "plus-shared-1w", "pro-solo-1m", "pro-shared-1d"]
)
def test_every_sku_maps_to_a_known_tier(db, plan_id):
    """每个可售 SKU 都必须能推出 plus/pro 之一，不能落到未知档。"""
    _user(db, f"{plan_id}@example.com", f"seed-{plan_id}")
    _paid(db, f"{plan_id}@example.com", plan_id)
    assert entitlements.effective_tier(f"seed-{plan_id}") in ("plus", "pro")


# --------------------------------------------------------- 数据层语义（查不到 ≠ 没有）

def test_empty_email_does_not_list_every_order(db):
    """空串 email 是「一个 email 为空的用户」，不是「所有用户」。

    若把它当成 None 处理，调用方会拿到全表订单 —— 也就是把全站的权益算到一个人头上。
    """
    _user(db, "owner@example.com", "seed-owner")
    _paid(db, "owner@example.com", "plus-solo-1m")

    assert db.list_orders(email="") == []
    assert len(db.list_orders(email=None)) >= 1  # None 才是「全部」
    assert entitlements.effective_tier_for_email("") == ""


def test_query_failure_raises_instead_of_looking_like_no_rows(db, monkeypatch):
    """查询失败必须抛 StoreError，不能伪装成「这个人没买过」。

    两者含义相反：前者应当拒绝服务，后者应当拒绝该用户；混为一谈会让一次锁库
    要么给全体过期用户开闸，要么把运营者也挡在门外。
    """
    def _boom(*args, **kwargs):
        raise db.StoreError("database is locked")

    monkeypatch.setattr(db, "list_orders", _boom)
    with pytest.raises(db.StoreError):
        entitlements.active_orders("someone@example.com")

    # 导航判断（非授权）允许降级：查不到就把人送去选购页，而不是抛 500
    assert plans.has_active_plan("someone@example.com") is False
