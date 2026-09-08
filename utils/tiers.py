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
_DEFAULT_TIERS: Dict[str, Dict[str, Any]] = {
    "free": {
        "label": "免费",
        "account_plan_types": ["free"],
        "group": "free",
        "quota_limit": 50,
        "quota_period": "day",
        "concurrency": "shared",
        "models": ["gpt-5-mini", "gpt-4o-mini", "o4-mini"],
    },
    "plus": {
        "label": "Plus",
        "account_plan_types": ["plus"],
        "group": "plus",
        "quota_limit": 500,
        "quota_period": "day",
        "concurrency": "few_shared",
        "models": [
            "gpt-5-5", "gpt-5", "gpt-5-thinking", "gpt-4o", "o4-mini",
            "o3", "o3-deep-research", "gpt-4o-mini",
        ],
    },
    "pro": {
        "label": "Pro",
        "account_plan_types": ["plus", "pro"],
        "group": "pro",
        "quota_limit": 2000,
        "quota_period": "day",
        "concurrency": "exclusive",
        "models": [
            "gpt-5-5", "gpt-5-pro", "gpt-5-thinking", "o3-pro", "gpt-5",
            "o3", "o3-deep-research", "o4-mini-deep-research",
            "gpt-4o-deep-research", "deep-research", "gpt-4o",
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
    """该档可用的 account.plan_type 号组范围（半专属分池的过滤键）。"""
    tier = get_tier(tier_id)
    if not tier:
        return []
    return tier.get("account_plan_types") or []


def tier_allows_model(tier_id: str, model: str) -> bool:
    """模型门禁：model 为空（未知/无模型）时放行（交由上游/号本身约束）。"""
    if not model:
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
    """从 user_auth 解析用户订阅档；无 user_auth 行（运营者 seed / 直传 token）返回 None。

    返回 None 是「不设限」的信号：只对 SaaS 注册用户执行档位/额度，车队运营侧走原有
    主链路不受影响（Stage 2 的 fail-open 边界）。
    """
    if not seed:
        return None
    try:
        from utils import store as _store
        row = _store.get_user_auth_by_seed(seed)
        if not row:
            return None
        return normalize_tier_id(row.get("tier_id"))
    except Exception as e:
        logger.debug(f"[tiers] resolve_user_tier error: {e}")
        return None


def user_usage_total(seed: str, tier_id: str) -> int:
    """该用户当前周期累计用量（落库 + 未 flush 的内存 pending 之和）。"""
    tier = get_tier(tier_id) or {}
    since = _quota_since(tier.get("quota_period"))
    try:
        from utils import usage as _usage
        return _usage.user_usage_total(seed, since)
    except Exception:
        return 0


def enforce_tier(seed: str, model: Optional[str] = None) -> None:
    """请求前档位执行：模型门禁 + 额度上限。无 user_auth 行时不设限（fail-open）。

    违规抛 :class:`fastapi.HTTPException`（403 模型 / 429 额度），由调用方直接向上返回。
    """
    tier_id = resolve_user_tier(seed)
    if not tier_id:
        return  # 非 SaaS 用户（运营者 seed / 直传 token），不设限

    tier = get_tier(tier_id) or {}

    if model and not tier_allows_model(tier_id, model):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="当前档位不包含此模型")

    limit = tier.get("quota_limit")
    if limit is not None and limit > 0:
        used = user_usage_total(seed, tier_id)
        if used >= limit:
            from fastapi import HTTPException
            raise HTTPException(status_code=429, detail="当前档位额度已用完")
