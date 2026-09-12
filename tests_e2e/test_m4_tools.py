"""M4 E2E — the four tool flows through the real gateway routes.

Failure-first: every test below asserts on a distinct failure class the user must
be able to tell apart, because "the button appeared" is not a working tool:

  no-permission   -> banned/forbidden paths and cross-tenant redemption
  quota           -> upstream 429 surfaced as 429, not a fake success
  proxy           -> upstream transport failure surfaced as 502
  protocol        -> upstream 4xx with a reason (file_size_mismatch) passed through
  resource        -> artifact URLs must be same-origin and fetchable
  cancel/resume   -> client disconnect and reload must not create a second task

All requests go through the real production entrypoints registered by
``app.py`` (``POST /backend-api/files``, ``PUT /backend-api/resource/upload/...``,
``GET /backend-api/files/<id>/download``, ``POST /backend-api/conversation``).
Nothing here re-implements gateway logic: the mock only plays chatgpt.com, and the
assertions read what the gateway actually sent and returned.
"""

import json
from urllib.parse import urlsplit

import pytest

import utils.globals as globals


UPLOAD_HOST = "https://sdmntprwestus2.oaiusercontent.com"
SAS_UPLOAD_URL = UPLOAD_HOST + "/files/file-abc/raw?se=2030&sig=WRITE-CAPABLE-SECRET"
# Shape observed from real upstream: the artifact URL upstream hands back is on
# chatgpt.com, which the gateway's rewrite table already maps to the mirror origin.
DOWNLOAD_URL = "https://chatgpt.com/backend-api/estuary/content?id=file-abc&sig=READ"
PAYLOAD = b"M4 e2e payload: the file-only marker is CHARTREUSE-7.\n"


@pytest.fixture
def website_sessions(monkeypatch, tmp_path, make_access_token):
    """Pin the bound website session so no real credential or network is used."""
    from gateway import frontend_sync as frontend
    frontend.invalidate_frontend_cache()
    monkeypatch.setattr(frontend, "SESSION_ARCHIVE_DIR", tmp_path)
    (tmp_path / "acc-m4.json").write_text(json.dumps(
        {"account": {"id": "acc-m4"}, "sessionToken": "test-acc-m4"}))

    def fetch(cookies, account_id, fingerprint, **kwargs):
        session = {"user": {"name": "Private owner", "email": "owner@example.test"},
                   "account": {"id": account_id, "planType": "plus"},
                   "accessToken": make_access_token(account_id=account_id),
                   "sessionToken": "PRIVATE-SESSION"}
        return {"html": "<html></html>", "session": session, "cookies": cookies}

    monkeypatch.setattr(frontend, "_fetch_official_html_sync", fetch)
    yield
    frontend.invalidate_frontend_cache()


@pytest.fixture
def mirror_user(client, seed_account, seed_user, make_access_token, website_sessions):
    """A mirror user (seed cookie) bound to a synthetic plus account."""
    token = make_access_token(account_id="acc-m4", plan_type="plus")
    seed_account(token)
    seed_user("seed-m4", token, plan_type="plus")
    client.cookies.set("token", "seed-m4")
    return client


def _upstream_files_router(server, *, create=None, uploaded=None, download=None):
    """Teach the mock upstream the file endpoints, with per-test responses.

    Installs a per-server handler *subclass* rather than patching the shared
    ``_RecordingHandler``, so one test's file routes cannot leak into another's.
    """
    server.file_responses = {
        "create": create if create is not None else (200, {
            "file_id": "file-abc", "status": "success", "upload_url": SAS_UPLOAD_URL}),
        "uploaded": uploaded if uploaded is not None else (200, {
            "file_id": "file-abc", "status": "success", "file_size_bytes": len(PAYLOAD),
            "download_url": DOWNLOAD_URL}),
        "download": download if download is not None else (200, {
            "status": "success", "download_url": DOWNLOAD_URL}),
    }
    base = server.RequestHandlerClass

    def route(path):
        path = path.split("?")[0]
        if path == "/backend-api/files":
            return "create"
        if path.endswith("/uploaded"):
            return "uploaded"
        if path.endswith("/download"):
            return "download"
        return None

    def _reply(self, kind, body=b""):
        self._record(body)
        code, payload = self.server.file_responses[kind]
        if isinstance(payload, str):
            # Non-JSON upstream body (e.g. an HTML error page from an edge node).
            self._send(code, payload.encode("utf-8"), "text/html")
        else:
            self._json(code, payload)

    class _FileHandler(base):
        def do_POST(self):
            kind = route(self.path)
            if kind:
                return _reply(self, kind, self._read_body())
            return base.do_POST(self)

        def do_GET(self):
            kind = route(self.path)
            if kind:
                return _reply(self, kind)
            return base.do_GET(self)

    server.RequestHandlerClass = _FileHandler
    return server


