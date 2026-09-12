"""The browser-facing research projection: a closed, honest whitelist.

Everything here is driven by field names that were actually observed on a real
Plus Deep Research turn through the mirror (``tests/fixtures/deep_research_plus_shapes.json``,
captured 2026-09-12, 23 events).  The fixture stores *shapes*, not values, so a
fixture-driven test can only claim "every observed shape is parsed, classified
and projected without raising, and only whitelisted keys come out".  Claims
about which value maps to which label are made in separate tests, each naming
the observation it rests on.

What the projection must never do, and is checked here:

* invent a percentage, an ETA, or a step number -- the real stream carries no
  plan/progress event, so there is nothing to derive one from;
* turn "no source container was seen" into "zero sources" (those are different
  statements, and only the second one is honest when upstream really said so);
* carry prompt text, metadata values, request ids or error strings;
* raise, whatever the upstream sends -- a research turn's value is its long
  tail, and one bad frame used to be able to end the whole projection.
"""

import json
from pathlib import Path

import pytest

from gateway import research_progress as rp
from gateway.research_progress import (
    EVENT_PROJECTION_KEYS,
    KIND_INPUT_MESSAGE,
    KIND_MESSAGE_MARKER,
    KIND_STE_METADATA,
    KIND_STREAM_COMPLETE,
    KIND_TITLE_GENERATION,
    PROJECTION_KEYS,
    ResearchProgressStore,
    classify,
    project_event,
)

FIXTURE = Path(__file__).parent / "fixtures" / "deep_research_plus_shapes.json"


def event(payload):
    if isinstance(payload, (bytes, str)):
        body = payload if isinstance(payload, str) else payload.decode()
    else:
        body = json.dumps(payload)
    return ("data: " + body + "\n\n").encode()


def _shape_payloads():
    """Rebuild one minimal payload per observed shape.

    Values are type-correct placeholders, never invented *field names*: every
    key comes from the capture.  The capture also recorded the upstream ``type``
    value wherever a frame carried one, and that value is restored verbatim so
    the type-keyed families are exercised too.  What this reconstruction cannot
    prove is which *value* maps to which label -- separate tests do that, each
    naming the observation it rests on.
    """
    captured = json.loads(FIXTURE.read_text())
    payloads = []
    for entry in captured["events"]:
        shape = entry.get("shape")
        if shape is None:
            continue
        payload = _build(shape)
        if entry.get("type") and isinstance(payload, dict):
            payload["type"] = entry["type"]
        payloads.append((entry["index"], entry.get("kind", ""), payload))
    return payloads


def _build(node):
    if node == "str":
        return "x"
    if node == "int":
        return 1
    if node == "float":
        return 1.0
    if node == "bool":
        return True
    if node == "null":
        return None
    if node == "{}":
        return {}
    if isinstance(node, dict) and node.get("type") == "array":
        items = node.get("items")
        if items in (None, "empty"):
            return []
        return [_build(items)]
    if isinstance(node, dict):
        return {key: _build(value) for key, value in node.items()}
    return None


# ---------------------------------------------------------------------------
# Totality over the real shape set
# ---------------------------------------------------------------------------

def test_every_observed_plus_shape_is_classified_and_projected():
    """A real 23-event Plus turn must not contain one frame we mishandle."""
    payloads = _shape_payloads()
    assert len(payloads) >= 15, "the capture must actually be exercised"
    families = set()
    for index, kind, payload in payloads:
        got_kind, got_payload = classify(event(payload))
        assert got_kind is not None, f"event {index} ({kind}) was dropped outright"
        families.add(got_kind)
        projection = project_event(got_payload, got_kind)
        assert isinstance(projection, dict), f"event {index} produced no projection"
        assert set(projection) <= EVENT_PROJECTION_KEYS, (
            f"event {index} emitted a key outside the whitelist: "
            f"{sorted(set(projection) - EVENT_PROJECTION_KEYS)}")
    # Not vacuous: the real shapes must land in more than the unknown bucket.
    assert len(families) >= 3, families
    assert families != {rp.KIND_OTHER_JSON}


def test_the_capture_covers_the_families_the_projection_claims_to_know():
    """The four naming families the UI maps are genuinely in the real capture."""
    observed = {entry.get("type") or entry.get("kind")
                for entry in json.loads(FIXTURE.read_text())["events"]}
    for family in ("message_marker", "server_ste_metadata", "message_stream_complete",
                   "conversation_async_status", "title_generation", "input_message"):
        assert family in observed, f"{family} is claimed but was never observed"


