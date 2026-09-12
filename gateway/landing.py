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
import utils.payment as payment
import utils.plans as plans
import utils.store as store
from utils.tiers import list_tiers
import utils.globals as globals
from chatgpt.authorization import _resolve_seed_account


@app.get("/landing", response_class=HTMLResponse)
async def landing_page(request: Request):
    # 落地页只摆月卡做价格锚点（12 种全摆会把首屏冲垮），完整组合在 /store 里选。
    featured = [p for p in plans.all_plans() if p["duration"] == "1m"]
    return templates.TemplateResponse(
        "landing.html", {"request": request, "plans": featured, "tiers": list_tiers()}
    )


@app.get("/try", response_class=HTMLResponse)
async def core_try_page(request: Request):
    """Local core-mirror selector; deliberately bypasses SaaS registration flows."""
    entries = [
        ("Free", "frontend-proof-free-1", "free"),
        ("Plus 一", "frontend-proof-plus-2", "plus"),
        ("Plus 二", "frontend-proof-plus-3", "plus"),
        ("Pro 一", "frontend-proof-pro-1", "pro"),
    ]
    # Seed the Pro selector with a real healthy Pro account on first use. The
    # alias is only a local routing handle and never exposes its credential.
    entry = globals.seed_map.get("frontend-proof-pro-1")
    if not isinstance(entry, dict) or not entry.get("token"):
        globals.seed_map["frontend-proof-pro-1"] = {"token": "", "plan_type": "pro", "conversations": []}
        _resolve_seed_account("frontend-proof-pro-1")
    return templates.TemplateResponse("core_try.html", {"request": request, "entries": entries})


@app.get("/api/tiers")
async def tiers_api(request: Request):
    return JSONResponse(list_tiers())


@app.post("/api/orders")
async def create_order(request: Request):
    """下单占位（旧落地页入口）。金额一律服务端定价，不接受客户端传值。

    真实下单走 ``POST /api/checkout``（见 ``gateway/saas.py``）；本入口保留兼容，
    同样只建 ``pending`` 单，置 ``paid`` 必须经支付 provider 激活路径。

    未配置支付渠道时同样拒绝建单 —— 与 ``/api/checkout`` 口径一致，
    否则这里会留下一堆永远付不了款的孤儿单。
    """
    from gateway.user import _current_email  # 惰性导入，避免与 user 模块的循环依赖
    email = _current_email(request)
    if not payment.get_provider():
        raise HTTPException(status_code=503, detail="支付渠道暂未开通，请联系客服")
    body = await request.json()
    plan_id = (body.get("tier_id") or "").strip()
    detail = plans.plan_detail(plan_id)
    if not detail:
        raise HTTPException(status_code=400, detail="无效套餐")
    order_id = "ord_" + pysecrets.token_hex(12)
    store.create_order(order_id, email, detail["id"], str(detail["price"]), status="pending")
    return JSONResponse(
        {"order_id": order_id, "tier_id": detail["id"], "amount": detail["price"], "status": "pending"}
    )
