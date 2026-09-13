"""Deep Research progress: what the mirror retains while a turn is running.

Scope and honest limits
-----------------------
The official research progress protocol is **not** captured.  What is captured
(see ``tmp/research-protocol/OFFICIAL_INTERFACE_EVIDENCE.md``) is the set of
endpoints the real frontend polls, the fact that a real turn ends with a
``[DONE]`` terminal, and the incremental-encoding contract the gateway already
negotiates through ``supported_encodings``.  Everything below therefore keys off
*structure that was actually observed*, and treats anything else as unknown:

* an event the mirror cannot classify is forwarded untouched and counted under a
  fingerprint of its field names -- it is never renamed into an invented phase,
  and never silently dropped;
* progress is retained per conversation, under the Seed that ran the turn, so a
  browser that reloads can ask the mirror instead of re-running the turn;
* retention is bounded in three dimensions (events, bytes, conversations) and
  what survives the bound is the newest state plus the terminal event;
* the assistant's own visible answer is projected as bounded text, because the
  official research region does not render it in a normal browser (measured on
  a real turn) -- see the report section below for what may and may not be
  shown;
* both encodings the frontend can negotiate are read: whole message snapshots
  (the shape the 2026-09-12 capture contains) and the incremental ``{p, o, v}``
  patches the new frontend renders from (see the incremental section below --
  the negotiation contract is documented, the delta *bytes* are not captured);
* the terminal marker reaches the browser exactly once per turn.

Nothing here claims a research turn *succeeded*.  A synthetic stream proves the
gateway's retention and isolation, never an upstream feature.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import utils.globals as globals
from app import app
from gateway.reverseProxy import resolve_seed_token
from gateway.sse_parser import extract_data
from utils.configs import chat_request_timeout
from utils.Logger import logger

# --- event kinds ------------------------------------------------------------
# Named after the *structure* that was observed, not after a product phase.
#
# The families below are the ones a real Plus Deep Research turn actually
# produced through the mirror on 2026-09-12 (23 events, field names and shapes
# retained in tests/fixtures/deep_research_plus_shapes.json).  A family is added
# here only when the capture contains it; anything else stays OTHER_JSON and is
# accounted for without being renamed.
KIND_TERMINAL = "terminal"                  # data buffer is exactly [DONE]
KIND_RESUME_TOKEN = "resume_token"          # legacy-endpoint artefact
KIND_MESSAGE_DELTA = "message_delta"        # incremental patch: p / o / v
KIND_MESSAGE_SNAPSHOT = "message_snapshot"  # full message object
KIND_INPUT_MESSAGE = "input_message"        # request echo of the user turn
KIND_TITLE_GENERATION = "title_generation"  # conversation title frame
KIND_MESSAGE_MARKER = "message_marker"      # marker/event pair on a message id
KIND_ASYNC_STATUS = "async_status"          # background-task status frame
KIND_STE_METADATA = "ste_metadata"          # per-turn server metadata frame
KIND_STREAM_COMPLETE = "stream_complete"    # upstream says the stream is done
KIND_URL_MODERATION = "url_moderation"      # link moderation frame
KIND_OTHER_JSON = "other_json"              # a JSON object we do not claim to know
KIND_NON_JSON = "non_json"                  # data that is not a JSON object

# Observed upstream ``type`` values -> the family they belong to.  Every key
# here appears in the real capture; a ``type`` that is not listed stays
# OTHER_JSON so a future upstream release cannot be silently mislabelled.
OBSERVED_TYPE_KINDS = {
    "input_message": KIND_INPUT_MESSAGE,
    "title_generation": KIND_TITLE_GENERATION,
    "message_marker": KIND_MESSAGE_MARKER,
    "conversation_async_status": KIND_ASYNC_STATUS,
    "server_ste_metadata": KIND_STE_METADATA,
    "message_stream_complete": KIND_STREAM_COMPLETE,
    "url_moderation": KIND_URL_MODERATION,
}
OBSERVED_EVENT_TYPES = frozenset(OBSERVED_TYPE_KINDS)

# The role whose message may speak for the turn.  Every message object in the
# capture carries ``author.role``; the user's own echo carries ``user``.
ASSISTANT_ROLE = "assistant"

TERMINAL_DATA = "[DONE]"
TERMINAL_STATES = frozenset({"complete", "cancelled", "failed"})

# An SSE comment frame.  Comments are part of the SSE spec, so every parser
# ignores them; they exist to keep an idle connection alive through
# intermediaries (nginx, browsers) that close on silence.
HEARTBEAT_FRAME = b": keep-alive\n\n"

DEFAULT_MAX_EVENTS = 512
DEFAULT_MAX_CONVERSATIONS = 64
DEFAULT_MAX_BYTES = 512 * 1024
DEFAULT_MAX_EVENT_BYTES = 4096
DEFAULT_RECORD_TTL = 6 * 60 * 60

_TRUNCATION_MARKER = " ...<truncated>"
_FINGERPRINT_FIELD_LIMIT = 24
_FINGERPRINT_FIELD_CHARS = 48

# Research turns idle for minutes between steps while the measured upstream
# silence tolerance is roughly `timeout + 6s` (see the evidence note), so the
# ordinary chat budget kills a healthy research turn.  A research turn therefore
# gets its own *bounded* default rather than the chat budget, and the knob below
# still overrides it outright.
RESEARCH_TIMEOUT_ENV = "CHAT_RESEARCH_TIMEOUT"
# Five minutes of tolerated upstream silence: ten times the shipped chat default
# (``CHAT_REQUEST_TIMEOUT`` = 30), never below whatever an operator configured for
# chat.  Bounded on purpose: this is a low-speed watchdog, not a turn deadline,
# so it only ever decides how long a stream may be *completely* quiet before the
# connection is treated as dead -- a stream that keeps talking is never cut by
# it.  Long enough for the minutes-long gaps a real research turn has, short
# enough that a genuinely dead upstream is still noticed promptly.
DEFAULT_RESEARCH_TIMEOUT = 300
_RESEARCH_MODEL_MARKERS = ("deep-research", "deepresearch")
_RESEARCH_MODEL_SLUGS = {"research"}
_RESEARCH_SYSTEM_HINTS = {
    "research",
    # Observed from the real Plus frontend (2026-09-12).  New builds route the
    # first-party Deep Research connector through a plugin-qualified hint while
    # keeping model=gpt-5-6-thinking.
    "plugin:connector_openai_deep_research",
}


def anon_id(value: str) -> str:
    """Stable anonymous identifier: distinguishable, not reversible."""
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else "-"


def classify(event: bytes):
    """Return ``(kind, payload)`` for one complete SSE event.

    ``kind`` is ``None`` for a frame that carries no data at all (a comment or a
    heartbeat) -- those are not progress and must not be counted as such.
    ``payload`` is the decoded JSON object when the data buffer is one.

    Total by construction: a truncated body, a non-object body or a frame the
    upstream cut mid-write all resolve to a kind rather than an exception.  A
    research turn's value is its long tail, so one unparseable frame must not be
    able to end the caller's loop.
    """
    try:
        data = extract_data(event)
    except Exception:
        return KIND_NON_JSON, None
    if data is None:
        return None, None
    payload_text = data.strip()
    if payload_text == TERMINAL_DATA:
        return KIND_TERMINAL, None
    if not payload_text.startswith("{"):
        return KIND_NON_JSON, None
    try:
        payload = json.loads(payload_text)
    except Exception:
        return KIND_NON_JSON, None
    if not isinstance(payload, dict):
        return KIND_NON_JSON, None
    return kind_of(payload), payload


def kind_of(payload: dict) -> str:
    """Classify one already-decoded object by structure, most specific first."""
    if not isinstance(payload, dict):
        return KIND_NON_JSON
    event_type = payload.get("type")
    if event_type == "resume_conversation_token":
        return KIND_RESUME_TOKEN
    if isinstance(event_type, str) and event_type in OBSERVED_TYPE_KINDS:
        return OBSERVED_TYPE_KINDS[event_type]
    if _is_incremental_patch(payload):
        return KIND_MESSAGE_DELTA
    if _is_message_snapshot(payload):
        return KIND_MESSAGE_SNAPSHOT
    return KIND_OTHER_JSON


def _is_incremental_patch(payload: dict) -> bool:
    """One ``{p, o, v}`` patch, or the batched ``{c, o, v: [{p, o, v}, ...]}``.

    The earlier version only recognised the single-patch form, so every batched
    frame -- half of a real turn -- fell into the unknown bucket.  Both forms
    carry the same three observed field names; only the envelope differs.
    """
    if "p" in payload and "o" in payload:
        return True
    if "o" not in payload:
        return False
    batch = payload.get("v")
    return (isinstance(batch, list) and bool(batch)
            and all(isinstance(item, dict) and "p" in item and "o" in item
                    for item in batch))


def _is_message_snapshot(payload: dict) -> bool:
    """A frame carrying a whole message object, in either observed envelope.

    ``{"v": {"message": {...}}}`` is the shape a real Plus turn spends almost
    all of its frames on; ``{"type": "message", "message": {...}}`` is the
    legacy envelope the same upstream uses on the old endpoint.
    """
    if payload.get("type") == "message" and isinstance(payload.get("message"), dict):
        return True
    value = payload.get("v")
    return isinstance(value, dict) and isinstance(value.get("message"), dict)


def is_terminal(event: bytes) -> bool:
    return classify(event)[0] == KIND_TERMINAL


def structure_fingerprint(payload: dict) -> str:
    """Identify an unclassified event by its field names, never its values.

    This is what lets a future real capture be diffed against what the mirror
    actually saw, without this module inventing a phase name and without
    persisting upstream content.
    """
    fields = sorted(payload.keys())[:_FINGERPRINT_FIELD_LIMIT]
    material = "|".join(field[:_FINGERPRINT_FIELD_CHARS] for field in fields)
    return hashlib.sha1(material.encode("utf-8", errors="replace")).hexdigest()[:12]


def conversation_id_of(payload: dict):
    """Conversation id from the two positions the upstream is known to use."""
    if not isinstance(payload, dict):
        return None
    conversation_id = payload.get("conversation_id")
    value = payload.get("v")
    if isinstance(value, dict):
        conversation_id = conversation_id or value.get("conversation_id")
    return conversation_id if isinstance(conversation_id, str) and conversation_id else None


# ---------------------------------------------------------------------------
# Projection: what the browser is allowed to learn from one upstream event
# ---------------------------------------------------------------------------
# The real Plus turn streamed no plan, no stage list and no completion ratio,
# so there is nothing here to derive one from.  The UI therefore has no concept
# of "step 2 of 5" -- only "the most recent activity upstream actually
# reported", each label hanging off one observed field.

# Labels keyed by observed upstream ``type`` values.  One label per observation:
# they are never ordered into a sequence, because the capture gives no ordering.
OBSERVED_ACTION_LABELS = {
    "conversation_async_status": "研究任务正在后台运行",
    "input_message": "正在整理研究问题",
    "message_marker": "正在推进研究任务",
    "server_ste_metadata": "研究任务已连接",
    "title_generation": "正在生成研究标题",
    "url_moderation": "正在检查来源链接",
    "message_stream_complete": "正在完成研究报告",
}

ACTION_WAITING = "等待上游事件"
ACTION_STARTING = "正在启动研究"
ACTION_SOURCES = "正在检索网络资料"
ACTION_ANALYSING = "正在分析资料"
ACTION_WRITING = "正在撰写研究报告"
ACTION_DONE = "研究已完成"
ACTION_CANCELLED = "研究已取消"
ACTION_FAILED = "研究失败"

# The browser-facing projection is a closed set.  Anything that is not a key
# here cannot reach the panel, which is what keeps an upstream-controlled value
# from leaking by accident.
PROJECTION_KEYS = frozenset({
    "action",              # one label, from the closed sets above
    "family",              # the classified family of the newest event
    "tool",                # observed tool token, or ""
    "markers",             # observed marker tokens, bounded and shape-checked
    "content_types",       # observed content_type tokens, bounded
    "sources",             # distinct evidenced source identifiers
    "sources_evidenced",   # upstream listed sources at all (count may be 0)
    "urls_moderated",      # count of url_moderation frames
    "research_confirmed",  # deep_research_version metadata was observed
    "report",              # the assistant's own visible answer text, bounded
    "report_final",        # that text came from the frame that ended the turn
    "report_truncated",    # the answer was longer than the bound
    "started_at",          # epoch seconds the turn began
    "elapsed_ms",          # frozen once the turn is terminal
    "finished_at",         # epoch seconds the terminal signal was observed
    "finished",            # bool
})

# ``terminal_state`` is consumed by the store to freeze the record and
# ``source_digests`` is the store's dedupe set; neither belongs in the
# browser-facing projection, so both live outside ``PROJECTION_KEYS``.
EVENT_PROJECTION_KEYS = PROJECTION_KEYS | {"terminal_state", "source_digests"}

MAX_MARKERS = 4
MAX_CONTENT_TYPES = 8

# Source containers and value keys, all of them observed on the real turn.  A
# count is only ever produced from a value found inside one of these
# containers, so a coincidental ``url`` key elsewhere cannot inflate it.
_SOURCE_CONTAINER_KEYS = {
    "content_references", "citations", "search_result_groups", "selected_sources",
    "caterpillar_selected_sources", "selected_mcp_sources",
}
_SOURCE_VALUE_KEYS = {"url", "resource_uri", "resolved_pineapple_uri"}

# Walk bounds.  A frame's payload is upstream-controlled and the final answer
# frame is the largest one, so the walk may not be linear in payload size.
MAX_SOURCE_DEPTH = 12
MAX_SOURCE_KEYS = 128
MAX_SOURCE_ENTRIES = 256
MAX_SOURCE_NODES = 20000

# A protocol token: no whitespace, no prose, no path separator -- so neither a
# prompt nor a URL can match it, and anything admitted is safe to render.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")


def _token(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if _TOKEN_RE.match(value) else ""


# ---------------------------------------------------------------------------
# The report: the assistant's own visible answer, bounded
# ---------------------------------------------------------------------------
# Measured on a real turn (2026-09-13): the request was a research turn, the
# upstream stream completed, and the browser still rendered no answer -- because
# the official bundle draws its own research region for this message and that
# region is keyed on a model identifier a normal browser cannot resolve, so the
# region resolved to a browser error page.  The answer itself was already in the
# stream the mirror forwarded.  This projection is where it becomes visible.
#
# The answer body is the observed ``content`` of an assistant message: ``text``
# (the answer frame, fixture index 12) or a ``parts`` array of strings (every
# other message frame in the capture).  What may be shown is decided only by
# evidence the capture contains:
#
# * only an assistant-authored message may speak -- the user's own question is
#   replayed upstream as an ``input_message`` with a body of its own, and it is
#   not the answer;
# * a message the capture marks ``is_visually_hidden_from_conversation`` is
#   internal and is not shown;
# * reasoning is not the answer -- the official UI does not show the model's
#   ``thoughts`` either;
# * a tool node's payload is not the answer (``web.run`` was the observed tool
#   recipient, and tool frames carry ``invoked_resource`` / ``invoked_plugin``);
# * the body is bounded and stripped of control characters, so one upstream
#   frame cannot make the panel unbounded or unrenderable.
#
# Nothing here invents an answer: no frame carries one, the projection carries
# an empty string, and the panel says so in words.
MAX_REPORT_CHARS = 6000
MAX_REPORT_PARTS = 64
# What is *built* before bounding, so an overflow is still visible to the bound.
_REPORT_BUILD_LIMIT = MAX_REPORT_CHARS * 2
_REPORT_TRUNCATION = "\n\n[报告内容过长，此处已截断]"

# Content types whose body is not the user-facing answer.  Both values are
# observed under the capture's ``content_type`` key.  The comparison uses the
# raw value rather than ``_token``: a protocol token never contains ``_``, so
# ``_token`` would silently drop ``reasoning_recap`` instead of excluding it.
NON_REPORT_CONTENT_TYPES = frozenset({"thoughts", "reasoning_recap"})

# A tool recipient is namespace-qualified (``web.run`` is the observed one).
_TOOL_RECIPIENT_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_TOOL_METADATA_KEYS = ("invoked_resource", "invoked_plugin")

# Everything a terminal or a browser would act on, minus the two whitespace
# characters a report legitimately uses.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _normalised_content_type(content) -> str:
    value = content.get("content_type") if isinstance(content, dict) else None
    return value.strip().lower() if isinstance(value, str) else ""


def _is_hidden_message(metadata) -> bool:
    """The capture's own "do not show this one" flag."""
    return (isinstance(metadata, dict)
            and metadata.get("is_visually_hidden_from_conversation") is True)


