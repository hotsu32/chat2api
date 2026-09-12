"""Plus 免费试用（注册赠送 3 次成功回复）。

正式产品只卖 Plus / Pro，新用户的入口是**试用**而不是 Free 档。试用的口径是
「3 次成功的生成」，不是「3 次请求」—— 上游报错、代理失败、鉴权失败、空流、断连
都不该扣用户的额度，否则第一天就会有人因为我们这边抖了一下而白白少一次。

因此一次生成分两步，两条聊天入口（``reverseProxy`` 与 ``f_conversation_gateway``）
都必须按这个契约接线：

    res_id = trials.reserve(seed)          # 请求准入
    ...                                    # 走上游
    trials.settle(res_id, seed)            # 确实产出了一次完整回复
    trials.release(res_id, seed)           # 任何失败 / 中断路径

一次请求从准入到响应结束的完整生命周期由 :class:`TrialAttempt` 持有：它把
「预留 → 完成信号 → 结算 / 退回」折成一份终态一次的账。用它的入口层（``/v1`` 的
生成入口）只要在响应生命周期结束的 ``finally`` 里调一次 ``finish(delivered)``：
重复的完成回调、迟到的错误、响应被取消都只会得到一次终态。网关的两条流式入口目前
在传输层各自维护同一套语义（见 ``gateway/generation.py`` 的 ``_Trial``），尚未改用
这个对象；无论用哪个，契约都是本模块开头那两行 ``reserve`` / ``settle`` / ``release``。

``reserve`` 的三种结局彼此可分，调用方必须逐一处理：

  - 返回预留 id（str）：SaaS 试用用户，额度已预占，成功后调 ``settle``，失败调 ``release``；
  - 返回 ``None``：不走试用账，不占额度 —— 运营者 seed（无 user_auth 行）或当前有
    有效付费订单的用户。调用方按各自规则放行，**不应视为拒绝**；
  - 抛 :class:`TrialDenied`：账号有试用记录但本次被拒绝，原因见 ``exc.reason``。
    ``exhausted`` = 三次用完；``account_not_active`` = 未验证 / 封禁 / 冻结；
    ``subscription_expired`` = 曾付费但已到期，不回落试用；

数据层故障一律抛 :class:`utils.store.StoreError`，绝不伪装成已付费或运营者路径。

``settle`` / ``release`` 都是幂等的终态操作：谁先到谁生效，后到的返回 False 且
不改变余额。重复投递的终止事件不会扣两次，迟到的错误也抹不掉已经成功的那次。

余额全部落 SQLite（``trial_grants`` / ``trial_reservations``），不做内存计数：
进程重启余额不能变，并发第 4 次必须被拒。

档位判据区分两种「余额」（point 4 订正）：
  - ``remaining`` = total - used - 在途预留数，用于页面展示真正还能发起的次数；
  - ``admission_balance`` = total - used，不扣在途预留，用于判断档位是否成立。
    若用 ``remaining`` 判档位，第三次生成在「预留成功」之后再被问一次档位就会答
    「无权益」，把自己踢出去（见 :func:`trial_tier` 注释）。

与付费权益的关系（见 :mod:`utils.entitlements`）：

  - 付费有效期内按订单档次算，试用不参与，也不消耗；
  - 订阅到期后**不会**回落到没用完的注册试用 —— 那是给新用户的入场券，不是续费通道；
  - 未验证 / 被封禁 / 被冻结的账号不能消费试用。

孤儿预留回收（point 5）：

  进程崩溃会在 ``reserved`` 状态留下永久占位行。解法是在预留行上记录产生它的进程
  实例 id（``_INSTANCE_ID``，启动时生成一次 UUID），恢复时释放所有 ``reserved`` 行
  中归属**其他实例**的那些，当前实例自己的行不碰（那可能是正在跑的长研究任务）。

  ``recover_orphan_reservations()`` 在进程启动后调用一次，或由网关在发现余额异常时
  主动触发；它枚举归属其他实例的 ``reserved`` 行，逐一用 ``os.kill(pid, 0)`` 验证
  PID 是否已死（``ProcessLookupError`` = 确实不存在；``PermissionError`` = 进程存在，
  保守跳过），最后批量调用 :func:`utils.store.release_reservations_by_instance_ids`
  只释放**确认死亡**的实例遗留行，返回实际回收数量。这个机制在单进程部署
  （本项目默认形态）下安全可靠；多进程部署需要共享「当前活跃实例集合」
  （如进程注册表）才能扩展，超出当前合同范围，届时由网关协调层补充 API。

纯 stdlib + 惰性 ``utils.store``，不导入 FastAPI，可单测。
"""
from __future__ import annotations

