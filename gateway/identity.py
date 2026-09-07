"""账号身份合成：从 access_token JWT 解码身份字段，合成 NextAuth session。

多账号池会话隔离的关键——每个 SeedToken 映射到池中某个账号，前端应看到该账号的
身份（name/email/account_id/plan_type），而不是账号持有者（owner）的身份。

用途：
  1. 重写 served HTML 的 client-bootstrap session（避免 owner 身份泄漏 + 修复 React #418 水合不一致）
  2. 拦截 /api/auth/session，返回合成 session，移除对 owner session cookie 的依赖
"""
import base64
import json
import time


def decode_jwt_payload(token: str) -> dict:
    """解码 JWT payload（不校验签名，仅取身份字段）。任何失败返回空 dict。"""
    if not token or "." not in token:
        return {}
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
    except Exception:
        return {}


def decode_account_identity(access_token: str) -> dict:
    """解码账号的等级身份（plan_type / real_email / nickname），供号池归池用。

    AccessToken 在启动时立即解码；Refresh/Session token 在首次换出 access_token 后
    懒解码写回。返回空 dict 表示解码失败（token 非 JWT 或 payload 不可解析）。
    """
    claims = decode_jwt_payload(access_token)
    if not claims:
        return {}
    auth = claims.get("https://api.openai.com/auth", {})
    profile = claims.get("https://api.openai.com/profile", {})
    return {
        "plan_type": auth.get("chatgpt_plan_type") or "unknown",
        "real_email": profile.get("email") or "",
        "nickname": profile.get("name") or "",
    }


def _iso_expiry(exp: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(exp))


def build_session(access_token: str) -> dict:
    """从账号 access_token 合成 NextAuth session 对象。token 为空/无法解码时返回 {}。"""
    if not access_token:
        return {}
    claims = decode_jwt_payload(access_token)
    if not claims:
        return {}

    auth = claims.get("https://api.openai.com/auth", {})
    profile = claims.get("https://api.openai.com/profile", {})
    mfa = claims.get("https://api.openai.com/mfa", {})

    user_id = auth.get("chatgpt_user_id") or auth.get("user_id") or ""
    account_id = auth.get("chatgpt_account_id") or ""
    plan_type = auth.get("chatgpt_plan_type") or "free"
    residency = auth.get("chatgpt_compute_residency") or "no_constraint"
    name = profile.get("name") or ""
    email = profile.get("email") or ""
    amr = auth.get("amr") or []

    idp = "auth0"
    sub = claims.get("sub", "")
    if "|" in sub:
        idp = sub.split("|", 1)[0]

    iat = int(claims.get("iat") or 0)
    exp = int(claims.get("exp") or 0)
    mfa_enabled = mfa.get("required") == "yes"

    return {
        "user": {
            "id": user_id,
            "name": name,
            "email": email,
            "idp": idp,
            "iat": iat,
            "amr": amr,
            "acr": ("http://schemas.openid.net/pape/policies/2007/06/multi-factor"
                    if mfa_enabled else "urn:openai:names:acr:default"),
            "mfa": mfa_enabled,
        },
        "expires": _iso_expiry(exp) if exp else "",
        "account": {
            "id": account_id,
            "createdTime": 0,
            "planType": plan_type,
            "structure": "personal",
            "isUsageBasedSeatEnabled": False,
            "isConversationClassifierEnabledForWorkspace": True,
            "hasFloraFeature": False,
            "isFedrampCompliantWorkspace": False,
            "isDelinquent": False,
            "residencyRegion": residency,
            "computeResidency": residency,
        },
        "accessToken": access_token,
        "authProvider": "openai",
        "sessionToken": "",
    }