# ---------------------------------------------------------------------------
# Resource class: the upload target must be same-origin (the P1 defect).
# ---------------------------------------------------------------------------

def test_create_upload_returns_same_origin_upload_url(mirror_user, mock_upstream):
    """The browser must never be handed the Azure SAS host directly.

    Before the resource proxy, ``upload_url`` reached the page as
    ``sdmntpr*.oaiusercontent.com``, so the upload PUT left the user's machine
    outside the mirror and outside the account's bound egress proxy.
    """
    _upstream_files_router(mock_upstream)
    resp = mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD), "use_case": "multimodal"})
    assert resp.status_code == 200
    upload_url = resp.json()["upload_url"]
    assert urlsplit(upload_url).netloc == urlsplit(str(mirror_user.base_url)).netloc
    assert "oaiusercontent.com" not in upload_url


def test_create_upload_does_not_leak_the_write_capable_sas_signature(mirror_user, mock_upstream):
    """The SAS query grants blob writes; it must stay inside the process."""
    _upstream_files_router(mock_upstream)
    resp = mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD), "use_case": "multimodal"})
    assert "WRITE-CAPABLE-SECRET" not in resp.text
    assert "sig=" not in resp.json()["upload_url"]


def test_upload_proxy_forwards_bytes_to_the_upstream_signed_url(mirror_user, mock_upstream,
                                                               monkeypatch):
    """A PUT to the handle must reach the exact upstream URL, bytes intact."""
    _upstream_files_router(mock_upstream)
    created = mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD), "use_case": "multimodal"})
    handle_path = urlsplit(created.json()["upload_url"]).path

    sent = {}

    class _FakeResponse:
        status_code = 201
        headers = {"content-type": "application/xml", "etag": '"0x8D"'}
        content = b""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            sent["client_kwargs"] = kwargs

        async def put(self, url, headers=None, data=None, **kwargs):
            sent["url"] = url
            sent["headers"] = headers
            sent["data"] = data
            return _FakeResponse()

        async def close(self):
            sent["closed"] = True

        async def discard(self):
            sent["discarded"] = True

    from gateway import resource_proxy
    monkeypatch.setattr(resource_proxy, "Client", _FakeClient)

    resp = mirror_user.put(handle_path, content=PAYLOAD,
                           headers={"content-type": "text/plain",
                                    "x-ms-blob-type": "BlockBlob"})
    assert resp.status_code == 201
    assert sent["url"] == SAS_UPLOAD_URL
    assert sent["data"] == PAYLOAD
    # Azure needs the blob-type header; our own credentials must not ride along.
    assert sent["headers"]["x-ms-blob-type"] == "BlockBlob"
    assert "authorization" not in {k.lower() for k in sent["headers"]}
    assert "cookie" not in {k.lower() for k in sent["headers"]}
    assert sent.get("closed") is True


def test_download_url_is_same_origin(mirror_user, mock_upstream):
    """The artifact side was already mirrored; lock it in so the pair stays symmetric.

    Verified against real upstream on port 5024: the artifact URL comes back on
    chatgpt.com and is rewritten to the mirror origin, so the browser fetches the
    image/file through us.  This test is the regression guard for that half.
    """
    _upstream_files_router(mock_upstream)
    resp = mirror_user.get("/backend-api/files/file-abc/download")
    assert resp.status_code == 200
    download_url = resp.json()["download_url"]
    assert urlsplit(download_url).netloc == urlsplit(str(mirror_user.base_url)).netloc
    assert "chatgpt.com" not in download_url