@pytest.mark.parametrize("payload", [
    {"type": "server_ste_metadata", "conversation_id": "c1", "metadata": {
        "request_id": "PRIVATE-REQUEST-ID", "turn_trace_id": "PRIVATE-TRACE",
        "user_agent": "PRIVATE-UA", "tool_name": "web.run", "tool_invoked": True}},
    {"c": 1, "v": {"conversation_id": "c1", "message": {
        "content": {"content_type": "thoughts", "parts": ["PROMPT-TEXT-MUST-NOT-SURVIVE"]},
        "metadata": {"request_id": "PRIVATE-REQUEST-ID"},
        "recipient": "web.run", "status": "in_progress"}}},
], ids=["ste-metadata", "message-snapshot"])
def test_projection_carries_no_identity_metadata_or_body_text(payload):
    blob = json.dumps(project_event(payload), ensure_ascii=False)
    for leak in ("PRIVATE-REQUEST-ID", "PRIVATE-TRACE", "PRIVATE-UA",
                 "PROMPT-TEXT-MUST-NOT-SURVIVE"):
        assert leak not in blob


# ---------------------------------------------------------------------------
# No invented numbers
# ---------------------------------------------------------------------------

def test_no_percentage_eta_or_step_number_exists_anywhere():
    """The real stream carries no plan or progress event, so none may appear.

    The tokens are specific enough not to collide with the protocol vocabulary
    the module legitimately uses (``metadata_*``, ``updated_at``).
    """
    source = Path(rp.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ("percent", "progress_ratio", "progressbar", "total_steps",
                      "step_index", "estimated_", "eta_seconds", "remain_seconds"):
        assert forbidden not in source, f"{forbidden} implies progress the upstream never sent"


def test_labels_are_a_closed_set_that_only_describes_observed_activity():
    """Every label is reachable from an observed field; none is a stage name."""
    assert all(label for label in rp.OBSERVED_ACTION_LABELS.values())
    blob = " ".join(rp.OBSERVED_ACTION_LABELS.values())
    for forbidden in ("%", "第", "步", "阶段"):
        assert forbidden not in blob
    # The mapping is keyed by real observed event types only.
    assert set(rp.OBSERVED_ACTION_LABELS) <= rp.OBSERVED_EVENT_TYPES


def test_unknown_event_contributes_only_a_generic_activity_counter():
    """An unclassifiable event must not become an activity claim."""
    payload = {"type": "phase_the_gateway_does_not_know", "count": 3}
    projection = project_event(payload, classify(event(payload))[0])
    assert projection["action"] == rp.ACTION_WAITING
    assert projection["family"] not in rp.OBSERVED_EVENT_TYPES
    assert projection["research_confirmed"] is False
    assert "phase_the_gateway_does_not_know" not in json.dumps(projection)


# ---------------------------------------------------------------------------
# Sources: evidenced, distinct, never invented
# ---------------------------------------------------------------------------

def _primary(**kw):
    return dict(model="gpt-5-6-thinking",
                system_hints=["plugin:connector_openai_deep_research"], **kw)


def test_sources_are_only_reported_when_a_source_container_was_observed():
    """Two different statements: "upstream listed no sources" and "no source
    list was ever seen".  The panel must be able to tell them apart."""
    seen_empty = {"c": 4, "v": {"conversation_id": "c1", "message": {
        "content": {"content_type": "text", "parts": ["x"]}, "status": "in_progress",
        "metadata": {"citations": [], "content_references": [],
                     "search_result_groups": [], "selected_sources": []}}}}
    never_seen = {"c": 5, "v": {"conversation_id": "c1", "message": {
        "content": {"content_type": "thoughts", "parts": ["x"]}, "status": "in_progress",
        "metadata": {"model_slug": "gpt-5-6-thinking"}}}}

    empty = project_event(seen_empty)
    absent = project_event(never_seen)
    assert empty["sources"] == 0 and empty["sources_evidenced"] is True
    assert absent["sources"] == 0 and absent["sources_evidenced"] is False


def test_source_containers_observed_in_the_real_capture_are_all_recognised():
    """The container names come from the capture's own metadata key list."""
    container = {"citations": [], "content_references": [], "search_result_groups": [],
                 "selected_sources": [], "caterpillar_selected_sources": [],
                 "selected_mcp_sources": []}
    for name in container:
        payload = {"c": 1, "v": {"conversation_id": "c1", "message": {
            "content": {"content_type": "text", "parts": ["x"]},
            "metadata": {name: [{"url": "https://example.test/a"}]}}}}
        assert project_event(payload)["sources"] == 1, f"{name} was not recognised"


def test_sources_are_counted_by_distinct_identifier_value_without_retaining_urls():
    payload = {"c": 12, "v": {"conversation_id": "c1", "message": {
        "content": {"content_type": "text", "parts": ["x"]},
        "metadata": {"content_references": [
            {"type": "sources", "items": [{"url": "https://example.test/a"},
                                          {"url": "https://example.test/a"}]},
            {"type": "sources", "items": [{"url": "https://example.test/b"},
                                          {"url": "https://example.test/c"}]},
            {"url": "https://example.test/b"}]}}}}
    projection = project_event(payload)
    assert projection["sources"] == 3
    assert "example.test" not in json.dumps(projection)


def test_resource_uri_sources_are_counted_too():
    """``resolved_pineapple_uri`` / ``resource_uri`` are observed key names."""
    payload = {"c": 18, "v": {"conversation_id": "c1", "message": {
        "content": {"content_type": "text", "parts": ["x"]},
        "metadata": {"selected_sources": [
            {"resource_uri": "connector://a"}, {"resource_uri": "connector://b"}]}}}}
    assert project_event(payload)["sources"] == 2


# ---------------------------------------------------------------------------
# Researchness, errors, sanitisation
# ---------------------------------------------------------------------------

def test_deep_research_version_metadata_confirms_a_research_turn_without_storing_its_value():
    payload = {"c": 4, "v": {"conversation_id": "c1", "message": {
        "content": {"content_type": "text", "parts": ["x"]},
        "metadata": {"deep_research_version": "PRIVATE-VERSION-VALUE",
                     "venus_model_variant": "PRIVATE-VARIANT"}}}}
    projection = project_event(payload)
    assert projection["research_confirmed"] is True
    assert "PRIVATE-VERSION-VALUE" not in json.dumps(projection)
    assert "PRIVATE-VARIANT" not in json.dumps(projection)


def test_marker_tokens_are_shape_checked_so_prompt_text_cannot_pass():
    """A marker is a short protocol token; a prompt or a URL never is."""
    payload = {"conversation_id": "c1", "type": "message_marker", "message_id": "m1",
               "event": "search", "marker": "Please research the following for me"}
    projection = project_event(payload, KIND_MESSAGE_MARKER)
    assert projection["markers"] == ["search"]
    assert "Please research" not in json.dumps(projection)
    for rejected in ("https://example.test/private?q=1", "has space", "x" * 40, ""):
        assert project_event({"conversation_id": "c1", "type": "message_marker",
                              "event": rejected})["markers"] == []


def test_error_payloads_report_failure_without_echoing_the_error_text():
    payload = {"c": 20, "v": {"conversation_id": "c1", "error": "PRIVATE-UPSTREAM-ERROR",
                              "error_code": "PRIVATE-CODE",
                              "message": {"content": {"content_type": "text", "parts": ["x"]},
                                          "status": "failed"}}}
    projection = project_event(payload)
    assert projection["terminal_state"] == "failed"
    assert "PRIVATE-UPSTREAM-ERROR" not in json.dumps(projection)
    assert "PRIVATE-CODE" not in json.dumps(projection)


@pytest.mark.parametrize("payload", [
    {"type": "message", "message": None},
    {"type": "message", "message": "a string"},
    {"type": "message", "message": []},
    {"c": 1, "v": "not-a-dict"},
    {"c": 1, "v": {"conversation_id": "c1", "message": {"content": "a string"}}},
    {"c": 1, "v": {"conversation_id": "c1", "message": {"metadata": "a string"}}},
    {"c": 1, "v": {"conversation_id": "c1", "message": {"content": {"parts": "x"}}}},
    {"c": 1, "v": {"conversation_id": "c1", "message": {"author": {"role": None}}}},
    {"conversation_id": "c1", "type": "input_message", "input_message": []},
    {"conversation_id": "c1", "async_status": {"nested": {"deep": {"deeper": []}}}},
    {"v": [{"o": "append", "p": "/x", "v": 1}, "not-a-dict", None]},
    {"type": ["a", "list"]},
    {"type": None},
    {},
], ids=["message-null", "message-string", "message-list", "v-string", "content-string",
        "metadata-string", "parts-string", "role-null", "input-message-list",
        "async-nested", "patch-list-mixed", "type-list", "type-null", "empty"])
def test_projection_is_total_over_malformed_payloads(payload):
    """A malformed frame must not end the turn's progress, let alone raise."""
    assert isinstance(project_event(payload), dict)
    assert set(project_event(payload)) <= EVENT_PROJECTION_KEYS


def test_projection_survives_a_truncated_json_body():
    truncated = event('{"c": 1, "v": {"conversation_id": "c1", "message": {"id"')
    kind, payload = classify(truncated)
    assert payload is None
    assert kind == rp.KIND_NON_JSON
    assert isinstance(project_event(payload, kind), dict)


def test_a_large_source_container_is_walked_without_unbounded_work():
    """Bounds are what keep one upstream frame from stalling the event loop."""
    entries = [{"url": f"https://example.test/{i}"} for i in range(4000)]
    payload = {"c": 1, "v": {"conversation_id": "c1", "message": {
        "content": {"content_type": "text", "parts": ["x"]},
        "metadata": {"content_references": entries}}}}
    assert project_event(payload)["sources"] <= 4000


# ---------------------------------------------------------------------------
# Terminal states and the frozen clock
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    ({"conversation_id": "c1", "type": "message_stream_complete"}, "complete"),
    ({"c": 1, "v": {"conversation_id": "c1", "message": {
        "author": {"role": "assistant"}, "end_turn": True,
        "metadata": {"is_complete": True},
        "status": "finished_successfully"}}}, "complete"),
    ({"c": 1, "v": {"conversation_id": "c1", "message": {
        "author": {"role": "assistant"}, "status": "failed"}}}, "failed"),
    ({"c": 1, "v": {"conversation_id": "c1", "message": {
        "author": {"role": "assistant"}, "status": "cancelled"}}}, "cancelled"),
    ({"c": 1, "v": {"conversation_id": "c1", "message": {
        "author": {"role": "assistant"}, "status": "incomplete"}}}, "failed"),
], ids=["stream-complete", "finished-successfully", "failed", "cancelled", "incomplete"])
def test_terminal_state_comes_only_from_an_observed_terminal_signal(payload, expected):
    assert project_event(payload)["terminal_state"] == expected


