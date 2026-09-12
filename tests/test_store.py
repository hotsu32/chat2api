"""store: SQLite DAO — CRUD, tier pool, conversations, usage, migration idempotency."""
import contextlib
import sqlite3
import threading

import pytest

import utils.store as store

ACCESS = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
REFRESH = "r" * 45


def test_init_db_idempotent(db):
    store.init_db()  # second call is a no-op
    assert store.get_meta("migrated") is None  # fresh DB, not yet migrated


def test_account_upsert_get_delete(db):
    store.upsert_account(ACCESS, token_type="AccessToken", plan_type="plus",
                         status="healthy", nickname="n1")
    acct = store.get_account(ACCESS)
    assert acct["plan_type"] == "plus"
    assert acct["status"] == "healthy"
    assert acct["nickname"] == "n1"
    # partial upsert must not clobber columns it did not receive
    store.upsert_account(ACCESS, plan_type="pro")
    assert store.get_account(ACCESS)["nickname"] == "n1"
    assert store.get_account(ACCESS)["plan_type"] == "pro"
    store.delete_account(ACCESS)
    assert store.get_account(ACCESS) is None


def test_account_status_default(db):
    store.upsert_account(ACCESS, token_type="AccessToken")
    assert store.get_account(ACCESS)["status"] == "healthy"


def test_account_by_plan(db):
    store.upsert_account("plus-1", plan_type="plus", status="healthy")
    store.upsert_account("plus-2", plan_type="plus", status="unhealthy")
    store.upsert_account("free-1", plan_type="free", status="healthy")
    assert [a["token"] for a in store.get_account_by_plan("plus")] == ["plus-1"]
    assert [a["token"] for a in store.get_healthy_accounts()] == ["plus-1", "free-1"]


def test_users_and_cascade(db):
    store.upsert_user("seed-a", plan_type="plus", current_account="plus-1")
    assert store.get_user("seed-a")["current_account"] == "plus-1"
    store.upsert_conversation("conv-1", "seed-a", "plus-1", "Hi", "t1", "t2")
    store.delete_user("seed-a")
    assert store.get_user("seed-a") is None
    assert store.list_seed_conversations("seed-a") == []


def test_conversation_account_tracking(db):
    store.upsert_conversation("conv-1", "seed-a", "acct-1", "Hi", "t1", "t2")
    store.upsert_conversation("conv-2", "seed-a", "acct-2", "Bye", "t1", "t3")
    convs = store.list_seed_conversations("seed-a")
    assert {c["conv_id"]: c["account"] for c in convs} == {"conv-1": "acct-1", "conv-2": "acct-2"}
    # replace rebuilds atomically
    store.replace_conversations([("conv-3", "seed-b", "acct-3", "T", "t1", "t1")])
    assert len(store.all_conversations()) == 1


def test_proxies(db):
    store.save_proxies([{"name": "p1", "proxy_url": "http://p1"}, {"proxy_url": ""}])
    proxies = store.list_proxies()
    assert len(proxies) == 1 and proxies[0]["proxy_url"] == "http://p1"


def test_usage_events(db):
    store.add_usage_events([
        ("seed-a", "acct-1", "conversation", 100),
        ("seed-a", "acct-2", "image", 101),
        ("seed-b", "acct-1", "conversation", 102),
    ])
    assert store.query_usage_count(seed="seed-a") == 2
    assert store.query_usage_count(account="acct-1") == 2
    assert store.query_usage_count() == 3
    assert store.query_usage(since=101)[0]["kind"] == "image"