def _is_tool_frame(recipient: str, metadata) -> bool:
    """Whether this message is a tool node rather than the assistant's answer."""
    if recipient and _TOOL_RECIPIENT_RE.match(recipient):
        return True
    if not isinstance(metadata, dict):
        return False
    return any(metadata.get(key) for key in _TOOL_METADATA_KEYS)


def _report_body(message) -> str:
    """The visible body of one message, or "" when it has none.

    Bounded while it is built, not after: a ``parts`` array is upstream-sized,
    and the join must not run over an unbounded list to then throw it away.  The
    build limit is deliberately twice the render limit, so a body that overflows
    still *looks* like one to ``_bound_report`` after control characters are
    stripped, and the truncation is reported instead of being silently lost.
    """
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, dict):
        return ""
    if _normalised_content_type(content) in NON_REPORT_CONTENT_TYPES:
        return ""
    text = content.get("text")
    if isinstance(text, str) and text.strip():
        return text[:_REPORT_BUILD_LIMIT]
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    collected, total = [], 0
    for part in parts[:MAX_REPORT_PARTS]:
        if not isinstance(part, str) or not part:
            continue
        collected.append(part)
        total += len(part)
        if total > _REPORT_BUILD_LIMIT:
            break
    return "\n".join(collected)


def _bound_report(text: str):
    """Sanitise and bound one report body. Returns ``(text, truncated)``."""
    cleaned = _CONTROL_CHARS_RE.sub(
        "", text.replace("\r\n", "\n").replace("\r", "\n"))
    if len(cleaned) <= MAX_REPORT_CHARS:
        return cleaned, False
    return cleaned[:MAX_REPORT_CHARS].rstrip() + _REPORT_TRUNCATION, True