def test_silence_is_not_a_terminal_signal():
    payload = {"c": 1, "v": {"conversation_id": "c1", "message": {
        "content": {"content_type": "thoughts", "parts": ["x"]}, "status": "in_progress"}}}
    assert project_event(payload)["terminal_state"] == ""


# ---------------------------------------------------------------------------
# Only the assistant's own answer may speak for the turn
# ---------------------------------------------------------------------------
# A message's ``status`` describes that message, not the turn.  The capture
# holds two ``input_message`` echoes (fixture indices 6 and 7) -- the user's own
# question, replayed upstream with its own ``status`` and a null ``end_turn`` --
# and a family of hidden/internal assistant submessages.  Reading the echo as a
# turn terminal froze the panel as complete before the research had started.
#
# The capture records only field *shapes*, not values, so the completion rule is
# narrowed to evidence the capture does show: ``author.role`` and ``status`` on
# every message frame, ``end_turn`` on every message frame, and
# ``metadata.is_complete`` on exactly one frame (index 12, the assistant's
# answer).  The values below are synthetic.

def _fixture_frame(index):
    """One payload rebuilt from the recorded capture's shape for that event."""
    for entry in json.loads(FIXTURE.read_text())["events"]:
        if entry["index"] == index:
            payload = _build(entry["shape"])
            if entry.get("type") and isinstance(payload, dict):
                payload["type"] = entry["type"]
            return payload
    raise AssertionError(f"the fixture has no event {index}")


