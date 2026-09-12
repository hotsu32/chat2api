"""引流落地页 + 支付占位（Stage 3）。

  - ``GET /landing``：产品介绍 + 免费注册入口 + 档位对比表（静态页，复用 templates/）。
  - ``GET /api/tiers``：档位目录 JSON（供落地页/前端渲染定价对比）。
  - ``POST /api/orders``：下单占位 —— 落库 ``pending`` 状态，不接真实支付（微信/支付宝后补）。

审美向 OpenAI / Anthropic 看齐（干净、留白、精致），不做 xyhelper 那种朴素列表。

## 开发 / 运营入口闸（``DEV_ACCESS_ENABLED``）

``/try`` 是一张**绕过注册与订阅**的演示入口：它直接给出绑到真实号池账号的 seed 别名，
点一下就进核心聊天页。这在本地做前端验收时很方便，但对外可达就是一个「任何人都能
用平台号池」的后门，而且没有任何权益检查挡在前面。

因此本模块里所有绕过 SaaS 流程的入口（``/try``、公开档位目录里的非售卖档）统一由
``DEV_ACCESS_ENABLED`` 控制，默认关闭：未显式打开时按 404 处理（不是 403 ——
403 等于告诉扫描器「这里有东西，只是不给你」，404 不提供这个信息）。

关闭闸门**不影响**正常售卖路径：``/landing``、``/api/orders``、``/store`` 等照常。
"""
import secrets as pysecrets

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import app, templates
import utils.configs as configs
import utils.payment as payment
import utils.plans as plans
import utils.store as store
from utils.tiers import list_tiers
import utils.globals as globals
from chatgpt.authorization import _resolve_seed_account

# 正式售卖档（``utils/tiers`` 的账号档目录里，只有这两档是可售卖商品）。
# Free 是账号侧的采集档，不是卖品 —— 公开目录里不出现它。
_SELLABLE_TIERS = ("plus", "pro")


def dev_access_enabled() -> bool:
    """开发 / 运营入口是否显式打开（默认关闭）。"""
    return bool(getattr(configs, "dev_access_enabled", False))


def require_dev_access() -> None:
    """闸门：未显式打开时把这些入口当作不存在。"""
    if not dev_access_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


@app.get("/landing", response_class=HTMLResponse)
async def landing_page(request: Request):
    # 落地页只摆月卡做价格锚点（12 种全摆会把首屏冲垮），完整组合在 /store 里选。
    featured = [p for p in plans.all_plans() if p["duration"] == "1m"]
    return templates.TemplateResponse(
        "landing.html", {"request": request, "plans": featured, "tiers": list_tiers()}
    )


@app.get("/try", response_class=HTMLResponse)
async def core_try_page(request: Request):
    """Local core-mirror selector; deliberately bypasses SaaS registration flows.

    仅在 ``DEV_ACCESS_ENABLED`` 打开时可达（见模块文档）：它直接把种子别名指向真实
    号池账号，对外暴露等于绕开全部订阅闸门。
    """
    require_dev_access()
    entries = [
        ("Free", "frontend-proof-free-1", "free"),
        ("Plus 一", "frontend-proof-plus-2", "plus"),
        ("Plus 二", "frontend-proof-plus-3", "plus"),
        ("Pro 一", "frontend-proof-pro-1", "pro"),
    ]
    # Trial aliases are tier-scoped handles.  Older runs may have sticky-bound
    # a Plus alias to a Free account; verify the real account row before reuse.
    # If it is missing or mismatched, clear only that alias and pick a healthy
    # account from its declared tier.  The browser still receives only a seed.
    ensure_core_trial_bindings(entries)
    return templates.TemplateResponse("core_try.html", {"request": request, "entries": entries})


def ensure_core_trial_bindings(entries):
    """修复 /try 演示别名的绑定（**仅开发闸门打开时**）。

    别名是开发专用句柄：把一个 seed 稳定指向某个真实号池账号，好让前端验收与
    mirror 验收脚本可重复。闸门关闭时本函数直接返回，不创建、不改写任何绑定 ——
    生产环境不该因为一次 `/?token=frontend-proof-*` 访问就凭空产生一条新的
    「任意人都能用的真实账号入口」。

    不清除既有绑定：那是别人的验收数据，且会破坏 mirror 验收脚本对别名的检查。
    真正的堵口在聊天页入口（`gateway/chatgpt.py` 的 `frontend-proof-` 前缀解析），
    属另一条线，见报告中的残余风险。
    """
    if not dev_access_enabled():
        return None
    _taken = set()
    for _label, _seed, _tier in entries:
        entry = globals.seed_map.get(_seed)
        current = store.get_account(entry.get("token", "")) if isinstance(entry, dict) else None
        if current and current.get("plan_type") == _tier and entry.get("token") not in _taken:
            _taken.add(entry["token"])
            continue
        candidates = [a for a in store.get_account_by_plan(_tier, status="healthy")
                      if a.get("token") and a["token"] not in _taken]
        if not candidates:
            # Do not silently downgrade a requested tier to another account class.
            globals.seed_map[_seed] = {"token": "", "plan_type": _tier, "conversations": []}
            globals.persist_seed_map()
            continue
        chosen = candidates[0]["token"]
        globals.seed_map[_seed] = {"token": chosen, "plan_type": _tier,
                                   "conversations": (entry or {}).get("conversations", [])}
        _taken.add(chosen)
        globals.persist_seed_map()
    return None


@app.get("/api/tiers")
async def tiers_api(request: Request):
    """公开档位目录：只返回**可售卖**的账号档（plus / pro）。

    free 是账号侧的采集档，不是商品；把它挂在一个定价对比接口上会让用户以为
    「有免费档可以买」。开发闸门打开时（本地联调 / 前端验收）返回完整目录，
    其余情况过滤掉非售卖档。

    目录字段本身不做删改 —— 前端仍能按档拿到号组范围、模型白名单与额度上限。
    """
    catalog = list_tiers()
    if dev_access_enabled():
        return JSONResponse(catalog)
    return JSONResponse({k: v for k, v in catalog.items() if k in _SELLABLE_TIERS})


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
