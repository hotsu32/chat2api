"""Own generation admission through the complete ASGI response lifetime.

Metadata and prepare requests do not acquire generation capacity. Both chat
routes acquire after resolving the account, before upstream authentication.
The response wrapper also releases on send failure, before a body iterator has
started, and on client disconnect; a background task alone cannot ensure this.

The same lifetime owns the *feedback* half of the contract. An admitted request
must tell the antiban guard how it ended -- completed, refused by the upstream,
lost to a transport failure, or cancelled -- because the guard's cooldown,
backoff, dead-account and bucket-degrade state is only as true as its input.
Before this, the gateway routes reported almost nothing: a successful turn
never reached ``report_success`` (so backoff never reset and the account was
never marked used), and a transport failure was mapped by the route into a
502/503 response that the guard never saw as a network failure at all. Capacity
protection then reacted to state that no longer tracked production.

Feedback is derived from the response lifetime itself -- status, the raised
exception chain, and the streamed events -- rather than requiring every call
site to remember to report. It stays body-free: only fixed protocol markers are
kept, and only their enum names leave this module.

Success is the one verdict that cannot be inferred from a 2xx alone. The
response commits its status at the *first* streamed event, so a stream that
then raises, is cancelled by a browser hitting stop, or simply ends without the
upstream's end-of-stream marker is byte-for-byte indistinguishable from a
delivered turn. Only the body iterator knows which of those happened, and only
the upstream's own terminal marker distinguishes "ended" from "delivered". Both
signals must be present before the guard is told the account did real work.
"""
from functools import wraps
from dataclasses import dataclass
import re

import anyio
import asyncio
from fastapi import HTTPException
from fastapi.responses import Response

from gateway.sse_parser import extract_data, extract_data_json, is_complete_event
from utils import trials, store
from utils.antiban import circuit, guard
from utils.Logger import logger

# Statuses a handler *raises* that mean "the upstream refused this one account":
# 401 rejected credential, 403 challenge/refusal, 429 rate limit -- the three the
# circuit's error table knows how to act on. The rest (400/402/404 and this
# layer's own 500/503) are conclusions reached here, not evidence about the
# account; reporting them would cool down an innocent account and turn one
# refusal into a self-reinforcing outage.
_RAISED_STATUS = frozenset({401, 403, 429})

# Statuses *returned* to the client are the upstream's own, so 5xx is upstream
# evidence too (the circuit's action for 5xx is a 30s light backoff; it never
# marks an account dead or degrades a bucket).
_RETURNED_STATUS = frozenset({401, 403, 429}) | frozenset(range(500, 600))

# 502/504 are this gateway's fixed mapping for "the upstream hop did not
# complete" (both the HTTPException and the JSONResponse are produced here, see
# gateway/reverseProxy.py and f_conversation_gateway.py). An upstream actually
# answering 502/504 falls in the same class, so both go to the network reason
# (degrade the bucket after 3 in a row) rather than 5xx account backoff.
_NETWORK_STATUS = {502: circuit.KIND_PROTOCOL, 504: circuit.KIND_TIMEOUT}

# Status code -> fixed normalised signal. The gateway path cannot see the
# upstream body, but "the upstream answered 401 for this account" is itself the
# classification input; this passes an enum word, never a response body.
_STATUS_SIGNAL = {401: "unauthorized"}

# Fixed protocol markers that may be sniffed out of a response body or event
# (**not** natural-language words: "banned" appears in ordinary prose, so it is
# not sniffable). Only the matched marker name is handed to the circuit's
# existing classifier; the body itself reaches no log and no metric.
_BODY_SIGNALS = ("cf_chl_opt", "account_deactivated")

# Sniff budget per request. Bodies can be large; only the head is read.
_SNIFF_LIMIT = 4096

# Cheap pre-check for an error frame in the stream; JSON parsing happens only on
# a hit, so ordinary events are not parsed at all.
_ERROR_FRAME_MARKER = b'"error"'

# Stream states that count as delivered in full. ``pending`` -- the state a
# request is in before its iterator reported anything -- is deliberately absent:
# treating it as terminal made every stream the route forgot to classify (and
# every stream still running when the response tore down) count as a delivered
# turn. Missing success is a lost pacing hint; a false success resets a real
# backoff and marks a dead account used, so the asymmetry is intentional.
#
# A 2xx response with no body iterator at all (the non-SSE branches) has no
# delivery evidence either way and so stays ``pending``: it is reported as
# nothing rather than credited on its status code. A chat turn does not arrive
# as a non-SSE body, and the alternative -- crediting whatever a 2xx carries --
# is what this rule exists to stop.
_COMPLETE_STREAMS = ("complete",)

