from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse

from app import app
from gateway.frontend_sync import get_frontend_template, get_session_cookie
from gateway.login import login_html
from utils.Logger import logger


@app.get("/", response_class=HTMLResponse)
async def chatgpt_html(request: Request):
    token = request.query_params.get("token")
    if not token:
        token = request.cookies.get("token")
    if not token:
        return await login_html(request)

    # SeedToken（非 45 长度 / 非 eyJ）先 URL encode，保持 gateway 反代时的识别语义
    inject_token = token
    if len(token) != 45 and not token.startswith("eyJhbGciOi"):
        inject_token = quote(token)

    # 官网最新 logged_in 版 HTML（已含正确的 client-bootstrap session 数据）
    html = await get_frontend_template()

    # 清空本地存储，避免不同用户间的前端状态串扰
    clear_script = "<script>localStorage.clear();</script>"
    html = html.replace("</head>", clear_script + "</head>", 1)

    response = HTMLResponse(content=html)
    # 用户标识 cookie（SeedToken / access_token），gateway 反代时据此识别用户
    response.set_cookie("token", value=token, expires="Thu, 01 Jan 2099 00:00:00 GMT")
    # 设置账号持有者的 session cookie，让前端能通过 cookie 认证
    for part in get_session_cookie().split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            if k == "__Secure-next-auth.session-token":
                response.set_cookie(
                    k, value=v, expires="Thu, 01 Jan 2099 00:00:00 GMT",
                    path="/", samesite="lax", secure=True, httponly=True,
                )
            elif k == "cf_clearance":
                response.set_cookie(k, value=v, expires="Thu, 01 Jan 2099 00:00:00 GMT", path="/")
            elif k == "oai-did":
                response.set_cookie(k, value=v, expires="Thu, 01 Jan 2099 00:00:00 GMT", path="/")
    return response