import os
import secrets
import uuid
from typing import Any, Dict, List, Optional

from utils.Logger import logger

# 注册赠送的次数与档次。正式产品不提供 Free，试用只能是 Plus。
SIGNUP_TRIAL_COUNT = 3
TRIAL_TIER = "plus"

# 能消费试用的账号状态。注册时若开了邮箱验证，行是 ``unverified``，
# 额度可以先落库，但验证通过前不能用。
_CONSUMABLE_STATUS = ("active",)

# 进程唯一实例 id：格式 "<pid>:<uuid>"，用于孤儿预留回收时的进程存活检查。
# PID 在同一进程生命周期内不变；UUID 保证重启后（PID 可能被复用）实例 id 也不重复。
_INSTANCE_ID = f"{os.getpid()}:{uuid.uuid4()}"


class TrialDenied(Exception):
    """试用额度被明确拒绝（区别于「不走试用账」的 None 路径）。

    ``reason`` 取值：
      - ``"exhausted"``            —— 三次额度已用完（或已被并发预留占满）；
      - ``"account_not_active"``   —— 账号未验证 / 封禁 / 冻结；
      - ``"subscription_expired"`` —— 曾购买订阅但已到期，不回落到试用。
    """

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)


def _state(email: str, strict: bool = False) -> Dict[str, Any]:
    from utils import store as _store

    grant = _store.get_trial_grant(email, strict=strict)
    if not grant:
        return {
            "granted": False, "tier": "", "total": 0, "used": 0,
            "reserved": 0, "remaining": 0, "admission_balance": 0,
        }
    reserved = _store.count_open_trial_reservations(email, strict=strict)
    total, used = grant["total"], grant["used"]
    return {
        "granted": True,
        "tier": grant.get("tier") or "",
        "total": total,
        "used": used,
        "reserved": reserved,
        # 页面展示用：扣掉在途预留，是用户真正还能发起的次数。
        "remaining": max(0, total - used - reserved),
        # 档位判据用：只认已结算的消费，不扣在途预留。
        # 若用 remaining 判档位，第三次预留后 remaining=0，enforce_tier 就把自己踢掉。
        "admission_balance": max(0, total - used),
    }


def trial_state(email: str, strict: bool = False) -> Dict[str, Any]:
    """试用账面：``granted / tier / total / used / reserved / remaining / admission_balance``。

    ``remaining`` 是扣掉在途预留的「可发起次数」，用于页面展示。
    ``admission_balance`` 是仅扣已结算消费的「档位判据余额」，用于 :func:`trial_tier`。
    """
    if not email:
        return {
            "granted": False, "tier": "", "total": 0, "used": 0,
            "reserved": 0, "remaining": 0, "admission_balance": 0,
        }
    return _state(email, strict=strict)


def trial_remaining(email: str, strict: bool = False) -> int:
    return trial_state(email, strict=strict)["remaining"]


def _user_row(email: str = "", seed: str = "", strict: bool = False):
    from utils import store as _store

    if seed:
        return _store.get_user_auth_by_seed(seed, strict=strict)
    if email:
        return _store.get_user_auth(email, strict=strict)
    return None


def _has_ever_purchased(email: str, strict: bool = False) -> bool:
    """该用户历史上是否有过任何已支付订单（含**已过期**的）。

    试用是新用户的入场券，不是续费通道：买过又到期的人不能靠没用完的注册试用续命。
    所以判据是「有没有付过钱」，而不是「现在有没有有效订单」。

    数据层故障时，``strict=False`` 仍然抛 :class:`utils.store.StoreError` ——
    把故障静默为「没买过」等于一次锁库就把过期用户全部绕过了付费门禁，远比误拦
    一次危险。调用方捕获 ``StoreError`` 并向上抛，不转化为任何「放行」语义。

    （注意：早期版本在 ``strict=False`` 时把故障当成「买过」，这是错的——
    那等于把数据库故障伪装成付费用户，测试要求 point 1 的 StoreError 传播。）
    """
    from utils import store as _store
    from utils.store import StoreError

    orders = _store.list_orders(email=email, strict=True)
    return any((o.get("status") or "") == "paid" for o in orders)


