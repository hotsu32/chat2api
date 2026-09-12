"""号池分档契约：授权范围是硬边界，不是「优先级」。

本文件只测 ``chatgpt/authorization.py`` 的选号与粘性复核，不碰权益推导本身
（那是 ``tests/test_entitlements.py`` 的地盘）。权益数据经 ``store`` 真表构造，
与 ``test_entitlements.py`` 同一套口径，避免测到实现细节而非行为。

钉死四条：
  1. 显式请求的档位/号组用尽 = 空手而归，绝不跨档借号（Plus/Pro 不被静默降级，
     Free 也不越级去吃付费号）。
  2. 空号组（``[]``）= 一个号都没授权，不等于「不设限」。
  3. 粘性绑定每次都要对当前授权范围复核 —— 降档/过期后旧绑定不能继续生效。
  4. 权益查不出来时不分号（fail-closed），不能把一次锁库变成全员随便选号。
"""
import time

import pytest
from fastapi import HTTPException

import utils.globals as globals
import utils.plans as plans
import utils.store as store
from chatgpt import authorization as auth

_PW = "pbkdf2_sha256$1$00$00"


@pytest.fixture(autouse=True)
def _reset_globals(db):
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.antiban_dead_tokens.clear()
    globals._plan_synced.clear()
    yield
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.antiban_dead_tokens.clear()
    globals._plan_synced.clear()


def _account(token, plan_type, status="healthy"):
    store.upsert_account(token, plan_type=plan_type, status=status)
    return token


def _saas_user(email, seed, status="active"):
    store.upsert_user_auth(email, password_hash=_PW, seed=seed, status=status)
    store.upsert_user(seed, status='active')


def _paid(email, plan_id, days_left=30):
    detail = plans.plan_detail(plan_id)
    order_id = f"ord-{email}-{plan_id}-{days_left}"
    store.create_order(order_id, email, detail["id"], str(detail["price"]), status="pending")
    store.activate_order(order_id, int(time.time()) + days_left * 86400)
    return order_id


def _pick_max(seq):
    """Deterministic stand-in for random.choice over account-row dicts."""
    return max(seq, key=lambda d: d["token"])


# ------------------------------------------------- 1 显式档位不跨档回退

def test_empty_pro_pool_never_falls_back_to_plus():
    """协调者 RED 用例：Pro 号组为空时，绝不把 Plus 号顶上来。"""
    _account("plus-only", "plus")
    assert auth._pick_healthy_account(plan_types=["pro"]) == ""


def test_explicit_tier_does_not_downgrade_to_another_pool():
    _account("plus-1", "plus")
    _account("free-1", "free")
    assert auth._pick_healthy_account("pro") == ""


def test_unknown_tier_does_not_borrow_from_other_pools():
    """未知档位是「请求了一个我们没有的号组」，不是「随便给一个」。"""
    _account("plus-1", "plus")
    _account("free-1", "free")
    assert auth._pick_healthy_account("team") == ""


def test_explicit_free_tier_does_not_consume_paid_accounts():
    """Free 是历史兼容状态，不得越级吃掉付费号池的容量。"""
    _account("plus-1", "plus")
    assert auth._pick_healthy_account("free") == ""


def test_multi_tier_scope_picks_within_scope_only(monkeypatch):
    """Pro 档授权 plus+pro 两个号组：范围内可选，范围外不可选。"""
    _account("plus-1", "plus")
    _account("pro-1", "pro")
    _account("free-1", "free")
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    assert auth._pick_healthy_account(plan_types=["plus", "pro"]) == "pro-1"


# ------------------------------------------------- 2 空号组 ≠ 不设限

def test_empty_plan_types_authorizes_nothing():
    """``[]`` 是「一个号都没授权」；历史实现把它当 falsy 走成了不设限。"""
    _account("plus-1", "plus")
    _account("free-1", "free")
    assert auth._pick_healthy_account(plan_types=[]) == ""


def test_no_tier_request_keeps_legacy_operator_fallback(monkeypatch):
    """兜底路径（既没档位也没号组）保持历史行为：先 free 池，再任意健康号。

    这条是运营者 seed / 直传 token 的老链路，不能被本次收紧误伤。
    """
    _account("free-1", "free")
    _account("plus-1", "plus")
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    assert auth._pick_healthy_account() == "free-1"
    store.upsert_account("free-1", status="disabled")
    assert auth._pick_healthy_account() == "plus-1"


