"""Two credential-bearing log paths on the request entrypoints.

Finding A - ``gateway/backend.py`` (POST /backend-api/conversation). The route
logged the pool account token and the bound egress proxy URL verbatim::

    logger.info(f"Request token: {req_token}")
    logger.info(f"Request proxy: {proxy_url}")

``req_token`` is the account credential (``resolve_seed_token`` -> pool account),
and ``proxy_url`` routinely carries ``user:pass@host`` -- the proxy is itself the
credential. Both are full values, not prefixes.

Finding B - ``chatgpt/refreshToken.py::chat_refresh``. The Refresh Token was
reduced to ``refresh_token[:8]`` and logged under that name -- a prefix is not
anonymization (it is stable and directly comparable across accounts, exactly
what ``anon_id``/``_anon`` exist to avoid). The same function pushed bounded
upstream response bodies into three surfaces: the exception message, the
persisted ``refresh_map[...]["last_error"]``, and the ``HTTPException.detail``
handed back to the caller.

Both paths land in the ring buffer (``utils/log_buffer``), which has no
redaction and is served to the admin log panel, so the values are operator
readable and downloadable, not merely transient stdout.

Every secret below is synthetic. The tests assert on the four surfaces a
credential can escape through -- caplog, the exception string, the HTTP
response detail, and the persisted refresh state -- plus a structural guard
that the backend route cannot silently go back to interpolating the raw names.
"""

import ast
import hashlib
import pathlib

import pytest
from fastapi import HTTPException

from chatgpt import refreshToken
from utils.antiban.guard import redact_proxy
import utils.globals as globals


# ---------------------------------------------------------------------------
# Synthetic credentials. Shapes are realistic; no real value is involved.
# ---------------------------------------------------------------------------

SYNTHETIC_RT = "rt_syntheticRefreshTokenMaterial0123456789abcdef"
SYNTHETIC_PROXY = "http://proxy-user-7d3:proxy-pass-9f1@egress-proxy.internal.test:8080"
SYNTHETIC_COOKIE = "syntheticNextAuthSessionCookieValueJWE"
BODY_SENTINEL = "UPSTREAM-BODY-SENTINEL-9f2c"
KEY_SENTINEL = "UPSTREAM-KEY-SENTINEL-a41d"
HOLDER_EMAIL = "holder@example.com"

# The full credential, every prefix short enough to be a "safe" log field, and
# the tail (a suffix is no better than a prefix).
SECRET_FRAGMENTS = (
    SYNTHETIC_RT,
    SYNTHETIC_RT[:8],
    SYNTHETIC_RT[:12],
    SYNTHETIC_RT[-8:],
)
PROXY_FRAGMENTS = (
    SYNTHETIC_PROXY,
    "proxy-user-7d3",
    "proxy-pass-9f1",
    "egress-proxy.internal.test",
)
BODY_FRAGMENTS = (BODY_SENTINEL, KEY_SENTINEL, HOLDER_EMAIL)


