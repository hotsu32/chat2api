"""支付网关（可插拔）+ 回调结算校验。

真实商户号（微信 / 支付宝）到位前，本模块只提供 ``mock`` 实现，并且**默认关闭**：
``PAYMENT_PROVIDER`` 未配置或填了未知值时一律拒绝下单（fail-closed）。
宁可「暂时不能下单」这种诚实的不可用，也不要一个静默放行的白拿后门。

订单生命周期由本模块定义，页面侧只能创建 ``pending`` 单，
置 ``paid`` 必须经 ``activate``，并且激活是幂等的（支付回调会重复投递）。

## provider 契约

接真实网关时新增一个 provider 类实现同样两个方法即可，页面侧无需改动：

  - ``name``            provider 标识（同时是回调事务 id 的命名空间）
  - ``begin(order)``    发起支付，返回跳转/二维码信息
  - ``verify(payload)`` 校验回调真伪，返回 :class:`ProviderCallback` 或 ``None``

``verify`` 的返回值被**类型强制**：只有 :class:`ProviderCallback` 会被接受，
字符串 / 字典 / ``order_id`` 回显一律判为无效回调。这是有意的 —— 旧实现允许
``verify`` 返回一个裸 ``order_id``，那等于把「激活哪张单」的决定权交给回调载荷，
任何能构造一个已知订单号的人都能结算它。

## 结算前的服务端校验（:func:`validate_callback`）

``verify`` 只回答「这回调是不是 provider 本人发的」。它**不**回答「这笔钱对不对」。
后者必须由服务端拿库里的订单来比对，逐条：

  1. 订单必须存在（查库失败抛 ``StoreError``，让网关重投而不是当失败）；
  2. 回调声明的渠道必须与当前 provider 一致；
  3. 金额必须与下单时服务端定价的快照**完全相等**（不接受「少付一点也行」）；
  4. 币种必须与 ``PAYMENT_CURRENCY`` 一致（防止用另一种面值更低的币种结算）；
  5. provider 事务 id 必须非空，且**不得已经绑给另一张订单**（重放到别的单）；
  6. 同一张单的重复回调放行 —— 结算本身幂等（``store.settle_order`` 不会重复延期）。

第 5 条由 ``store.bind_payment_transaction`` 承担：读绑定与写绑定在**同一个事务**里
完成（BEGIN IMMEDIATE + 进程内写锁），所以并发回调只有一个赢家；返回 ``bound`` /
``idempotent`` 放行，``conflict`` 判为重放。存储层故障抛 ``StoreError``（语义是让网关
重投），**不**降级成「当作没绑过」—— 把「查不了」当成「没绑过」，等于让一次数据库
打嗝放行一笔可能的重放。

事务 id ↔ 订单 的绑定写在车队库的 ``meta`` 表里（``paytxn:<provider>:<txn>``）。
用 KV 而不是新列，是为了不动 ``utils.store`` 的表结构：这条绑定是支付层的记账，
不是订单的固有属性。真实网关接入时应换成 provider 自己的对账表，P0 阶段这个
KV 足以挡住「同一个支付流水号被用来激活第二张单」。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

import utils.configs as configs
import utils.store as store
from utils.Logger import logger


class PaymentError(Exception):
    """支付链路不可用 / 回调校验失败。

    ``reason`` 是固定匿名代码，可直接进日志与审计，不含任何回调原文。
    """

    def __init__(self, message: str, reason: str = "payment_error"):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ProviderCallback:
    """provider 校验通过后的回调事实（真实渠道必须填满每个字段）。

    ``amount`` / ``currency`` 是**provider 声明的**收款事实；
    它们不会被直接采信，必须与库里的订单逐条比对（见 :func:`validate_callback`）。
    """

    order_id: str
    transaction_id: str
    amount: str
    currency: str
    provider: str

    def __post_init__(self) -> None:
        # 标识字段在这里归一化一次：绑定 key 与校验必须用同一个值，否则
        # "txn1" 与 " txn1" 会拼成两个 key，同一笔支付能绑上两张单。
        object.__setattr__(self, "transaction_id", str(self.transaction_id or "").strip())
        if not str(self.order_id or "").strip():
            raise PaymentError("回调缺少订单号", "missing_order_id")
        if not self.transaction_id:
            raise PaymentError("回调缺少支付流水号", "missing_transaction_id")


class MockProvider:
    """演示用：下单后立即视为支付成功，不产生任何真实扣款。

    仅供本地联调与前端验收，需同时设置 ``PAYMENT_PROVIDER=mock`` 和
    ``APP_ENV=development``（或测试隔离环境的 ``test``）。

    它**没有**可校验的签名，因此 ``verify`` 永远返回 ``None``：mock 不接受回调，
    结算只走 ``api_checkout`` 里的 ``auto_settle`` 路径。把 mock 的 verify 留成
    「回显 order_id」曾是一个真实缺口 —— 任何注册用户「建单 + 自投回调」两步就能
    把自己的单置成已支付。宁可在本地联调时少一条路径，也不能留这个形状。
    """

    name = "mock"
    is_mock = True

    def begin(self, order: Dict[str, Any]) -> Dict[str, Any]:
        if configs.app_env not in ('development', 'test'):
            raise PaymentError("当前环境禁止演示支付", "mock_disabled")
        return {"provider": self.name, "order_id": order.get("order_id"), "auto_settle": True}

    def verify(self, payload: Dict[str, Any]) -> Optional[ProviderCallback]:
        """mock 无签名可验 —— 永远判为无效回调（fail-closed）。"""
        logger.warning("[payment] mock provider must not accept payment callbacks")
        return None


_PROVIDERS = {"mock": MockProvider}


def get_provider():
    """返回当前 provider；未配置 / 未知值返回 None（调用方据此拒绝下单）。"""
    name = (getattr(configs, "payment_provider", "") or "").strip().lower()
    if not name:
        return None
    if name == 'mock' and configs.app_env not in ('development', 'test'):
        logger.warning('[payment] mock payment disabled outside local/test environments')
        return None
    cls = _PROVIDERS.get(name)
    if not cls:
        logger.warning("[payment] unknown provider, checkout disabled")
        return None
    return cls()


def require_provider():
    """取 provider，未配置时抛 :class:`PaymentError`。"""
    provider = get_provider()
    if not provider:
        raise PaymentError("支付渠道暂未开通，请联系客服", "provider_unconfigured")
    return provider


def provider_is_mock() -> bool:
    provider = get_provider()
    return bool(provider and getattr(provider, "is_mock", False))


def expected_currency() -> str:
    return (getattr(configs, "payment_currency", "CNY") or "CNY").upper()


def normalize_amount(value: Any) -> Optional[Decimal]:
    """把回调声明的金额规整成可比较的 ``Decimal``；非法返回 ``None``。

    只接受「主单位十进制字符串/数字」这一种口径（``"99"``、``"99.00"``、``99``）。
    以分为单位上报的渠道（微信 ``total_fee`` 之类）必须由 provider 适配层换算成
    主单位，否则这里会因为差了 100 倍而拒绝对账 —— 这是刻意的：换算口径属于渠道
    适配知识，不该让结算层去猜。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    # 超过两位小数说明口径不是「主单位 × 100」，拒绝而不是四舍五入。
    if -amount.as_tuple().exponent > 2:
        return None
    return amount


