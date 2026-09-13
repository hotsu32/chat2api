"""f/conversation 服务端 sentinel 处理。

官网新版前端用 f/conversation 接口（sentinel 灰度），前端算的 PoW/turnstile
绑定前端浏览器 IP，经 gateway 反代后 IP 变化会触发风控。这里拦截新接口，
由服务端统一算 sentinel + PoW，转发到老接口 /backend-api/conversation。
"""
import asyncio
import copy
import hashlib
import json
import random
import time
import uuid
import anyio

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app import app
from chatgpt.authorization import verify_token
from chatgpt.fp import extract_header_fp, get_fp
from chatgpt.proofofWork import get_config, get_answer_token, get_requirements_token
from gateway.frontend_sync import (
    FrontendSessionError,
    get_session_cookie,
    refresh_cached_frontend,
)
from gateway.identity import decode_jwt_payload
from gateway.reverseProxy import (
    _is_transient_network_error,
    content_generator,
    headers_accept_list,
    resolve_seed_token,
)
from gateway.sse_parser import extract_data_json
from gateway.research_progress import (
    detection_summary,
    heartbeat_interval,
    is_research_turn,
    store as research_progress_store,
    stream_timeout_for,
    with_heartbeat,
)
from gateway.conversation_scope import conversation_is_foreign
from utils.tiers import enforce_tier
from gateway.generation import admit_generation, generation_lifetime, track_generation_client
from gateway.generation import observe_generation_end, observe_generation_event
from utils.Client import Client
from utils.Logger import logger
from utils.configs import (
    accept_language,
    pick_chatgpt_base_url,
    UpstreamNotConfigured,
    chat_request_timeout,
    turnstile_solver_url,
    sentinel_proxy_url_list,
    sentinel_cache_ttl,
    sentinel_strict,
)

# 服务端 sentinel 缓存：req_token -> {chat_token, proof_token, turnstile_token}
_sentinel_cache = {}
_sentinel_cookie_cache = {}

# curl_cffi 自动解压上游 body，透传上游的 content-encoding 会让浏览器把明文
# 再按 gzip/br 解一次，直接 ERR_CONTENT_DECODING_FAILED（回复永远渲染不出来）。
# 长度/分帧同理由网关自己重算。
_HOP_BY_HOP_RESPONSE_HEADERS = ("content-encoding", "content-length", "transfer-encoding")


def _anon(value: str) -> str:
    """日志用的稳定匿名标识：够区分账号/会话，但不可反推凭据。"""
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else "-"


def _host(base_url: str) -> str:
    return base_url.replace("https://", "").replace("http://", "")


def _sanitize_response_headers(rheaders):
    return {k: v for k, v in rheaders.items()
            if k.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS}


async def _upstream_context(request, req_token, host_url):
    """构造 f/conversation 上游请求的完整身份：头 + 指纹 + 账号官网 cookie。

    与 ``chatgpt_reverse_proxy`` 同一套规则——这条链路绕开了通用反代，若只透传
    浏览器头，上游收到的是「镜像用户的浏览器 + 无账号归属」的请求：
      * 缺 chatgpt-account-id，Plus/Pro 的工作区权益无法归属，turn 被拒；
      * 缺账号官网 cookie，chatgpt.com 认不出这是该账号的会话；
      * origin/referer 还指向镜像域名，等于对 chatgpt.com 发跨站 POST。

    返回 (headers, fp, cookies, access_token)。account 不可解析时 fail-closed 抛 401。
    """
    if not req_token:
        raise HTTPException(status_code=401, detail="No account available")
    access_token = await verify_token(req_token)
    fp = get_fp(req_token).copy()
    try:
        context = await refresh_cached_frontend(req_token, access_token or "", dict(fp))
    except FrontendSessionError:
        raise HTTPException(status_code=503, detail="Account website session unavailable") from None
    if context:
        access_token = context["session"]["accessToken"]
    if not access_token:
        raise HTTPException(status_code=401, detail="No account available")

    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() in headers_accept_list
    }
    headers.update(extract_header_fp(fp))
    headers.update({
        "authorization": f"Bearer {access_token}",
        "accept-language": accept_language,
        "host": _host(host_url),
        "origin": host_url,
        "referer": f"{host_url}/",
    })
    account_id = decode_jwt_payload(access_token).get(
        "https://api.openai.com/auth", {}).get("chatgpt_account_id")
    if account_id:
        headers["chatgpt-account-id"] = account_id

    # 账号自己的官网 session cookie（由网关注入，浏览器侧没有也不该有）。
    # 绝不透传 request.cookies：那是镜像用户的浏览器 jar（含我们自己的 seed token）。
    cookies = dict(get_session_cookie(req_token, access_token))
    for cookie in _sentinel_cookie_cache.get(req_token, []):
        cookies[cookie["name"]] = cookie["value"]
    return headers, fp, cookies, access_token


