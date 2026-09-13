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
import re
import time
import urllib.parse

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
# Dashboard 两个购买入口渲染出来的目标套餐（独享月卡）。与上面两个「拼车」常量
# 刻意分开：入口指向的是独享，链路测试买的是拼车，两者不能互相冒充。
PLUS_CTA_PLAN = "plus-solo-1m"
PRO_CTA_PLAN = "pro-solo-1m"
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


def _plan_days(plan_id):
    import utils.plans as plans
    return plans.plan_detail(plan_id)["days"]


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
# CTA → 超市页：Dashboard 的购买入口必须把用户选中的档带过去
# ---------------------------------------------------------------------------

# 超市页会提交的那一个隐藏字段（也是 `js` 在浏览器里实时改写的那一个）
_PLAN_INPUT_RE = re.compile(r'<input type="hidden" name="plan" id="plan-id" value="([^"]*)">')
# 三个轴的选中态：只有服务端渲染出来的 `is-selected` 才算「预选」
_OPTION_RE = r'<button class="([^"]*)" type="button" data-value="%s">'


def _cta_href(body, label):
    """取出 Dashboard 上某个购买 CTA 渲染出来的 href。

    断言的是**页面里真实渲染的链接**，而不是测试自己拼的 URL —— 拼接会绕过
    「入口是否带上档位」这个 bug 本身（这正是本次修复要钉死的行为）。
    """
    match = re.search(r'<a class="[^"]*plan-cta[^"]*" href="([^"]*)">' + label, body)
    assert match, f"Dashboard 没有渲染出「{label}」入口"
    return match.group(1)


def _store_preselection(client, href):
    """跟随 CTA 打开超市页，返回 ``(页面正文, 会提交的 plan 值)``。"""
    page = client.get(href)
    assert page.status_code == 200, f"{href} 未渲染超市页：{page.status_code}"
    match = _PLAN_INPUT_RE.search(page.text)
    assert match, "超市页没有渲染 hidden plan 字段"
    return page.text, match.group(1)


def _axis_selected(body, axis_value):
    """该轴取值对应的按钮是否带 ``is-selected``（无 JS 时的预选事实）。"""
    match = re.search(_OPTION_RE % re.escape(axis_value), body)
    assert match, f"超市页没有渲染 data-value={axis_value} 的选项"
    return "is-selected" in match.group(1).split()


def _js_selection(body):
    """超市页脚本里的初始 state —— 浏览器加载后 ``render()`` 会照它重写表单。

    服务端渲染的 hidden 值只对「无 JS / 测试客户端」成立；真实浏览器里覆盖它的是
    这段脚本。两者只要不同源，用户看到的预选就会在加载瞬间被打回默认档。
    """
    block = re.search(r"const state = \{(.*?)\};", body, re.S)
    assert block, "超市页没有渲染预选 state 脚本"
    fields = dict(re.findall(r'(\w+):\s*"([^"]*)"', block.group(1)))
    assert set(fields) == {"tier", "density", "duration"}, f"预选 state 字段不全：{fields}"
    return fields


def _exhaust_trial(seed, email):
    """把注册赠额全部结算完（走真实的预留→结算账，不直接改余额）。"""
    remaining = trials.trial_state(email)["remaining"]
    assert remaining > 0, "该账号没有可结算的试用额度"
    for _ in range(remaining):
        res_id = trials.reserve(seed)
        assert res_id, "试用额度未按预期预留（余额提前耗尽）"
        assert trials.settle(res_id, seed), "预留结算失败"
    assert trials.trial_state(email)["remaining"] == 0


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

    # 入口把档位带过去：跟随渲染出来的两个 CTA，超市页必须各自预选对应档位
    pro_body, pro_plan = _store_preselection(client, _cta_href(body, "购买 Pro"))
    plus_body, plus_plan = _store_preselection(client, _cta_href(body, "购买 Plus"))
    assert pro_plan == PRO_CTA_PLAN and _axis_selected(pro_body, "pro")
    assert _js_selection(pro_body)["tier"] == "pro"
    assert plus_plan == PLUS_CTA_PLAN and _axis_selected(plus_body, "plus")
    assert _js_selection(plus_body)["tier"] == "plus"

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

    # -- 8b. 到期前同档续费：叠加有效期，且不得出现并行的活动入口卡片 --------------
    month = _plan_days(PLUS_PLAN) * 86400
    latest = max(o["expires_at"] for o in store.list_orders(email=EMAIL)
                 if o["status"] == "paid" and o["tier_id"] == PLUS_PLAN)
    before_ids = {o["order_id"] for o in store.list_orders(email=EMAIL)}
    assert _checkout(client, PLUS_PLAN).status_code == 200
    stacked = [o for o in store.list_orders(email=EMAIL) if o["order_id"] not in before_ids]
    assert len(stacked) == 1
    assert stacked[0]["expires_at"] == latest + month, "续费应从现有到期时间往后叠"

    active = [s for s in _user_subscriptions(EMAIL) if s["healthy"]]
    assert [s["plan_id"] for s in active] == [PLUS_PLAN], "同档续费产生了并行的活动卡片"
    assert client.get("/dashboard").text.count("进入 ChatGPT") == 1, "同档续费多给了一个入口"

    # -- 9. Plus → Pro 升级：改绑健康 Pro 号，并释放旧 Plus 号的真实容量 -----------
    assert _checkout(client, PRO_PLAN).status_code == 200
    row = store.get_user(seed)
    assert (row["status"], row["plan_type"], row["current_account"]) == ("active", "pro", pro_token)
    assert _live_holders(plus_token) == 0, "升级后旧 Plus 号仍被本 Seed 占用"

    body = client.get("/dashboard").text
    assert "ChatGPT Pro 队列" in body and "服务正常" in body
    _assert_no_free_product(body)

    # 绑定只有一个，入口也只能有一个：生效档次是 Pro 时，旧 Plus 有效期不再显示为可用服务
    subs_after = _user_subscriptions(EMAIL)
    serviceable = [s for s in subs_after if s["serviceable"]]
    assert len(serviceable) == 1, \
        f"升级后应恰好剩一个可用入口，实际 {[(s['plan_id'], s['status_text']) for s in subs_after]}"
    assert serviceable[0]["plan_id"] == PRO_PLAN
    assert not [s for s in subs_after if s["healthy"] and s["plan_id"] != PRO_PLAN], \
        "旧档位仍在展示活动服务卡"
    assert body.count("进入 ChatGPT") == 1, "Dashboard 并列展示了多个可用入口"
    # 付过钱的历史不能被抹掉：已过期的那张单如实留着
    assert [s["status_text"] for s in subs_after].count("已过期") == 1

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


