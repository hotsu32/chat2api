"""SaaS 用户侧页面路由（本轮：前端 8 页 + 核心链路最小联动）。

页面：
  ``/store``      超市选购（三列点选 + 实时报价）
  ``/checkout``   结算确认（占位支付）
  ``/dashboard``  登录后的控制台（套餐入口卡片 + 空态购买入口）
  ``/wallet``     钱包 / 账户（我的套餐 + 订单 + 兑换码 + 邀请）
  ``/usage``      使用记录
  ``/settings``   个人设置（改密 / 邀请码 / 退出）

动作：
  ``POST /api/checkout``  下单 → 占位支付 mock 成功 → 写 ``orders(status=paid)``
  ``POST /api/redeem``    兑换码（占位）
  ``POST /api/password``  修改密码

登录态 / CSRF 复用 ``gateway.user``；套餐目录见 ``utils.plans``。

页面侧未登录 → 303 ``/signin``（而非 JSON 401）；真实支付接入前 ``method`` 仅记录不扣款。
"""
from __future__ import annotations

import hashlib
import secrets as pysecrets
import threading
import time
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import app, templates
import utils.configs as configs
import utils.entitlements as entitlements
import utils.payment as payment
import utils.plans as plans
import utils.ratelimit as ratelimit
import utils.store as store
import utils.usage as usage
from utils.Logger import logger
from gateway.user import (
    REVOKED_DETAIL,
    REVOKED_QUERY,
    _current_email,
    _render_with_csrf,
    _set_session_cookies,
    _signin_email_key,
    _verify_csrf,
    hash_password,
    verify_password,
)

_SIGNIN = "/signin"

# 结算锁：续费叠加是「读到期时间 → 加时长 → 写回」，同一用户的两笔订单并发结算时
# 必须串行，否则后一笔会覆盖前一笔的累加（用户付两个月只拿到一个月）。
# 用一把全局锁而不是 per-email 锁：结算是低频且极短的操作（两三次 DB 调用），
# 全局串行的代价可以忽略，而 per-email 锁字典只增不减、会随客户数无限长大。
# 锁序固定为 _SETTLE_LOCK -> store._WRITE_LOCK，不存在反向获取，故无死锁。
# 多进程部署时本锁失效，需换 DB 行锁 / Redis 锁。
_SETTLE_LOCK = threading.Lock()


# --------------------------------------------------------------------- helpers

