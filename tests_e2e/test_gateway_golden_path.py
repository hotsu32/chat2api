"""Stage 2 — gateway golden-path E2E (TEST_PLAN §5 E1/E2/E3 + §6 gateway items).

Exercises the real FastAPI gateway routes against the loopback mock upstream:
  E1  first visit -> login -> assign -> anonymized session / client-bootstrap rewrite
  E2  dead account -> auto fail-over -> history follows account
  E3  usage double-granularity (seed + account)
  plus /ces/* short-circuit and banned-path gating.
"""

import utils.globals as globals
import utils.store as store
import utils.usage as usage


def _mask(token):
    return token[:6] + "..." + token[-4:]


# ---------------------------------------------------------------------------
# E1  first visit -> login -> assign -> anonymized identity
# ---------------------------------------------------------------------------

def test_root_without_seed_serves_login(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"RefreshToken" in resp.content  # login page, not the owner live template


def test_seed_visit_rewrites_client_bootstrap_identity(client, monkeypatch, seed_user, seed_account,
                                                       make_access_token):
    tok = make_access_token(account_id="acc-e1", plan_type="plus")
    seed_account(tok)
    seed_user("seed-e1", tok, plan_type="plus")

    # 用 canned 官网 HTML（含 owner 身份的 client-bootstrap）替代真实抓取，避免网络。
    async def _fake_template():
        return (
            "<html><head></head><body>"
            '<script type="application/json" id="client-bootstrap" nonce="x">'
            '{"authStatus":"logged_in","session":{"user":{"name":"Owner Real","email":"owner@example.com"}}}'
            "</script>"
            "</body></html>"
        )

    monkeypatch.setattr("gateway.chatgpt.get_frontend_template", _fake_template)

    resp = client.get("/", cookies={"token": "seed-e1"})
    assert resp.status_code == 200
    assert resp.cookies.get("token") == "seed-e1"
    # 重写后注入的是匿名身份，而非 owner 身份
    assert b'"name":"ChatGPT"' in resp.content
    assert b'"email":""' in resp.content
    assert b"Owner Real" not in resp.content
    assert b"owner@example.com" not in resp.content


def test_auth_session_returns_anonymized_session(client, seed_user, seed_account, make_access_token):
    tok = make_access_token(account_id="acc-e1", plan_type="plus")
    seed_account(tok)
    seed_user("seed-e1", tok, plan_type="plus")

    resp = client.get("/api/auth/session", cookies={"token": "seed-e1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["name"] == "ChatGPT"
    assert data["user"]["email"] == ""
    assert data["account"]["planType"] == "plus"
    assert data["account"]["id"] == "acc-e1"


# ---------------------------------------------------------------------------
# E2  dead account -> auto fail-over -> history follows account
# ---------------------------------------------------------------------------

def test_account_status_masks_identity(client, seed_user, seed_account, make_access_token):
    tok = make_access_token(account_id="acc-a")
    seed_account(tok, plan_type="plus")
    seed_user("seed-e2", tok, plan_type="plus")

    resp = client.get("/api/account-status", cookies={"token": "seed-e2"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["account"] == _mask(tok)
    assert data["status"] == "healthy"
    # 身份匿名化：name 为 ChatGPT，不泄漏 owner 真实身份
    assert data["identity"]["name"] == "ChatGPT"


def test_auto_failover_on_disabled_account(client, seed_user, seed_account, make_access_token):
    tok_a = make_access_token(account_id="acc-a")
    tok_b = make_access_token(account_id="acc-b")
    seed_account(tok_a, plan_type="plus")
    seed_account(tok_b, plan_type="plus")
    seed_user("seed-e2", tok_a, plan_type="plus")

    resp = client.get("/api/account-status", cookies={"token": "seed-e2"})
    assert resp.json()["account"] == _mask(tok_a)

    # 号挂：绑定账号 disabled 后，下一次会话解析触发同等级 fail-over
    store.upsert_account(tok_a, status="disabled")

    resp = client.get("/api/auth/session", cookies={"token": "seed-e2"})
    assert resp.status_code == 200

    resp = client.get("/api/account-status", cookies={"token": "seed-e2"})
    data = resp.json()
    assert data["account"] == _mask(tok_b)
    assert data["status"] == "healthy"


def test_switch_account_rejected_when_healthy(client, seed_user, seed_account, make_access_token):
    tok_a = make_access_token(account_id="acc-a")
    tok_b = make_access_token(account_id="acc-b")
    seed_account(tok_a, plan_type="plus")
    seed_account(tok_b, plan_type="plus")
    seed_user("seed-e2", tok_a, plan_type="plus")

    resp = client.post("/api/switch-account", cookies={"token": "seed-e2"})
    assert resp.status_code == 400


def test_switch_account_on_disabled_account(client, seed_user, seed_account, make_access_token):
    tok_a = make_access_token(account_id="acc-a")
    tok_b = make_access_token(account_id="acc-b")
    seed_account(tok_a, plan_type="plus")
    seed_account(tok_b, plan_type="plus")
    seed_user("seed-e2", tok_a, plan_type="plus")

    store.upsert_account(tok_a, status="disabled")

    resp = client.post("/api/switch-account", cookies={"token": "seed-e2"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["switched"] is True
    assert data["account"] == _mask(tok_b)
    # seed 重绑定到健康号
    assert globals.seed_map["seed-e2"]["token"] == tok_b


def test_conversations_filtered_by_current_account(client, seed_user, seed_account, make_access_token):
    tok_a = make_access_token(account_id="acc-a")
    tok_b = make_access_token(account_id="acc-b")
    seed_account(tok_a, plan_type="plus")
    seed_account(tok_b, plan_type="plus")
    seed_user("seed-e2", tok_a, plan_type="plus", conversations=["conv-1", "conv-2"])

    globals.conversation_map["conv-1"] = {
        "id": "conv-1", "title": "A chat", "account": tok_a, "is_archived": False,
    }
    globals.conversation_map["conv-2"] = {
        "id": "conv-2", "title": "B chat", "account": tok_b, "is_archived": False,
    }

    resp = client.get("/backend-api/conversations", cookies={"token": "seed-e2"})
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == ["conv-1"]  # 只列当前账号（tok_a）的会话


# ---------------------------------------------------------------------------
# E3  usage double granularity (seed + account)
# ---------------------------------------------------------------------------

def test_usage_counted_per_seed_and_account(client, seed_user, seed_account, make_access_token):
    tok = make_access_token(account_id="acc-e3", plan_type="plus")
    seed_account(tok)
    seed_user("seed-e3", tok, plan_type="plus")

    n = 3
    for _ in range(n):
        resp = client.post("/backend-api/conversation", cookies={"token": "seed-e3"}, json={})
        assert resp.status_code == 200

    # 内存缓冲计数
    assert usage.pending_count() == n

    flushed = usage.flush_usage()
    assert flushed == n

    # 双粒度：seed 与 account 各计 n 次
    assert usage.user_usage("seed-e3") == n
    assert usage.account_usage(tok) == n


# ---------------------------------------------------------------------------
# §6 gateway extras: /ces/* short-circuit
# ---------------------------------------------------------------------------

def test_ces_short_circuit_returns_202(client):
    resp = client.get("/ces/telemetry/collect")
    assert resp.status_code == 202