def trial_tier(email: str, strict: bool = False) -> str:
    """试用当前是否构成权益：还有未结算余额且账号可用则 ``"plus"``，否则 ``""``。

    供 :mod:`utils.entitlements` 在**没有**有效付费订单时兜底调用。

    判据使用 ``admission_balance``（total - used），**不**使用 ``remaining``
    （total - used - reserved）。原因：若使用 remaining，第三次生成已预留但尚未
    结算时 remaining=0，enforce_tier 在预留之后再次检查档位就会答「无权益」，
    把本次请求的自己踢出去（acceptance.md line 78）。
    """
    if not email:
        return ""
    row = _user_row(email=email, strict=strict)
    if not row or (row.get("status") or "") not in _CONSUMABLE_STATUS:
        return ""
    if _has_ever_purchased(email, strict=strict):
        return ""
    from utils import store as _store
    grant = _store.get_trial_grant(email, strict=strict)
    if (not grant or grant['seed'] != row.get('seed') or grant['tier'] != TRIAL_TIER
            or grant['total'] - grant['used'] <= 0):
        return ""
    return TRIAL_TIER


def reserve(seed: str) -> Optional[str]:
    """请求准入：占住一次试用额度，返回预留 id；三种结局彼此可分。

    - 返回 str：SaaS 试用用户，额度已预占；
    - 返回 None：运营者 seed 或当前有效付费用户，不走试用账（**非拒绝**）；
    - 抛 :class:`TrialDenied`：有账号但本次被拒绝，``exc.reason`` 说明原因；
    - 抛 :class:`utils.store.StoreError`：数据层故障，不得被当成任何放行路径。

    调用方必须区分后三种：``None`` = 付费免扣，``TrialDenied`` = 明确拒绝，
    ``StoreError`` = 系统故障。若把后两种当成 ``None`` 处理，相当于一次故障或
    额度耗尽就静默给用户免费放行。
    """
    from utils import store as _store
    from utils.store import StoreError

    if not seed:
        return None

    # 数据层故障在此直接传播（StoreError），不捕获。
    row = _user_row(seed=seed, strict=True)
    if not row:
        # 无 user_auth 行 = 运营者 seed / 直传 token，fail-open。
        return None

    status = row.get("status") or ""
    if status not in _CONSUMABLE_STATUS:
        raise TrialDenied("account_not_active")

    email = row.get("email") or ""
    if not email:
        # 极端边缘：有 user_auth 但无 email（数据一致性问题），视为无法消费。
        raise TrialDenied("account_not_active")

    # 曾付费检查：故障抛 StoreError，不伪装。
    has_paid = _has_ever_purchased(email, strict=True)
    if has_paid:
        # 区分「当前有效付费」和「曾付费但到期」：
        # 两者都不走试用账，但语义不同。
        # 当前有效付费 → None（付费按有效期不限次数，不占试用额度）。
        # 曾付费但到期  → TrialDenied("subscription_expired")（不回落到试用）。
        from utils import entitlements as _ent
        active = _ent.active_orders(email)
        if active:
            return None  # 当前有效付费，不占试用账
        raise TrialDenied("subscription_expired")

    # 原子预留：INSERT ... SELECT ... WHERE balance >= 1，持 _WRITE_LOCK。
    res_id = "res_" + secrets.token_hex(12)
    success = _store.reserve_trial(res_id, email, seed, 1, instance_id=_INSTANCE_ID)
    if not success:
        raise TrialDenied("exhausted")
    return res_id


def settle(res_id: str, seed: str) -> bool:
    """确认这次预留产出了一次成功回复，扣减余额。

    ``seed`` 必填，在 SQL 里校验归属——拿到别人的预留 id 不能结算。
    返回是否由**本次调用**完成了流转：重复终态返回 False，余额只扣一次。
    """
    if not res_id or not seed:
        return False
    from utils import store as _store

    return _store.settle_trial_reservation(res_id, seed)


def release(res_id: str, seed: str) -> bool:
    """这次生成失败 / 被中断，退回预留的额度。

    ``seed`` 必填，在 SQL 里校验归属。
    已结算的预留不退（终态不可逆），已释放的重复调用返回 False。
    失败路径必须调用它，否则余额会被在途预留永久占住。
    """
    if not res_id or not seed:
        return False
    from utils import store as _store

    return _store.release_trial_reservation(res_id, seed)


