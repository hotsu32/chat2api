"""E2E harness for the gateway + /v1 OpenAI-compatible + antiban + harvester layers.

Runs the real FastAPI app (gateway routes included) against a loopback mock of
chatgpt.com, with zero real network and zero real credentials.

Import order is load-bearing, mirroring ``tests/conftest.py``:

1. pin ``sys.path`` and ``chdir`` into a throwaway dir (redirect ``data/``,
   ``version.txt`` and the cwd-relative Jinja ``templates/`` dir),
2. copy ``templates/`` into that dir (``app.py:34`` uses ``Jinja2Templates(directory="templates")``,
   and ``gateway/v1.py`` reads ``templates/initialize.json`` at import time),
3. pin every env var ``utils.configs`` reads (it reads env exactly once),
4. ``import app`` last so ``app.py:43`` registers the gateway route modules in the
   correct order (``backend.py`` catch-all last).

``ENABLE_GATEWAY=true`` is the one difference from ``tests/conftest.py``; the two
suites therefore cannot share a process and must be invoked separately
(``pytest tests/`` vs ``pytest tests_e2e/``).
"""

import base64
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Session-level isolation (runs at conftest import time).
# ---------------------------------------------------------------------------
_SESSION_TMP = tempfile.mkdtemp(prefix="chat2api-e2e-")
os.chdir(_SESSION_TMP)
os.makedirs(os.path.join(_SESSION_TMP, "data"), exist_ok=True)
with open(os.path.join(_SESSION_TMP, "version.txt"), "w", encoding="utf-8") as f:
    f.write("1.8.8-test\n")

_templates_src = _PROJECT_ROOT / "templates"
_templates_dst = Path(_SESSION_TMP) / "templates"
if _templates_src.is_dir():
    shutil.copytree(_templates_src, _templates_dst)

os.environ["FLEET_DB_PATH"] = os.path.join(_SESSION_TMP, "data", "chat2api.db")
os.environ["SESSION_DB_PATH"] = os.path.join(_SESSION_TMP, "data", "sessions.db")
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["ENABLE_GATEWAY"] = "true"
os.environ["ENABLE_ANTIBAN"] = "false"
os.environ["ENABLE_SESSION_STICKY"] = "false"
os.environ["SCHEDULED_REFRESH"] = "false"
os.environ["AUTO_SEED"] = "true"
os.environ["RANDOM_TOKEN"] = "false"
os.environ["CONVERSATION_ONLY"] = "true"
os.environ["NO_SENTINEL"] = "false"
os.environ["CHECK_MODEL"] = "false"
os.environ["CHATGPT_BASE_URL"] = ""
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_USER"] = ""
os.environ["SMTP_PASSWORD"] = ""
# The developer .env carries real SMTP / Turnstile / verification settings; unless
# they are pinned here, load_dotenv leaks them in and registration starts demanding
# a live captcha + a mailbox round-trip that the suite cannot satisfy.
os.environ["REQUIRE_EMAIL_VERIFICATION"] = "false"
os.environ["TURNSTILE_SITE_KEY"] = ""
os.environ["TURNSTILE_SECRET_KEY"] = ""
os.environ["REGISTER_RATE_LIMIT"] = "0"
# Checkout is fail-closed without a provider; the mock one keeps the purchase
# path exercisable without any real money movement.
os.environ["PAYMENT_PROVIDER"] = "mock"
os.environ["APP_ENV"] = "test"
# Synthetic binding capacity only; this is not a measured production limit.
os.environ["FLEET_MAX_SHARED_SEEDS_PER_ACCOUNT"] = "2"
os.environ["PROXY_URL"] = ""
os.environ["SENTINEL_PROXY_URL"] = ""
os.environ["OPENAI_AUTH_TOKEN_URL"] = "https://auth.example/oauth/token"
os.environ["OPENAI_AUTH_AUTHORIZE_URL"] = "https://auth.example/oauth/authorize"
os.environ["OPENAI_AUTH_REDIRECT_URI"] = "http://localhost:1455/auth/callback"
os.environ["OPENAI_AUTH_CLIENT_ID"] = "test-client-id"
os.environ["OPENAI_AUTH_SCOPE"] = "openid profile email offline_access"

