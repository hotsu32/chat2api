"""Tests for the M5 acceptance harness itself.

An acceptance harness that is wrong produces confident, wrong verdicts -- which
is worse than no harness, because a reader trusts it. These tests pin the
properties the harness's credibility rests on:

  * redaction actually redacts (no credential can reach an evidence file);
  * the matrix cannot silently lose a cell, and an unmeasured cell can never
    read as a pass;
  * the leak scanner distinguishes "a JWT is present" from "the pooled account's
    real upstream token is present" -- the difference between normal frontend
    operation and a credential leak.

These are pure-function tests: no server, no browser, no database.
"""
from __future__ import annotations

import json

import pytest

from scripts.mirror_acceptance import leak_scan, matrix, redact


# --------------------------------------------------------------------- redact

def test_anon_id_is_stable_and_does_not_contain_the_secret():
    secret = "sk-live-abcdef0123456789"
    first = redact.anon_id(secret)
    assert first == redact.anon_id(secret), "same input must yield the same label"
    assert secret not in first
    assert first != redact.anon_id(secret + "x"), "different inputs must differ"


def test_anon_id_handles_missing_value_without_raising():
    assert redact.anon_id(None).endswith("#none")
    assert redact.anon_id("").endswith("#none")


def test_safe_account_drops_every_sensitive_column():
    row = {
        "token": "eyJreal.token.value",
        "refresh_info": '{"refresh_token": "secret"}',
        "fingerprint": "fp-secret",
        "real_email": "someone@example.com",
        "nickname": "Real Name",
        "proxy_url": "http://user:pass@proxy:8080",
        "note": "operator private note",
        "user_agent": "Mozilla/5.0 (distinctive fingerprint)",
        "plan_type": "pro",
        "status": "healthy",
    }
    out = redact.safe_account(row)
    assert out["plan_type"] == "pro"
    assert out["status"] == "healthy"
    serialised = json.dumps(out)
    # The allowlist -- not the denylist -- is what keeps these out, which is why
    # `note` and `user_agent` are asserted here even though `assert_clean` does
    # not reject those key names globally (a matrix cell has a legitimate `note`).
    for secret in ("eyJreal", "secret", "fp-secret", "someone@example.com",
                   "Real Name", "user:pass", "operator private note",
                   "distinctive fingerprint"):
        assert secret not in serialised


def test_safe_user_reduces_the_binding_to_an_anonymous_handle():
    out = redact.safe_user({"seed": "frontend-proof-pro-4", "plan_type": "pro",
                            "current_account": "eyJtoken", "status": "active"})
    assert out["plan_type"] == "pro"
    assert "eyJtoken" not in json.dumps(out)
    assert out["bound_account"].startswith("acct#")


def test_assert_clean_rejects_a_sensitive_key_at_any_depth():
    # The realistic failure is a nested copy-paste, not a top-level one.
    payload = {"results": [{"meta": {"token": "eyJleak"}}]}
    with pytest.raises(AssertionError) as excinfo:
        redact.assert_clean(payload)
    assert "token" in str(excinfo.value)


def test_assert_clean_accepts_a_properly_redacted_payload():
    redact.assert_clean({"account": "acct#abc123", "plan_type": "plus",
                         "nested": [{"verdict": "consistent"}]})


# --------------------------------------------------------------------- matrix

def test_default_matrix_covers_every_tier_form_and_feature():
    cells = matrix.default_matrix()
    assert {c.tier for c in cells} == set(matrix.TIERS)
    assert {c.form for c in cells} == set(matrix.FORMS)
    # All four tools from the plan's M4 section must be present as cells.
    for feature in ("deep_research", "web_search", "file_analysis", "image_gen"):
        assert any(c.feature == feature for c in cells), feature


def test_every_cell_starts_unmeasured_so_nothing_is_assumed_to_pass():
    cells = matrix.default_matrix()
    assert all(c.status == "NOT_MEASURED" for c in cells)
    summary = matrix.Matrix(cells).summary()
    assert summary["coverage_pct"] == 0.0
    assert summary["verdict"] == "INCOMPLETE"


def test_cell_keys_are_unique():
    cells = matrix.default_matrix()
    keys = [c.key for c in cells]
    assert len(keys) == len(set(keys)), "a duplicate key would silently overwrite a result"


def test_partial_coverage_never_reports_a_pass_verdict():
    m = matrix.Matrix()
    # Mark every cell but one as passing -- the verdict must still be INCOMPLETE.
    for cell in m.cells[:-1]:
        m.record(cell.key, "PASS")
    assert m.summary()["verdict"] == "INCOMPLETE"


