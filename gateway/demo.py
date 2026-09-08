"""演示 demo（核心链路）：主页选 free/plus 组 -> 填 token -> 进组聊天（Stage 3 补充）。

只做一件事：把访客按 token 路由进 free 号组或 plus 号组，进去后就是官网反代聊天。
两个 token 是演示 seed（自生成的组标识，非 OpenAI 凭据），分别绑定 free / plus 号组。
"""
from fastapi import Request
from fastapi.responses import HTMLResponse

from app import app, templates

# 演示用两个 seed token（非 OpenAI 凭据）：绑定关系见 user_auth.tier_id -> account.plan_type 号组。
DEMO_TOKENS = {
    "free": "demo-free-pool",
    "plus": "demo-plus-pool",
}


@app.get("/demo", response_class=HTMLResponse)
async def demo_page(request: Request):
    return templates.TemplateResponse(
        "demo.html", {"request": request, "tokens": DEMO_TOKENS}
    )
