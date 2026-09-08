"""用户侧注册/登录/账号（Stage 1：把「运营者发 seed」改为「用户自注册 → 拿 seed → 绑档位」）。

复用 orchestrator 的 session + CSRF 模式，但作用域是「真实用户」而非「运营者」：

  - 密码用 PBKDF2 加盐哈希落库（不存明文）。
  - 注册即生成 seed，落到 ``globals.seed_map`` + ``store.user_auth``；免费档默认（``default_tier_id``）。
  - 登录后跳 ``/?token=<seed>`` 直接进聊天，全程无运营者介入。
  - 邮箱验证若缺 SMTP 资源 → 占位（``require_email_verification`` 默认 False，跳过验证）。

路由命名与既有 ``/login``（seed 表单）错开，避免冲突：``/register``、``/signin``、
``/signout``、``/account``。
"""
import hmac
import hashlib
import base64
import json
import secrets as pysecrets
import time

from fastapi import HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app import app, templates
import utils.configs as configs
import utils.globals as globals
import utils.store as store
from utils.Logger import logger
from utils.tiers import default_tier_id, get_tier, normalize_tier_id, user_usage_total

_PBKDF2_ITERATIONS = 100_000

# 未配置 USER_SESSION_SECRET 时用进程内随机密钥（重启后会话失效，dev/test 可接受）。
_session_secret = configs.user_session_secret or pysecrets.token_hex(32)
if not configs.user_session_secret:
    logger.warning("[user] USER_SESSION_SECRET 未配置，使用进程内随机密钥（重启会话失效）")


# --------------------------------------------------------------------------- crypto

def hash_password(password: str) -> str:
    salt = pysecrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _sign(data: str) -> str:
    return hmac.new(_session_secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()


def _issue_session(email: str) -> str:
    """自签名会话 token（stdlib HMAC，无 itsdangerous 依赖）。"""
    payload = {"email": email, "exp": int(time.time()) + configs.user_session_max_age}
    data = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{data}.{_sign(data)}"


def _verify_session(token: str):
    """校验会话 token，返回 email 或 None。"""
    try:
        data, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(_sign(data), sig):
            return None
        pad = "=" * (-len(data) % 4)
        payload = json.loads(base64.urlsafe_b64decode(data + pad).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload.get("email")
    except Exception:
        return None


def _valid_email(email: str) -> bool:
    return bool(email) and "@" in email and "." in email.split("@", 1)[-1]


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


# ------------------------------------------------------------------------ session

def _set_session_cookies(request: Request, response: Response, email: str) -> None:
    token = _issue_session(email)
    csrf = pysecrets.token_hex(16)
    secure = _is_https(request)
    response.set_cookie(
        configs.user_session_cookie, token,
        max_age=configs.user_session_max_age, httponly=True, samesite="strict",
        secure=secure, path="/",
    )
    response.set_cookie(
        configs.user_csrf_cookie, csrf,
        max_age=configs.user_session_max_age, httponly=False, samesite="strict",
        secure=secure, path="/",
    )


def _current_email(request: Request) -> str:
    """校验会话，返回已登录用户 email；无效抛 401。"""
    token = request.cookies.get(configs.user_session_cookie) or ""
    email = _verify_session(token) if token else None
    if email and store.get_user_auth(email):
        return email
    raise HTTPException(status_code=401, detail="未登录")


def _ensure_csrf(request: Request, response: Response) -> str:
    """读/下发 CSRF cookie，返回当前 token（表单 hidden 字段用）。"""
    token = request.cookies.get(configs.user_csrf_cookie) or pysecrets.token_hex(16)
    response.set_cookie(
        configs.user_csrf_cookie, token,
        max_age=configs.user_session_max_age, httponly=False, samesite="strict",
        secure=_is_https(request), path="/",
    )
    return token


def _render_with_csrf(request: Request, template: str, ctx: dict, status_code: int = 200):
    """渲染模板并在响应上确保 CSRF cookie，表单内嵌同一 token。"""
    token = _ensure_csrf(request, Response())
    resp = templates.TemplateResponse(template, {**ctx, "request": request, "csrf_token": token})
    resp.set_cookie(
        configs.user_csrf_cookie, token,
        max_age=configs.user_session_max_age, httponly=False, samesite="strict",
        secure=_is_https(request), path="/",
    )
    resp.status_code = status_code
    return resp


def _verify_csrf(request: Request, form_token: str) -> None:
    cookie_val = request.cookies.get(configs.user_csrf_cookie) or ""
    if not cookie_val or not form_token or not hmac.compare_digest(cookie_val, form_token):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


def _login_redirect(request: Request, email: str) -> RedirectResponse:
    row = store.get_user_auth(email) or {}
    seed = row.get("seed") or ""
    resp = RedirectResponse(url=f"/?token={seed}", status_code=303)
    _set_session_cookies(request, resp, email)
    return resp


# --------------------------------------------------------------------------- routes

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return _render_with_csrf(request, "register.html", {"error": None})


@app.post("/register")
async def register(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    csrf = form.get("csrf_token") or ""
    _verify_csrf(request, csrf)

    error = None
    if not _valid_email(email):
        error = "请输入有效邮箱"
    elif len(password) < 8:
        error = "密码至少 8 位"
    elif store.get_user_auth(email):
        error = "该邮箱已注册"
    if error:
        return _render_with_csrf(request, "register.html", {"error": error}, status_code=400)

    seed = pysecrets.token_hex(16)
    status = "active" if not configs.require_email_verification else "unverified"
    store.upsert_user_auth(
        email, password_hash=hash_password(password), seed=seed,
        tier_id=default_tier_id(), status=status,
    )
    # 落到 seed_map + users，首请求由 _resolve_seed_account 分配号
    globals.seed_map[seed] = {"token": "", "plan_type": None, "conversations": []}
    globals.persist_seed_map()

    return _login_redirect(request, email)


@app.get("/signin", response_class=HTMLResponse)
async def signin_page(request: Request):
    return _render_with_csrf(request, "signin.html", {"error": None})


@app.post("/signin")
async def signin(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    csrf = form.get("csrf_token") or ""
    _verify_csrf(request, csrf)

    row = store.get_user_auth(email)
    if not row or not verify_password(password, row.get("password_hash") or ""):
        return _render_with_csrf(
            request, "signin.html",
            {"error": "邮箱或密码错误", "email": email}, status_code=401,
        )
    return _login_redirect(request, email)


@app.post("/signout")
async def signout(request: Request):
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(configs.user_session_cookie, path="/")
    resp.delete_cookie(configs.user_csrf_cookie, path="/")
    return resp


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    email = _current_email(request)
    row = store.get_user_auth(email) or {}
    seed = row.get("seed") or ""
    tier_id = normalize_tier_id(row.get("tier_id"))
    tier = get_tier(tier_id) or {}
    used = user_usage_total(seed, tier_id)
    limit = tier.get("quota_limit")
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "email": email,
            "seed": seed,
            "tier_id": tier_id,
            "tier_label": tier.get("label") or tier_id,
            "models": tier.get("models") or [],
            "used": used,
            "quota_limit": limit,
            "status": row.get("status") or "active",
        },
    )