def test_full_coverage_with_a_failure_reports_fail():
    m = matrix.Matrix()
    for cell in m.cells:
        m.record(cell.key, "PASS")
    m.record(m.cells[0].key, "FAIL", note="streaming never incremented")
    assert m.summary()["verdict"] == "FAIL"


def test_record_rejects_an_unknown_status():
    m = matrix.Matrix()
    with pytest.raises(ValueError):
        m.record(m.cells[0].key, "PROBABLY_FINE")


def test_record_rejects_an_unknown_cell():
    m = matrix.Matrix()
    with pytest.raises(KeyError):
        m.record("free/desktop/new/cold/telepathy/none", "PASS")


def test_blocked_is_distinct_from_fail_and_from_pass():
    m = matrix.Matrix()
    key = m.cells[0].key
    m.record(key, "BLOCKED", note="cloudflare challenge")
    counts = m.summary()["by_status"]
    assert counts["BLOCKED"] == 1
    assert counts["PASS"] == 0
    assert counts["FAIL"] == 0


def test_generation_budget_tracks_recorded_generations():
    m = matrix.Matrix()
    m.record(m.cells[0].key, "PASS", generations=2)
    m.record(m.cells[1].key, "PASS", generations=3)
    assert m.generations_used() == 5
    assert m.budget_remaining() == matrix.TOTAL_GENERATION_BUDGET - 5


def test_roundtrip_through_disk_preserves_results(tmp_path):
    m = matrix.Matrix()
    key = m.cells[0].key
    m.record(key, "PASS", evidence="turn-free1.json", generations=1,
             metrics={"dom_changes_before_terminal": 7})
    path = tmp_path / "matrix.json"
    m.save(str(path))
    reloaded = matrix.Matrix.load(str(path))
    cell = reloaded.get(key)
    assert cell.status == "PASS"
    assert cell.metrics["dom_changes_before_terminal"] == 7
    assert reloaded.generations_used() == 1


def test_markdown_shows_unmeasured_cells_explicitly():
    m = matrix.Matrix()
    m.record(m.cells[0].key, "PASS")
    md = m.markdown()
    # The whole point: a reader must see the gaps, not just the greens.
    assert "-- not measured --" in md
    assert "coverage" in md


def test_saving_a_matrix_with_a_leaked_token_raises(tmp_path):
    m = matrix.Matrix()
    m.record(m.cells[0].key, "PASS", metrics={"token": "eyJleaked"})
    with pytest.raises(AssertionError):
        m.save(str(tmp_path / "matrix.json"))


# ------------------------------------------------------------------ leak_scan

# A syntactically valid JWT whose payload decodes; not a real credential.
def _fake_jwt(payload: str) -> str:
    import base64
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.c2lnbmF0dXJlc2lnbmF0dXJl"


def test_scan_page_finds_no_jwt_in_clean_content():
    result = leak_scan.scan_page("<html><body>hello</body></html>", {})
    assert result["jwt_count"] == 0
    assert result["upstream_credential_exposed"] is False


def test_scan_page_flags_the_pooled_accounts_real_token():
    token = _fake_jwt('{"iss":"https://auth.openai.com","exp":123}')
    digests = {"accounts.token": leak_scan._digest(token)}
    result = leak_scan.scan_page(f'<script>{{"accessToken":"{token}"}}</script>', digests)
    assert result["jwt_count"] == 1
    assert result["upstream_credential_exposed"] is True
    assert result["findings"][0]["matches_upstream"] == ["accounts.token"]


def test_scan_page_does_not_flag_an_unrelated_jwt():
    # A mirror-scoped token is fine; only the pooled account's token is a leak.
    page_token = _fake_jwt('{"iss":"https://mirror.local"}')
    other = _fake_jwt('{"iss":"https://auth.openai.com"}')
    digests = {"accounts.token": leak_scan._digest(other)}
    result = leak_scan.scan_page(f"<script>{page_token}</script>", digests)
    assert result["jwt_count"] == 1
    assert result["upstream_credential_exposed"] is False


def test_classification_never_echoes_the_token():
    token = _fake_jwt('{"iss":"https://auth.openai.com"}')
    finding = leak_scan.classify_jwt(token, {})
    serialised = json.dumps(finding)
    assert token not in serialised
    assert token[:24] not in serialised
    # Metadata we do need for triage survives.
    assert finding["issuer"] == "https://auth.openai.com"
    assert finding["length"] == len(token)


def test_digest_is_one_way_and_short_enough_to_be_useless_alone():
    digest = leak_scan._digest("some-secret-value")
    assert len(digest) == 12
    assert "some-secret" not in digest
