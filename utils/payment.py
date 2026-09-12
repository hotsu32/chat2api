"""支付网关（可插拔）。

真实商户号（微信 / 支付宝）到位前，本模块只提供 ``mock`` 实现，并且**默认关闭**：
``PAYMENT_PROVIDER`` 未配置或填了未知值时一律拒绝下单（fail-closed）。
宁可「暂时不能下单」这种诚实的不可用，也不要一个静默放行的白拿后门。

订单生命周期由本模块定义，页面侧只能创建 ``pending`` 单，
置 ``paid`` 必须经 ``activate``，并且激活是幂等的（支付回调会重复投递）。

接真实网关时新增一个 provider 类实现同样三个方法即可，页面侧无需改动：

  - ``name``            provider 标识
  - ``begin(order)``    发起支付，返回跳转/二维码信息
  - ``verify(payload)`` 校验回调真伪（签名 + 金额 + 归属），返回 order_id 或 None
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import utils.configs as configs
from utils.Logger import logger


class PaymentError(Exception):
    """支付链路不可用（未配置 provider / 回调校验失败）。"""


class MockProvider:
    """演示用：下单后立即视为支付成功，不产生任何真实扣款。

    仅供本地联调与前端验收，靠 ``PAYMENT_PROVIDER=mock`` 显式开启。
    """

    name = "mock"
    is_mock = True

    def begin(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return {"provider": self.name, "order_id": order.get("order_id"), "auto_settle": True}

    def verify(self, payload: Dict[str, Any]) -> Optional[str]:
        """mock 无签名可验，只回显 order_id。真实 provider 必须校验签名 + 金额 + 归属。"""
        return (payload or {}).get("order_id") or None


_PROVIDERS = {"mock": MockProvider}


def get_provider():
    """返回当前 provider；未配置 / 未知值返回 None（调用方据此拒绝下单）。"""
    name = (getattr(configs, "payment_provider", "") or "").strip().lower()
    if not name:
        return None
    cls = _PROVIDERS.get(name)
    if not cls:
        logger.warning(f"[payment] unknown PAYMENT_PROVIDER={name!r}, checkout disabled")
        return None
    return cls()


def require_provider():
    """取 provider，未配置时抛 :class:`PaymentError`。"""
    provider = get_provider()
    if not provider:
        raise PaymentError("支付渠道暂未开通，请联系客服")
    return provider


def provider_is_mock() -> bool:
    provider = get_provider()
    return bool(provider and getattr(provider, "is_mock", False))
