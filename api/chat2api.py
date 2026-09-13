import asyncio
import json
import time
import types
import uuid

import anyio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Request, HTTPException, Form, Security
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from starlette.responses import Response

import utils.audit as audit
import utils.globals as globals
from app import app, templates, security_scheme
from chatgpt.ChatService import ChatService
from chatgpt.authorization import refresh_all_tokens
from chatgpt import session_sticky
from utils.bootstrap import initialize_from_env
from utils.Logger import logger
from utils.configs import api_prefix, scheduled_refresh, history_disabled, enable_session_sticky
from utils.retry import async_retry
from utils.store import StoreError
from utils.tiers import enforce_tier
from utils import antiban
from utils import fleet_health
from utils import trials
from utils import usage
from utils.antiban import circuit as antiban_circuit

scheduler = AsyncIOScheduler()


def _require_pool_admin(request: Request):
    """Protect legacy pool controls with the canonical admin boundary.

    ``api.chat2api`` is imported before ``gateway.admin`` during app startup,
    so the import must remain lazy to avoid a circular import.
    """
    from gateway.admin import require_admin_auth
    require_admin_auth(request)


def _responses_input_to_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in {"input_text", "output_text", "text"}:
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif item_type == "message":
                chunks.append(_responses_input_to_messages(item))
        return "\n".join(part for part in chunks if part)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), list):
            return _responses_input_to_text(value["content"])
    return str(value)


def _responses_input_to_messages(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        role = value.get("role")
        if role in {"system", "developer", "user", "assistant"}:
            content = _responses_input_to_text(value.get("content"))
            return {"role": role, "content": content or ""}
    return None


def _convert_responses_request_to_chat(payload):
    messages = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions.strip()})

    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            message = _responses_input_to_messages(item)
            if message:
                messages.append(message)
            else:
                text = _responses_input_to_text(item)
                if text:
                    messages.append({"role": "user", "content": text})
    elif raw_input is not None:
        text = _responses_input_to_text(raw_input)
        if text:
            messages.append({"role": "user", "content": text})

    if not messages:
        raise HTTPException(status_code=400, detail={"error": "input is required"})

    chat_payload = {
        "model": payload.get("model"),
        "messages": messages,
        "stream": bool(payload.get("stream", False)),
    }
    for key in (
        "temperature",
        "top_p",
        "max_output_tokens",
        "presence_penalty",
        "frequency_penalty",
        "user",
    ):
        if key in payload:
            value = payload[key]
            if key == "max_output_tokens":
                chat_payload["max_tokens"] = value
            else:
                chat_payload[key] = value
    return chat_payload


def _convert_chat_response_to_responses(chat_response, request_payload):
    choice = ((chat_response or {}).get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output_text = message.get("content", "") or ""
    usage = chat_response.get("usage") or {}
    created = int(time.time())
    response_id = f"resp_{uuid.uuid4().hex}"
    model = chat_response.get("model") or request_payload.get("model")
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": output_text,
                        "annotations": [],
                    }
                ],
            }
        ],
        "output_text": output_text,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "finish_reason": choice.get("finish_reason"),
    }


def _compact_responses_payload(data):
    usage = data.get("usage") or {}
    return {
        "id": data.get("id"),
        "object": "response.compact",
        "model": data.get("model"),
        "output_text": data.get("output_text", ""),
        "finish_reason": data.get("finish_reason"),
        "usage": usage,
    }


def _admit_generation(seed):
    """产品准入：套餐 / 试用资格闸 + 试用额度预留。

    返回三态，与 :func:`utils.trials.reserve` 对齐但**先**过档位闸：

      - ``None``：运营者 seed / 直传 token / 有效付费用户 —— 不占试用账，
        **不是拒绝**；
      - :class:`utils.trials.TrialAttempt`：SaaS 试用用户，本次生成已预占一次额度，
        响应生命周期结束时用 ``finish(delivered)`` 结算；
      - 抛 ``HTTPException``：无有效套餐 / 试用额度用尽 / 账号不可用 402，
        权益或台账不可用 503。

    模型门禁刻意不在这里执行（``enforce_tier`` 的第二参数留空）：``/v1`` 的模型别名
    （如 ``gpt-4o``）与档位目录里的 slug 不同名，按目录白名单执行会把既有 API 客户端
    的合法请求判成 403。档位、额度和试用资格它照样管。
    """
    enforce_tier(seed)
    try:
        reservation = trials.reserve(seed)
    except trials.TrialDenied:
        raise HTTPException(
            status_code=402, detail="Plus trial unavailable; choose a subscription") from None
    except StoreError:
        raise HTTPException(
            status_code=503, detail="Trial accounting temporarily unavailable") from None
    if reservation is None:
        return None
    return trials.TrialAttempt(reservation, seed)


