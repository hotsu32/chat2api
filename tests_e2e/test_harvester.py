"""Stage 5 — harvester H1–H3 (admin-gated OAuth / cookie-import / RT lifecycle).

Admin auth uses the ``Authorization: Bearer <ADMIN_PASSWORD>`` header (conftest
pins ADMIN_PASSWORD=test-admin-password). All upstream network is pointed at the
loopback mock; the only hardcoded host (``chatgpt.com/api/auth/session`` inside
``fetch_session_access_token``) is stubbed at the ``Client`` seam for H2.
"""

import json
from urllib.parse import parse_qs, urlparse

import utils.configs as configs
import utils.globals as globals
from chatgpt import refreshToken
from utils import harvester_meta


def _admin():
    return {"Authorization": f"Bearer {configs.admin_password}"}


# ---------------------------------------------------------------------------
# H2 stub: replace the curl_cffi Client used by fetch_session_access_token with
# a canned-payload client (the hardcoded https://chatgpt.com host is unredirectable).
# ---------------------------------------------------------------------------

_SESSION_PAYLOAD = {}


class _StubResp:
    def __init__(self, payload, status=200):
        self.text = json.dumps(payload)
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.cookies = {}  # no NextAuth rotation


class _StubClient:
    def __init__(self, proxy=None, timeout=15, verify=True, impersonate=None):
        self.proxy = proxy

    async def get(self, url, headers=None, timeout=None):
        return _StubResp(dict(_SESSION_PAYLOAD))

    async def post(self, url, data=None, headers=None, timeout=None):
        return _StubResp({})

    async def close(self):
        pass


# ---------------------------------------------------------------------------
# H1 OAuth: authorize/start -> exchange -> rt into token_list + harvester_meta
# ---------------------------------------------------------------------------

def test_harvester_oauth_flow(client, mock_upstream, monkeypatch):
    monkeypatch.setattr(configs, "openai_auth_token_url", mock_upstream.url + "/oauth/token")

    resp = client.post(
        "/admin/harvester/authorize/start",
        headers=_admin(),
        json={"email": "alice@example.com", "note": "team-a"},
    )
    assert resp.status_code == 200
    start = resp.json()
    assert start["status"] == "success"
    session_id = start["session_id"]
    state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]

    callback_url = f"com.openai.chat://auth?code=FAKE_CODE&state={state}"
    resp = client.post(
        "/admin/harvester/authorize/exchange",
        headers=_admin(),
        json={"session_id": session_id, "callback_url": callback_url},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["email"] == "alice@example.com"

    rt = "rt_" + "a" * 60
    assert body["rt_prefix"] == rt[:12]
    assert rt in globals.token_list
    rec = harvester_meta.get("alice@example.com")
    assert rec["imported_token"] == rt
    assert rec["last_rt_prefix"] == rt[:12]


def test_harvester_oauth_state_mismatch(client, mock_upstream, monkeypatch):
    monkeypatch.setattr(configs, "openai_auth_token_url", mock_upstream.url + "/oauth/token")

    resp = client.post(
        "/admin/harvester/authorize/start",
        headers=_admin(),
        json={"email": "alice2@example.com"},
    )
    session_id = resp.json()["session_id"]

    resp = client.post(
        "/admin/harvester/authorize/exchange",
        headers=_admin(),
        json={
            "session_id": session_id,
            "callback_url": "com.openai.chat://auth?code=FAKE_CODE&state=WRONG",
        },
    )
    assert resp.status_code == 400
    # 一次性会话已被消费，不得留下任何 token
    assert all("rt_" not in t for t in globals.token_list)


def test_harvester_requires_admin_auth(client):
    resp = client.post(
        "/admin/harvester/authorize/start",
        json={"email": "intruder@example.com"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# H2 Cookie import: good cookie -> sess- key + refresh_map; bad -> error_token_list
# ---------------------------------------------------------------------------

def test_cookie_import_good(client, monkeypatch, make_access_token):
    monkeypatch.setattr("chatgpt.refreshToken.Client", _StubClient)
    at = make_access_token()
    _SESSION_PAYLOAD.clear()
    _SESSION_PAYLOAD["accessToken"] = at

    cookie_value = "eyJ" + "a" * 80
    resp = client.post(
        "/admin/harvester/import-cookie",
        headers=_admin(),
        json={"email": "bob@example.com", "session_token": cookie_value},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["token_type"] == "SessionToken"

    storage_key = "sess-" + cookie_value
    assert storage_key in globals.token_list
    assert globals.refresh_map[storage_key]["token"] == at


def test_cookie_import_bad_goes_error_list(client, monkeypatch):
    monkeypatch.setattr("chatgpt.refreshToken.Client", _StubClient)
    _SESSION_PAYLOAD.clear()  # 无 accessToken -> fetch_session_access_token 判死

    cookie_value = "eyJ" + "b" * 80
    resp = client.post(
        "/admin/harvester/import-cookie",
        headers=_admin(),
        json={"email": "bad@example.com", "session_token": cookie_value},
    )
    assert resp.status_code == 400
    assert "sess-" + cookie_value in globals.error_token_list
    assert "sess-" + cookie_value not in globals.token_list


# ---------------------------------------------------------------------------
# H3 RT lifecycle: refresh an imported rt_ -> refresh_map populated via /oauth/token
# ---------------------------------------------------------------------------

def test_rt_refresh_lifecycle(client, mock_upstream, monkeypatch):
    monkeypatch.setattr(refreshToken, "openai_auth_token_url", mock_upstream.url + "/oauth/token")

    rt = "rt_" + "c" * 60
    globals.token_list.append(rt)  # 模拟已导入的 RefreshToken

    resp = client.post(
        "/admin/routing/accounts/refresh",
        headers=_admin(),
        json={"token": rt},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    meta = globals.refresh_map.get(rt, {})
    assert meta.get("token")  # mock /oauth/token 返回了 access_token
    assert meta.get("last_success_at") > 0
