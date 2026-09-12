"""Payment provider contract: a signed callback is not a licence to settle blindly.

``verify`` only proves *who* sent the callback. Everything that decides whether money
actually arrived for *this* order is checked server-side against the stored order:
amount, currency, provider transaction identity and replay. These tests pin each of
those checks, plus the type contract that stopped ``verify`` from being able to return
a bare order id.
"""

import sqlite3
import threading
from decimal import Decimal

import pytest

from utils import configs, payment, store


class _MetaGuard:
    """A connection proxy whose statements against the ``meta`` KV table fail.

    Simulates a locked / corrupt binding store *without* disturbing the rest of the
    callback path (the order lookup still works), so the tests below pin what happens
    when only the provider-transaction binding step is unavailable.
    """

    def __init__(self, conn, fail_on="any"):
        self._conn = conn
        self._fail_on = fail_on

    def execute(self, sql, *params):
        keyword = sql.strip().split(" ", 1)[0].upper()
        if "meta" in sql.lower() and self._fail_on in ("any", keyword.lower()):
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *params)

    # Context-manager dunders are looked up on the type, not via __getattr__.
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._conn.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def unavailable_binding(monkeypatch):
    """Install a ``meta``-only storage failure; ``fail_on`` selects select/insert/any."""
    real_connect = store._connect

    def _install(fail_on="any"):
        monkeypatch.setattr(store, "_connect", lambda: _MetaGuard(real_connect(), fail_on))

    return _install


def _raw_meta(key):
    """Read the binding straight from the file — the storage layer is stubbed out."""
    conn = sqlite3.connect(store._db_path())
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class _Provider:
    """Minimal non-mock provider double: signed payloads, structured results."""

    name = "fake"
    is_mock = False

    def begin(self, order):
        return {"provider": self.name, "order_id": order.get("order_id")}

    def verify(self, payload):
        return (payload or {}).get("callback")


def _callback(order_id, *, amount="99", currency=None, txn="txn-1", provider="fake"):
    return payment.ProviderCallback(
        order_id=order_id,
        transaction_id=txn,
        amount=amount,
        currency=currency if currency is not None else payment.expected_currency(),
        provider=provider,
    )


def _pending_order(order_id="ord_contract", amount="99"):
    store.create_order(order_id, "buyer@example.com", "plus-solo-1m", amount, status="pending")
    return store.get_order(order_id)


# --------------------------------------------------------------- amount handling

@pytest.mark.parametrize("value,expected", [
    ("99", Decimal("99")),
    ("99.00", Decimal("99")),
    (99, Decimal("99")),
    (" 12.5 ", Decimal("12.5")),
])
def test_normalize_amount_accepts_major_unit_decimals(value, expected):
    assert payment.normalize_amount(value) == expected


@pytest.mark.parametrize("value", [
    None, "", "abc", "-1", "1.234", True, False, "NaN", "Infinity", [], {},
])
def test_normalize_amount_rejects_everything_else(value):
    """不接受负价、超过两位小数、非有限值与布尔 —— 口径不符就是拒绝，不做四舍五入。"""
    assert payment.normalize_amount(value) is None


# --------------------------------------------------------------- callback contract

def test_callback_requires_order_and_transaction_identity():
    with pytest.raises(payment.PaymentError):
        payment.ProviderCallback(order_id="", transaction_id="txn", amount="1",
                                 currency="CNY", provider="fake")
    with pytest.raises(payment.PaymentError):
        payment.ProviderCallback(order_id="ord_1", transaction_id="", amount="1",
                                 currency="CNY", provider="fake")


def test_bare_order_id_is_no_longer_a_valid_verification_result():
    """旧契约（verify 回显 order_id 字符串）必须被判无效。"""
    with pytest.raises(payment.PaymentError) as exc:
        payment.accept_callback(_Provider(), {"callback": "ord_echoed"})
    assert exc.value.reason == "invalid_callback"


def test_callback_from_another_provider_is_rejected():
    with pytest.raises(payment.PaymentError) as exc:
        payment.accept_callback(_Provider(), {"callback": _callback("ord_1", provider="someone-else")})
    assert exc.value.reason == "provider_mismatch"


