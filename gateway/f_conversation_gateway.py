"""f/conversation 服务端 sentinel 处理。

官网新版前端用 f/conversation 接口（sentinel 灰度），前端算的 PoW/turnstile
绑定前端浏览器 IP，经 gateway 反代后 IP 变化会触发风控。这里拦截新接口，
由服务端统一算 sentinel + PoW，转发到老接口 /backend-api/conversation。
"""
import hashlib
import json
import random
import uuid

from fastapi import Request, HTTPException
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app import app
from chatgpt.authorization import get_req_token, verify_token
from chatgpt.fp import get_fp
from chatgpt.proofofWork import get_config, get_answer_token, get_requirements_token
from gateway.reverseProxy import content_generator, headers_accept_list, resolve_seed_token
from utils.Client import Client
from utils.Logger import logger
from utils.configs import (
    chatgpt_base_url_list,
    turnstile_solver_url,
    sentinel_proxy_url_list,
    force_no_history,
)

# 服务端 sentinel 缓存：req_token -> {chat_token, proof_token, turnstile_token}
_sentinel_cache = {}
_sentinel_cookie_cache = {}


def _build_headers(request, access_token, req_token):
    fp = get_fp(req_token).copy()
    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() in headers_accept_list
    }
    headers.update(fp)
    headers.update({"authorization": f"Bearer {access_token}"})
    return headers, fp