# Exit verdicts a route may publish for its body iterator. Anything else is
# ignored rather than guessed at, which leaves the stream unclassified and
# therefore never successful.
_STREAM_END_STATES = ("complete", "failed", "cancelled")


# --- terminal-answer evidence -----------------------------------------------
# One predicate decides whether the frames seen so far *are* the turn's answer,
# and the research projection (``gateway.research_progress.project_event``)
# answers it from the same evidence. That module mounts routes on ``app``, so
# importing its projection from here would close a cycle; the rule is therefore
# restated once, here, and both ledgers are kept in step by the tests that drive
# the same real capture.
#
# What the capture fixes (``tests/fixtures/deep_research_plus_shapes.json``, the
# 2026-09-12 Plus Deep Research turn, 23 events, field shapes only):
#
# * the answer frame is the only frame carrying ``metadata.is_complete``; it is
#   assistant-authored, ``finished_successfully``, carries ``end_turn``, and
#   carries the answer as ``content.text`` -- with no ``parts`` at all. Reading
#   only ``parts`` meant the one frame a real research turn ends on never
#   charged the trial it consumed;
# * every interim frame carries ``parts`` instead and carries no ``is_complete``,
#   while several of them are upstream-marked
#   ``is_visually_hidden_from_conversation`` or name a tool invocation in their
#   metadata. ``end_turn`` alone is not an answer: the capture records the field
#   on the walk of frames leading up to it.
#
# So "the turn produced an answer" is: the assistant closed the turn, the frame
# declares itself complete, a visible body is present, and the frame is not one
# of the internal ones (hidden, tool-addressed, or reasoning) that share the
# stream. The result feeds the trial ledger alone; the antiban verdict above
# keeps its own, older evidence, because a lost pacing hint and a wrongly
# charged generation are not the same mistake.
ASSISTANT_ROLE = "assistant"
FINISHED_STATUS = "finished_successfully"

_HIDDEN_KEY = "is_visually_hidden_from_conversation"

# A tool recipient is namespace-qualified (``web.run`` is the observed one), and
# the observed tool metadata keys are the same two the projection excludes.
_TOOL_RECIPIENT_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_TOOL_METADATA_KEYS = ("invoked_resource", "invoked_plugin")

# Content types whose body is not the answer: reasoning is not the report, and
# the official UI does not show it either.
NON_ANSWER_CONTENT_TYPES = frozenset({"thoughts", "reasoning_recap"})


def _declares_completion(metadata) -> bool:
    """Whether this frame's own metadata permits it to be the answer frame.

    ``is_complete`` is the flag the capture shows on exactly one frame -- the
    answer -- so a frame that carries message metadata at all has to declare it.
    A frame that carries none is the plain chat shape this ledger has always
    settled: it has no completion flag to declare, and the rest of the predicate
    still has to hold for it.

    That last clause is the rule's only divergence from the projection, and the
    capture cannot reach it: every message frame of the real research turn
    carries metadata, so for the stream this rule exists for, the two agree
    frame by frame (``tests/test_trial_research_settlement.py`` drives exactly
    that). It stays because the ordinary chat shape -- the one this ledger has
    settled since it existed, and the one the gateway's chat fixtures pin -- has
    no metadata to read a completion flag out of, and a plain chat turn is not a
    research turn.
    """
    if not isinstance(metadata, dict) or not metadata:
        return True
    return metadata.get("is_complete") is True


def _normalised_content_type(content) -> str:
    value = content.get("content_type") if isinstance(content, dict) else None
    return value.strip().lower() if isinstance(value, str) else ""


def _has_body(content) -> bool:
    """Whether this content carries the answer body, in either observed shape.

    ``content.text`` is the answer frame's shape and ``content.parts`` is every
    other frame's; neither is privileged, and a body that is only whitespace is
    not an answer.
    """
    if not isinstance(content, dict):
        return False
    text = content.get("text")
    if isinstance(text, str) and text.strip():
        return True
    parts = content.get("parts")
    return isinstance(parts, list) and any(
        isinstance(part, str) and part.strip() for part in parts)


def _tool_recipient(recipient) -> bool:
    return bool(recipient) and bool(_TOOL_RECIPIENT_RE.match(recipient))