def _message_of(payload):
    """The message object, from the positions the capture shows it in.

    Three observed envelopes carry one: ``{"v": {"message": ...}}`` on the
    incremental frames, ``{"type": "message", "message": ...}`` on the legacy
    one, and ``{"input_message": ...}`` on the request echo.
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get("v")
    candidates = (payload.get("input_message"), payload.get("message"),
                  value if isinstance(value, dict) else None)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        nested = candidate.get("message")
        if isinstance(nested, dict):
            return nested
        if "content" in candidate or "author" in candidate:
            return candidate
    return None


def _has_error(payload) -> bool:
    """Whether the frame carries an upstream error, without reading its text.

    ``error`` / ``error_code`` are observed keys, null on every healthy frame of
    the capture.  Only their presence is used; their values are upstream body
    fragments and are never copied anywhere.
    """
    def has_error_fields(candidate):
        if not isinstance(candidate, dict):
            return False
        for key in ("error", "error_code"):
            value = candidate.get(key)
            if isinstance(value, str):
                if value.strip():
                    return True
            elif value not in (None, False, 0, [], {}):
                return True
        return False

    if not isinstance(payload, dict):
        return False
    if has_error_fields(payload):
        return True

    value = payload.get("v")
    # A single patch's value is an envelope only when it is an object.  A list
    # here is commonly a source/reference array and must not be inspected.
    if isinstance(value, dict) and has_error_fields(value):
        return True

    # The batched form is the one protocol list whose members are operations.
    # Inspect the operation object and a dictionary operation value, but never
    # recurse into arbitrary list values such as content references.
    if payload.get("o") == "patch" and isinstance(value, list):
        for operation in value[:MAX_PATCH_OPS]:
            if not isinstance(operation, dict):
                continue
            if has_error_fields(operation):
                return True
            operation_value = operation.get("v")
            if isinstance(operation_value, dict) and has_error_fields(operation_value):
                return True
    return False


def _collect_sources(payload):
    """Return ``(digests, evidenced)`` for one frame.

    ``digests`` are hashes, so a URL never survives the call.  ``evidenced``
    records whether upstream listed sources *at all*: "upstream reported an
    empty list" and "no source list has been seen yet" are different statements
    and the panel shows different words for them.
    """
    found = set()
    state = {"evidenced": False, "budget": MAX_SOURCE_NODES, "keys": MAX_SOURCE_KEYS,
             "entries": MAX_SOURCE_ENTRIES}
    _walk_sources(payload, False, 0, found, state)
    return found, state["evidenced"]


def _walk_sources(node, active, depth, found, state):
    if state["budget"] <= 0 or depth > MAX_SOURCE_DEPTH:
        return
    state["budget"] -= 1
    if isinstance(node, dict):
        for key, value in list(node.items())[:MAX_SOURCE_KEYS]:
            if key in _SOURCE_CONTAINER_KEYS:
                state["evidenced"] = True
            child_active = active or key in _SOURCE_CONTAINER_KEYS
            if child_active and key in _SOURCE_VALUE_KEYS and isinstance(value, str) and value:
                found.add(hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest())
            _walk_sources(value, child_active, depth + 1, found, state)
    elif isinstance(node, list):
        for value in node[:MAX_SOURCE_ENTRIES]:
            _walk_sources(value, active, depth + 1, found, state)


# ---------------------------------------------------------------------------
# The incremental family: patches instead of whole messages
# ---------------------------------------------------------------------------
# ``rewrite_f_conversation_body`` forwards the frontend's ``supported_encodings``
# to the legacy endpoint, so the browser's turn is answered with the delta
# encoding that frontend renders from::
#
#     {"p": "/message/content/parts/0", "o": "append", "v": "..."}
#     {"o": "patch", "v": [{"p": ..., "o": ..., "v": ...}, ...], "c": 3}
#
# Read as whole messages, every one of those frames is an anonymous patch: the
# answer they build and the source containers they add are invisible, and a real
# research turn would reach the panel as "upstream sent no body" while the
# browser had already been handed the entire answer -- the exact failure the
# report projection exists to prevent.
#
# The fold below is deliberately *not* a JSON-Pointer implementation.  It
# recognises the paths this protocol uses and ignores every other one, so an
# unknown path can never write into the state; it stays observable as an event
# and contributes no claim.  The reconstructed message is then handed to
# ``project_event`` unchanged, which keeps one set of rules deciding what may be
# shown: assistant-authored only, hidden/tool/reasoning excluded, bounded body,
# and completion only on upstream's own end-of-turn evidence.
#
# Reconstructed state is per turn (reset in ``begin``), bounded in slots and in
# characters, and never leaves the server except as the count/label projection.
_PATCH_OPS = frozenset({"add", "replace", "append", "remove"})
MAX_PATCH_OPS = 64
MAX_DELTA_PARTS = 64
_MAX_DELTA_TOKEN_CHARS = 64
_MESSAGE_PATH = "/message"
_MESSAGE_PATH_PREFIX = "/message/"
_PARTS_PATH = "content/parts"
_PARTS_PATH_PREFIX = "content/parts/"
_TEXT_PATH = "content/text"
_METADATA_PATH_PREFIX = "metadata/"
# Per-turn ceiling on distinct source identifiers.  The walk is bounded per
# frame, but the number of frames is not, so without a ceiling a long stream
# could grow the digest set without limit.  A turn that lists thousands of
# sources is out of any real range, and the ceiling only stops the count from
# growing -- it never removes what was already evidenced.
MAX_SOURCE_DIGESTS = 4096
# Message statuses that end a turn without completing it.  Only these are read
# off a patch, and only in the failure direction: completion still needs the
# full end-of-turn evidence.
_TERMINAL_STATUSES = {"failed": "failed", "cancelled": "cancelled",
                      "incomplete": "failed"}


def patch_operations(payload) -> list:
    """The bounded ``(path, op, value)`` list of one incremental frame.

    Both observed envelopes are read: the single patch (``p``/``o``/``v`` at the
    frame root) and the batch (``o`` plus ``v`` holding a list of patches).  A
    frame with neither shape yields no operations -- it is not a patch frame,
    and nothing downstream may read it as one.  Path and operator must be
    strings; a frame where either is not is not an operation.

    The batch form is only a batch when *every* item really is an operation: a
    single patch is free to have a list as its *value* (a source container
    replaced in one patch is exactly that), and reading that as a batch would
    silently discard the patch.
    """
    if not isinstance(payload, dict):
        return []
    path, op, batch = payload.get("p"), payload.get("o"), payload.get("v")
    if isinstance(batch, list) and isinstance(op, str) and batch:
        operations = [(item["p"], item["o"], item.get("v"))
                      for item in batch[:MAX_PATCH_OPS]
                      if isinstance(item, dict) and isinstance(item.get("p"), str)
                      and _is_operation(item.get("o"))]
        if len(operations) == min(len(batch), MAX_PATCH_OPS):
            return operations
    if isinstance(path, str) and isinstance(op, str):
        return [(path, op, batch)]
    return []


def _is_operation(op) -> bool:
    """Whether a value names one of the protocol's patch operators."""
    return isinstance(op, str) and op.strip().lower() in _PATCH_OPS


