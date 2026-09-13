"""The incremental projection: the family a production turn actually streams.

``gateway/f_conversation_gateway.rewrite_f_conversation_body`` forwards the
frontend's ``supported_encodings`` to the legacy endpoint (see
``tests/test_m2_stream_encoding.py``), so the browser's turn is answered with the
delta encoding the new frontend renders from::

    {"p": "/message/content/parts/0", "o": "append", "v": "..."}
    {"o": "patch", "v": [{"p": ..., "o": ..., "v": ...}, ...], "c": 3}

Read as whole messages, every one of those frames is an anonymous patch: the
answer they build and the source containers they add are invisible.  A real
research turn would then reach the panel as "upstream sent no body" while the
browser had already been handed the entire answer -- the exact failure the
report projection exists to prevent.

These frames are synthetic *shapes*: the paths and operators are the protocol's,
the bodies are placeholders.  Nothing here claims a real capture of the delta
encoding; the negotiation contract is the evidence, and it is recorded in the
test names rather than asserted as a measurement.

What the fold must never do, and is checked here:

* attribute a body to a message whose author upstream never named -- fail
  closed, because the user's own words and a tool's payload are both bodies
  that are not the answer;
* let an unknown patch path write anything into the panel;
* keep a source value (a URL is a credential-free but still upstream datum that
  the projection deliberately reduces to a digest);
* grow without a bound, or survive into the next turn.
"""

import json

import pytest

from gateway import research_progress as rp
from gateway.research_progress import (
    PROJECTION_KEYS,
    ResearchProgressStore,
    anon_id,
    classify,
    fold_delta,
    new_delta_state,
    patch_operations,
    project_event,
)

CONVERSATION = "conversation-delta-1"
SEED = "seed-token-delta"

ANSWER_PARTS = ("研究报告：第一段。", "第二段。", "结论。")
ANSWER = "".join(ANSWER_PARTS)


def frame(payload) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode("utf-8")


def append(path, text):
    """The observed append patch: a JSON-pointer path and a string value."""
    return {"p": path, "o": "append", "v": text}


def replace(path, value):
    return {"p": path, "o": "replace", "v": value}


def batch(*ops, c=3):
    """The observed batched envelope: ``o`` plus a list of patches in ``v``."""
    return {"c": c, "o": "patch", "v": list(ops)}


def assistant_message(**over):
    message = {
        "id": "m2",
        "author": {"role": "assistant", "name": None},
        "content": {"content_type": "text", "parts": []},
        "status": "in_progress",
        "metadata": {},
    }
    message.update(over)
    return message


# The turn the official frontend renders: the message is introduced whole, then
# its body, its sources and finally its end-of-turn evidence arrive as patches.
def answer_stream(*, role=True, content_type="text", sources=True, settle=True):
    frames = [replace("/message", assistant_message(
        **({} if role else {"author": {}})))]
    if content_type != "text":
        # The content type is patched onto the message that was just introduced,
        # in the order upstream does it: message first, then its content.
        frames.append(replace("/message/content/content_type", content_type))
    frames += [append("/message/content/parts/0", part) for part in ANSWER_PARTS]
    if sources:
        frames.append(replace("/message/metadata/content_references", [
            {"type": "web", "url": "https://delta.test/one"},
            {"type": "web", "url": "https://delta.test/two"},
            {"type": "web", "url": "https://delta.test/one"},
        ]))
        frames.append(replace("/message/metadata/citations",
                              [{"metadata": {"url": "https://delta.test/one"}}]))
    if settle:
        frames += [
            replace("/message/status", "finished_successfully"),
            replace("/message/end_turn", True),
            replace("/message/metadata/is_complete", True),
        ]
    return frames


def run(frames, *, outcome="complete"):
    """Fold a stream and read the restored view.

    ``outcome=None`` leaves the turn running, which is what an assertion about
    the *live* activity label needs: a terminal outcome legitimately rewrites the
    label to how the turn ended.
    """
    store = ResearchProgressStore()
    recorder = store.recorder(SEED, CONVERSATION, research=True)
    for item in frames:
        recorder.record(frame(item))
    if outcome is not None:
        recorder.finish(outcome)
    return store, store.projection_snapshot(CONVERSATION, recorder.owner)


