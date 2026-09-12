"""单用户全链路 SaaS 验收：注册 → 3 次 Plus 试用 → 耗尽 → 购买 → 到期冻结 → 续费恢复 → 升级 Pro。

补的是审计点名的「没有一个测试走完整链路」：阶段证据散落在 ≥8 个文件里，``renew_url``
从未被跟踪断言，冻结→恢复与 Plus→Pro 改绑都没有路由级序列证明（见
``work/agent-team/four-lines-final-acceptance-deepseek/jobs/saas_browser_acceptance``）。

跑法与其他 ``tests_e2e`` 相同（设计上必须与 ``tests/`` 分开进程）：

    .venv/bin/python -m pytest tests_e2e/test_saas_full_journey_e2e.py -q

刻意**不**重复的错误路径（各自已有专项测试，断言更细）：

  - 上游截断 / 空流不扣次数        → tests_e2e/test_v1_trial_lifetime_e2e.py
  - 支付未配置 / 回调验签 / 重放    → tests_e2e/test_user_saas.py, test_saas_product_gate.py
  - 容量未配置 / 分配失败可重试     → tests_e2e/test_payment_fulfillment.py
  - 到期不回落未用完的试用          → tests_e2e/test_trial_dashboard.py
  - 冻结不丢历史 / 登录态           → tests_e2e/test_four_line_registration.py
  - 结算与分配的故障注入            → tests_e2e/test_payment_fulfillment.py

全文只读真实 SQLite 账面与 mock 上游收到的报文，不 mock 权益、不 mock 试用余额。
"""
import time

import utils.configs as configs
import utils.store as store
import utils.trials as trials
from gateway.saas import _user_subscriptions
from utils import seed_lifecycle

EMAIL = "journey@example.test"
PASSWORD = "Journey-example-password-734!"
SECOND_EMAIL = "journey-second@example.test"
PLUS_PLAN = "plus-shared-1m"
PRO_PLAN = "pro-shared-1m"
CONVERSATION_PATH = "/backend-api/conversation"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register(client, email, password=PASSWORD):
    client.get("/register")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post("/register", data={"email": email, "password": password,
                                          "csrf_token": csrf})


def _checkout(client, plan_id, follow_redirects=True):
    """走 mock 渠道下单 —— auto_settle 会立刻结算并尝试分配（无真实扣款）。"""
    client.get(f"/checkout?plan={plan_id}")
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    return client.post(
        "/api/checkout", data={"plan": plan_id, "csrf_token": csrf},
        follow_redirects=follow_redirects,
    )


def _chat(client, seed, stream=True):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {seed}"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
              "stream": stream},
    )


def _upstream_records(mock_upstream, path=CONVERSATION_PATH):
    return [r for r in mock_upstream.records if r["path"].split("?")[0] == path]


def _live_holders(account):
    """当前真实占用该号的活动/试用 Seed 数（容量口径与 seed_lifecycle 一致）。"""
    with store._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM users WHERE current_account=? AND status IN ('active','trial')",
            (account,),
        ).fetchone()
    return row[0]


def _entitlements_tier(seed):
    from utils import entitlements
    return entitlements.effective_tier(seed)


def _assert_no_free_product(body):
    """产品只卖 Plus / Pro：任何用户可见页面都不得出现 Free 档。"""
    assert "Free" not in body, "页面出现了 Free 档"
    assert "free" not in body.lower(), "页面出现了 free 档"


def _expire_order(order_id, seconds_ago=60):
    """时间旅行：把已支付订单的到期时间挪到过去（到期靠算不靠扫表）。"""
    with store._connect() as conn:
        conn.execute("UPDATE orders SET expires_at=? WHERE order_id=?",
                     (int(time.time()) - seconds_ago, order_id))


# ---------------------------------------------------------------------------
# The journey
# ---------------------------------------------------------------------------

