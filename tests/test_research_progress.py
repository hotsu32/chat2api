"""Deep Research progress state: what the mirror keeps while a turn streams.

The upstream protocol for research progress is NOT fully captured (see
``tmp/research-protocol/OFFICIAL_INTERFACE_EVIDENCE.md``).  These tests therefore
assert only properties that hold for *any* upstream event shape:

* an event the mirror cannot classify is still forwarded and still accounted
  for -- never silently dropped;
* the terminal marker is forwarded exactly once even if upstream repeats it;
* per-conversation retention is bounded, and what survives the bound is the
  newest state plus the terminal one;
* a restore snapshot is only readable by the Seed that owns the conversation,
  and carries no credential material.

No test names an upstream research event, because none is known.
"""

import asyncio
import ast
import hashlib
import json
from pathlib import Path

import pytest

from gateway.research_progress import (
    HEARTBEAT_FRAME,
    KIND_INPUT_MESSAGE,
    KIND_MESSAGE_DELTA,
    KIND_NON_JSON,
    KIND_OTHER_JSON,
    KIND_STE_METADATA,
    KIND_STREAM_COMPLETE,
    KIND_TERMINAL,
    KIND_TITLE_GENERATION,
    ResearchProgressStore,
    classify,
    conversation_id_of,
    detection_summary,
    is_terminal,
    kind_of,
    stream_timeout_for,
    structure_fingerprint,
    with_heartbeat,
)


def ev(payload, *, prefix=""):
    """One SSE event carrying ``payload`` (str or dict) as its data buffer."""
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return f"{prefix}data: {data}\n\n".encode()


DONE = ev("[DONE]")
DELTA = ev({"p": "/message/content/parts/0", "o": "append", "v": "step"})
SNAPSHOT = ev({"type": "message", "message": {"author": {"role": "assistant"}}})
UNKNOWN = ev({"type": "unclassified_future_phase", "value": {"nested": 1}})


def test_detection_summary_excludes_user_text_and_arbitrary_metadata_values():
    secret_prompt = "PRIVATE research prompt must never enter logs"
    summary = detection_summary({
        "model": "gpt-5-6",
        "conversation_mode": {"kind": "primary_assistant", "private": secret_prompt},
        "messages": [{
            "recipient": "all",
            "content": {"content_type": "text", "parts": [secret_prompt]},
            "metadata": {"connector_id": "private-value", "private": secret_prompt},
        }],
    })
    rendered = json.dumps(summary)
    assert secret_prompt not in rendered
    assert "private-value" not in rendered
    assert summary["conversation_mode_kind"] == "primary_assistant"
    assert summary["message_metadata_keys"] == ["connector_id", "private"]


def test_current_first_party_connector_hint_is_detected_as_research():
    from gateway.research_progress import is_research_turn

    assert is_research_turn({
        "model": "gpt-5-6-thinking",
        "system_hints": ["plugin:connector_openai_deep_research"],
    }) is True


@pytest.fixture
def store():
    s = ResearchProgressStore(max_events=8, max_conversations=3, max_bytes=4096)
    yield s
    s.clear()


# ---------------------------------------------------------------------------
# Classification: structure only, and nothing is discarded for being unknown
# ---------------------------------------------------------------------------

def test_terminal_is_recognised_by_the_observed_done_marker():
    assert classify(DONE)[0] == KIND_TERMINAL


def test_incremental_patch_is_recognised_by_its_observed_field_names():
    assert classify(DELTA)[0] == KIND_MESSAGE_DELTA


def test_unknown_json_object_is_recorded_as_unknown_not_named():
    """An unrecognised phase must not be renamed into an invented one."""
    kind, payload = classify(UNKNOWN)
    assert kind == KIND_OTHER_JSON
    assert payload == {"type": "unclassified_future_phase", "value": {"nested": 1}}


def test_non_json_data_is_kept_as_its_own_kind():
    assert classify(ev("not-json-at-all"))[0] == KIND_NON_JSON


def test_comment_frame_carries_no_event():
    assert classify(b": keep-alive\n\n")[0] is None


