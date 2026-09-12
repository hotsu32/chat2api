"""Tests for the verdict rules that turn probe evidence into matrix statuses.

These rules are where an acceptance harness most easily lies: a missing field
that defaults to zero, a control matched from the wrong turn, a stream that
errored but still produced a terminal frame. Each test below pins one way the
rules must refuse to report a pass.

The records are hand-built minimal fixtures, not copies of the probe's output
shape -- a test that mirrors the producer would pass even if both were wrong.
"""
from __future__ import annotations

from scripts.mirror_acceptance import verdicts


def _turn(**over):
    """A minimal well-formed completed turn; override one thing per test."""
    rec = {
        "stage": "done",
        "terminal_verified": True,
        "dom_changes_before_terminal": 9,
        "dom_change_count": 11,
        "identity_handovers": 0,
        "server_answer_length": 700,
        "assistant": {"tailLength": 700},
        "sse": {"dataFrames": 40, "errorFrames": 0},
        "controls": {
            name: {"status": "measured", "ms_after_terminal": 120,
                   "actionable_after_hover": True, "suspect_pre_terminal_mount": False}
            for name in ("copy", "more", "share", "rate")
        },
    }
    rec.update(over)
    return rec


# ------------------------------------------------------------------ chat (M2)

def test_streaming_turn_with_many_increments_passes():
    out = verdicts.chat_verdict(_turn())
    assert out["status"] == "PASS"
    assert out["metrics"]["dom_changes_before_terminal"] == 9


def test_single_end_commit_is_a_failure_not_a_pass():
    # The exact defect problem 2 describes: the answer appears all at once.
    out = verdicts.chat_verdict(_turn(dom_changes_before_terminal=1))
    assert out["status"] == "FAIL"
    assert "one step" in out["note"]


def test_error_frames_are_named_in_the_attribution():
    out = verdicts.chat_verdict(_turn(dom_changes_before_terminal=0,
                                      sse={"dataFrames": 20, "errorFrames": 9}))
    assert out["status"] == "FAIL"
    assert "9 error frames" in out["note"]


def test_incomplete_probe_is_unmeasurable_never_fail():
    # A probe that crashed says nothing about the product. Reporting FAIL here
    # would blame the mirror for the harness's own timeout.
    out = verdicts.chat_verdict({"stage": "pin_new_assistant", "failedAtStage": "pin_new_assistant"})
    assert out["status"] == "UNMEASURABLE"


def test_missing_terminal_frame_is_unmeasurable():
    out = verdicts.chat_verdict(_turn(terminal_verified=False))
    assert out["status"] == "UNMEASURABLE"


def test_absent_increment_field_does_not_become_zero():
    rec = _turn()
    del rec["dom_changes_before_terminal"]
    assert verdicts.chat_verdict(rec)["status"] == "UNMEASURABLE"


# --------------------------------------------------------------- buttons (M3)

def test_buttons_within_budget_pass():
    assert verdicts.buttons_verdict(_turn())["status"] == "PASS"


def test_buttons_over_budget_fail_and_name_the_slowest():
    rec = _turn()
    rec["controls"]["share"]["ms_after_terminal"] = 4451
    out = verdicts.buttons_verdict(rec)
    assert out["status"] == "FAIL"
    assert "share" in out["note"] and "4451" in out["note"]


def test_control_mounted_before_its_own_terminal_is_unmeasurable():
    # An impossible timing means the selector matched a previous turn's action
    # bar. That reads as a spectacular pass if taken at face value.
    rec = _turn()
    rec["controls"]["copy"]["suspect_pre_terminal_mount"] = True
    rec["controls"]["copy"]["ms_after_terminal"] = -12026
    out = verdicts.buttons_verdict(rec)
    assert out["status"] == "UNMEASURABLE"
    assert "selector scope" in out["note"]


def test_unmatched_selector_is_a_probe_gap_not_a_product_failure():
    rec = _turn()
    rec["controls"]["rate"] = {"status": "unmeasured", "selectorsTried": ["#nope"]}
    out = verdicts.buttons_verdict(rec)
    assert out["status"] == "UNMEASURABLE"
    assert "probe gap" in out["note"]


def test_mounted_but_unclickable_control_fails():
    rec = _turn()
    rec["controls"]["more"]["actionable_after_hover"] = False
    out = verdicts.buttons_verdict(rec)
    assert out["status"] == "FAIL"
    assert "more" in out["note"]


# -------------------------------------------------------------------- delivery

def test_rendered_answer_passes():
    assert verdicts.delivery_verdict(_turn())["status"] == "PASS"


def test_empty_screen_with_a_server_side_answer_is_attributed_to_rendering():
    out = verdicts.delivery_verdict(_turn(assistant={"tailLength": 0},
                                          server_answer_length=739))
    assert out["status"] == "FAIL"
    assert "server-side" in out["note"] and "739" in out["note"]


def test_empty_screen_with_no_server_answer_is_not_blamed_on_rendering():
    out = verdicts.delivery_verdict(_turn(assistant={"tailLength": 0},
                                          server_answer_length=0))
    assert out["status"] == "FAIL"
    assert "no server-side answer" in out["note"]


def test_unrecorded_render_length_is_unmeasurable():
    out = verdicts.delivery_verdict(_turn(assistant={}))
    assert out["status"] == "UNMEASURABLE"


# ---------------------------------------------------------------- dispatching

def test_evaluate_refuses_features_it_has_no_rule_for():
    # Tool cells have no automated rule yet; they must not silently pass.
    out = verdicts.evaluate(_turn(), "deep_research")
    assert out["status"] == "UNMEASURABLE"


def test_every_verdict_rule_can_only_pass_on_positive_evidence():
    # An empty record carries no evidence of anything; no rule may return PASS.
    for name, fn in verdicts.VERDICTS.items():
        assert fn({})["status"] != "PASS", name