# ---------------------------------------------------------------------------
# The answer an incremental turn streams
# ---------------------------------------------------------------------------

def test_the_streamed_answer_is_projected_not_reported_as_missing():
    """The whole point: the body upstream streamed is the body the panel shows."""
    _, view = run(answer_stream())
    assert view["projection"]["report"] == ANSWER
    assert view["projection"]["report_truncated"] is False


def test_the_frame_that_ends_the_turn_settles_the_report():
    """``finished_successfully`` + ``end_turn`` + ``is_complete`` is the same
    end-of-turn evidence the snapshot path requires, so the body may be called
    the report rather than "the last body received"."""
    _, view = run(answer_stream())
    assert view["projection"]["report_final"] is True
    assert view["state"] == "complete"
    assert view["projection"]["finished"] is True
    assert view["projection"]["action"] == rp.ACTION_DONE


def test_an_append_that_grows_the_body_settles_on_the_longest_version():
    """A streamed body arrives in pieces; the panel must show all of them."""
    _, view = run(answer_stream())
    for part in ANSWER_PARTS:
        assert part in view["projection"]["report"]


def test_the_batched_patch_envelope_is_folded_too():
    """The batch form carries the same patches; it must not be a blind spot."""
    ops = [replace("/message", assistant_message()),
           *[append("/message/content/parts/0", part) for part in ANSWER_PARTS],
           replace("/message/status", "finished_successfully"),
           replace("/message/end_turn", True),
           replace("/message/metadata/is_complete", True)]
    _, view = run([batch(*ops)])
    assert view["projection"]["report"] == ANSWER
    assert view["state"] == "complete"


def test_patch_operations_reads_both_envelopes_and_nothing_else():
    """A frame that is not a patch must yield no operations at all."""
    assert patch_operations({"p": "/message", "o": "add", "v": {}}) == [
        ("/message", "add", {})]
    assert patch_operations({"o": "patch", "v": [{"p": "/a", "o": "add", "v": 1}]}) == [
        ("/a", "add", 1)]
    for payload in ({}, {"v": {"message": {}}}, {"p": "/a"}, "x", None,
                    {"p": 1, "o": 2, "v": 3}, {"o": "patch", "v": "x"}):
        assert patch_operations(payload) == [], payload


# ---------------------------------------------------------------------------
# Fail closed: no author, no body
# ---------------------------------------------------------------------------

def test_a_body_upstream_never_attributed_is_not_shown():
    """The author may not be guessed.

    A body with no observed author is as likely to be the user's own words or a
    tool's payload as the answer, so the projection stays empty and the panel
    says upstream sent no body -- the honest reading of "we cannot tell".
    """
    _, view = run(answer_stream(role=False))
    assert view["projection"]["report"] == ""


def test_an_author_named_by_a_later_patch_is_honoured():
    """A turn that names the author separately still shows its answer."""
    frames = [append("/message/content/parts/0", "答案正文。"),
              replace("/message/author/role", "assistant"),
              replace("/message/status", "finished_successfully"),
              replace("/message/end_turn", True),
              replace("/message/metadata/is_complete", True)]
    _, view = run(frames)
    assert view["projection"]["report"] == "答案正文。"
    assert view["projection"]["report_final"] is True


def test_a_user_authored_patch_body_is_never_the_report():
    """The echo of the user's own turn is not an answer."""
    frames = [replace("/message", assistant_message(author={"role": "user"})),
              append("/message/content/parts/0", "我的问题是什么？")]
    _, view = run(frames)
    assert view["projection"]["report"] == ""


@pytest.mark.parametrize("content_type", ["thoughts", "reasoning_recap"])
def test_reasoning_patched_incrementally_is_never_the_report(content_type):
    """Reasoning is not the answer, whether it arrives as a message or a patch."""
    _, view = run(answer_stream(content_type=content_type))
    assert view["projection"]["report"] == ""


def test_a_hidden_message_patched_incrementally_is_never_the_report():
    """The capture's own "do not show this one" flag, patched as a whole message
    first and then grown -- which is the order upstream introduces a message."""
    frames = [replace("/message", assistant_message(metadata={
                  "is_visually_hidden_from_conversation": True})),
              append("/message/content/parts/0", "内部草稿。")]
    _, view = run(frames)
    assert view["projection"]["report"] == ""