class _CompletionObserver:
    """OpenAI 兼容流的成功完成信号（不缓存正文，只记形状）。

    成功 = 出现过带非空正文的增量 **且** 出现过带 ``finish_reason`` 的终止分片。
    上游报错时 ``chatFormat.stream_response`` 只补一个 ``data: [DONE]`` 就收尾，
    不会有终止分片；空流同理。所以「有 [DONE]」不等于成功，这个区分就是扣费判据。
    """

    def __init__(self):
        self.text = False
        self.terminal = False
        self.failed = False

    def observe(self, chunk):
        if not isinstance(chunk, str) or not chunk.startswith("data: "):
            return
        payload = chunk[6:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            data = json.loads(payload)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        if data.get("error"):
            self.failed = True
            return
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        # 逐条 choice 嗅探：终止信号只出现在某一条上时也不能漏判（漏判 = 成功不扣次数）。
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str) and content.strip():
                self.text = True
            reason = choice.get("finish_reason")
            if isinstance(reason, str) and reason:
                self.terminal = True

    @property
    def completed(self):
        return self.terminal and self.text and not self.failed


async def _observe_completion(iterator, attempt):
    """把分片发给客户端之前先嗅探完成信号（只置位，不结算）。"""
    observer = _CompletionObserver()
    try:
        async for chunk in iterator:
            observer.observe(chunk)
            if observer.completed:
                attempt.mark_completed()
            yield chunk
    finally:
        # 生成器被取消 / 提前关闭时，内层生成器持有的上游流也要跟着结束，
        # 否则它会一直挂在连接池里等下一个请求来 drain（见 test_m2_stream_cancel.py）。
        # shield：断连时本协程正处于取消状态，不 shield 的话这个 await 会立刻再抛，
        # 内层生成器根本关不掉，只能等 GC。
        close = getattr(iterator, "aclose", None)
        if close is not None:
            with anyio.CancelScope(shield=True):
                try:
                    await close()
                except Exception:
                    pass


def _response_has_content(payload):
    """非流式响应是否真的产出了一段回复（空正文不算一次成功生成）。"""
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return bool(isinstance(content, str) and content.strip())


class _GenerationLifetime:
    """一次生成持有的账号租约 + 试用账，直到 ASGI 发送任务结束才结束。

    必须由 :class:`_LifetimeResponse` 在 ``finally`` 里收尾：流式响应在正文迭代器
    抛错或发送失败时会从 Starlette 的任务组里直接抛出，``BackgroundTask`` 在那两条
    路径上根本不会执行 —— 账号并发槽位就此永久泄漏。
    """

    def __init__(self, chat_service, attempt=None):
        self.chat_service = chat_service
        self.attempt = attempt
        self._closed = False

    async def close(self, delivered):
        """按「是否完整交付」结清试用账并释放租约；重复调用是 no-op。

        台账与租约是两件事：结账失败（哪怕是意料之外的异常）也不能把槽位一起丢掉，
        所以 ``close_client`` 放在 ``finally`` 里 —— 归还槽位是硬保证，结账是尽力而为。
        """
        if self._closed:
            return
        self._closed = True
        # 客户端断连时 Starlette 会取消响应任务：清理必须在取消域里跑完，
        # 否则槽位归还到一半就被打断。
        with anyio.CancelScope(shield=True):
            try:
                if self.attempt is not None:
                    self.attempt.finish(delivered)
            finally:
                await self.chat_service.close_client()