def new_delta_state() -> dict:
    """Empty per-turn state for the message an incremental turn is building."""
    return {
        # The message this state describes.  A turn can build several in a row
        # (a tool submessage, then the answer), and everything below describes
        # one of them, so a new id resets the message-scoped half of the state.
        "message_id": "",
        "role": "",
        "content_type": "",
        "text": "",
        "parts": {},
        "chars": 0,
        "status": "",
        "end_turn": None,
        "recipient": "",
        "is_complete": False,
        "hidden": False,
        "terminal": "",
        # Frame-level error evidence is turn-scoped.  Keep only its presence,
        # never the upstream error text or code, so later completion patches
        # cannot overwrite a failure with a synthetic success.
        "error_observed": False,
        # Turn-scoped: these have already been folded into the record by the
        # time a message boundary is crossed, so a reset cannot lose them.
        "containers": set(),
        "digests": set(),
        "research_confirmed": False,
    }


def _reset_message_scope(state):
    """Forget the message being built; keep everything the turn already proved."""
    state.update({
        "role": "", "content_type": "", "text": "", "parts": {}, "chars": 0,
        "status": "", "end_turn": None, "recipient": "", "is_complete": False,
        "hidden": False, "terminal": "",
    })


def _put_part(state, index: int, text: str, append: bool):
    """Write one body slot, bounded in slot count and in total characters."""
    existing = state["parts"].get(index, "")
    if index not in state["parts"] and len(state["parts"]) >= MAX_DELTA_PARTS:
        return
    room = _REPORT_BUILD_LIMIT - (state["chars"] - len(existing))
    if room <= 0:
        return
    combined = (existing + text) if append else text
    combined = combined[:room]
    state["chars"] += len(combined) - len(existing)
    state["parts"][index] = combined


def _replace_parts(state, parts):
    """Replace the whole body from a parts array, clearing what was there.

    A parts array is the message declaring its own content, so a slot it does
    not fill is empty rather than "keep whatever the previous message left".
    """
    state["parts"] = {}
    state["chars"] = 0
    if not isinstance(parts, list):
        return
    for index, part in enumerate(parts[:MAX_DELTA_PARTS]):
        _put_part(state, index, part if isinstance(part, str) else "", append=False)


def _add_container(state, key: str, value):
    """Record that upstream listed sources, reducing them to digests at once."""
    state["containers"].add(key)
    if len(state["digests"]) >= MAX_SOURCE_DIGESTS:
        return
    digests, _ = _collect_sources({key: value})
    state["digests"].update(digests)


def seed_delta_state(state, message):
    """Fold a whole message object into the incremental state.

    The capture contains frames that carry a message envelope *and* patch fields
    in the same body; seeding first keeps those frames projected exactly as they
    were, with any patches applied on top of them.

    A message object that names a *different* message than the one being built
    starts a new one: without that, a tool submessage's ``recipient`` would
    still be in force when the answer's body arrived and the answer would be
    excluded as a tool payload, and the answer would be appended to the tool
    message's body.  Both are wrong in opposite directions, so the boundary is
    read from the protocol's own name for the message.

    The assumption this rests on, stated rather than implied: a message object
    that carries *no* id is folded into the message already being built, because
    the capture shows an ``id`` on every message object and there is no other
    way to tell a refresh from an introduction.  The failure direction is the
    safe one -- at worst an answer stays unshown, never misattributed.
    """
    if not isinstance(message, dict):
        return
    message_id = message.get("id")
    if isinstance(message_id, str) and message_id:
        if state["message_id"] and message_id != state["message_id"]:
            _reset_message_scope(state)
        state["message_id"] = message_id
    author = message.get("author") if isinstance(message.get("author"), dict) else {}
    role = _token(author.get("role"))
    if role:
        state["role"] = role
    if "recipient" in message:
        # Authoritative, including "no recipient": this is the message's own
        # object saying what it is.
        state["recipient"] = _token(message.get("recipient"))
    status = message.get("status")
    if isinstance(status, str) and status.strip():
        state["status"] = status.strip()[:_MAX_DELTA_TOKEN_CHARS]
        state["terminal"] = _TERMINAL_STATUSES.get(state["status"], "")
    if isinstance(message.get("end_turn"), bool):
        state["end_turn"] = message["end_turn"]
    content = message.get("content") if isinstance(message.get("content"), dict) else {}
    content_type = content.get("content_type")
    if isinstance(content_type, str) and content_type.strip():
        state["content_type"] = content_type.strip()[:_MAX_DELTA_TOKEN_CHARS]
    if isinstance(content.get("text"), str) and content["text"]:
        state["text"] = content["text"][:_REPORT_BUILD_LIMIT]
    if isinstance(content.get("parts"), list):
        _replace_parts(state, content["parts"])
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    for key in _SOURCE_CONTAINER_KEYS:
        if key in metadata:
            _add_container(state, key, metadata[key])
    if _is_hidden_message(metadata):
        state["hidden"] = True
    if metadata.get("is_complete") is True:
        state["is_complete"] = True
    if metadata.get("deep_research_version") not in (None, "", False):
        state["research_confirmed"] = True


def _apply_patch(state, path, op, value):
    """Fold one patch into the state; an unrecognised path writes nothing."""
    if not isinstance(path, str) or not isinstance(op, str):
        return
    op = op.strip().lower()
    if op not in _PATCH_OPS:
        return
    if path in ("", _MESSAGE_PATH):
        # ``/message`` is where a whole message object is introduced.
        if isinstance(value, dict):
            seed_delta_state(state, value)
        return
    if not path.startswith(_MESSAGE_PATH_PREFIX):
        return
    field = path[len(_MESSAGE_PATH_PREFIX):]
    if field == "content/content_type":
        if isinstance(value, str) and value.strip():
            state["content_type"] = value.strip()[:_MAX_DELTA_TOKEN_CHARS]
        return
    if field == _TEXT_PATH:
        if op == "remove":
            state["text"] = ""
        elif isinstance(value, str):
            # ``append`` concatenates, like the parts form does.
            combined = (state["text"] + value) if op == "append" else value
            state["text"] = combined[:_REPORT_BUILD_LIMIT]
        return
    if field == _PARTS_PATH:
        if op == "remove":
            state["parts"] = {}
            state["chars"] = 0
        elif isinstance(value, list):
            # Only a real array replaces the body: a malformed patch must not be
            # able to clear a body upstream already streamed.
            _replace_parts(state, value)
        return
    if field.startswith(_PARTS_PATH_PREFIX):
        index = field[len(_PARTS_PATH_PREFIX):]
        if not index.isdigit():
            return
        index = int(index)
        if index >= MAX_DELTA_PARTS:
            return
        if op == "remove":
            state["chars"] -= len(state["parts"].pop(index, ""))
        elif isinstance(value, str) and value:
            _put_part(state, index, value, append=(op == "append"))
        return
    if field == "status":
        if isinstance(value, str) and value.strip():
            state["status"] = value.strip()[:_MAX_DELTA_TOKEN_CHARS]
            # Only the failure direction is read here.  Completion still needs
            # upstream's full end-of-turn evidence -- and a *user* echo cannot
            # end a turn this way, because the echo names its author and the
            # projection excludes a non-assistant message from the pass below.
            state["terminal"] = _TERMINAL_STATUSES.get(state["status"], "")
        return
    if field == "end_turn":
        if isinstance(value, bool):
            state["end_turn"] = value
        return
    if field == "recipient":
        state["recipient"] = _token(value)
        return
    if field == "author/role":
        state["role"] = _token(value)
        return
    if field.startswith(_METADATA_PATH_PREFIX):
        key = field[len(_METADATA_PATH_PREFIX):]
        if key in _SOURCE_CONTAINER_KEYS:
            # A removal is not the same statement as a listing: "upstream
            # dropped its source list" must not read as "upstream listed none".
            if op != "remove":
                _add_container(state, key, value)
        elif key == "is_complete":
            if value is True:
                state["is_complete"] = True
        elif key == "is_visually_hidden_from_conversation":
            if value is True:
                state["hidden"] = True
        elif key == "deep_research_version":
            if value not in (None, "", False):
                state["research_confirmed"] = True