# ---------------------------------------------------------------------------
# Source evidence, still reduced to a count
# ---------------------------------------------------------------------------

def test_patched_source_containers_are_evidenced_and_counted_distinctly():
    _, view = run(answer_stream())
    projection = view["projection"]
    assert projection["sources"] == 2, "two distinct urls, not three entries"
    assert projection["sources_evidenced"] is True


def test_a_patched_source_value_never_reaches_the_projection():
    """The projection is a closed key set and carries no upstream datum."""
    _, view = run(answer_stream())
    rendered = json.dumps(view, ensure_ascii=False)
    assert "delta.test" not in rendered
    assert set(view["projection"]) == set(PROJECTION_KEYS)


def test_a_turn_that_patched_no_source_container_evidences_nothing():
    """"Upstream listed none" and "no list was seen" are different statements,
    and only the first one may be reported as evidenced."""
    _, view = run(answer_stream(sources=False))
    assert view["projection"]["sources"] == 0
    assert view["projection"]["sources_evidenced"] is False, \
        "no source container was patched, so nothing may be claimed"


def test_a_patched_empty_container_is_still_evidence_upstream_listed_sources():
    frames = [replace("/message", assistant_message()),
              replace("/message/metadata/content_references", [])]
    _, view = run(frames)
    assert view["projection"]["sources"] == 0
    assert view["projection"]["sources_evidenced"] is True


# ---------------------------------------------------------------------------
# Activity, endpoints, and the unknown
# ---------------------------------------------------------------------------

def test_activity_follows_the_patched_content_type():
    """A patch that says "writing" is an activity claim; a patch that says
    "thinking" is a different one -- both derived from observed values only."""
    writing = run([replace("/message", assistant_message()),
                   append("/message/content/parts/0", "x")],
                  outcome=None)[1]["projection"]
    assert writing["action"] == rp.ACTION_WRITING
    thinking = run([replace("/message", assistant_message(
        content={"content_type": "thoughts", "parts": []}))],
        outcome=None)[1]["projection"]
    assert thinking["action"] == rp.ACTION_ANALYSING


def test_an_explicit_failure_patch_is_not_a_completion():
    frames = [replace("/message", assistant_message()),
              append("/message/content/parts/0", "半截报告"),
              replace("/message/status", "failed")]
    store, view = run(frames, outcome="failed")
    assert store.snapshot(CONVERSATION)["state"] == "failed"
    assert view["projection"]["report_final"] is False
    assert view["projection"]["action"] == rp.ACTION_FAILED


def test_delta_envelope_error_survives_later_completion_patches():
    """An envelope error must remain failure evidence through the rest of the turn."""
    frames = [
        {"c": 1, "o": "replace", "p": "/message", "v": {
            "conversation_id": CONVERSATION,
            "error": "upstream detail must not be retained",
            "error_code": "server_error",
            "message": assistant_message(),
        }},
        replace("/message/status", "finished_successfully"),
        replace("/message/end_turn", True),
        replace("/message/metadata/is_complete", True),
    ]
    store, view = run(frames, outcome="complete")
    assert store.snapshot(CONVERSATION)["state"] == "failed"
    assert view["projection"]["action"] == rp.ACTION_FAILED
    assert view["projection"]["finished"] is True
    assert "upstream detail" not in json.dumps(view)


def test_error_wins_when_same_frame_also_contains_completion_evidence():
    """An error must not be lost when upstream combines it with completion."""
    message = assistant_message(
        status="finished_successfully",
        end_turn=True,
        metadata={"is_complete": True},
    )
    store, view = run([replace("/message", {
        "conversation_id": CONVERSATION,
        "error": "upstream detail",
        "error_code": "server_error",
        "message": message,
    })])
    assert store.snapshot(CONVERSATION)["state"] == "failed"
    assert view["projection"]["action"] == rp.ACTION_FAILED
    assert "upstream detail" not in json.dumps(view)


