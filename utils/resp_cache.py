"""Per-account TTL response cache for slow upstream GET endpoints.

页面加载慢的根因：反代每次都对 chatgpt.com 真打上游请求（models / accounts/check），
经代理节点 ~3s 基础延迟，大响应（models 48KB）更慢（4-13s）。这些 GET 响应短期内不变，
用按账号隔离的内存 TTL 缓存，二次加载直接命中，省掉上游往返。

只缓存有「短期内不变」契约的路径：models / accounts/check / 静态资源。会话相关路径
（backend-api/conversation/...）一律不缓存，见下方 _TTLS 注释。

Key = (req_token, path, query) — 按账号隔离，避免跨号串数据；req_token 是账号 access token。
缓存的是「未改写、未脱敏」的原始上游 body，命中后走与冷路径完全相同的改写/脱敏管线，
因此改写（origin host / petrol）与脱敏（镜像用户身份字段）行为在命中/未命中下保持一致。
"""
import threading
import time

_cache = {}
_lock = threading.Lock()

# 内存缓存条目上限：静态资源按 cache-busted URL 无限累积（每条目可达 KB~MB 级 JS bundle），
# 长时间运行会无界增长。超出上限按最旧插入时间淘汰（FIFO），保证内存有界。
_MAX_ENTRIES = 2048

# 各路径的缓存 TTL（秒）
#
# conversation/ 前缀被刻意排除：该前缀不只覆盖会话详情，还覆盖 conversation/{id}/stream_status、
# async-status、textdocs 以及全局的 conversation/init —— 都是随进行中的一轮对话不断变化的动态状态
# （真实浏览器基线显示 stream_status 在一轮内被反复轮询）。按 conversation/{id} 的定向失效既覆盖
# 不到全局状态路径，也无法在会话活跃期间阻止详情变陈旧。在「已完成会话」有一个可验证的判定契约
# 之前，这里不做任何 conversation/ 缓存。
_TTLS = {
    # 模型列表基本静态（账号 plan_type 变化才会变），切号时整体失效
    "backend-api/models": 3600,
    # 账号状态，变化极低频
    "backend-api/accounts/check/": 300,
    # CDN 静态资源（cache-busted 不可变），长 TTL 缓存让多用户冷启动也命中
    "assets/": 86400,
}


def cacheable(path: str, method: str):
    """返回该 GET 路径的缓存 TTL；不可缓存返回 None。"""
    if method.upper() != "GET":
        return None
    for prefix, ttl in _TTLS.items():
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return ttl
        elif path == prefix:
            return ttl
    return None


def get(key):
    with _lock:
        entry = _cache.get(key)
        if not entry:
            return None
        if time.time() - entry["at"] > entry["ttl"]:
            _cache.pop(key, None)
            return None
        return entry


def set(key, entry, ttl):
    stored = dict(entry)
    stored["at"] = time.time()
    stored["ttl"] = ttl
    with _lock:
        _cache[key] = stored
        if len(_cache) > _MAX_ENTRIES:
            overflow = len(_cache) - _MAX_ENTRIES
            oldest = sorted(_cache.items(), key=lambda kv: kv[1]["at"])[:overflow]
            for k, _ in oldest:
                _cache.pop(k, None)


def invalidate_path_prefix(prefix: str):
    """失效所有 path 命中 prefix 的缓存条目（跨账号）。用于会话详情定向失效。"""
    with _lock:
        for key in list(_cache.keys()):
            if isinstance(key, tuple) and len(key) >= 2 and key[1].startswith(prefix):
                _cache.pop(key, None)


def invalidate_all():
    """整体清空（切号时账号变更，旧账号的缓存全部作废）。"""
    with _lock:
        _cache.clear()


def size():
    with _lock:
        return len(_cache)
