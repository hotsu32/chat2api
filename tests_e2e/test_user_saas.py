"""Stage 0–4 用户侧 SaaS E2E：档位模型 / 注册登录 / 档位与额度执行 / 落地页+支付占位。

全部跑在真实 FastAPI app + loopback mock 上游上，零真实网络、零真实凭据。

覆盖验收判据：
  Stage 0  user.tier 能持久化 + 枚举「可用号组 / 可用模型 / 额度上限」
  Stage 1  注册 → 登录 → 拿 seed → 走 GET /?token=seed 进聊天，无运营者介入
  Stage 2  免费档超额被限、付费档进专属号组、换档后换号组
  Stage 3  落地页可访问、下单落库 pending
  fail-open 运营者 seed（无 user_auth 行）不设限
"""
import utils.configs as configs
import utils.globals as globals
import utils.store as store
import utils.tiers as tiers


def _make_user(seed, email, tier_id="free"):
    store.upsert_user_auth(
        email, password_hash="pbkdf2_sha256$1$00$00", seed=seed, tier_id=tier_id, status="active"
    )
    globals.seed_map[seed] = {"token": "", "plan_type": None, "conversations": []}


def _register(client, email, password):
    client.get("/register")  # 下发 CSRF cookie
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post(
        "/register", data={"email": email, "password": password, "csrf_token": csrf}
    )


def _signin(client, email, password):
    client.get("/signin")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post(
        "/signin", data={"email": email, "password": password, "csrf_token": csrf}
    )


def _fake_template(monkeypatch):
    """用 canned 官网 HTML 替代真实抓取，避免 data/session_cookie.txt 依赖。"""
    async def _fake():
        return "<html><head></head><body>chat</body></html>"
    monkeypatch.setattr("gateway.chatgpt.get_frontend_template", _fake)


# ---------------------------------------------------------------------------
# Stage 0 — 档位与分池数据模型
# ---------------------------------------------------------------------------

def test_tier_catalog_enumerates_group_models_quota():
    cat = tiers.list_tiers()
    assert set(cat) >= {"free", "plus", "pro"}
    for tid in ("free", "plus", "pro"):
        t = cat[tid]
        assert t["account_plan_types"], f"{tid} 缺号组范围"
        assert t["models"], f"{tid} 缺模型白名单"
        assert t.get("quota_limit") is not None, f"{tid} 缺额度上限"


def test_tier_account_plan_types_and_model_gate():
    assert tiers.tier_account_plan_types("free") == ["free"]
    assert "plus" in tiers.tier_account_plan_types("pro")
    assert tiers.tier_allows_model("free", "gpt-4o-mini") is True
    assert tiers.tier_allows_model("free", "o3") is False
    assert tiers.tier_allows_model("plus", "o3") is True
    # 未知档位 / 空模型 fail-open
    assert tiers.tier_allows_model("free", "") is True
    assert tiers.tier_allows_model("nope", "o3") is True


def test_resolve_user_tier_none_without_user_auth():
    assert tiers.resolve_user_tier("seed-no-user") is None


# ---------------------------------------------------------------------------
# Stage 1 — 注册 / 登录 / seed 绑定
# ---------------------------------------------------------------------------

def test_register_creates_user_with_free_tier_and_seed(client, monkeypatch, seed_account, make_access_token):
    seed_account(make_access_token(account_id="acc-free", plan_type="free"), plan_type="free")
    _fake_template(monkeypatch)

    # register 成功 → 303 跳 /?token=seed（TestClient 跟随重定向，终态为聊天页 200）
    resp = _register(client, "alice@example.com", "password123")
    assert resp.status_code == 200
    assert any(r.status_code == 303 for r in resp.history)

    row = store.get_user_auth("alice@example.com")
    assert row is not None
    assert row["tier_id"] == "free"
    assert row["status"] == "active"
    assert row["seed"]
    # 会话 cookie 已下发
    assert client.cookies.get(configs.user_session_cookie)
    # seed 已落到 seed_map
    assert row["seed"] in globals.seed_map


def test_login_then_enter_chat(client, monkeypatch, seed_account, make_access_token):
    seed_account(make_access_token(account_id="acc-free", plan_type="free"), plan_type="free")
    _fake_template(monkeypatch)
    _register(client, "bob@example.com", "password123")
    row = store.get_user_auth("bob@example.com")
    seed = row["seed"]

    # 退出登录态，再登录
    client.post("/signout")
    resp = _signin(client, "bob@example.com", "password123")
    assert resp.status_code == 200

    # 用 seed 进聊天：解析到免费号，返回官网模板（重写身份）
    resp = client.get("/", cookies={"token": seed})
    assert resp.status_code == 200
    assert resp.cookies.get("token") == seed


def test_login_rejects_wrong_password(client):
    _register(client, "carol@example.com", "password123")
    client.post("/signout")
    resp = _signin(client, "carol@example.com", "wrong-password")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Stage 2 — 档位与额度执行