# Imported only after the env above is pinned (utils.configs reads env at import).
import utils.configs as configs  # noqa: E402
import utils.globals as globals  # noqa: E402
import utils.ratelimit as ratelimit  # noqa: E402
import utils.store as store  # noqa: E402
import utils.usage as usage  # noqa: E402
from chatgpt.authorization import OPERATOR_SEED_STATUS  # noqa: E402
import app  # noqa: E402,F401  -- registers gateway routes (must be last)

from starlette.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Per-test reset: fresh SQLite + empty in-memory globals + empty usage buffer.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    db_file = tmp_path / "chat2api.db"
    monkeypatch.setattr(store, "_DB_PATH", str(db_file))
    monkeypatch.setattr(store, "_INITIALIZED", False)
    store.init_db()

    globals.token_list = []
    globals.error_token_list = []
    globals.refresh_map = {}
    globals.fp_map = {}
    globals.routing_config = {}
    globals.seed_map = {}
    globals.conversation_map = {}
    globals.antiban_bucket = {"buckets": {}, "account_index": {}}
    globals.antiban_geo_cache = {}
    globals.antiban_dead_tokens = {}
    globals.antiban_iprep_cache = {}
    globals.account_warnings = {}
    globals._plan_synced = set()
    globals.count = 0

    usage._pending = []

    # Rate-limit counters live in a module-level dict, not the DB, so the per-test
    # DB swap above does not clear them. Without this, the signin-throttle tests
    # would poison every later test that signs in.
    ratelimit.reset()

    # Route upstream to a blank list; the `client` fixture repoints it to the mock.
    configs.chatgpt_base_url_list[:] = []

    # Drop the model-slug cache (keyed by host_url, which changes per mock port).
    from chatgpt.ChatService import ChatService
    ChatService.available_model_cache = {}

    yield


# ---------------------------------------------------------------------------
# JWT / token builders (pure, no network). Reused from tests/conftest.py.
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
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
# Mock upstream: a recording chatgpt.com stand-in.
# ---------------------------------------------------------------------------

def _sse_body(reply: str) -> bytes:
    """Build a minimal upstream conversation SSE stream that yields ``reply``.

    Shape matches what ``head_process_response``/``stream_response`` expect:
    an empty ``in_progress`` frame (consumed by head_process_response to signal
    start), then a single ``finished_successfully`` + ``end_turn`` frame carrying
    the whole reply, then ``[DONE]``.
    """
    lines = [
        json.dumps({
            "message": {
                "id": "msg-1",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [""]},
                "status": "in_progress",
                "metadata": {},
            },
            "conversation_id": "conv-1",
        }),
        json.dumps({
            "message": {
                "id": "msg-1",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [reply]},
                "status": "finished_successfully",
                "end_turn": True,
                "metadata": {},
            },
            "conversation_id": "conv-1",
        }),
        "[DONE]",
    ]
    return ("data: " + "\n\ndata: ".join(lines) + "\n\n").encode("utf-8")


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _record(self, body=b""):
        self.server.records.append({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "cookie": self.headers.get("Cookie", ""),
            # Full header set: the gateway is responsible for deciding the upstream
            # identity (account id, origin, host, fingerprint), so tests must be able
            # to assert on what actually left the process.
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": body,
        })

    def _send(self, code, body, content_type="application/json", extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self):
        self._record()
        path = self.path.split("?")[0]
        if path == "/backend-api/models":
            self._json(200, {"models": [{"slug": "gpt-5-5"}, {"slug": "gpt-4o"}]})
        elif path == "/backend-api/me":
            self._json(200, {"email": "owner@example.com", "name": "Owner", "id": "u-1"})
        elif path == "/api/auth/session":
            self._json(200, {
                "user": {"email": "owner@example.com", "name": "Owner", "id": "u-1"},
                "accessToken": build_access_token(),
            })
        elif path == "/backend-api/conversations":
            self._json(200, {"items": [], "total": 0, "limit": 20, "offset": 0})
        elif path == "/backend-api/accounts/check/v4-2023-04-27":
            self._json(200, {
                "accounts": {
                    "default": {
                        "account": {
                            "account_id": "acc-real-1",
                            "account_user_id": "user-owner__acc-real-1",
                            "account_email": "owner@example.com",
                            "account_name": "Owner Real",
                            "email": "owner@example.com",
                            "name": "Owner Real",
                            "phone_number": "+15551234567",
                            "picture": "https://cdn.example.com/avatar.png",
                        }
                    }
                }
            })
        elif path == "/backend-api/settings":
            self._json(200, {
                "email": "owner@example.com",
                "name": "Owner Real",
                "phone_number": "+15551234567",
                "theme": "dark",
            })
        elif path.startswith("/backend-api/conversation/"):
            conversation_id = path.rsplit("/", 1)[-1]
            self._json(200, {
                "title": "My Chat",
                "conversation_id": conversation_id,
                "create_time": 1700000000,
                "is_archived": False,
            })
        else:
            self._json(200, {})

    def do_POST(self):
        body = self._read_body()
        self._record(body)
        path = self.path.split("?")[0]
        if path == "/backend-api/conversation":
            # curl_cffi transparently decompresses, so an upstream content-encoding
            # must never be forwarded verbatim to the browser.
            self._send(200, self.server.conversation_sse, "text/event-stream",
                       self.server.conversation_headers)
        elif path == "/backend-api/sentinel/chat-requirements":
            self._send(200, json.dumps({
                "persona": "chatgpt-paid",
                "arkose": {"required": False, "dx": None},
                "turnstile": {"required": False},
                "proofofwork": {"required": False, "difficulty": "000032", "seed": "seed", "type": "pow"},
                "token": "sentinel-token-123",
            }).encode("utf-8"), "application/json",
                {"Set-Cookie": "oai-sc=sentinel-cookie-value; Path=/"})
        elif path == "/oauth/token":
            self._json(200, {
                "refresh_token": "rt_" + "a" * 60,
                "access_token": build_access_token(account_id="acc-mock"),
                "token_type": "Bearer",
                "expires_in": 86400,
            })
        else:
            self._json(200, {})

    def do_PATCH(self):
        body = self._read_body()
        self._record(body)
        self._json(200, {})