def test_migrate_idempotent(db):
    seed_map = {"seed-a": {"token": ACCESS, "plan_type": "plus", "conversations": ["conv-1"]}}
    conv_map = {"conv-1": {"id": "conv-1", "title": "Hi", "create_time": "t1", "update_time": "t2"}}
    refresh_map = {ACCESS: {"token": "refreshed", "last_success_at": 123}}
    fp_map = {ACCESS: {"impersonate": "chrome119", "user-agent": "ua", "proxy_url": "http://p"}}
    routing = {
        "proxies": [{"name": "p1", "proxy_url": "http://p"}],
        "bindings": {ACCESS: {"group": "Group A", "proxy_name": "p1", "proxy_url": "http://p"}},
        "account_meta": {ACCESS: {"note": "hi"}},
    }
    store.migrate([ACCESS, REFRESH], [REFRESH], seed_map, conv_map, refresh_map, fp_map, routing)
    assert store.is_migrated() is True
    assert store.get_account(ACCESS)["token_type"] == "AccessToken"
    assert store.get_account(ACCESS)["status"] == "healthy"
    assert store.get_account(REFRESH)["status"] == "unhealthy"  # error token -> unhealthy
    assert store.get_user("seed-a")["current_account"] == ACCESS
    assert store.list_seed_conversations("seed-a")[0]["conv_id"] == "conv-1"
    assert store.list_proxies()[0]["proxy_url"] == "http://p"
    # second migrate is a no-op (idempotent via meta flag)
    store.migrate([ACCESS, REFRESH], [REFRESH], seed_map, conv_map, refresh_map, fp_map, routing)
    assert len(store.list_accounts()) == 2


def test_load_all_roundtrip(db):
    seed_map = {"seed-a": {"token": ACCESS, "plan_type": "plus", "conversations": ["conv-1"]}}
    conv_map = {"conv-1": {"id": "conv-1", "title": "Hi", "create_time": "t1", "update_time": "t2"}}
    routing = {"bindings": {ACCESS: {"group": "Group A", "proxy_name": "p1", "proxy_url": "http://p"}}}
    store.migrate([ACCESS, REFRESH], [REFRESH], seed_map, conv_map, {}, {}, routing)
    loaded = store.load_all()
    assert set(loaded["token_list"]) == {ACCESS, REFRESH}
    assert loaded["error_token_list"] == [REFRESH]
    assert loaded["seed_map"]["seed-a"]["token"] == ACCESS
    assert "conv-1" in loaded["conversation_map"]
    assert loaded["routing_config"]["bindings"][ACCESS]["proxy_url"] == "http://p"


def test_reload_preserves_frozen_seed_state_and_history(db):
    store.upsert_user('frozen-seed', plan_type='plus', current_account='original-account', status='frozen')
    store.upsert_conversation('historical-conversation', 'frozen-seed', 'original-account', 'History', 't1', 't2')
    loaded = store.load_all()
    assert loaded['seed_map']['frozen-seed']['status'] == 'frozen'
    assert loaded['seed_map']['frozen-seed']['token'] == 'original-account'
    assert loaded['seed_map']['frozen-seed']['conversations'] == ['historical-conversation']


# ---------------------------------------------------- payment transaction binding

class _MetaGuard:
    """Connection proxy whose statements against the ``meta`` KV table fail."""

    def __init__(self, conn, fail_on):
        self._conn = conn
        self._fail_on = fail_on

    def execute(self, sql, *params):
        keyword = sql.strip().split(" ", 1)[0].lower()
        if "meta" in sql.lower() and self._fail_on in ("any", keyword):
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *params)

    # Context-manager dunders are looked up on the type, not via __getattr__.
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._conn.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_bind_payment_transaction_claims_a_fresh_key(db):
    key = "paytxn:fake:txn-1"
    assert store.bind_payment_transaction(key, "ord-a") == store.PAYMENT_TXN_BOUND
    assert store.get_meta(key) == "ord-a"


def test_bind_payment_transaction_same_order_is_idempotent(db):
    """渠道重投同一笔回调：同一张单再认领一次不算冲突，也不改写绑定值。"""
    key = "paytxn:fake:txn-dup"
    assert store.bind_payment_transaction(key, "ord-a") == store.PAYMENT_TXN_BOUND
    assert store.bind_payment_transaction(key, "ord-a") == store.PAYMENT_TXN_IDEMPOTENT
    assert store.get_meta(key) == "ord-a"


def test_bind_payment_transaction_other_order_conflicts_without_overwriting(db):
    """一个支付流水号绑给第二张单 = 重放：拒绝，且绝不改写已有绑定。"""
    key = "paytxn:fake:txn-shared"
    store.bind_payment_transaction(key, "ord-first")
    assert store.bind_payment_transaction(key, "ord-second") == store.PAYMENT_TXN_CONFLICT
    assert store.get_meta(key) == "ord-first", "第二张单覆盖了第一张的绑定"


