# REVIEW_PACKET — Plus trial settles on the research turn's own answer frame

Scope: `gateway/generation.py` (`_Trial` terminal-answer evidence) and one new
test file. Branch `codex/deepseek-research-trial-settlement` (worktree, unmerged).

## 1. What changed, in user terms

A Plus-trial user who runs a Deep Research turn now spends **one** of their three
generations when the turn really delivers an answer, and spends **nothing** when
what arrives is an interim tool/reasoning frame or a failed/cancelled turn. Before
this change the gateway ledger did the opposite on both counts: the real answer
frame (which carries its body as `content.text`) was invisible to it, so a
delivered research turn was refunded, while an interim assistant frame that
merely carried `end_turn` could settle the quota. The trial quota was therefore
not a bound on the most expensive model in the Plus whitelist.

## 2. Files changed

| File | Change |
| --- | --- |
| `gateway/generation.py` | `_Trial` now reads the answer body in both observed shapes (`content.text` **and** `content.parts`), requires the same completion evidence the research projection requires (`metadata.is_complete` + `end_turn` + `finished_successfully`), and rejects the frames that module already classifies as not-the-answer (hidden, tool-addressed, reasoning). +152/-20. |
| `tests/test_trial_research_settlement.py` | New. 15 tests driving the real ASGI generation lifetime with frames rebuilt from the real capture. |

No other file is touched: `gateway/share.py`, `utils/globals.py`, chat upstream
routing, SaaS templates and payment code are untouched.

## 3. Why this shape (decision record)

* **The evidence is taken from the research projection.** `project_event` decides
  "this turn completed" from `author.role == assistant` + `status ==
  finished_successfully` + `end_turn` + `metadata.is_complete is True`
  (`gateway/research_progress.py:596-599`), and it decides what body may speak
  from the same frame (`:598-599`, `_report_body` at `:399-427`, hidden/tool
  exclusion at `:384-396`). The trial ledger now asks the same question.
* **No import of `research_progress`.** That module mounts routes on `app`, so
  importing its projection from `gateway/generation.py` closes a cycle. The rule
  is restated once, in one place, next to the ledger it feeds, and both sides are
  pinned to the *same real capture* by their tests.
* **One rule, one deliberate divergence.** A frame that carries *no* message
  metadata is still settled on the legacy chat shape. Every message frame of the
  real capture carries metadata, so the two ledgers agree frame by frame on the
  stream this rule exists for; the carve-out keeps the ordinary chat shape (which
  the gateway's unit and e2e chat fixtures pin, and which has no completion flag
  to read) charging as it always did. Documented in `_declares_completion`.
* **Failure direction.** Every new rejection releases rather than settles, and
  the release direction is the one the /v1 ledger already takes on "not proven
  delivered" (`utils.trials.TrialAttempt`).

## 4. Evidence

### 4.1 RED — the new tests against the pre-change implementation

`git checkout -- gateway/generation.py` (original implementation), then:

```
$ python -m pytest tests/test_trial_research_settlement.py -o addopts=-q
FAILED tests/test_trial_research_settlement.py::test_the_captured_answer_frame_settles_the_trial_once
FAILED tests/test_trial_research_settlement.py::test_a_streamed_answer_is_settled_from_patch_frames
FAILED tests/test_trial_research_settlement.py::test_the_trial_is_charged_once_however_often_the_answer_arrives
FAILED tests/test_trial_research_settlement.py::test_an_interim_frame_carrying_end_turn_does_not_settle
FAILED tests/test_trial_research_settlement.py::test_a_tool_frame_carrying_end_turn_does_not_settle
FAILED tests/test_trial_research_settlement.py::test_a_hidden_frame_carrying_end_turn_does_not_settle
FAILED tests/test_trial_research_settlement.py::test_only_the_captures_answer_frame_can_settle_the_trial
7 failed, 8 passed in 0.20s
```

The two halves of the mismatch, from that run:

```
assert _run(monkeypatch, [answer]) == ['settled']
E  AssertionError: assert ['released'] == ['settled']
```
(the real answer frame, `content.text` + `metadata.is_complete`, charged nothing)

```
assert settled == [ANSWER_FRAME]
E  AssertionError: only the answer frame may charge, but [2, 3, 4, 9, 10, 11, 17, 18] charged
```
(eight interim / tool / hidden assistant frames each settled the quota, and the
answer frame was not among them)

### 4.2 GREEN — after the change

```
$ python -m pytest tests/test_trial_research_settlement.py -o addopts=-q
15 passed in 0.16s

$ python -m pytest tests/ -o addopts=-q
980 passed in 16.69s

$ python -m pytest tests_e2e/ -o addopts=-q
502 passed, 68 warnings in 269.66s

$ python -m pytest <focused: generation / research / trial> -o addopts=-q
299 passed in 1.86s

$ python -m pytest <targeted e2e: antiban / conversation / research / trial> -o addopts=-q
135 passed, 67 warnings in 71.39s
```