def _tool_metadata(metadata) -> bool:
    return isinstance(metadata, dict) and any(
        metadata.get(key) for key in _TOOL_METADATA_KEYS)


@dataclass
class _Trial:
    reservation: str
    seed: str
    done: bool = False
    failed: bool = False
    settled: bool = False
    role: str = ""
    status: str = ""
    end_turn: bool = False
    has_body: bool = False
    content_type: str = ""
    declared_complete: bool = False
    hidden: bool = False
    tool_recipient: bool = False
    tool_metadata: bool = False
    delta_path: str = ""
    delta_operation: str = "replace"

    @property
    def assistant_complete(self):
        """Whether the frames observed so far are the turn's own answer.

        The evidence is the research projection's, and the internal-frame
        exclusions are the ones its report gate applies: an answer nobody could
        see is not a generation the user consumed.
        """
        return (self.role == ASSISTANT_ROLE
                and self.status == FINISHED_STATUS
                and self.end_turn
                and self.has_body
                and self.declared_complete
                and self.content_type not in NON_ANSWER_CONTENT_TYPES
                and not (self.hidden or self.tool_recipient or self.tool_metadata))

    def _snapshot(self, message):
        # Retain only completion metadata, never generated text or identifiers.
        self.role, self.status, self.end_turn, self.has_body = '', '', False, False
        self.content_type, self.declared_complete = '', False
        self.hidden, self.tool_recipient, self.tool_metadata = False, False, False
        if not isinstance(message, dict):
            return
        author, content = message.get('author'), message.get('content')
        self.role = author.get('role', '') if isinstance(author, dict) else ''
        self.status = message.get('status', '')
        self.end_turn = message.get('end_turn') is True
        self.has_body = _has_body(content)
        self.content_type = _normalised_content_type(content)
        # Recipient first: the tool rule reads it together with the metadata.
        recipient = message.get('recipient')
        self.tool_recipient = _tool_recipient(recipient if isinstance(recipient, str) else '')
        self._metadata(message.get('metadata'))
        if self.status in ('failed', 'cancelled', 'incomplete'):
            self.failed = True

    def _metadata(self, metadata):
        self.hidden = isinstance(metadata, dict) and metadata.get(_HIDDEN_KEY) is True
        self.tool_metadata = _tool_metadata(metadata)
        self.declared_complete = _declares_completion(metadata)

    def _delta(self, delta, depth=0):
        if not isinstance(delta, dict):
            return
        operation = delta.get('o', self.delta_operation)
        value = delta.get('v')
        if operation == 'patch':
            if depth >= 8 or not isinstance(value, list) or len(value) > 128:
                self.failed = True
                return
            for item in value:
                self._delta(item, depth + 1)
            return
        path = delta.get('p', self.delta_path)
        if not isinstance(path, str) or operation not in ('add', 'replace', 'append', 'remove'):
            return
        self.delta_path, self.delta_operation = path, operation
        if operation == 'remove':
            value = None
        if path in ('', '/') and isinstance(value, dict) and 'message' in value:
            self._snapshot(value['message'])
        elif path == '/message':
            self._snapshot(value)
        elif path == '/message/author':
            self.role = value.get('role', '') if isinstance(value, dict) else ''
        elif path == '/message/author/role':
            self.role = value if isinstance(value, str) else ''
        elif path == '/message/status':
            self.status = value if isinstance(value, str) else ''
            if self.status in ('failed', 'cancelled', 'incomplete'):
                self.failed = True
        elif path == '/message/end_turn':
            self.end_turn = value is True
        elif path == '/message/recipient':
            self.tool_recipient = _tool_recipient(value if isinstance(value, str) else '')
        elif path == '/message/metadata':
            self._metadata(value)
        elif path == '/message/metadata/is_complete':
            self.declared_complete = value is True
        elif path == '/message/content':
            self.has_body = _has_body(value)
            self.content_type = _normalised_content_type(value)
        elif path == '/message/content/text':
            self.has_body = isinstance(value, str) and bool(value.strip())
        elif path == '/message/content/parts':
            self.has_body = _has_body({'parts': value})
        elif path == '/message/content/parts/0':
            nonempty = isinstance(value, str) and bool(value.strip())
            self.has_body = self.has_body or nonempty if operation == 'append' else nonempty

    def observe(self, event):
        if not is_complete_event(event):
            self.failed = True
            return
        if extract_data(event) == '[DONE]':
            self.done = True
        payload = extract_data_json(event)
        if not isinstance(payload, dict):
            return
        if payload.get('error') or payload.get('type') == 'error':
            self.failed = True
        if 'message' in payload:
            self._snapshot(payload['message'])
        elif 'v' in payload:
            self._delta(payload)

    def finish(self, delivered):
        """End the response lifetime: settled only if ``completed and delivered``.

        ``completed`` is the answer-shape evidence above (``assistant_complete``:
        the turn's own answer frame) plus the upstream's terminal frame;
        ``delivered`` is the response lifetime's own record that the whole ASGI
        body reached the client. A browser that disconnects -- or a send that
        fails -- after the answer was produced but before the response finished
        never received a complete reply, and a reply nobody received is not a
        consumed generation. This is the rule the /v1 lifetime already applies
        (``utils.trials.TrialAttempt``: "只有「本次生成确实完成」且「客户端确实收到了
        完整响应」同时成立才结算"), and the two ledgers must not disagree about
        the same turn.

        Settled once: the response wrapper and the pre-response failure path
        both funnel here, and settling twice would hand out two slots (or
        charge one reservation twice).
        """
        if self.settled:
            return
        self.settled = True
        if delivered and self.assistant_complete and self.done and not self.failed:
            trials.settle(self.reservation, self.seed)
        else:
            trials.release(self.reservation, self.seed)