def test_resume_token_is_never_retained_by_store(store):
    event = ev({"type": "resume_conversation_token", "token": "must-not-be-stored"})
    rec = store.recorder("seed-a", "c1")
    assert rec.record(event) is True
    snapshot = store.snapshot("c1")
    assert snapshot["events"] == []
    assert "must-not-be-stored" not in str(snapshot)


def test_fingerprint_uses_field_names_not_values():
    """The fingerprint identifies a *shape*, so it must carry no payload.

    Two events that differ only in their values share a fingerprint: the
    fingerprint is an inventory of shapes, while the event text itself (also
    retained) is what a future capture reads the names out of.
    """
    a = structure_fingerprint({"type": "phase_a", "detail": "secret-value"})
    b = structure_fingerprint({"detail": "other-value", "type": "phase_a"})
    c = structure_fingerprint({"type": "phase_a", "detail": "secret-value", "extra": 1})
    assert a == b, "field order must not change the fingerprint"
    assert a != c, "a different field set is a different shape"
    assert "secret-value" not in a
    assert "phase_a" not in a


def test_conversation_id_is_read_from_the_observed_positions():
    assert conversation_id_of({"conversation_id": "c1"}) == "c1"
    assert conversation_id_of({"v": {"conversation_id": "c2"}}) == "c2"
    assert conversation_id_of({"v": "not-a-dict"}) is None
    assert conversation_id_of({"unrelated": 1}) is None


# ---------------------------------------------------------------------------
# Recording: forward everything, account for everything, terminal once
# ---------------------------------------------------------------------------

def test_unknown_event_is_forwarded_and_counted(store):
    rec = store.recorder("seed-a", "c1")
    assert rec.record(UNKNOWN) is True
    snap = store.snapshot("c1")
    assert snap["events_seen"] == 1
    assert snap["unknown"] and len(snap["unknown"]) == 1
    assert snap["events"][0]["text"].startswith("data: ")


def test_terminal_is_forwarded_once_even_when_upstream_repeats_it(store):
    rec = store.recorder("seed-a", "c1")
    assert rec.record(DELTA) is True
    assert rec.record(DONE) is True
    assert rec.record(DONE) is False, "a repeated terminal must not reach the browser twice"
    assert rec.record(DONE) is False
    snap = store.snapshot("c1")
    assert snap["terminal"] is True
    assert sum(1 for e in snap["events"] if e["kind"] == KIND_TERMINAL) == 1


def test_late_events_after_terminal_are_still_forwarded(store):
    """A terminal marker is not licence to stop forwarding a live stream."""
    rec = store.recorder("seed-a", "c1")
    rec.record(DONE)
    assert rec.record(DELTA) is True
    assert store.snapshot("c1")["events_seen"] == 2


def test_conversation_id_discovered_midstream_rekeys_the_record(store):
    rec = store.recorder("seed-a", None)
    rec.record(ev({"conversation_id": "late-id", "message": {}}))
    rec.record(DELTA)
    assert store.snapshot("late-id") is not None
    assert store.snapshot("late-id")["events_seen"] == 2


def test_state_reflects_how_the_stream_ended(store):
    rec = store.recorder("seed-a", "c1")
    rec.record(DELTA)
    rec.finish("cancelled")
    snap = store.snapshot("c1")
    assert snap["state"] == "cancelled"
    assert snap["terminal"] is False


# ---------------------------------------------------------------------------
# Bounded retention
# ---------------------------------------------------------------------------

def test_retention_keeps_the_newest_events_and_the_terminal_one(store):
    rec = store.recorder("seed-a", "c1")
    for i in range(20):
        rec.record(ev({"p": f"/x/{i}", "o": "append", "v": str(i)}))
    rec.record(DONE)
    snap = store.snapshot("c1")
    assert snap["truncated"] is True
    assert snap["events_seen"] == 21
    assert len(snap["events"]) <= store.max_events
    assert snap["terminal"] is True, "the terminal event must survive eviction"
    assert any(e["kind"] == KIND_TERMINAL for e in snap["events"])
    kept = [e["index"] for e in snap["events"]]
    assert max(kept) == snap["events_seen"] - 1, "the newest event must be retained"