def test_batched_nested_error_survives_later_completion_patches():
    """A batched operation value is still an error envelope, not a success."""
    nested = {
        "conversation_id": CONVERSATION,
        "error": "nested detail",
        "error_code": "nested_error",
        "message": assistant_message(),
    }
    frames = [
        replace("/message", assistant_message()),
        batch(replace("/message", nested)),
        replace("/message/status", "finished_successfully"),
        replace("/message/end_turn", True),
        replace("/message/metadata/is_complete", True),
    ]
    store, view = run(frames)
    assert store.snapshot(CONVERSATION)["state"] == "failed"
    assert view["projection"]["action"] == rp.ACTION_FAILED
    assert "nested detail" not in json.dumps(view)


def test_unrelated_nested_error_fields_do_not_fail_a_healthy_snapshot():
    """Only protocol envelope errors count, not source/message metadata."""
    message = assistant_message(
        status="finished_successfully",
        end_turn=True,
        metadata={
            "is_complete": True,
            "content_references": [{"url": "https://example.test", "error": False}],
        },
    )
    store, view = run([{"v": {"message": message}}])
    assert store.snapshot(CONVERSATION)["state"] == "complete"
    assert view["projection"]["action"] == rp.ACTION_DONE


@pytest.mark.parametrize("value", [False, 0, [], {}])
def test_falsy_error_values_do_not_fail_a_healthy_envelope(value):
    """The protocol uses null/empty values for no error, not falsy sentinels."""
    message = assistant_message(
        status="finished_successfully",
        end_turn=True,
        metadata={"is_complete": True},
    )
    store, view = run([{"v": {
        "conversation_id": CONVERSATION,
        "error": value,
        "error_code": None,
        "message": message,
    }}])
    assert store.snapshot(CONVERSATION)["state"] == "complete"
    assert view["projection"]["action"] == rp.ACTION_DONE


def test_a_cancelled_patch_is_not_a_completion():
    frames = [replace("/message", assistant_message()),
              replace("/message/status", "cancelled")]
    store, _ = run(frames, outcome="cancelled")
    assert store.snapshot(CONVERSATION)["state"] == "cancelled"


def test_end_turn_alone_is_not_enough_for_the_incremental_path():
    """The same conjunction the snapshot path requires: status *and* end_turn
    *and* is_complete.  A tool submessage carries end_turn too."""
    frames = [replace("/message", assistant_message()),
              append("/message/content/parts/0", "答案"),
              replace("/message/end_turn", True)]
    store, view = run(frames, outcome="failed")
    assert view["projection"]["finished"] is True
    assert view["state"] == "failed", "no is_complete was observed"
    assert view["projection"]["report_final"] is False


def test_an_unknown_patch_path_contributes_nothing_but_stays_observable():
    """It must not become an activity claim, a body, or a silent drop."""
    store = ResearchProgressStore()
    recorder = store.recorder(SEED, CONVERSATION, research=True)
    recorder.record(frame(replace("/message/metadata/secret_field", "PROMPT-TEXT")))
    recorder.record(frame(replace("/some/new/upstream/path", 42)))
    view = store.projection_snapshot(CONVERSATION, recorder.owner)
    assert view["projection"]["report"] == ""
    assert view["projection"]["action"] == rp.ACTION_WAITING
    assert "PROMPT-TEXT" not in json.dumps(view)
    assert store.snapshot(CONVERSATION)["events_seen"] == 2, \
        "an unreadable patch is still an observed event"


@pytest.mark.parametrize("payload", [
    {"p": None, "o": "append", "v": "x"},
    {"p": "/message/content/parts/0", "o": None, "v": "x"},
    {"p": "/message/content/parts/0", "o": "append", "v": {"not": "text"}},
    {"p": "/message/content/parts/0", "o": "explode", "v": "x"},
    {"p": "/message/content/parts/notanumber", "o": "append", "v": "x"},
    {"o": "patch", "v": ["not-an-op", {"p": "/message"}, 5]},
    {"p": "/message/status", "o": "replace", "v": {"nested": True}},
    {"p": "/message/end_turn", "o": "replace", "v": "yes"},
])
def test_a_malformed_patch_is_total_and_never_raises(payload):
    state = new_delta_state()
    synthesized = fold_delta(payload, state)
    projection = project_event(synthesized, rp.KIND_MESSAGE_DELTA)
    assert set(projection) <= rp.EVENT_PROJECTION_KEYS
    assert projection["report"] == ""
    assert projection["terminal_state"] == ""


