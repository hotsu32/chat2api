"""极简内存滑动窗口限流（同 IP 注册频率、登录失败次数等）。

单进程内存实现，重启即清空 —— 够用于「防脚本批量注册 / 撞库」这一层。
多实例部署时应换 Redis（本轮不做，见 KIMI_REVIEW「未采纳」）。

两种用法，按「什么该被计数」区分：

  ``allow(key, limit)``           检查并占用一次。适合「每来一次就算一次」的场景（注册）。
  ``over_limit`` + ``hit``        检查与计数分离。适合只想给**失败**计数的场景（登录）——
                                  登录成功若也扣配额，正常用户反复登录会把自己锁死。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict

_lock = threading.Lock()
# 普通 dict 而非 defaultdict：读路径（remaining / over_limit）绝不能凭空建条目。
# 登录限流的 key 含用户提交的 email，是攻击者可控的；一旦读也建条目，
# 灌一百万个不存在的邮箱就等于灌一百万条常驻记录 —— 限流本身成了内存耗尽的入口。
_hits: Dict[str, Deque[float]] = {}

# key 总数上限。正常站点远到不了（一个 IP / 一个账号各占一条），
# 到得了就说明正在被灌；此时淘汰，保证内存有界。
_MAX_KEYS = 50_000

# 见过的最长窗口。``_prune_locked`` 用它决定「多旧算全过期」——
# 写死 1 小时的话，配了更长窗口（如 SIGNIN_EMAIL_RATE_WINDOW > 3600）时，
# 窗口内仍然有效的记录会被提前回收，限流被静默稀释。
_max_window = 3600


def _prune_locked(now: float) -> None:
    """清掉所有已经全部过期的 key。调用方必须持锁。

    统一用「见过的最长窗口」兜底：多留一会儿只是晚一点回收，不影响正确性；
    留得不够则会把还在生效的计数抹掉，那是正确性问题。
    """
    cutoff = now - _max_window
    for key in [k for k, q in _hits.items() if not q or q[-1] < cutoff]:
        _hits.pop(key, None)


def _evict_locked() -> None:
    """表满时腾一个位置。调用方必须持锁。

    淘汰键是「命中次数最少，同数再看最久未命中」，**不是**单纯的最久未命中。
    后者可以被用来绕过限流：受害者的失败记录必然早于攻击者的洪水，按时间淘汰
    会先挤掉受害者那条，账号桶归零，撞库继续。按命中数淘汰时，洪水里各只有 1 次
    命中的噪声 key 会先自相残杀，攻击者挤不掉一条已经累计了多次失败的记录。
    """
    victim = min(_hits, key=lambda k: (len(_hits[k]), _hits[k][-1] if _hits[k] else 0))
    _hits.pop(victim, None)


def _touch_locked(key: str, now: float, window: int) -> Deque[float]:
    """取出（必要时创建）``key`` 的队列并丢弃窗口外的记录。调用方必须持锁。"""
    global _max_window
    if window > _max_window:
        _max_window = window
    q = _hits.get(key)
    if q is None:
        if len(_hits) >= _MAX_KEYS:
            _prune_locked(now)
            if len(_hits) >= _MAX_KEYS:
                _evict_locked()   # 仍然满 = 正在被灌
        q = _hits[key] = deque()
    cutoff = now - window
    while q and q[0] < cutoff:
        q.popleft()
    return q


def _count_locked(key: str, now: float, window: int) -> int:
    """窗口内的计数；key 不存在返回 0 且**不**建条目。调用方必须持锁。"""
    q = _hits.get(key)
    if q is None:
        return 0
    cutoff = now - window
    while q and q[0] < cutoff:
        q.popleft()
    if not q:
        _hits.pop(key, None)   # 空队列不留着占位
        return 0
    return len(q)


def allow(key: str, limit: int, window: int = 3600) -> bool:
    """``key`` 在 ``window`` 秒内是否还有配额；``limit <= 0`` 表示不限流。

    命中则记一次并返回 True；超限返回 False（不记）。
    """
    if limit <= 0:
        return True
    now = time.time()
    with _lock:
        q = _touch_locked(key, now, window)
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def remaining(key: str, limit: int, window: int = 3600) -> int:
    """剩余配额（用于提示文案）。只读，不建条目。"""
    if limit <= 0:
        return -1
    now = time.time()
    with _lock:
        return max(0, limit - _count_locked(key, now, window))


def over_limit(key: str, limit: int, window: int = 3600) -> bool:
    """是否**已经**超限。只读，不计数、不建条目。"""
    if limit <= 0:
        return False
    now = time.time()
    with _lock:
        return _count_locked(key, now, window) >= limit


def hit(key: str, window: int = 3600) -> None:
    """记一次，不做判断（判断交给 :func:`over_limit`）。

    ``window`` 只用于顺手清理过期项，不影响是否记录。
    """
    now = time.time()
    with _lock:
        _touch_locked(key, now, window).append(now)


def reset(key: str = "") -> None:
    """清空计数（登录成功 / 重置密码成功；测试也用）。``key`` 为空清全部。"""
    global _max_window
    with _lock:
        if key:
            _hits.pop(key, None)
        else:
            _hits.clear()
            _max_window = 3600   # 全清时连「见过的最长窗口」一起复位，别让上一个用例的长窗口漏到下一个
