"""Antiban-enabled turns must not hand browser-profile metadata to the transport.

Root-cause regression for the second-turn 502:

``utils.antiban.fingerprint.ensure_extended`` (called from admission) persists
browser-profile metadata - ``screen``, ``viewport``, ``webgl``, ... as nested
structures - into the *same* ``fp_map`` entry that ``chatgpt.fp.get_fp`` returns.
``gateway.reverseProxy`` then merged that whole record into the outbound headers
(``headers.update(fp)``), so curl_cffi's header encoder hit a dict and raised
``AttributeError: 'dict' object has no attribute 'encode'``; the catch-all turned
it into ``502 Upstream request failed``. The reported asymmetry - first turn
fine, second turn 502 - is an ordering artifact: the turn that admits the
account is also the turn that persists the profile, so whichever turn builds its
headers *after* admission crashes. Reverse-proxy turns admit before assembling,
hence both turns fail here; the fix must make both succeed.

Every assertion below reads the recorded upstream request or the persisted
fingerprint, never a log line.
"""

import asyncio
import json

import pytest

import utils.configs as configs
import utils.globals as globals

ACCOUNT = "acc-fpx"
SEED = "seed-fpx"

# Fields that exist only to drive antiban / browser-profile decisions. None of
# them is an HTTP header; all of them used to travel upstream verbatim.
# ``connection`` is deliberately absent: it is also a transport-level header
# emitted by curl_cffi itself, so only its *value* is checked below.
PROFILE_ONLY_FIELDS = (
    "screen", "viewport", "webgl", "webgpu", "webrtc", "audio",
    "languages", "nav_platform", "pixel_ratio", "hardware_concurrency",
    "device_memory", "max_touch_points", "color_scheme", "color_gamut",
    "prefers_reduced_motion", "canvas_hash", "font_list_hash",
    "font_list_count", "audio_fp_hash", "timezone", "intl_locale",
    "user_pace", "virtual_page_load_ms",
    # Internal routing metadata written by utils.routing.sync_bindings_to_fp.
    "group", "proxy_name", "updated_at",
)
# Profile fields whose name alone can never be a legitimate HTTP header.
UNAMBIGUOUS_PROFILE_KEYS = PROFILE_ONLY_FIELDS


def _renderings(value):
    """Every textual form a profile value could take on the wire."""
    if isinstance(value, (dict, list)):
        return {json.dumps(value), json.dumps(value, separators=(",", ":"))}
    return {str(value)}


@pytest.fixture
def antiban_turns_account(monkeypatch, tmp_path, make_access_token, seed_user, seed_account):
    """One seed bound to one verified account, with antiban admission enabled."""
    from gateway import frontend_sync as frontend
    from utils.antiban import cooldown

    frontend.invalidate_frontend_cache()
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    monkeypatch.setattr(frontend, "SESSION_ARCHIVE_DIR", tmp_path)
    access = make_access_token(account_id=ACCOUNT, plan_type="plus")
    (tmp_path / (ACCOUNT + ".json")).write_text(
        json.dumps({"account": {"id": ACCOUNT}, "sessionToken": "website-" + ACCOUNT}))

    def fetch(cookies, account_id, fingerprint, **kwargs):
        return {
            "html": "<html></html>",
            "session": {
                "user": {"id": "u-" + account_id},
                "account": {"id": account_id, "planType": "plus"},
                "accessToken": access,
                "sessionToken": "PRIVATE-SESSION",
            },
            "cookies": dict(cookies),
        }

    monkeypatch.setattr(frontend, "_fetch_official_html_sync", fetch)
    monkeypatch.setattr(configs, "enable_antiban", True)
    # This suite isolates the HTTP-header boundary. Pacing/cooldown behavior is
    # covered separately and must not make the deterministic second turn wait.
    monkeypatch.setattr(configs, "account_min_interval_seconds", 0)
    monkeypatch.setattr(configs, "account_cooldown_jitter", 0)
    seed_account(access, plan_type="plus")
    seed_user(SEED, access, plan_type="plus")
    # Warm the account's website context the way rendering its page would.
    asyncio.run(frontend.get_frontend_template(access, access, {}))
    yield access
    frontend.invalidate_frontend_cache()
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()