# ---------------------------------------------------------------------------
# No-permission class.
# ---------------------------------------------------------------------------

def test_unknown_upload_handle_is_404_not_a_silent_success(mirror_user, mock_upstream):
    resp = mirror_user.put("/backend-api/resource/upload/bogus-handle", content=PAYLOAD)
    assert resp.status_code == 404


def test_other_users_upload_handle_is_403(mirror_user, mock_upstream, client_factory,
                                          seed_account, seed_user, make_access_token):
    """User B must not be able to write into user A's upload slot."""
    _upstream_files_router(mock_upstream)
    handle_path = urlsplit(mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD),
        "use_case": "multimodal"}).json()["upload_url"]).path

    other = client_factory()
    token_b = make_access_token(account_id="acc-m4", plan_type="plus")
    seed_account(token_b)
    seed_user("seed-other", token_b, plan_type="plus")
    other.cookies.set("token", "seed-other")

    resp = other.put(handle_path, content=PAYLOAD)
    assert resp.status_code == 403


def test_expired_upload_handle_is_410(mirror_user, mock_upstream, monkeypatch):
    """An expired handle must say so, not fall through to a confusing 404/502."""
    _upstream_files_router(mock_upstream)
    handle_path = urlsplit(mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD),
        "use_case": "multimodal"}).json()["upload_url"]).path

    from gateway import resource_proxy
    monkeypatch.setattr(resource_proxy, "_HANDLE_TTL", -1.0)
    resp = mirror_user.put(handle_path, content=PAYLOAD)
    assert resp.status_code == 410


def test_upload_slot_is_not_created_when_upstream_denies_entitlement(mirror_user, mock_upstream):
    """A 403 from upstream must reach the user as 403 and mint no handle."""
    _upstream_files_router(mock_upstream, create=(403, {
        "detail": {"code": "feature_not_available", "message": "Not available on your plan"}}))
    from gateway import resource_proxy
    before = len(resource_proxy._handles)
    resp = mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD), "use_case": "multimodal"})
    assert resp.status_code == 403
    assert "Not available on your plan" in resp.text
    assert len(resource_proxy._handles) == before


# ---------------------------------------------------------------------------
# Quota class.
# ---------------------------------------------------------------------------

def test_upload_quota_exhaustion_surfaces_as_429(mirror_user, mock_upstream):
    _upstream_files_router(mock_upstream, create=(429, {
        "detail": {"code": "rate_limit_exceeded",
                   "message": "You've hit the file upload limit."}}))
    resp = mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD), "use_case": "multimodal"})
    assert resp.status_code == 429
    assert "file upload limit" in resp.text


# ---------------------------------------------------------------------------
# Protocol class.
# ---------------------------------------------------------------------------

def test_file_size_mismatch_is_passed_through_not_masked(mirror_user, mock_upstream):
    """Observed for real against upstream: the reason must survive the hop.

    If the gateway swallowed this into a generic 200/500, the UI would show a
    spinner forever instead of a fixable error.
    """
    _upstream_files_router(mock_upstream, uploaded=(400, {
        "detail": {"message": "file_size_mismatch"}}))
    resp = mirror_user.post("/backend-api/files/file-abc/uploaded", json={})
    assert resp.status_code == 400
    assert "file_size_mismatch" in resp.text


def test_non_json_create_response_does_not_crash_the_route(mirror_user, mock_upstream):
    """A malformed upstream body must degrade, not 500."""
    _upstream_files_router(mock_upstream, create=(502, "<html>bad gateway</html>"))
    resp = mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD), "use_case": "multimodal"})
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Proxy / transport class.
# ---------------------------------------------------------------------------