def synthesise_delta_message(state) -> dict:
    """The message object the incremental state adds up to.

    Handed to ``project_event`` so exactly the same rules decide what may be
    shown.  Container keys are synthesised as *presence markers* with no value:
    the digests were computed from the patch value as it arrived, never carried.
    """
    metadata = {}
    if state["containers"]:
        metadata.update({key: [] for key in sorted(state["containers"])})
    if state["hidden"]:
        metadata["is_visually_hidden_from_conversation"] = True
    if state["is_complete"]:
        metadata["is_complete"] = True
    if state["research_confirmed"]:
        # Presence marker only: this module never reads the version's value.
        metadata["deep_research_version"] = True
    message = {
        "author": {"role": state["role"]},
        "content": {"content_type": state["content_type"],
                    "parts": [state["parts"][index] for index in sorted(state["parts"])]},
        "status": state["status"],
        "recipient": state["recipient"],
        "metadata": metadata,
    }
    if state["text"]:
        # The capture's own answer frame carries the body here, so a turn that
        # patched it must be able to say so.
        message["content"]["text"] = state["text"]
    if state["end_turn"] is not None:
        message["end_turn"] = state["end_turn"]
    envelope = {"message": message}
    if state.get("error_observed"):
        # Anonymous sentinel consumed by _has_error(); the upstream value is
        # intentionally not retained or exposed.
        envelope["error"] = True
    return {"v": envelope}


def fold_delta(payload, state) -> dict:
    """Fold one incremental frame into ``state``; return the message it builds.

    Always returns a payload.  A frame the fold could not read adds nothing to
    the state, and the synthesised message then says nothing new -- which is why
    an unknown patch path can never become an activity claim or a body.
    """
    if not isinstance(state, dict) or not state:
        state = new_delta_state()
    seed_delta_state(state, _message_of(payload))
    for path, op, value in patch_operations(payload):
        _apply_patch(state, path, op, value)
    if _has_error(payload):
        state["error_observed"] = True
    return synthesise_delta_message(state)


