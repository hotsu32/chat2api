"""Plus 免费试用：授予 / 预留 / 结算 的持久化与恰好一次语义。

试用余额是**钱**，不是统计量。所以这里测的全是「不能多扣也不能少扣」：

  - 授予与建号同一事务，注册成功就一定有额度，赠送失败则整笔回滚；
  - 一次生成 = 一次预留 + 一次终态（成功结算 / 失败释放），重复终态只算一次；
  - 第 4 次并发预留必须被拒，哪怕四个线程同时抢；
  - ``reserve`` 的三种结局彼此可分：预留 id / None（不走试用账）/ ``TrialDenied``；
  - 数据层故障一律 ``StoreError``，绝不伪装成「付费用户」或「运营者」；
  - 计数全在 SQLite 里，进程重启后余额不变（不能只靠内存计数器）。

口径全部走 ``utils.trials`` 的公开 API，不断言表结构 —— 网关两条流式入口将来要按
这个 API 接线，测试必须钉住的是行为契约。
"""
import sqlite3
import threading

import pytest

import utils.entitlements as entitlements
import utils.store as store
import utils.trials as trials
from utils.store import StoreError
from utils.trials import TrialDenied


TRIAL_EMAIL = "trial-user@example.com"
TRIAL_SEED = "seed-trial-user"


def _register_like(db, email=TRIAL_EMAIL, seed=TRIAL_SEED, status="active"):
    """走真实注册用的原子 DAO：建 user_auth 行 + 发试用，同一事务。"""
    db.create_user_with_trial(
        email, password_hash="pbkdf2_sha256$1$00$00", seed=seed, status=status,
        trial_tier=trials.TRIAL_TIER, trial_total=trials.SIGNUP_TRIAL_COUNT,
    )
    return email, seed


def _paid_order(db, email, plan_id="plus-shared-1m", expires_at=2_000_000_000):
    order_id = f"ord-{email}-{plan_id}"
    db.create_order(order_id, email, plan_id, "39", status="pending")
    db.activate_order(order_id, expires_at)
    return order_id


def _break_db(monkeypatch):
    """让所有 store 查询抛库级异常（模拟锁库 / 磁盘满 / 文件损坏）。"""
    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "_connect", _boom)


# --------------------------------------------------------------- 授予（grant）

def test_raw_dao_user_has_no_trial(db):
    """裸 DAO 建的账号不自动获得试用 —— 授予是注册路径的动作，不是建行的副作用。"""
    db.upsert_user_auth("dao-only@example.com", password_hash="x", seed="seed-dao-only",
                        status="active")
    assert trials.trial_remaining("dao-only@example.com") == 0
    assert trials.trial_tier("dao-only@example.com") == ""
    with pytest.raises(trials.TrialDenied):
        trials.reserve("seed-dao-only")
    assert entitlements.effective_tier("seed-dao-only") == ""


def test_signup_grant_gives_three_plus_trials(db):
    email, seed = _register_like(db)
    state = trials.trial_state(email)
    assert state["granted"] is True
    assert state["tier"] == "plus"
    assert state["total"] == trials.SIGNUP_TRIAL_COUNT == 3
    assert state["used"] == 0
    assert state["reserved"] == 0
    assert state["remaining"] == 3
    assert entitlements.effective_tier(seed) == "plus"


@pytest.mark.parametrize('column,value', [('seed', 'stale-seed'), ('tier', 'pro')])
def test_grant_must_match_current_seed_and_plus_tier(db, column, value):
    email, seed = _register_like(db)
    with store._connect() as conn:
        conn.execute(f'UPDATE trial_grants SET {column}=? WHERE email=?', (value, email))
    assert trials.trial_tier(email, strict=True) == ''
    with pytest.raises(TrialDenied):
        trials.reserve(seed)
    assert store.count_open_trial_reservations(email, strict=True) == 0