def test_single_user_full_saas_journey(client, client_factory, mock_upstream, monkeypatch,
                                       seed_account, make_access_token):
    plus_token = make_access_token(account_id="acc-journey-plus", plan_type="plus")
    pro_token = make_access_token(account_id="acc-journey-pro", plan_type="pro")
    seed_account(plus_token, plan_type="plus")
    seed_account(pro_token, plan_type="pro")

    # -- 1. 注册即赠 Plus 试用，且没有任何 Free 选项 --------------------------
    monkeypatch.setattr(configs, "require_email_verification", False)
    assert _register(client, EMAIL).status_code == 200

    auth = store.get_user_auth(EMAIL)
    seed = auth["seed"]
    assert store.list_orders(email=EMAIL) == [], "注册不应产生任何订单（更不该是 Free 档）"
    state = trials.trial_state(EMAIL)
    assert (state["tier"], state["total"], state["used"], state["remaining"]) == ("plus", 3, 0, 3)
    assert _entitlements_tier(seed) == "plus"
    _assert_no_free_product(client.get("/store").text)
    _assert_no_free_product(client.get("/dashboard").text)

    # -- 2. 三次真实生成（真实路由 + 真实 ChatService + mock 上游），每次扣 1 次 ----
    for index in range(3):
        resp = _chat(client, seed, stream=index % 2 == 0)
        assert resp.status_code == 200
        if index % 2 == 0:
            assert "Hello, world" in resp.text and "[DONE]" in resp.text
        else:
            assert resp.json()["choices"][0]["message"]["content"] == "Hello, world"
        state = trials.trial_state(EMAIL)
        assert (state["used"], state["reserved"], state["remaining"]) == (index + 1, 0, 2 - index)

    # 试用身份按 Plus 档分号：既没绑到 Pro 号，也没被降级去用 Free 号
    row = store.get_user(seed)
    assert (row["status"], row["plan_type"], row["current_account"]) == \
        ("trial", "plus", plus_token)

    # -- 3. 耗尽：第 4 次 402，且不打上游 ------------------------------------
    before = len(mock_upstream.records)
    assert _chat(client, seed, stream=False).status_code == 402
    assert len(mock_upstream.records) == before, "额度耗尽仍打了上游"

    # -- 4. Dashboard 显示耗尽态 + 购买 CTA，不显示试用入口，也不显示 Free --------
    body = client.get("/dashboard").text
    assert "3 次试用已用完" in body
    assert "购买 Plus" in body and "购买 Pro" in body
    assert "开始试用" not in body
    _assert_no_free_product(body)

    # -- 5. 购买 Plus 拼车月卡：mock 结算 → 立即绑定同档健康号 -------------------
    assert _checkout(client, PLUS_PLAN).status_code == 200
    paid = [o for o in store.list_orders(email=EMAIL) if o["status"] == "paid"]
    assert [o["tier_id"] for o in paid] == [PLUS_PLAN]
    order = paid[0]
    assert order["expires_at"] > time.time()

    row = store.get_user(seed)
    assert (row["status"], row["plan_type"], row["current_account"]) == ("active", "plus", plus_token)
    body = client.get("/dashboard").text
    assert "服务正常" in body and "进入 ChatGPT" in body
    _assert_no_free_product(body)

    # 付费期内按订单走，不再消耗注册试用额度
    assert _chat(client, seed, stream=False).status_code == 200
    assert trials.trial_state(EMAIL)["used"] == 3
    assert f"Bearer {plus_token}" == _upstream_records(mock_upstream)[-1]["authorization"]

    # 用户历史：到期冻结必须原样保留
    store.upsert_conversation("journey-history", seed, plus_token, "Example", "1", "2")

    # -- 6. 时间旅行 + 调度器生命周期作业：过期即冻结，保留绑定与历史 -------------
    _expire_order(order["order_id"])
    frozen = seed_lifecycle.freeze_expired_seeds()  # 调度器 seed_expiry 作业本体
    assert frozen["frozen"] >= 1 and frozen["errors"] == 0

    row = store.get_user(seed)
    assert row["status"] == "frozen", "到期后 Seed 必须被冻结"
    assert row["current_account"] == plus_token, "冻结不得抹掉绑定"
    assert store.list_seed_conversations(seed)[0]["conv_id"] == "journey-history"
    assert store.get_user_auth(EMAIL)["status"] == "active", "冻结不得影响登录态"

    # 冻结后再聊天：真实路由 402，且不打上游
    before = len(mock_upstream.records)
    denied = client.post(CONVERSATION_PATH, cookies={"token": seed},
                         json={"model": "auto", "messages": []})
    assert denied.status_code == 402
    assert len(mock_upstream.records) == before

    # -- 7. 续费入口是可跟踪的精确 URL，GET 可达 --------------------------------
    subs = _user_subscriptions(EMAIL)
    assert [s["renew_url"] for s in subs] == [f"/checkout?plan={PLUS_PLAN}"]
    renew_url = subs[0]["renew_url"]
    body = client.get("/dashboard").text
    assert f'href="{renew_url}"' in body and "去续费" in body
    # 第二个渲染点（钱包页）指向同一个 URL，不能各自写死一套
    assert f'href="{renew_url}"' in client.get("/wallet").text
    checkout_page = client.get(renew_url)
    assert checkout_page.status_code == 200
    _assert_no_free_product(checkout_page.text)

    # -- 8. 续费（即同一 URL 的套餐下单）：解冻恢复服务，绑定保持原号 --------------
    before_ids = {o["order_id"] for o in store.list_orders(email=EMAIL)}
    assert _checkout(client, PLUS_PLAN).status_code == 200
    renewed = [o for o in store.list_orders(email=EMAIL) if o["order_id"] not in before_ids]
    assert len(renewed) == 1, "续费必须恰好产生一张新订单"
    renewal = renewed[0]
    assert (renewal["status"], renewal["tier_id"]) == ("paid", PLUS_PLAN)
    assert renewal["expires_at"] > int(time.time()), "续费后必须重新拿到未来有效期"

    row = store.get_user(seed)
    assert (row["status"], row["plan_type"], row["current_account"]) == ("active", "plus", plus_token)
    body = client.get("/dashboard").text
    assert "服务正常" in body and "进入 ChatGPT" in body
    _assert_no_free_product(body)
    # 历史到期单如实保留（不能把「付过但过期」擦成没买过），新单独立复活服务
    assert sorted(s["status_text"] for s in _user_subscriptions(EMAIL)) == ["已过期", "服务正常"]

    assert _chat(client, seed, stream=False).status_code == 200, "续费后必须能继续聊天"
    assert trials.trial_state(EMAIL)["used"] == 3, "曾付费用户不得回落到注册试用"

    # -- 9. Plus → Pro 升级：改绑健康 Pro 号，并释放旧 Plus 号的真实容量 -----------
    assert _checkout(client, PRO_PLAN).status_code == 200
    row = store.get_user(seed)
    assert (row["status"], row["plan_type"], row["current_account"]) == ("active", "pro", pro_token)
    assert _live_holders(plus_token) == 0, "升级后旧 Plus 号仍被本 Seed 占用"

    body = client.get("/dashboard").text
    assert "ChatGPT Pro 队列" in body and "服务正常" in body
    _assert_no_free_product(body)

    assert _chat(client, seed, stream=False).status_code == 200
    assert f"Bearer {pro_token}" == _upstream_records(mock_upstream)[-1]["authorization"], \
        "升级后生成仍走旧 Plus 号"

    # 释放出来的 Plus 容量真的能被别人用：把共享容量压到 1，第二个用户仍绑得上。
    # 若升级没有释放，这里会因 capacity_exceeded 落到「待分配账号」。
    monkeypatch.setattr(configs, "max_shared_seeds_per_account", 1)
    other = client_factory()
    assert _register(other, SECOND_EMAIL).status_code == 200
    assert _checkout(other, PLUS_PLAN).status_code == 200
    second_seed = store.get_user_auth(SECOND_EMAIL)["seed"]
    assert store.get_user(second_seed)["current_account"] == plus_token
    assert "服务正常" in other.get("/dashboard").text
