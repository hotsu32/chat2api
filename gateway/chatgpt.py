import json
import re
import uuid

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app import app
from chatgpt.authorization import verify_token
from gateway.frontend_sync import (get_frontend_template, get_cached_frontend,
                                   compose_frontend, FrontendSessionError)
from chatgpt.fp import get_fp
from gateway.identity import build_session
from gateway.login import login_html
from gateway.reverseProxy import get_real_req_token, resolve_seed_token
from gateway.research_panel import PANEL_TAGS
from utils.Logger import logger


# 官网 HTML 里的 client-bootstrap：<script type="application/json" id="client-bootstrap" nonce="...">JSON</script>
_CLIENT_BOOTSTRAP_RE = re.compile(
    r'(<script\b[^>]*\bid="client-bootstrap"[^>]*>)(.*?)(</script>)',
    re.DOTALL,
)


def _stable_device_id(user_id: str, salt: str) -> str:
    """按种子用户派生稳定 device id，替换 statsig 里 owner 的设备指纹。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"chat2api:{salt}:{user_id}"))


def _sanitize_statsig_payload(payload: str, session: dict) -> str:
    """statsigPayload 内嵌 owner 的 user 对象（userID/email/account_id/设备 ID），
    替换为种子账号身份；gate/config 评估值保持不变（服务端已预评估，客户端不重评）。
    解析失败原样返回——其中只有弱分析属性，无凭据。"""
    try:
        data = json.loads(payload)
    except Exception:
        return payload
    user = data.get("user")
    if not isinstance(user, dict):
        return payload

    su = session.get("user") or {}
    account = session.get("account") or {}
    user_id = su.get("id") or ""
    account_id = account.get("id") or ""
    plan_type = account.get("planType") or "free"

    user["userID"] = user_id
    user["email"] = su.get("email") or ""
    custom_ids = user.get("customIDs")
    if isinstance(custom_ids, dict):
        for k in ("account_id", "workspace_id"):
            if k in custom_ids:
                custom_ids[k] = account_id
        for k in ("stableID", "WebAnonymousCookieID", "DeviceId"):
            if k in custom_ids:
                custom_ids[k] = _stable_device_id(user_id, k)
    custom = user.get("custom")
    if isinstance(custom, dict):
        if "account_user_id" in custom:
            custom["account_user_id"] = user_id
        if "account_id" in custom:
            custom["account_id"] = account_id
        if "plan_type" in custom:
            custom["plan_type"] = plan_type
        if "is_paid" in custom:
            custom["is_paid"] = plan_type not in ("free", "")
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _rewrite_client_bootstrap(html: str, session: dict) -> str:
    """把 client-bootstrap 的身份字段替换为种子账号身份，保留其余全部启动配置。

    client-bootstrap 不是一小段身份凭据，而是前端应用的完整启动状态（statsigPayload
    特性开关约 477KB、sessionId、entryContext、cluster、locale 等 40+ 键，共约 510KB，
    占整页 90%）。整体替换会让前端启动时 JSON.parse(undefined) 崩溃、页面只剩裸壳。
    因此只外科手术式覆盖身份/凭据字段：session 换成种子账号合成 session、user 同步覆盖、
    statsigPayload 内嵌的 owner userID/email/account_id/设备 ID 脱敏后保留。
    解析失败时回退整体替换——宁可页面降级，也不能把 owner 凭据原样下发。
    """
    _bs = chr(92)  # 反斜杠，用于拼出脚本内 unicode 转义序列
    def _repl(m):
        try:
            data = json.loads(m.group(2))
            data["authStatus"] = "logged_in"
            source_session = data.get("session") or {}
            # The fetcher has verified this source account; preserve actual plan,
            # workspace and subscription fields instead of synthetic defaults.
            from gateway.identity import sanitize_session
            data["session"] = sanitize_session(source_session) if source_session.get('account') else session
            data["user"] = data["session"].get("user") or {}
            sp = data.get("statsigPayload")
            if isinstance(sp, str) and sp:
                data["statsigPayload"] = _sanitize_statsig_payload(sp, data["session"])
            bootstrap = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

        except Exception as e:
            logger.warning(f"[chatgpt_html] bootstrap parse failed; fallback full replace: {e}")
            bootstrap = json.dumps({"authStatus": "logged_in", "session": session}, ensure_ascii=False)
        # 脚本内转义 < > &，避免破坏 <script> 边界 / HTML 解析
        bootstrap = bootstrap.replace("&", _bs + "u0026").replace("<", _bs + "u003c").replace(">", _bs + "u003e")
        return m.group(1) + bootstrap + m.group(3)

    html, n = _CLIENT_BOOTSTRAP_RE.subn(_repl, html)
    if n == 0:
        raise FrontendSessionError('Official frontend bootstrap cannot be sanitized')
    return html


@app.get("/", response_class=HTMLResponse)
async def chatgpt_html(request: Request):
    # 主页只认显式 ?token=（来自 Dashboard「进入 ChatGPT」入口）。
    # 裸访问 / 一律回 Dashboard —— 主页是 Dashboard，不是聊天页；
    # 未登录由 /dashboard 自行 303 到 /signin。不再回退读 cookie token，
    # 否则残留的 token cookie 会把访问主页的用户直接送进聊天。
    token = request.query_params.get("token")
    if not token:
        return RedirectResponse(url="/dashboard", status_code=302)
    if token.startswith("frontend-proof-"):
        from gateway.landing import require_dev_access
        require_dev_access()
    if not _entitled(token):
        # 套餐过期 / 未购买：入口直接拦掉，不渲染聊天页。
        # seed 是永久凭据，收藏了带 token 的链接也绕不过去。
        return RedirectResponse(url="/store?expired=1", status_code=302)
    if token.startswith("frontend-proof-"):
        # Trial aliases are explicit tier handles; repair stale sticky bindings
        # before resolving the account so direct links behave like /try buttons.
        from gateway.landing import ensure_core_trial_bindings
        ensure_core_trial_bindings([
            ("Free", "frontend-proof-free-1", "free"),
            ("Plus 一", "frontend-proof-plus-2", "plus"),
            ("Plus 二", "frontend-proof-plus-3", "plus"),
            ("Pro 一", "frontend-proof-pro-1", "pro"),
        ])
    return await _render_account_page(request, token)


def _entitled(seed: str) -> bool:
    """该 seed 当前是否有权进聊天。

    非 SaaS seed（运营者 / 直传 token）返回 True —— 车队运营侧不受 SaaS 权益约束。

    这里只决定「要不要渲染这张 HTML 页」，真正的闸在 ``enforce_tier``：数据层故障时
    页面照渲染（不把运营者挡在门外），但任何会话请求都会被 enforce_tier 拦成 503，
    所以放行一张静态页并不构成白拿。
    """
    try:
        from utils import entitlements
        tier = entitlements.effective_tier(seed)
        return tier is None or bool(tier)
    except Exception as e:
        logger.warning(f"[chatgpt_html] entitlement check failed, rendering page anyway: {e}")
        return True


@app.get('/c/{conversation_id}', response_class=HTMLResponse)
async def conversation_page(request: Request, conversation_id: str):
    """Reload an owned conversation without changing the bare-root contract."""
    import utils.globals as globals
    # 走统一的身份解析入口，而不是直接读 cookie：会话归属键必须是**当前有效**的
    # 身份，否则一次开发运行留下的公开别名（frontend-proof-*）在闸门关闭后仍然是
    # 一把能问出「这个会话存在吗」的钥匙。resolve_seed_token 读的仍是 token cookie
    # （仅在缺失时回退 Authorization 头），并对被禁用的别名 fail-closed。
    token = resolve_seed_token(request)
    entry = globals.seed_map.get(token) or {}
    if conversation_id not in entry.get('conversations', []):
        raise HTTPException(status_code=404, detail='Conversation not found')
    if not _entitled(token):
        return RedirectResponse(url="/store?expired=1", status_code=302)
    return await _render_account_page(request, token)


def _website_unavailable():
    return HTMLResponse(
        '<h1>此账号的官网会话暂不可用</h1><p>请运营者检查该账号的会话配置后重试。</p>',
        status_code=503, headers={'Cache-Control': 'no-store'})


async def _render_account_page(request: Request, token: str):

    # 会话隔离：解析 SeedToken -> 账号 access_token，合成该账号的 session 身份
    try:
        req_token = await get_real_req_token(token)
        access_token = await verify_token(req_token) or ""
    except Exception as e:
        if isinstance(e, HTTPException) and e.status_code == 503:
            return _website_unavailable()
        logger.warning(
            f"[chatgpt_html] resolve seed account failed status="
            f"{getattr(e, 'status_code', type(e).__name__)}"
        )
        access_token = ""
    session = build_session(access_token)
    if not session:
        # 种子账号无法解析（无绑定账号 / token 失效）时不下发官网 live 模板，
        # 否则会把模板里账号持有者（owner）的 client-bootstrap 身份原样泄漏给镜像用户。
        return await login_html(request)

    # 官网最新 logged_in 版 HTML（client-bootstrap 里是 owner 身份，重写为种子账号身份）
    try:
        fetched_html = await get_frontend_template(req_token, access_token, get_fp(req_token).copy())
        context = get_cached_frontend(req_token, access_token)
        html = compose_frontend(context) if context else fetched_html
        if not html:
            raise FrontendSessionError('Website context needs revalidation')
        html = _rewrite_client_bootstrap(html, session)
    except FrontendSessionError:
        return _website_unavailable()

    # 清空本地存储，避免不同用户间的前端状态串扰
    clear_script = "<script>localStorage.clear();</script>"
    html = html.replace("</head>", clear_script + PANEL_TAGS + "</head>", 1)

    response = HTMLResponse(content=html, headers={'Cache-Control': 'no-store'})
    # 用户标识 cookie（SeedToken / access_token），gateway 反代时据此识别用户
    response.set_cookie("token", value=token, expires="Thu, 01 Jan 2099 00:00:00 GMT")
    # Integrity state belongs to this account's page response.  Auth-session
    # responses deliberately do not set it, preventing a delayed old-account
    # refresh from overwriting a newly selected account's browser state.
    context = get_cached_frontend(req_token, access_token)
    integrity_state = (context or {}).get('cookies', {}).get('__Secure-oai-is')
    if integrity_state:
        response.set_cookie('__Secure-oai-is', integrity_state, secure=True,
                            httponly=False, samesite='lax')
    else:
        response.delete_cookie('__Secure-oai-is', secure=True, httponly=False, samesite='lax')
    # 不再把 owner 的 session cookie 下发到用户浏览器（凭据泄漏）；
    # 上游认证由 gateway 服务端注入（reverseProxy 里 get_session_cookie）。
    return response
