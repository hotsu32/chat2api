"""套餐目录（Plans Catalog）。

套餐 = 账号档次(plus/pro) × 共享密度(solo/shared) × 时长(1d/1w/1m) = 12 种组合。

  - ``plan_id`` 形如 ``"plus-solo-1m"``（档次-密度-时长）。
  - ``orders.tier_id`` 字段存的就是这个 ``plan_id``（下单买的「什么」）。
  - 价格本轮为**占位价**，运行后再定（见计划「非目标」）。

与 ``utils/tiers.py`` 严格分列：
  - ``tiers.py`` 管 ``account.plan_type``（号本身的档：free/plus/pro）→ 可用模型/号组。
  - 本模块管**用户买的套餐**（档次×密度×时长）→ 号组 + 有效期 + 价格。

纯函数 + stdlib，不导入 FastAPI，可单测。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

TIERS: Tuple[str, ...] = ("plus", "pro")
DENSITIES: Tuple[str, ...] = ("solo", "shared")
DURATIONS: Tuple[str, ...] = ("1d", "1w", "1m")

TIER_LABEL = {"plus": "Plus", "pro": "Pro"}
DENSITY_LABEL = {"solo": "独享", "shared": "拼车"}
DURATION_LABEL = {"1d": "1 天", "1w": "1 周", "1m": "1 个月"}
DURATION_KICKER = {"1d": "日卡", "1w": "周卡", "1m": "月卡"}
DURATION_UNIT = {"1d": "/ 天", "1w": "/ 周", "1m": "/ 月"}
DURATION_DAYS = {"1d": 1, "1w": 7, "1m": 30}

# 占位价（元）：档次 → 密度 → 时长
PRICE_TABLE: Dict[str, Dict[str, Dict[str, int]]] = {
    "plus": {
        "solo": {"1d": 6, "1w": 32, "1m": 99},
        "shared": {"1d": 2, "1w": 10, "1m": 39},
    },
    "pro": {
        "solo": {"1d": 12, "1w": 65, "1m": 199},
        "shared": {"1d": 4, "1w": 20, "1m": 69},
    },
}

# 该套餐对应的号组（半专属分池的过滤键；与 tiers.py 的 group 对齐）
_TIER_GROUP = {"plus": "plus", "pro": "pro"}


def prices() -> Dict[str, Dict[str, Dict[str, int]]]:
    """价格表（供超市页前端实时组合价使用）。"""
    return PRICE_TABLE


def parse_plan_id(plan_id: Optional[str]) -> Optional[Tuple[str, str, str]]:
    """``"plus-solo-1m"`` → ``("plus", "solo", "1m")``；非法返回 None。"""
    if not plan_id or not isinstance(plan_id, str):
        return None
    parts = plan_id.split("-")
    if len(parts) != 3:
        return None
    tier, density, duration = parts
    if tier not in TIERS or density not in DENSITIES or duration not in DURATIONS:
        return None
    return tier, density, duration


def price_of(tier: str, density: str, duration: str) -> Optional[int]:
    try:
        return PRICE_TABLE[tier][density][duration]
    except KeyError:
        return None


def plan_short_label(plan_id: str) -> str:
    """``"Plus · 独享 · 月卡"``（列表/订单里的简短标签）。"""
    parsed = parse_plan_id(plan_id)
    if not parsed:
        return plan_id or "—"
    tier, density, duration = parsed
    return f"{TIER_LABEL[tier]} · {DENSITY_LABEL[density]} · {DURATION_KICKER[duration]}"


def plan_detail(plan_id: str) -> Optional[Dict[str, Any]]:
    """套餐完整详情（超市页/结算页/Dashboard 卡片共用）。"""
    parsed = parse_plan_id(plan_id)
    if not parsed:
        return None
    tier, density, duration = parsed
    price = price_of(tier, density, duration)
    if price is None:
        return None
    if density == "solo":
        desc = "专属号池，独立会话，稳定不掉线。"
    else:
        desc = "多人共享同一号池，性价比高，稳定性一般。"
    return {
        "id": plan_id,
        "tier": tier,
        "density": density,
        "duration": duration,
        "group": _TIER_GROUP[tier],
        "kicker": plan_short_label(plan_id),
        "name": f"ChatGPT {TIER_LABEL[tier]} 队列",
        "desc": desc,
        "price": price,
        "unit": DURATION_UNIT[duration],
        "days": DURATION_DAYS[duration],
        "label": f"{TIER_LABEL[tier]} · {DENSITY_LABEL[density]} · {DURATION_LABEL[duration]}",
    }


def all_plans() -> List[Dict[str, Any]]:
    """全部 12 种套餐详情（列表顺序：档次 → 密度 → 时长）。"""
    out: List[Dict[str, Any]] = []
    for tier in TIERS:
        for density in DENSITIES:
            for duration in DURATIONS:
                detail = plan_detail(f"{tier}-{density}-{duration}")
                if detail:
                    out.append(detail)
    return out


def has_active_plan(email: str) -> bool:
    """是否存在**未过期**的已支付订单（登录后分流 / Dashboard 空态共用）。

    口径与权益层一致（含续费叠加物化的 ``expires_at``），避免两处各算一套有效期。
    惰性导入，保持本模块对 FastAPI / 数据层无顶层依赖。

    这是**导航**判断不是**授权**判断：查库失败时答 False（把人送去 /store）比抛 500
    体验好，而真正的闸在 ``enforce_tier``，不会因为这里答错而漏放。
    """
    from utils import entitlements as _entitlements
    from utils.store import StoreError

    try:
        return bool(_entitlements.active_orders(email))
    except StoreError:
        return False
