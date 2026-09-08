import asyncio
import json
import random

from fastapi import HTTPException

import utils.configs as configs
import utils.globals as globals
import utils.store as store
from chatgpt.refreshToken import rt2ac, sess2ac
from utils.Logger import logger
from utils.antiban import circuit as antiban_circuit


def _account_is_usable(token: str) -> bool:
    """号是否可用：非熔断死、非错误列表、非无账号行、非手动 disabled。"""
    if not token:
        return False
    if antiban_circuit.is_token_dead(token):
        return False
    if token in globals.error_token_list:
        return False
    acct = store.get_account(token)
    if not acct:
        return False
    if acct.get("status") == "disabled":
        return False
    return True


def _account_tier(token: str):
    """读取账号的 plan_type 等级（SQLite 真相源）。"""
    if not token:
        return None
    acct = store.get_account(token)
    return (acct or {}).get("plan_type")


def _seed_plan_types(seed: str):
    """半专属分池：user.tier → account.plan_type 号组范围。无 user_auth 行返回 None（回落旧行为）。"""
    try:
        from utils.tiers import resolve_user_tier, tier_account_plan_types
        tier_id = resolve_user_tier(seed)
        if not tier_id:
            return None
        return tier_account_plan_types(tier_id) or None
    except Exception:
        return None


def _pick_healthy_account(tier=None, plan_types=None):
    """从等级号池挑一个健康账号；等级池空则回退任意健康账号。

    plan_types（account.plan_type 集合）优先于 tier：供 user.tier → 号组 的半专属分池。
    候选额外经 _account_is_usable 过滤（剔除熔断 dead / error / disabled），
    否则 mark_dead 只写 antiban_dead_tokens 不改 accounts.status，
    failover 会把死号重选回来（见真号冒烟：60 采样命中死号 31 次）。
    """
    def _usable(candidates):
        return [c for c in candidates if _account_is_usable(c.get("token", ""))]

    if plan_types:
        candidates: list = []
        for pt in plan_types:
            candidates.extend(store.get_account_by_plan(pt, status="healthy"))
        usable = _usable(candidates)
        if usable:
            return random.choice(usable)["token"]
    else:
        tier = tier or "free"
        candidates = store.get_account_by_plan(tier, status="healthy")
        usable = _usable(candidates)
        if usable:
            return random.choice(usable)["token"]
    usable = _usable(store.get_healthy_accounts())
    return random.choice(usable)["token"] if usable else ""


def _resolve_seed_account(seed: str) -> str:
    """粘性路由：seed 已绑定健康账号则复用，否则按 user.tier 号组分配/切换并写回。"""
    entry = globals.seed_map.get(seed)
    current = entry.get("token", "") if isinstance(entry, dict) else ""
    if current and _account_is_usable(current):
        return current  # 粘性绑定，号还健康

    tier = entry.get("plan_type") if isinstance(entry, dict) else None
    token = _pick_healthy_account(tier, _seed_plan_types(seed))
    if not token:
        return ""  # 号池耗尽

    assigned_tier = _account_tier(token) or tier or "free"
    if isinstance(entry, dict):
        entry["token"] = token
        entry["plan_type"] = assigned_tier
    else:
        globals.seed_map[seed] = {"token": token, "plan_type": assigned_tier, "conversations": []}
    globals.persist_seed_map()
    return token


def switch_seed_account(seed: str) -> str:
    """强制切换 seed 到同号组的健康账号（供 /api/switch-account 调用）。返回新账号 token 或 ""。"""
    entry = globals.seed_map.get(seed)
    tier = entry.get("plan_type") if isinstance(entry, dict) else None
    token = _pick_healthy_account(tier, _seed_plan_types(seed))
    if not token:
        return ""
    assigned_tier = _account_tier(token) or tier or "free"
    if isinstance(entry, dict):
        entry["token"] = token
        entry["plan_type"] = assigned_tier
    else:
        globals.seed_map[seed] = {"token": token, "plan_type": assigned_tier, "conversations": []}
    globals.persist_seed_map()
    return token


def get_req_token(req_token, seed=None):
    if configs.auto_seed:
        available_token_list = list(set(globals.token_list) - set(globals.error_token_list))
        length = len(available_token_list)
        if seed and length > 0:
            return _resolve_seed_account(seed)

        if req_token in configs.authorization_list:
            if len(available_token_list) > 0:
                if configs.random_token:
                    req_token = random.choice(available_token_list)
                    return req_token
                else:
                    globals.count += 1
                    globals.count %= length
                    return available_token_list[globals.count]
            else:
                return ""
        else:
            return req_token
    else:
        seed = req_token
        if seed not in globals.seed_map.keys():
            raise HTTPException(status_code=401, detail={"error": "Invalid Seed"})
        return globals.seed_map[seed]["token"]


async def verify_token(req_token):
    if not req_token:
        if configs.authorization_list:
            logger.error("Unauthorized with empty token.")
            raise HTTPException(status_code=401)
        else:
            return None
    else:
        if req_token.startswith("eyJhbGciOi") or req_token.startswith("fk-"):
            access_token = req_token
            globals.sync_account_plan(req_token)
            return access_token
        # SessionToken：带 sess- 前缀的 chatgpt.com __Secure-next-auth.session-token
        elif req_token.startswith("sess-"):
            try:
                if req_token in globals.error_token_list:
                    raise HTTPException(status_code=401, detail="Error SessionToken")
                access_token = await sess2ac(req_token, force_refresh=False)
                globals.sync_account_plan(req_token, access_token)
                return access_token
            except HTTPException as e:
                raise HTTPException(status_code=e.status_code, detail=e.detail)
        # 识别 RefreshToken：老版 45 字符 或 新版 Auth0 'rt_' 前缀（长度 ≥ 60）
        elif (req_token.startswith("rt_") and len(req_token) >= 60) or len(req_token) == 45:
            try:
                if req_token in globals.error_token_list:
                    raise HTTPException(status_code=401, detail="Error RefreshToken")

                access_token = await rt2ac(req_token, force_refresh=False)
                globals.sync_account_plan(req_token, access_token)
                return access_token
            except HTTPException as e:
                raise HTTPException(status_code=e.status_code, detail=e.detail)
        else:
            return req_token


async def refresh_all_tokens(force_refresh=False):
    for token in list(set(globals.token_list) - set(globals.error_token_list)):
        try:
            if token.startswith("sess-"):
                await asyncio.sleep(0.5)
                await sess2ac(token, force_refresh=force_refresh)
            elif (token.startswith("rt_") and len(token) >= 60) or len(token) == 45:
                await asyncio.sleep(0.5)
                await rt2ac(token, force_refresh=force_refresh)
        except HTTPException:
            pass
    logger.info("All tokens refreshed.")
