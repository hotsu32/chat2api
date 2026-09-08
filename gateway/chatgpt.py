import json
import re
import uuid

from fastapi import Request
from fastapi.responses import HTMLResponse

from app import app
from chatgpt.authorization import verify_token
from gateway.frontend_sync import get_frontend_template
from gateway.identity import build_session
from gateway.login import login_html
from gateway.reverseProxy import get_real_req_token
from utils.Logger import logger


# 官网 HTML 里的 client-bootstrap：<script type="application/json" id="client-bootstrap" nonce="...">JSON</script>
_CLIENT_BOOTSTRAP_RE = re.compile(
    r'(<script\s+type="application/json"\s+id="client-bootstrap"[^>]*>)(.*?)(</script>)',
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
            data["session"] = session
            data["user"] = session.get("user") or {}
            sp = data.get("statsigPayload")
            if isinstance(sp, str) and sp:
                data["statsigPayload"] = _sanitize_statsig_payload(sp, session)
            bootstrap = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

        except Exception as e:
            logger.warning(f"[chatgpt_html] bootstrap parse failed; fallback full replace: {e}")
            bootstrap = json.dumps({"authStatus": "logged_in", "session": session}, ensure_ascii=False)
        # 脚本内转义 < > &，避免破坏 <script> 边界 / HTML 解析
        bootstrap = bootstrap.replace("&", _bs + "u0026").replace("<", _bs + "u003c").replace(">", _bs + "u003e")
        return m.group(1) + bootstrap + m.group(3)

    html, n = _CLIENT_BOOTSTRAP_RE.subn(_repl, html, count=1)
    if n == 0:
        logger.warning("[chatgpt_html] client-bootstrap tag not found; identity not rewritten")
    return html


@app.get("/", response_class=HTMLResponse)
async def chatgpt_html(request: Request):
    token = request.query_params.get("token")
    if not token:
        token = request.cookies.get("token")
    if not token:
        return await login_html(request)

    # 会话隔离：解析 SeedToken -> 账号 access_token，合成该账号的 session 身份
    try:
        req_token = await get_real_req_token(token)
        access_token = await verify_token(req_token) or ""
    except Exception as e:
        logger.warning(f"[chatgpt_html] resolve seed account failed: {e}")
        access_token = ""
    session = build_session(access_token)
    if not session:
        # 种子账号无法解析（无绑定账号 / token 失效）时不下发官网 live 模板，
        # 否则会把模板里账号持有者（owner）的 client-bootstrap 身份原样泄漏给镜像用户。
        return await login_html(request)

    # 官网最新 logged_in 版 HTML（client-bootstrap 里是 owner 身份，重写为种子账号身份）
    html = await get_frontend_template()
    html = _rewrite_client_bootstrap(html, session)

    # 清空本地存储，避免不同用户间的前端状态串扰
    clear_script = "<script>localStorage.clear();</script>"
    html = html.replace("</head>", clear_script + "</head>", 1)

    response = HTMLResponse(content=html)
    # 用户标识 cookie（SeedToken / access_token），gateway 反代时据此识别用户
    response.set_cookie("token", value=token, expires="Thu, 01 Jan 2099 00:00:00 GMT")
    # 不再把 owner 的 session cookie 下发到用户浏览器（凭据泄漏）；
    # 上游认证由 gateway 服务端注入（reverseProxy 里 get_session_cookie）。
    return response