def _fmt_date(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _invite_code(email: str) -> str:
    """占位邀请码。

    TODO(返利上线前必须换掉)：``sha256(email)[:6]`` 是纯函数，任何人都能算出
    任意用户的邀请码。现在只是展示，无害；一旦邀请返利真的付钱，这就是一个
    「输入别人邮箱即可冒领其返利」的接口。换成建表存服务端随机码 + 唯一约束。
    """
    return hashlib.sha256((email or "").encode("utf-8")).hexdigest()[:6].upper()


def _user_seed(email: str) -> str:
    row = store.get_user_auth(email) or {}
    return row.get("seed") or ""


def _page_auth(request: Request):
    """页面侧登录校验，返回 ``(email, redirect)``：一个为真另一个必为 None。

    API 侧直接用 ``_current_email``（抛 401），页面侧要跳转，二者语义不同。

    失败时顺手把跳转也造好：会话是「因改密被吊销」的，登录页要能说出原因 ——
    被踢的那台设备若只是莫名跳回登录页，看起来像 bug 而不像安全措施生效。
    一次校验同时得出两者，避免为了拿原因再查一遍库。
    """
    try:
        return _current_email(request), None
    except HTTPException as e:
        target = f"{_SIGNIN}?{REVOKED_QUERY}" if e.detail == REVOKED_DETAIL else _SIGNIN
        return None, RedirectResponse(url=target, status_code=303)


def _sub_expiry(order: dict) -> Optional[int]:
    """订单 → 到期时间戳；无法解析套餐时返回 None。

    ``expires_at`` 由支付激活时物化（含同档续费叠加）；历史数据缺该列时按
    ``created_at + days`` 兜底。
    """
    detail = plans.plan_detail(order.get("tier_id") or "")
    if not detail:
        return None
    expires = order.get("expires_at")
    if expires:
        return int(expires)
    return int(order.get("created_at") or 0) + detail["days"] * 86400


def _user_subscriptions(email: str) -> list:
    """已支付订单 → 订阅卡片列表（含有效期 / 剩余天数 / 健康态）。

    健康检查本轮简化为「未过期即正常」；号池真实可用性后续接入 antiban。

    卡片口径必须与 ``entitlements.active_orders`` 一致：权益层认的订单这里就得显示，
    否则老用户会「有权益但看不到卡片」，既没有续费入口，Dashboard 还会把他弹回超市。
    """
    now = int(time.time())
    subs = []
    for o in store.list_orders(email=email):
        if (o.get("status") or "") != "paid":
            continue
        detail = plans.plan_detail(o.get("tier_id") or "")
        if detail:
            expires = _sub_expiry(o)
            card = {
                "plan_id": detail["id"],
                "kicker": detail["kicker"],
                "name": detail["name"],
                "desc": detail["desc"],
                "price": detail["price"],
                "renew_url": f"/checkout?plan={detail['id']}",
            }
        else:
            # landing.py 老路径写过裸档位（free/plus/pro）。权益层按月卡兜底认它，
            # 展示层也必须认，并把续费引导指到该档的月卡上。
            legacy = _legacy_card(o)
            if not legacy:
                continue
            expires, card = legacy
        if expires is None:
            continue
        healthy = expires > now
        days_left = max(0, (expires - now + 86399) // 86400) if healthy else 0
        subs.append({
            **card,
            "expires_at": _fmt_date(expires),
            "days_left": days_left,
            "healthy": healthy,
            "status_text": "服务正常" if healthy else "已过期",
        })
    return subs


def _legacy_card(order: dict) -> Optional[tuple]:
    """遗留裸档位订单 → ``(expires_ts, 卡片字段)``；不是遗留格式则 None。

    口径与 ``entitlements._order_window`` 的兜底保持一致（月卡 30 天）。
    """
    tier = (order.get("tier_id") or "").strip()
    if tier not in ("plus", "pro"):
        return None
    expires = order.get("expires_at")
    expires = int(expires) if expires else int(order.get("created_at") or 0) + 30 * 86400
    renew_plan = f"{tier}-shared-1m"
    return expires, {
        "plan_id": tier,
        "kicker": f"{plans.TIER_LABEL[tier]} · 旧版套餐",
        "name": f"ChatGPT {plans.TIER_LABEL[tier]} 队列",
        "desc": "早期套餐，按月计算有效期。续费请选择下方对应档位。",
        "price": "—",
        "renew_url": f"/checkout?plan={renew_plan}",
    }


def _order_rows(email: str) -> list:
    rows = []
    now = int(time.time())
    for o in store.list_orders(email=email):
        status = (o.get("status") or "").lower()
        if status == "pending" and now - int(o.get("created_at") or 0) > configs.order_pending_ttl:
            status = "expired"
        rows.append({
            "order_id": o.get("order_id") or "",
            "plan_id": o.get("tier_id") or "",
            "plan_label": plans.plan_short_label(o.get("tier_id") or ""),
            "amount": o.get("amount") or "0",
            "status_text": {"paid": "已支付", "pending": "待支付", "failed": "失败", "expired": "已过期"}.get(status, status or "—"),
            "created_at": _fmt_date(int(o.get("created_at") or 0)) if o.get("created_at") else "—",
        })
    return rows


_KIND_LABELS = {"conversation": "对话", "image": "图片生成", "audio": "语音"}
_USAGE_DAYS = 30


def _usage_rows(email: str) -> list:
    """Aggregate recent events by day and public kind label."""
    seed = _user_seed(email)
    since = int(time.time()) - _USAGE_DAYS * 86400
    groups = {}
    for event in usage.user_daily_usage(seed, since=since):
        kind = _KIND_LABELS.get(event.get("kind"), "其他")
        key = (event.get("date") or "—", kind)
        groups[key] = groups.get(key, 0) + int(event.get("count") or 0)
    return [
        {"at": date, "plan": kind, "sessions": count}
        for (date, kind), count in sorted(groups.items(), reverse=True)
    ]


def _reusable_pending_order(email: str, plan_id: str, price: int) -> Optional[dict]:
    """同 email + 同套餐 + 同价 的未超时 pending 单 → 复用之。

    防双击：连点下单不能产生多张并行订单，否则回调把它们全置 paid 时
    用户会拿到成倍的有效期（或在 mock 下白拿）。

    金额必须也相同：定价改过之后，旧 pending 单存的是改价前的快照，
    复用它等于让用户按旧价付款拿新套餐，差额没有任何人会发现。
    """
    now = int(time.time())
    for o in store.list_orders(email=email):
        if (o.get("status") or "") != "pending":
            continue
        if (o.get("tier_id") or "") != plan_id:
            continue
        if str(o.get("amount") or "") != str(price):
            continue
        if now - int(o.get("created_at") or 0) <= configs.order_pending_ttl:
            return o
    return None


def _grant_expiry(email: str, detail: dict, now: Optional[int] = None) -> int:
    """支付成功后该订单的到期时间：同档有活跃套餐则从其到期时间叠加，否则从现在起算。

    这样提前续费不会吞掉剩余天数（并行双时钟是纯坑用户）。
    跨档不折算：高低档时钟各自并行走，由权益层取最高档。
    """
    now = int(time.time()) if now is None else now
    base = entitlements.tier_expiry(email, detail["tier"], now) or now
    return max(base, now) + detail["days"] * 86400


# ---------------------------------------------------------------------- 页面

@app.get("/store", response_class=HTMLResponse)
async def store_page(request: Request, expired: str = ""):
    _, redirect = _page_auth(request)
    if redirect:
        return redirect
    return _render_with_csrf(
        request, "store.html",
        {"prices": plans.prices(), "expired": bool(expired), "nav_active": "store"},
    )


@app.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request, plan: str = "", order: str = ""):
    email, redirect = _page_auth(request)
    if redirect:
        return redirect
    detail = plans.plan_detail(plan)
    if not detail:
        return RedirectResponse(url="/store", status_code=303)

    order_state = None
    if order:
        found = store.get_order(order)
        if found and found.get("email") == email:
            status = (found.get("status") or "").lower()
            if status == "pending" and int(time.time()) - int(found.get("created_at") or 0) > configs.order_pending_ttl:
                status = "expired"
            order_state = {
                "order_id": found.get("order_id") or "",
                "amount": found.get("amount") or detail["price"],
                "status": status,
            }
        elif found:
            logger.warning(f"[saas] checkout order ownership mismatch email={email} order={order}")

    # 渠道没开通就在 GET 侧说清楚，别让用户选完支付方式、点了确认才吃 503
    provider = payment.get_provider()
    return _render_with_csrf(
        request, "checkout.html",
        {
            "plan": detail,
            "order_state": order_state,
            "is_mock": bool(provider and getattr(provider, "is_mock", False)),
            "payable": provider is not None,
            "nav_active": "store",
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    email, redirect = _page_auth(request)
    if redirect:
        return redirect
    subs = _user_subscriptions(email)
    return _render_with_csrf(
        request,
        "dashboard.html",
        {
            "email": email,
            "seed": _user_seed(email),
            "subscriptions": subs,
            "nav_active": "dashboard",
        },
    )


@app.get("/wallet", response_class=HTMLResponse)
async def wallet_page(request: Request, redeem_msg: str = ""):
    email, redirect = _page_auth(request)
    if redirect:
        return redirect
    return _render_with_csrf(
        request,
        "wallet.html",
        {
            "email": email,
            "seed": _user_seed(email),
            "subscriptions": _user_subscriptions(email),
            "orders": _order_rows(email),
            "invite_code": _invite_code(email),
            "redeem_msg": redeem_msg or None,
            "nav_active": "wallet",
        },
    )


@app.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request):
    email, redirect = _page_auth(request)
    if redirect:
        return redirect
    return _render_with_csrf(
        request,
        "usage.html",
        {
            "email": email,
            "subscriptions": _user_subscriptions(email),
            "records": _usage_rows(email),
            "nav_active": "usage",
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, pw_msg: str = ""):
    email, redirect = _page_auth(request)
    if redirect:
        return redirect
    return _render_with_csrf(
        request,
        "settings.html",
        {"email": email, "invite_code": _invite_code(email), "pw_msg": pw_msg or None, "nav_active": "settings"},
    )


# ---------------------------------------------------------------------- 动作

@app.post("/api/checkout")
async def api_checkout(request: Request):
    """下单：建 pending 单 → 交给支付 provider。

    权益只认 ``paid``，而置 paid 只能经 :func:`_settle_order`（provider 激活路径），
    页面侧无法直接把订单写成已支付。
    """
    email = _current_email(request)
    form = await request.form()
    _verify_csrf(request, form.get("csrf_token") or "")
    detail = plans.plan_detail((form.get("plan") or "").strip())
    if not detail:
        raise HTTPException(status_code=400, detail="无效套餐")

    # 支付渠道未配置 → 拒绝下单（fail-closed）。没接真支付时宁可不能买，
    # 也不能让用户点一下就白拿套餐。
    try:
        provider = payment.require_provider()
    except payment.PaymentError as e:
        return _render_with_csrf(
            request, "checkout.html",
            {"plan": detail, "is_mock": False, "payable": False,
             "nav_active": "store", "error": str(e)},
            status_code=503,
        )

    # 幂等：复用未超时的同套餐 pending 单，防双击产生多张并行订单
    existing = _reusable_pending_order(email, detail["id"], detail["price"])
    if existing:
        order_id = existing["order_id"]
    else:
        order_id = "ord_" + pysecrets.token_hex(12)
        # 金额一律服务端定价，不接受客户端传值
        store.create_order(order_id, email, detail["id"], str(detail["price"]), status="pending")

    order = store.get_order(order_id) or {}
    begin = provider.begin(order)

    # mock 渠道：立即结算（仅本地联调；真实渠道走异步回调 /api/payment/callback）
    if begin.get("auto_settle"):
        _settle_order(order_id)
        return RedirectResponse(url="/dashboard", status_code=303)

    return RedirectResponse(url=f"/checkout?plan={detail['id']}&order={order_id}", status_code=303)


def _already_paid(order_id: str) -> bool:
    """订单是否已处于 paid —— 用于区分「回调重投」与「真的激活失败」。"""
    order = store.get_order(order_id) or {}
    return (order.get("status") or "") == "paid"


def _unfulfillable(order_id: str) -> bool:
    """订单能否被这版代码激活；不能则置 ``failed`` 并返回 True。

    套餐下架后，旧的 pending 单再也解析不出 ``plan_detail``，``_settle_order``
    会永远返回 False。若照常回 500，支付网关会无限重投一笔谁也激活不了的单。
    置终态 + 告警，让重投停下来、让人来处理（补发或退款）。
    """
    order = store.get_order(order_id)
    if not order or (order.get("status") or "") != "pending":
        return False
    if plans.plan_detail(order.get("tier_id") or ""):
        return False
    logger.error(
        f"[saas] order {order_id} paid for a delisted plan "
        f"{order.get('tier_id')!r}; marked failed, needs manual fulfilment"
    )
    store.update_order_status(order_id, "failed")
    return True


def _settle_order(order_id: str) -> bool:
    """激活订单：算好到期时间 → 原子置 paid。重复调用不会延长有效期。

    整段持锁：``_grant_expiry`` 是「读当前到期时间 → 加时长」，读和写之间若插进
    另一笔同档订单的激活，两笔会算出同一个 base，用户付两个月只拿到一个月。
    ``activate_order`` 自身的原子 UPDATE 只保护单张订单，保护不了这个跨订单的累加。
    """
    with _SETTLE_LOCK:
        order = store.get_order(order_id)
        if not order or (order.get("status") or "") != "pending":
            return False
        detail = plans.plan_detail(order.get("tier_id") or "")
        if not detail:
            return False
        expires = _grant_expiry(order.get("email") or "", detail)
        return store.activate_order(order_id, expires)


@app.post("/api/payment/callback")
async def api_payment_callback(request: Request):
    """支付回调入口。真伪校验（签名 / 金额 / 归属）由 provider 的 ``verify`` 负责。

    不做任何登录态判断 —— 回调来自支付网关而非浏览器，鉴权靠 provider 签名。

    mock 渠道直接 403：mock 的 ``verify`` 只回显 order_id，没有签名可验，这个口一旦
    对外可达，任何注册用户「建单 + 自投回调」两步就能把自己的单置成已支付。
    mock 的结算全走 ``api_checkout`` 的 ``auto_settle``，本入口对它毫无用处。
    """
    provider = payment.get_provider()
    if not provider:
        raise HTTPException(status_code=503, detail="支付渠道未开通")
    if getattr(provider, "is_mock", False):
        raise HTTPException(status_code=403, detail="演示渠道不接受支付回调")
    try:
        payload = await request.json()
    except Exception:
        payload = dict(await request.form())
    order_id = provider.verify(payload)
    if not order_id:
        raise HTTPException(status_code=400, detail="回调校验失败")
    # 幂等：重复投递不会延长有效期。但「已付款却激活不了」（订单不存在 / DB 写失败）
    # 必须让网关看到失败并重投，否则这笔钱就静默沉没了。
    # 例外是重投也救不回来的单（套餐已下架）—— 置 failed 告警，不让网关空转。
    if not _settle_order(order_id) and not _already_paid(order_id):
        if _unfulfillable(order_id):
            return {"ok": False, "detail": "订单需人工处理"}
        raise HTTPException(status_code=500, detail="订单激活失败")
    return {"ok": True}


@app.post("/api/redeem")
async def api_redeem(request: Request):
    _current_email(request)
    form = await request.form()
    _verify_csrf(request, form.get("csrf_token") or "")
    code = (form.get("code") or "").strip()
    msg = "兑换码功能暂未开放" if code else "请输入兑换码"
    return RedirectResponse(url=f"/wallet?redeem_msg={msg}", status_code=303)


@app.post("/api/password")
async def api_password(request: Request):
    email = _current_email(request)
    form = await request.form()
    _verify_csrf(request, form.get("csrf_token") or "")
    old = form.get("old") or ""
    new = form.get("new") or ""
    # 改密同样要限流。它虽然要求已登录，但 old 字段就是一个密码预言机：
    # 拿到一张会话 cookie 的人可以在这里慢慢猜原密码，猜中即可改密 —— 而改密会吊销
    # 所有会话，等于把「临时借到的登录态」升级成「真正的账号所有权」，真主人被锁在门外。
    # 复用登录的账号桶（同一账号的密码尝试，不论从哪个入口来，都该算在一起）。
    pw_key = _signin_email_key(email)
    if ratelimit.over_limit(pw_key, configs.signin_email_rate_limit,
                            configs.signin_email_rate_window):
        return RedirectResponse(
            url="/settings?pw_msg=密码尝试过于频繁，请稍后再试", status_code=303)
    row = store.get_user_auth(email) or {}
    if not verify_password(old, row.get("password_hash") or ""):
        ratelimit.hit(pw_key, configs.signin_email_rate_window)
        return RedirectResponse(url="/settings?pw_msg=当前密码不正确", status_code=303)
    if len(new) < 8:
        return RedirectResponse(url="/settings?pw_msg=新密码至少 8 位", status_code=303)
    store.upsert_user_auth(email, password_hash=hash_password(new))
    # 改密必须吊销此前签发的全部会话 —— 否则「我改了密码」并不能把偷走 cookie 的人赶走，
    # 而这正是用户改密码的主要动机。
    try:
        pw_version = store.bump_pw_version(email)
    except store.StoreError as e:
        # 不能报「已更新」了事：密码变了但旧会话还活着，用户会以为自己安全了。
        logger.error(f"[saas] 改密后吊销会话失败 {email}: {e}")
        return RedirectResponse(
            url="/settings?pw_msg=密码已更新，但旧登录状态未能全部失效，请联系客服",
            status_code=303,
        )
    # 证明了知道旧密码 → 清账号桶，本人不该因为之前几次笔误还被卡着
    ratelimit.reset(pw_key)
    resp = RedirectResponse(url="/settings?pw_msg=密码已更新，其他设备需重新登录", status_code=303)
    # 当前这台设备换发新版本的 cookie：它刚刚证明了自己知道旧密码，没理由把操作者自己踢下线。
    _set_session_cookies(request, resp, email, pw_version)
    return resp
