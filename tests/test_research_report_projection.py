"""The final report: the mirror shows the answer the official UI does not.

Measured on a real turn (2026-09-13): the request was a research turn, the
upstream ``text/event-stream`` completed, and the browser still showed no report
-- the official research region is keyed on a model identifier a normal browser
cannot resolve, so it resolved to a browser error page while the answer itself
had already been streamed into the mirror.  The mirror's own panel is the
supported path, and this file pins what it may show.

The fixture (``deep_research_plus_shapes.json``) records field *names*, not
values, so every payload below is rebuilt from one observed shape and given
synthetic values.  Which shape carries the answer is not invented: the capture
shows exactly one frame with ``metadata.is_complete`` -- index 12, the assistant
message that carries ``citations``, ``content_references``, ``finish_details``
and a ``content.text`` body -- and the module's own terminal detection already
established that frame as the one that ends the turn.

What must never happen, and is checked here:

* the user's own question (replayed upstream as an ``input_message`` echo) must
  never be shown as the report;
* a hidden or tool message must never be shown as the report;
* the model's reasoning must never be shown as the report;
* the report must be bounded and free of control characters;
* a turn whose stream carried no answer body must project an empty report, not
  an invented one.
"""

import json
from pathlib import Path

import pytest

from gateway import research_progress as rp
from gateway.research_progress import (
    EVENT_PROJECTION_KEYS,
    KIND_INPUT_MESSAGE,
    MAX_REPORT_CHARS,
    ResearchProgressStore,
    project_event,
)

FIXTURE = Path(__file__).parent / "fixtures" / "deep_research_plus_shapes.json"

ANSWER_FRAME = 12      # the only captured frame carrying metadata.is_complete
ECHO_FRAMES = (6, 7)   # the capture's two input_message echoes
TOOL_FRAME = 18        # assistant frame carrying invoked_resource/search_result_groups

# The real turn's page rendered this as an unresolvable iframe.  It is a model
# identifier comparison inside the official bundle, so it is not a URL the
# gateway may ever treat as one -- and it must never reach the projection.
SENTINEL = "internal://deep-research"

PROMPT = "PRIVATE-PROMPT-TEXT-MUST-NOT-SURVIVE"


def event(payload):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return ("data: " + body + "\n\n").encode()


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


def _fixture_frame(index):
    """One payload rebuilt from the recorded capture's shape for that event."""
    for entry in json.loads(FIXTURE.read_text())["events"]:
        if entry["index"] == index:
            payload = _build(entry["shape"])
            if entry.get("type") and isinstance(payload, dict):
                payload["type"] = entry["type"]
            return payload
    raise AssertionError(f"the fixture has no event {index}")


def _message(index):
    payload = _fixture_frame(index)
    return payload, payload["v"]["message"]


def _answer(body="报告正文：一项研究结论。", **content):
    """The observed answer frame, carrying a synthetic body."""
    payload, message = _message(ANSWER_FRAME)
    message["author"]["role"] = "assistant"
    message["status"] = "finished_successfully"
    message["end_turn"] = True
    message["metadata"]["is_complete"] = True
    message["content"].pop("parts", None)
    message["content"].update({"content_type": "text", "text": body})
    message["content"].update(content)
    return payload


def _store():
    return ResearchProgressStore(max_events=64, max_conversations=8, max_bytes=1 << 20)


# ---------------------------------------------------------------------------
# The answer reaches the projection
# ---------------------------------------------------------------------------

def test_the_capture_s_answer_frame_projects_its_body_and_is_marked_final():
    """Index 12 is the frame the capture shows ending the turn; so does its body."""
    projection = project_event(_answer("结论：X 与 Y 相关。"))
    assert projection["report"] == "结论：X 与 Y 相关。"
    assert projection["report_final"] is True
    assert projection["report_truncated"] is False


def test_a_widget_answer_frame_still_projects_its_text_and_never_the_region_key():
    """The real gap, reproduced from the answer frame's own shape.

    The official page draws its research region for this message and that region
    does not resolve in a normal browser.  The mirror cannot fix the bundle, so
    what it must do is exactly this: show the text the upstream sent.
    """
    payload = _answer("最终研究报告正文。")
    message = payload["v"]["message"]
    # The observed SDK/widget metadata on this frame, pointed at the sentinel.
    message["metadata"]["chatgpt_sdk"] = dict(
        message["metadata"].get("chatgpt_sdk") or {},
        resolved_pineapple_uri=SENTINEL, resource_name="deep_research")
    projection = project_event(payload)
    assert projection["report"] == "最终研究报告正文。"
    assert SENTINEL not in json.dumps(projection, ensure_ascii=False)
    assert "deep_research" not in json.dumps(projection)