INPUT_MESSAGE_ECHOES = (6, 7)          # the capture's two input_message frames


@pytest.mark.parametrize("index", INPUT_MESSAGE_ECHOES)
def test_a_user_message_echo_that_is_already_finished_never_ends_the_turn(index):
    payload = _fixture_frame(index)
    echo = payload["input_message"]
    echo["author"]["role"] = "user"
    echo["status"] = "finished_successfully"
    echo["end_turn"] = None                        # null in the capture
    projection = project_event(payload, KIND_INPUT_MESSAGE)
    assert projection["terminal_state"] == "", \
        "the echo of the user's own question is not the turn's terminal"


def test_a_user_message_echo_cannot_fail_the_turn_either():
    payload = _fixture_frame(INPUT_MESSAGE_ECHOES[0])
    echo = payload["input_message"]
    echo["author"]["role"] = "user"
    echo["status"] = "incomplete"
    assert project_event(payload, KIND_INPUT_MESSAGE)["terminal_state"] == ""


def test_a_fixture_shaped_assistant_submessage_does_not_end_the_turn():
    """An assistant submessage (the tool node) finishes without ending the turn.

    The capture records ``end_turn`` as a boolean on several assistant frames
    and as null on others, but not which is which, so the submessage is built
    from the observed shape with the values the *intermediate* role implies.
    """
    payload = _fixture_frame(8)                    # an observed message_delta frame
    message = payload["v"]["message"]
    message["author"]["role"] = "assistant"
    message["status"] = "finished_successfully"
    message["end_turn"] = None
    message["metadata"].pop("is_complete", None)   # never observed on this frame
    assert project_event(payload)["terminal_state"] == ""