# ------------------------------------------------- 3 权益驱动的分配

def test_plus_entitlement_never_binds_free_account():
    """Plus 用户 + 只剩 Free 号 = 分不到号，而不是塞一个 Free 号糊弄过去。"""
    _account("free-1", "free")
    _saas_user("plus@example.com", "seed-plus")
    _paid("plus@example.com", "plus-solo-1m")
    assert auth._resolve_seed_account("seed-plus") == ""
    assert "seed-plus" not in globals.seed_map


def test_plus_entitlement_binds_plus_account(monkeypatch):
    _account("free-1", "free")
    _account("plus-1", "plus")
    _saas_user("ok@example.com", "seed-ok")
    _paid("ok@example.com", "plus-solo-1m")
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    assert auth._resolve_seed_account("seed-ok") == "plus-1"
    assert globals.seed_map["seed-ok"]["token"] == "plus-1"


def test_expired_user_cannot_allocate_account():
    """到期 seed 不再领新号（不占实时容量），但也不删它的历史绑定。"""
    _account("plus-1", "plus")
    _saas_user("exp@example.com", "seed-exp")
    _paid("exp@example.com", "plus-solo-1m", days_left=-1)
    globals.seed_map["seed-exp"] = {
        "token": "plus-1", "plan_type": "plus", "conversations": ["conv-old"],
    }
    store.upsert_user('seed-exp', current_account='plus-1', plan_type='plus')
    store.upsert_conversation('conv-old', 'seed-exp', 'plus-1', 'Example', '1', '2')
    assert auth._resolve_seed_account("seed-exp") == ""
    # 历史归属保留：冻结不是删号
    assert globals.seed_map["seed-exp"]["conversations"] == ["conv-old"]
    assert globals.seed_map["seed-exp"]["token"] == "plus-1"


def test_registered_but_unpaid_user_cannot_allocate_account():
    _account("plus-1", "plus")
    _saas_user("new@example.com", "seed-new")
    assert auth._resolve_seed_account("seed-new") == ""


def _operator_import(seed, token, tier):
    """Operator import (``POST /seedtoken``): in-memory binding + grant marker.

    Without the explicit marker the seed is not an operator — see
    ``tests/test_unknown_seed_authorization.py``.
    """
    globals.seed_map[seed] = {"token": token, "plan_type": tier, "conversations": []}
    globals.persist_seed(seed)
    store.upsert_user(seed, status=auth.OPERATOR_SEED_STATUS)
    return seed


def test_operator_seed_needs_a_persisted_grant(monkeypatch):
    """「没有 user_auth 行」不再是「运营者，随便挑号」：授权来自显式导入。"""
    _account("plus-1", "plus")
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    # 没人导入过这个名字 —— 一个号都不给。
    assert auth._resolve_seed_account("seed-operator") == ""
    assert "seed-operator" not in globals.seed_map
    # 导入之后按声明的号组分配，运营者链路保持可用。
    _operator_import("seed-operator", "", "plus")
    assert auth._resolve_seed_account("seed-operator") == "plus-1"


# ------------------------------------------------- 3 粘性绑定的复核

def test_sticky_binding_revalidated_against_authorized_tiers(monkeypatch):
    """旧绑定落在授权范围外（降档/历史遗留）时必须重选，不能凭「还健康」留着。"""
    _account("free-1", "free")
    _account("plus-1", "plus")
    _saas_user("re@example.com", "seed-re")
    _paid("re@example.com", "plus-solo-1m")
    globals.seed_map["seed-re"] = {"token": "free-1", "plan_type": "free", "conversations": []}
    store.upsert_user('seed-re', current_account='free-1', plan_type='free')
    monkeypatch.setattr(auth.random, "choice", _pick_max)
    assert auth._resolve_seed_account("seed-re") == "plus-1"
    assert globals.seed_map["seed-re"]["plan_type"] == "plus"