def test_a_parts_shaped_answer_body_is_projected_too():
    """Every other message frame in the capture carries ``content.parts``."""
    payload, message = _message(ANSWER_FRAME)
    message["author"]["role"] = "assistant"
    message["status"] = "in_progress"
    message["content"] = {"content_type": "text", "parts": ["第一段", "第二段"]}
    assert project_event(payload)["report"] == "第一段\n第二段"


def test_an_unmarked_answer_is_projected_but_not_claimed_as_final():
    """Upstream sent a body without the end-of-turn evidence: say so."""
    payload, message = _message(ANSWER_FRAME)
    message["author"]["role"] = "assistant"
    message["status"] = "in_progress"
    message["end_turn"] = None
    message["content"] = {"content_type": "text", "text": "草稿正文"}
    projection = project_event(payload)
    assert projection["report"] == "草稿正文"
    assert projection["report_final"] is False


def test_a_turn_that_streamed_no_answer_body_projects_an_empty_report():
    """Nothing to show is a fact about upstream, not a licence to invent one."""
    payload, message = _message(8)
    message["author"]["role"] = "assistant"
    message["content"] = {"content_type": "text", "parts": []}
    projection = project_event(payload)
    assert projection["report"] == ""
    assert projection["report_final"] is False


# ---------------------------------------------------------------------------
# What may never become the report
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("index", ECHO_FRAMES)
def test_the_echo_of_the_user_s_own_question_is_never_the_report(index):
    """The user's question is replayed upstream with a body of its own."""
    payload = _fixture_frame(index)
    echo = payload["input_message"]
    echo["author"]["role"] = "user"
    echo["status"] = "finished_successfully"
    echo["content"] = {"content_type": "text", "parts": [PROMPT]}
    projection = project_event(payload, KIND_INPUT_MESSAGE)
    assert projection["report"] == ""
    assert PROMPT not in json.dumps(projection, ensure_ascii=False)


def test_an_assistant_message_that_repeats_the_prompt_is_not_a_user_echo_leak():
    """Guard the other direction: the rule is the role, not the absence of text.

    A tool or hidden message is excluded for its own reasons -- this test pins
    that the *echo* exclusion is driven by ``author.role``, so a future edit
    cannot "fix" a leak by emptying every report.
    """
    payload = _answer("正文引用了用户提出的问题：" + PROMPT)
    projection = project_event(payload)
    assert PROMPT in projection["report"], "an assistant body is the assistant's own"
    assert projection["report_final"] is True


def test_a_visually_hidden_message_is_never_the_report():
    payload = _answer("内部子消息，不应显示")
    payload["v"]["message"]["metadata"]["is_visually_hidden_from_conversation"] = True
    assert project_event(payload)["report"] == ""


@pytest.mark.parametrize("content_type", ["thoughts", "reasoning_recap"])
def test_reasoning_is_never_the_report(content_type):
    """The official UI does not show the model's reasoning either."""
    payload = _answer("推理过程")
    payload["v"]["message"]["content"]["content_type"] = content_type
    projection = project_event(payload)
    assert projection["report"] == ""
    assert projection["report_final"] is False


def test_a_tool_frame_body_is_never_the_report():
    """``web.run`` is the observed tool recipient; its body is tool payload."""
    payload, message = _message(TOOL_FRAME)
    message["author"]["role"] = "assistant"
    message["status"] = "finished_successfully"
    message["end_turn"] = True
    message["content"] = {"content_type": "text", "parts": ["TOOL-PAYLOAD-TEXT"]}
    projection = project_event(payload)
    assert projection["report"] == ""
    assert "TOOL-PAYLOAD-TEXT" not in json.dumps(projection)


def test_a_tool_recipient_is_excluded_even_without_the_invoked_metadata():
    payload = _answer("TOOL-PAYLOAD-TEXT")
    payload["v"]["message"]["recipient"] = "web.run"
    assert project_event(payload)["report"] == ""


# ---------------------------------------------------------------------------
# Bounded, sanitised, and inside the whitelist
# ---------------------------------------------------------------------------