class _Feedback:
    """How one admitted generation ended, in a form the guard can act on.

    Holds no body, header, identifier or credential: ``marker`` is restricted to
    a fixed protocol token, ``network_kind`` to the circuit enum. Everything
    else is a status code or a bounded label.
    """

    __slots__ = ("status", "raised", "stream", "terminal", "network_kind", "marker",
                 "error_frame", "sniffed", "reported")

    def __init__(self):
        self.status = 0            # 0 = the handler never reached a response
        self.raised = False        # status came from a raised HTTPException
        self.stream = "pending"    # pending | complete | failed | cancelled
        self.terminal = False      # the upstream's own end-of-stream marker was seen
        self.network_kind = ""
        self.marker = ""
        self.error_frame = False
        self.sniffed = 0
        self.reported = False

    def observe_body(self, body):
        """Bounded sniff of a response body prefix; keeps only a fixed marker."""
        remaining = _SNIFF_LIMIT - self.sniffed
        if remaining <= 0 or self.marker or not isinstance(body, (bytes, bytearray, memoryview)):
            return
        chunk = bytes(body[:remaining])
        self.sniffed += len(chunk)
        low = chunk.lower()
        for signal in _BODY_SIGNALS:
            if signal.encode() in low:
                self.marker = signal
                return

    def observe_event(self, event):
        if self.status >= 400:
            self.observe_body(event)
        if not isinstance(event, (bytes, bytearray)):
            return
        if not self.terminal and b'[DONE]' in event:
            # The upstream's end-of-stream frame, and the same marker the trial
            # account is settled on (``_Trial.finish`` requires it, so a stream
            # without one never charges a trial). The byte-test keeps the
            # ordinary event free, the parse keeps a literal "[DONE]" inside a
            # quoted message from counting, and ``is_complete_event`` keeps a
            # frame truncated at EOF from counting.
            if is_complete_event(event) and extract_data(event) == '[DONE]':
                self.terminal = True
        if self.error_frame:
            return
        if _ERROR_FRAME_MARKER not in event:
            return
        payload = extract_data_json(event)
        # "200 + an error frame" is a shape the upstream really produces. It must
        # not count as success, or one failed turn would reset the backoff.
        if isinstance(payload, dict) and (payload.get('error') or payload.get('type') == 'error'):
            self.error_frame = True

    def observe_exception(self, exc):
        kind = circuit.network_kind_from_exception(exc)
        if kind and not self.network_kind:
            self.network_kind = kind
        status = _http_status(exc)
        if status and not self.status:
            self.status, self.raised = status, True


def _http_status(exc):
    """Status of the outermost HTTPException in an exception (group); 0 if none."""
    if isinstance(exc, HTTPException):
        return exc.status_code
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            status = _http_status(inner)
            if status:
                return status
    return 0


async def admit_generation(request, account_token, seed=""):
    ctx = await guard.acquire_context(account_token)
    request.state.generation_admission = ctx
    request.state.generation_feedback = _Feedback()
    error = guard.admission_error(ctx)
    if error:
        raise error
    try:
        reservation = trials.reserve(seed)
    except trials.TrialDenied:
        raise HTTPException(402, 'Plus trial unavailable; choose a subscription') from None
    except store.StoreError:
        raise HTTPException(503, 'Trial accounting temporarily unavailable') from None
    if reservation is not None:
        request.state.generation_trial = _Trial(reservation, seed)