def anon_of(value: str) -> str:
    """The published anonymous-identifier contract: irreversible, 12 hex chars.

    Computed here independently of the implementation so a helper that quietly
    starts returning a prefix fails this file instead of satisfying it.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def assert_absent(text: str, fragments, surface: str) -> None:
    for fragment in fragments:
        assert fragment not in text, f"{surface} carried {fragment!r}"


def assert_surfaces_clean(surfaces, fragments, skip=()) -> None:
    """No fragment may appear on any surface outside ``skip``.

    ``skip`` exists for one narrow reason: ``refresh_map[...]["last_proxy"]`` is
    a write-only routing fact that predates this change and is read by nothing
    in the tree, so its contents are a separate decision from log/exception
    hygiene. Every *log* and *exception* surface is always checked.
    """
    for name, text in surfaces:
        if name in skip:
            continue
        assert_absent(text, fragments, name)


# ---------------------------------------------------------------------------
# chat_refresh harness
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, text, content_type="application/json"):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}


class _FakeClient:
    """Returns queued responses; records the request so routing stays pinned."""

    queue = []
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def post(self, url, **kwargs):
        _FakeClient.calls.append({"url": url, **kwargs})
        response = _FakeClient.queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self):
        pass


@pytest.fixture
def refresh_env(monkeypatch):
    """Isolate chat_refresh: synthetic proxy, no persistence, empty globals."""
    _FakeClient.queue = []
    _FakeClient.calls = []
    monkeypatch.setattr(refreshToken, "Client", _FakeClient)
    monkeypatch.setattr(refreshToken, "get_bound_proxy", lambda _token: None)
    monkeypatch.setattr(refreshToken, "proxy_url_list", [SYNTHETIC_PROXY])
    monkeypatch.setattr(refreshToken, "persist_refresh_map", lambda: None)
    monkeypatch.setattr(refreshToken, "persist_error_tokens", lambda: None)
    monkeypatch.setattr(globals, "refresh_map", {})
    monkeypatch.setattr(globals, "error_token_list", [])
    return _FakeClient


def failure_surfaces(caplog, error) -> tuple:
    """The four surfaces a refresh credential can escape through.

    The persisted surface is the refresh_map *entry*, not the map: the map is
    keyed by the token itself, which is the store's pre-existing index (storage
    design, not a log). What this change owns is the fields written into the
    entry -- ``last_error`` above all.
    """
    return (
        ("caplog", caplog.text),
        ("exception", str(error.value)),
        ("detail", str(error.value.detail)),
        ("refresh_map", str(globals.refresh_map.get(SYNTHETIC_RT, {}))),
    )


# ---------------------------------------------------------------------------
# B - Refresh Token prefix, upstream body, and proxy credentials
# ---------------------------------------------------------------------------

async def test_rejected_refresh_never_publishes_token_body_or_proxy(refresh_env, caplog):
    """A 403 ``invalid_grant`` is the most common real failure.

    Its body is the one the old code copied into the exception message, and the
    token prefix was logged on the same line.
    """
    refresh_env.queue.append(_FakeResponse(
        403,
        f'{{"error":"invalid_grant","detail":"{BODY_SENTINEL}","email":"{HOLDER_EMAIL}"}}',
    ))

    with pytest.raises(HTTPException) as error:
        await refreshToken.chat_refresh(SYNTHETIC_RT)

    assert error.value.status_code == 500
    surfaces = failure_surfaces(caplog, error)
    assert_surfaces_clean(surfaces, SECRET_FRAGMENTS)
    assert_surfaces_clean(surfaces, BODY_FRAGMENTS)
    assert_surfaces_clean(surfaces, PROXY_FRAGMENTS, skip=("refresh_map",))

    # The rejection is still classified (error_token_list bookkeeping and
    # operator diagnostics depend on it), just without echoing the body.
    assert "refresh_rejected" in caplog.text
    assert "status=403" in caplog.text
    assert "invalid_grant" in globals.refresh_map[SYNTHETIC_RT]["last_error"]


async def test_non_json_200_never_publishes_body(refresh_env, caplog):
    """A 200 carrying HTML must not carry that HTML into the failure path."""
    refresh_env.queue.append(_FakeResponse(
        200, f"<html><body>{BODY_SENTINEL}</body></html>", content_type="text/html"))

    with pytest.raises(HTTPException) as error:
        await refreshToken.chat_refresh(SYNTHETIC_RT)

    surfaces = failure_surfaces(caplog, error)
    assert_surfaces_clean(surfaces, SECRET_FRAGMENTS)
    assert_surfaces_clean(surfaces, BODY_FRAGMENTS)
    assert_surfaces_clean(surfaces, PROXY_FRAGMENTS, skip=("refresh_map",))

    # The bounded classification survives: a non-JSON 200 is still identifiable.
    assert "non_json" in caplog.text
    assert "text/html" in caplog.text


async def test_missing_access_token_never_publishes_payload_keys(refresh_env, caplog):
    """``keys=list(payload.keys())`` is body-derived text, not a bounded enum.

    Upstream keys are named here by the upstream; a sentinel key proves the
    payload shape is being echoed rather than classified.
    """
    refresh_env.queue.append(_FakeResponse(
        200, f'{{"{KEY_SENTINEL}":"x","user":{{"email":"{HOLDER_EMAIL}"}}}}'))

    with pytest.raises(HTTPException) as error:
        await refreshToken.chat_refresh(SYNTHETIC_RT)

    surfaces = failure_surfaces(caplog, error)
    assert_surfaces_clean(surfaces, SECRET_FRAGMENTS)
    assert_surfaces_clean(surfaces, BODY_FRAGMENTS)

    assert "missing_access_token" in caplog.text


async def test_library_exception_message_never_reaches_logs_or_state(refresh_env, caplog):
    """A transport error is text this process does not author.

    ``requests``/``curl_cffi`` proxy errors embed the proxy URL, which carries
    ``user:pass``. ``str(e)`` therefore cannot be forwarded to any surface --
    only the exception *class* may be.
    """
    refresh_env.queue.append(ConnectionError(
        f"HTTPSConnectionPool failed via {SYNTHETIC_PROXY}"))

    with pytest.raises(HTTPException) as error:
        await refreshToken.chat_refresh(SYNTHETIC_RT)

    assert error.value.status_code == 500
    surfaces = failure_surfaces(caplog, error)
    assert_surfaces_clean(surfaces, SECRET_FRAGMENTS)
    assert_surfaces_clean(surfaces, PROXY_FRAGMENTS, skip=("refresh_map",))
    assert_surfaces_clean(surfaces, BODY_FRAGMENTS)

    # The failure is still attributable: class name is a bounded, safe enum.
    assert "ConnectionError" in caplog.text
    assert "ConnectionError" in str(globals.refresh_map)


async def test_refresh_diagnostics_keep_anonymous_correlation(refresh_env, caplog):
    """Hardening must not cost the ability to correlate repeated failures.

    The digest is stable per token, so N failures by one account group together
    without the token ever being present.
    """
    for _ in range(2):
        refresh_env.queue.append(_FakeResponse(403, '{"error":"invalid_grant"}'))

    for _ in range(2):
        with pytest.raises(HTTPException):
            await refreshToken.chat_refresh(SYNTHETIC_RT)

    assert anon_of(SYNTHETIC_RT) in caplog.text
    assert_absent(caplog.text, SECRET_FRAGMENTS, "caplog")
    # No proxy was actually used (the template carried no credentials-free
    # placeholder), so the proxy fact stays a bounded yes/no.
    assert "proxy=" in caplog.text


async def test_successful_refresh_still_returns_token_and_sends_form_body(refresh_env):
    """Compatibility lock: the request the upstream sees is unchanged."""
    refresh_env.queue.append(_FakeResponse(200, '{"access_token":"synthetic-access"}'))

    assert await refreshToken.chat_refresh(SYNTHETIC_RT) == "synthetic-access"

    call = refresh_env.calls[0]
    assert call["url"] == refreshToken.openai_auth_token_url
    # The credential still goes *to the upstream* -- only the log/exception
    # surfaces were narrowed.
    assert SYNTHETIC_RT in call["data"]


# ---------------------------------------------------------------------------
# B (adjacent) - rotated-cookie parse failure logged an unbounded repr
# ---------------------------------------------------------------------------

class _ExplodingJar:
    """Fails both the dict-like read and the iterable fallback, as a malformed
    jar would; each failure carries the cookie value in its message."""

    def items(self):
        raise ValueError(f"malformed set-cookie {SYNTHETIC_COOKIE}")

    def __iter__(self):
        raise ValueError(f"malformed set-cookie {SYNTHETIC_COOKIE}")


class _CookieResponse:
    cookies = _ExplodingJar()


def test_rotated_cookie_parse_failure_never_logs_the_cookie(caplog):
    """Sibling of the sess2ac path, which already logs ``type(e).__name__`` only.

    This path handles cookie values and logged the exception ``repr``, so a jar
    that fails while holding a token would print it.
    """
    assert refreshToken._extract_rotated_cookie(_CookieResponse(), "") is None
    assert_absent(caplog.text, (SYNTHETIC_COOKIE,), "caplog")


# ---------------------------------------------------------------------------
# A - gateway/backend.py request-path diagnostics
# ---------------------------------------------------------------------------

def test_request_diagnostics_publish_only_anonymous_identifiers(caplog):
    """The route-level facts stay, the credentials do not.

    ``log_conversation_request_diagnostics`` is the single logging point for the
    conversation route, so exercising it covers every field the route emits.
    """
    from gateway.backend import log_conversation_request_diagnostics

    log_conversation_request_diagnostics(SYNTHETIC_RT, SYNTHETIC_PROXY, "safari15_3", "UA/1.0")

    assert_absent(caplog.text, SECRET_FRAGMENTS, "caplog")
    assert_absent(caplog.text, PROXY_FRAGMENTS, "caplog")

    # Correlation survives: the account digest and the shared proxy redaction.
    assert anon_of(SYNTHETIC_RT) in caplog.text
    assert redact_proxy(SYNTHETIC_PROXY) in caplog.text
    # Non-credential request facts are still recorded.
    assert "safari15_3" in caplog.text
    assert "UA/1.0" in caplog.text


def test_request_diagnostics_handle_an_absent_proxy(caplog):
    """No proxy is a normal state and must not turn into a None-logging path."""
    from gateway.backend import log_conversation_request_diagnostics

    log_conversation_request_diagnostics(SYNTHETIC_RT, None, "chrome124", "UA/1.0")

    assert redact_proxy(None) in caplog.text
    assert_absent(caplog.text, SECRET_FRAGMENTS, "caplog")


# ---------------------------------------------------------------------------
# A (adjacent) - library exception messages echoed the proxy credential
# ---------------------------------------------------------------------------

def test_transport_exception_message_is_not_logged(refresh_env, caplog):
    """The evidenced shape: curl echoes the proxy URL on a proxy misconfig.

    Measured in this environment against the real client:

        Client(proxy="http://proxy-user-7d3:proxy-pass-9f1@127.0.0.1:99999")
        -> ConnectionError("Failed to perform, curl: (5) Unsupported proxy
           syntax in 'http://proxy-user-7d3:proxy-pass-9f1@127.0.0.1:99999'")

    A bad port or scheme is an ordinary operator typo, so the credential lands
    in the log -- and in the admin log panel -- on a config error, not an
    attack. ``safe_exception_label`` keeps the class and drops the message.
    """
    from gateway.backend import safe_exception_label

    label = safe_exception_label(ConnectionError(
        f"Failed to perform, curl: (5) Unsupported proxy syntax in '{SYNTHETIC_PROXY}': "
        "Port number was not a decimal number"
    ))

    assert label == "ConnectionError"
    assert_absent(label, SECRET_FRAGMENTS, "label")
    assert_absent(label, PROXY_FRAGMENTS, "label")


def test_locally_authored_http_exception_keeps_its_classification():
    """Hardening must not flatten the two sentinel failures into one bucket.

    ``detail`` on this path is local literals ("Failed to get chat requirements"
    / "Failed to solve proof of work"), so it is safe to keep -- and it is the
    only thing that says *which* step failed.
    """
    from gateway.backend import safe_exception_label

    label = safe_exception_label(HTTPException(status_code=403, detail="Failed to solve proof of work"))

    assert "status=403" in label
    assert "Failed to solve proof of work" in label


def test_safe_exception_label_cannot_carry_an_upstream_body():
    """An exception shaped like a body-bearing failure still logs as class-only."""
    from gateway.backend import safe_exception_label

    label = safe_exception_label(ValueError(f"bad payload {BODY_SENTINEL}"))

    assert label == "ValueError"
    assert_absent(label, BODY_FRAGMENTS, "label")


# --- structural guard ------------------------------------------------------

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
# The two files that own the credential boundary. `seed` is included below: it
# is the mirror-side handle a pool account is bound to.
_GUARDED_SOURCES = (
    _PROJECT_ROOT / "gateway" / "backend.py",
    _PROJECT_ROOT / "chatgpt" / "refreshToken.py",
)

_CREDENTIAL_NAMES = {
    "req_token", "proxy_url", "access_token", "refresh_token", "session_token", "seed",
}
# Exception bindings. Their *message* is text this process does not author, so
# it may only appear as a class name (`type(e).__name__`) -- never as a value.
_EXCEPTION_NAMES = {"e", "exc", "err", "error", "exception"}
# Calls that turn such a value into a safe-to-log form.
_REDACTORS = {"_anon", "redact_proxy", "anon_id"}
_LOG_METHODS = {"info", "warning", "error", "debug", "critical", "exception"}


def _logger_calls(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS
                and isinstance(func.value, ast.Name) and func.value.id == "logger"):
            yield node


def _is_redacted(node) -> bool:
    """True when the expression wraps a call to an allowlisted redactor."""
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id in _REDACTORS
        for inner in ast.walk(node)
    )


def _published_values(value):
    """The expressions a slot actually prints.

    A ternary's *condition* is a presence test, not a published value:
    ``f"proxy={'yes' if proxy_url else 'no'}"`` prints a bounded enum and is a
    legitimate diagnostic. Only the branches can carry the value.
    """
    if isinstance(value, ast.IfExp):
        return (value.body, value.orelse)
    return (value,)


def _unbounded_exception_use(value):
    """The exception *value* being interpolated, if it is.

    Recognizes the two shapes that leaked: ``f"{e}"`` / ``f"{e!r}"`` (a bare
    Name) and ``f"{str(e)[:400]}"`` (a ``str()`` call, possibly subscripted).
    ``f"{type(e).__name__}"`` is deliberately not a match -- the class name is
    the safe form.
    """
    if isinstance(value, ast.Subscript):
        value = value.value
    if isinstance(value, ast.Name) and value.id in _EXCEPTION_NAMES:
        return value.id
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "str"):
        for argument in value.args:
            if isinstance(argument, ast.Name) and argument.id in _EXCEPTION_NAMES:
                return f"str({argument.id})"
    return None


def _unsafe_interpolations(tree):
    """Logger calls that publish a credential or an unvetted exception message.

    F-string slots are checked (``f"{req_token}"`` is a FormattedValue over a
    bare Name); ``f"{_anon(req_token)}"`` passes because the slot's value is a
    redactor Call, not the Name.
    """
    findings = []
    for call in _logger_calls(tree):
        for argument in call.args:
            if isinstance(argument, ast.Name) and argument.id in _CREDENTIAL_NAMES:
                findings.append((argument.lineno, argument.id))
                continue
            for inner in ast.walk(argument):
                if not isinstance(inner, ast.FormattedValue):
                    continue
                if _is_redacted(inner.value):
                    continue
                for published in _published_values(inner.value):
                    names = {n.id for n in ast.walk(published) if isinstance(n, ast.Name)}
                    credential_names = names & _CREDENTIAL_NAMES
                    if credential_names:
                        findings.append((inner.lineno, ",".join(sorted(credential_names))))
                    unflagged = _unbounded_exception_use(published)
                    if unflagged:
                        findings.append((inner.lineno, f"raw-exception:{unflagged}"))
    return findings


@pytest.mark.parametrize("source", _GUARDED_SOURCES, ids=lambda p: p.name)
def test_logger_never_interpolates_a_raw_credential_or_exception_message(source):
    """The regression guard for both findings.

    A behavior test cannot reach the backend route: it is registered under
    ``if no_sentinel:`` and every suite pins ``NO_SENTINEL=false``. The
    statement shape is therefore pinned instead -- it is what leaked, and it is
    what a future edit would reintroduce.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))

    findings = _unsafe_interpolations(tree)

    assert findings == [], (
        f"{source.name} logger calls interpolate raw credentials or exception "
        f"messages: {findings} (use _anon()/redact_proxy()/safe_exception_label())"
    )