class _LifetimeResponse(Response):
    """把响应的 ASGI 生命周期接到 :class:`_GenerationLifetime` 上。

    复用原响应的状态码、头部与正文迭代器，只在外面套一层 ``finally``：
    正常收尾、发送失败、被取消，租约与试用账都在这里结束。
    """

    def __init__(self, response, lifetime):
        super().__init__(status_code=response.status_code)
        self.raw_headers = response.raw_headers
        self.response = response
        self.lifetime = lifetime

    async def __call__(self, scope, receive, send):
        delivered = False

        async def observe_send(message):
            nonlocal delivered
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                delivered = True

        try:
            await self.response(scope, receive, observe_send)
        finally:
            await self.lifetime.close(delivered and 200 <= self.status_code < 300)


async def _process_responses_request(request_data, req_token):
    chat_request_data = _convert_responses_request_to_chat(request_data)
    if chat_request_data.get("stream"):
        raise HTTPException(status_code=400, detail={"error": "stream responses is not supported yet"})

    attempt = _admit_generation(req_token)
    try:
        chat_service, res = await async_retry(process, chat_request_data, req_token)
    except BaseException:
        # 准入已经占掉一次试用额度，响应生命周期之前的任何失败都要退回。
        if attempt is not None:
            attempt.release()
        raise
    lifetime = _GenerationLifetime(chat_service, attempt)
    try:
        if isinstance(res, types.AsyncGeneratorType):
            raise HTTPException(status_code=400, detail={"error": "stream responses is not supported yet"})
        if attempt is not None and _response_has_content(res):
            attempt.mark_completed()
        return _convert_chat_response_to_responses(res, request_data), lifetime
    except BaseException:
        await lifetime.close(delivered=False)
        raise


@app.on_event("startup")
async def app_start():
    initialize_from_env()
    # Single-host SQLite: reclaim confirmed dead owners, never a live research
    # request merely because it belongs to another process or takes a long time.
    from utils.trials import recover_orphan_reservations
    recover_orphan_reservations()
    from utils.seed_lifecycle import freeze_expired_seeds
    freeze_expired_seeds()
    scheduler.add_job(
        id='seed_expiry', func=freeze_expired_seeds,
        trigger='interval', seconds=60, max_instances=1, coalesce=True,
    )
    await antiban.init()

    # Session sticky: 启动时初始化 SQLite + 启动 TTL 清理定时任务
    if enable_session_sticky:
        session_sticky.init_db()
        scheduler.add_job(
            id='session_sticky_cleanup',
            func=session_sticky.cleanup_expired,
            trigger='interval',
            hours=24,
        )

    # Antiban 自愈定时任务
    from utils.configs import enable_antiban, circuit_bucket_heal_minutes, circuit_dead_account_recheck_hours
    if enable_antiban:
        scheduler.add_job(
            id='antiban_heal',
            func=antiban_circuit.scheduled_heal,
            trigger='interval',
            minutes=max(int(circuit_bucket_heal_minutes), 5),
        )

    # Fleet 健康检查：周期探活每个账号，写 accounts.status 三态。
    # 复用 circuit_dead_account_recheck_hours 作为探活间隔（现配置原本零引用）。
    scheduler.add_job(
        id='fleet_health_check',
        func=fleet_health.check_all_accounts,
        trigger='interval',
        hours=max(int(circuit_dead_account_recheck_hours), 1),
    )

    # 用量统计：内存计数周期落库 usage_events（避免反代热路径每请求写 SQLite）。
    from utils.configs import usage_flush_interval_seconds
    scheduler.add_job(
        id='usage_flush',
        func=usage.flush_usage,
        trigger='interval',
        seconds=max(int(usage_flush_interval_seconds), 15),
    )

    if scheduled_refresh:
        scheduler.add_job(id='refresh', func=refresh_all_tokens, trigger='cron', hour=3, minute=0, day='*/2',
                          kwargs={'force_refresh': True})
        scheduler.start()
        asyncio.get_event_loop().call_later(0, lambda: asyncio.create_task(refresh_all_tokens(force_refresh=False)))
    elif enable_antiban:
        # 只有 antiban 启用、没启用 refresh 时，也需要把 scheduler 跑起来
        scheduler.start()
    elif enable_session_sticky:
        # 仅 session_sticky 启用时，scheduler 也要启动以执行 cleanup
        scheduler.start()
    else:
        # 仅健康检查启用时，也需要把 scheduler 跑起来
        scheduler.start()


