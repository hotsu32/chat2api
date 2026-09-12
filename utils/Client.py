import asyncio
import os
import threading
import time

from curl_cffi.requests import AsyncSession

from utils.proxy_health import record as _record_health

# 连接复用：按 (proxy, impersonate, verify, timeout) 池化 AsyncSession，让 curl 的
# TCP+TLS 连接跨请求存活，省去每次请求 ~1.2s 的握手。会话 check-out 时清空 cookie，
# 避免同节点不同账号的 cookie 串号；close() 改为归还池子，空闲超过 _POOL_IDLE_TTL 的
# 会话由 reaper 硬关闭回收（curl 对已断开的 keep-alive 连接会透明重连，TTL 只用来限内存）。
#
# 单一 AsyncSession 同时承载 post/get/request 与 post_stream：sentinel（prepare 阶段）与
# conversation 共用同一条连接，使 prepare 预热好的连接能被 conversation 直接复用（preconnect）。
# curl_cffi.AsyncSession 绑定创建它的事件循环，跨 loop 复用会报 "attached to a different
# loop"；因此池按 loop 隔离：条目记录所属 loop，check-out 时 loop 不一致视为未命中并丢弃。
_POOL_IDLE_TTL = float(os.getenv("CLIENT_POOL_TTL", 60))
_POOL_MAX_SIZE = int(os.getenv("CLIENT_POOL_MAX_SIZE", 128))

_session_pool = {}  # key -> list of idle entries {"session","last_used","loop"}
_pool_lock = threading.Lock()


def _pool_key(proxy, impersonate, verify, timeout):
    return (proxy, impersonate, verify, timeout)


def _current_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _clear_cookies(session):
    try:
        session.cookies.clear()
    except Exception:
        pass


async def _close_entry(entry):
    s = entry.get("session")
    if s is not None:
        try:
            await s.close()
        except Exception:
            pass


def _schedule_close(entry):
    """在条目所属的原事件循环上异步关闭；loop 已死则丢引用交给 GC（仅测试多 loop 场景触发）。"""
    loop = entry.get("loop")
    if loop is not None and not loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_close_entry(entry), loop)
            return
        except Exception:
            pass
    entry["session"] = None


def _collect_expired_locked(now):
    """把空闲超时 + 超全局上限的条目从池中移出，返回待硬关闭的条目列表。"""
    expired = []
    for key in list(_session_pool.keys()):
        entries = _session_pool[key]
        kept = [e for e in entries if now - e["last_used"] <= _POOL_IDLE_TTL]
        expired.extend(e for e in entries if now - e["last_used"] > _POOL_IDLE_TTL)
        if kept:
            _session_pool[key] = kept
        else:
            del _session_pool[key]

    total = sum(len(v) for v in _session_pool.values())
    if total > _POOL_MAX_SIZE:
        all_entries = sorted(
            (e for v in _session_pool.values() for e in v),
            key=lambda e: e["last_used"],
        )
        overflow = total - _POOL_MAX_SIZE
        removed = {id(e) for e in all_entries[:overflow]}
        expired.extend(all_entries[:overflow])
        for key in list(_session_pool.keys()):
            _session_pool[key] = [e for e in _session_pool[key] if id(e) not in removed]
            if not _session_pool[key]:
                del _session_pool[key]
    return expired


async def _reap():
    now = time.time()
    with _pool_lock:
        expired = _collect_expired_locked(now)
    for e in expired:
        await _close_entry(e)


class Client:
    def __init__(self, proxy=None, timeout=15, verify=True, impersonate='safari15_3'):
        self.proxies = {"http": proxy, "https": proxy}
        self.timeout = timeout
        self.verify = verify
        self.impersonate = impersonate
        self._proxy = proxy
        self._key = _pool_key(proxy, impersonate, verify, timeout)
        self._returned = False
        loop = _current_loop()

        with _pool_lock:
            idle = _session_pool.get(self._key)
            entry = None
            stale = []
            while idle:
                e = idle.pop()
                e_loop = e.get("loop")
                if loop is None or e_loop is None or e_loop is loop:
                    entry = e
                    break
                stale.append(e)
            if idle is not None and not idle:
                del _session_pool[self._key]

        for e in stale:
            _schedule_close(e)

        if entry is None:
            entry = {
                "session": AsyncSession(proxies=self.proxies, timeout=self.timeout, impersonate=self.impersonate, verify=self.verify),
                "last_used": time.time(),
                "loop": loop,
            }
        else:
            entry["loop"] = loop

        self._entry = entry
        self.session = entry["session"]
        # 干净启动：清空 cookie，防止同节点上一账号残留 cookie 串到本次请求
        _clear_cookies(self.session)

    async def _timed(self, coro):
        """Await a request and record its outcome against the node's health."""
        start = time.time()
        try:
            r = await coro
        except Exception:
            _record_health(self._proxy, False, (time.time() - start) * 1000.0)
            raise
        _record_health(self._proxy, True, (time.time() - start) * 1000.0)
        return r

    async def post(self, *args, **kwargs):
        return await self._timed(self.session.post(*args, **kwargs))

    async def post_stream(self, *args, headers=None, cookies=None, **kwargs):
        return await self._timed(self.session.post(*args, headers=headers, cookies=cookies, **kwargs))

    async def get(self, *args, **kwargs):
        return await self._timed(self.session.get(*args, **kwargs))

    async def request(self, *args, **kwargs):
        return await self._timed(self.session.request(*args, **kwargs))

    async def put(self, *args, **kwargs):
        return await self._timed(self.session.put(*args, **kwargs))

    async def close(self):
        """归还连接池（不真正关闭，供 curl 复用 TCP+TLS 连接）。幂等。"""
        if self._returned:
            return
        self._returned = True
        self._entry["last_used"] = time.time()
        with _pool_lock:
            _session_pool.setdefault(self._key, []).append(self._entry)
        await _reap()

    async def discard(self):
        """硬关闭并丢弃（用于瞬时网络错误后强制重建连接）。幂等。"""
        if self._returned:
            return
        self._returned = True
        await _close_entry(self._entry)
