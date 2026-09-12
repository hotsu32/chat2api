import asyncio
import hashlib
import json
import random
import time
from datetime import datetime, timezone

from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse, Response
from starlette.background import BackgroundTask

import utils.globals as globals
from chatgpt.authorization import verify_token, get_req_token, reject_development_alias
from chatgpt.fp import extract_header_fp, get_fp
from utils.Client import Client
from utils.Logger import logger
from utils.configs import chatgpt_base_url_list, sentinel_proxy_url_list, force_no_history, file_host, voice_host, accept_language
from gateway.frontend_sync import get_session_cookie, refresh_cached_frontend, FrontendSessionError
from gateway.identity import decode_jwt_payload
from gateway.sse_parser import extract_data_json, iter_sse_events_async
from utils.usage import record_usage
from utils.tiers import enforce_tier
from gateway.generation import admit_generation, generation_lifetime, track_generation_client
from gateway.generation import observe_generation_stream
from utils import resp_cache


def generate_current_time():
    current_time = datetime.now(timezone.utc)
    formatted_time = current_time.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    return formatted_time


def _usage_kind(path: str):
    """Coarse usage category for a proxied path; None for non-usage traffic (assets/session)."""
    p = path.lower()
    if "conversation" in p:
        return "conversation"
    if "images/generations" in p:
        return "image"
    if "audio/speech" in p:
        return "audio"
    return None


