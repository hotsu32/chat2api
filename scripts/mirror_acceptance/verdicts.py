"""Turn probe evidence into matrix verdicts, by rule rather than by opinion.

The assignment forbids marking an unverified path as successful, so the mapping
from evidence to status is written down here as code and applied uniformly. The
important property is that every route to PASS requires positive evidence: a
missing field yields UNMEASURABLE, never PASS. An absent measurement and a
measurement of zero are different outcomes, and the earlier probe's habit of
conflating them is what made its "success" readings worthless.

Three verdicts are produced per live turn:

  chat     -- did the answer stream incrementally into the DOM (problem 2)
  buttons  -- did the turn's action bar mount within 500 ms of the terminal
              frame (problem 3)
  delivery -- did the user end up with the answer at all

`delivery` exists because `chat` and `buttons` can both be measured on a turn
that ultimately showed the user nothing, and a matrix without it would report
two greens on a broken turn.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# The plan's M3 entry metric. Measured from the SSE terminal frame to the moment
# the control mounts (exists + is visible) inside the turn's own container --
# NOT to the moment it becomes clickable, which only happens on hover and would
# measure the probe's timing rather than the product's.
BUTTON_BUDGET_MS = 500

# Problem 2 asks for multiple pre-terminal DOM increments. One increment is a
# single commit at the end, which is the defect, not the fix.
MIN_PRE_TERMINAL_INCREMENTS = 2


def _sse(rec: Dict[str, Any]) -> Dict[str, Any]:
    return rec.get("sse") or {}


def chat_verdict(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Problem 2: incremental rendering before the terminal frame."""
    if rec.get("stage") != "done":
        return {"status": "UNMEASURABLE",
                "note": f"probe did not complete (failed at {rec.get('failedAtStage')})"}
    if not rec.get("terminal_verified"):
        return {"status": "UNMEASURABLE", "note": "no SSE terminal frame observed"}

    increments = rec.get("dom_changes_before_terminal")
    if increments is None:
        return {"status": "UNMEASURABLE", "note": "increment count not recorded"}

    sse = _sse(rec)
    metrics = {
        "dom_changes_before_terminal": increments,
        "dom_change_count": rec.get("dom_change_count"),
        "sse_data_frames": sse.get("dataFrames"),
        "sse_error_frames": sse.get("errorFrames"),
        "identity_handovers": rec.get("identity_handovers"),
        "final_length": (rec.get("assistant") or {}).get("tailLength"),
    }

    if increments >= MIN_PRE_TERMINAL_INCREMENTS:
        return {"status": "PASS",
                "note": f"{increments} pre-terminal DOM increments", "metrics": metrics}

    # Streaming did not reach the DOM. Attribute it, because "the mirror did not
    # render" and "upstream sent nothing to render" are different owners.
    if sse.get("errorFrames"):
        return {"status": "FAIL",
                "note": (f"{increments} pre-terminal increments; stream carried "
                         f"{sse.get('errorFrames')} error frames -- content never "
                         "streamed to the DOM"),
                "metrics": metrics}
    return {"status": "FAIL",
            "note": (f"{increments} pre-terminal increments (need "
                     f">={MIN_PRE_TERMINAL_INCREMENTS}); answer committed in one "
                     "step rather than streaming"),
            "metrics": metrics}


def buttons_verdict(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Problem 3: the turn action bar mounts within the budget of the terminal."""
    if rec.get("stage") != "done":
        return {"status": "UNMEASURABLE", "note": "probe did not complete"}
    controls = rec.get("controls") or {}
    if not controls:
        return {"status": "UNMEASURABLE", "note": "no control measurements recorded"}

    unmeasured = [n for n, c in controls.items() if c.get("status") != "measured"]
    suspect = [n for n, c in controls.items() if c.get("suspect_pre_terminal_mount")]
    if suspect:
        # A control that mounted before the terminal frame of its own turn was
        # matched from somewhere else; the reading is not about this turn.
        return {"status": "UNMEASURABLE",
                "note": f"pre-terminal mount for {sorted(suspect)} -- selector scope wrong"}
    if unmeasured:
        return {"status": "UNMEASURABLE",
                "note": f"never matched: {sorted(unmeasured)} (probe gap, not a product finding)"}

    timings = {n: c.get("ms_after_terminal") for n, c in controls.items()}
    if any(v is None for v in timings.values()):
        return {"status": "UNMEASURABLE", "note": "terminal frame missing; latency undefined"}

    worst_name = max(timings, key=lambda n: timings[n])
    worst = timings[worst_name]
    metrics = {"ms_after_terminal": timings, "worst_ms": worst,
               "budget_ms": BUTTON_BUDGET_MS,
               "actionable_after_hover": {n: c.get("actionable_after_hover")
                                          for n, c in controls.items()}}

    if not all(c.get("actionable_after_hover") for c in controls.values()):
        dead = sorted(n for n, c in controls.items() if not c.get("actionable_after_hover"))
        return {"status": "FAIL", "note": f"controls mounted but not actionable: {dead}",
                "metrics": metrics}
    if worst <= BUTTON_BUDGET_MS:
        return {"status": "PASS",
                "note": f"all four mounted within {worst} ms of terminal", "metrics": metrics}
    return {"status": "FAIL",
            "note": (f"slowest control '{worst_name}' mounted {worst} ms after terminal "
                     f"(budget {BUTTON_BUDGET_MS} ms)"),
            "metrics": metrics}


def delivery_verdict(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Did the user actually receive the answer on screen?

    Kept separate from `chat` because a turn can stream perfectly and still end
    empty, or end full having never streamed. Server-side length, when the probe
    captured it, decides attribution.
    """
    if rec.get("stage") != "done":
        return {"status": "UNMEASURABLE", "note": "probe did not complete"}
    assistant = rec.get("assistant") or {}
    shown = assistant.get("tailLength")
    server = rec.get("server_answer_length")
    metrics = {"rendered_length": shown, "server_answer_length": server,
               "sse_error_frames": _sse(rec).get("errorFrames")}

    if shown is None:
        return {"status": "UNMEASURABLE", "note": "rendered length not recorded"}
    if shown > 0:
        return {"status": "PASS", "note": f"{shown} chars rendered", "metrics": metrics}
    if server:
        return {"status": "FAIL",
                "note": (f"nothing rendered, but {server} chars exist server-side "
                         "-- rendering defect, not upstream"),
                "metrics": metrics}
    return {"status": "FAIL",
            "note": "nothing rendered and no server-side answer observed", "metrics": metrics}


VERDICTS = {"chat": chat_verdict, "buttons": buttons_verdict, "delivery": delivery_verdict}


def evaluate(rec: Dict[str, Any], feature: str) -> Dict[str, Any]:
    fn = VERDICTS.get(feature)
    if fn is None:
        return {"status": "UNMEASURABLE", "note": f"no verdict rule for feature {feature!r}"}
    return fn(rec)