async def _shutdown(chat_service):
    """响应建立之前的异常/取消路径：shield 住取消域，保证账号槽位真的归还。

    取消（客户端断连 / 服务停机）也是 ``BaseException``，被 ``except Exception``
    漏掉时槽位就永久留在在飞状态 —— 容量单调泄漏，而不是报错。
    """
    with anyio.CancelScope(shield=True):
        await chat_service.close_client()


async def to_send_conversation(request_data, req_token):
    chat_service = ChatService(req_token)
    try:
        await chat_service.set_dynamic_data(request_data)
        await chat_service.get_chat_requirements()
        return chat_service
    except BaseException as e:
        # 取消同样是失败路径：账号槽位在响应建立之前也必须归还。
        await _shutdown(chat_service)
        if isinstance(e, HTTPException):
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        if isinstance(e, asyncio.CancelledError):
            raise
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


async def process(request_data, req_token):
    chat_service = await to_send_conversation(request_data, req_token)
    try:
        await chat_service.prepare_send_conversation()
        res = await chat_service.send_conversation()
        return chat_service, res
    except BaseException as e:
        await _shutdown(chat_service)
        if isinstance(e, HTTPException):
            if e.status_code == 500:
                raise HTTPException(status_code=500, detail="Server error")
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        if isinstance(e, asyncio.CancelledError):
            raise
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


def parse_bool_query(value, default):
    if value is None:
        return default
    return str(value).lower() in ['true', '1', 't', 'y', 'yes']


def format_models_response(model_slugs):
    data = []
    for model_slug in sorted(model_slugs):
        data.append({
            "id": model_slug,
            "object": "model",
            "created": 0,
            "owned_by": "openai",
        })
    return {
        "object": "list",
        "data": data,
    }


@app.post(f"/{api_prefix}/v1/chat/completions" if api_prefix else "/v1/chat/completions")
async def send_conversation(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    req_token = credentials.credentials
    try:
        request_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "Invalid JSON body"})
    # Session sticky: LibreChat conv_id → ChatGPT conv_id 翻译注入
    # 副作用: 命中映射时改写 request_data['conversation_id'/'parent_message_id'/'messages']
    # 返回 lc_conv_id 用于流式响应嗅探回写；未启用或无 lc 字段时返回 None
    lc_conv_id = session_sticky.inject_session(request_data) if enable_session_sticky else None
    # 产品准入：档位/试用资格 + 试用额度预留。必须在打上游之前，
    # 否则一次拒绝会先花掉上游的连接与并发槽位。
    attempt = _admit_generation(req_token)
    try:
        chat_service, res = await async_retry(process, request_data, req_token)
    except BaseException:
        # 响应还没建立，本次预留不可能被交付 —— 立刻退回。
        if attempt is not None:
            attempt.release()
        raise
    # 把 lc_conv_id 挂到 chat_service 上，供 stream_response 嗅探时回写 DB
    if lc_conv_id:
        chat_service.librechat_conv_id = lc_conv_id
    lifetime = _GenerationLifetime(chat_service, attempt)
    try:
        if isinstance(res, types.AsyncGeneratorType):
            body = res if attempt is None else _observe_completion(res, attempt)
            return _LifetimeResponse(
                StreamingResponse(body, media_type="text/event-stream"), lifetime)
        if attempt is not None and _response_has_content(res):
            attempt.mark_completed()
        return _LifetimeResponse(JSONResponse(res, media_type="application/json"), lifetime)
    except HTTPException as e:
        await lifetime.close(delivered=False)
        if e.status_code == 500:
            logger.error(f"Server error, {str(e)}")
            raise HTTPException(status_code=500, detail="Server error")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await lifetime.close(delivered=False)
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


@app.post(f"/{api_prefix}/v1/responses" if api_prefix else "/v1/responses")
async def send_responses(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    req_token = credentials.credentials
    try:
        request_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "Invalid JSON body"})
    lifetime = None
    try:
        response_payload, lifetime = await _process_responses_request(request_data, req_token)
        return _LifetimeResponse(
            JSONResponse(response_payload, media_type="application/json"), lifetime)
    except HTTPException as e:
        if lifetime is not None:
            await lifetime.close(delivered=False)
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        if lifetime is not None:
            await lifetime.close(delivered=False)
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