def _is_textual_content(content_type: str) -> bool:
    """响应是否可安全按文本缓存（字体/wasm/octet-stream 等二进制会被 atext 破坏，须排除）。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("text/"):
        return True
    return ct in (
        "application/json",
        "application/javascript",
        "application/x-javascript",
        "application/xml",
        "application/ld+json",
        "application/manifest+json",
    )


# 通用反代里 JSON 载荷中会暴露账号持有者真实身份的字段 → 匿名化值。
# 注意：通用反代无法穷举所有身份字段，这里是「定向缓解 + 残余风险已文档化」，非全量脱敏。
_GENERIC_IDENTITY_FIELDS = {
    "account_email": "",
    "account_name": "ChatGPT",
    "email": "",
    "name": "ChatGPT",
    "first_name": "ChatGPT",
    "last_name": "",
    "phone_number": "",
    "picture": "",
}


def _scrub_identity_fields(node, *, identity=False):
    """Scrub profile objects, preserving names in model/tool/business objects."""
    if isinstance(node, dict):
        for key in list(node.keys()):
            if key in _GENERIC_IDENTITY_FIELDS and (identity or key.startswith('account_')):
                node[key] = _GENERIC_IDENTITY_FIELDS[key]
            else:
                _scrub_identity_fields(node[key], identity=key in ('user', 'profile', 'account', 'owner'))
    elif isinstance(node, list):
        for item in node:
            _scrub_identity_fields(item, identity=identity)
    return node


def _rewrite_and_scrub(content, *, path, base_url, petrol, origin_host, seed_cookie, content_type):
    """对上游 body 做 URL 改写 + 身份脱敏，返回最终字符串。

    与冷路径原逻辑完全一致，抽成独立函数供「缓存命中」与「缓存未命中」共用，
    保证改写（origin host / petrol）与脱敏（镜像用户身份字段）在两种路径下行为一致。
    """
    if "public-api/" in path:
        content = (content
                   .replace("https://ab.chatgpt.com", f"{petrol}://{origin_host}")
                   .replace("https://cdn.oaistatic.com", f"{petrol}://{origin_host}")
                   .replace("webrtc.chatgpt.com", voice_host if voice_host else "webrtc.chatgpt.com")
                   .replace("files.oaiusercontent.com", file_host if file_host else "files.oaiusercontent.com")
                   .replace("chatgpt.com/ces", f"{origin_host}/ces")
                   )
    else:
        content = (content
                   .replace("https://ab.chatgpt.com", f"{petrol}://{origin_host}")
                   .replace("https://cdn.oaistatic.com", f"{petrol}://{origin_host}")
                   .replace("webrtc.chatgpt.com", voice_host if voice_host else "webrtc.chatgpt.com")
                   .replace("files.oaiusercontent.com", file_host if file_host else "files.oaiusercontent.com")
                   .replace("web-sandbox.oaiusercontent.com", f"{origin_host}/sandbox")
                   .replace("https://chatgpt.com", f"{petrol}://{origin_host}")
                   .replace("chatgpt.com/ces", f"{origin_host}/ces")
                   )
    if base_url == "https://web-sandbox.oaiusercontent.com":
        content = content.replace("/assets", "/sandbox/assets")
    # 定向脱敏：镜像用户（有 seed cookie）访问 backend-api JSON 时，抹除已知身份字段，
    # 防止 catch-all 透传把账号持有者真实身份（email/name/phone 等）泄漏给镜像用户。
    if seed_cookie and "backend-api/" in path and "application/json" in content_type:
        try:
            payload = json.loads(content)
            _scrub_identity_fields(payload, identity=path.rstrip('/') in (
                'backend-api/me', 'backend-api/settings', 'backend-api/user'))
            if path.rstrip('/') == 'backend-api/me':
                for org in (payload.get('orgs') or {}).get('data', []):
                    _scrub_identity_fields(org, identity=True)
                    for field, value in (('title', 'ChatGPT'), ('description', '')):
                        if field in org:
                            org[field] = value
            content = json.dumps(payload, ensure_ascii=False)
        except (ValueError, TypeError):
            # 非合法 JSON（如 HTML 片段）跳过脱敏，保持原样透传
            pass
    return content


headers_reject_list = [
    "x-real-ip",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-port",
    "x-forwarded-host",
    "x-forwarded-server",
    "cf-warp-tag-id",
    "cf-visitor",
    "cf-ray",
    "cf-connecting-ip",
    "cf-ipcountry",
    "cdn-loop",
    "remote-host",
    "x-frame-options",
    "x-xss-protection",
    "x-content-type-options",
    "content-security-policy",
    "host",
    "cookie",
    "connection",
    "content-length",
    "content-encoding",
    "x-middleware-prefetch",
    "x-nextjs-data",
    "purpose",
    "x-forwarded-uri",
    "x-forwarded-path",
    "x-forwarded-method",
    "x-forwarded-protocol",
    "x-forwarded-scheme",
    "cf-request-id",
    "cf-worker",
    "cf-access-client-id",
    "cf-access-client-device-type",
    "cf-access-client-device-model",
    "cf-access-client-device-name",
    "cf-access-client-device-brand",
    "x-middleware-prefetch",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-server",
    "x-real-ip",
    "x-forwarded-port",
    "cf-connecting-ip",
    "cf-ipcountry",
    "cf-ray",
    "cf-visitor",
]

headers_accept_list = [
    "openai-sentinel-chat-requirements-token",
    "openai-sentinel-proof-token",
    "openai-sentinel-turnstile-token",
    "openai-sentinel-arkose-token",
    "x-conduit-token",
    "x-openai-target-path",
    "x-openai-target-route",
    "x-oai-is",
    "x-oai-turn-trace-id",
    "accept",
    "authorization",
    "accept-encoding",
    "accept-language",
    "content-type",
    "oai-device-id",
    "oai-session-id",
    "oai-client-version",
    "oai-client-build-number",
    "oai-echo-logs",
    "oai-language",
    "oai-telemetry",
    "sec-ch-ua",
    "sec-ch-ua-arch",
    "sec-ch-ua-bitness",
    "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-mobile",
    "sec-ch-ua-model",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
]


async def get_real_req_token(token):
    req_token = get_req_token(token)
    if len(req_token) == 45 or req_token.startswith("eyJhbGciOi"):
        return req_token
    else:
        req_token = get_req_token("", token)
        if not req_token:
            from utils import store
            try:
                registered = store.get_user_auth_by_seed(token, strict=True)
            except store.StoreError:
                raise HTTPException(503, 'Account allocation unavailable') from None
            if registered:
                raise HTTPException(503, 'No eligible account capacity available')
            raise HTTPException(401, 'No account available')
        return req_token


def resolve_seed_token(request: Request) -> str:
    """返回用户身份标识（SeedToken）：优先取 `token` cookie，回退到 Authorization 头。

    浏览器前端会把账号持有者抓取 logged_in HTML 时带出的 client-bootstrap JWT 塞进
    Authorization 头；若以它为准，所有用户会串号到同一账号。真正的用户身份是 `token` cookie。

    开发别名（``frontend-proof-*``）在这里一并被拒：HTML 入口的闸只挡浏览器，
    而本函数是每一条后端路由读取调用者身份的唯一入口（``gateway/account.py``、
    ``gateway/backend.py``、``gateway/f_conversation_gateway.py``、
    ``gateway/resource_proxy.py`` 都走这里）。别名是开发闸门专用的真实账号句柄，
    闸门关闭时必须在**读到身份的那一刻**就不存在，而不是等到页面或某一条链路去挡。
    """
    seed = request.cookies.get("token", "").strip()
    if not seed:
        seed = request.headers.get("authorization", "").replace("Bearer ", "").strip()
    reject_development_alias(seed)
    return seed


# curl_cffi 走 Clash 代理时偶发 SSL_ERROR_SYSCALL / connection reset（GFW 对新建 TLS 连接 RST）。
# 浏览器靠静默重试 + keep-alive 复用连接所以稳定；镜像网关注每次请求都新建连接，需对幂等请求做轻量重试。
_TRANSIENT_NETWORK_MARKERS = (
    "ssl_error_syscall",
    "ssl_connect",
    "connection closed",
    "connection reset",
    "connection aborted",
    "connection refused",
    "timed out",
    "timeout",
    "recv failure",
    "send failure",
    "broken pipe",
    "network is unreachable",
    "remote end closed",
    "eof",
)


def _is_transient_network_error(exc) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_NETWORK_MARKERS)


async def _request_with_retry(method, url, *, params, headers, cookies, data, max_attempts, client_factory):
    """发上游请求；幂等请求遇到瞬时网络错误（SSL reset / 连接重置）时重建 client 重试。

    成功返回 (response, client)，client 保持打开（供流式响应 / background 关闭）；
    失败时内部已关闭所有 client 并 re-raise 原始异常。
    """
    last_exc = None
    for attempt in range(max_attempts):
        client = client_factory()
        try:
            r = await client.request(method, url, params=params, headers=headers,
                                     cookies=cookies, data=data, stream=True, allow_redirects=False)
            return r, client
        except Exception as e:
            await client.discard()
            last_exc = e
            if attempt < max_attempts - 1 and _is_transient_network_error(e):
                logger.warning(
                    f"[retry] transient network error attempt={attempt + 1}/{max_attempts} kind={type(e).__name__}"
                )
                await asyncio.sleep(0.4 * (attempt + 1))
                continue
            raise
    raise last_exc


def save_conversation(token, conversation_id, title=None):
    entry = globals.seed_map.get(token)
    if not isinstance(entry, dict):
        return False
    conversations = entry.setdefault("conversations", [])
    if not isinstance(conversations, list):
        conversations = entry["conversations"] = list(conversations) if isinstance(conversations, (tuple, set)) else []
    if conversation_id not in globals.conversation_map:
        conversation_detail = {
            "id": conversation_id,
            "title": title,
            "create_time": generate_current_time(),
            "update_time": generate_current_time(),
            # 会话历史跟号走：记录创建时的账号，切号后列表按当前账号过滤
            "account": globals.seed_map.get(token, {}).get("token", ""),
        }
        globals.conversation_map[conversation_id] = conversation_detail
    else:
        globals.conversation_map[conversation_id]["update_time"] = generate_current_time()
        if title:
            globals.conversation_map[conversation_id]["title"] = title
    if conversation_id not in conversations:
        conversations.insert(0, conversation_id)
    else:
        conversations.remove(conversation_id)
        conversations.insert(0, conversation_id)
    globals.persist_conversation(token, conversation_id)
    globals.persist_seed(token)
    # 会话内容已变（新消息），失效该会话详情缓存，避免后续 GET 命中旧内容
    resp_cache.invalidate_path_prefix(f"backend-api/conversation/{conversation_id}")
    if title:
        logger.info(f"Conversation ID: {conversation_id}, Title: {title}")
    return True


def _conversation_fields(payload):
    """从一条已解析的 SSE data 载荷里取出 (conversation_id, title)。

    上游有两种携带方式：首个事件的顶层键，以及 delta 事件 `v` 字典里的同名键。
    """
    conversation_id = payload.get("conversation_id")
    title = payload.get("title")
    v = payload.get("v")
    if isinstance(v, dict):
        conversation_id = conversation_id or v.get("conversation_id")
        title = title or v.get("title")
    return conversation_id, title


async def content_generator(r, token, history=True):
    """透传上游流，顺带记录会话 id/title。

    按 SSE 事件边界重组后再解析：raw chunk 可能被 TCP 任意切分（半个事件、
    粘包、UTF-8 多字节中间断开），按 chunk 前缀匹配会漏记或误记。事件本身原样
    下发（注释、[DONE]、EOF 处的残缺事件都不改动）。
    """
    conversation_id = None
    title = None
    track = history and len(token) != 45 and not token.startswith("eyJhbGciOi")
    async for event in iter_sse_events_async(r.aiter_content()):
        if track and (not conversation_id or not title):
            try:
                # 每个事件只解析一次，结果同时供顶层与 v 字典两种形态取值
                payload = extract_data_json(event)
                if payload is not None:
                    event_cid, event_title = _conversation_fields(payload)
                    if event_cid and not conversation_id:
                        conversation_id = event_cid
                        save_conversation(token, conversation_id)
                        title = (globals.conversation_map.get(conversation_id) or {}).get("title")
                    # 没有 conversation_id 就落库会写出无主记录，必须先等到 id
                    if event_title and not title and conversation_id:
                        title = event_title
                        save_conversation(token, conversation_id, title)
            except Exception:
                # Do not let bookkeeping break the upstream stream, but make a
                # failed ownership record observable without exposing IDs/tokens.
                logger.warning(
                    f"[conversation] record_failed seed={hashlib.sha256(str(token).encode()).hexdigest()[:12]} "
                    "reason=bookkeeping_error"
                )
        yield event


@generation_lifetime
async def chatgpt_reverse_proxy(request: Request, path: str):
    try:
        origin_host = request.url.netloc
        if request.url.is_secure:
            petrol = "https"
        else:
            petrol = "http"
        if "x-forwarded-proto" in request.headers:
            petrol = request.headers["x-forwarded-proto"]
        if "cf-visitor" in request.headers:
            cf_visitor = json.loads(request.headers["cf-visitor"])
            petrol = cf_visitor.get("scheme", petrol)

        params = dict(request.query_params)
        request_cookies = {}
        # 静态资源（cdn/assets）走匿名公网 CDN，携带凭据反而触发 CDN 鉴权 403
        is_static_asset = "cdn/" in path or "assets/" in path
        # 注入账号持有者的 session cookie（__Secure-next-auth.session-token / cf_clearance / oai-did），
        # 镜像用户浏览器没有这些 cookie，需由网关注入，chatgpt.com 才能正确认证
        # 例外：estuary/content 等 sig 签名端点，sig 本身已自足；注入 session cookie 会让上游
        # 拿签名与会话做一致性校验而冲突，返回 500（实测不带 cookie 时返回 200 image/png）。
        # 例外：静态资源也不注入——CDN 对带 session cookie 的请求直接 403。

        # headers = {
        #     key: value for key, value in request.headers.items()
        #     if (key.lower() not in ["host", "origin", "referer", "priority",
        #                             "oai-device-id"] and key.lower() not in headers_reject_list)
        # }
        headers = {
            key: value for key, value in request.headers.items()
            if (key.lower() in headers_accept_list)
        }

        base_url = random.choice(chatgpt_base_url_list) if chatgpt_base_url_list else "https://chatgpt.com"
        context = None
        if is_static_asset:
            headers.pop('authorization', None)
            base_url = "https://cdn.oaistatic.com"
            # 官网新版前端资源路径带 /cdn/ 前缀（/cdn/assets/xxx），cdn 上实际是 /assets/xxx
            path = path.replace("cdn/", "", 1)
        if "file-" in path and "backend-api" not in path:
            base_url = "https://files.oaiusercontent.com"
        if "v1/" in path:
            base_url = "https://ab.chatgpt.com"
        if "sandbox" in path:
            base_url = "https://web-sandbox.oaiusercontent.com"
            path = path.replace("sandbox/", "")

        # 会话隔离：账号身份以 `token` cookie（SeedToken）为准，而非浏览器 Authorization 头里的
        # client-bootstrap JWT（那是账号持有者抓 HTML 时泄漏的凭据，会导致所有用户串号到同一账号）。
        # 静态资源早退：跳过 seed 解析与 Authorization 注入，匿名直连 CDN
        # 原始 seed cookie（不含 Authorization 回退）：用于判断「镜像用户 vs 直连 API 客户端」，
        # 决定 catch-all JSON 是否做身份脱敏。
        if is_static_asset:
            seed_token = ""
            seed_cookie = request.cookies.get("token", "").strip()
            req_token = ""
            access_token = None
        else:
            seed_token = resolve_seed_token(request)
            seed_cookie = request.cookies.get("token", "").strip()
            enforce_tier(seed_token)
            req_token = await get_real_req_token(seed_token)
            if request.method == "POST" and path in ("backend-api/conversation", "backend-alt/conversation"):
                await admit_generation(request, req_token, seed_token)
            access_token = await verify_token(req_token)
            try:
                context = await refresh_cached_frontend(req_token, access_token or '', get_fp(req_token).copy())
            except FrontendSessionError:
                raise HTTPException(status_code=503, detail='Account website session unavailable') from None
            if context:
                access_token = context['session']['accessToken']
            if "estuary" not in path and base_url == 'https://chatgpt.com':
                request_cookies = get_session_cookie(req_token, access_token or '')
            if access_token:
                headers.update({"authorization": f"Bearer {access_token}"})
                account_id = decode_jwt_payload(access_token).get('https://api.openai.com/auth', {}).get('chatgpt_account_id')
                if account_id:
                    headers['chatgpt-account-id'] = account_id
                    if seed_cookie and path == 'backend-api/subscriptions':
                        params['account_id'] = account_id
        fp = get_fp(req_token).copy()

        session_id = hashlib.md5(req_token.encode()).hexdigest()

        proxy_url = fp.pop("proxy_url", None)
        impersonate = fp.pop("impersonate", "safari15_3")
        user_agent = fp.get("user-agent")
        # 只允许 HTTP 头白名单字段出网。fp 记录同时承载 antiban 浏览器画像
        # （screen/viewport/webgl 等嵌套结构）与路由元数据，整条 update 进去会让
        # curl_cffi 编码 dict 头抛 AttributeError → 502（准入之后组头的那一轮必崩）。
        headers.update(extract_header_fp(fp))

        headers.update({
            "accept-language": accept_language,
            "host": base_url.replace("https://", "").replace("http://", ""),
            "origin": base_url,
            "referer": f"{base_url}/"
        })
        if "v1/initialize" in path:
            headers.update({"user-agent": request.headers.get("user-agent")})
            if "statsig-api-key" not in headers:
                headers.update({
                    "statsig-sdk-type": "js-client",
                    "statsig-api-key": "client-tnE5GCU2F2cTxRiMbvTczMDT1jpwIigZHsZSdqiy4u",
                    "statsig-sdk-version": "5.1.0",
                    "statsig-client-time": int(time.time() * 1000),
                })

        data = await request.body()

        history = True
        if path.endswith("backend-api/conversation") or path.endswith("backend-alt/conversation"):
            try:
                req_json = json.loads(data)
                history = not req_json.get("history_and_training_disabled", False)
            except Exception:
                pass
            if force_no_history:
                history = False
                req_json = json.loads(data)
                req_json["history_and_training_disabled"] = True
                data = json.dumps(req_json).encode("utf-8")

        # 档位执行（Stage 2）：对 SaaS 注册用户（有 user_auth 行）执行模型门禁 + 额度上限。
        # 运营者 seed / 直传 token 无 user_auth 行 → fail-open 不设限。超限抛 403/429 向上返回。
        _chat_model = None
        if path.endswith("backend-api/conversation") or path.endswith("backend-alt/conversation"):
            try:
                _chat_model = (json.loads(data) or {}).get("model")
            except Exception:
                _chat_model = None
        enforce_tier(seed_token, _chat_model)

        if "backend-api/sentinel/chat-requirements" in path and sentinel_proxy_url_list:
            sentinel_proxy_url = random.choice(sentinel_proxy_url_list).replace("{}", session_id) if sentinel_proxy_url_list else None

            def _make_client():
                client = Client(proxy=sentinel_proxy_url)
                track_generation_client(request, client)
                return client
        else:
            proxy_url = proxy_url.replace("{}", session_id) if proxy_url else None

            def _make_client():
                client = Client(proxy=proxy_url, impersonate=impersonate)
                track_generation_client(request, client)
                return client

        # 幂等请求（GET/HEAD/OPTIONS）遇到瞬时 SSL/连接重置自动重试，消除偶发 502
        # （如 GPT 图片 estuary/content、会话轮询 api/auth/session）
        max_attempts = 3 if request.method.upper() in ("GET", "HEAD", "OPTIONS") else 1
        # 慢上游 GET 响应缓存：models / accounts/check / conversation/{id} 短期内不变，
        # 命中直接返回，省掉经代理节点的上游往返（页面加载 / 按钮渲染慢的根因）。
        cache_ttl = resp_cache.cacheable(path, request.method)
        cache_key = None
        if cache_ttl:
            cache_key = (req_token, path, tuple(sorted(params.items())),
                         context['revision'] if context else '')
            cached = resp_cache.get(cache_key)
            if cached is not None:
                content = _rewrite_and_scrub(
                    cached["content"], path=path, base_url=base_url, petrol=petrol,
                    origin_host=origin_host, seed_cookie=seed_cookie,
                    content_type=cached["rheaders"].get("content-type", ""),
                )
                out_headers = {
                    "cache-control": cached["rheaders"].get("cache-control", ""),
                    "content-type": cached["rheaders"].get("content-type", ""),
                    "expires": cached["rheaders"].get("expires", ""),
                    "content-disposition": cached["rheaders"].get("content-disposition", ""),
                }
                return Response(content=content, headers=out_headers, status_code=cached["status"])
        client = None
        try:
            r, client = await _request_with_retry(
                request.method, f"{base_url}/{path}", params=params, headers=headers,
                cookies=request_cookies, data=data, max_attempts=max_attempts, client_factory=_make_client,
            )
            track_generation_client(request, client)
            # 用量统计：成功发起上游请求后计数（内存），周期 flush 落库
            record_usage(seed_token, req_token, _usage_kind(path))
            background = BackgroundTask(client.close)
            if r.status_code == 307 or r.status_code == 302 or r.status_code == 301:
                return Response(status_code=307,
                                headers={"Location": r.headers.get("Location")
                                .replace("ab.chatgpt.com", origin_host)
                                .replace("chatgpt.com", origin_host)
                                .replace("cdn.oaistatic.com", origin_host)
                                .replace("https", petrol)}, background=background)
            elif 'stream' in r.headers.get("content-type", ""):
                logger.info(f"Request UA: {user_agent}")
                logger.info(f"Request impersonate: {impersonate}")
                conv_key = r.cookies.get("conv_key", "")
                # Only a generation response can establish a new conversation.
                # A status/polling stream must not grant ownership from its data.
                track_history = history and request.method == "POST" and path in (
                    "backend-api/conversation", "backend-alt/conversation")
                response = StreamingResponse(observe_generation_stream(request, content_generator(r, seed_token, track_history)), status_code=r.status_code,
                                  media_type=r.headers.get("content-type", ""),
                                  background=None if request.state.generation_admission is not None else background)
                response.set_cookie("conv_key", value=conv_key)
                return response
            elif 'image' in r.headers.get("content-type", "") or "audio" in r.headers.get("content-type", "") or "video" in r.headers.get("content-type", ""):
                # curl_cffi 的 acontent() 会自动解压 gzip/br，需剥离原 content-encoding，
                # 否则浏览器拿到明文又按 br/gzip 解码，报 ERR_CONTENT_DECODING_FAILED
                rheaders = {k: v for k, v in r.headers.items()
                            if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
                response = Response(content=await r.acontent(), headers=rheaders,
                                        status_code=r.status_code, background=background)
                return response
            else:
                if path.endswith("backend-api/conversation") or path.endswith("backend-alt/conversation") or "/register-websocket" in path:
                    response = Response(content=(await r.acontent()), media_type=r.headers.get("content-type"),
                                        status_code=r.status_code, background=background)
                else:
                    content = await r.atext()
                    # 缓存慢上游 GET 的原始 body（改写/脱敏前的原文），命中时走同一条 _rewrite_and_scrub。
                    # 仅缓存 200 + 文本响应：错误状态码（403/429/500）不得污染缓存，二进制不得被 atext 破坏。
                    if cache_key and r.status_code == 200 and _is_textual_content(r.headers.get("content-type", "")):
                        resp_cache.set(cache_key, {
                            "content": content,
                            "status": r.status_code,
                            # 归一化小写键：命中路径用 rheaders.get("content-type") 取 content-type 决定是否脱敏，
                            # 键大小写是 curl_cffi 库行为而非契约，显式归一化避免升级后静默跳过脱敏
                            "rheaders": {k.lower(): v for k, v in r.headers.items()},
                        }, cache_ttl)
                    content = _rewrite_and_scrub(
                        content, path=path, base_url=base_url, petrol=petrol,
                        origin_host=origin_host, seed_cookie=seed_cookie,
                        content_type=r.headers.get("content-type", ""),
                    )
                    rheaders = dict(r.headers)
                    rheaders = {
                        "cache-control": rheaders.get("cache-control", ""),
                        "content-type": rheaders.get("content-type", ""),
                        "expires": rheaders.get("expires", ""),
                        "content-disposition": rheaders.get("content-disposition", ""),
                    }
                    response = Response(content=content, headers=rheaders,
                                        status_code=r.status_code, background=background)
                return response
        except HTTPException as e:
            if client is not None:
                await client.close()
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        except Exception as e:
            if client is not None:
                await client.close()
            logger.error(f"Reverse proxy failed: {type(e).__name__}")
            raise HTTPException(status_code=502, detail="Upstream request failed")
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail="Gateway request failed") from None