def _body():
    return {
        "model": "gpt-5-6",
        "messages": [{"id": "msg-u1", "author": {"role": "user"},
                      "content": {"content_type": "text", "parts": ["hi"]}}],
        "conversation_id": "conv-fpx",
        "parent_message_id": "client-created-root",
    }


def _turn(client):
    return client.post("/backend-api/conversation", cookies={"token": SEED}, json=_body())


def _turns(mock_upstream):
    return [r for r in mock_upstream.records
            if r["path"].split("?")[0] == "/backend-api/conversation"]


def test_consecutive_admitted_turns_survive_extended_fingerprint(
        client, mock_upstream, antiban_turns_account):
    """Two admitted turns in a row must both reach the upstream."""
    first = _turn(client)
    second = _turn(client)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert len(_turns(mock_upstream)) == 2


def test_outbound_headers_carry_the_profile_never_the_profile_record(
        client, mock_upstream, antiban_turns_account):
    """Fingerprint headers stay; browser-profile metadata must not leave the process."""
    assert _turn(client).status_code == 200
    assert _turn(client).status_code == 200

    fp = globals.fp_map[antiban_turns_account]
    assert isinstance(fp.get("screen"), dict), "antiban lost its persisted profile"
    assert isinstance(fp.get("webgl"), dict), "antiban lost its persisted webgl profile"

    for record in _turns(mock_upstream):
        headers = record["headers"]
        for leaked in UNAMBIGUOUS_PROFILE_KEYS:
            assert leaked not in headers, f"{leaked} was sent upstream as an HTTP header"
        # The pre-fix leak was value-level too: routing metadata (group/proxy_name/
        # updated_at) is a plain string and would have travelled as a well-formed
        # header. Compare values, since names like ``connection`` are transport-own.
        for field in UNAMBIGUOUS_PROFILE_KEYS + ("connection",):
            if field in headers and field in fp:
                sent = _renderings(fp[field]) | {str(fp[field])}
                assert headers[field] not in sent, f"{field} value reached the upstream"
        # A scalar header value cannot arrive as anything but a string, so the
        # fingerprint headers themselves must survive the boundary untouched.
        assert headers.get("user-agent") == fp["user-agent"]
        assert headers.get("sec-ch-ua-platform") == fp["sec-ch-ua-platform"]
        assert all(isinstance(value, str) for value in headers.values())


def test_profile_still_reaches_antiban_consumers(client, antiban_turns_account):
    """The boundary trims the outbound copy only; antiban decisions keep the profile.

    Two turns, not one: admission extends the record before ``get_fp`` has ever
    materialised a base fingerprint for this token, and ``get_fp`` regenerates a
    record that has no UA/impersonate instead of merging into the profile-only
    one. That first-turn discard is pre-existing and out of this boundary's
    scope (see the report's residual risks); from the second turn on the profile
    is the persisted one antiban reads.
    """
    from utils.antiban import fingerprint

    assert _turn(client).status_code == 200
    assert _turn(client).status_code == 200

    contextual = fingerprint.get_contextual_info(antiban_turns_account)
    assert contextual is not None
    assert contextual["screen_width"] > 0
    assert contextual["screen_height"] > 0
    assert contextual["page_width"] <= contextual["screen_width"]
    assert fingerprint.get_hardware_concurrency(antiban_turns_account)


def test_f_conversation_path_is_covered_by_the_same_boundary(client, mock_upstream, antiban_turns_account):
    """The official frontend path uses the same scalar-only header boundary."""
    first = client.post("/backend-api/f/conversation", cookies={"token": SEED}, json=_body())
    second = client.post("/backend-api/f/conversation", cookies={"token": SEED}, json=_body())
    assert (first.status_code, second.status_code) == (200, 200)