def test_sticky_binding_out_of_scope_with_empty_pool_yields_nothing():
    """范围外的旧绑定 + 范围内无号 = 空手而归，不得退回那个越权的旧号。"""
    _account("free-1", "free")
    _saas_user("gone@example.com", "seed-gone")
    _paid("gone@example.com", "plus-solo-1m")
    globals.seed_map["seed-gone"] = {"token": "free-1", "plan_type": "free", "conversations": []}
    store.upsert_user('seed-gone', current_account='free-1', plan_type='free')
    assert auth._resolve_seed_account("seed-gone") == ""


def test_sticky_binding_kept_when_in_scope():
    _account("plus-1", "plus")
    _account("plus-2", "plus")
    _saas_user("keep@example.com", "seed-keep")
    _paid("keep@example.com", "plus-solo-1m")
    globals.seed_map["seed-keep"] = {"token": "plus-1", "plan_type": "plus", "conversations": []}
    store.upsert_user('seed-keep', current_account='plus-1', plan_type='plus')
    assert auth._resolve_seed_account("seed-keep") == "plus-1"


def test_switch_respects_authorized_tiers():
    """强制切号同样受授权范围约束（否则切换成了绕过分池的后门）。"""
    _account("free-1", "free")
    _saas_user("sw@example.com", "seed-sw")
    _paid("sw@example.com", "plus-solo-1m")
    globals.seed_map["seed-sw"] = {"token": "", "plan_type": "plus", "conversations": []}
    assert auth.switch_seed_account("seed-sw") == ""


def test_switch_denied_without_entitlement():
    _account("plus-1", "plus")
    _saas_user("noent@example.com", "seed-noent")
    assert auth.switch_seed_account("seed-noent") == ""


# ------------------------------------------------- 4 权益查询失败 fail-closed

def _raise_store_error(_seed):
    raise store.StoreError("database is locked")


def test_seed_plan_types_propagates_store_error(monkeypatch):
    """查不出权益必须炸出来，不能静默返回 None（= 不设限）。"""
    import utils.tiers as tiers
    monkeypatch.setattr(tiers, "resolve_user_tier", _raise_store_error)
    with pytest.raises(store.StoreError):
        auth._seed_plan_types("seed-boom")


def test_store_error_allocates_nothing(monkeypatch):
    import utils.tiers as tiers
    monkeypatch.setattr(tiers, "resolve_user_tier", _raise_store_error)
    _account("plus-1", "plus")
    with pytest.raises(HTTPException) as failure:
        auth._resolve_seed_account("seed-boom")
    assert failure.value.status_code == 503
    assert "seed-boom" not in globals.seed_map
    with pytest.raises(HTTPException) as failure:
        auth.switch_seed_account("seed-boom")
    assert failure.value.status_code == 503


def test_store_error_does_not_keep_sticky_binding(monkeypatch):
    """分不清「运营者」和「过期用户」的时候，连老绑定也不放行。"""
    import utils.tiers as tiers
    _account("plus-1", "plus")
    globals.seed_map["seed-boom"] = {"token": "plus-1", "plan_type": "plus", "conversations": []}
    monkeypatch.setattr(tiers, "resolve_user_tier", _raise_store_error)
    before = dict(globals.seed_map["seed-boom"])
    with pytest.raises(HTTPException) as failure:
        auth._resolve_seed_account("seed-boom")
    assert failure.value.status_code == 503
    assert globals.seed_map["seed-boom"] == before


def test_empty_pool_seed_request_cannot_be_used_as_an_upstream_token(monkeypatch):
    monkeypatch.setattr(auth.configs, "auto_seed", True)
    _saas_user("expired@example.com", "seed-expired")
    assert auth.get_req_token("seed-expired", seed="seed-expired") == ""


def test_operator_sticky_binding_respects_declared_tier():
    _account("free-1", "free")
    _account("plus-1", "plus")
    _operator_import("seed-operator", "free-1", "plus")
    globals.seed_map["seed-operator"]["conversations"] = ["old-conversation"]
    assert auth._resolve_seed_account("seed-operator") == "plus-1"
    assert globals.seed_map["seed-operator"]["conversations"] == ["old-conversation"]


@pytest.mark.parametrize("status", ["degraded", "unhealthy", "dead", "disabled"])
def test_unavailable_sticky_account_does_not_accept_new_work(status):
    _account("plus-old", "plus", status=status)
    _operator_import("seed-operator", "plus-old", "plus")
    assert auth._resolve_seed_account("seed-operator") == ""