async def _server_sentinel(request, access_token, req_token, headers, fp):
    """服务端调 sentinel chat-requirements，返回 (chat_token, proof_token, turnstile_token, client, clients, session_id, user_agent)。"""
    user_agent = fp.get(
        "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    )
    host_url = random.choice(chatgpt_base_url_list) if chatgpt_base_url_list else "https://chatgpt.com"
    proxy_url = fp.pop("proxy_url", None)
    impersonate = fp.pop("impersonate", "safari15_3")

    session_id = hashlib.md5(req_token.encode()).hexdigest()
    proxy_url = proxy_url.replace("{}", session_id) if proxy_url else None
    client = Client(proxy=proxy_url, impersonate=impersonate)
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
        for cookie in _sentinel_cookie_cache.get(req_token, []):
            clients.session.cookies.set(**cookie)
        r = await clients.post(
            f"{host_url}/backend-api/sentinel/chat-requirements",
            headers=headers, json=data, timeout=10,
        )
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
                logger.info(f"Turnstile ignored: {e}")

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
        logger.error(f"[f_conversation] server sentinel failed: {e}")
        await client.close()
        await clients.close()
        raise


@app.post("/backend-api/sentinel/chat-requirements/prepare")
async def f_sentinel_prepare(request: Request):
    """拦截 sentinel prepare：服务端算 sentinel，返回假 prepare_token（让前端跳过 PoW）。"""
    token = resolve_seed_token(request)
    req_token = await get_real_req_token_wrapper(token)
    access_token = await verify_token(req_token)
    headers, fp = _build_headers(request, access_token, req_token)
    try:
        chat_token, proof_token, turnstile_token, client, clients, _, _ = await _server_sentinel(
            request, access_token, req_token, headers, fp
        )
        _sentinel_cache[req_token] = {
            "chat_token": chat_token,
            "proof_token": proof_token,
            "turnstile_token": turnstile_token,
        }
        await client.close()
        await clients.close()
    except HTTPException:
        raise
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
    return {"conduit_token": str(uuid.uuid4())}


@app.post("/backend-api/f/conversation")
async def f_conversation(request: Request):
    """拦截 f/conversation：服务端 sentinel + 转发老接口 /backend-api/conversation。"""
    token = resolve_seed_token(request)
    req_token = await get_real_req_token_wrapper(token)
    access_token = await verify_token(req_token)
    headers, fp = _build_headers(request, access_token, req_token)

    host_url = random.choice(chatgpt_base_url_list) if chatgpt_base_url_list else "https://chatgpt.com"
    proxy_url = fp.pop("proxy_url", None)
    impersonate = fp.pop("impersonate", "safari15_3")
    user_agent = fp.get("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")

    session_id = hashlib.md5(req_token.encode()).hexdigest()
    proxy_url = proxy_url.replace("{}", session_id) if proxy_url else None
    client = Client(proxy=proxy_url, impersonate=impersonate)
    if sentinel_proxy_url_list:
        sentinel_proxy_url = (
            random.choice(sentinel_proxy_url_list).replace("{}", session_id)
            if sentinel_proxy_url_list else None
        )
        clients = Client(proxy=sentinel_proxy_url, impersonate=impersonate)
    else:
        clients = client

    # 用缓存的 sentinel token，或重新服务端算
    sentinel = _sentinel_cache.get(req_token, {})
    if not sentinel:
        try:
            chat_token, proof_token, turnstile_token, c2, c3, _, _ = await _server_sentinel(
                request, access_token, req_token, headers, fp
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

    # 清理 f/ 特有字段，转成老接口 conversation 兼容格式
    data = await request.body()
    try:
        body = json.loads(data)
        for _k in (
            "client_prepare_state", "supports_buffering", "enable_message_followups",
            "force_parallel_switch", "local_function_names",
            "paragen_cot_summary_display_override",
        ):
            body.pop(_k, None)
        if body.get("parent_message_id") == "client-created-root":
            body["parent_message_id"] = str(uuid.uuid4())
        body["supported_encodings"] = []
        body["websocket_request_id"] = str(uuid.uuid4())
        body.setdefault("force_paragen", False)
        body.setdefault("force_rate_limit", False)
        body.setdefault("reset_rate_limits", False)
        body.setdefault("suggestions", [])
        # 清理 messages 里的 f/ 特有字段，对齐老接口 conversation 格式
        for _m in body.get("messages", []):
            _m.pop("create_time", None)
            _md = _m.get("metadata") or {}
            _md.pop("submission_mode", None)
            _md.pop("serialization_metadata", None)
            _m["metadata"] = _md
        data = json.dumps(body).encode("utf-8")
    except Exception:
        pass

    params = dict(request.query_params)
    request_cookies = dict(request.cookies)

    async def _close(c, cs):
        for cl in (c, cs):
            if cl:
                try:
                    await cl.close()
                except Exception:
                    pass

    background = BackgroundTask(_close, client, clients)
    # 转发到老接口 conversation（不是 f/conversation）
    r = await client.post_stream(
        f"{host_url}/backend-api/conversation",
        params=params, headers=headers, cookies=request_cookies,
        data=data, stream=True, allow_redirects=False,
    )
    rheaders = r.headers
    content_type = rheaders.get("content-type", "")
    logger.info(f"[f_conversation] upstream status={r.status_code} ct={content_type}")

    async def _filter_gen():
        _count = 0
        _forwarded = 0
        async for _chunk in content_generator(r, token, True):
            _count += 1
            _s = _chunk.decode("utf-8", errors="replace")
            if _s.startswith("data: {"):
                try:
                    _d = json.loads(_s[6:])
                    _t = _d.get("type", "message")
                    # 老接口 conversation 会先返回 resume token + system/user 回显，
                    # 过滤掉，只转发 assistant 回复（对齐 f/conversation 前端期望的流）
                    if _t == "resume_conversation_token":
                        continue
                    if _t == "message":
                        _role = (_d.get("message") or {}).get("author", {}).get("role")
                        if _role in ("user", "system"):
                            continue
                except Exception:
                    pass
            _forwarded += 1
            yield _chunk
        logger.info(f"[f_conversation] total={_count} forwarded={_forwarded}")

    if "stream" in content_type or "text/event-stream" in content_type:
        response = StreamingResponse(
            _filter_gen(),
            headers=rheaders,
            media_type=content_type,
            background=background,
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
