"""引流落地页 + 支付占位（Stage 3）。

  - ``GET /landing``：产品介绍 + 免费注册入口 + 档位对比表（静态页，复用 templates/）。
  - ``GET /api/tiers``：档位目录 JSON（供落地页/前端渲染定价对比）。
  - ``POST /api/orders``：下单占位 —— 落库 ``pending`` 状态，不接真实支付（微信/支付宝后补）。

审美向 OpenAI / Anthropic 看齐（干净、留白、精致），不做 xyhelper 那种朴素列表。
"""
import secrets as pysecrets

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import app, templates
import utils.store as store
from utils.tiers import list_tiers, normalize_tier_id


@app.get("/landing", response_class=HTMLResponse)
async def landing_page(request: Request):
    return templates.TemplateResponse(
        "landing.html", {"request": request, "tiers": list_tiers()}
    )


@app.get("/api/tiers")
async def tiers_api(request: Request):
    return JSONResponse(list_tiers())


@app.post("/api/orders")
async def create_order(request: Request):
    from gateway.user import _current_email  # 惰性导入，避免与 user 模块的循环依赖
    email = _current_email(request)
    body = await request.json()
    tier_id = normalize_tier_id(body.get("tier_id"))
    amount = body.get("amount") or ""
    order_id = "ord_" + pysecrets.token_hex(12)
    store.create_order(order_id, email, tier_id, amount, status="pending")
    return JSONResponse(
        {"order_id": order_id, "tier_id": tier_id, "amount": amount, "status": "pending"}
    )