`tests_e2e/test_antiban_generation_feedback.py::test_delivered_completion_charges_the_trial_once`
(the ordinary chat turn through the mock upstream, `metadata: {}`) and
`::test_disconnect_after_the_answer_releases_the_trial` are both in that run.

### 4.3 The reviewer's own reproduction, before and after

The finding's reproduction script (read-only, stubbed ledger, no account, no
upstream, no production database) is unchanged; only the implementation differs.

```
before:  observed answer frame: content.text   assistant_complete=False -> RELEASED (trial NOT charged)
         chat shape: content.parts             assistant_complete=True  -> SETTLED (trial charged)

after:   observed answer frame: content.text   assistant_complete=True  -> SETTLED
         chat shape: content.parts             assistant_complete=True  -> SETTLED
```

Conversely, `_Trial` driven directly with the frames that must not charge:

```
index 12 answer (content.text, is_complete)                              -> SETTLED
index 4 interim (parts, NO is_complete key)                              -> RELEASED
index 4 interim with is_complete=False                                   -> RELEASED
index 11 interim (parts, is_complete forced True, upstream-hidden)       -> RELEASED
index 17 tool frame (invoked_resource, hidden dropped, is_complete True) -> RELEASED
index 12 answer with status=failed                                       -> RELEASED
```

## 5. Acceptance criteria

| Criterion | Test | Status |
| --- | --- | --- |
| Real research terminal answer with `content.text`, no parts, settles once | `test_the_captured_answer_frame_settles_the_trial_once`, `test_the_trial_is_charged_once_however_often_the_answer_arrives` | Verified |
| Interim / tool / hidden assistant frames with `end_turn` do not settle | `test_an_interim_frame_carrying_end_turn_does_not_settle`, `test_a_tool_frame_...`, `test_a_hidden_frame_...`, `test_only_the_captures_answer_frame_can_settle_the_trial` | Verified |
| Failed / cancelled / incomplete stream releases | `test_a_failed_cancelled_or_incomplete_turn_releases` (3 params) | Verified |
| Ordinary chat parts-shape completion stays compatible | `test_an_ordinary_chat_parts_completion_still_settles` + the pre-existing `test_completed_generation_settles_the_trial_once` and the e2e chat-trial tests | Verified |
| Exactly-once settlement | `_Trial.finish`'s `settled` latch (pre-existing); `test_the_trial_is_charged_once_however_often_the_answer_arrives` | Verified |

## 6. Manual acceptance (no account, no upstream)

```
python -m pytest tests/test_trial_research_settlement.py -v
python -m pytest tests/test_generation_lifetime.py tests/test_generation_feedback.py \
                 tests/test_research_projection.py tests/test_research_report_projection.py -q
python -m pytest tests_e2e/test_antiban_generation_feedback.py -q
```

## 7. Risks and rollback

* **A post-answer full-message frame would clear the answer evidence**, because
  `_snapshot` resets the message flags (it always did). The capture contains no
  such frame after index 12 (the frames that follow are title/marker/status
  frames that carry no message object), and the failure direction is a release,
  not a false charge. Untested against a real upstream; unverified.
* **`metadata.is_complete` is required of any frame that carries metadata.**
  Verified against the real research capture (index 12 is the only frame with the
  flag; the interim frames have metadata and no flag). For an ordinary chat turn
  the gateway's only evidence is its own synthetic mock, which carries
  `metadata: {}` and still settles — so ordinary chat is verified only for that
  shape. If a future capture shows a real chat completion frame that carries
  metadata *without* `is_complete`, ordinary chat would release instead of
  settle; the fix is to widen `_declares_completion`, and the test that would
  catch it is `test_an_ordinary_chat_parts_completion_still_settles`.
* **Not touched:** `gateway/share.py`, `utils/globals.py`, chat upstream routing,
  SaaS templates, payment code. No real accounts, tokens, cookies, upstream
  calls or production databases were used; the fixture is the committed sanitized
  capture and every reservation id in the tests is synthetic.
* **Rollback:** revert the single commit. Nothing is persisted and no schema,
  route or config changes.

## 8. Evidence provenance

| Item | Source | Confidence |
| --- | --- | --- |
| Terminal evidence (`is_complete`/`end_turn`/`content.text`, hidden/tool frames) | `tests/fixtures/deep_research_plus_shapes.json`, the committed 2026-09-12 capture (shapes only), read through `tests/test_research_projection.py`'s reconstruction | Verified |
| Red/green test output | commands in section 4, run in this worktree | Verified |
| Reviewer's mismatch | their reproduction script, re-run against both implementations | Verified |
| Ordinary chat metadata shape in production | not captured; only the repo's mock upstream | Cannot verify |
