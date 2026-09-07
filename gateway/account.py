"""账号状态 + 自主切换端点（fleet / 车队管理）。

提供给前端用户侧的接口：
  GET  /api/account-status  -> 当前账号健康态 + 匿名身份（tier / 脱敏账号）
  POST /api/switch-account  -> 当前账号不健康时，切换到同等级的健康账号

切换会失效旧账号的服务端 sentinel 缓存（f_conversation_gateway 与 backend 的
openai_sentinel 缓存），避免旧账号的 PoW/turnstile 凭据被新账号复用。
"""
import json

from fastapi import Request, HTTPException

from app import app
from chatgpt.authorization import verify_token, switch_seed_account
from gateway.reverseProxy import resolve_seed_token
from gateway.identity import build_session
import utils.globals as globals
import utils.store as store
from utils.Logger import logger


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return token[:2] + "***"
    return token[:6] + "..." + token[-4:]


def _account_health(token: str) -> str:
    """账号健康态（healthy / unhealthy / disabled / unknown）。"""
    if not token:
        return "unknown"
    acct = store.get_account(token) or {}
    if acct.get("status") == "disabled":
        return "disabled"
    if acct.get("status") == "unhealthy":
        return "unhealthy"
    try:
        from utils.antiban import circuit as antiban_circuit
        if antiban_circuit.is_token_dead(token):
            return "unhealthy"
    except Exception:
        pass
    if token in globals.error_token_list:
        return "unhealthy"
    if acct.get("status") == "healthy" or token in globals.token_list:
        return "healthy"
    return "unknown"


def _invalidate_account_caches(old_token: str) -> None:
    """失效旧账号 req_token 的内存 sentinel 缓存。"""
    if not old_token:
        return
    try:
        from gateway.f_conversation_gateway import _sentinel_cache, _sentinel_cookie_cache
        _sentinel_cache.pop(old_token, None)
        _sentinel_cookie_cache.pop(old_token, None)
    except Exception as e:
        logger.debug(f"[account] f_conversation sentinel cache invalidate skip: {e}")
    try:
        from gateway import backend as _backend
        getattr(_backend, "openai_sentinel_tokens_cache", {}).pop(old_token, None)
        getattr(_backend, "openai_sentinel_cookies_cache", {}).pop(old_token, None)
    except Exception as e:
        logger.debug(f"[account] backend sentinel cache invalidate skip: {e}")


@app.get("/api/account-status")
async def account_status(request: Request):
    seed = resolve_seed_token(request)
    if not seed:
        raise HTTPException(status_code=401, detail="No seed token")
    entry = globals.seed_map.get(seed)
    current = entry.get("token", "") if isinstance(entry, dict) else ""
    tier = entry.get("plan_type") if isinstance(entry, dict) else None

    try:
        access_token = await verify_token(current) or ""
    except Exception:
        access_token = ""
    session = build_session(access_token)  # 匿名化身份

    return {
        "tier": tier or (session.get("account") or {}).get("planType") or "free",
        "account": _mask_token(current),
        "status": _account_health(current),
        "identity": {
            "name": (session.get("user") or {}).get("name"),
            "plan_type": (session.get("account") or {}).get("planType"),
        },
    }


@app.post("/api/switch-account")
async def switch_account(request: Request):
    seed = resolve_seed_token(request)
    if not seed:
        raise HTTPException(status_code=401, detail="No seed token")
    entry = globals.seed_map.get(seed)
    current = entry.get("token", "") if isinstance(entry, dict) else ""

    if current and _account_health(current) == "healthy":
        raise HTTPException(status_code=400, detail="Current account is healthy; no switch needed")

    old_token = current
    new_token = switch_seed_account(seed)
    if not new_token:
        raise HTTPException(status_code=503, detail="No healthy account available in this tier")

    _invalidate_account_caches(old_token)

    try:
        access_token = await verify_token(new_token) or ""
    except Exception:
        access_token = ""
    session = build_session(access_token)

    return {
        "switched": True,
        "account": _mask_token(new_token),
        "tier": (session.get("account") or {}).get("planType") or "free",
        "identity": {
            "name": (session.get("user") or {}).get("name"),
            "plan_type": (session.get("account") or {}).get("planType"),
        },
    }