def test_end_turn_alone_is_not_the_answer_frame():
    """A tool submessage may carry end_turn too; the answer frame carries is_complete."""
    payload = _fixture_frame(18)                   # a tool-bearing assistant frame
    message = payload["v"]["message"]
    message["author"]["role"] = "assistant"
    message["status"] = "finished_successfully"
    message["end_turn"] = True
    assert "is_complete" not in message.get("metadata", {}), \
        "the capture shows is_complete on the answer frame only"
    assert project_event(payload)["terminal_state"] == ""


def test_a_fixture_shaped_assistant_answer_completes_only_with_its_evidence():
    payload = _fixture_frame(12)                   # the answer frame
    message = payload["v"]["message"]
    message["author"]["role"] = "assistant"
    message["status"] = "finished_successfully"
    message["metadata"]["is_complete"] = True      # observed on this frame
    message["end_turn"] = False
    assert project_event(payload)["terminal_state"] == ""
    message["end_turn"] = True
    assert project_event(payload)["terminal_state"] == "complete", \
        "the assistant's end_turn answer frame is the observed completion evidence"
    message["author"]["role"] = "user"
    assert project_event(payload)["terminal_state"] == "", \
        "the same evidence on a user message still says nothing about the turn"


def test_an_explicit_failure_or_cancellation_needs_no_end_turn():
    """A cancelled/failed turn may be replayed without end_turn -- keep the signal."""
    for status, expected in (("cancelled", "cancelled"), ("failed", "failed"),
                             ("incomplete", "failed")):
        payload = {"c": 4, "v": {"conversation_id": "c1", "message": {
            "author": {"role": "assistant"}, "end_turn": None, "status": status}}}
        assert project_event(payload)["terminal_state"] == expected, status


def test_a_full_fixture_shaped_turn_without_a_turn_terminal_is_never_complete():
    """The echo alone must not freeze the record; a turn-level frame still does."""
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=True)
    echo = _fixture_frame(INPUT_MESSAGE_ECHOES[0])
    echo["input_message"]["author"]["role"] = "user"
    echo["input_message"]["status"] = "finished_successfully"
    assert recorder.record(event(echo)) is True
    # The observed turn-level status frame, which is not a completion either.
    assert recorder.record(event({"conversation_id": "c1", "type": "conversation_async_status",
                                  "async_status": 1})) is True
    assert recorder.record(event({"c": 2, "v": {"conversation_id": "c1", "message": {
        "author": {"role": "assistant"},
        "content": {"content_type": "thoughts", "parts": ["x"]},
        "status": "in_progress"}}})) is True

    view = store.active_snapshot(recorder.owner)
    assert view["state"] == "streaming"
    assert view["projection"]["finished"] is False

    # The observed turn terminal still ends it, exactly once.
    assert recorder.record(event({"conversation_id": "c1",
                                  "type": "message_stream_complete"})) is True
    view = store.active_snapshot(recorder.owner)
    assert view["state"] == "complete"
    assert view["projection"]["finished"] is True