@app.post(f"/{api_prefix}/v1/responses/compact" if api_prefix else "/v1/responses/compact")
async def send_responses_compact(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    req_token = credentials.credentials
    try:
        request_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "Invalid JSON body"})
    lifetime = None
    try:
        data, lifetime = await _process_responses_request(request_data, req_token)
        return _LifetimeResponse(
            JSONResponse(_compact_responses_payload(data), media_type="application/json"),
            lifetime)
    except HTTPException as e:
        if lifetime is not None:
            await lifetime.close(delivered=False)
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        if lifetime is not None:
            await lifetime.close(delivered=False)
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")

@app.get(f"/{api_prefix}/v1/models" if api_prefix else "/v1/models")
async def list_models(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    chat_service = ChatService(credentials.credentials)
    try:
        await chat_service.resolve_auth_context()
        chat_service.history_disabled = parse_bool_query(
            request.query_params.get("history_disabled", request.query_params.get("history_and_training_disabled")),
            history_disabled,
        )
        request_account_id = request.headers.get("ChatGPT-Account-ID") or request.headers.get("Chatgpt-Account-Id")
        if request_account_id:
            chat_service.account_id = request_account_id
        await chat_service.initialize_request_context()
        model_slugs = await chat_service.fetch_available_models()
        return JSONResponse(format_models_response(model_slugs), media_type="application/json")
    except HTTPException as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")
    finally:
        await chat_service.close_client()


@app.get(f"/{api_prefix}/tokens" if api_prefix else "/tokens", response_class=HTMLResponse)
async def upload_html(request: Request):
    _require_pool_admin(request)
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return templates.TemplateResponse("tokens.html",
                                      {"request": request, "api_prefix": api_prefix, "tokens_count": tokens_count})


@app.post(f"/{api_prefix}/tokens/upload" if api_prefix else "/tokens/upload")
async def upload_post(request: Request, text: str = Form(...)):
    _require_pool_admin(request)
    lines = text.split("\n")
    for line in lines:
        if line.strip() and not line.startswith("#"):
            globals.token_list.append(line.strip())
    globals.persist_token_list()
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/clear" if api_prefix else "/tokens/clear")
async def clear_tokens(request: Request):
    _require_pool_admin(request)
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.persist_token_list()
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/error" if api_prefix else "/tokens/error")
async def error_tokens(request: Request):
    _require_pool_admin(request)
    error_tokens_list = list(set(globals.error_token_list))
    return {"status": "success", "error_tokens": error_tokens_list}


@app.get(f"/{api_prefix}/tokens/add/{{token}}" if api_prefix else "/tokens/add/{token}")
async def add_token_legacy(request: Request, token: str):
    _require_pool_admin(request)
    raise HTTPException(
        status_code=410,
        detail="Token-in-URL import is disabled; use POST /tokens/add",
    )


@app.post(f"/{api_prefix}/tokens/add" if api_prefix else "/tokens/add")
async def add_token(request: Request, text: str = Form(...)):
    _require_pool_admin(request)
    token = text.strip()
    if token and not token.startswith("#"):
        globals.token_list.append(token)
        globals.persist_token_list()
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/seed_tokens/clear" if api_prefix else "/seed_tokens/clear")
async def clear_seed_tokens(request: Request):
    _require_pool_admin(request)
    # 与 DELETE /seedtoken 的 clear 同一条所有权边界：显式原子吊销运营者授权，再按
    # durable 状态重装账户域缓存。旧实现是「清内存 + 整表写回」——seed_map 一空，
    # 写回就变成删光所有 users 行与会话（注册用户的账号和历史一起没了）。池管理端的
    # 「清 seed」是一个授权操作，不是一个删账号的操作。
    try:
        revoked = globals.revoke_all_operator_grants()
    except StoreError:
        raise HTTPException(status_code=503, detail="Seed revocation unavailable") from None
    globals.persist_conversation_map()
    audit.record("pool.seeds_cleared", detail={"count": revoked, "source": "pool_control"})
    logger.info(f"Seed token count: {len(globals.seed_map)}")
    return {"status": "success", "seed_tokens_count": len(globals.seed_map),
            "revoked": revoked}