def _feedback_for(request):
    """The feedback record for this request, if it was an admitted generation.

    ``observe_generation_stream`` wraps *every* streamed upstream response, and
    only chat turns are admitted; polling and status streams share the same
    wrapper and legitimately have no admission to report on.
    """
    return getattr(request.state, "generation_feedback", None)


def observe_generation_event(request, event):
    trial = getattr(request.state, "generation_trial", None)
    if trial is not None:
        trial.observe(event)
    feedback = _feedback_for(request)
    if feedback is not None:
        feedback.observe_event(event)


async def observe_generation_stream(request, iterator):
    feedback = _feedback_for(request)
    completed = False
    try:
        async for event in iterator:
            observe_generation_event(request, event)
            yield event
        completed = True
    except (GeneratorExit, asyncio.CancelledError):
        # The client went away (or Starlette tore the body task down). That is
        # the user's decision, not evidence about the account.
        if feedback is not None:
            feedback.stream = "cancelled"
        raise
    except BaseException as exc:
        # A mid-stream upstream failure is invisible from the outside: the
        # response already committed 200, so this branch is the only place the
        # failure becomes observable at all.
        if feedback is not None:
            feedback.stream = "failed"
            feedback.observe_exception(exc)
        raise
    finally:
        if completed and feedback is not None:
            # "the iterator ran out without raising" -- not yet "delivered".
            # Whether the upstream sent its terminal frame is a separate fact
            # the event observer records.
            feedback.stream = "complete"


def observe_generation_end(request, outcome, exc=None):
    """Publish how an admitted generation's body iterator ended.

    ``generation_lifetime`` sees only the *response* object's lifetime, which
    cannot distinguish a stream that delivered its terminal frame from one that
    stopped after the first chunk: both are a 2xx response that returned. A
    route that owns its iterator (``f_conversation``) therefore has to say how
    it ended, or the record stays ``pending`` for the whole request and the
    guard is left guessing.

    Called from the iterator's own ``finally``, so every exit the iterator
    itself observes -- exhaustion, a raised exception, the cancellation or
    ``GeneratorExit`` of a client that went away -- is published synchronously.
    One teardown is out of its reach: Starlette never closes a body iterator, so
    a generator abandoned while suspended in ``send()`` may be finalized only
    later. Its record then stays ``pending`` and nothing is reported, which is
    the safe direction -- an unreported turn is a missing pacing hint, never a
    false success.

    ``outcome`` is the route's own exit verdict (``complete`` / ``failed`` /
    ``cancelled``); anything else is ignored, leaving the stream unclassified
    and never successful. ``exc`` is optional and is reduced by
    ``observe_exception`` to the circuit's enum and a status code -- never text.
    """
    feedback = _feedback_for(request)
    if feedback is None:
        return
    if outcome in _STREAM_END_STATES:
        feedback.stream = outcome
    if exc is not None:
        feedback.observe_exception(exc)


def track_generation_client(request, client):
    """Register upstream ownership before awaiting network work."""
    if request.state.generation_admission is not None:
        clients = request.state.generation_clients
        if not any(existing is client for existing in clients):
            clients.append(client)


async def _report_feedback(feedback, ctx):
    """Tell the antiban guard how this request ended, at most once.

    Never raises and never records a body. Whether antiban is on is the guard's
    own decision (``ctx.enabled``): the entry point stays unconditional, as the
    pre-existing call sites were, so "the switch is off" cannot silently mean
    "no call happens", which is invisible in both tests and metrics.
    """
    if feedback is None or ctx is None or feedback.reported:
        return
    feedback.reported = True
    if ctx.admission_denied:
        # A denial is the guard's own verdict (already counted in the anonymous
        # denial stats). Feeding it back as an upstream error would let a 503
        # cooldown denial be classified as an upstream 5xx, extending the very
        # cooldown that caused it.
        return
    try:
        await _dispatch_feedback(feedback, ctx)
    except Exception as exc:
        # Feedback must not disturb response teardown, and an exception's text
        # (which can carry upstream fragments) must not reach the log.
        logger.error(f"[generation] antiban feedback failed: {type(exc).__name__}")