def test_dashboard_pro_cta_preselects_and_purchases_pro(client, mock_upstream, monkeypatch,
                                                        seed_account, make_access_token):
    """Dashboard 的「购买 Pro」入口必须一路把 Pro 带到结算为止。

    回归的是这条真实缺陷：``store_page`` 丢掉 ``?plan=``，而 ``store.html`` 又把
    预选档和 hidden 值写死成 ``plus-solo-1m``，于是用户点「购买 Pro」买到的是 Plus。
    因此这里断言的是**从渲染出来的 CTA 一路走到订单**，而不是直接 POST 一个测试
    自己拼的 plan —— 后者恰好会绕过出问题的那一段（查询串 → 预选 → 表单值）。
    """
    pro_token = make_access_token(account_id="acc-cta-pro", plan_type="pro")
    seed_account(pro_token, plan_type="pro")

    monkeypatch.setattr(configs, "require_email_verification", False)
    email = "cta-pro@example.test"
    assert _register(client, email).status_code == 200
    seed = store.get_user_auth(email)["seed"]

    # 两个购买入口只在试用耗尽后渲染
    _exhaust_trial(seed, email)
    dashboard = client.get("/dashboard").text
    assert "购买 Plus" in dashboard and "购买 Pro" in dashboard

    # 1. 跟随**渲染出来的** Pro CTA：超市页要预选 Pro，且提交值就是 Pro
    href = _cta_href(dashboard, "购买 Pro")
    assert href == f"/store?plan={PRO_CTA_PLAN}"
    body, rendered_plan = _store_preselection(client, href)
    assert rendered_plan == PRO_CTA_PLAN
    assert _axis_selected(body, "pro"), "Pro 入口没有把 Pro 档预选上"
    assert not _axis_selected(body, "plus")
    # 预选必须贯穿到用户看到的摘要价，而不只是隐藏字段
    assert "Pro · 独享 · 1 个月" in body and 'id="sel-price">¥199' in body
    # 以及脚本状态：否则浏览器一加载 render() 就把预选打回 Plus
    assert _js_selection(body) == {"tier": "pro", "density": "solo", "duration": "1m"}

    # 2. 把超市页渲染出来的那个值真的提交出去 → 落一张 Pro 订单
    csrf = client.cookies.get(configs.user_csrf_cookie) or ""
    resp = client.post("/api/checkout", data={"plan": rendered_plan, "csrf_token": csrf},
                       follow_redirects=True)
    assert resp.status_code == 200
    paid = [o for o in store.list_orders(email=email) if o["status"] == "paid"]
    assert [o["tier_id"] for o in paid] == [PRO_CTA_PLAN], "「购买 Pro」入口最终卖出的不是 Pro"

    # 3. Plus 入口没有被这次修复带偏：仍然预选 Plus
    plus_body, plus_plan = _store_preselection(client, _cta_href(dashboard, "购买 Plus"))
    assert plus_plan == PLUS_CTA_PLAN
    assert _axis_selected(plus_body, "plus") and not _axis_selected(plus_body, "pro")

    # 4. 未知 / 试图注入的值一律退回默认档，不能把任意字符串塞进结算表单
    for bad in ("", "free-solo-1m", "pro-solo-1m-extra", "pro-solo", "  pro-solo-1m  ",
                "<script>alert(1)</script>", "../../etc/passwd"):
        _, value = _store_preselection(client, "/store?plan=" + urllib.parse.quote(bad))
        assert value == "plus-solo-1m", f"非法 plan {bad!r} 未退回默认档，得到 {value!r}"

    # 5. 默认落地（无查询串）保持原样：Plus 独享月卡
    default_body, default_plan = _store_preselection(client, "/store")
    assert default_plan == "plus-solo-1m"
    assert _axis_selected(default_body, "plus") and _axis_selected(default_body, "1m")
    assert _js_selection(default_body) == {"tier": "plus", "density": "solo", "duration": "1m"}
    # 非法 plan 也不能只改脚本而不改服务端渲染（两者必须同源）
    invalid_body, _ = _store_preselection(client, "/store?plan=pro-solo-1m-extra")
    assert _js_selection(invalid_body) == {"tier": "plus", "density": "solo", "duration": "1m"}
