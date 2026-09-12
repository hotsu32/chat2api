# REVIEW_PACKET — Deep Research final report mirror gap

Date: 2026-09-13
Scope: `gateway/research_progress.py`, `gateway/research_panel.py`,
`gateway/f_conversation_gateway.py`, research tests/fixtures.

## 1. The gap

Real Pro turn, 2026-09-13, new chat, Deep Research selected, typed normally:

- request: `research=True`, `system_hints=[plugin:connector_openai_deep_research]`,
  `deep_research_version` metadata observed;
- upstream: `text/event-stream`, HTTP 200, `stream_release outcome=complete`, `events=21`;
- retained: `state=complete`, `sources=0`, `sources_reported=True`;
- DOM: only an `internal://deep-research` AXWebArea resolving to
  `chrome-error://chromewebdata/`;
- custom panel: `研究已完成` + event count + elapsed, no report.

Diagnosis: `internal://deep-research` is a model-identifier comparison inside the
official bundle, not a URL — the mirror cannot make that region render (this was
already documented in `gateway/research_panel.py`). The answer itself was in the
stream the mirror forwarded, and `project_event` read only `content_type` from it
and dropped every body. The projection carried progress, never the answer.

## 2. The fix

`gateway/research_progress.py`

- New closed-set projection keys: `report`, `report_final`, `report_truncated`.
- The body is read from the observed `message.content` of an assistant message:
  `text` (the capture's answer frame, fixture index 12) or a `parts` array of
  strings (every other captured message frame).
- Admission is decided only by evidence in the capture: `author.role == assistant`;
  not `is_visually_hidden_from_conversation`; not a reasoning content type
  (`thoughts` / `reasoning_recap`, compared on the raw value — `_token` cannot
  match `reasoning_recap` because a protocol token never contains `_`); not a tool
  frame (`web.run` recipient shape, `invoked_resource` / `invoked_plugin`).
- Bounded while built (`_REPORT_BUILD_LIMIT = 2 × MAX_REPORT_CHARS = 6000`,
  `MAX_REPORT_PARTS = 64`) and sanitised of control characters at the bound.
- `report_final` is true only when the body came from the frame that carries the
  turn's completion evidence (assistant + `finished_successfully` + `end_turn` +
  `metadata.is_complete`) — the same evidence the existing terminal detection uses.
- Record precedence: a settled answer outranks an interim one; a later interim
  frame cannot overwrite a settled answer; same rank is last-wins. A new turn on
  the same conversation clears the report (`begin()`), like `finished_at`.
- No report observed ⇒ `""`, never an invented one.

`gateway/research_panel.py`

- New report block rendered with `textContent` only (no `innerHTML` anywhere in
  the asset), labelled `研究报告` when upstream marked the frame and
  `研究报告（最后收到的正文）` when it did not; a finished turn with no body says
  `上游未提供可显示的报告正文`; a truncated body says so.
- The block is created lazily, so a turn with no answer keeps the panel exactly
  as it was. Assets bumped `?v=3` → `?v=4`.

`gateway/f_conversation_gateway.py`

- `phase=research_progress` now also logs `report_chars=<n>` and `report_final=<bool>`.
  Length only, never the text — this is the line that tells a real deployment
  whether the panel will show an answer.

## 3. Evidence

Baseline before the change (run as two invocations; `tests/` and `tests_e2e/`
cannot share one pytest process — see §5):

```
pytest tests/test_research_projection.py tests/test_research_progress.py tests/test_research_progress_transport.py
  -> 96 passed                              (pre-existing baseline)
pytest tests_e2e/test_research_progress_restore.py tests_e2e/test_research_progress_panel.py
  -> 29 passed                              (pre-existing baseline)
pytest tests_e2e/test_research_progress_browser.py
  -> 12 passed                              (pre-existing baseline)
```

After the change:

```
pytest tests/ -k research
  -> 154 passed, 738 deselected in 1.98s
pytest tests/
  -> 892 passed in 14.84s
pytest tests_e2e/test_research_progress_restore.py \
       tests_e2e/test_research_progress_panel.py \
       tests_e2e/test_research_progress_browser.py \
       tests_e2e/test_research_route_isolation.py \
       tests_e2e/test_f_conversation_upstream.py \
       tests_e2e/test_f_conversation_generation_feedback.py
  -> 144 passed, 62 warnings in 119.58s
```

Non-vacuity of the new browser tests (temporarily removing `applyReport(root,p);`
from `PANEL_JS`, then restoring — restored state verified by re-running the suite):

```
pytest tests_e2e/test_research_progress_browser.py -k report
  FAILED test_browser_scenario[report]           -> "timeout waiting for rendered report"
  FAILED test_browser_scenario[mobile_report]    -> "timeout waiting for rendered report"
  FAILED test_browser_scenario[no_report]        -> "timeout waiting for rendered report"
  FAILED test_the_report_does_not_cover_the_composer_on_a_phone
```