# ---------------------------------------------------------------------------

def test_free_tier_blocks_premium_model(client, seed_account, make_access_token):
    seed_account(make_access_token(account_id="acc-free", plan_type="free"), plan_type="free")
    _make_user("seed-gate", "gate@example.com", tier_id="free")

    resp = client.post(
        "/backend-api/conversation", cookies={"token": "seed-gate"}, json={"model": "o3"}
    )
    assert resp.status_code == 403


def test_free_tier_quota_exceeded_returns_429(client, seed_account, make_access_token):
    tok = make_access_token(account_id="acc-free", plan_type="free")
    seed_account(tok, plan_type="free")
    seed = "seed-quota"
    _make_user(seed, "quota@example.com", tier_id="free")

    # 预置 free 档满额（50）条当日用量
    for _ in range(50):
        store.add_usage_event(seed, tok, "conversation")

    resp = client.post(
        "/backend-api/conversation", cookies={"token": seed}, json={"model": "gpt-4o-mini"}
    )
    assert resp.status_code == 429


def test_free_user_routes_to_free_pool(client, seed_account, make_access_token):
    tok_free = make_access_token(account_id="acc-free", plan_type="free")
    tok_plus = make_access_token(account_id="acc-plus", plan_type="plus")
    seed_account(tok_free, plan_type="free")
    seed_account(tok_plus, plan_type="plus")
    _make_user("seed-pool", "pool@example.com", tier_id="free")

    client.get("/api/auth/session", cookies={"token": "seed-pool"})
    assert globals.seed_map["seed-pool"]["token"] == tok_free


def test_upgrade_tier_switches_pool_on_failover(client, seed_account, make_access_token):
    tok_free = make_access_token(account_id="acc-free", plan_type="free")
    tok_plus = make_access_token(account_id="acc-plus", plan_type="plus")
    seed_account(tok_free, plan_type="free")
    seed_account(tok_plus, plan_type="plus")
    _make_user("seed-up", "up@example.com", tier_id="free")

    client.get("/api/auth/session", cookies={"token": "seed-up"})
    assert globals.seed_map["seed-up"]["token"] == tok_free

    # 换档到 plus + free 号挂掉 → failover 切进 plus 号组
    store.upsert_user_auth("up@example.com", tier_id="plus")
    store.upsert_account(tok_free, status="disabled")
    client.get("/api/auth/session", cookies={"token": "seed-up"})
    assert globals.seed_map["seed-up"]["token"] == tok_plus


def test_failover_skips_marked_dead_account(client, seed_account, make_access_token):
    # 回归：mark_dead 只写 antiban_dead_tokens（JSON），不改 accounts.status，
    # 故 _pick_healthy_account 必须经 _account_is_usable 过滤，否则 failover 重选死号。
    from utils.antiban import circuit
    tok_dead = make_access_token(account_id="acc-plus-dead", plan_type="plus")
    tok_live = make_access_token(account_id="acc-plus-live", plan_type="plus")
    seed_account(tok_dead, plan_type="plus")
    _make_user("seed-dead", "dead@example.com", tier_id="plus")

    # 首次绑定：号池只有 tok_dead 一个 plus → 确定性绑定到 tok_dead
    client.get("/api/auth/session", cookies={"token": "seed-dead"})
    assert globals.seed_map["seed-dead"]["token"] == tok_dead

    # 再放入第二个健康 plus 号，然后封禁 tok_dead
    seed_account(tok_live, plan_type="plus")
    circuit.mark_dead(tok_dead, "account_deactivated")

    # failover 必须切到 tok_live，绝不重选死号 tok_dead
    client.get("/api/auth/session", cookies={"token": "seed-dead"})
    assert globals.seed_map["seed-dead"]["token"] == tok_live


def test_operator_seed_not_limited_by_tier(client, seed_account, make_access_token):
    # 运营者 seed（无 user_auth 行）走旧主链路，不受档位/额度限制
    tok = make_access_token(account_id="acc-op", plan_type="plus")
    seed_account(tok, plan_type="plus")
    globals.seed_map["seed-op"] = {"token": tok, "plan_type": "plus", "conversations": []}

    resp = client.post(
        "/backend-api/conversation", cookies={"token": "seed-op"}, json={"model": "o3"}
    )
    # 不设限 → 正常透传到 mock 上游（200）
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Stage 3 — 落地页 + 支付占位
# ---------------------------------------------------------------------------

def test_landing_page_serves(client):
    resp = client.get("/landing")
    assert resp.status_code == 200
    assert b"register" in resp.content


def test_order_placeholder_persists_pending(client):
    _register(client, "buyer@example.com", "password123")
    row = store.get_user_auth("buyer@example.com")

    resp = client.post("/api/orders", json={"tier_id": "plus", "amount": "99"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["tier_id"] == "plus"

    order = store.get_order(data["order_id"])
    assert order is not None
    assert order["status"] == "pending"
    assert order["email"] == "buyer@example.com"
