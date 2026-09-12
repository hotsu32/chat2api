"""权益推导（Entitlements）—— 付费与权限之间的唯一桥梁。

设计原则：**orders 是唯一真相源，权益是算出来的，不是存出来的**。

不回写 ``user_auth.tier_id``，因此不存在「下单忘了回写」「sweeper 挂了收不回」
这类双真相源漂移。到期是每次查询时用时间戳算出来的，天然准确。

三态语义（``effective_tier`` 的返回值）：

  - ``None``   无 ``user_auth`` 行 —— 运营者 seed / 直传 token，不设限（fail-open）。
  - ``""``     有 ``user_auth`` 行但无有效套餐 —— 未购买或已过期，应当拒绝。
  - ``"plus"`` / ``"pro"`` 有效套餐的档次。

注意 ``None`` 与 ``""`` 语义相反，调用方必须用 ``is None`` 区分，不能用真值判断。

档次解析必须走 ``plans.parse_plan_id``：``orders.tier_id`` 存的是 plan_id
（形如 ``plus-solo-1m``），而 ``tiers.normalize_tier_id("plus-solo-1m")`` 不在档位
目录里会**回落 free**，误用会让所有付费用户被降级。

纯 stdlib + 惰性导入 ``utils.store``，不导入 FastAPI，可单测。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import utils.plans as plans

# 档次高低序（跨档并存时取最高档）
_TIER_RANK = {"plus": 1, "pro": 2}

# landing.py 旧路径往 orders.tier_id 写过裸档位（free/plus/pro，无密度无时长）。
# 这些行 plan_detail 解析不出来，给一个兼容映射，避免老用户权益凭空消失。
_LEGACY_TIER_DAYS = 30


def _order_window(order: Dict[str, Any]) -> Optional[tuple]:
    """订单 → ``(tier, start_ts, expires_ts)``；无法解析套餐时返回 None。

    ``expires_at`` 在支付激活时已物化落库（含同档续费叠加）；缺失时（历史数据）
    回落到 ``created_at + days``。
    """
    raw = order.get("tier_id") or ""
    created = int(order.get("created_at") or 0)

    detail = plans.plan_detail(raw)
    if detail:
        tier, days = detail["tier"], detail["days"]
    elif raw in _TIER_RANK:
        # 遗留裸档位行（landing.py 老格式），按月卡口径兜底
        tier, days = raw, _LEGACY_TIER_DAYS
    else:
        return None

    expires = order.get("expires_at")
    expires = int(expires) if expires else created + days * 86400
    return tier, created, expires


def active_orders(email: str, now: Optional[int] = None) -> List[Dict[str, Any]]:
    """该用户当前**未过期的已支付**订单（按到期时间倒序）。

    查询失败抛 ``store.StoreError`` 而非静默返回空列表 —— 空列表的含义是
    「没买过」，用它掩盖「查不到」会让一次 DB 故障静默变成全员免单或全员封禁。
    """
    from utils import store as _store

    now = int(time.time()) if now is None else now
    out = []
    for o in _store.list_orders(email=email, strict=True):
        if (o.get("status") or "") != "paid":
            continue
        window = _order_window(o)
        if not window:
            continue
        tier, _start, expires = window
        if expires <= now:
            continue
        out.append({**o, "_tier": tier, "_expires": expires})
    out.sort(key=lambda r: r["_expires"], reverse=True)
    return out


def tier_expiry(email: str, tier: str, now: Optional[int] = None) -> Optional[int]:
    """该用户在 ``tier`` 档上最晚的到期时间；无有效订单返回 None。

    续费叠加用：新单从当前到期时间起算，而不是从 now 起算（否则提前续费的天数被吞）。
    """
    latest = None
    for o in active_orders(email, now):
        if o["_tier"] != tier:
            continue
        if latest is None or o["_expires"] > latest:
            latest = o["_expires"]
    return latest


def effective_tier_for_email(email: str, now: Optional[int] = None) -> str:
    """该邮箱当前生效的档次（多单取最高档）；无有效套餐返回 ``""``。"""
    best, best_rank = "", -1
    for o in active_orders(email, now):
        rank = _TIER_RANK.get(o["_tier"], -1)
        if rank > best_rank:
            best, best_rank = o["_tier"], rank
    return best


def effective_tier(seed: str, now: Optional[int] = None) -> Optional[str]:
    """seed → 当前生效档次。

    ``None`` = 非 SaaS 用户（不设限）；``""`` = SaaS 用户但无有效套餐（应拒绝）。

    数据层查询失败时抛 ``store.StoreError``，**不**回落成 ``None`` ——
    否则一次锁库就等于给全体过期用户开闸。调用方按「有 user_auth 行就拒绝」处理。
    """
    if not seed:
        return None
    from utils import store as _store

    row = _store.get_user_auth_by_seed(seed, strict=True)
    if not row:
        return None  # 运营者 seed / 直传 token —— 保持既有 fail-open 契约
    if (row.get("status") or "") != "active":
        return ""  # 未验证 / 被封禁 —— 有账号但无权益
    return effective_tier_for_email(row.get("email") or "", now)


def subscription_expiry(seed: str, now: Optional[int] = None) -> Optional[int]:
    """seed 当前生效套餐的到期时间戳（取最晚）；无有效套餐返回 None。

    与 ``effective_tier`` 同样走 ``strict=True``：本函数目前无调用方，但只要有人
    拿它做「还能用到几号」的判断，非 strict 读就会把查库失败说成「没套餐」。
    """
    from utils import store as _store

    row = _store.get_user_auth_by_seed(seed, strict=True) if seed else None
    if not row:
        return None
    latest = None
    for o in active_orders(row.get("email") or "", now):
        if latest is None or o["_expires"] > latest:
            latest = o["_expires"]
    return latest