def _store():
    return ResearchProgressStore(max_events=64, max_conversations=8, max_bytes=1 << 20)


def test_elapsed_time_freezes_once_the_turn_is_terminal():
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=True)
    recorder.record(event({"c": 1, "v": {"conversation_id": "c1",
                                         "message": {"status": "in_progress"}}}))
    recorder.finish("complete")
    first = store.active_snapshot(recorder.owner)["projection"]
    assert first["finished"] is True
    import time
    time.sleep(0.05)
    second = store.active_snapshot(recorder.owner)["projection"]
    assert second["elapsed_ms"] == first["elapsed_ms"], \
        "a finished turn's clock must not keep running"
    assert second["finished_at"] == first["finished_at"]


def test_an_unfinished_turn_keeps_accumulating_elapsed_time():
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=True)
    recorder.record(event({"c": 1, "v": {"conversation_id": "c1",
                                         "message": {"status": "in_progress"}}}))
    first = store.active_snapshot(recorder.owner)["projection"]
    assert first["finished"] is False
    import time
    time.sleep(0.05)
    second = store.active_snapshot(recorder.owner)["projection"]
    assert second["elapsed_ms"] > first["elapsed_ms"]


def test_active_projection_is_exactly_the_whitelist_and_never_the_event_text():
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=True)
    recorder.record(event({"c": 1, "v": {"conversation_id": "c1", "message": {
        "content": {"content_type": "thoughts", "parts": ["PRIVATE"]},
        "status": "in_progress"}}}))
    snapshot = store.active_snapshot(recorder.owner)
    assert set(snapshot["projection"]) == PROJECTION_KEYS
    assert "PRIVATE" not in json.dumps(snapshot)
    assert "events" not in snapshot and "text" not in json.dumps(snapshot)


def test_projection_is_bounded_across_many_frames():
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=True)
    for i in range(200):
        recorder.record(event({"conversation_id": "c1", "type": "message_marker",
                               "event": f"token{i}", "marker": f"marker{i}"}))
        recorder.record(event({"c": i, "v": {"conversation_id": "c1", "message": {
            "content": {"content_type": f"type{i}", "parts": ["x"]}}}}))
    projection = store.active_snapshot(recorder.owner)["projection"]
    assert len(projection["markers"]) <= rp.MAX_MARKERS
    assert len(projection["content_types"]) <= rp.MAX_CONTENT_TYPES


def test_projection_only_accumulates_for_research_turns():
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=False)
    recorder.record(event({"c": 1, "v": {"conversation_id": "c1", "message": {
        "content": {"content_type": "thoughts", "parts": ["x"]}}}}))
    assert store.active_snapshot(recorder.owner) == {"research": False}


# ---------------------------------------------------------------------------
# The families the new classification adds must still be forward-compatible
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    ({"c": 1, "o": "append", "p": "/x", "v": {"conversation_id": "c1"}}, rp.KIND_MESSAGE_DELTA),
    ({"c": 5, "o": "add", "v": [{"o": "append", "p": "/a", "v": 1},
                                {"o": "append", "p": "/b", "v": 2}]}, rp.KIND_MESSAGE_DELTA),
    ({"c": 2, "v": {"conversation_id": "c1", "message": {"id": "m"}}},
     rp.KIND_MESSAGE_SNAPSHOT),
    ({"conversation_id": "c1", "title": "t", "type": "title_generation"},
     KIND_TITLE_GENERATION),
    ({"conversation_id": "c1", "type": "conversation_async_status",
      "async_status": "running"}, rp.KIND_ASYNC_STATUS),
    ({"conversation_id": "c1", "type": "server_ste_metadata", "metadata": {}},
     KIND_STE_METADATA),
    ({"conversation_id": "c1", "type": "message_stream_complete"}, KIND_STREAM_COMPLETE),
    ({"conversation_id": "c1", "input_message": {"id": "m"}, "type": "input_message"},
     KIND_INPUT_MESSAGE),
], ids=["patch", "patch-list", "snapshot", "title", "async-status", "ste-metadata",
        "stream-complete", "input-message"])
def test_real_families_are_classified(payload, expected):
    assert classify(event(payload))[0] == expected


def test_an_unknown_type_still_falls_back_to_other_json():
    assert classify(event({"type": "unclassified_future_phase"}))[0] == rp.KIND_OTHER_JSON