def test_retention_is_bounded_per_conversation(store):
    rec = store.recorder("seed-a", "c1")
    for i in range(50):
        rec.record(ev({"p": str(i), "o": "append", "v": "x" * 200}))
    snap = store.snapshot("c1")
    assert len(snap["events"]) <= store.max_events
    assert sum(len(e["text"]) for e in snap["events"]) <= store.max_bytes


def test_number_of_tracked_conversations_is_bounded(store):
    for i in range(6):
        store.recorder("seed-a", f"c{i}").record(DELTA)
    tracked = [c for c in (f"c{i}" for i in range(6)) if store.snapshot(c)]
    assert len(tracked) <= store.max_conversations
    assert "c5" in tracked, "the most recent conversation must be retained"


def test_oversized_event_is_truncated_not_stored_whole(store):
    rec = store.recorder("seed-a", "c1")
    rec.record(ev({"type": "huge", "blob": "z" * 50000}))
    text = store.snapshot("c1")["events"][0]["text"]
    assert len(text) <= store.max_event_bytes + 64


# ---------------------------------------------------------------------------
# Snapshot safety
# ---------------------------------------------------------------------------

def test_snapshot_never_contains_the_seed_or_account_credential(store):
    seed = "SEED-TOKEN-VALUE"
    account = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFjY291bnQifQ.account"
    store.recorder(seed, "c1").record(DELTA)
    blob = json.dumps(store.snapshot("c1"))
    assert seed not in blob
    assert account not in blob
    assert hashlib.sha256(seed.encode()).hexdigest()[:8] in blob


def test_unknown_conversation_has_no_snapshot(store):
    assert store.snapshot("never-seen") is None


# ---------------------------------------------------------------------------
# Heartbeat (opt-in) and the research turn's upstream silence budget
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_fills_idle_gaps_and_loses_no_event():
    async def slow():
        for i in range(3):
            await asyncio.sleep(0.05)
            yield ev({"p": str(i), "o": "append", "v": "x"})

    out = [chunk async for chunk in with_heartbeat(slow(), interval=0.01)]
    assert sum(1 for c in out if c == HEARTBEAT_FRAME) >= 2
    assert [c for c in out if c != HEARTBEAT_FRAME] == [ev({"p": str(i), "o": "append", "v": "x"})
                                                        for i in range(3)]
    assert HEARTBEAT_FRAME.startswith(b":"), "a heartbeat must be an SSE comment frame"


@pytest.mark.asyncio
async def test_heartbeat_does_not_delay_a_stream_that_keeps_talking():
    async def fast():
        for i in range(3):
            yield ev({"p": str(i), "o": "append", "v": "x"})

    out = [chunk async for chunk in with_heartbeat(fast(), interval=5.0)]
    assert len(out) == 3


@pytest.mark.asyncio
async def test_abandoning_a_heartbeat_wrapped_stream_closes_it():
    """A browser disconnect must still reach the source generator.

    The wrapper runs the source as a task, so a naive implementation leaks it and
    the upstream keeps generating into a socket nobody reads. Closing the wrapper
    has to close the source, because that is what releases the upstream client.
    """
    closed = []

    async def source():
        try:
            while True:
                yield ev({"p": "0", "o": "append", "v": "x"})
                await asyncio.sleep(0.01)
        finally:
            closed.append(True)

    stream = with_heartbeat(source(), interval=0.01)
    assert await stream.__anext__()
    await stream.aclose()
    assert closed == [True]


def test_research_turn_silence_budget_defaults_to_the_existing_timeout(monkeypatch):
    """Unset means no behaviour change: capacity semantics stay where they were."""
    from utils.configs import chat_request_timeout
    monkeypatch.delenv("CHAT_RESEARCH_TIMEOUT", raising=False)
    assert stream_timeout_for({"model": "gpt-5-6"}) == chat_request_timeout
    assert stream_timeout_for({"model": "o3-deep-research"}) == chat_request_timeout