async def _dispatch_feedback(feedback, ctx):
    # 1. Transport evidence wins: a connect/timeout/SSL/DNS signature in the
    #    exception chain, or this gateway's fixed "hop did not complete" status.
    kind = feedback.network_kind or _NETWORK_STATUS.get(feedback.status, "")
    if kind:
        await guard.report_network_error(ctx, kind)
        return
    # 2. An upstream refusal of this one account -> the circuit's own classifier.
    allowed = _RAISED_STATUS if feedback.raised else _RETURNED_STATUS
    if feedback.status and (feedback.marker or feedback.status in allowed):
        await guard.report_error(ctx, feedback.status,
                                 feedback.marker or _STATUS_SIGNAL.get(feedback.status))
        return
    # 3. A fully delivered 2xx: the iterator ended cleanly *and* the upstream
    #    sent its end-of-stream marker, with no error frame. That is the only
    #    honest evidence for "reset backoff / mark used"; a stream that merely
    #    stopped -- truncated, cancelled, or missing its terminal frame -- is
    #    reported as nothing at all rather than as a turn that happened.
    if (200 <= feedback.status < 300 and not feedback.error_frame
            and feedback.stream in _COMPLETE_STREAMS and feedback.terminal):
        await guard.report_success(ctx)


async def _release(ctx, clients, reusable, trial=None, delivered=False, feedback=None):
    # Starlette cancels response tasks on disconnect. Cleanup must finish before
    # capacity is returned, or the upstream may still run under a freed lease.
    try:
        with anyio.CancelScope(shield=True):
            for client in clients:
                try:
                    await (client.close() if reusable else client.discard())
                except Exception as exc:
                    logger.error(f"[generation] cleanup failed: {type(exc).__name__}")
            if trial is not None:
                try:
                    trial.finish(delivered)
                except store.StoreError:
                    # Keep a failed settlement reserved rather than silently
                    # treating it as paid or granting unlimited new requests.
                    logger.error('[generation] trial settlement unavailable')
            # Report before the lease is handed back: the guard's verdict belongs
            # to this request, and the slot may be reused the moment it is free.
            await _report_feedback(feedback, ctx)
    finally:
        guard.release_context(ctx)


class _AdmittedResponse(Response):
    def __init__(self, response, ctx, clients, trial, feedback):
        super().__init__(status_code=response.status_code)
        self.raw_headers = response.raw_headers
        self.response = response
        self.ctx = ctx
        self.clients = clients
        self.trial = trial
        self.feedback = feedback

    async def __call__(self, scope, receive, send):
        body_sent = False
        feedback = self.feedback
        if feedback is not None:
            feedback.status = self.status_code

        async def observe_send(message):
            nonlocal body_sent
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                body_sent = True

        try:
            if self.status_code >= 400 and feedback is not None:
                # 只在错误响应上嗅探固定协议标记（例如 CF 挑战页、账号停用 JSON），
                # 正文本身不外传；2xx 的正常生成内容不做任何扫描。
                feedback.observe_body(getattr(self.response, 'body', None))
            await self.response(scope, receive, observe_send)
        finally:
            # ``delivered`` reads the same as the /v1 lifetime's: the whole ASGI
            # body reached the client on a 2xx response. A browser that hangs up
            # before the final body message, or a send that fails, leaves it
            # False and the trial is handed back rather than charged.
            await _release(self.ctx, self.clients, reusable=body_sent, trial=self.trial,
                           delivered=body_sent and 200 <= self.status_code < 300,
                           feedback=feedback)


def generation_lifetime(handler):
    @wraps(handler)
    async def wrapped(request, *args, **kwargs):
        request.state.generation_admission = None
        request.state.generation_clients = []
        request.state.generation_trial = None
        request.state.generation_feedback = None
        try:
            response = await handler(request, *args, **kwargs)
        except BaseException as exc:
            # Covers every exit that never produces a response: a raised
            # HTTPException from the route, an unexpected error, and the
            # cancellation task of a client that disconnected mid-handler.
            ctx = request.state.generation_admission
            if ctx is not None:
                feedback = request.state.generation_feedback
                if feedback is not None:
                    feedback.observe_exception(exc)
                await _release(ctx, request.state.generation_clients, reusable=False,
                               trial=request.state.generation_trial, feedback=feedback)
            raise
        ctx = request.state.generation_admission
        return _AdmittedResponse(response, ctx, request.state.generation_clients,
                                 request.state.generation_trial,
                                 request.state.generation_feedback) if ctx is not None else response
    return wrapped
