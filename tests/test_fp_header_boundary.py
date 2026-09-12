"""The outbound boundary between fingerprint headers and browser-profile metadata.

An ``fp_map`` entry is one flat namespace serving two consumers whose
requirements are opposite:

* transport - a small set of stable, scalar values that leave the process as
  HTTP headers (UA, ``sec-ch-ua-*``, ``oai-*``);
* antiban / browser profile - structured metadata (``screen``, ``viewport``,
  ``webgl``, ...) written by ``utils.antiban.fingerprint.ensure_extended``, plus
  the routing metadata (``group`` / ``proxy_name`` / ``updated_at``) written by
  ``utils.routing``.

Merging the record into headers (``headers.update(fp)``) fed curl_cffi a dict
and its encoder raised ``AttributeError: 'dict' object has no attribute
'encode'`` -> 502. The boundary is an allowlist, not a denylist: a new profile
field must not be able to leak upstream just because nobody remembered to add
it to a skip list.
"""

import pytest

import utils.configs as configs
import utils.globals as globals
from chatgpt.fp import FP_HEADER_FIELDS, extract_header_fp, get_fp
from utils.antiban import fingerprint


def _extended_record():
    return {
        "user-agent": "Mozilla/5.0 (Macintosh) Chrome/124.0.0.0 Safari/537.36",
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "oai-device-id": "6f1c6f4e-0000-4000-8000-000000000000",
        "oai-session-id": "5b2a5b2a-0000-4000-8000-000000000000",
        "screen": {"width": 1920, "height": 1080, "color_depth": 24},
        "viewport": {"page_width": 1820, "page_height": 900},
        "webgl": {"vendor": "Apple Inc.", "renderer": "Apple M1"},
        "webgpu": {"architecture": "apple-silicon", "vendor": "apple"},
        "webrtc": {"local_ip": "192.168.1.24", "ice_candidate_type": "host"},
        "connection": {"effective_type": "4g", "downlink": 12.5},
        "audio": {"sample_rate": 48000},
        "languages": ["en-US", "en"],
        "group": "plus",
        "proxy_name": "residential-1",
        "updated_at": "2026-01-01T00:00:00Z",
        "proxy_url": "http://user:pass@10.0.0.1:8080",
        "impersonate": "chrome124",
    }


def test_outbound_headers_are_an_allowlist_not_a_pass_through():
    headers = extract_header_fp(_extended_record())
    assert set(headers) == {
        "user-agent", "sec-ch-ua-platform", "sec-ch-ua", "oai-device-id", "oai-session-id"}


@pytest.mark.parametrize("field", [
    "screen", "viewport", "webgl", "webgpu", "webrtc", "connection", "audio",
    "languages", "group", "proxy_name", "updated_at", "proxy_url", "impersonate",
])
def test_profile_and_routing_fields_never_become_headers(field):
    record = _extended_record()
    assert field in record, "test data drifted from the fp record contract"
    assert field not in extract_header_fp(record)


def test_header_values_survive_verbatim():
    headers = extract_header_fp(_extended_record())
    assert headers["user-agent"] == _extended_record()["user-agent"]
    assert headers["sec-ch-ua-platform"] == '"macOS"'
    assert headers["sec-ch-ua"] == '"Chromium";v="124", "Google Chrome";v="124"'


def test_keys_are_lowercased_so_the_allowlist_matches_stored_records():
    assert extract_header_fp({"User-Agent": "UA/1.0", "OAI-Device-Id": "d1"}) == {
        "user-agent": "UA/1.0", "oai-device-id": "d1"}


@pytest.mark.parametrize("value", [{"nested": "dict"}, ["list"], None, object(), True, False])
def test_unsupported_values_are_dropped_loudly_not_encoded(monkeypatch, caplog, value):
    """An allowlisted field holding a bad value is a contract violation, not noise.

    JSON-encoding it would put a well-formed but meaningless header on the wire;
    curl_cffi raises on a dict, and sends a bool as libcurl's own "1"/"0" rather
    than the field's convention (sec-ch-ua-mobile uses "?1"/"?0"). Drop and say so.
    """
    caplog.set_level("WARNING")
    assert extract_header_fp({"sec-ch-ua-platform": value}) == {}
    assert "sec-ch-ua-platform" in caplog.text


def test_numbers_are_stringified_because_float_would_crash_the_transport():
    headers = extract_header_fp({
        "oai-device-id": 7, "oai-session-id": 1.5, "sec-ch-ua-mobile": "?0",
    })
    assert headers == {"oai-device-id": "7", "oai-session-id": "1.5", "sec-ch-ua-mobile": "?0"}
    for value in headers.values():
        assert value.encode("latin-1") is not None


def test_every_value_is_encodable_by_the_http_transport():
    """curl_cffi encodes each header value; a dict fails exactly here."""
    for value in extract_header_fp(_extended_record()).values():
        assert isinstance(value, str)
        assert value.encode("latin-1") is not None


def test_ensure_extended_adds_profile_fields_only():
    """Guards the boundary from the producer side: no profile field is a header."""
    globals.fp_map["tok-boundary"] = {}
    extended = fingerprint.ensure_extended("tok-boundary")
    assert extended, "ensure_extended produced nothing; test data drifted"
    assert set(extended) & FP_HEADER_FIELDS == set()
    assert extract_header_fp(extended) == {}


def test_real_extended_record_yields_scalar_headers_only(monkeypatch):
    """End-to-end at module level: a generated + extended record stays encodable."""
    monkeypatch.setattr(configs, "enable_antiban", True)
    globals.fp_map["tok-real"] = {}
    configs.proxy_url_list[:] = []

    get_fp("tok-real")
    fingerprint.ensure_extended("tok-real")
    merged = get_fp("tok-real")

    assert isinstance(merged.get("screen"), dict), "profile must stay available to antiban"
    headers = extract_header_fp(merged)
    assert headers.get("sec-ch-ua-platform"), "CH fingerprint header was lost"
    assert all(isinstance(value, str) for value in headers.values())
    for leaked in ("screen", "viewport", "webgl", "group", "proxy_name", "updated_at"):
        assert leaked not in headers
