import json
import re

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


def _rewrite_client_bootstrap(html: str, session: dict) -> str:
    """把 client-bootstrap 的 session 重写为种子账号身份，替换掉 owner 的身份/凭据。"""
    bootstrap = json.dumps(
        {"authStatus": "logged_in", "session": session},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # 脚本内转义 < > &，避免破坏 <script> 边界 / HTML 解析
    bootstrap = bootstrap.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")

    def _repl(m):
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
