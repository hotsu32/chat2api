"""Browser acceptance for the research panel: the behaviour, not the bytes.

The other panel tests read the shipped JS/CSS as text.  That can only prove
what the source says, not what a browser *does* with it.  These tests load the
real assets into a real Chromium, served over real HTTP from a scripted mirror,
and observe the panel the way a user and a phone would:

* the in-progress sequence, then the terminal state;
* the frozen clock and the stopped timer after the terminal;
* sources counted only when upstream evidenced them;
* a malformed response body survived;
* a refresh on ``/c/<id>`` restored from the server-side store;
* idle polling before any turn exists;
* a phone viewport where the panel must not cover the composer.

The mirror is scripted; the browser is not.  Nothing here claims a real
upstream run -- see the honesty note at the bottom.
"""

import http.server
import json
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from gateway.research_panel import PANEL_CSS, PANEL_JS

PROBE = Path(__file__).resolve().parent / "browser" / "research_panel_probe.js"

_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>mirror</title>
<link rel="stylesheet" href="/_chat-share/research-panel.css">
<style>html,body{margin:0;height:100%;background:#111}</style></head>
<body><div id="composer" style="position:fixed;left:0;right:0;bottom:0;height:96px;background:#2a2a2a"></div>
<script src="/_chat-share/research-panel.js" defer></script></body></html>"""


def _frame(*, state="streaming", action="正在分析资料", sources=0, evidenced=False,
           finished=False, events=3, elapsed_ms=4200, started_at=1000.0,
           finished_at=None, tool="", markers=(), content_types=("thoughts",),
           report="", report_final=False, report_truncated=False):
    return {
        "research": True,
        "conversation_id": "conv-abc",
        "state": state,
        "terminal": finished,
        "events_seen": events,
        "updated_at": started_at,
        "projection": {
            "action": action,
            "family": "message_snapshot",
            "tool": tool,
            "markers": list(markers),
            "content_types": list(content_types),
            "sources": sources,
            "sources_evidenced": evidenced,
            "urls_moderated": 0,
            "research_confirmed": True,
            "report": report,
            "report_final": report_final,
            "report_truncated": report_truncated,
            "started_at": started_at,
            "elapsed_ms": elapsed_ms,
            "finished_at": finished_at,
            "finished": finished,
        },
    }


_STREAMING = _frame()
_WITH_SOURCES = _frame(action="正在检索网络资料", sources=2, evidenced=True,
                       events=7, elapsed_ms=9000, tool="web.run",
                       markers=["search"])
_FINISHED = _frame(state="complete", action="研究已完成", sources=2, evidenced=True,
                   finished=True, events=9, elapsed_ms=12000, started_at=1000.0,
                   finished_at=1013.0, tool="web.run", markers=["search"])

# The assistant's own body, as the mirror projects it: markup included on
# purpose, so a browser test can prove it is rendered as text and not parsed.
REPORT_BODY = ('<img src=x onerror="window.__reportExecuted=1"> 报告正文：开头段落。\n'
               + '\n'.join(f'段落 {i} 的内容，用于验证换行与滚动。' for i in range(2, 40)))
_REPORTED = _frame(state="complete", action="研究已完成", sources=0, evidenced=True,
                   finished=True, events=21, elapsed_ms=31000, started_at=1000.0,
                   finished_at=1031.0, report=REPORT_BODY, report_final=True,
                   report_truncated=True)

# The same body, but upstream never marked that frame as the end of the turn:
# the panel must describe what it received rather than call it a finished report.
_UNMARKED = _frame(state="complete", action="研究已完成", finished=True,
                   events=9, elapsed_ms=12000, started_at=1000.0,
                   finished_at=1013.0, report="最后收到的正文", report_final=False)

# Each scenario is the ordered response list for /active; the last entry repeats.
SCENARIOS = {
    "sequence": [{"research": False}, _STREAMING, _WITH_SOURCES, _FINISHED],
    "idle": [{"research": False}],
    "error": [_frame(state="failed", action="研究失败", finished=True,
                     elapsed_ms=3000, started_at=2000.0, finished_at=2003.0)],
    "cancelled": [_frame(state="cancelled", action="研究已取消", finished=True,
                         elapsed_ms=3000, started_at=2000.0, finished_at=2003.0)],
    "unreported": [_frame(evidenced=False)],
    "empty_sources": [_frame(state="complete", action="研究已完成", sources=0,
                             evidenced=True, finished=True, elapsed_ms=5000,
                             started_at=1000.0, finished_at=1005.0)],
    "mobile": [_STREAMING],
    "desktop": [_STREAMING],
    "malformed": ["RAW:not json at all", 500, _STREAMING],
    "restore": [{"research": False}],
    "report": [_REPORTED],
    "mobile_report": [_REPORTED],
    "unmarked_report": [_UNMARKED],
    "no_report": [_frame(state="complete", action="研究已完成", finished=True,
                         elapsed_ms=5000, started_at=1000.0, finished_at=1005.0)],
}

# The restore scenario answers on the per-conversation route instead.
RESTORE_FRAME = _frame(state="complete", action="研究已完成", sources=3, evidenced=True,
                       finished=True, events=14, elapsed_ms=21000, started_at=1000.0,
                       finished_at=1021.0)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.path != "/__stats":
            self.server.stats["total"] += 1
        self.wfile.write(payload)

    def do_GET(self):
        scenario = self.server.scenario
        if self.path == "/__stats":
            self._send(200, json.dumps(self.server.stats), "application/json")
        elif self.path.startswith("/_chat-share/research-panel.js"):
            self._send(200, PANEL_JS, "application/javascript")
        elif self.path.startswith("/_chat-share/research-panel.css"):
            self._send(200, PANEL_CSS, "text/css")
        elif self.path.startswith("/backend-api/research-progress/active"):
            self.server.stats["active"] += 1
            self._respond_from_script(scenario)
        elif self.path.endswith("/projection"):
            self.server.stats["projection"] += 1
            if scenario == "restore":
                self._send(200, json.dumps(RESTORE_FRAME), "application/json")
            else:
                self._send(404, json.dumps({"detail": "Conversation not found"}),
                           "application/json")
        else:
            self._send(200, _HTML, "text/html; charset=utf-8")

    def _respond_from_script(self, scenario):
        script = SCENARIOS[scenario]
        index = min(self.server.calls.get(scenario, 0), len(script) - 1)
        self.server.calls[scenario] = self.server.calls.get(scenario, 0) + 1
        entry = script[index]
        if isinstance(entry, str) and entry.startswith("RAW:"):
            self._send(200, entry[4:], "application/json")
        elif isinstance(entry, int):
            self._send(entry, "upstream is unhappy", "text/html")
        else:
            self._send(200, json.dumps(entry), "application/json")


def _serve(scenario):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.scenario = scenario
    server.calls = {}
    server.stats = {"total": 0, "active": 0, "projection": 0}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _node_available():
    if not shutil.which("node"):
        return False
    try:
        probe = subprocess.run(
            ["node", "-e", "require('module')"],
            capture_output=True, timeout=30)
    except Exception:
        return False
    return probe.returncode == 0


@pytest.fixture(autouse=True)
def _require_node():
    if not _node_available():
        pytest.skip("node is unavailable; the browser probe cannot run")


def _run_probe(scenario, timeout=180):
    server = _serve(scenario)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        completed = subprocess.run(
            ["node", str(PROBE), url, scenario],
            capture_output=True, text=True, timeout=timeout)
    finally:
        server.shutdown()
        server.server_close()
    if completed.returncode == 42:
        pytest.skip("playwright is unavailable; the browser probe cannot run")
    assert completed.stdout.strip(), completed.stderr[-2000:]
    assert completed.returncode == 0, \
        f"{completed.stdout[-4000:]}\n{completed.stderr[-2000:]}"
    return json.loads(completed.stdout)


@pytest.mark.parametrize("scenario", ["sequence", "idle", "error", "cancelled",
                                      "unreported", "empty_sources", "malformed",
                                      "restore", "mobile", "desktop", "report",
                                      "mobile_report", "no_report",
                                      "unmarked_report"])
def test_browser_scenario(scenario):
    report = _run_probe(scenario)
    assert report["failures"] == [], report
    assert report["checks"], report


def test_sequence_reaches_a_terminal_state_then_stops_polling():
    """The acceptance path, asserted by name so a regression names itself."""
    checks = _run_probe("sequence")["checks"]
    for name in ("in_progress_headline", "in_progress_sources_pending",
                 "terminal_headline", "terminal_sources_counted", "terminal_evidence",
                 "terminal_elapsed_frozen", "terminal_stops_polling",
                 "no_ratio_in_rendered_text"):
        assert checks[name]["ok"] is True, {name: checks[name]}


def test_the_answer_the_official_region_hides_is_rendered_by_the_panel():
    """The live gap, end to end in a real browser.

    A Pro research turn completed and the page showed no report, because the
    official research region does not resolve in a normal browser.  The mirror's
    panel must render the body the upstream stream carried -- as text, labelled
    by what upstream marked, with the terminal state still readable.
    """
    checks = _run_probe("report")["checks"]
    for name in ("report_rendered", "report_markup_not_parsed", "report_labelled_final",
                 "report_truncation_noted", "terminal_headline_still_shown",
                 "no_ratio_in_rendered_text", "report_panel_stays_bounded",
                 "no_uncaught_page_error"):
        assert checks[name]["ok"] is True, {name: checks[name]}


def test_a_finished_turn_with_no_body_says_so_instead_of_showing_a_blank_box():
    """The honest fallback the sources=0 gap actually hit."""
    checks = _run_probe("no_report")["checks"]
    for name in ("report_box_shown_for_a_finished_turn", "no_report_says_so",
                 "no_report_has_no_truncation_note", "no_report_keeps_the_rest_of_the_panel"):
        assert checks[name]["ok"] is True, {name: checks[name]}


def test_the_report_does_not_cover_the_composer_on_a_phone():
    """A long body must not be able to push the panel over the composer."""
    report = _run_probe("mobile_report")
    geometry = report["geometry"]
    assert geometry["overlaps"] is False, geometry
    assert geometry["panel"]["bottom"] <= geometry["composer"]["top"], geometry
    assert report["checks"]["report_panel_stays_bounded"]["ok"] is True, report["checks"]


def test_phone_layout_keeps_the_composer_reachable():
    report = _run_probe("mobile")
    geometry = report["geometry"]
    assert geometry["overlaps"] is False, geometry
    assert geometry["panel"]["bottom"] <= geometry["composer"]["top"], geometry
    assert report["checks"]["dismiss_removes_panel"]["ok"] is True


# ---------------------------------------------------------------------------
# What this file does not establish
# ---------------------------------------------------------------------------
#
# The mirror's responses are scripted here.  A pass proves the panel's behaviour
# given a projection, never that a real Plus/Pro turn produces one -- that is
# covered separately by tests_e2e/test_research_progress_restore.py against the
# gateway's own retention, and by the anonymous `phase=research_progress` log
# line, which is the only place a real deployment records what was retained.
#
# In particular this file cannot show that the official frontend's own research
# region renders: `internal://deep-research` is a model-identifier sentinel in
# the official bundle, not a URL, so whether that region appears depends on the
# bundle's own comparison.  The mirror's panel is deliberately independent of it.