def test_the_report_is_bounded_and_control_characters_are_removed():
    payload = _answer("正文\x00\x07开头" + "长" * (MAX_REPORT_CHARS * 2) + "\x1b[31m")
    projection = project_event(payload)
    report = projection["report"]
    assert projection["report_truncated"] is True
    assert len(report) <= MAX_REPORT_CHARS + len(rp._REPORT_TRUNCATION)
    for control in ("\x00", "\x07", "\x1b"):
        assert control not in report
    assert "已截断" in report


def test_a_report_inside_the_bound_is_not_marked_truncated():
    projection = project_event(_answer("短报告"))
    assert projection["report_truncated"] is False
    assert "已截断" not in projection["report"]


def test_an_upstream_sized_parts_array_is_not_walked_without_a_bound():
    payload = _answer()
    payload["v"]["message"]["content"] = {
        "content_type": "text", "parts": ["字" * 500] * 5000}
    projection = project_event(payload)
    assert projection["report"]
    assert len(projection["report"]) <= MAX_REPORT_CHARS + len(rp._REPORT_TRUNCATION)


def test_the_report_stays_inside_the_projection_whitelist():
    """A future key must be added to the closed set, not smuggled in."""
    projection = project_event(_answer("正文"))
    assert set(projection) <= EVENT_PROJECTION_KEYS
    assert projection["report"]


# ---------------------------------------------------------------------------
# The record: which frame's body survives, and for how long
# ---------------------------------------------------------------------------

def test_the_frame_that_ends_the_turn_outranks_an_earlier_interim_body():
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=True)
    interim, interim_message = _message(ANSWER_FRAME)
    interim_message["author"]["role"] = "assistant"
    interim_message["content"] = {"content_type": "text", "text": "中间草稿"}
    recorder.record(event(interim))
    recorder.record(event(_answer("最终研究报告")))
    projection = store.active_snapshot(recorder.owner)["projection"]
    assert projection["report"] == "最终研究报告"
    assert projection["report_final"] is True


def test_a_settled_answer_is_not_overwritten_by_a_later_interim_frame():
    """A follow-up artifact after the answer must not replace the answer."""
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=True)
    recorder.record(event(_answer("最终研究报告")))
    later, later_message = _message(ANSWER_FRAME)
    later_message["author"]["role"] = "assistant"
    later_message["content"] = {"content_type": "text", "text": "后续说明"}
    recorder.record(event(later))
    projection = store.active_snapshot(recorder.owner)["projection"]
    assert projection["report"] == "最终研究报告"


def test_a_new_turn_does_not_inherit_the_previous_turns_report():
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=True)
    recorder.record(event(_answer("上一轮的报告")))
    recorder.finish("complete")
    assert store.active_snapshot(recorder.owner)["projection"]["report"] == "上一轮的报告"

    second = store.recorder("seed-a", "c1", research=True)
    second.record(event({"c": 1, "v": {"conversation_id": "c1", "message": {
        "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": ["这一轮才刚开始"]},
        "status": "in_progress"}}}))
    projection = store.active_snapshot(second.owner)["projection"]
    assert projection["report"] == "这一轮才刚开始"
    assert projection["report_final"] is False


def test_a_frame_with_no_body_never_clears_a_report_already_retained():
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=True)
    recorder.record(event(_answer("研究报告")))
    recorder.record(event({"c": 3, "v": {"conversation_id": "c1", "message": {
        "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": []},
        "status": "in_progress"}}}))
    assert store.active_snapshot(recorder.owner)["projection"]["report"] == "研究报告"


def test_the_report_is_retained_for_a_restore_and_stays_owner_scoped():
    """The refresh path reads the same record; another Seed reads nothing."""
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=True)
    recorder.record(event(_answer("可恢复的报告正文")))
    recorder.finish("complete")
    restored = store.projection_snapshot("c1", recorder.owner)
    assert restored["projection"]["report"] == "可恢复的报告正文"
    assert store.projection_snapshot("c1", "someone-else") is None


def test_a_non_research_turn_projects_no_report_at_all():
    """Ordinary chat must never appear on the panel, report included."""
    store = _store()
    recorder = store.recorder("seed-a", "c1", research=False)
    recorder.record(event(_answer("普通聊天回复")))
    assert store.active_snapshot(recorder.owner) == {"research": False}