def test_official_research_slug_is_treated_as_a_research_turn(monkeypatch):
    from utils.configs import chat_request_timeout
    monkeypatch.setenv("CHAT_RESEARCH_TIMEOUT", str(chat_request_timeout * 10))
    assert stream_timeout_for({"model": "research"}) == chat_request_timeout * 10
    assert stream_timeout_for({"model": "gpt-5-6"}) == chat_request_timeout


def test_research_turn_can_be_given_a_longer_silence_budget(monkeypatch):
    from utils.configs import chat_request_timeout
    monkeypatch.setenv("CHAT_RESEARCH_TIMEOUT", str(chat_request_timeout * 10))
    assert stream_timeout_for({"model": "o3-deep-research"}) == chat_request_timeout * 10
    assert stream_timeout_for({"system_hints": ["research"]}) == chat_request_timeout * 10
    assert stream_timeout_for({"model": "gpt-5-6"}) == chat_request_timeout


def test_invalid_research_timeout_is_ignored_rather_than_crashing_a_turn(monkeypatch):
    from utils.configs import chat_request_timeout
    monkeypatch.setenv("CHAT_RESEARCH_TIMEOUT", "not-a-number")
    assert stream_timeout_for({"model": "o3-deep-research"}) == chat_request_timeout


# ---------------------------------------------------------------------------
# The real Plus shape set: classification must be total over it
# ---------------------------------------------------------------------------

def test_the_batched_patch_form_is_classified_as_a_patch():
    """A real turn sends ``{c, o, v: [p/o/v, ...]}`` as well as the single form.

    The earlier classifier only knew ``{p, o}`` at the top level, so every
    batched frame -- the incremental ones the UI streams fastest -- landed in
    the unknown bucket.
    """
    batch = {"c": 5, "o": "add", "v": [{"o": "append", "p": "/message/content/parts/0", "v": "a"},
                                       {"o": "append", "p": "/message/content/parts/0", "v": "b"}]}
    assert kind_of(batch) == KIND_MESSAGE_DELTA
    single = {"c": 1, "o": "append", "p": "/message/content/parts/0", "v": "a"}
    assert kind_of(single) == KIND_MESSAGE_DELTA


def test_the_snapshot_envelope_of_a_real_turn_is_recognised():
    assert kind_of({"c": 2, "v": {"conversation_id": "c1",
                                  "message": {"id": "m1"}}}) == "message_snapshot"
    assert kind_of({"type": "message", "message": {"id": "m1"}}) == "message_snapshot"


def test_a_type_we_have_not_seen_stays_unknown_rather_than_being_guessed():
    """An unknown ``type`` is not renamed into a phase we happen to have.

    It may still be recognised by *structure* -- ``p``/``o``/``v`` is the
    observed patch shape whatever the envelope calls itself -- but an unknown
    type with no observed structure is inventoried, never labelled.
    """
    assert kind_of({"type": "some_future_phase"}) == KIND_OTHER_JSON
    assert kind_of({"type": "some_future_phase", "detail": {"nested": 1}}) == KIND_OTHER_JSON
    assert kind_of({"p": "/x", "o": "append", "v": 1, "type": "some_future_phase"}) \
        == KIND_MESSAGE_DELTA


@pytest.mark.parametrize("payload,expected", [
    ({"conversation_id": "c1", "type": "title_generation", "title": "t"}, KIND_TITLE_GENERATION),
    ({"conversation_id": "c1", "type": "message_marker", "event": "search"}, "message_marker"),
    ({"conversation_id": "c1", "type": "conversation_async_status", "async_status": "x"},
     "async_status"),
    ({"conversation_id": "c1", "type": "server_ste_metadata", "metadata": {}}, KIND_STE_METADATA),
    ({"conversation_id": "c1", "type": "message_stream_complete"}, KIND_STREAM_COMPLETE),
    ({"conversation_id": "c1", "type": "url_moderation"}, "url_moderation"),
    ({"conversation_id": "c1", "type": "input_message", "input_message": {}}, KIND_INPUT_MESSAGE),
], ids=["title", "marker", "async", "ste", "complete", "moderation", "input"])
def test_each_observed_type_has_its_own_family(payload, expected):
    assert kind_of(payload) == expected