def test_mock_provider_never_accepts_a_callback():
    """mock 无签名可验：verify 恒为 None，因此不可能经回调路径结算。"""
    assert payment.MockProvider().verify({"order_id": "ord_x"}) is None
    with pytest.raises(payment.PaymentError) as exc:
        payment.accept_callback(payment.MockProvider(), {"order_id": "ord_x"})
    assert exc.value.reason == "invalid_callback"


# --------------------------------------------------------------- settlement checks

def test_matching_callback_returns_the_stored_order(db):
    order = _pending_order()
    verified = payment.validate_callback(_Provider(), _callback(order["order_id"]))
    assert verified["order_id"] == order["order_id"]
    assert verified["email"] == "buyer@example.com"


@pytest.mark.parametrize("amount,reason", [
    ("98", "amount_mismatch"),      # 少付
    ("100", "amount_mismatch"),     # 多付
    ("99.01", "amount_mismatch"),
    ("9900", "amount_mismatch"),    # 以分为单位的渠道没有适配成主单位
    ("free", "invalid_amount"),
])
def test_amount_must_equal_the_server_side_order_amount(db, amount, reason):
    order = _pending_order()
    with pytest.raises(payment.PaymentError) as exc:
        payment.validate_callback(_Provider(), _callback(order["order_id"], amount=amount))
    assert exc.value.reason == reason
    assert store.get_order(order["order_id"])["status"] == "pending"


def test_currency_must_match_the_configured_settlement_currency(db, monkeypatch):
    order = _pending_order()
    monkeypatch.setattr(configs, "payment_currency", "CNY")
    with pytest.raises(payment.PaymentError) as exc:
        payment.validate_callback(_Provider(), _callback(order["order_id"], currency="usd"))
    assert exc.value.reason == "currency_mismatch"

    with pytest.raises(payment.PaymentError) as exc:
        payment.validate_callback(_Provider(), _callback(order["order_id"], currency=""))
    assert exc.value.reason == "currency_mismatch"


def test_unknown_order_is_reported_as_such(db):
    with pytest.raises(payment.PaymentError) as exc:
        payment.validate_callback(_Provider(), _callback("ord_missing"))
    assert exc.value.reason == "unknown_order"


@pytest.mark.parametrize("status", ["failed", "expired"])
def test_closed_orders_do_not_accept_payment_facts(db, status):
    _pending_order("ord_closed")
    store.update_order_status("ord_closed", status)
    with pytest.raises(payment.PaymentError) as exc:
        payment.validate_callback(_Provider(), _callback("ord_closed"))
    assert exc.value.reason == "order_not_payable"


def test_one_provider_transaction_cannot_settle_another_order(db):
    """同一支付流水号被用来激活第二张单 —— 重放，拒绝。"""
    first = _pending_order("ord_first")
    second = _pending_order("ord_second")
    payment.validate_callback(_Provider(), _callback(first["order_id"], txn="txn-shared"))

    with pytest.raises(payment.PaymentError) as exc:
        payment.validate_callback(_Provider(), _callback(second["order_id"], txn="txn-shared"))
    assert exc.value.reason == "transaction_replay"


def test_replayed_callback_for_the_same_order_stays_accepted(db):
    """同一张单的重复投递要放行 —— 结算本身幂等，渠道会重发。"""
    order = _pending_order()
    for _ in range(3):
        payment.validate_callback(_Provider(), _callback(order["order_id"], txn="txn-dup"))


def test_transaction_identity_is_namespaced_per_provider(db):
    order = _pending_order("ord_ns")
    payment.validate_callback(_Provider(), _callback(order["order_id"], txn="txn-same"))

    other = type("OtherProvider", (_Provider,), {"name": "other"})()
    # 别的渠道的同一串流水号是另一回事，不该撞在一起
    assert payment.validate_callback(other, _callback(order["order_id"], txn="txn-same",
                                                      provider="other"))["order_id"] == order["order_id"]


# ------------------------------------------------------ binding storage failures

