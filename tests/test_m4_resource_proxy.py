"""Unit tests for the M4 upload resource proxy (``gateway.resource_proxy``).

These exercise the handle registry and its host allowlist directly, because they
are the SSRF and cross-tenant boundary: the route will dereference whatever a
handle points at, so what may be registered, who may redeem it, and for how long
is the whole security contract.

Route-level behaviour (the real ``POST /backend-api/files`` entrypoint and the
real PUT proxy route) is covered in ``tests_e2e/test_m4_tools.py``, which needs
``ENABLE_GATEWAY=true`` and therefore a separate process.
"""

import time

import pytest

from gateway import resource_proxy


# ---------------------------------------------------------------------------
# Host allowlist: the guard against being turned into an open relay.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://sdmntprwestus.oaiusercontent.com/files/abc/raw?sig=x",
    "https://files.oaiusercontent.com/file-123",
    "https://videos.oaiusercontent.com/v/1",
])
def test_upstream_asset_hosts_are_proxyable(url):
    assert resource_proxy.is_proxyable_asset_host(url) is True


@pytest.mark.parametrize("url", [
    # Not an upstream asset host at all.
    "https://evil.example.com/steal",
    # Suffix smuggling: the allowlist must match on a domain boundary, not substring.
    "https://oaiusercontent.com.evil.example.com/x",
    # Plaintext would expose the SAS signature on the wire.
    "http://files.oaiusercontent.com/file-123",
    # Non-HTTP schemes must never reach an HTTP client.
    "file:///etc/passwd",
    "gopher://files.oaiusercontent.com/",
    # Loopback / link-local: the classic SSRF targets.
    "https://127.0.0.1:5024/admin",
    "https://169.254.169.254/latest/meta-data/",
    "",
])
def test_non_asset_hosts_are_refused(url):
    assert resource_proxy.is_proxyable_asset_host(url) is False


def test_credentialed_url_cannot_smuggle_an_allowed_host_as_userinfo():
    """``https://files.oaiusercontent.com@evil.example.com`` resolves to evil.example.com.

    A substring check on the raw URL would accept it; the hostname must be parsed.
    """
    url = "https://files.oaiusercontent.com@evil.example.com/x"
    assert resource_proxy.is_proxyable_asset_host(url) is False


# ---------------------------------------------------------------------------
# Handle registry: minting, tenant binding, expiry.
# ---------------------------------------------------------------------------

GOOD_URL = "https://sdmntprwestus.oaiusercontent.com/files/abc/raw?sig=secret"


def test_registering_a_disallowed_host_mints_no_handle():
    assert resource_proxy.register_upload_url(
        "https://evil.example.com/x", "seed-a", "tok-a") is None


def test_handle_is_opaque_and_does_not_leak_the_signed_url():
    handle = resource_proxy.register_upload_url(GOOD_URL, "seed-a", "tok-a")
    assert handle
    # The handle is what reaches the browser; a write-capable SAS signature must
    # not be reconstructable from it.
    assert "sig" not in handle and "oaiusercontent" not in handle
    assert "secret" not in handle


def test_handle_resolves_for_the_seed_that_minted_it():
    handle = resource_proxy.register_upload_url(GOOD_URL, "seed-a", "tok-a")
    entry, problem = resource_proxy._resolve(handle, "seed-a")
    assert problem is None
    assert entry["url"] == GOOD_URL
    assert entry["req_token"] == "tok-a"


def test_another_seed_cannot_redeem_someone_elses_upload_handle():
    """Cross-tenant redemption would let user B write into user A's upload slot."""
    handle = resource_proxy.register_upload_url(GOOD_URL, "seed-a", "tok-a")
    entry, problem = resource_proxy._resolve(handle, "seed-b")
    assert entry is None
    assert problem == "forbidden"


def test_unknown_handle_is_rejected():
    entry, problem = resource_proxy._resolve("not-a-real-handle", "seed-a")
    assert entry is None
    assert problem == "unknown"


def test_handles_are_unique_per_registration():
    first = resource_proxy.register_upload_url(GOOD_URL, "seed-a", "tok-a")
    second = resource_proxy.register_upload_url(GOOD_URL, "seed-a", "tok-a")
    assert first != second


def test_expired_handle_is_rejected_and_dropped(monkeypatch):
    """A SAS upload URL is short-lived; a stale handle must not outlive it."""
    handle = resource_proxy.register_upload_url(GOOD_URL, "seed-a", "tok-a")
    monkeypatch.setattr(time, "time",
                        lambda: resource_proxy._handles[handle]["created_at"]
                        + resource_proxy._HANDLE_TTL + 1)
    entry, problem = resource_proxy._resolve(handle, "seed-a")
    assert entry is None
    assert problem == "expired"
    assert handle not in resource_proxy._handles


def test_registry_does_not_grow_without_bound(monkeypatch):
    """Upload slots are created per user action; the registry must stay bounded."""
    monkeypatch.setattr(resource_proxy, "_MAX_HANDLES", 8)
    for _ in range(40):
        resource_proxy.register_upload_url(GOOD_URL, "seed-a", "tok-a")
    assert len(resource_proxy._handles) <= 8