@pytest.mark.parametrize("key,order_id", [("", "ord-a"), ("paytxn:fake:txn", ""), ("", "")])
def test_bind_payment_transaction_refuses_incomplete_identity(db, key, order_id):
    with pytest.raises(store.StoreError):
        store.bind_payment_transaction(key, order_id)


@pytest.mark.parametrize("fail_on", ["any", "select", "insert"])
def test_bind_payment_transaction_storage_failure_is_typed(db, monkeypatch, fail_on):
    """读 / 写 / 整条连接的故障都必须变成 StoreError，而不是静默返回一个结果。

    把「查不了」和「没绑过」混为一谈，就是让一次数据库打嗝变成一次放行。
    """
    real_connect = store._connect
    monkeypatch.setattr(store, "_connect", lambda: _MetaGuard(real_connect(), fail_on))

    with pytest.raises(store.StoreError):
        store.bind_payment_transaction("paytxn:fake:txn-fail", "ord-a")


def test_bind_payment_transaction_connect_failure_is_typed(db, monkeypatch):
    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_connect", _boom)
    with pytest.raises(store.StoreError):
        store.bind_payment_transaction("paytxn:fake:txn-fail", "ord-a")


def test_bind_payment_transaction_write_failure_leaves_no_partial_row(db, monkeypatch):
    """写失败后不得留下半截绑定：重投时必须仍然看到「未绑定」这一真实状态。"""
    key = "paytxn:fake:txn-half"
    real_connect = store._connect
    monkeypatch.setattr(store, "_connect", lambda: _MetaGuard(real_connect(), "insert"))

    with pytest.raises(store.StoreError):
        store.bind_payment_transaction(key, "ord-a")

    # 绕过被注入故障的连接，直接读文件：绑定表里没有半截行
    conn = sqlite3.connect(store._db_path())
    try:
        assert conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone() is None
    finally:
        conn.close()


@pytest.fixture(params=[True, False], ids=["with-write-lock", "begin-immediate-only"])
def in_process_lock(request):
    """Whether the process-local write lock is left in place during a race test."""
    return request.param


def test_bind_payment_transaction_has_exactly_one_winner_under_concurrency(
    db, monkeypatch, in_process_lock
):
    """N 个并发认领同一个流水号：恰好一个 bound，其余 conflict。

    旧实现先读后写，并发时全部都能读到「未绑定」，于是同一个流水号绑上了 N 张单。

    两个变体各覆盖一层防线：``with-write-lock`` 是单进程常态；``begin-immediate-only``
    把进程内写锁换成 no-op，逼出真正的 SQLite 事务语义 —— 多 worker 部署时
    ``_WRITE_LOCK`` 形同虚设，跨进程只有 BEGIN IMMEDIATE + busy_timeout 在挡。
    只有这一层在时仍然必须恰好一个赢家，否则删掉 BEGIN IMMEDIATE 也没人会发现。
    """
    if not in_process_lock:
        monkeypatch.setattr(store, "_WRITE_LOCK", contextlib.nullcontext())

    key = "paytxn:fake:txn-race"
    order_ids = [f"ord-race-{i}" for i in range(8)]
    barrier = threading.Barrier(len(order_ids))
    lock = threading.Lock()
    outcomes = {}

    def _worker(order_id):
        barrier.wait(timeout=10)
        try:
            outcome = store.bind_payment_transaction(key, order_id)
        except store.StoreError as exc:
            outcome = f"store_error:{exc}"
        with lock:
            outcomes[order_id] = outcome

    threads = [threading.Thread(target=_worker, args=(order_id,)) for order_id in order_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(outcomes) == len(order_ids), outcomes
    winners = [o for o in order_ids if outcomes[o] == store.PAYMENT_TXN_BOUND]
    assert len(winners) == 1, outcomes
    assert list(outcomes.values()).count(store.PAYMENT_TXN_CONFLICT) == len(order_ids) - 1, outcomes
    assert store.get_meta(key) == winners[0]