@pytest.mark.parametrize("fail_on", ["select", "insert"])
def test_binding_storage_failure_never_validates_the_order(db, unavailable_binding, fail_on):
    """绑定表读不了 / 写不了时，回调必须整体失败，而不是退回「当作没绑过」。

    这正是旧实现的 fail-open 缺口：``get_meta`` 吞掉异常返回 ``None``、``set_meta``
    吞掉异常静默返回，于是一次数据库打嗝 == 「这个流水号从没被用过」，回调被放行
    进入结算。存储故障与「未绑定」是两件不同的事实，必须区分。
    """
    order = _pending_order("ord_bind_fail")
    unavailable_binding(fail_on)

    with pytest.raises(store.StoreError):
        payment.validate_callback(_Provider(), _callback(order["order_id"], txn="txn-fail"))

    # 没有静默记下半截绑定，也没有任何状态流转
    assert _raw_meta("paytxn:fake:txn-fail") is None
    assert store.get_order(order["order_id"])["status"] == "pending"


def test_transaction_id_is_normalized_before_it_becomes_a_binding_key(db):
    """渠道对同一笔支付上报的空白变体必须落到同一个绑定 key 上。

    ``__post_init__`` 用 ``strip()`` 判非空却把原值留给 key 时，"txn-ws" 与 " txn-ws "
    是两个 key，同一笔钱可以各绑一张单 —— 校验和记账必须用同一个值。
    """
    first = _pending_order("ord_ws_first")
    second = _pending_order("ord_ws_second")

    payment.validate_callback(_Provider(), _callback(first["order_id"], txn="txn-ws"))
    with pytest.raises(payment.PaymentError) as exc:
        payment.validate_callback(_Provider(), _callback(second["order_id"], txn=" txn-ws "))
    assert exc.value.reason == "transaction_replay"


def test_unknown_binding_outcome_never_validates_the_order(db, monkeypatch):
    """契约外的绑定结果不得被当成「通过」：按数据层异常处理，让渠道重投。

    这条分支现在不可达（DAO 只返回三个约定值），但它必须是 fail-closed 的：将来有人
    加第四个结果却忘了在这里处理时，默认动作不能是结算。
    """
    order = _pending_order("ord_unknown_outcome")
    monkeypatch.setattr(store, "bind_payment_transaction", lambda *_a, **_kw: "something-new")

    with pytest.raises(store.StoreError):
        payment.validate_callback(_Provider(), _callback(order["order_id"]))
    assert store.get_order(order["order_id"])["status"] == "pending"


def test_concurrent_callbacks_bind_exactly_one_order(db):
    """同一个支付流水号被两个并发回调携带：只能有一张单被绑定/放行。

    旧实现分两步（先读后写），并发时两个回调都能读到「未绑定」再各自写入，
    同一个流水号就把两张单都结算了。
    """
    orders = [_pending_order(f"ord_race_{i}")["order_id"] for i in range(6)]
    barrier = threading.Barrier(len(orders))
    lock = threading.Lock()
    outcomes = {}

    def _worker(order_id):
        barrier.wait(timeout=10)
        try:
            payment.validate_callback(_Provider(), _callback(order_id, txn="txn-race"))
            outcome = "accepted"
        except payment.PaymentError as exc:
            outcome = exc.reason
        except store.StoreError:
            outcome = "store_error"
        with lock:
            outcomes[order_id] = outcome

    threads = [threading.Thread(target=_worker, args=(order_id,)) for order_id in orders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(outcomes) == len(orders)
    accepted = [order_id for order_id, outcome in outcomes.items() if outcome == "accepted"]
    assert len(accepted) == 1, outcomes
    assert list(outcomes.values()).count("transaction_replay") == len(orders) - 1, outcomes

    # 六个回调只留下一个绑定，且绑的正是被放行的那张单
    assert _raw_meta("paytxn:fake:txn-race") == accepted[0]


# --------------------------------------------------------------- mock remains local

@pytest.mark.parametrize("environment", ["production", "", "staging"])
def test_mock_provider_stays_unreachable_outside_local_environments(monkeypatch, environment):
    monkeypatch.setattr(configs, "app_env", environment, raising=False)
    monkeypatch.setattr(configs, "payment_provider", "mock")
    assert payment.get_provider() is None
    with pytest.raises(payment.PaymentError):
        payment.MockProvider().begin({"order_id": "ord_synthetic"})


def test_non_mock_provider_unknown_values_still_fail_closed(monkeypatch):
    monkeypatch.setattr(configs, "payment_provider", "wechat")
    # 尚未实现的渠道不得因为「名字看着像真的」而被放行
    assert payment.get_provider() is None
