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
from chatgpt.authorization import verify_token, get_req_token
from chatgpt.fp import get_fp
from utils.Client import Client
from utils.Logger import logger
from utils.configs import chatgpt_base_url_list, sentinel_proxy_url_list, force_no_history, file_host, voice_host, accept_language
from gateway.frontend_sync import get_session_cookie


def generate_current_time():
    current_time = datetime.now(timezone.utc)
    formatted_time = current_time.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    return formatted_time


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
        return req_token


def resolve_seed_token(request: Request) -> str:
    """返回用户身份标识（SeedToken）：优先取 `token` cookie，回退到 Authorization 头。

    浏览器前端会把账号持有者抓取 logged_in HTML 时带出的 client-bootstrap JWT 塞进
    Authorization 头；若以它为准，所有用户会串号到同一账号。真正的用户身份是 `token` cookie。
    """
    seed = request.cookies.get("token", "").strip()
    if not seed:
        seed = request.headers.get("authorization", "").replace("Bearer ", "").strip()
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
            await client.close()
            last_exc = e
            if attempt < max_attempts - 1 and _is_transient_network_error(e):
                logger.warning(
                    f"[retry] transient network error ({attempt + 1}/{max_attempts}) for {url}: {str(e)[:120]}"
                )
                await asyncio.sleep(0.4 * (attempt + 1))
                continue
            raise
    raise last_exc


def save_conversation(token, conversation_id, title=None):
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
    if conversation_id not in globals.seed_map[token]["conversations"]:
        globals.seed_map[token]["conversations"].insert(0, conversation_id)
    else:
        globals.seed_map[token]["conversations"].remove(conversation_id)
        globals.seed_map[token]["conversations"].insert(0, conversation_id)
    globals.persist_conversation(token, conversation_id)
    globals.persist_seed_map()
    if title:
        logger.info(f"Conversation ID: {conversation_id}, Title: {title}")


async def content_generator(r, token, history=True):
    conversation_id = None
    title = None
    async for chunk in r.aiter_content():
        try:
            if history and (len(token) != 45 and not token.startswith("eyJhbGciOi")) and (not conversation_id or not title):
                chat_chunk = chunk.decode('utf-8')
                if not conversation_id or not title and chat_chunk.startswith("event: delta\n\ndata: {"):
                    chunk_data = chat_chunk[19:]
                    conversation_id = json.loads(chunk_data).get("v").get("conversation_id")
                    if conversation_id:
                        save_conversation(token, conversation_id)
                        title = globals.conversation_map[conversation_id].get("title")
                if chat_chunk.startswith("data: {"):
                    if "\n\nevent: delta" in chat_chunk:
                        index = chat_chunk.find("\n\nevent: delta")
                        chunk_data = chat_chunk[6:index]
                    elif "\n\ndata: {" in chat_chunk:
                        index = chat_chunk.find("\n\ndata: {")
                        chunk_data = chat_chunk[6:index]
                    else:
                        chunk_data = chat_chunk[6:]
                    chunk_data = chunk_data.strip()
                    if conversation_id is None:
                        conversation_id = json.loads(chunk_data).get("conversation_id")
                        if conversation_id:
                            save_conversation(token, conversation_id)
                            title = globals.conversation_map[conversation_id].get("title")
                    if title is None:
                        title = json.loads(chunk_data).get("title")
                        if title:
                            save_conversation(token, conversation_id, title)
        except Exception as e:
            # logger.error(e)
            # logger.error(chunk.decode('utf-8'))
            pass
        yield chunk


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
        request_cookies = dict(request.cookies)
        # 注入账号持有者的 session cookie（__Secure-next-auth.session-token / cf_clearance / oai-did），
        # 镜像用户浏览器没有这些 cookie，需由网关注入，chatgpt.com 才能正确认证
        # 例外：estuary/content 等 sig 签名端点，sig 本身已自足；注入 session cookie 会让上游
        # 拿签名与会话做一致性校验而冲突，返回 500（实测不带 cookie 时返回 200 image/png）。
        if "estuary" not in path:
            try:
                for _k, _v in (p.split("=", 1) for p in get_session_cookie().split("; ") if "=" in p):
                    request_cookies[_k] = _v
            except Exception:
                pass

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
        if "cdn/" in path or "assets/" in path:
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
        seed_token = resolve_seed_token(request)
        req_token = await get_real_req_token(seed_token)
        access_token = await verify_token(req_token)
        if access_token:
            headers.update({"authorization": f"Bearer {access_token}"})
        fp = get_fp(req_token).copy()

        session_id = hashlib.md5(req_token.encode()).hexdigest()

        proxy_url = fp.pop("proxy_url", None)
        impersonate = fp.pop("impersonate", "safari15_3")
        user_agent = fp.get("user-agent")
        headers.update(fp)

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


        if "backend-api/sentinel/chat-requirements" in path and sentinel_proxy_url_list:
            sentinel_proxy_url = random.choice(sentinel_proxy_url_list).replace("{}", session_id) if sentinel_proxy_url_list else None

            def _make_client():
                return Client(proxy=sentinel_proxy_url)
        else:
            proxy_url = proxy_url.replace("{}", session_id) if proxy_url else None

            def _make_client():
                return Client(proxy=proxy_url, impersonate=impersonate)

        # 幂等请求（GET/HEAD/OPTIONS）遇到瞬时 SSL/连接重置自动重试，消除偶发 502
        # （如 GPT 图片 estuary/content、会话轮询 api/auth/session）
        max_attempts = 3 if request.method.upper() in ("GET", "HEAD", "OPTIONS") else 1
        client = None
        try:
            r, client = await _request_with_retry(
                request.method, f"{base_url}/{path}", params=params, headers=headers,
                cookies=request_cookies, data=data, max_attempts=max_attempts, client_factory=_make_client,
            )
            background = BackgroundTask(client.close)
            if r.status_code == 307 or r.status_code == 302 or r.status_code == 301:
                return Response(status_code=307,
                                headers={"Location": r.headers.get("Location")
                                .replace("ab.chatgpt.com", origin_host)
                                .replace("chatgpt.com", origin_host)
                                .replace("cdn.oaistatic.com", origin_host)
                                .replace("https", petrol)}, background=background)
            elif 'stream' in r.headers.get("content-type", ""):
                logger.info(f"Request token: {req_token}")
                logger.info(f"Request proxy: {proxy_url}")
                logger.info(f"Request UA: {user_agent}")
                logger.info(f"Request impersonate: {impersonate}")
                conv_key = r.cookies.get("conv_key", "")
                response = StreamingResponse(content_generator(r, seed_token, history), media_type=r.headers.get("content-type", ""),
                                  background=background)
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
                    rheaders = dict(r.headers)
                    content_type = rheaders.get("content-type", "")
                    cache_control = rheaders.get("cache-control", "")
                    expires = rheaders.get("expires", "")
                    content_disposition = rheaders.get("content-disposition", "")
                    rheaders = {
                        "cache-control": cache_control,
                        "content-type": content_type,
                        "expires": expires,
                        "content-disposition": content_disposition
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
            logger.error(f"Reverse proxy failed for {path}: {str(e)}")
            raise HTTPException(status_code=502, detail="Upstream request failed")
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