@pytest.mark.parametrize("raw", [
    b'data: {"c": 1, "v": {',
    b'data: [DONE',
    b'data: {"a": 1}\x00\xff',
    b'data:',
    b'data: {"c": 1, "v": {"nested": {"deeper": [1, 2, 3]}}}',
    b": keep-alive\n\n",
    b"",
    b"\xff\xfe not utf-8 \xff",
], ids=["truncated", "partial-done", "binary", "empty-data", "deep-object", "comment",
        "empty", "not-utf8"])
def test_classify_is_total_over_bodies_the_upstream_could_cut_short(raw):
    """A research turn's long tail must not be ended by one odd frame."""
    kind, payload = classify(raw)
    assert kind is None or isinstance(kind, str)
    assert payload is None or isinstance(payload, dict)


def test_a_body_that_parses_but_is_not_an_object_is_not_a_terminal():
    kind, payload = classify(b'data: "a string"\n\n')
    assert kind == KIND_NON_JSON and payload is None
    assert is_terminal(b'data: "a string"\n\n') is False


# ---------------------------------------------------------------------------
# The terminal clock, at the store level
# ---------------------------------------------------------------------------

def test_the_first_terminal_signal_freezes_the_record(store_like):
    rec = store_like.recorder("seed-a", "c1", research=True)
    rec.record(ev({"c": 1, "v": {"conversation_id": "c1",
                                 "message": {"status": "in_progress"}}}))
    # The turn must actually have observed a terminal marker: a bare
    # ``finish("complete")`` is the transport's iterator ending, which is
    # classified separately below.
    rec.record(DONE)
    rec.finish("complete")
    frozen = store_like.active_snapshot(rec.owner)["projection"]
    # A late frame after the terminal must not restart the clock or the state.
    rec.record(ev({"c": 2, "v": {"conversation_id": "c1",
                                 "message": {"status": "in_progress"}}}))
    rec.finish("failed")
    after = store_like.active_snapshot(rec.owner)["projection"]
    assert after["finished"] is True
    assert after["finished_at"] == frozen["finished_at"]
    assert store_like.active_snapshot(rec.owner)["state"] == "complete", \
        "the first terminal signal wins"


def test_a_new_turn_on_the_same_conversation_restarts_the_clock(store_like):
    rec = store_like.recorder("seed-a", "c1", research=True)
    rec.record(ev({"c": 1, "v": {"conversation_id": "c1", "message": {"status": "in_progress"}}}))
    rec.record(DONE)
    rec.finish("complete")
    assert store_like.active_snapshot(rec.owner)["projection"]["finished"] is True

    again = store_like.recorder("seed-a", "c1", research=True)
    again.record(ev({"c": 2, "v": {"conversation_id": "c1", "message": {"status": "in_progress"}}}))
    view = store_like.active_snapshot(again.owner)
    assert view["state"] == "streaming"
    assert view["projection"]["finished"] is False
    assert view["projection"]["finished_at"] is None


@pytest.fixture
def store_like():
    s = ResearchProgressStore(max_events=64, max_conversations=4)
    yield s
    s.clear()


# ---------------------------------------------------------------------------
# "The iterator ended" is not "upstream said it finished"
# ---------------------------------------------------------------------------
# The transport drives `finish("complete")` when its async-for returns, which
# happens for any reason the upstream socket stops delivering -- including a
# stream cut before the terminal marker.  Recording that as complete paints a
# truncated research turn as "研究已完成" in the panel.

_STREAMING_FRAME = ev({"c": 1, "v": {"conversation_id": "c1",
                                     "message": {"status": "in_progress"}}})