class TrialAttempt:
    """一次生成持有的试用预留：终态一次，且只有「完成 + 交付」才扣次数。

    ``reserve`` 返回预留 id 只说明准入通过 —— 用户还没拿到东西。这次预留算不算
    消费，取决于本次生成有没有走到**项目定义的成功完成信号**：

      - :meth:`mark_completed`：传输层观察到成功完成信号（下游出现带
        ``finish_reason`` 的终止分片且有正文 / 非流式响应带非空正文）。只置位，
        自己不下账，重复调用无副作用；
      - :meth:`settle` / :meth:`release`：终态操作。谁先到谁生效，后到的返回 False
        且不再改变余额 —— 重复投递的终止事件、迟到的错误回调因此变得无害；
      - :meth:`finish`：响应生命周期结束时的合并判据。只有「本次生成确实完成」
        且「客户端确实收到了完整响应」同时成立才结算；上游报错、空流、生成器抛错、
        客户端断连、发送失败一律退回额度。

    断连不扣次数是刻意的：没人收到的回复不算「一次成功的生成」。反过来 ``delivered``
    也不看客户端在哪个字节断的 —— 只有整段响应发完才算交付。

    台账（SQLite）不可用时 ``finish`` 记录错误并返回 False，且**不**把本次尝试标成
    终态：宁可让这次预留留在在途状态（不产生免费额度，也不冒充已付费），也不在清理
    路径上抛异常打断响应收尾。它不会发明第二次额度，也不会把故障说成已结算。
    """

    def __init__(self, reservation: str, seed: str) -> None:
        self.reservation = reservation
        self.seed = seed
        self.completed = False
        self._resolved = False

    @property
    def resolved(self) -> bool:
        """本次尝试是否已落到终态（结算或退回，且台账已确认）。"""
        return self._resolved

    def mark_completed(self) -> None:
        """记录「本次生成已产出完整回复」。幂等，且自己不下账。"""
        self.completed = True

    def settle(self) -> bool:
        """成功结算；重复调用返回 False，余额只扣一次。"""
        if self._resolved:
            return False
        if settle(self.reservation, self.seed):
            self._resolved = True
            return True
        # 台账里已是终态（重复回调 / 已被 release 抢先），不再重试。
        self._resolved = True
        return False

    def release(self) -> bool:
        """退回额度；已结算的不退款，重复调用返回 False。"""
        if self._resolved:
            return False
        if release(self.reservation, self.seed):
            self._resolved = True
            return True
        self._resolved = True
        return False

    def finish(self, delivered: bool) -> bool:
        """响应生命周期结束：``completed and delivered`` 才结算，其余退回。

        返回 True 仅当**本次尝试被结算**（真的扣掉了一次额度）；退回、重复回调、
        台账不可用都返回 False。调用方据此判断这次生成是否消费了试用额度。
        """
        from utils.store import StoreError

        try:
            if self.completed and delivered:
                return self.settle()
            self.release()
            return False
        except StoreError:
            logger.error("[trials] attempt finalization unavailable; reservation left open")
            return False


def recover_orphan_reservations() -> int:
    """释放上一个进程崩溃遗留的孤儿预留，返回回收数量。

    回收依据是**进程实例 id 的 PID 存活检查**，不是时间戳。``_INSTANCE_ID`` 格式为
    ``"<pid>:<uuid>"``：PID 用于存活检测，UUID 保证重启后 PID 复用时实例 id 仍唯一。

    判活规则（fail-closed）：
      - ``ProcessLookupError`` → 进程确实不存在，可回收；
      - ``PermissionError``   → 进程存在但无权发信号，**不**回收（保守处理）；
      - 任何其他 OSError     → 不回收；
      - iid 无法解析为 "<pid>:..." → 不回收（可能是旧格式行，保守跳过）。

    当前进程自己写的预留（``instance_id == _INSTANCE_ID``）不被触碰。
    ``instance_id IS NULL`` 的行也不被触碰（由 store 过滤）。
    """
    from utils import store as _store

    orphan_ids = _store.get_orphan_instance_ids(_INSTANCE_ID)
    dead: List[str] = []
    for iid in orphan_ids:
        try:
            pid = int(iid.split(":")[0])
        except (ValueError, IndexError):
            continue  # 无法解析 PID — 保守跳过
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            dead.append(iid)  # PID 确实不存在，可回收
        except OSError:
            pass  # PermissionError 或其他 — 进程可能存在，保守跳过
    recovered = _store.release_reservations_by_instance_ids(dead)
    if recovered:
        logger.info("[trials] orphan reservation recovery completed")
    return recovered
