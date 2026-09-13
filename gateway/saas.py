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
import time
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import app, templates
import utils.audit as audit
import utils.configs as configs
import utils.entitlements as entitlements
import utils.payment as payment
import utils.plans as plans
import utils.ratelimit as ratelimit
import utils.seed_lifecycle as seed_lifecycle
import utils.store as store
import utils.trials as trials
import utils.usage as usage
from utils.Logger import logger
from utils.seed_lifecycle import LifecycleDenied
from gateway.user import (
    DISABLED_DETAIL,
    DISABLED_QUERY,
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

# 结算曾经的进程内锁（``_SETTLE_LOCK``）与自算到期时间（``_grant_expiry``）已移除。
# 「读当前到期时间 → 加时长 → 写回」这个累加现在由 ``store.settle_order`` 在单个
# ``BEGIN IMMEDIATE`` 事务里完成，跨连接、跨进程都成立；进程内锁只能保护同一个
# 进程，多 worker 部署下等于没有锁。详见 :func:`_settle_order`。
#
# 支付成功之后的账号分配见 :func:`_fulfil_order`：``paid`` 只是「付过钱」，
# 不等于「能聊天」，两者必须分开显示。


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

    失败时顺手把跳转也造好：会话是「因改密被吊销」或「账号被停用」的，登录页要能说出
    原因 —— 被踢的那台设备若只是莫名跳回登录页，看起来像 bug 而不像安全措施生效。
    一次校验同时得出两者，避免为了拿原因再查一遍库。
    """
    try:
        return _current_email(request), None
    except HTTPException as e:
        if e.detail == REVOKED_DETAIL:
            target = f"{_SIGNIN}?{REVOKED_QUERY}"
        elif e.detail == DISABLED_DETAIL:
            target = f"{_SIGNIN}?{DISABLED_QUERY}"
        else:
            target = _SIGNIN
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
    """已支付订单 → 订阅卡片列表（含有效期 / 剩余天数 / 健康态 / 可服务态）。

    ``healthy`` = 订单未过期，是权益层口径（决定有没有试用区、能不能续费）；
    ``serviceable`` = 未过期**且** Seed 已绑定到可用账号，决定要不要给「进入
    ChatGPT」入口。付了钱但没绑上号的订单不能显示成服务正常。

    卡片口径必须与 ``entitlements.active_orders`` 一致：权益层认的订单这里就得显示，
    否则老用户会「有权益但看不到卡片」，既没有续费入口，Dashboard 还会把他弹回超市。

    ``healthy`` 只说明这张订单自己没过期。可用的绑定只有一个，**入口也只能有一个**：
    有多张活动卡片时 Dashboard 会并列渲染多个「进入 ChatGPT」，而它们指向同一个
    Seed、只有一张能对上当前绑定 —— 到期前同档续费（叠加出第二段窗口）和 Plus 升 Pro
    （旧档还没到期）都会命中。所以活动卡片只保留**生效档次**（最高有效档，与
    ``_seed_binding`` 及权益层同口径）里到期最晚的那张。已过期的卡片是付款历史，
    原样保留：不删订单、不改支付记录，只是不再把它当作现在能用的服务。
    """
    now = int(time.time())
    binding = _seed_binding(email)
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
        # healthy 只说明「订单没过期」，serviceable 才是「现在真的能进聊天」。
        # 付款成功但账号分配失败时前者为真、后者为假，页面必须显示「待分配 + 重试」，
        # 而不是「服务正常 + 进入」—— 后者会把用户送进一个必然失败的入口。
        serviceable = healthy and binding["bound"]
        if not healthy:
            status_text = "已过期"
        elif serviceable:
            status_text = "服务正常"
        else:
            status_text = "待分配账号"
        subs.append({
            **card,
            "expires_at": _fmt_date(expires),
            "days_left": days_left,
            "healthy": healthy,
            "serviceable": serviceable,
            "status_text": status_text,
            # 归并活动卡片用的内部字段，返回前剔除（不进模板上下文）。
            "_tier": detail["tier"] if detail else card["plan_id"],
            "_expires": expires,
        })

    # 活动入口只留一个：生效档次里到期最晚的那张。直接询问权益层的
    # 公开接口，避免展示层依赖其私有档次排序实现。
    active = [s for s in subs if s["healthy"]]
    current = []
    if active:
        effective_tier = entitlements.effective_tier_for_email(email, now)
        candidates = [s for s in active if s["_tier"] == effective_tier]
        if candidates:
            current = [max(candidates, key=lambda s: s["_expires"])]
    history = [s for s in subs if not s["healthy"]]
    for s in subs:
        s.pop("_tier")
        s.pop("_expires")
    return current + history


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


# 分配失败原因 → 用户可读文案。原因是 ``utils.seed_lifecycle`` 定义的固定匿名代码，
# 不含账号、邮箱或异常原文，因此可以安全地经查询参数往返。
_ALLOCATION_MESSAGES = {
    "capacity_unconfigured": "当前套餐的账号容量尚未配置，请联系客服处理。",
    "capacity_exceeded": "当前套餐的账号已满，请稍后重试。",
    "exclusive_conflict": "当前套餐的独享账号已被占用，请稍后重试。",
    "account_unknown": "暂时没有可用账号，请稍后重试。",
    "account_not_healthy": "暂时没有可用账号，请稍后重试。",
    "no_healthy_candidate": "暂时没有可用账号，请稍后重试。",
    "cross_tier": "暂时没有同档可用账号，请稍后重试。",
    "auth_not_active": "账号状态异常，请联系客服处理。",
    "operator_seed": "账号状态异常，请联系客服处理。",
    "no_seed": "账号初始化未完成，请联系客服处理。",
    "no_entitlement": "订单权益尚未生效，请稍后重试。",
    "store_error": "系统繁忙，请稍后重试。",
}


def _allocation_message(reason: str) -> str:
    """把匿名失败代码翻成用户可读文案；未知代码退化到通用文案，不原样回显。"""
    return _ALLOCATION_MESSAGES.get(reason or "", "账号分配未完成，请稍后重试。")


def _seed_binding(email: str) -> dict:
    """该 email 的 Seed 当前绑定状态。

    ``store.get_user`` 在查不到行（含数据层故障）时返回 None，这里按「未绑定」处理。
    方向是 fail-closed：宁可在状态不明时显示一个可重试的「待分配」，也不能谎称
    服务已经可用、把用户送进一个必然失败的入口。
    """
    seed = _user_seed(email)
    if not seed:
        return {"bound": False, "seed": "", "account": "", "plan_type": "", "status": ""}
    row = store.get_user(seed) or {}
    account = row.get("current_account") or ""
    entitled_tier = entitlements.effective_tier_for_email(email)
    acct = store.get_account(account) if account else None
    serviceable = (
        bool(account) and row.get("status") == "active" and
        bool(entitled_tier) and row.get("plan_type") == entitled_tier and
        bool(acct) and acct.get("status") == "healthy" and
        acct.get("plan_type") == entitled_tier
    )
    return {
        "bound": serviceable,
        "seed": seed,
        "account": account,
        "plan_type": row.get("plan_type") or "",
        "status": row.get("status") or "",
    }


def _allocate_seed(email: str) -> tuple:
    """把已付费用户的 Seed 绑定到对应档次的健康账号，返回 ``(ok, reason)``。

    绑定逻辑完全复用 :func:`utils.seed_lifecycle.route_seed`：它在单个 SQLite 事务里
    读真实权益、优先复用原绑定、按容量与独享规则挑同档健康候选。这里不重复实现，
    也不试图绕过它的裁决。

    失败如实返回而不是吞成成功：付款事实已经落在订单上，绑定失败是一个**可以重试
    的后续步骤**，把它伪装成成功只会让用户对着一个进不去的入口反复点击。
    """
    seed = _user_seed(email)
    if not seed:
        return False, "no_seed"
    try:
        seed_lifecycle.route_seed(seed, configs.max_shared_seeds_per_account)
        return True, ""
    except LifecycleDenied as e:
        # reason 是固定匿名代码；不记录邮箱 / seed / 具体账号。
        logger.error("[saas] seed allocation denied")
        return False, e.reason
    except store.StoreError:
        # 注意：route_seed 的内存发布失败会在事务已提交之后抛 StoreError。
        # 此时数据库里的绑定其实已经写好，如实报失败 + 允许重试即可 ——
        # 重试是幂等的，会把内存条目按已持久化的状态重新发布。
        logger.error("[saas] seed allocation unavailable")
        return False, "store_error"


def _fulfil_order(order_id: str) -> dict:
    """结算订单并分配账号，返回 ``{"settled", "paid", "allocated", "reason"}``。

    两段语义必须分开，这是本函数存在的全部理由：

      - **结算**（:func:`utils.store.settle_order`）是支付事实。一旦 ``paid`` 就永不
        回滚，重复回调不会重复延长有效期，数据层故障则抛 ``StoreError`` 让调用方
        决定是否让支付网关重投；
      - **分配**（:func:`_allocate_seed`）是付款之后的服务落地，可能因为容量未配置、
        没有同档健康账号或数据库故障而失败。

    分配失败**不得**抹掉付款事实，也不得重复延长有效期。因此这里先结算、再分配，
    分配结果只是返回值的一部分。每次重复回调都会顺带重试一次分配，用户的
    Dashboard 上另有显式的重试入口，待分配状态因此是可恢复的。
    """
    settled = store.settle_order(order_id)  # 数据层故障向上抛 StoreError
    order = store.get_order(order_id, strict=True) or {}
    paid = (order.get("status") or "") == "paid"
    outcome = {"settled": bool(settled), "paid": paid, "allocated": False, "reason": ""}
    if not paid:
        return outcome
    ok, reason = _allocate_seed(order.get("email") or "")
    outcome["allocated"] = ok
    outcome["reason"] = reason
    if not ok:
        logger.error("[saas] paid order awaiting account allocation")
        # 失败原因不落库（不为它新增列），但**必须可检索**：运营台要能回答
        # 「这个人付了钱为什么还不能用」。记在这里而不是回调处理里 ——
        # 两条结算路径（mock 直结 / 渠道回调）都会走到这一步，只记回调路径
        # 会让本地联调与任何非回调结算的失败在运营台上完全不可见。
        audit.record("payment.awaiting_allocation", ok=False,
                     subject=audit.subject_id(order.get("email") or ""),
                     detail={"order_id": order_id, "reason": reason})
    return outcome


# ---------------------------------------------------------------------- 页面

# 超市页的默认预选档：没有任何（有效的）``?plan=`` 时的落地状态。
# 与 Dashboard 的「购买 Plus」入口指向同一档，保持历史默认体验不变。
_STORE_DEFAULT_PLAN = "plus-solo-1m"


@app.get("/store", response_class=HTMLResponse)
async def store_page(request: Request, expired: str = "", plan: str = ""):
    _, redirect = _page_auth(request)
    if redirect:
        return redirect
    # 入口链接（Dashboard 的「购买 Pro」）用 ``?plan=`` 指定要预选的档位。
    # 只有目录里真实存在的 plan_id 才生效，未知/缺失/畸形一律退回默认档 ——
    # 这样查询串既不能改变「默认落地」这一既有行为，也不能把任意字符串塞进
    # 结算表单的 hidden 值。校验走 :func:`plans.plan_detail`，与结算页同源。
    selected = plans.plan_detail(plan) or plans.plan_detail(_STORE_DEFAULT_PLAN)
    return _render_with_csrf(
        request, "store.html",
        {
            "prices": plans.prices(),
            "expired": bool(expired),
            "selected": selected,
            "nav_active": "store",
        },
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
async def dashboard_page(request: Request, fulfil: str = ""):
    email, redirect = _page_auth(request)
    if redirect:
        return redirect
    subs = _user_subscriptions(email)

    # 付费成功但账号分配没完成：必须显示成「待分配 + 重试」，而不是服务正常。
    # ``fulfil`` 只承载 :func:`_allocation_message` 认识的匿名代码，未知值退化到
    # 通用文案，不会被原样回显。
    fulfilment_pending = any(s["healthy"] and not s["serviceable"] for s in subs)
    fulfilment_msg = ""
    if fulfil == "ok":
        fulfilment_msg = "账号分配已完成，可以开始使用了。"
    elif fulfil:
        fulfilment_msg = _allocation_message(fulfil)
    elif fulfilment_pending:
        # 失败原因没有落库（不为它新增列，免得同一件事有第二份真相），所以首次渲染
        # 只说到「还没分配好」并指向重试入口；具体原因在用户点一次重试之后给出。
        fulfilment_msg = "账号分配尚未完成，请点击下方「重试分配」。"

    # 试用状态：DB 故障时渲染明确的不可用态，不使用零余额。
    trial_ctx: dict = {}
    try:
        ts = trials.trial_state(email, strict=True)
        eff = entitlements.effective_tier(_user_seed(email))
        # 有付费权益时试用区块收起（不展示），但要让模板知道已付费。
        has_paid = bool(subs and any(s.get("healthy") for s in subs))
        # 共享试用容量：0 = 未配置（fail-closed），此时**不能**分配任何共享账号。
        # 这是运维事实而不是账号状态，必须在页面上说清楚，否则用户看到的是
        # 「当前账号状态不支持使用试用额度」，会去找客服解封一个根本没被封的账号。
        capacity = int(getattr(configs, "max_shared_seeds_per_account", 0) or 0)
        trial_ctx = {
            "trial_db_ok": True,
            "trial_granted": ts["granted"],
            "trial_tier": ts["tier"],
            "trial_total": ts["total"],
            "trial_used": ts["used"],
            "trial_reserved": ts["reserved"],
            "trial_remaining": ts["remaining"],
            "trial_admission_balance": ts["admission_balance"],
            "trial_capacity_configured": capacity > 0,
            # 入口不可用时的**真实原因**（匿名代码，见 trials.trial_blocked_reason）。
            # 模板原来只有一个兜底分支，把「买过但到期」和「未验证/被封禁」渲染成
            # 同一句「当前账号状态不支持使用试用额度」—— 到期用户的账号是 active 的，
            # 这句话会把他推去找客服解封一个根本没被封的账号。判据与 reserve 同源，
            # 所以页面说得出的原因，就是准入裁决会给出的那一个。
            "trial_block_reason": trials.trial_blocked_reason(email, strict=True),
            # entry_enabled: 有试用权益且至少有一次未被在途占住的额度
            "trial_entry_enabled": (
                eff == "plus"
                and not has_paid
                and ts["granted"]
                and ts["remaining"] > 0
                and capacity > 0
            ),
            # all_in_flight: 额度未耗尽但全被在途预留占住（显示「忙碌」而非「耗尽」）
            "trial_busy": (
                eff == "plus"
                and not has_paid
                and ts["granted"]
                and ts["admission_balance"] > 0
                and ts["remaining"] == 0
            ),
            # exhausted: 注册赠额全部已结算用完
            "trial_exhausted": ts["granted"] and ts["admission_balance"] <= 0,
            "has_paid_plan": has_paid,
        }
    except store.StoreError:
        logger.error("[saas] dashboard trial_state unavailable")
        trial_ctx = {"trial_db_ok": False}

    return _render_with_csrf(
        request,
        "dashboard.html",
        {
            "email": email,
            "seed": _user_seed(email),
            "subscriptions": subs,
            "nav_active": "dashboard",
            "fulfilment_msg": fulfilment_msg,
            "fulfilment_pending": fulfilment_pending,
            **trial_ctx,
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
        try:
            outcome = _fulfil_order(order_id)
        except store.StoreError:
            # 结算没落库就等于这次付款没变成权益，不能假装成功把人送回控制台。
            logger.error("[saas] mock settlement unavailable")
            return _render_with_csrf(
                request, "checkout.html",
                {"plan": detail, "is_mock": True, "payable": True, "nav_active": "store",
                 "error": "系统繁忙，本次支付未能完成结算，请稍后在控制台重试。"},
                status_code=503,
            )
        # 记一条带 source=mock_auto_settle 的审计：演示结算绝不能被误读成真实收款。
        if outcome.get("settled"):
            audit.record("payment.settled", subject=audit.subject_id(email), detail={
                "order_id": order_id, "amount": detail["price"],
                "currency": payment.expected_currency(), "tier_id": detail["id"],
                "source": "mock_auto_settle",
            })
        return RedirectResponse(url="/dashboard", status_code=303)

    return RedirectResponse(url=f"/checkout?plan={detail['id']}&order={order_id}", status_code=303)


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
    """激活订单：单事务内叠加同档有效期并置 paid。重复调用不会延长有效期。

    累加语义（同 email 同档从现有到期时间往后叠、跨档各自并行）由
    :func:`utils.store.settle_order` 在一个 ``BEGIN IMMEDIATE`` 事务里完成 ——
    读和写之间不再有窗口，跨连接、跨进程都成立，因此这里不再持任何进程内锁。

    ``StoreError`` 向上传播而不是吞成 False：数据层故障要重投，重复回调不该重投，
    这两件事必须是可分的结果。
    """
    return store.settle_order(order_id)


@app.post("/api/payment/callback")
async def api_payment_callback(request: Request):
    """支付回调入口。真伪校验（签名）由 provider 的 ``verify`` 负责。

    不做任何登录态判断 —— 回调来自支付网关而非浏览器，鉴权靠 provider 签名。

    mock 渠道直接 403：mock 没有签名可验（``verify`` 永远返回 None），这个口一旦
    对外可达，任何注册用户「建单 + 自投回调」两步就能把自己的单置成已支付。
    mock 的结算全走 ``api_checkout`` 的 ``auto_settle``，本入口对它毫无用处。

    验签**不等于**可信结算：``payment.validate_callback`` 还会拿库里的订单逐条比对
    金额、币种、provider 事务 id 归属与重放（见 ``utils/payment`` 模块文档）。
    只有这一步过了才允许进入 ``_fulfil_order``。
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

    try:
        callback = payment.accept_callback(provider, payload)
        order = payment.validate_callback(provider, callback)
    except payment.PaymentError as e:
        # reason 是固定匿名代码；不记录回调原文（可能含渠道签名等敏感字段）。
        logger.error(f"[saas] payment callback rejected: {e.reason}")
        audit.record("payment.callback_rejected", ok=False,
                     detail={"reason": e.reason})
        if e.reason == "unknown_order":
            # 该单可能还没落库（跨进程可见性），重投是唯一可能补救的动作。
            raise HTTPException(status_code=500, detail="订单激活失败")
        # 金额 / 币种 / 流水号 / 渠道任何一条不符都是终局：重投一万次也还是不符。
        raise HTTPException(status_code=400, detail="回调校验失败")
    except store.StoreError:
        logger.error("[saas] payment callback verification unavailable")
        raise HTTPException(status_code=500, detail="订单激活失败")

    order_id = order["order_id"]
    subject = audit.subject_id(order.get("email") or "")
    # 结算 + 分配。数据层故障必须让网关看到失败并重投，否则这笔钱就静默沉没了。
    try:
        outcome = _fulfil_order(order_id)
    except store.StoreError:
        logger.error("[saas] payment callback settlement unavailable")
        raise HTTPException(status_code=500, detail="订单激活失败")

    if not outcome["paid"]:
        # 例外是重投也救不回来的单（套餐已下架）—— 置 failed 告警，不让网关空转。
        if _unfulfillable(order_id):
            audit.record("payment.unfulfillable", ok=False, subject=subject,
                         detail={"order_id": order_id, "reason": "plan_delisted"})
            return {"ok": False, "detail": "订单需人工处理"}
        raise HTTPException(status_code=500, detail="订单激活失败")

    # 走到这里付款已经落库，且重复投递不会延长有效期。剩下的只是账号分配：
    # 分配失败同样回 200 —— 钱确实收到了，重投回调也解决不了容量问题。
    # 恢复路径是 Dashboard 上的显式重试入口；重复回调会顺带再试一次。
    if outcome["settled"]:
        audit.record("payment.settled", subject=subject, detail={
            "order_id": order_id, "amount": order.get("amount"),
            "currency": payment.expected_currency(), "tier_id": order.get("tier_id"),
            "source": "provider_callback",
        })
    # 分配失败的审计由 _fulfil_order 记（两条结算路径共用一处），此处不重复记。
    return {"ok": True, "fulfilled": outcome["allocated"]}


@app.post("/api/fulfillment/retry")
async def api_fulfillment_retry(request: Request):
    """重试把已付费订单绑定到同档健康账号。

    付款事实与账号分配是两件事：已经付过钱、但绑定因为容量未配置或没有同档健康账号
    而失败的用户，需要一个明确且可以反复点击的恢复入口，而不是只能等客服。

    只操作当前登录用户自己的 Seed —— 不接收订单号或 seed 参数，因此不存在改别人
    绑定的可能；鉴权与 CSRF 与其它用户动作一致。重试是幂等的：绑定成功后再点一次
    只会重新确认同一绑定，不会延长有效期、也不会重复扣费。
    """
    email = _current_email(request)
    form = await request.form()
    _verify_csrf(request, form.get("csrf_token") or "")
    ok, reason = _allocate_seed(email)
    if ok:
        return RedirectResponse(url="/dashboard?fulfil=ok", status_code=303)
    # 只回传已知代码，未知原因退化到通用文案，URL 里绝不出现原始字符串。
    code = reason if reason in _ALLOCATION_MESSAGES else "failed"
    return RedirectResponse(url=f"/dashboard?fulfil={code}", status_code=303)


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