def test_a_natural_end_without_a_terminal_marker_is_not_recorded_as_complete(store_like):
    rec = store_like.recorder("seed-a", "c1", research=True)
    rec.record(_STREAMING_FRAME)
    # What the transport reports after its iterator is exhausted.
    rec.finish("complete")

    view = store_like.active_snapshot(rec.owner)
    assert view["state"] != "complete", \
        "a stream that ended without a terminal marker must not be recorded as complete"
    assert view["state"] == "failed", \
        "the honest existing terminal state for an ended-but-unfinished turn"
    assert view["terminal"] is False, "no terminal marker was ever observed"
    projection = view["projection"]
    assert projection["finished"] is True, "the turn is over; the panel clock must stop"
    assert projection["action"] != "研究已完成"


def test_a_real_terminal_marker_still_completes_exactly_once(store_like):
    """[DONE]: the observed upstream terminal still freezes the turn as complete."""
    rec = store_like.recorder("seed-a", "c1", research=True)
    rec.record(_STREAMING_FRAME)
    assert rec.record(DONE) is True
    assert rec.record(DONE) is False, "the terminal marker reaches the browser once"
    rec.finish("complete")

    view = store_like.active_snapshot(rec.owner)
    assert view["state"] == "complete"
    assert view["terminal"] is True
    assert view["projection"]["action"] == "研究已完成"


def test_a_real_stream_complete_frame_still_completes_without_the_done_marker(store_like):
    """`message_stream_complete` is the other observed terminal; it alone suffices."""
    rec = store_like.recorder("seed-a", "c1", research=True)
    rec.record(ev({"conversation_id": "c1", "type": "message_stream_complete"}))
    rec.finish("complete")

    view = store_like.active_snapshot(rec.owner)
    assert view["state"] == "complete"
    assert view["projection"]["finished"] is True


def test_an_upstream_failure_before_the_end_is_not_turned_into_a_completion(store_like):
    """A truncated turn the projection already classified stays classified."""
    rec = store_like.recorder("seed-a", "c1", research=True)
    rec.record(ev({"c": 1, "v": {"conversation_id": "c1",
                                 "message": {"status": "failed"}}}))
    rec.finish("complete")

    view = store_like.active_snapshot(rec.owner)
    assert view["state"] == "failed"
    assert view["projection"]["action"] != "研究已完成"


@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
def test_explicit_non_complete_outcomes_are_unchanged(store_like, outcome):
    rec = store_like.recorder("seed-a", "c1", research=True)
    rec.record(_STREAMING_FRAME)
    rec.finish(outcome)
    assert store_like.active_snapshot(rec.owner)["state"] == outcome


# ---------------------------------------------------------------------------
# internal://deep-research is a bundle sentinel, not something to "support"
# ---------------------------------------------------------------------------

GATEWAY_PACKAGES = ("gateway", "chatgpt", "utils", "api")


def _string_constants(path: Path):
    """Every string literal that is *used*, excluding docstrings.

    A docstring may explain why the sentinel is not handled; code that carries
    it as a value is exactly what this guard exists to catch.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


def test_no_module_treats_internal_deep_research_as_a_url():
    """The mirror must not fake the official research component.

    ``internal://deep-research`` is one half of a model-identifier comparison
    inside the official bundle -- not a scheme a browser can resolve.  Nothing
    in the gateway may navigate to it, fetch it, rewrite it, or pretend to
    satisfy it; the mirror's own panel is the supported path.
    """
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for package in GATEWAY_PACKAGES:
        for path in (root / package).rglob("*.py"):
            if "internal://" in "".join(_string_constants(path)):
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"internal:// is a sentinel, not a URL: {offenders}"


def test_the_panel_assets_never_reference_the_sentinel():
    from gateway.research_panel import PANEL_CSS, PANEL_JS
    assert "internal://" not in PANEL_JS
    assert "internal://" not in PANEL_CSS


def test_the_sentinel_s_meaning_is_documented_where_it_matters():
    from gateway import research_panel
    doc = research_panel.__doc__ or ""
    assert "internal://deep-research" in doc
    assert "bundle" in doc, "the reason must be recorded, not just the rule"
