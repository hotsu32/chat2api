"""Regression tests for the 6 identity-leak / authorization fixes (Stage 1).

Each test locks the *correct* behavior:

#1  seed 解析失败 → 登录页，而非运营者 live 模板（client-bootstrap 身份泄漏）
#2  banned_paths 按 seed cookie 而非 Authorization 判断
#3  /backend-api/me 匿名 email == ""
#4  /backend-api/accounts/check 抹除 account 内 owner 身份字段
#5  catch-all backend-api JSON 定向脱敏（已知身份字段置空，白名单保留）
#6  跨 seed 会话详情越权（归属校验在代理之前，未归属 404）
"""


def test_empty_session_serves_login_not_owner_template(client):
    """#1 一个无法解析为账号的 seed，不得下发携带 owner client-bootstrap 的 live 模板。"""
    resp = client.get("/", cookies={"token": "seed-nobody"})
    assert resp.status_code == 200
    # 登录页（含 RefreshToken 输入），而非官网 live 模板（含 client-bootstrap 身份 JSON）
    assert b"RefreshToken" in resp.content
    assert b"client-bootstrap" not in resp.content


def test_banned_path_blocked_for_mirror_user_with_direct_token(client, make_access_token):
    """#2 浏览器同时带 seed cookie 与号池 accessToken 时，仍按镜像用户封禁。"""
    resp = client.get(
        "/backend-api/payments",
        cookies={"token": "seed-x"},
        headers={"Authorization": "Bearer " + make_access_token()},
    )
    assert resp.status_code == 403


def test_banned_path_allowed_for_direct_client(client, make_access_token):
    """#2 真正无 seed cookie 的直连 API 客户端放行（不误封）。"""
    resp = client.get(
        "/backend-api/payments",
        headers={"Authorization": "Bearer " + make_access_token()},
    )
    assert resp.status_code == 200


def test_me_returns_anonymized_email(client):
    """#3 镜像用户访问 /backend-api/me，email 必须为空串。"""
    resp = client.get("/backend-api/me", cookies={"token": "seed-x"})
    assert resp.status_code == 200
    assert resp.json()["email"] == ""


def test_accounts_check_scrubs_owner_identity(client):
    """#4 /backend-api/accounts/check 抹除 account 内 owner 身份字段并匿名化 user_id。"""
    resp = client.get("/backend-api/accounts/check/v4-2023-04-27", cookies={"token": "seed-x"})
    assert resp.status_code == 200
    acct = resp.json()["accounts"]["default"]["account"]
    assert acct["account_user_id"] == "user-chatgpt__acc-real-1"
    assert acct["account_email"] == ""
    assert acct["account_name"] == "ChatGPT"
    assert acct["email"] == ""
    assert acct["name"] == "ChatGPT"
    assert acct["phone_number"] == ""
    assert acct["picture"] == ""


def test_catchall_backend_api_json_scrubs_identity(client):
    """#5 catch-all 透传的 backend-api JSON 中，已知身份字段置空、白名单字段保留。"""
    resp = client.get("/backend-api/settings", cookies={"token": "seed-x"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == ""
    assert data["name"] == "ChatGPT"
    assert data["phone_number"] == ""
    assert data["theme"] == "dark"  # 非身份字段保留


def test_cross_seed_conversation_detail_forbidden(client, mock_upstream, seed_user, seed_account,
                                                  make_access_token):
    """#6 未归属的会话详情对镜像用户返回 404，且归属校验在代理之前执行。"""
    tok_a = make_access_token(account_id="acc-a")
    seed_account(tok_a)
    seed_user("seed-a", tok_a, conversations=["conv-1"])
    seed_user("seed-b", "unused-token", conversations=[])

    # owner 可访问
    resp_a = client.get("/backend-api/conversation/conv-1", cookies={"token": "seed-a"})
    assert resp_a.status_code == 200

    # 记录 owner 访问后、越权访问前的上游请求数
    before = sum(1 for r in mock_upstream.records
                 if r["path"].startswith("/backend-api/conversation/conv-1"))

    # 越权用户 → 404，且不得触发上游代理（校验在代理之前）
    resp_b = client.get("/backend-api/conversation/conv-1", cookies={"token": "seed-b"})
    assert resp_b.status_code == 404

    after = sum(1 for r in mock_upstream.records
                if r["path"].startswith("/backend-api/conversation/conv-1"))
    assert after == before