def test_a_frame_the_upstream_cut_mid_json_still_leaves_the_turn_alive():
    """One unparseable frame must not end the loop or freeze the turn."""
    store = ResearchProgressStore()
    recorder = store.recorder(SEED, CONVERSATION, research=True)
    recorder.record(frame(replace("/message", assistant_message())))
    recorder.record(b'data: {"p": "/message/content/parts/0", "o": "app')
    recorder.record(frame(append("/message/content/parts/0", "答案")))
    view = store.projection_snapshot(CONVERSATION, recorder.owner)
    assert view["projection"]["report"] == "答案"
    assert store.snapshot(CONVERSATION)["events_seen"] == 3


# ---------------------------------------------------------------------------
# Bounds and turn isolation
# ---------------------------------------------------------------------------

def test_the_fold_is_bounded_rather_than_linear_in_the_stream():
    frames = [replace("/message", assistant_message())]
    frames += [append("/message/content/parts/0", "x" * 500) for _ in range(200)]
    _, view = run(frames)
    assert view["projection"]["report_truncated"] is True
    assert len(view["projection"]["report"]) <= rp.MAX_REPORT_CHARS + 64


def test_a_flood_of_patch_operations_is_bounded_per_frame():
    state = new_delta_state()
    payload = {"o": "patch", "v": [append("/message/content/parts/0", "x")
                                   for _ in range(5000)]}
    fold_delta(payload, state)
    assert len(state["parts"]) <= rp.MAX_DELTA_PARTS


def test_a_source_container_patched_larger_than_the_bound_is_still_counted():
    entries = [{"url": f"https://delta.test/{index}"} for index in range(500)]
    frames = [replace("/message", assistant_message()),
              replace("/message/metadata/search_result_groups", entries)]
    _, view = run(frames)
    assert 0 < view["projection"]["sources"] <= rp.MAX_SOURCE_ENTRIES


def test_a_new_turn_does_not_inherit_the_previous_incremental_body():
    store = ResearchProgressStore()
    first = store.recorder(SEED, CONVERSATION, research=True)
    for item in answer_stream():
        first.record(frame(item))
    assert store.projection_snapshot(CONVERSATION, first.owner)["projection"]["report"]

    second = store.recorder(SEED, CONVERSATION, research=True)
    second.record(frame(replace("/message", assistant_message())))
    view = store.projection_snapshot(CONVERSATION, second.owner)
    assert view["projection"]["report"] == ""
    assert view["projection"]["sources"] == 0


def test_the_frozen_clock_of_an_incremental_turn_survives_a_later_chat_turn():
    """A follow-up chat turn retires the panel, not the frozen research clock."""
    store = ResearchProgressStore()
    recorder = store.recorder(SEED, CONVERSATION, research=True)
    for item in answer_stream():
        recorder.record(frame(item))
    frozen = store.projection_snapshot(CONVERSATION, recorder.owner)["projection"]

    recorder.finish("complete")
    chat = store.recorder(SEED, CONVERSATION, research=False)
    chat.record(frame(replace("/message", assistant_message())))
    chat.finish("complete")
    view = store.projection_snapshot(CONVERSATION, chat.owner)
    assert view["projection"]["report"] == frozen["report"], \
        "a chat turn must not take the retained research body with it"
    assert store.active_snapshot(anon_id(SEED)) == {"research": False}


# ---------------------------------------------------------------------------
# One turn, several messages: the answer must not inherit the tool's identity
# ---------------------------------------------------------------------------