@pytest.fixture
def mock_upstream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.records = []
    server.conversation_sse = _sse_body("Hello, world")
    server.conversation_headers = {}
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def client(mock_upstream):
    # Repoint the upstream base-URL list (mutable in place so ChatService's
    # imported reference sees it) at the mock, then build a bare TestClient.
    # NOTE: `app` is the module; the FastAPI instance is `app.app` (app.py:20).
    configs.chatgpt_base_url_list[:] = [mock_upstream.url]
    return TestClient(app.app)


@pytest.fixture
def client_factory(mock_upstream):
    """Build additional TestClients, each with its own cookie jar.

    Needed to model "two devices, one account": session revocation can only be
    observed when the stale cookie lives somewhere the acting request cannot touch.
    """
    configs.chatgpt_base_url_list[:] = [mock_upstream.url]

    def _make():
        return TestClient(app.app)

    return _make


# ---------------------------------------------------------------------------
# Seeding helpers: inject synthetic accounts + seed bindings for golden-path tests.
# ---------------------------------------------------------------------------

@pytest.fixture
def seed_account():
    """Return a helper: ``seed_account(token, plan_type, status)`` -> upserts an
    account row AND appends to ``globals.token_list`` (mirrors production import)."""
    def _seed(token, plan_type="plus", status="healthy", **extra):
        store.upsert_account(token, plan_type=plan_type, status=status, **extra)
        globals.token_list.append(token)
        return token
    return _seed


@pytest.fixture
def seed_user():
    """Return a helper: ``seed_user(seed, token, plan_type)`` -> writes a seed_map
    entry (sticky binding) + persists the user row.

    Operator-style Seeds (no ``user_auth`` row) are authorized by the explicit
    grant marker that only the authenticated ``POST /seedtoken`` import writes,
    so the helper stamps it too — see ``chatgpt/authorization.py``.
    """
    def _seed(seed, token, plan_type="plus", conversations=None):
        globals.seed_map[seed] = {
            "token": token,
            "plan_type": plan_type,
            "conversations": conversations or [],
        }
        globals.persist_seed_map()
        store.upsert_user(seed, status=OPERATOR_SEED_STATUS)
        return seed
    return _seed