def project_event(payload, kind=None) -> dict:
    """Project a real upstream event into credential-free UI state.

    Total: whatever the upstream sends, this returns a whitelisted dict.  Values
    are read only to select between closed sets of labels and to count sources;
    no upstream string is copied into the result except short protocol tokens
    that must match ``_TOKEN_RE`` to be admitted at all.
    """
    projection = {
        "action": ACTION_WAITING,
        "family": kind if isinstance(kind, str) and kind else "",
        "tool": "",
        "markers": [],
        "content_types": [],
        "sources": 0,
        "sources_evidenced": False,
        "source_digests": [],
        "urls_moderated": 0,
        "research_confirmed": False,
        "report": "",
        "report_final": False,
        "report_truncated": False,
        "terminal_state": "",
    }
    if not isinstance(payload, dict):
        return projection
    try:
        if not projection["family"]:
            projection["family"] = kind_of(payload)
        event_type = payload.get("type") if isinstance(payload.get("type"), str) else ""
        frame_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        message = _message_of(payload) or {}
        content = message.get("content") if isinstance(message.get("content"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        content_type = _token(content.get("content_type"))
        recipient = _token(message.get("recipient"))
        role = _token(author.get("role"))
        status = message.get("status") if isinstance(message.get("status"), str) else ""
        # ``end_turn`` is an observed boolean on the assistant's message object:
        # true only on the message that closes the turn.
        end_turn = message.get("end_turn") is True

        action = OBSERVED_ACTION_LABELS.get(event_type, "")
        tool = ""
        if "web.run" in (recipient, metadata.get("tool_name"), frame_metadata.get("tool_name")):
            action, tool = ACTION_SOURCES, "web.run"
        elif content_type in {"thoughts", "reasoning_recap"}:
            action = ACTION_ANALYSING
        elif content_type == "text" and status == "in_progress":
            action = ACTION_WRITING

        markers = []
        for key in ("event", "marker"):
            token = _token(payload.get(key))
            if token and token not in markers:
                markers.append(token)

        sources, evidenced = _collect_sources(payload)

        # A message's ``status`` describes *that message*, not the turn.  The
        # real stream also carries the echo of the user's own question
        # (``input_message``) and hidden/internal submessages, and every one of
        # those is already "finished" the moment it appears -- a user echo with
        # ``finished_successfully`` used to freeze the panel as complete at the
        # start of the turn.  Only a message authored by the assistant may speak
        # for the turn, and completion needs the observed end-of-turn evidence on
        # top of the status: ``end_turn`` (a tool submessage is assistant-authored
        # and may carry it too) *and* ``metadata.is_complete``, which the capture
        # shows on exactly one frame -- the assistant's answer, the same frame
        # that carries the citations and the finish details.
        #
        # The failure/cancellation branch deliberately does not require
        # ``end_turn``: a cancelled or failed turn may be replayed without it, and
        # discarding that signal would let the stream's own ``[DONE]`` record the
        # turn as complete instead.  Erring toward "failed" is honest; erring
        # toward "complete" is what this whole fix is about.
        #
        # Turn-level families stay untouched: the ``[DONE]`` terminal (handled by
        # the recorder/store), ``message_stream_complete``, an explicit failure or
        # cancellation, and the payload-level ``error`` / ``error_code`` below,
        # none of which hang off a message object.  Incremental patches (the
        # batched ``{c, o, v: [{p, o, v}, ...]}`` form) carry no message object
        # and therefore never take part in terminal detection; a terminal is only
        # ever read from a snapshot frame or a turn-level family.
        terminal_state = ""
        assistant_message = role == ASSISTANT_ROLE
        assistant_answer = (assistant_message and status == "finished_successfully"
                            and end_turn and metadata.get("is_complete") is True)
        if _has_error(payload):
            terminal_state = "failed"
        elif assistant_message and status in {"failed", "cancelled", "incomplete"}:
            terminal_state = "cancelled" if status == "cancelled" else "failed"
        elif event_type == "message_stream_complete":
            terminal_state = "complete"
        elif assistant_answer:
            terminal_state = "complete"

        # The answer body, from the same evidence the terminal detection uses:
        # only an assistant message may speak, hidden and tool messages may not,
        # and the frame that ends the turn is the one that settles the report.
        report, report_truncated, report_final = "", False, False
        if assistant_message and not _is_hidden_message(metadata) \
                and not _is_tool_frame(recipient, metadata):
            body = _report_body(message)
            if body:
                report, report_truncated = _bound_report(body)
                report_final = assistant_answer

        projection.update({
            "action": action or ACTION_WAITING,
            "tool": tool,
            "markers": markers[:MAX_MARKERS],
            "content_types": [content_type] if content_type else [],
            "sources": len(sources),
            "sources_evidenced": evidenced,
            "source_digests": sorted(sources),
            "urls_moderated": 1 if event_type == "url_moderation" else 0,
            "research_confirmed": "deep_research_version" in metadata,
            "report": report,
            "report_final": report_final,
            "report_truncated": report_truncated,
            "terminal_state": terminal_state,
        })
    except Exception:
        # Never let one unusual frame take the projection (and with it the
        # caller's loop) down.  The event itself is not lost: it is retained and
        # forwarded by the caller regardless of what this returns.
        return projection
    return projection


def is_research_turn(body) -> bool:
    """Whether this request is a research turn, using fields the repo already sets.

    ``api/models.py`` publishes the deep-research model aliases, and
    ``chatgpt/services/model_mixin.py`` injects ``system_hints=["research"]`` for
    them; both are existing, observable request fields.
    """
    if not isinstance(body, dict):
        return False
    model = str(body.get("model") or "").lower()
    if model in _RESEARCH_MODEL_SLUGS or any(marker in model for marker in _RESEARCH_MODEL_MARKERS):
        return True
    hints = body.get("system_hints")
    return isinstance(hints, list) and any(
        str(hint).lower() in _RESEARCH_SYSTEM_HINTS for hint in hints)


def detection_summary(body) -> dict:
    """Credential-free request shape used to diagnose research detection.

    User text and arbitrary values are deliberately excluded.  The remaining
    model/hint/content-type/recipient fields are protocol selectors needed to
    keep detection aligned with frontend releases.
    """
    if not isinstance(body, dict):
        return {"body": type(body).__name__}
    mode = body.get("conversation_mode")
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    content_types, recipients, metadata_keys = set(), set(), set()
    for item in messages[:8]:
        if not isinstance(item, dict):
            continue
        recipient = item.get("recipient")
        if isinstance(recipient, str):
            recipients.add(recipient[:80])
        content = item.get("content")
        if isinstance(content, dict) and isinstance(content.get("content_type"), str):
            content_types.add(content["content_type"][:80])
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            metadata_keys.update(str(key)[:80] for key in metadata.keys())
    return {
        "keys": sorted(str(key)[:80] for key in body.keys()),
        "model": str(body.get("model") or "")[:80],
        "system_hints": [str(value)[:80] for value in body.get("system_hints", [])[:16]]
        if isinstance(body.get("system_hints"), list) else [],
        "conversation_mode_keys": sorted(str(key)[:80] for key in mode.keys())
        if isinstance(mode, dict) else [],
        "conversation_mode_kind": str(mode.get("kind") or "")[:80]
        if isinstance(mode, dict) else "",
        "message_content_types": sorted(content_types),
        "message_recipients": sorted(recipients),
        "message_metadata_keys": sorted(metadata_keys),
    }


def stream_timeout_for(body, default=None) -> float:
    """Upstream silence budget for this turn.

    With ``stream=True`` a scalar ``timeout`` is not a stream deadline: curl_cffi
    maps it to a low-speed watchdog (``curl_cffi/requests/utils.py``), so the
    measured effect is "how long may upstream go quiet", not "how long may the
    turn run".  A research turn legitimately idles for minutes between steps, so
    leaving it on the chat budget kills it mid-turn -- the stream simply stops
    with no terminal event, which is exactly the failure mode the retention work
    exists to avoid.

    Three cases, in order:

    * an ordinary chat turn keeps the budget it always had, unchanged;
    * an explicit ``CHAT_RESEARCH_TIMEOUT`` wins outright for research turns, in
      both directions -- an operator asking for a *shorter* budget than the
      default means it;
    * otherwise a research turn gets ``DEFAULT_RESEARCH_TIMEOUT``, never below
      whatever an operator configured for chat.  A value that cannot be parsed,
      or that is not positive, counts as "not configured" rather than as a
      budget -- a typo must not shorten a research turn.
    """
    budget = chat_request_timeout if default is None else default
    if not is_research_turn(body):
        return budget
    research_default = max(budget, DEFAULT_RESEARCH_TIMEOUT)
    raw = (os.getenv(RESEARCH_TIMEOUT_ENV) or "").strip()
    if not raw:
        return research_default
    try:
        value = float(raw)
    except ValueError:
        return research_default
    return value if value > 0 else research_default


class Recorder:
    """One turn's handle onto the store.

    Buffers events until the conversation id is known (a new conversation only
    reveals its id mid-stream), then attaches them to that conversation.
    """

    def __init__(self, store, seed: str, conversation_id, research=False):
        self._store = store
        self.owner = anon_id(seed)
        self.conversation_id = conversation_id or None
        self._pending = []
        self._terminal_seen = False
        self._attached = False
        self._detached = False
        self.research = bool(research)
        if self.conversation_id:
            self._attach(self.conversation_id)

    def _attach(self, conversation_id: str):
        if self._detached:
            return
        if not self._store.begin(conversation_id, self.owner, research=self.research):
            # Another Seed already owns progress for this conversation: retain
            # nothing rather than mixing two users' turns into one record.
            self._detached = True
            self._pending = []
            logger.warning("[research_progress] phase=refused conversation=foreign_seed_owned")
            return
        self.conversation_id = conversation_id
        self._attached = True
        buffered, self._pending = self._pending, []
        for event in buffered:
            self._store.record(conversation_id, event)

    def record(self, event: bytes) -> bool:
        """Account for one forwarded event. Returns whether to forward it."""
        kind, payload = classify(event)
        if kind == KIND_TERMINAL:
            if self._terminal_seen:
                # A repeated terminal must not reach the browser twice.
                return False
            self._terminal_seen = True
        if kind is None:
            return True
        if not self._attached and not self._detached:
            discovered = conversation_id_of(payload) if payload else None
            if discovered:
                self._attach(discovered)
        if self._attached:
            self._store.record(self.conversation_id, event)
        elif not self._detached:
            self._pending.append(event)
            if len(self._pending) > self._store.max_events:
                del self._pending[:len(self._pending) - self._store.max_events]
        return True

    def finish(self, outcome: str):
        self._store.finish(self.conversation_id, outcome)


class _Progress:
    """Retained state for one conversation."""

    def __init__(self, conversation_id: str, owner: str, research=False):
        self.conversation_id = conversation_id
        self.owner = owner
        self.events = []          # [{"index", "kind", "text"}]
        self.seen = 0
        self.bytes = 0
        self.unknown = {}         # fingerprint -> count
        self.terminal = False
        self.state = "streaming"
        self.truncated = False
        self.updated_at = time.time()
        # Two different questions, two different flags.  ``research`` is sticky:
        # it marks a conversation that has a retained research turn, which is
        # what keeps that turn's projection alive for the restore route.
        # ``current_turn_research`` describes the turn that is running *now*,
        # which is what the active poll answers -- a chat turn on the same
        # conversation refreshes the record without reviving the panel.
        self.research = bool(research)
        self.current_turn_research = bool(research)
        self.started_at = self.updated_at
        # Set exactly once, by the first terminal signal observed for this turn.
        # Everything the panel shows as "how long it took" is computed against
        # this, so a finished turn's clock stops instead of counting up forever.
        self.finished_at = None
        self.action = ACTION_STARTING
        self.family = ""
        self.tool = ""
        self.markers = []
        self.content_types = []
        self.source_digests = set()
        self.sources_evidenced = False
        self.urls_moderated = 0
        self.research_confirmed = False
        # The assistant's own visible answer.  Bounded in ``project_event``
        # before it ever reaches here, and only ever overwritten by a frame
        # that carries an answer -- never cleared by a frame that has none.
        self.report = ""
        self.report_final = False
        self.report_truncated = False
        # Per-turn state for the incremental family: the message an incremental
        # turn builds patch by patch.  Reset by ``begin`` with the rest of the
        # turn's state, so a new turn never inherits the previous one's body.
        self.delta = new_delta_state()

    def projection(self, now):
        """The browser-facing view: whitelisted keys, no upstream text."""
        end = self.finished_at if self.finished_at is not None else now
        return {
            "action": self.action,
            "family": self.family,
            "tool": self.tool,
            "markers": list(self.markers),
            "content_types": list(self.content_types),
            "sources": len(self.source_digests),
            "sources_evidenced": self.sources_evidenced,
            "urls_moderated": self.urls_moderated,
            "research_confirmed": self.research_confirmed,
            "report": self.report,
            "report_final": self.report_final,
            "report_truncated": self.report_truncated,
            "started_at": self.started_at,
            "elapsed_ms": max(0, int((end - self.started_at) * 1000)),
            "finished_at": self.finished_at,
            "finished": self.state in TERMINAL_STATES,
        }


class ResearchProgressStore:
    """Bounded, thread-safe retention of per-conversation progress events."""

    def __init__(self, max_events=DEFAULT_MAX_EVENTS,
                 max_conversations=DEFAULT_MAX_CONVERSATIONS,
                 max_bytes=DEFAULT_MAX_BYTES,
                 max_event_bytes=DEFAULT_MAX_EVENT_BYTES,
                 record_ttl=DEFAULT_RECORD_TTL):
        self.max_events = max_events
        self.max_conversations = max_conversations
        self.max_bytes = max_bytes
        self.max_event_bytes = max_event_bytes
        self.record_ttl = record_ttl
        self._records = OrderedDict()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def recorder(self, seed: str, conversation_id=None, research=False) -> Recorder:
        return Recorder(self, seed, conversation_id, research=research)

    def begin(self, conversation_id: str, owner: str, research=False) -> bool:
        """Open a new turn's retention. False if another Seed owns the record."""
        with self._lock:
            record = self._records.get(conversation_id)
            if record is not None and record.owner != owner:
                return False
            if record is None:
                record = _Progress(conversation_id, owner, research=research)
                self._records[conversation_id] = record
                self._evict_conversations()
            else:
                record.updated_at = time.time()
                # Sticky: the conversation keeps its retained research turn for
                # the restore route.  Per-turn: this turn decides whether the
                # research panel is current, so a chat turn on the same
                # conversation puts the panel away instead of refreshing it.
                record.research = record.research or bool(research)
                record.current_turn_research = bool(research)
                if research:
                    # A new research turn supersedes the previous turn's
                    # *projection* outright: its terminal marker, clock, answer
                    # and every accumulated evidence field belong to the turn
                    # that is running now.  The retained event log and its
                    # counters stay cumulative on purpose -- they are the
                    # forensic record of the conversation, bounded by
                    # ``_evict_events``, and resetting them would throw away the
                    # only copy of what upstream actually sent.
                    record.events = [e for e in record.events
                                     if e["kind"] != KIND_TERMINAL]
                    record.terminal = False
                    record.state = "streaming"
                    record.started_at = record.updated_at
                    record.finished_at = None
                    record.report = ""
                    record.report_final = False
                    record.report_truncated = False
                    record.delta = new_delta_state()
                    record.action = ACTION_STARTING
                    # Every field a projection accumulates describes the turn
                    # that produced it.  Carrying one of them over would report
                    # the previous turn's sources, markers or tool as this
                    # turn's -- evidence this turn never produced.
                    record.family = ""
                    record.tool = ""
                    record.markers = []
                    record.content_types = []
                    record.source_digests = set()
                    record.sources_evidenced = False
                    record.urls_moderated = 0
                    record.research_confirmed = False
                # A plain chat turn on the same conversation deliberately leaves
                # the research projection alone: the panel is retired by the
                # per-turn flag above, while the frozen clock and the answer the
                # research turn produced stay restorable -- which is what a
                # refresh onto that conversation is asking for.  Overwriting them
                # with the chat turn's own state is how a refresh used to bring
                # the panel back with an empty report over an unrelated turn.
            return True

    def record(self, conversation_id: str, event: bytes):
        kind, payload = classify(event)
        # Resume tokens are upstream continuation credentials. They may be
        # consumed by the transport, but must never enter the progress cache or
        # its browser-readable restore endpoint.
        if kind is None or kind == KIND_RESUME_TOKEN:
            return
        text = event.decode("utf-8", errors="replace")
        if len(text) > self.max_event_bytes:
            text = text[:self.max_event_bytes] + _TRUNCATION_MARKER
        with self._lock:
            record = self._records.get(conversation_id)
            if record is None:
                return
            record.events.append({"index": record.seen, "kind": kind, "text": text})
            record.seen += 1
            record.bytes += len(text)
            record.updated_at = time.time()
            if kind == KIND_TERMINAL:
                record.terminal = True
            elif kind == KIND_OTHER_JSON and isinstance(payload, dict):
                fingerprint = structure_fingerprint(payload)
                record.unknown[fingerprint] = record.unknown.get(fingerprint, 0) + 1
            if record.current_turn_research and isinstance(payload, dict):
                self._apply_projection(record, self._project(record, payload, kind))
            self._evict_events(record)
            self._records.move_to_end(conversation_id)

    def _project(self, record, payload, kind):
        """One frame's projection, folding the incremental family first.

        A patch frame carries no message object, so it is folded into the turn's
        incremental state and projected as the message that state adds up to.
        Source digests are computed from the patch value as it arrives -- the
        same bounded walk the snapshot path uses -- and merged here, so an
        incremental turn can evidence sources without the store ever keeping a
        source value.
        """
        if kind != KIND_MESSAGE_DELTA:
            return project_event(payload, kind)
        projection = project_event(fold_delta(payload, record.delta), kind)
        if record.delta["digests"]:
            projection["source_digests"] = sorted(
                set(projection["source_digests"]) | record.delta["digests"])
        explicit = record.delta.get("terminal")
        if explicit and not projection.get("terminal_state") \
                and record.delta.get("role") in ("", ASSISTANT_ROLE):
            # A failure or cancellation upstream stated on the message it was
            # building.  Only the failure direction is read from a patch: a
            # cancelled turn replayed without a named author would otherwise be
            # recorded complete by the transport's own ``[DONE]`` marker, and
            # erring toward "failed" is the honest direction.  A message
            # upstream positively attributed to someone else is left alone.
            #
            # The consequence is accepted rather than accidental: a *tool*
            # submessage that fails freezes the whole turn as failed even if the
            # answer arrives afterwards.  The snapshot path has always behaved
            # this way -- its failure branch deliberately does not require
            # ``end_turn`` -- and a turn whose own record says it failed is the
            # honest reading of that stream.
            projection["terminal_state"] = explicit
        return projection

    def _apply_projection(self, record, projection):
        """Fold one event's projection into the record, within fixed bounds."""
        if projection.get("action"):
            record.action = projection["action"]
        if projection.get("family"):
            record.family = projection["family"]
        if projection.get("tool"):
            record.tool = projection["tool"]
        for token in projection.get("markers") or ():
            if token not in record.markers:
                record.markers = (record.markers + [token])[-MAX_MARKERS:]
        content_type = (projection.get("content_types") or [""])[0]
        if content_type and content_type not in record.content_types:
            record.content_types = (record.content_types + [content_type])[-MAX_CONTENT_TYPES:]
        if projection.get("sources_evidenced"):
            record.sources_evidenced = True
        if projection.get("research_confirmed"):
            record.research_confirmed = True
        record.urls_moderated += projection.get("urls_moderated") or 0
        if len(record.source_digests) < MAX_SOURCE_DIGESTS:
            record.source_digests.update(projection.get("source_digests") or ())
        # Report precedence, decided by evidence rather than by arrival order:
        # the frame that ends the turn carries the answer and outranks any
        # interim body, while a later interim frame must not overwrite it.  Two
        # frames of the same rank are ordered by arrival, so a streamed answer
        # that grows frame by frame settles on its last and longest version.
        report = projection.get("report") or ""
        if report:
            settled = bool(projection.get("report_final"))
            if settled or not (record.report and record.report_final):
                record.report = report
                record.report_final = settled
                record.report_truncated = bool(projection.get("report_truncated"))
        terminal_state = projection.get("terminal_state")
        if terminal_state in TERMINAL_STATES:
            self._mark_terminal(record, terminal_state)

    def _mark_terminal(self, record, state):
        """Freeze the turn at its first terminal signal.

        Everything after the first terminal observation -- a later status frame,
        the transport's own ``finish()``, a repeated ``[DONE]`` -- must not be
        able to restart the clock or overwrite how the turn actually ended.  The
        one exception is a *new* turn on the same conversation, which clears
        ``finished_at`` in ``begin()`` before any frame of that turn arrives.
        """
        if record.finished_at is not None:
            return
        record.state = state
        record.finished_at = record.updated_at = time.time()

    def _terminal_observed(self, record) -> bool:
        """Whether upstream actually declared this turn finished.

        Two observed shapes count, and only these: the ``[DONE]`` terminal
        (retained as an event, ``record.terminal``) and a projected
        ``terminal_state`` (``finished_successfully`` / ``message_stream_complete``
        / an explicit failure or cancellation), which sets ``finished_at``.
        Anything else is silence, and silence is not an ending.
        """
        return bool(record.terminal) or record.finished_at is not None

    def finish(self, conversation_id, outcome: str):
        if not conversation_id:
            return
        with self._lock:
            record = self._records.get(conversation_id)
            if record is None:
                return
            if outcome == "complete" and not self._terminal_observed(record):
                # The transport reports "complete" when its event iterator
                # returns, which happens for any reason the upstream stops
                # delivering -- including a stream cut before the terminal
                # marker.  Recording that as complete would render a truncated
                # research turn as 研究已完成.  The turn is over either way, so
                # it is classified with the existing honest terminal state
                # instead of being left looking like it is still running.
                outcome = "failed"
                # Enough to locate the affected turn afterwards (hashed id, event
                # count) without putting an upstream identifier in the log.
                logger.warning(
                    f"[research_progress] phase=ended_without_terminal state=failed "
                    f"conversation={anon_id(conversation_id)} events_seen={record.seen}"
                )
            if outcome in TERMINAL_STATES:
                self._mark_terminal(record, outcome)
            else:
                record.state = outcome
            if record.research:
                if record.state == "complete":
                    record.action = ACTION_DONE
                elif record.state == "cancelled":
                    record.action = ACTION_CANCELLED
                elif record.state == "failed":
                    record.action = ACTION_FAILED
            record.updated_at = time.time()

    # -- read --------------------------------------------------------------

    def snapshot(self, conversation_id):
        with self._lock:
            record = self._records.get(conversation_id)
            if record is None:
                return None
            return {
                "conversation_id": record.conversation_id,
                "owner": record.owner,
                "state": record.state,
                "terminal": record.terminal,
                "events_seen": record.seen,
                "truncated": record.truncated,
                "unknown": dict(record.unknown),
                "updated_at": record.updated_at,
                "events": [dict(e) for e in record.events],
            }

    def active_snapshot(self, owner: str):
        """Return the newest research projection owned by one Seed.

        "Is a research turn current for this Seed?" is a question about the
        Seed's *newest* turn, not about whether research ever ran.  A chat turn
        that starts after a research turn -- in the same conversation or in
        another one -- makes the retained research panel stale, so this returns
        ``{"research": False}`` and the browser puts the panel away instead of
        leaving a finished report over an unrelated turn.

        The retained record itself is deliberately *not* discarded: the
        per-conversation restore route keeps serving it, which is what makes a
        finished report still reachable on the page that ran it.
        """
        now = time.time()
        with self._lock:
            candidates = [record for record in self._records.values()
                          if record.owner == owner
                          and now - record.updated_at <= self.record_ttl]
            if not candidates:
                return {"research": False}
            record = max(candidates, key=lambda item: item.updated_at)
            if not record.current_turn_research:
                return {"research": False}
            return self._view(record, now)

    def projection_snapshot(self, conversation_id: str, owner: str):
        """One conversation's research projection, for a browser that reloaded.

        Served entirely from what the mirror already retained: a refresh must
        never make the mirror re-run the turn or re-ask the account.  Returns
        None unless the same Seed owns the record, so a second user sharing the
        upstream account cannot probe for another's progress.
        """
        now = time.time()
        with self._lock:
            record = self._records.get(conversation_id)
            if record is None or record.owner != owner or not record.research:
                return None
            return self._view(record, now)

    def _view(self, record, now):
        """The browser-facing shape shared by both read paths.

        ``research`` is the panel's own show/hide bit and answers "is a research
        turn current for this conversation", not "did one ever run here".  A
        conversation whose newest turn is an ordinary chat still *serves* its
        retained research projection -- the frozen clock and the answer stay
        readable -- but it does not ask the panel to open over that chat turn.
        """
        return {
            "research": bool(record.current_turn_research),
            # The panel needs the id to tell "this is the turn I am watching"
            # from "an older research turn on the same account".
            "conversation_id": record.conversation_id,
            "state": record.state,
            "terminal": record.terminal,
            "events_seen": record.seen,
            "updated_at": record.updated_at,
            "projection": record.projection(now),
        }

    def clear(self):
        with self._lock:
            self._records.clear()

    # -- bounds ------------------------------------------------------------

    def _evict_events(self, record):
        if len(record.events) <= self.max_events and record.bytes <= self.max_bytes:
            return
        record.truncated = True
        # The terminal event is the one thing a restored page must not lose, so
        # it is exempt from both bounds; everything else is kept newest-first
        # until the event budget is spent, then trimmed to the byte budget.
        newest = [e for e in reversed(record.events) if e["kind"] != KIND_TERMINAL]
        terminal = [e for e in record.events if e["kind"] == KIND_TERMINAL]
        kept, used = terminal[:], sum(len(e["text"]) for e in terminal)
        for entry in newest:
            if len(kept) >= self.max_events:
                break
            if used + len(entry["text"]) > self.max_bytes:
                continue
            kept.append(entry)
            used += len(entry["text"])
        if newest and not any(e["kind"] != KIND_TERMINAL for e in kept):
            # The newest state is the other thing a restored page cannot do
            # without; a single large event must not empty the record.
            entry = newest[0]
            kept.append(entry)
            used += len(entry["text"])
        record.events = sorted(kept, key=lambda e: e["index"])
        record.bytes = used

    def _evict_conversations(self):
        while len(self._records) > self.max_conversations:
            oldest = min(self._records, key=lambda key: self._records[key].updated_at)
            del self._records[oldest]


store = ResearchProgressStore()


@app.get("/backend-api/research-progress/active")
async def active_research_progress(request: Request):
    seed = resolve_seed_token(request)
    if not seed:
        return JSONResponse({"research": False})
    return JSONResponse(store.active_snapshot(anon_id(seed)))


@app.get("/backend-api/research-progress/{conversation_id}")
async def research_progress_snapshot(request: Request, conversation_id: str):
    """What the mirror retained for one conversation, for a reloading browser.

    Seed ownership is checked first and the record's own owner hash must agree,
    so a second Seed sharing the same upstream account cannot read (or even
    probe for) another user's research progress.  No upstream request is made.

    This is the *forensic* view: it carries the retained event text so a future
    protocol capture can be diffed against what the mirror actually saw.  The
    panel uses ``/projection`` below instead, which never returns upstream
    bodies to a browser.
    """
    seed = resolve_seed_token(request)
    owned = conversation_id in (globals.seed_map.get(seed) or {}).get("conversations", [])
    record = store.snapshot(conversation_id) if owned else None
    if record is None or record.get("owner") != anon_id(seed):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return JSONResponse(record)


@app.get("/backend-api/research-progress/{conversation_id}/projection")
async def research_progress_projection(request: Request, conversation_id: str):
    """The panel's restore path: projection only, no upstream text.

    A research turn outlives the page that started it, so a reload has to be
    able to re-render the current -- or final -- state without re-running the
    turn.  Ownership is decided here, before anything is served, and the
    response is the same closed key set the live poll returns.
    """
    seed = resolve_seed_token(request)
    if not seed:
        raise HTTPException(status_code=404, detail="Conversation not found")
    view = store.projection_snapshot(conversation_id, anon_id(seed))
    if view is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return JSONResponse(view)


async def with_heartbeat(events, interval: float):
    """Re-yield ``events``, inserting SSE comment frames when it goes quiet.

    Opt-in: the caller passes a positive interval only when an operator has
    decided the deployment needs it.  Events are never reordered, delayed once
    available, or dropped.
    """
    if not interval or interval <= 0:
        async for event in events:
            yield event
        return
    iterator = events.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if pending in done:
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield event
            else:
                yield HEARTBEAT_FRAME
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        closer = getattr(iterator, "aclose", None)
        if closer is not None:
            with contextlib.suppress(BaseException):
                await closer()


def heartbeat_interval() -> float:
    raw = (os.getenv("RESEARCH_PROGRESS_HEARTBEAT_SECONDS") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value if value > 0 else 0.0