def test_registration_dao_is_atomic_user_and_grant(db):
    """建号与赠额同一事务：赠额失败则**连用户行一起回滚**，不留下无额度的僵尸账号。

    用重复的 grant 主键制造赠额失败：先手工插一条 grant，再用同 email 建号。
    """
    db.create_trial_grant("atomic@example.com", "seed-pre", "plus", 3)
    with pytest.raises(StoreError):
        db.create_user_with_trial(
            "atomic@example.com", password_hash="x", seed="seed-atomic",
            status="active", trial_tier="plus", trial_total=3,
        )
    # 用户行不能留下 —— 注册要么整笔成功，要么整笔失败
    assert db.get_user_auth("atomic@example.com") is None


def test_registration_dao_rejects_duplicate_email(db):
    _register_like(db, email="dup@example.com", seed="seed-dup-1")
    with pytest.raises(StoreError):
        db.create_user_with_trial(
            "dup@example.com", password_hash="x", seed="seed-dup-2",
            status="active", trial_tier="plus", trial_total=3,
        )
    assert db.get_user_auth("dup@example.com")["seed"] == "seed-dup-1"


def test_grant_survives_module_reload(db):
    """余额落在 SQLite 里：重新导入模块（模拟进程重启）后读数不变。"""
    import importlib

    email, _seed = _register_like(db)
    rid = trials.reserve(TRIAL_SEED)
    assert trials.settle(rid, TRIAL_SEED) is True

    importlib.reload(trials)
    assert trials.trial_remaining(email) == 2


# ------------------------------------------------------- 预留 / 结算 / 释放

def test_reserve_then_settle_consumes_exactly_one(db):
    email, seed = _register_like(db)
    rid = trials.reserve(seed)
    assert rid
    # 预留期间余额已被占住，避免并发多开把同一次额度花两遍
    assert trials.trial_state(email)["reserved"] == 1
    assert trials.trial_remaining(email) == 2

    assert trials.settle(rid, seed) is True
    state = trials.trial_state(email)
    assert state["used"] == 1
    assert state["reserved"] == 0
    assert state["remaining"] == 2


def test_duplicate_settle_consumes_once(db):
    """重复终态（SSE 终止事件投递两次 / 重试）只扣一次。"""
    email, seed = _register_like(db)
    rid = trials.reserve(seed)
    assert trials.settle(rid, seed) is True
    assert trials.settle(rid, seed) is False  # 第二次不是本次调用消费的
    assert trials.trial_remaining(email) == 2


def test_failed_generation_releases_reservation(db):
    """上游报错 / 空流 / 断连 → 释放预留，余额原样退回。"""
    email, seed = _register_like(db)
    rid = trials.reserve(seed)
    assert trials.release(rid, seed) is True
    assert trials.trial_remaining(email) == 3
    assert trials.trial_state(email)["used"] == 0


def test_release_after_settle_does_not_refund(db):
    """已结算的预留不能再被释放退款（终态不可逆）。"""
    email, seed = _register_like(db)
    rid = trials.reserve(seed)
    assert trials.settle(rid, seed) is True
    assert trials.release(rid, seed) is False
    assert trials.trial_remaining(email) == 2


def test_settle_after_release_does_not_consume(db):
    """已释放的预留不能被迟到的成功事件重新扣走。"""
    email, seed = _register_like(db)
    rid = trials.reserve(seed)
    assert trials.release(rid, seed) is True
    assert trials.settle(rid, seed) is False
    assert trials.trial_remaining(email) == 3


def test_fourth_reservation_denied(db):
    email, seed = _register_like(db)
    for _ in range(3):
        rid = trials.reserve(seed)
        assert rid
        assert trials.settle(rid, seed) is True
    assert trials.trial_remaining(email) == 0
    with pytest.raises(trials.TrialDenied) as exc:
        trials.reserve(seed)
    assert exc.value.reason == "exhausted"
    assert trials.trial_tier(email) == ""
    assert entitlements.effective_tier(seed) == ""