async def _server_sentinel(request, access_token, req_token, headers, fp, cookies=None):
    """服务端调 sentinel chat-requirements，返回 (chat_token, proof_token, turnstile_token, client, clients, session_id, user_agent)。"""
    # 显式空上游 = 本地 503，先于构造任何客户端：sentinel 请求带账号凭据，不能发给
    # 一个没配置过的默认站点。
    try:
        host_url = pick_chatgpt_base_url()
    except UpstreamNotConfigured:
        raise HTTPException(status_code=503, detail="upstream not configured") from None
    user_agent = fp.get(
        "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    )
    proxy_url = fp.pop("proxy_url", None)
    impersonate = fp.pop("impersonate", "safari15_3")

    session_id = hashlib.md5(req_token.encode()).hexdigest()
    proxy_url = proxy_url.replace("{}", session_id) if proxy_url else None
    # 与 f_conversation 的 conversation client 使用相同 timeout，使两者命中同一连接池 key：
    # prepare 阶段的 sentinel 请求建立起的 TCP+TLS 连接，能被随后 f/conversation 直接复用（preconnect）。
    client = Client(proxy=proxy_url, impersonate=impersonate, timeout=chat_request_timeout)
    sentinel_proxy_url = None
    if sentinel_proxy_url_list:
        sentinel_proxy_url = (
            random.choice(sentinel_proxy_url_list).replace("{}", session_id)
            if sentinel_proxy_url_list else None
        )
        clients = Client(proxy=sentinel_proxy_url, impersonate=impersonate)
    else:
        clients = client

    proof_token = None
    turnstile_token = None
    try:
        config = get_config(user_agent, session_id)
        p = get_requirements_token(config)
        data = {"p": p}
        # sentinel 请求也做瞬时网络错误重试：GFW 对新建 TLS 连接偶发 RST（SSL_ERROR_SYSCALL），
        # 与 reverseProxy._request_with_retry 同因；不重试会空 sentinel 级联 422。
        for attempt in range(2):
            try:
                r = await clients.post(
                    f"{host_url}/backend-api/sentinel/chat-requirements",
                    headers=headers, cookies=dict(cookies or {}), json=data, timeout=10,
                )
                break
            except Exception as e:
                if attempt == 0 and _is_transient_network_error(e):
                    logger.warning(f"[f_conversation] sentinel transient network error, retrying: {type(e).__name__}")
                    await clients.discard()
                    if sentinel_proxy_url_list:
                        clients = Client(proxy=sentinel_proxy_url, impersonate=impersonate)
                    else:
                        client = clients = Client(proxy=proxy_url, impersonate=impersonate, timeout=chat_request_timeout)
                    await asyncio.sleep(0.4)
                    continue
                raise
        oai_sc = r.cookies.get("oai-sc")
        if oai_sc:
            _sentinel_cookie_cache[req_token] = [{"name": "oai-sc", "value": oai_sc}]
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Failed to get chat requirements")
        resp = r.json()

        turnstile = resp.get("turnstile", {})
        if turnstile.get("required"):
            turnstile_dx = turnstile.get("dx")
            try:
                if turnstile_solver_url:
                    res = await client.post(
                        turnstile_solver_url,
                        json={"url": "https://chatgpt.com", "p": p, "dx": turnstile_dx, "ua": user_agent},
                    )
                    turnstile_token = res.json().get("t")
            except Exception as e:
                logger.info(f"Turnstile unavailable: {type(e).__name__}")

        proofofwork = resp.get("proofofwork", {})
        if proofofwork.get("required"):
            proof_token, solved = await run_in_threadpool(
                get_answer_token,
                proofofwork.get("seed"),
                proofofwork.get("difficulty"),
                config,
            )
            if not solved:
                raise HTTPException(status_code=403, detail="Failed to solve proof of work")
        chat_token = resp.get("token")
        return chat_token, proof_token, turnstile_token, client, clients, session_id, user_agent
    except Exception as e:
        logger.error(f"[f_conversation] server sentinel failed: {type(e).__name__}")
        await client.close()
        await clients.close()
        raise


@app.post("/backend-api/sentinel/chat-requirements/prepare")
async def f_sentinel_prepare(request: Request):
    """拦截 sentinel prepare：服务端算 sentinel，返回假 prepare_token（让前端跳过 PoW）。"""
    token = resolve_seed_token(request)
    req_token = await get_real_req_token_wrapper(token)
    # 显式空上游 = 本地 503：上游 origin/host 头与 sentinel 都依赖它，不能先干活再猜。
    try:
        host_url = pick_chatgpt_base_url()
    except UpstreamNotConfigured:
        raise HTTPException(status_code=503, detail="upstream not configured") from None
    headers, fp, cookies, access_token = await _upstream_context(request, req_token, host_url)
    try:
        chat_token, proof_token, turnstile_token, client, clients, _, _ = await _server_sentinel(
            request, access_token, req_token, headers, fp, cookies
        )
        _sentinel_cache[req_token] = {
            "chat_token": chat_token,
            "proof_token": proof_token,
            "turnstile_token": turnstile_token,
            "cached_at": time.time(),
        }
        await client.close()
        await clients.close()
    except HTTPException:
        if sentinel_strict:
            raise
        # 降级：PoW 解算失败 / 风控拒（HTTPException 403/429 等）不再硬失败透传给前端。
        # 不写缓存，f/conversation 命中空缓存时会走 sentinel={} 降级继续，与 f/conversation
        # 现有降级语义对齐。SENTINEL_STRICT=1 可恢复旧行为做调试对照。
        logger.warning("[f_sentinel_prepare] server sentinel failed, degrading (SENTINEL_STRICT=off)")
    except Exception:
        pass
    return {
        "prepare_token": str(uuid.uuid4()),
        "proofofwork": {"required": False, "difficulty": None, "seed": None},
        "turnstile": {"required": False, "dx": None},
        "arkose": {"required": False, "dx": None},
    }


@app.post("/backend-api/sentinel/chat-requirements/finalize")
async def f_sentinel_finalize(request: Request):
    """拦截 sentinel finalize：返回假 chat_token。"""
    return {"token": str(uuid.uuid4())}


@app.post("/backend-api/f/conversation/prepare")
async def f_conversation_prepare(request: Request):
    """拦截 f/conversation/prepare：返回假 conduit_token。"""
    started = time.monotonic()
    token = str(uuid.uuid4())
    logger.info(f"[f_conversation_prepare] status=200 response=json bytes={len(token) + 21} "
                f"elapsed_ms={int((time.monotonic() - started) * 1000)}")
    return {"conduit_token": token}


_F_ONLY_FIELDS = (
    "client_prepare_state", "supports_buffering", "enable_message_followups",
    "force_parallel_switch", "local_function_names",
    "paragen_cot_summary_display_override",
)


def rewrite_f_conversation_body(body: dict) -> dict:
    """把 f/conversation 的请求体改写成老接口 /backend-api/conversation 格式。

    不修改入参（调用方随后还要读 body["model"] 做档位执行），返回新 dict。

    关键：``supported_encodings`` 必须原样透传。新版官网前端 POST f/conversation
    时协商的是增量编码（``{"p": ..., "o": "append", "v": ...}``），它的渲染器只有
    这条增量路径；一旦网关把该字段清空，上游改发整条 message 快照，前端就没有可
    追加的东西，只能在终态 reconcile 一次——浏览器实测正是「流式 18 帧、DOM 直到
    终态后才一次性出现全文」。证据见
    tmp/agent-team/account-frontend/m2-streaming/gen-plus-shapes.json。
    """
    out = copy.deepcopy(body)
    for _k in _F_ONLY_FIELDS:
        out.pop(_k, None)
    if out.get("parent_message_id") == "client-created-root":
        out["parent_message_id"] = str(uuid.uuid4())
    out["websocket_request_id"] = str(uuid.uuid4())
    out.setdefault("force_paragen", False)
    out.setdefault("force_rate_limit", False)
    out.setdefault("reset_rate_limits", False)
    out.setdefault("suggestions", [])
    # 清理 messages 里的 f/ 特有字段，对齐老接口 conversation 格式
    for _m in out.get("messages", []):
        _m.pop("create_time", None)
        _md = _m.get("metadata") or {}
        _md.pop("submission_mode", None)
        _md.pop("serialization_metadata", None)
        _m["metadata"] = _md
    return out


def is_legacy_echo_event(payload: dict) -> bool:
    """Whether a parsed upstream event is a legacy-endpoint artefact to drop.

    The gateway forwards f/conversation to the legacy /backend-api/conversation,
    which prefixes the stream with a resume token and echoes back the user and
    system turns.  The new frontend does not expect either, so they are dropped
    here; everything else is forwarded untouched.

    Every field is type-checked before it is destructured.  ``type`` and
    ``message`` are upstream-controlled, and a frame only has to be a legal JSON
    object to reach this point — ``{"message": null}``, ``{"message": "..."}``
    and ``{"message": {"author": null}}`` all are.  The previous version chained
    ``.get()`` through those positions, so such a frame raised AttributeError
    inside the streaming body.  That does not merely mis-classify one event: the
    exception propagates out of the generator and tears down the whole
    StreamingResponse, so the browser loses every later event *and* the terminal
    one, mid-turn, with the response already committed as 200 (measured: a
    stream of 8 events delivered 3, no terminal, no [DONE]).  A Deep Research
    turn is exactly the case that cannot absorb that — its value is the long
    tail of intermediate progress events.

    Classification is therefore fail-open: an event whose shape is not
    recognised is forwarded rather than dropped or fatal.  That matches the
    project rule that unknown events must not be silently discarded, and keeps
    the drop set to events positively identified as legacy echoes -- the
    account-isolation filters below are unchanged.
    """
    if payload.get("type", "message") == "resume_conversation_token":
        return True
    if payload.get("type", "message") != "message":
        return False
    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    author = message.get("author")
    if not isinstance(author, dict):
        return False
    return author.get("role") in ("user", "system")


@app.post("/backend-api/f/conversation")
@generation_lifetime
async def f_conversation(request: Request):
    """拦截 f/conversation：服务端 sentinel + 转发老接口 /backend-api/conversation。"""
    started = time.monotonic()
    token = resolve_seed_token(request)
    # 权益不足先拒绝，避免为已到期请求刷新官网会话或发起上游预检。
    enforce_tier(token)
    req_token = await get_real_req_token_wrapper(token)
    # 显式空上游 = 本地 503，先于试用额度预约与官网会话刷新：没有目标就没有要送的对象，
    # 不该为一个注定失败的一轮占掉额度或刷新凭据。
    try:
        host_url = pick_chatgpt_base_url()
    except UpstreamNotConfigured:
        raise HTTPException(status_code=503, detail="upstream not configured") from None
    await admit_generation(request, req_token, token)
    # 匿名阶段日志：只打可稳定关联同一账号/会话的哈希前缀，不落 token / cookie / 代理地址。
    logger.info(f"[f_conversation] phase=received seed={_anon(token)} account={_anon(req_token)}")

    headers, fp, request_cookies, access_token = await _upstream_context(request, req_token, host_url)

    proxy_url = fp.pop("proxy_url", None)
    impersonate = fp.pop("impersonate", "safari15_3")

    session_id = hashlib.md5(req_token.encode()).hexdigest()
    proxy_url = proxy_url.replace("{}", session_id) if proxy_url else None
    # 上游静默预算：stream=True 时 curl_cffi 把标量 timeout 映射成低速看门狗，
    # 量到的是「上游能安静多久」而不是「这一轮能跑多久」（见 utils/configs.py 与
    # tmp/research-protocol/OFFICIAL_INTERFACE_EVIDENCE.md）。研究轮在步骤之间会长时间
    # 无事件，因此允许单独放宽；未配置时取值与原先完全一致。Starlette 会缓存 body，
    # 下面那次解析仍读同一份字节。
    try:
        _early_body = json.loads(await request.body())
    except Exception:
        _early_body = None
    logger.info(
        f"[f_conversation] phase=request_shape research={is_research_turn(_early_body)} "
        f"shape={json.dumps(detection_summary(_early_body), ensure_ascii=True, separators=(',', ':'))}"
    )
    # 续聊的会话归属在请求体里。拒绝「已属于另一个 Seed」的会话，且必须在任何上游
    # 请求之前——否则同账号的另一个镜像用户可以先于归属检查把这一轮打到别人的会话上。
    # 镜像没有记录的会话仍然放行：首轮新建要靠 content_generator 在流里认领 id。
    _early_conversation_id = _early_body.get("conversation_id") if isinstance(_early_body, dict) else None
    if isinstance(_early_conversation_id, str) and \
            conversation_is_foreign(_early_conversation_id, token):
        raise HTTPException(status_code=404, detail="Conversation not found")
    upstream_timeout = stream_timeout_for(_early_body)
    client = Client(proxy=proxy_url, impersonate=impersonate, timeout=upstream_timeout)
    track_generation_client(request, client)
    if sentinel_proxy_url_list:
        sentinel_proxy_url = (
            random.choice(sentinel_proxy_url_list).replace("{}", session_id)
            if sentinel_proxy_url_list else None
        )
        clients = Client(proxy=sentinel_proxy_url, impersonate=impersonate)
    else:
        clients = client

    track_generation_client(request, clients)

    # 用缓存的 sentinel token，或重新服务端算。缓存带 TTL：chat-requirements token 有时效，
    # 过期（或 SENTINEL_CACHE_TTL=0 不缓存）时视为未命中重新解算，避免 stale token 触发上游 403。
    sentinel = _sentinel_cache.get(req_token, {})
    if sentinel and sentinel_cache_ttl > 0:
        if time.time() - sentinel.get("cached_at", 0) > sentinel_cache_ttl:
            _sentinel_cache.pop(req_token, None)
            sentinel = {}
    if not sentinel:
        try:
            # 传一份完整 fp 给 _server_sentinel：此处 fp 已在上面 pop 掉 proxy_url/impersonate，
            # 若直接用会走 proxy=None 直连被 GFW 重置，sentinel 空令牌级联 422。
            _sentinel_fp = get_fp(req_token).copy()
            chat_token, proof_token, turnstile_token, c2, c3, _, _ = await _server_sentinel(
                request, access_token, req_token, headers, _sentinel_fp, request_cookies
            )
            sentinel = {
                "chat_token": chat_token,
                "proof_token": proof_token,
                "turnstile_token": turnstile_token,
            }
            await c2.close()
            await c3.close()
        except HTTPException:
            raise
        except Exception:
            sentinel = {}
    headers.update({
        "openai-sentinel-chat-requirements-token": sentinel.get("chat_token", ""),
        "openai-sentinel-proof-token": sentinel.get("proof_token", ""),
        "openai-sentinel-turnstile-token": sentinel.get("turnstile_token", ""),
    })
    # sentinel 交换回来的 oai-sc 把 chat_token 绑到这次交换上；不带回去这一轮 turn，
    # 上游视 token 为未绑定而拒。_server_sentinel 刚写完缓存，此处回填到本次 cookie。
    for cookie in _sentinel_cookie_cache.get(req_token, []):
        request_cookies[cookie["name"]] = cookie["value"]

    # 清理 f/ 特有字段，转成老接口 conversation 兼容格式
    data = await request.body()
    try:
        body = rewrite_f_conversation_body(json.loads(data))
        data = json.dumps(body).encode("utf-8")
    except Exception:
        pass

    # 档位执行（Stage 2）：模型门禁 + 额度上限（无 user_auth 行 fail-open）。
    try:
        _f_model = body.get("model")
    except Exception:
        _f_model = None
    enforce_tier(token, _f_model)

    params = dict(request.query_params)

    async def _close(c, cs):
        for cl in (c, cs):
            if cl:
                try:
                    await cl.close()
                except Exception:
                    pass

    async def _release(c, cs, *, discard=False):
        """Release the upstream response and its clients.

        ``discard=True`` is for a stream that ended early (client disconnect,
        upstream error): hard-close the session instead of pooling it.  That
        is what makes the upstream observe the disconnect and stop generating
        (measured: upstream raises ConnectionResetError and stops immediately).

        Deliberately does *not* call ``r.aclose()`` first: curl_cffi's aclose
        drains the remaining body rather than aborting it — measured 4.16s on a
        stream that then delivered every remaining event, i.e. it neither frees
        the connection promptly nor stops upstream generation.
        """
        for cl in (c, cs):
            if cl:
                try:
                    await (cl.discard() if discard else cl.close())
                except Exception:
                    pass

    # 转发到老接口 conversation（不是 f/conversation）。上游经代理可能瞬时超时/SSL reset，
    # 做一次重建重试；最终失败返回 502 JSON，而不是未捕获异常 -> 500 栈（前端会渲染成「生成失败」）。
    r = None
    for _attempt in range(2):
        try:
            r = await client.post_stream(
                f"{host_url}/backend-api/conversation",
                params=params, headers=headers, cookies=request_cookies,
                data=data, stream=True, allow_redirects=False,
            )
            break
        except Exception as e:
            if _attempt == 0 and _is_transient_network_error(e):
                logger.warning(f"[f_conversation] transient network error, retrying: {type(e).__name__}")
                await client.discard()
                client = Client(proxy=proxy_url, impersonate=impersonate, timeout=upstream_timeout)
                track_generation_client(request, client)
                await asyncio.sleep(0.4)
                continue
            logger.error(f"[f_conversation] phase=upstream status=error error={type(e).__name__} "
                         f"elapsed_ms={int((time.monotonic() - started) * 1000)}")
            await _close(client, clients)
            return JSONResponse(status_code=502, content={"detail": "upstream unavailable"})

    background = BackgroundTask(_close, client, clients)
    rheaders = _sanitize_response_headers(r.headers)
    content_type = r.headers.get("content-type", "")
    logger.info(f"[f_conversation] phase=upstream status={r.status_code} "
                f"account={_anon(req_token)} "
                f"response={content_type.split(';', 1)[0]} "
                f"elapsed_ms={int((time.monotonic() - started) * 1000)}")

    async def _filter_gen():
        # content_generator already reassembles the transport into whole SSE
        # events (and does the conversation_id/title tracking), so split-chunk
        # (partial), sticky-packet (multi-event) and multi-byte UTF-8
        # boundaries never let a filtered event through.  Re-wrapping it in
        # iter_sse_events_async here would just parse the same bytes twice.
        completed = False
        failed = None
        failure = None
        events = 0
        try:
            body_conversation_id = body.get("conversation_id")
        except Exception:
            body_conversation_id = None
        # 保留这一轮的研究进度，供刷新/重连后向网关索取（上游协议未知，见
        # gateway/research_progress.py 的边界说明）。记录发生在事件已经确定要下发给
        # 浏览器之后，因此它不会改变线上字节，只是把它留档。
        recorder = research_progress_store.recorder(
            token, body_conversation_id, research=is_research_turn(body)
        )

        async def _pass_through():
            nonlocal events
            async for event in content_generator(r, token, True):
                # Each `event` is now exactly one SSE event (including its trailing
                # blank line).  extract_data_json finds the data: field regardless
                # of any leading event:/id:/retry: prefix lines, handles multiline
                # events, and returns None for [DONE], comments, and non-JSON data.
                _d = extract_data_json(event)
                if _d is not None and is_legacy_echo_event(_d):
                    continue
                observe_generation_event(request, event)
                if not recorder.record(event):
                    # A repeated terminal marker: the browser already got one.
                    continue
                events += 1
                yield event

        try:
            # 心跳默认关闭（interval=0 时 with_heartbeat 是直通）。开启时只在事件之间
            # 插入 SSE 注释帧，不改变、不延迟、不丢弃任何事件。
            async for event in with_heartbeat(_pass_through(), heartbeat_interval()):
                yield event
            completed = True
        except asyncio.CancelledError:
            # Client disconnect. Not an error; recorded as cancelled below.
            raise
        except Exception as exc:
            # A mid-stream failure is invisible from the outside: the response
            # committed 200 at its first event, so the browser sees a stream
            # that simply stops with no terminal event.  Without this branch it
            # was also indistinguishable in the logs from a user pressing stop.
            # The type is what reaches the log and the feedback record; the
            # exception object is handed over only so the circuit can recognise
            # a transport failure, and it keeps none of the exception's text.
            failed = type(exc).__name__
            failure = exc
            raise
        finally:
            # The outcome is recorded here rather than in the branches above: a
            # browser disconnect closes this generator with GeneratorExit, which
            # matches neither except clause, and the release path below runs for
            # all three exits.
            #
            # The guard is told in the same place, and for the same reason.  It
            # has no other way to learn how this stream ended: the response is
            # already committed as 200, so a turn that delivered its terminal
            # frame and a turn that died after the first event look identical
            # from the response lifetime.  Only this iterator sees the
            # difference, so "pending" can never reach the guard as a success.
            outcome = "complete" if completed else ("failed" if failed else "cancelled")
            observe_generation_end(request, outcome, failure)
            recorder.finish(outcome)
            # The browser disconnecting (user hits stop, closes the tab,
            # navigates away) makes Starlette cancel the body task, which
            # closes this generator — the response's BackgroundTask never runs.
            # Without releasing here the upstream keeps generating into a
            # socket nobody reads: measured, a 60-event upstream sent 29 more
            # events over 3s after the "cancel" and never saw a disconnect.
            #
            # A cut-short stream must be discarded, not pooled.  close() hands
            # the session back to the shared pool with the abandoned response
            # still attached, so the next turn for this account checks out a
            # session that is still mid-stream.  discard() hard-closes, which
            # is what makes the upstream observe the disconnect and stop.
            with anyio.CancelScope(shield=True):
                await _release(client, clients, discard=not completed)
            # Shape only: how the stream ended, how many events reached the
            # browser, how long it ran.  No bodies, headers or query strings.
            # This is the only authoritative record that a cancelled turn was
            # actually torn down -- a browser-side observer cannot show it.
            logger.info(f"[f_conversation] phase=stream_release "
                        f"account={_anon(req_token)} "
                        f"outcome={outcome} "
                        f"error={failed or '-'} "
                        f"released={'pooled' if completed else 'discarded'} "
                        f"events={events} "
                        f"elapsed_ms={int((time.monotonic() - started) * 1000)}")
            # Whether the mirror kept anything a reloading browser can restore,
            # in counts only: no event text, no metadata values, no prompt.  A
            # research turn that streams correctly but retains nothing is a
            # silent failure, and this is the line that would show it.  The
            # report is measured, never printed: its length and whether upstream
            # marked that frame as the end of the turn are what decide whether a
            # real deployment's panel will show an answer at all.
            if recorder.research:
                record = (research_progress_store.projection_snapshot(
                    recorder.conversation_id, recorder.owner)
                    if recorder.conversation_id else None)
                projection = (record or {}).get("projection") or {}
                logger.info(f"[f_conversation] phase=research_progress "
                            f"account={_anon(req_token)} "
                            f"retained={'yes' if record else 'no'} "
                            f"state={(record or {}).get('state', '-')} "
                            f"retained_events={(record or {}).get('events_seen', 0)} "
                            f"sources={projection.get('sources', 0)} "
                            f"sources_reported={projection.get('sources_evidenced', False)} "
                            f"report_chars={len(projection.get('report') or '')} "
                            f"report_final={projection.get('report_final', False)} "
                            f"finished={projection.get('finished', False)}")

    if "stream" in content_type or "text/event-stream" in content_type:
        # X-Accel-Buffering: no tells nginx not to buffer the SSE stream.
        # Without it, nginx holds all chunks until EOF and the browser only
        # sees content at the terminal state — never during generation.
        rheaders["x-accel-buffering"] = "no"
        # StreamingResponse defaults to 200.  An upstream 403/429 that still
        # carries text/event-stream would otherwise reach the browser as a
        # successful but empty stream, which the UI renders as a silently
        # truncated reply instead of an error.
        response = StreamingResponse(
            _filter_gen(),
            status_code=r.status_code,
            headers=rheaders,
            media_type=content_type,
            # No background task here: _filter_gen's finally already releases
            # on every exit path, including the client-disconnect cancellation
            # that never reaches a response background task at all.
        )
        return response
    else:
        return Response(
            content=(await r.atext()), headers=rheaders,
            media_type=content_type, status_code=r.status_code, background=background,
        )


async def get_real_req_token_wrapper(token):
    """兼容 gateway.reverseProxy.get_real_req_token（SeedToken 匹配账号）。"""
    from gateway.reverseProxy import get_real_req_token
    return await get_real_req_token(token)