def test_upload_transport_failure_surfaces_as_502(mirror_user, mock_upstream, monkeypatch):
    """A dead egress proxy must produce 502, never a fake 201."""
    _upstream_files_router(mock_upstream)
    handle_path = urlsplit(mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD),
        "use_case": "multimodal"}).json()["upload_url"]).path

    discarded = {}

    class _ExplodingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def put(self, *args, **kwargs):
            raise ConnectionError("SSL_ERROR_SYSCALL")

        async def close(self):
            pass

        async def discard(self):
            discarded["yes"] = True

    from gateway import resource_proxy
    monkeypatch.setattr(resource_proxy, "Client", _ExplodingClient)
    resp = mirror_user.put(handle_path, content=PAYLOAD)
    assert resp.status_code == 502
    # A broken connection must not be returned to the pool.
    assert discarded.get("yes") is True
    # The failure message must not leak the signed URL.
    assert "sig=" not in resp.text


def test_upload_failure_does_not_leak_upstream_exception_text(mirror_user, mock_upstream,
                                                              monkeypatch):
    _upstream_files_router(mock_upstream)
    handle_path = urlsplit(mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD),
        "use_case": "multimodal"}).json()["upload_url"]).path

    class _ExplodingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def put(self, *args, **kwargs):
            raise RuntimeError("proxy http://user:password@10.0.0.1:7890 refused")

        async def close(self):
            pass

        async def discard(self):
            pass

    from gateway import resource_proxy
    monkeypatch.setattr(resource_proxy, "Client", _ExplodingClient)
    resp = mirror_user.put(handle_path, content=PAYLOAD)
    assert resp.status_code == 502
    assert "password" not in resp.text
    assert "10.0.0.1" not in resp.text


# ---------------------------------------------------------------------------
# Cancel / resume class: no duplicate task creation.
# ---------------------------------------------------------------------------

def test_reissuing_create_upload_does_not_reuse_a_stale_handle(mirror_user, mock_upstream):
    """Each create call is a distinct upload slot; handles must not collide."""
    _upstream_files_router(mock_upstream)
    body = {"file_name": "probe.txt", "file_size": len(PAYLOAD), "use_case": "multimodal"}
    first = mirror_user.post("/backend-api/files", json=body).json()["upload_url"]
    second = mirror_user.post("/backend-api/files", json=body).json()["upload_url"]
    assert first != second


def test_reloading_the_conversation_list_creates_no_upstream_conversation(mirror_user,
                                                                         mock_upstream):
    """Refresh must be a pure read: a resumed tool run must not start a second one."""
    _upstream_files_router(mock_upstream)
    globals.seed_map["seed-m4"]["conversations"] = ["conv-1"]
    globals.conversation_map["conv-1"] = {"id": "conv-1", "title": "T", "is_archived": False}
    for _ in range(3):
        assert mirror_user.get("/backend-api/conversations?limit=5&offset=0").status_code == 200
    posts = [r for r in mock_upstream.records
             if r["method"] == "POST" and r["path"].startswith("/backend-api/conversation")]
    assert posts == []


def test_redeeming_a_handle_twice_does_not_mint_a_second_slot(mirror_user, mock_upstream,
                                                             monkeypatch):
    """Retrying after a cancelled PUT must reuse the one slot, not register another."""
    _upstream_files_router(mock_upstream)
    from gateway import resource_proxy

    class _NoopClient:
        def __init__(self, *args, **kwargs):
            pass

        async def put(self, *args, **kwargs):
            class _R:
                status_code = 201
                headers = {}
                content = b""
            return _R()

        async def close(self):
            pass

        async def discard(self):
            pass

    monkeypatch.setattr(resource_proxy, "Client", _NoopClient)
    created = mirror_user.post("/backend-api/files", json={
        "file_name": "probe.txt", "file_size": len(PAYLOAD), "use_case": "multimodal"})
    handle_path = urlsplit(created.json()["upload_url"]).path
    before = set(resource_proxy._handles)
    assert mirror_user.put(handle_path, content=PAYLOAD).status_code == 201
    assert mirror_user.put(handle_path, content=PAYLOAD).status_code == 201
    assert set(resource_proxy._handles) == before