def test_a_second_message_in_the_same_turn_replaces_the_first():
    """A research turn builds a tool submessage and then the answer.

    Both are introduced at ``/message``.  If the tool's ``recipient`` survived
    into the answer, the answer would be excluded as a tool payload and the
    panel would report "upstream sent no body"; if its body survived, the answer
    would be appended to the tool's text.  Both are wrong, in opposite
    directions, so the boundary is read from the message's own id.
    """
    frames = [
        replace("/message", {"id": "tool-1", "author": {"role": "assistant"},
                             "recipient": "web.run",
                             "content": {"content_type": "text", "parts": []},
                             "status": "in_progress", "metadata": {}}),
        append("/message/content/parts/0", "检索结果摘录，不是答案。"),
        replace("/message", {"id": "answer-2", "author": {"role": "assistant"},
                             "content": {"content_type": "text", "parts": []},
                             "status": "in_progress", "metadata": {}}),
        append("/message/content/parts/0", ANSWER),
        replace("/message/status", "finished_successfully"),
        replace("/message/end_turn", True),
        replace("/message/metadata/is_complete", True),
    ]
    _, view = run(frames)
    assert view["projection"]["report"] == ANSWER
    assert "检索结果摘录" not in view["projection"]["report"]
    assert view["projection"]["report_final"] is True


def test_a_same_id_message_refresh_does_not_discard_the_body_already_streamed():
    """Only a *different* message starts a new one; a refresh of the same id
    (a status update, say) must keep what was already appended to it."""
    frames = [
        replace("/message", {"id": "m-1", "author": {"role": "assistant"},
                             "content": {"content_type": "text", "parts": []},
                             "status": "in_progress", "metadata": {}}),
        append("/message/content/parts/0", "已经流出的正文。"),
        replace("/message", {"id": "m-1", "status": "in_progress",
                             "content": {"content_type": "text"}}),
    ]
    _, view = run(frames, outcome=None)
    assert view["projection"]["report"] == "已经流出的正文。"


def test_a_parts_array_is_authoritative_and_clears_an_earlier_body():
    """A message declaring its own parts is not adding to the previous one."""
    state = new_delta_state()
    fold_delta({"p": "/message", "o": "replace", "v": {
        "id": "m-1", "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": ["旧正文"]},
        "metadata": {}}}, state)
    fold_delta({"p": "/message/content/parts", "o": "replace",
                "v": ["新正文"]}, state)
    assert list(state["parts"].values()) == ["新正文"]


# ---------------------------------------------------------------------------
# The body's other observed positions
# ---------------------------------------------------------------------------

def test_an_answer_patched_as_content_text_is_projected():
    """The capture's own answer frame carries the body in ``content.text``."""
    frames = [replace("/message", {"id": "m-1", "author": {"role": "assistant"},
                                   "content": {"content_type": "text"},
                                   "status": "in_progress", "metadata": {}}),
              replace("/message/content/text", "以 text 字段送达的答案。"),
              replace("/message/status", "finished_successfully"),
              replace("/message/end_turn", True),
              replace("/message/metadata/is_complete", True)]
    _, view = run(frames)
    assert view["projection"]["report"] == "以 text 字段送达的答案。"
    assert view["projection"]["report_final"] is True


def test_an_appended_content_text_keeps_what_came_before():
    """``append`` means append on this path too, not "replace with the rest"."""
    frames = [replace("/message", {"id": "m-1", "author": {"role": "assistant"},
                                   "content": {"content_type": "text"},
                                   "metadata": {}}),
              append("/message/content/text", "前半段。"),
              append("/message/content/text", "后半段。")]
    _, view = run(frames, outcome=None)
    assert view["projection"]["report"] == "前半段。后半段。"


def test_a_whole_parts_array_replaced_in_one_patch_is_projected():
    frames = [replace("/message", {"id": "m-1", "author": {"role": "assistant"},
                                   "content": {"content_type": "text", "parts": []},
                                   "metadata": {}}),
              replace("/message/content/parts", "ignored"),
              replace("/message/content/parts", ["第一段。", "第二段。"])]
    _, view = run(frames, outcome=None)
    assert view["projection"]["report"] == "第一段。\n第二段。"


def test_a_malformed_parts_patch_cannot_clear_a_body_already_streamed():
    frames = [replace("/message", {"id": "m-1", "author": {"role": "assistant"},
                                   "content": {"content_type": "text", "parts": []},
                                   "metadata": {}}),
              append("/message/content/parts/0", "已流出的正文。"),
              replace("/message/content/parts", {"not": "an array"})]
    _, view = run(frames, outcome=None)
    assert view["projection"]["report"] == "已流出的正文。"


