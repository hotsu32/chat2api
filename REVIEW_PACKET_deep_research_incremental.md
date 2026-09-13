# REVIEW_PACKET — Deep Research progress: incremental encoding + turn lifecycle

Work line: the Deep Research projection (gateway/research_progress.py).
Branch: codex/deepseek-research-progress-20260913-retry
Date: 2026-09-13

## Product impact

1. A Deep Research turn produced by the current official frontend streams its
   answer as incremental patches, not as whole message snapshots. The panel used
   to show "upstream provided no report body" for such a turn even though the
   browser had already received the whole answer; it now shows the answer, and
   the source count the stream actually evidenced.
2. After a plain chat follow-up in the same conversation, reloading the page no
   longer re-opens the research panel (with an empty report) over that chat.
3. A second research turn in the same conversation no longer inherits the first
   turn's source count, markers or tool.

## Root causes and fixes

### R1 — the incremental event family was not parsed

`gateway/f_conversation_gateway.rewrite_f_conversation_body` forwards the
frontend's `supported_encodings` to the legacy endpoint (`tests/test_m2_stream_encoding.py`
pins that), so the browser's turn is answered with the delta encoding the new
frontend renders from:

```
{"p": "/message/content/parts/0", "o": "append", "v": "..."}
{"o": "patch", "v": [{"p": ..., "o": ..., "v": ...}, ...], "c": 3}
```

Those frames carry no message object, so the projection saw an anonymous patch:
body empty, source containers invisible, `content_type` invisible.

Fix — `patch_operations()` / `fold_delta()` / `synthesise_delta_message()` fold
the patches into one bounded message and hand it to the existing
`project_event()`, so a single set of rules (assistant-only, hidden/tool/
reasoning excluded, bounded body, completion needs `end_turn` + `is_complete` +
`status`) decides what the panel may show. Unknown patch paths write nothing and
stay observable as events. Source values are reduced to digests at fold time and
never retained.

### R2 — a follow-up chat turn resurrected the research panel on refresh

The panel prefers `/backend-api/research-progress/{id}/projection` when the URL
names a conversation. That route answered `research: true` for any conversation
that had *ever* run research, while `begin()` had already cleared the previous
turn's report — so a reload re-opened the panel with an empty report.

Fix — `_view()`'s `research` bit is now `current_turn_research`, matching the
active route; a plain chat turn no longer overwrites the retained research
projection (frozen clock and answer stay restorable).

### R3 — a new research turn inherited the previous turn's evidence

`begin()` reset the report and the clock but not `source_digests`, `markers`,
`tool`, `content_types`, `urls_moderated`, `sources_evidenced` or
`research_confirmed`.

Fix — all projection-accumulated fields reset with the turn. The retained event
log and its counters stay cumulative on purpose (bounded by `_evict_events`).

## Evidence

### Failing-first (new tests run against `HEAD`'s `gateway/research_progress.py`)

```
E   AssertionError: assert '' == '研究报告：结论正文。'
    tests_e2e/test_research_progress_restore.py:748
E   AssertionError: the panel must stay closed over a chat turn
    assert True is False
    tests_e2e/test_research_progress_restore.py:802
E   AssertionError: the new turn evidenced no sources
    assert 2 == 0
    tests_e2e/test_research_progress_restore.py:821
3 failed, 1 passed, 34 deselected
```

The unit file failed to import (`ImportError: cannot import name 'fold_delta'`)
against the unmodified module; a direct probe of one delta stream through the
pre-fix code returned `report=""`, `sources=0`, `sources_evidenced=false`.

### After the fix

```
$ .venv/bin/python -m pytest tests/ -p no:randomly
1061 passed in 16.93s

$ .venv/bin/python -m pytest tests_e2e/ -p no:randomly
517 passed, 73 warnings in 283.31s (0:04:43)

$ .venv/bin/python -m pytest tests_e2e/test_research_progress_browser.py \
      tests_e2e/test_research_progress_panel.py \
      tests_e2e/test_research_progress_restore.py \
      tests_e2e/test_research_route_isolation.py \
      tests_e2e/test_f_conversation_upstream.py \
      tests_e2e/test_f_conversation_generation_feedback.py
exit 0 (targeted research + f/conversation suites)

$ .venv/bin/python -m pytest tests/test_research_incremental_projection.py \
      tests/test_research_progress.py tests/test_research_projection.py \
      tests/test_research_report_projection.py tests/test_research_progress_transport.py
209 passed in 1.50s
```

Both full runs above are with the final code.

### New coverage

`tests/test_research_incremental_projection.py` (51 cases): in-progress delta
sequences, the batched envelope, source evidence and its bounds, completion
freeze, failure/cancellation patches, malformed patches, unknown paths still
counted, a second message in one turn, `content/text` and whole-`parts` patches,
digest flooding, turn isolation.

`tests_e2e/test_research_progress_restore.py` (34 -> 38): an incremental turn end
to end through the gateway (body, sources, terminal, bytes unchanged), a
follow-up plain chat not resurrecting the panel on refresh, and a second research
turn not inheriting the first turn's evidence.

## Second opinion (Kimi Code)

Two rounds, session `session_b816ded8-0977-4f37-adaf-0967f4396e9d`.

Round 1 found one medium-high defect and several medium findings, all now fixed
or recorded: a second message in the same turn inherited the first one's
`recipient`/parts (fixed with an id-keyed message boundary), a failure status on
an unattributed message was not terminal (fixed, failure direction only),
unbounded digest growth (fixed), unparsed `content/text` and whole-`parts`
patches (fixed), and a batch/single-patch ambiguity (fixed).

Round 2 verified each fix against the code and seven hand probes and recorded
two explicit residual assumptions plus three documentation-level follow-ups.

## Residual risks (explicit)

* **Unverified against real upstream.** The delta frames are constructed from the
  protocol contract this repo already documents (`supported_encodings`,
  `rewrite_f_conversation_body`) and from the operators/paths that contract
  implies. No delta byte stream was captured, so `tests/fixtures/` deliberately
  gained no delta fixture — a shape file would look like a capture. The
  snapshot family remains covered by the real 2026-09-12 shape capture.
* **No id means "same message".** A message object without an `id` is folded into
  the message being built. Every message object in the capture carries an `id`;
  if a future release introduces an id-less message, the worst case is an
  unshown answer, never a misattributed one.
* **A failing tool submessage freezes the turn as failed**, even if the answer
  follows. The snapshot path has always behaved this way (its failure branch
  deliberately does not require `end_turn`).
* **Digest ceilings are soft** (`MAX_SOURCE_DIGESTS` + one frame's worth), so a
  turn listing more than ~4k distinct sources under-reports its count. It never
  over-reports and never loses the `sources_evidenced` fact.
* **Not addressed, recorded**: each event is still classified twice
  (`Recorder.record` and `store.record`) and the projection is computed under the
  store lock — pre-existing, bounded, and out of this change's scope.

## Files

* `gateway/research_progress.py`
* `tests/test_research_incremental_projection.py` (new)
* `tests_e2e/test_research_progress_restore.py`
* `templates/chatgpt.html`, `gateway/research_panel.py`,
  `gateway/f_conversation_gateway.py`: assigned but **unchanged** — the browser
  contract, the panel assets and the transport already satisfied their part of
  the acceptance criteria, and the fold needed no change outside the module.