Non-vacuity of the new unit tests: `report` did not exist as a projection key at
HEAD (`git show HEAD:gateway/research_progress.py | grep -n '"report"'` → no
match; only the words "reported"/"reports" in comments), so every assertion on
`projection["report"]` fails at HEAD with `KeyError`.

Panel asset guards re-checked against the shipped JS text: no `innerHTML`,
`outerHTML`, `insertAdjacentHTML`, `document.write`, `document.cookie`,
`localStorage`, `sessionStorage`, `progressbar`, `percent`, `ETA`; all previously
pinned substrings (`else if(verdict==='idle')…`, `else{stop();armResume()}`,
`armResume`, the `/c/<id>` regex, the projection route, `p.sources_evidenced?…`,
`最近活动：`, the four headline phrases, `不构成官方进度`) still present.

## 4. Acceptance mapping

| Acceptance criterion | Where it is proven |
| --- | --- |
| Mirror-visible final report for a turn whose official UI points at the sentinel | `tests/test_research_report_projection.py::test_a_widget_answer_frame_still_projects_its_text_and_never_the_region_key`; `tests_e2e/test_research_progress_restore.py::test_a_completed_turn_exposes_the_answer_body_the_official_region_hides`; `tests_e2e/test_research_progress_browser.py::test_the_answer_the_official_region_hides_is_rendered_by_the_panel` |
| Honest non-empty terminal result when upstream sent no body | `…::test_a_turn_with_no_answer_body_reports_none_rather_than_inventing_one`; browser scenario `no_report` |
| Intermediate activity visible, terminal freezes elapsed | pre-existing `test_elapsed_time_freezes_once_the_turn_is_terminal`, browser `sequence`; unchanged |
| Sources only when evidenced, zero stays honest | `…::test_citations_are_counted_when_the_stream_evidenced_them`, `…::test_zero_sources_are_reported_as_zero_when_upstream_listed_none`, `tests/test_research_projection.py` source tests |
| Refresh restore | `…::test_the_report_restores_after_a_refresh_without_touching_upstream` (two GETs identical, no upstream record added), `…::test_the_report_route_stays_owner_scoped` |
| Desktop/mobile panel | browser scenarios `report`, `mobile_report` (no composer overlap, within viewport, single scroll container) |
| Normal chat unchanged | `tests/test_research_progress.py::test_a_non_research_turn_logs_no_research_phase_line`, `…::test_a_non_research_turn_never_appears_in_the_panel`, `…::test_a_non_research_turn_projects_no_report_at_all`; `test_f_conversation_upstream.py` 56 passed |
| No secrets in logs/fixtures/endpoints/Git | `…::test_the_phase_log_measures_a_real_report_without_printing_it` asserts `report_chars=<len>` present and that neither the report text nor account/session/URL tokens appear in the line; whole-diff scan for credential patterns is clean |

## 5. Unverified / cannot verify

- **No real-network verification in this worktree.** Nothing here contacts
  ChatGPT. What is proven is that a stream shaped like the capture's answer frame
  reaches the panel; that the *real* Pro stream carries a body in
  `content.text` / `content.parts` on an assistant frame is inferred from the
  2026-09-12 Plus shape capture (fixture index 12), not re-measured.
- **Real-network step to run after deploy:** start one Deep Research turn in a
  normal browser and check, in the gateway log:
  `[f_conversation] phase=research_progress … report_chars=<n> report_final=<bool>`.
  `report_chars=0` on a turn that visibly produced an answer means the real stream
  puts the body somewhere the capture did not show; the retained forensic events
  (`/backend-api/research-progress/<id>`) are then the place to look, and the
  fixture should be extended with the observed shape before the rule is widened.
- **`report_final=False` on a real answer frame is expected, not a failure**: it
  means that frame did not carry `end_turn` + `metadata.is_complete`. The panel
  then renders the body as `研究报告（最后收到的正文）`.
- **Pre-existing, untouched:** projection counters (`source_digests`,
  `markers`, `content_types`, `urls_moderated`) accumulate across turns on the
  same conversation while `report`, `started_at` and `finished_at` are per-turn.
  Not changed here; the report is deliberately per-turn.

## 6. Rollback

Single revert of this commit restores the previous projection and panel assets.
The report keys are additive; no retained state, endpoint contract, request
shape or upstream call is changed, so a revert cannot orphan stored data.

## 7. Test-harness note

`pytest tests/ tests_e2e/` in one process makes every `tests_e2e` route return
404 (verified against the unmodified baseline: 22 failures before any change in
this packet). The two roots pass when run as separate invocations. Pre-existing;
not introduced or addressed here.
