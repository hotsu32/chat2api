"""前端实时同步：用账号 session-token cookie 抓官网最新 HTML（logged_in 版）。

替代 templates/chatgpt.html 快照，使镜像前端始终跟随官网最新版。
官网首页 SSR 依赖 __Secure-next-auth.session-token cookie（不认 Bearer access_token），
所以必须用账号持有者的 session cookie 抓取。
"""
import os
import time

from curl_cffi import requests as cffi_requests
from starlette.concurrency import run_in_threadpool

from utils.Logger import logger
from utils.configs import proxy_url_list

# 官网首页 HTML 模板缓存（账号无关的 HTML 骨架；client-bootstrap 里的 user/account 是抓取账号的，
# 前端加载后会通过 cookie / session 数据刷新）
_template_cache = {"html": None, "fetched_at": 0.0}

# 模板刷新周期（秒）。前端 build 变化约 1-2 周一次，30 分钟足够跟随。
TEMPLATE_TTL = 1800

_FETCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# session cookie 文件：一行，格式 __Secure-next-auth.session-token=xxx; cf_clearance=yyy; oai-did=zzz
SESSION_COOKIE_FILE = os.path.join("data", "session_cookie.txt")


def _load_session_cookie() -> str:
    if os.path.exists(SESSION_COOKIE_FILE):
        with open(SESSION_COOKIE_FILE, "r", encoding="utf-8") as f:
            line = f.read().strip()
            if line:
                return line
    raise RuntimeError(
        f"session cookie not configured: {SESSION_COOKIE_FILE} "
        "(格式: __Secure-next-auth.session-token=xxx; cf_clearance=yyy; oai-did=zzz)"
    )


def _fetch_official_html_sync(cookie: str, proxy_url: str = None) -> str:
    r = cffi_requests.get(
        "https://chatgpt.com/",
        impersonate="chrome124",
        proxy=proxy_url,
        headers={"User-Agent": _FETCH_UA, "Cookie": cookie},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"fetch official frontend failed: HTTP {r.status_code}")
    if '"authStatus":"logged_in"' not in r.text:
        raise RuntimeError("official frontend not logged_in (session cookie expired?)")
    return r.text


def _default_proxy() -> str:
    if proxy_url_list:
        return proxy_url_list[0].replace("{}", "")
    return None


async def get_frontend_template() -> str:
    """返回官网最新 logged_in HTML 骨架，带缓存。"""
    now = time.time()
    if _template_cache["html"] and now - _template_cache["fetched_at"] < TEMPLATE_TTL:
        return _template_cache["html"]

    cookie = _load_session_cookie()
    proxy_url = _default_proxy()
    html = await run_in_threadpool(_fetch_official_html_sync, cookie, proxy_url)
    _template_cache["html"] = html
    _template_cache["fetched_at"] = now
    logger.info(f"[frontend_sync] template refreshed ({len(html)} bytes, logged_in)")
    return html


def get_session_cookie() -> str:
    """返回用于设置给用户浏览器的 session cookie 字符串（账号持有者的凭据）。"""
    return _load_session_cookie()
