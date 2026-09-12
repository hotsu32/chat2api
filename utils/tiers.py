"""user.tier 档位模型（Stage 0/2 地基）。

把「用户订阅档位」立成真模型，与「账号本身档位」(`account.plan_type`) 严格分列：

  - ``account.plan_type`` = 号本身是什么档（free/plus/pro），来自 access token JWT。
  - ``user.tier`` = 用户在我们这儿买了哪一档（决定号池范围 / 可用模型 / 额度上限）。

档位目录 config-driven：优先读 ``data/tiers.json``，缺失时回落到内嵌默认档位。
每个档位枚举出三样东西（Stage 0 验收）：

  - ``account_plan_types``：该档可用哪些 account.plan_type 的号（号组范围）。
  - ``models``：该档可用模型白名单。
  - ``quota_limit`` / ``quota_period``：额度上限（次/周期）。

本模块只依赖 stdlib + ``utils.store``（惰性导入），不导入 FastAPI，纯函数可单测。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from utils.Logger import logger

# 内嵌默认档位（缺失 data/tiers.json 时的回落；也是测试/冒烟的无文件基线）。
# models 白名单与真实 2026 ChatGPT 型号 slug 对齐（2026-09-08 实测 /backend-api/models）：
#   free 号:  gpt-5-5 / gpt-5-6 / 各 mini / research / auto
#   plus 号:  额外有 instant / thinking / *-wm 系列
# auto 由 tier_allows_model 单独放行，不写进白名单。
_DEFAULT_TIERS: Dict[str, Dict[str, Any]] = {
    "free": {
        "label": "免费",
        "account_plan_types": ["free"],
        "group": "free",
        "quota_limit": 50,
        "quota_period": "day",
        "concurrency": "shared",
        "models": [
            "gpt-5-5", "gpt-5-6",
            "gpt-5-3-mini", "gpt-5-5-mini", "gpt-5-6-mini",
            "gpt-5-4-t-mini", "gpt-5-6-t-mini", "gpt-5-6-t-mini-mini",
            "research",
        ],
    },
    "plus": {
        "label": "Plus",
        "account_plan_types": ["plus"],
        "group": "plus",
        "quota_limit": 500,
        "quota_period": "day",
        "concurrency": "few_shared",
        "models": [
            "gpt-5-5", "gpt-5-5-instant", "gpt-5-6", "gpt-5-6-instant",
            "gpt-5-5-thinking", "gpt-5-6-thinking",
            "gpt-5.5-wm", "gpt-5.6-sol-wm", "gpt-5.6-terra-wm", "gpt-5.6-luna-wm", "gpt-6-astra-wm",
            "gpt-5-3-mini", "gpt-5-5-mini", "gpt-5-6-mini", "gpt-5-4-t-mini", "gpt-5-6-t-mini",
            "research",
        ],
    },
    "pro": {
        "label": "Pro",
        "account_plan_types": ["pro"],
        "group": "pro",
        "quota_limit": 2000,
        "quota_period": "day",
        "concurrency": "exclusive",
        "models": [
            "gpt-5-5", "gpt-5-5-instant", "gpt-5-6", "gpt-5-6-instant",
            "gpt-5-5-thinking", "gpt-5-6-thinking",
            "gpt-5.5-wm", "gpt-5.6-sol-wm", "gpt-5.6-terra-wm", "gpt-5.6-luna-wm", "gpt-6-astra-wm",
            "gpt-5-3-mini", "gpt-5-5-mini", "gpt-5-6-mini", "gpt-5-4-t-mini", "gpt-5-6-t-mini",
            "research",
        ],
    },
}

_DEFAULT_TIER_ID = "free"
_TIERS_FILE = os.path.join("data", "tiers.json")

_catalog: Optional[Dict[str, Dict[str, Any]]] = None


def _load_catalog() -> Dict[str, Dict[str, Any]]:
    """读 data/tiers.json；失败/缺失回落内嵌默认档位。"""
    try:
        with open(_TIERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        tiers = data.get("tiers") if isinstance(data, dict) else None
        if isinstance(tiers, dict) and tiers:
            return tiers
    except Exception as e:
        logger.warning(f"[tiers] load data/tiers.json failed, using defaults: {e}")
    return dict(_DEFAULT_TIERS)


def catalog() -> Dict[str, Dict[str, Any]]:
    global _catalog
    if _catalog is None:
        _catalog = _load_catalog()
    return _catalog


def reload_catalog() -> None:
    """强制重载（配置热更新 / 测试重置）。"""
    global _catalog
    _catalog = None


def list_tiers() -> Dict[str, Dict[str, Any]]:
    return catalog()


def get_tier(tier_id: str) -> Optional[Dict[str, Any]]:
    if not tier_id:
        return None
    return catalog().get(tier_id)


def default_tier_id() -> str:
    return _DEFAULT_TIER_ID


def normalize_tier_id(tier_id: Optional[str]) -> str:
    """未知/空档位回落默认档（免费），保证永远有档可落。"""
    if tier_id and tier_id in catalog():
        return tier_id
    return default_tier_id()


def tier_account_plan_types(tier_id: str) -> List[str]:
    """配置可以缩小号组，不能让 Plus/Pro 产品使用其他档账号。"""
    tier = get_tier(tier_id)
    if not tier:
        return []
    configured = tier.get("account_plan_types") or []
    if tier_id in ("plus", "pro"):
        return [tier_id] if tier_id in configured else []
    return configured


def tier_allows_model(tier_id: str, model: str) -> bool:
    """模型门禁：model 为空（未知/无模型）或 ``auto``（默认「自动选型」，由账号/上游
    决定具体型号）时放行；其余按档位白名单精确匹配。"""
    if not model:
        return True
    if model == "auto":
        return True
    tier = get_tier(tier_id)
    if not tier:
        return True
    allowed = tier.get("models")
    if not allowed:
        return True
    return model in allowed


def tier_quota(tier_id: str) -> Optional[int]:
    tier = get_tier(tier_id)
    if not tier:
        return None
    return tier.get("quota_limit")


def _quota_since(period: Optional[str]) -> int:
    now = int(time.time())
    if period == "day":
        return now - 86400
    if period == "month":
        return now - 30 * 86400
    return 0


def resolve_user_tier(seed: str) -> Optional[str]:
    """从**已支付且未过期的订单**解析用户当前档位；无 user_auth 行返回 None。

    返回 None 只说明「这个 seed 没有 SaaS 身份」，**不是**「可以随便用号池」：
    号池分配的授权契约在 ``chatgpt/authorization.py``（注册付费/Plus 试用，
    或显式运营者/导入/开发句柄），本函数只回答档位问题。历史上把 None 读成
    「运营者，不设限」正是匿名 cookie 拿到付费号池的那条链路。

    档位不再读 ``user_auth.tier_id``（那是「注册时写死 free 再也没变过」的死字段），
    改为每次从 orders 推导 —— orders 是唯一真相源，到期靠算不靠扫表回收。
    SaaS 用户但无有效套餐时返回 ``""``（有账号无权益），与 None 语义相反，
    调用方必须用 ``is None`` 区分；本函数对外只承诺「None = 无 SaaS 身份」这一条契约。

    数据层故障会向上抛 ``store.StoreError``：此时我们**无法**判断这个 seed 是运营者
    还是过期用户，静默按「不设限」处理等于把闸门焊死在开的位置。由 ``enforce_tier``
    决定怎么收口。
    """
    if not seed:
        return None
    from utils import entitlements
    from utils.store import StoreError

    try:
        return entitlements.effective_tier(seed)
    except StoreError:
        raise
    except Exception:
        raise StoreError("Entitlement resolution failed") from None


def user_usage_total(seed: str, tier_id: str) -> int:
    """该用户当前周期累计用量（落库 + 未 flush 的内存 pending 之和）。"""
    tier = get_tier(tier_id) or {}
    since = _quota_since(tier.get("quota_period"))
    try:
        from utils import usage as _usage
        return _usage.user_usage_total(seed, since)
    except Exception as e:
        # 用量子系统单独故障时按 0 计（额度闸放行）：此时用户已经通过了
        # resolve_user_tier 那道闸，确实持有有效套餐，因为统计不出用量就
        # 把付费用户拒之门外是更糟的误伤。但必须留声 —— 静默的 0 会让
        # 「额度形同虚设」这件事在日志里查无实据。
        logger.warning(f"[tiers] usage lookup failed, counting as 0: {e}")
        return 0


def enforce_tier(seed: str, model: Optional[str] = None) -> None:
    """请求前档位执行：有效套餐 → 模型门禁 + 额度上限。

    三态（见 ``utils.entitlements``）：

      - ``None``  无 user_auth 行（直传 token / 已授权的运营者 seed）→ 本函数不设档位限制。
        注意这里**不做**授权判断：谁能拿到号池账号由 ``chatgpt/authorization.py`` 的分配
        契约决定。本函数放行一个未知 seed，不等于它会拿到账号 —— 拿不到账号的请求会在
        取号阶段被拒（401/503），不会到达上游。
      - ``""``    SaaS 用户但无未过期的已支付订单 → 402 拒绝（未购买 / 已过期）。
      - 档位 id   按该档执行模型白名单 + 额度。

    违规抛 :class:`fastapi.HTTPException`（402 无有效套餐 / 403 模型 / 429 额度），
    由调用方直接向上返回。

    权益数据查不出来时抛 503 而非放行：分不清运营者和过期用户的时候，
    正确的答案是「暂时不可用」，不是「都放进来」。
    """
    from fastapi import HTTPException
    from utils.store import StoreError

    try:
        tier_id = resolve_user_tier(seed)
    except StoreError:
        logger.error("[tiers] entitlement lookup failed, refusing")
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")

    if tier_id is None:
        return  # 非 SaaS 用户（运营者 seed / 直传 token），不设限

    if not tier_id:
        from utils.seed_lifecycle import freeze_if_expired, LifecycleDenied
        try:
            freeze_if_expired(seed)
        except (StoreError, LifecycleDenied):
            logger.error("[tiers] Seed freeze unavailable, refusing")
            raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试") from None
        # 注册了但没买 / 买过但已过期 —— 不降级到免费档，直接拒绝并引导续费
        raise HTTPException(status_code=402, detail="套餐已过期或未购买，请前往续费")

    tier = get_tier(tier_id) or {}

    if model and not tier_allows_model(tier_id, model):
        raise HTTPException(status_code=403, detail="当前档位不包含此模型")

    # Plus/Pro 按有效期使用，不再把旧目录的每日次数作为付费门禁。
    # 注册试用的三次额度由独立预留/结算控制，不复用历史 usage 计数。
    if tier_id in ("plus", "pro"):
        return

    limit = tier.get("quota_limit")
    if limit is not None and limit > 0:
        used = user_usage_total(seed, tier_id)
        if used >= limit:
            raise HTTPException(status_code=429, detail="当前档位额度已用完")