def _txn_key(provider_name: str, transaction_id: str) -> str:
    return f"paytxn:{(provider_name or '').lower()}:{transaction_id}"


def accept_callback(provider, payload: Dict[str, Any]) -> ProviderCallback:
    """调用 provider 的 ``verify`` 并强制其返回结构化结果。"""
    result = provider.verify(payload)
    if not isinstance(result, ProviderCallback):
        raise PaymentError("回调校验失败", "invalid_callback")
    if (result.provider or "").lower() != (getattr(provider, "name", "") or "").lower():
        raise PaymentError("回调渠道不匹配", "provider_mismatch")
    return result


def validate_callback(provider, callback: ProviderCallback) -> Dict[str, Any]:
    """结算前的服务端校验，返回已核验的订单行。

    只回答「这笔回调能否安全地激活这张单」，不做任何状态流转 —— 调用方拿到订单后
    自行走 ``_fulfil_order``。任何一条不满足都抛 :class:`PaymentError`（带匿名
    ``reason``）；数据层故障抛 :class:`store.StoreError`，语义是「重投」。
    """
    order = store.get_order(callback.order_id, strict=True)
    if not order:
        raise PaymentError("订单不存在", "unknown_order")

    status = (order.get("status") or "").lower()
    if status not in ("pending", "paid"):
        # failed / expired 单不再接受支付事实，避免把已作废的单重新激活。
        raise PaymentError("订单状态不接受支付", "order_not_payable")

    declared = normalize_amount(callback.amount)
    if declared is None:
        raise PaymentError("回调金额非法", "invalid_amount")
    expected = normalize_amount(order.get("amount"))
    if expected is None or declared != expected:
        # 金额不一致：不给任何「接近」的余地，也不回显两个数字。
        raise PaymentError("回调金额与订单不符", "amount_mismatch")

    currency = (callback.currency or "").strip().upper()
    if not currency or currency != expected_currency():
        raise PaymentError("回调币种不符", "currency_mismatch")

    key = _txn_key(callback.provider, callback.transaction_id)
    # 认领流水号必须是**一个**事务：旧的 get_meta -> set_meta 两步里，两个并发回调
    # 都能读到「未绑定」再各自写入，同一个支付流水号就把两张单都结算了。
    # 存储层故障抛 StoreError（语义是「重投」），绝不降级成「当作没绑过」——
    # 后者会把一次数据库打嗝变成一次放行。
    outcome = store.bind_payment_transaction(key, callback.order_id)
    if outcome == store.PAYMENT_TXN_CONFLICT:
        # 同一个支付流水号被用来激活另一张单 —— 要么是重放攻击，要么是渠道对账错乱，
        # 两种都不该自动放行。
        logger.error("[payment] provider transaction already bound to another order")
        raise PaymentError("支付流水号已被使用", "transaction_replay")
    if outcome not in (store.PAYMENT_TXN_BOUND, store.PAYMENT_TXN_IDEMPOTENT):
        # 存储层给了契约外的结果。语义上这仍属数据层异常（不是「这笔回调不合法」），
        # 所以按 StoreError 处理：让网关重投，而不是回 400 把这笔钱丢掉。
        logger.error("[payment] unexpected provider transaction binding outcome")
        raise store.StoreError("payment transaction binding outcome unknown")

    return order
