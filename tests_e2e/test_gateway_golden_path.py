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
import json
import pytest


@pytest.fixture
def website_sessions(monkeypatch, tmp_path, make_access_token):
    from gateway import frontend_sync as frontend
    frontend.invalidate_frontend_cache()
    monkeypatch.setattr(frontend, 'SESSION_ARCHIVE_DIR', tmp_path)
    for account in ('acc-e1', 'acc-a', 'acc-b'):
        (tmp_path / (account + '.json')).write_text(json.dumps({
            'account': {'id': account}, 'sessionToken': 'test-' + account}))

    def fetch(cookies, account_id, fingerprint, **kwargs):
        assert cookies['__Secure-next-auth.session-token'] == 'test-' + account_id
        session = {'user': {'name': 'Private owner', 'email': 'owner@example.test'},
                   'account': {'id': account_id, 'planType': 'plus'},
                   'accessToken': make_access_token(account_id=account_id),
                   'sessionToken': 'PRIVATE-SESSION'}
        return {'html': '<html></html>', 'session': session, 'cookies': cookies}

    monkeypatch.setattr(frontend, '_fetch_official_html_sync', fetch)
    yield
    frontend.invalidate_frontend_cache()


def _mask(token):
    return token[:6] + "..." + token[-4:]


# ---------------------------------------------------------------------------
# E1  first visit -> login -> assign -> anonymized identity
# ---------------------------------------------------------------------------

def test_root_without_seed_redirects_to_dashboard(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.history[0].status_code == 302
    assert resp.history[0].headers['location'] == '/dashboard'
    assert resp.url.path == '/signin'
    assert b"client-bootstrap" not in resp.content


def test_seed_visit_rewrites_client_bootstrap_identity(client, monkeypatch, seed_user, seed_account,
                                                       make_access_token):
    import json as _json
    import re as _re

    tok = make_access_token(account_id="acc-e1", plan_type="plus")
    seed_account(tok)
    seed_user("seed-e1", tok, plan_type="plus")

    # 用 canned 官网 HTML 替代真实抓取，避免网络。client-bootstrap 里嵌入 owner 身份的
    # session + statsigPayload（含 owner userID/email/customIDs），并带非身份键 sessionId，
    # 用于验证「外科手术式保留启动配置、仅脱敏身份」而非整体替换。
    _statsig = _json.dumps({
        "feature_gates": {"gate_a": {"value": True}, "gate_b": {"value": False}},
        "user": {
            "userID": "user-OWNERLEAK",
            "email": "owner@example.com",
            "customIDs": {"account_id": "acc-OWNERLEAK", "DeviceId": "dev-OWNERLEAK"},
            "custom": {"account_id": "acc-OWNERLEAK", "plan_type": "plus", "is_paid": True},
        },
    })
    _bootstrap = _json.dumps({
        "authStatus": "logged_in",
        "sessionId": "sid-preserve-me",
        "session": {"user": {"name": "Owner Real", "email": "owner@example.com"}},
        "statsigPayload": _statsig,
    })

    async def _fake_template(req_token, access_token, fingerprint):
        assert req_token == tok and access_token == tok
        return (
            "<html><head></head><body>"
            '<script type="application/json" id="client-bootstrap" nonce="x">'
            + _bootstrap +
            "</script>"
            "</body></html>"
        )

    monkeypatch.setattr("gateway.chatgpt.get_frontend_template", _fake_template)

    resp = client.get("/?token=seed-e1")
    assert resp.status_code == 200
    assert resp.cookies.get("token") == "seed-e1"

    # 解析下发页面里的 client-bootstrap 做字段级断言
    m = _re.search(
        r'<script type="application/json" id="client-bootstrap"[^>]*>(.*?)</script>',
        resp.content.decode("utf-8"), _re.DOTALL)
    assert m, "client-bootstrap tag missing from served page"
    data = _json.loads(m.group(1))

    # 非身份启动配置被保留（整体替换会丢掉它们）
    assert data["sessionId"] == "sid-preserve-me"
    sp = _json.loads(data["statsigPayload"])
    assert sp["feature_gates"]["gate_a"]["value"] is True  # 特性开关保留

    # 身份脱敏：session / statsig 内都不含 owner 痕迹
    assert data["session"]["user"]["name"] == "ChatGPT"
    assert data["session"]["user"]["email"] == ""
    assert data["session"]["account"]["planType"] == "plus"
    assert data["session"]["account"]["id"] == "acc-e1"
    assert sp["user"]["userID"] != "user-OWNERLEAK"
    assert sp["user"]["email"] == ""
    assert sp["user"]["customIDs"]["account_id"] == "acc-e1"
    assert sp["user"]["customIDs"]["DeviceId"] != "dev-OWNERLEAK"
    assert sp["user"]["custom"]["account_id"] == "acc-e1"
    assert sp["user"]["custom"]["plan_type"] == "plus"
    assert sp["user"]["custom"]["is_paid"] is True

    # 原文层面：owner 身份字串不出现在页面任何位置
    assert b"Owner Real" not in resp.content
    assert b"owner@example.com" not in resp.content
    assert b"OWNERLEAK" not in resp.content


def test_auth_session_returns_anonymized_session(client, seed_user, seed_account, make_access_token, website_sessions):
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


def test_auto_failover_on_disabled_account(client, seed_user, seed_account, make_access_token, website_sessions):
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


# ---------------------------------------------------------------------------
# E4  frontend f/conversation: send a message -> streamed assistant reply
# ---------------------------------------------------------------------------

def test_f_conversation_streams_assistant_reply(client, seed_user, seed_account, make_access_token):
    """发消息能回：POST /backend-api/f/conversation 走真实 gateway + mock 上游，
    返回 text/event-stream 且包含 assistant 回复，而不是 500 或空流。"""
    tok = make_access_token(account_id="acc-e4", plan_type="plus")
    seed_account(tok)
    seed_user("seed-e4", tok, plan_type="plus")

    body = {
        "model": "gpt-5-6",
        "messages": [
            {"id": "msg-u1", "author": {"role": "user"},
             "content": {"content_type": "text", "parts": ["hello"]}},
        ],
        "conversation_id": "conv-e4",
        "parent_message_id": "msg-u1",
    }
    resp = client.post(
        "/backend-api/f/conversation", cookies={"token": "seed-e4"}, json=body
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    # 流式里回的是 assistant 回复（mock 上游固定回 "Hello, world"），且至少一条 assistant 帧
    assert b"Hello, world" in resp.content
    assert b'"role": "assistant"' in resp.content