def test_concurrent_fourth_reservation_denied(db):
    """四个线程同时抢第 4 次：恰好 3 个拿到预留，第 4 个拿到明确的拒绝。"""
    _email, seed = _register_like(db)
    granted, denied = [], []
    barrier = threading.Barrier(4)

    def _grab():
        barrier.wait()
        try:
            granted.append(trials.reserve(seed))
        except trials.TrialDenied:
            denied.append(1)

    threads = [threading.Thread(target=_grab) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 3, f"应恰好放出 3 个预留，实际 {len(granted)}"
    assert len(set(granted)) == 3, "预留 id 必须互不相同"
    assert len(denied) == 1, "第 4 个必须拿到 TrialDenied，而不是静默的 None"


# ------------------------------------------------- enforce → reserve 竞态

def test_third_inflight_reservation_does_not_revoke_own_tier(db):
    """第 3 次请求自己把自己拒掉的经典坑。

    ``enforce_tier`` 先判档位再预留。若档位判据用 ``remaining``（已扣在途预留），
    前两次尚未结算时 remaining=1，第三次预留后 remaining=0 —— 同一次请求在
    「预留成功」之后再被问一次档位就会答「无权益」，把自己踢出去。

    准入档位必须用 ``total - used``（只认已结算的消费），在途预留不参与。
    """
    email, seed = _register_like(db)
    r1 = trials.reserve(seed)
    r2 = trials.reserve(seed)
    r3 = trials.reserve(seed)
    assert r1 and r2 and r3

    # 三次都还在途：显示余额为 0，但档位仍然成立（没有任何一次已结算）
    assert trials.trial_state(email)["remaining"] == 0
    assert trials.trial_state(email)["admission_balance"] == 3
    assert trials.trial_tier(email) == "plus"
    assert entitlements.effective_tier(seed) == "plus"

    # 第 4 次仍被原子预留拒绝 —— 准入由 reservation 决定，不由档位判据决定
    with pytest.raises(trials.TrialDenied):
        trials.reserve(seed)


def test_enforce_then_reserve_race_denies_the_loser(db):
    """两个请求同时通过档位检查，只剩 1 次额度：赢家拿预留，输家必须拿到 TrialDenied。

    这正是「先 enforce 后 reserve」的竞态。若输家拿到 None，调用方会把它当成
    「付费用户，不用扣次数」而照常打上游 —— 一次免费的越权生成。
    """
    email, seed = _register_like(db)
    for _ in range(2):
        rid = trials.reserve(seed)
        assert trials.settle(rid, seed) is True
    assert trials.trial_state(email)["admission_balance"] == 1

    outcomes = []
    barrier = threading.Barrier(2)

    def _run():
        barrier.wait()
        # 模拟网关：先问档位（两边都会答 plus），再预留
        tier = trials.trial_tier(email)
        try:
            outcomes.append((tier, "granted" if trials.reserve(seed) else "not_trial"))
        except trials.TrialDenied:
            outcomes.append((tier, "denied"))

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [o[0] for o in outcomes] == ["plus", "plus"], "两边都应通过档位检查"
    results = sorted(o[1] for o in outcomes)
    assert results == ["denied", "granted"], f"必须一赢一明确拒绝，实际 {results}"


# ------------------------------------------------------------ 归属与资格

def test_settlement_requires_seed(db):
    """settle / release 必须带 seed —— 可选归属校验等于默认不校验。"""
    _register_like(db)
    rid = trials.reserve(TRIAL_SEED)
    with pytest.raises(TypeError):
        trials.settle(rid)
    with pytest.raises(TypeError):
        trials.release(rid)


def test_reservation_is_seed_bound(db):
    """预留 id 绑定 seed：换一个 seed 结算不了别人的预留。"""
    _register_like(db)
    _register_like(db, email="other@example.com", seed="seed-other")

    rid = trials.reserve(TRIAL_SEED)
    assert trials.settle(rid, "seed-other") is False
    assert trials.release(rid, "seed-other") is False
    assert trials.trial_state(TRIAL_EMAIL)["reserved"] == 1
    assert trials.settle(rid, TRIAL_SEED) is True


def test_settlement_rejects_seed_reassigned_away_from_owner(db):
    """归属在 SQL 里对 user_auth 复核：seed 已不属于该 email 时不能结算。

    seed 轮换 / 重新绑定之后，旧 seed 手里的预留 id 不该还能扣走这个账号的额度。
    """
    email, seed = _register_like(db)
    rid = trials.reserve(seed)
    db.upsert_user_auth(email, seed="seed-rotated")  # 账号换了新 seed
    assert trials.settle(rid, seed) is False
    assert trials.release(rid, seed) is False
    assert trials.trial_state(email)["used"] == 0


def test_unknown_reservation_id_is_rejected(db):
    _register_like(db)
    assert trials.settle("res-does-not-exist", TRIAL_SEED) is False
    assert trials.release("res-does-not-exist", TRIAL_SEED) is False


@pytest.mark.parametrize("status", ["unverified", "banned", "frozen"])
def test_non_active_user_cannot_consume_trial(db, status):
    """未验证 / 被封禁的账号不能消费试用，且必须是**明确拒绝**而非 None。"""
    email, seed = _register_like(db, email=f"{status}@example.com",
                                 seed=f"seed-{status}", status=status)
    with pytest.raises(trials.TrialDenied) as exc:
        trials.reserve(seed)
    assert exc.value.reason == "account_not_active"
    assert trials.trial_tier(email) == ""
    assert entitlements.effective_tier(seed) == ""


def test_expired_subscriber_reserve_is_denied_not_none(db):
    """曾付费但已到期：不回落到试用，且必须是明确拒绝（不是「付费免预留」）。"""
    email, seed = _register_like(db)
    _paid_order(db, email, expires_at=1)
    with pytest.raises(trials.TrialDenied) as exc:
        trials.reserve(seed)
    assert exc.value.reason == "subscription_expired"


def test_reserve_with_unknown_seed_returns_none(db):
    """无 user_auth 行 = 运营者 seed / 直传 token：不走试用账，返回 None。

    这是 ``enforce_tier`` 既有的 fail-open 边界，不能改成拒绝，否则运营主链路断掉。
    """
    assert trials.reserve("seed-nobody") is None
    assert trials.reserve("") is None


# ------------------------------------------------ 数据层故障不得伪装成放行

def test_reserve_raises_store_error_when_db_is_down(db, monkeypatch):
    """查库失败必须抛 StoreError —— 既不能当成「付费用户」，也不能当成「运营者」。

    两种伪装都是静默放行：调用方会以为这次不用扣次数而照常打上游。
    """
    _register_like(db)
    _break_db(monkeypatch)
    with pytest.raises(StoreError):
        trials.reserve(TRIAL_SEED)


def test_purchase_check_failure_does_not_masquerade_as_paid(db, monkeypatch):
    """订单查询失败不得被当成「买过」而静默跳过试用通道。"""
    _register_like(db)
    real_list_orders = store.list_orders

    def _boom(*a, **kw):
        raise StoreError("orders unavailable")
    monkeypatch.setattr(store, "list_orders", _boom)
    with pytest.raises(StoreError):
        trials.reserve(TRIAL_SEED)
    with pytest.raises(StoreError):
        trials.trial_tier(TRIAL_EMAIL, strict=True)

    monkeypatch.setattr(store, "list_orders", real_list_orders)
    assert trials.reserve(TRIAL_SEED)


def test_trial_state_strict_raises_on_db_failure(db, monkeypatch):
    _register_like(db)
    _break_db(monkeypatch)
    with pytest.raises(StoreError):
        trials.trial_state(TRIAL_EMAIL, strict=True)


def test_effective_tier_propagates_store_error(db, monkeypatch):
    """权益层不能把试用查询故障吞成「无权益」或「运营者不设限」。"""
    _register_like(db)
    _break_db(monkeypatch)
    with pytest.raises(StoreError):
        entitlements.effective_tier(TRIAL_SEED)


# --------------------------------------------- 孤儿预留回收（崩溃恢复）

def test_orphan_reservations_recovered_by_instance(db):
    """进程崩溃留下的在途预留必须能回收，否则额度被永久占住。

    回收依据是**归属进程实例**而不是时间：用超时回收会在一次长研究还在跑的时候
    把它的预留放掉，导致同一次生成扣两次或被并发挤掉。
    """
    import subprocess
    import uuid as _uuid

    email, seed = _register_like(db)
    rid = trials.reserve(seed)
    assert trials.trial_state(email)["remaining"] == 2

    # 用一个已退出子进程的 PID 构造符合 "<pid>:<uuid>" 格式的死亡实例 id。
    # os.kill(dead_pid, 0) 在该 PID 回收后会抛 ProcessLookupError，触发回收逻辑。
    proc = subprocess.Popen(["/usr/bin/true"])
    dead_pid = proc.pid
    proc.wait()
    dead_iid = f"{dead_pid}:{_uuid.uuid4()}"
    with store._connect() as conn:
        conn.execute(
            "UPDATE trial_reservations SET instance_id=? WHERE res_id=?",
            (dead_iid, rid),
        )

    recovered = trials.recover_orphan_reservations()
    assert recovered == 1
    assert trials.trial_state(email)["remaining"] == 3
    assert trials.trial_state(email)["used"] == 0


def test_recovery_does_not_touch_current_instance_reservations(db):
    """本进程自己还在跑的预留绝不能被回收 —— 那正是长研究任务的预留。"""
    email, seed = _register_like(db)
    rid = trials.reserve(seed)
    assert trials.recover_orphan_reservations() == 0
    assert trials.trial_state(email)["reserved"] == 1
    assert trials.settle(rid, seed) is True


def test_recovery_does_not_refund_terminal_reservations(db):
    """已结算 / 已释放的预留不参与回收，回收不产生第二次退款。"""
    import subprocess
    import uuid as _uuid

    email, seed = _register_like(db)
    settled = trials.reserve(seed)
    released = trials.reserve(seed)
    assert trials.settle(settled, seed) is True
    assert trials.release(released, seed) is True

    proc = subprocess.Popen(["/usr/bin/true"])
    dead_pid = proc.pid
    proc.wait()
    dead_iid = f"{dead_pid}:{_uuid.uuid4()}"
    with store._connect() as conn:
        conn.execute(
            "UPDATE trial_reservations SET instance_id=? WHERE res_id=? OR res_id=?",
            (dead_iid, settled, released),
        )

    assert trials.recover_orphan_reservations() == 0
    assert trials.trial_state(email)["used"] == 1
    assert trials.trial_state(email)["remaining"] == 2


# ------------------------------------------------------------- 权益优先级

def test_paid_entitlement_takes_precedence_over_trial(db):
    """买了 Pro 就按 Pro 算，试用不把人降级回 Plus。"""
    email, seed = _register_like(db)
    _paid_order(db, email, "pro-solo-1m")
    assert entitlements.effective_tier(seed) == "pro"


def test_purchase_does_not_burn_remaining_trials(db):
    """买了之后试用余额不被清零，只是不再作为权益来源。"""
    email, _seed = _register_like(db)
    _paid_order(db, email)
    assert trials.trial_state(email)["remaining"] == 3


def test_expired_subscription_does_not_fall_back_to_trial(db):
    """到期用户不能靠没用完的注册试用续命 —— 那是给新用户的，不是续费通道。"""
    email, seed = _register_like(db)
    _paid_order(db, email, expires_at=1)  # 早已过期
    assert trials.trial_remaining(email) == 3  # 余额还在
    assert trials.trial_tier(email) == ""      # 但不再构成权益
    assert entitlements.effective_tier(seed) == ""


def test_purchased_user_reserve_returns_none(db):
    """付费用户不占试用额度，且这是「不走试用账」而非「被拒」—— 返回 None。"""
    email, seed = _register_like(db)
    _paid_order(db, email)
    assert trials.reserve(seed) is None


def test_operator_seed_without_user_auth_stays_unrestricted(db):
    """无 user_auth 行的运营者 seed 仍然 fail-open，试用逻辑不能改这条契约。"""
    assert entitlements.effective_tier("seed-operator-no-row") is None


# ------------------------------------------------------------------ 日志

def test_new_trial_logs_carry_no_identifiers(db, monkeypatch, caplog):
    """新增日志不得写入 email / seed / 预留 id / 异常原文。"""
    import logging

    caplog.set_level(logging.DEBUG)
    email, seed = _register_like(db)
    rid = trials.reserve(seed)
    trials.settle(rid, seed)
    try:
        _break_db(monkeypatch)
        trials.trial_state(email)
    except StoreError:
        pass

    blob = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (email, seed, rid, "database is locked"):
        assert secret not in blob, f"日志泄露了 {secret!r}"