# ---------------------------------------------------------------------------
# Failures that arrive before the author does
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    ("failed", "failed"), ("cancelled", "cancelled"), ("incomplete", "failed")])
def test_a_failure_status_patch_is_terminal_even_without_a_named_author(status, expected):
    """Erring toward "failed" is the honest direction.

    A cancelled turn is replayed without ``end_turn``, so without this the
    transport's own ``[DONE]`` marker would record it as a completed research
    turn: the one misreading this module exists to prevent.
    """
    store, view = run([replace("/message", {"content": {"content_type": "text"}}),
                       replace("/message/status", status)],
                      outcome="complete")
    assert view["state"] == expected
    assert view["projection"]["finished"] is True
    assert view["projection"]["report_final"] is False


def test_a_status_patch_cannot_complete_a_turn_on_its_own():
    """The failure direction is read from a patch; completion never is."""
    frames = [replace("/message", {"id": "m-1", "author": {"role": "assistant"},
                                   "content": {"content_type": "text"}, "metadata": {}}),
              replace("/message/content/text", "正文"),
              replace("/message/status", "finished_successfully")]
    store, view = run(frames, outcome=None)
    assert view["projection"]["finished"] is False
    assert view["projection"]["report_final"] is False


def test_a_user_authored_status_patch_is_not_a_terminal_signal():
    """The echo of the user's own turn cannot end the turn."""
    frames = [replace("/message", {"id": "u-1", "author": {"role": "user"},
                                   "content": {"content_type": "text"},
                                   "status": "failed", "metadata": {}})]
    store, view = run(frames, outcome=None)
    assert view["projection"]["finished"] is False
    assert store.snapshot(CONVERSATION)["state"] == "streaming"


# ---------------------------------------------------------------------------
# Bounds on the evidence itself
# ---------------------------------------------------------------------------

def test_a_source_digest_flood_is_bounded():
    """The per-frame walk is bounded; the number of frames is not.

    The ceilings are soft -- a frame already in flight may carry the set past
    the mark -- so the bound asserted here is the ceiling plus one frame's worth
    of distinct identifiers.
    """
    store = ResearchProgressStore()
    recorder = store.recorder(SEED, CONVERSATION, research=True)
    for frame_index in range(40):
        recorder.record(frame(replace(
            "/message/metadata/content_references",
            [{"url": "https://delta.test/%d/%d" % (frame_index, index)}
             for index in range(256)])))
    record = store.snapshot(CONVERSATION)
    assert record is not None
    assert record["events_seen"] == 40, "every frame is still an observed event"
    projection = store.projection_snapshot(CONVERSATION, recorder.owner)["projection"]
    assert 0 < projection["sources"] <= rp.MAX_SOURCE_DIGESTS + rp.MAX_SOURCE_ENTRIES
    assert projection["sources_evidenced"] is True


def test_a_removed_source_container_is_not_evidence_upstream_listed_sources():
    """"Upstream dropped its list" is not "upstream listed none"."""
    frames = [replace("/message", {"id": "m-1", "author": {"role": "assistant"},
                                   "content": {"content_type": "text"}, "metadata": {}}),
              {"p": "/message/metadata/content_references", "o": "remove", "v": None}]
    _, view = run(frames, outcome=None)
    assert view["projection"]["sources_evidenced"] is False


def test_a_list_valued_single_patch_is_never_mistaken_for_a_batch():
    """Every item must be an operation for the batch envelope to apply."""
    payload = {"p": "/message/metadata/citations", "o": "replace",
               "v": [{"p": "not-a-path", "o": "not-an-op"},
                     {"p": "/message/content/parts/0", "o": "append", "v": "x"}]}
    assert patch_operations(payload) == [
        ("/message/metadata/citations", "replace", payload["v"])]


def test_the_incremental_family_is_recorded_as_its_own_family():
    """The panel names what it saw; a patch is not reported as a snapshot."""
    _, view = run([append("/message/content/parts/0", "x")])
    assert view["projection"]["family"] == rp.KIND_MESSAGE_DELTA


def test_an_incremental_turn_is_only_projected_for_a_research_turn():
    """A chat turn's patches must not accumulate a research projection."""
    store = ResearchProgressStore()
    recorder = store.recorder(SEED, CONVERSATION, research=False)
    for item in answer_stream():
        recorder.record(frame(item))
    assert store.projection_snapshot(CONVERSATION, recorder.owner) is None
