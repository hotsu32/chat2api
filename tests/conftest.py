"""Test harness — isolate the app from the real ``data/`` directory before any import.

Import order matters: pytest imports this conftest before the test modules, and the
chat2api package resolves ``data/``, ``version.txt`` and ``.env`` relative to the
current working directory. We chdir into a throwaway dir first so the account domain
(SQLite) and the orthogonal JSON subsystems never read or write production secrets,
then pin env vars before ``utils.configs`` is first imported.
"""

import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

# Put the project root on sys.path so the namespace packages (utils / gateway /
# chatgpt) import regardless of whether pytest is invoked as `python -m pytest`
# or via the `pytest` console script. Must run before any project import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Session-level isolation (runs at conftest import time).
# ---------------------------------------------------------------------------
_SESSION_TMP = tempfile.mkdtemp(prefix="chat2api-test-")
os.chdir(_SESSION_TMP)
os.makedirs(os.path.join(_SESSION_TMP, "data"), exist_ok=True)
with open(os.path.join(_SESSION_TMP, "version.txt"), "w", encoding="utf-8") as f:
    f.write("1.8.8-test\n")

os.environ["FLEET_DB_PATH"] = os.path.join(_SESSION_TMP, "data", "chat2api.db")
os.environ["SESSION_DB_PATH"] = os.path.join(_SESSION_TMP, "data", "sessions.db")
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["ENABLE_GATEWAY"] = "false"
os.environ["ENABLE_ANTIBAN"] = "false"
os.environ["ENABLE_SESSION_STICKY"] = "false"
os.environ["SCHEDULED_REFRESH"] = "false"
os.environ["AUTO_SEED"] = "true"
os.environ["RANDOM_TOKEN"] = "false"

# Imported only after the env above is pinned (utils.configs reads env at import).
import utils.store as store  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh, empty SQLite database for one test (isolated from the session DB)."""
    db_file = tmp_path / "chat2api.db"
    monkeypatch.setattr(store, "_DB_PATH", str(db_file))
    monkeypatch.setattr(store, "_INITIALIZED", False)
    store.init_db()
    return store


# ---------------------------------------------------------------------------
# JWT / token builders (pure, no network).
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_jwt(claims: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    seg = lambda d: _b64url(json.dumps(d, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{seg(header)}.{seg(claims)}.testsig"


def build_access_token(plan_type="plus", email="owner@example.com", name="Owner Name",
                       account_id="acc-123", user_id="u-123", sub="auth0|abc123",
                       iat=1700000000, exp=2000000000):
    claims = {
        "sub": sub,
        "iat": iat,
        "exp": exp,
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan_type,
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
            "amr": ["pwd"],
        },
        "https://api.openai.com/profile": {"email": email, "name": name},
        "https://api.openai.com/mfa": {"required": "no"},
    }
    return build_jwt(claims)


@pytest.fixture
def make_jwt():
    return build_jwt


@pytest.fixture
def make_access_token():
    return build_access_token


# ---------------------------------------------------------------------------
# Mock upstream (chatgpt.com stand-in) for E2E golden-path tests.
# Point CHATGPT_BASE_URL / chatgpt_base_url at the returned URL to route the
# gateway through this canned server instead of the real chatgpt.com.
# ---------------------------------------------------------------------------

class _MockUpstreamHandler(http.server.BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/backend-api/me"):
            self._json(200, {"email": "owner@example.com", "name": "Owner", "id": "u-1"})
        elif self.path.startswith("/api/auth/session"):
            self._json(200, {"user": {"email": "owner@example.com", "name": "Owner"}})
        elif self.path.startswith("/backend-api/sentinel/chat-requirements"):
            self._json(200, {"token": "req-token", "arkose": None, "turnstile": None, "proofofwork": None})
        else:
            self._json(200, {})

    def do_POST(self):
        if self.path.startswith("/backend-api/conversation"):
            self._json(200, {"conversation_id": "conv-mock", "message": {"content": "mock reply"}})
        else:
            self._json(200, {})

    def log_message(self, *args):  # silence request logs
        pass


@pytest.fixture
def mock_upstream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
