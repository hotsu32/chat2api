"""store: SQLite DAO — CRUD, tier pool, conversations, usage, migration idempotency."""
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