# Every shape below is a line that actually existed in one of the two files (or
# the safe replacement for it), so the guard is pinned to the findings rather
# than to an abstract idea of "unsafe".
_GUARD_CASES = [
    # (source line, must_fire)
    ('logger.info(f"Request token: {req_token}")', True),
    ('logger.info(f"Request proxy: {proxy_url}")', True),
    ('logger.error(f"[chat_refresh] token={refresh_token[:8]}... failed")', True),
    ('logger.error(f"[sess2ac] failed: {str(e)[:400]}")', True),
    ('logger.error(f"Sentinel failed: {e}")', True),
    ('logger.warning(f"[rotated_cookie] parse cookies failed: {e!r}")', True),
    ('logger.info(f"Request account: {_anon(req_token)}")', False),
    ('logger.info(f"Request proxy: {redact_proxy(proxy_url)}")', False),
    ('logger.info(f"account={_anon(refresh_token)} failed={type(e).__name__}")', False),
    ('logger.info(f"proxy={\'yes\' if proxy_url else \'no\'}")', False),
    ('logger.info(f"body_len={len(raw_text)}")', False),
]


@pytest.mark.parametrize("line, must_fire", _GUARD_CASES)
def test_guard_detects_every_evidenced_leak_shape(line, must_fire):
    """A guard that cannot fire is not a guard."""
    tree = ast.parse(line + "\n")

    fired = bool(_unsafe_interpolations(tree))

    assert fired is must_fire, f"guard disagreed on: {line}"


def test_conversation_route_is_wired_to_the_anonymous_diagnostics():
    """Finding A's call site: the helper must actually be what the route calls."""
    source = _PROJECT_ROOT / "gateway" / "backend.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    route = next(
        (node for node in ast.walk(tree)
         if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat_conversations"),
        None,
    )
    assert route is not None, "chat_conversations route not found in gateway/backend.py"

    called = {
        node.func.id
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "log_conversation_request_diagnostics" in called
