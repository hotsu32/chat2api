"""Fetch authenticated bootstrap with the bound account's own website session.

Dynamic state expires after 60 seconds, including build/permission evaluations.
Static assets remain shared. Raw credentials stay in server memory/local archives.
"""
import copy
import asyncio
import hashlib
import json
import math
import re
import time
from pathlib import Path

from curl_cffi import requests as cffi_requests
from starlette.concurrency import run_in_threadpool

from gateway.identity import decode_jwt_payload

SESSION_ARCHIVE_DIR = Path('data/private/account-sessions')
SESSION_COOKIE_FILE = 'data/session_cookie.txt'
TEMPLATE_TTL = 60
_FETCH_IMPERSONATE = 'chrome124'
_FETCH_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
             'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
_template_cache = {}
_static_shell_cache = {}
_inflight = {}
_BOOTSTRAP = re.compile(r'<script\b[^>]*\bid="client-bootstrap"[^>]*>(.*?)</script>', re.S)
_COOKIE_NAME = '__Secure-next-auth.session-token'
_BOOTSTRAP_MARKER = '__CHAT2API_DYNAMIC_BOOTSTRAP__'


class FrontendSessionError(RuntimeError):
    """Safe, credential-free error suitable for the mirror entry page."""


def _static_shell(html):
    """Return the build-specific HTML shell with account state removed."""
    return _BOOTSTRAP.sub(lambda m: m.group(0).replace(m.group(1), _BOOTSTRAP_MARKER, 1), html, count=1)


def _build_key(html):
    return hashlib.sha256(_static_shell(html).encode()).hexdigest()


def compose_frontend(context):
    """Compose a cached static shell with the verified account bootstrap."""
    html = context.get('html', '')
    shell = context.get('static_shell')
    if not shell:
        return html
    match = _BOOTSTRAP.search(html)
    if not match:
        return html
    bootstrap = match.group(1)
    return shell.replace(_BOOTSTRAP_MARKER, bootstrap, 1)


def _account_id(token):
    return decode_jwt_payload(token).get('https://api.openai.com/auth', {}).get('chatgpt_account_id')


def _access_token_current(token):
    expiry = decode_jwt_payload(token).get('exp')
    return (type(expiry) in (int, float) and math.isfinite(expiry)
            and expiry > time.time())


def _session_cookies(value):
    pieces = value.removeprefix('sess-').split('|||')
    if not value or any(not piece for piece in pieces):
        raise FrontendSessionError('Website session is missing')
    return {_COOKIE_NAME + ('.' + str(i) if len(pieces) > 1 else ''): piece
            for i, piece in enumerate(pieces)}


def _credentials(req_token, account_id):
    # A stored SessionToken is already the credential selected by pool routing.
    if req_token.startswith('sess-'):
        return _session_cookies(req_token)
    matches = []
    for path in sorted(SESSION_ARCHIVE_DIR.glob('*.json')):
        if path.name == 'index.json':
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            raise FrontendSessionError('Website session archive cannot be read') from None
        if record.get('account', {}).get('id') == account_id:
            matches.append(record.get('sessionToken', ''))
    if matches:
        if len(set(matches)) != 1:
            raise FrontendSessionError('Conflicting website sessions for this account')
        return _session_cookies(matches[0])
    # Legacy Free cookie is accepted only after upstream identity verification.
    # It is never injected directly into another account's backend requests.
    try:
        raw = Path(SESSION_COOKIE_FILE).read_text().strip()
    except OSError:
        raise FrontendSessionError('No website session configured for this account') from None
    cookies = dict(piece.strip().split('=', 1) for piece in raw.split(';') if '=' in piece)
    if not any(key == _COOKIE_NAME or key.startswith(_COOKIE_NAME + '.') for key in cookies):
        raise FrontendSessionError('Website session is missing')
    return cookies


def _fetch_official_html_sync(cookies, account_id, fingerprint, *, refresh=False):
    session = cffi_requests.Session(
        # Preserve the existing proven SSR transport, while keeping each
        # account's own proxy. Persisted pool fingerprints are not rewritten.
        impersonate=_FETCH_IMPERSONATE,
        proxy=fingerprint.get('proxy_url'),
    )
    try:
        for name, value in cookies.items():
            session.cookies.set(name, value, domain='chatgpt.com', path='/')
        auth_params = {'refresh': 'true'} if refresh else {}
        response = None
        for attempt in range(2):
            response = session.get('https://chatgpt.com/api/auth/session',
                                   params=auth_params,
                                   headers={'User-Agent': _FETCH_UA}, timeout=30)
            challenge = response.status_code == 403 and response.headers.get('cf-mitigated') == 'challenge'
            if not challenge or attempt:
                break
            time.sleep(0.2)
        if response.status_code != 200:
            raise FrontendSessionError('Website authentication failed')
        auth = response.json()
        if auth.get('error'):
            raise FrontendSessionError('Website session renewal failed')
        if _account_id(auth.get('accessToken', '')) != account_id or auth.get('account', {}).get('id') != account_id:
            raise FrontendSessionError('Website session does not match the bound account')
        if not _access_token_current(auth.get('accessToken', '')):
            raise FrontendSessionError('Website access token has expired or invalid expiry')
        response = session.get('https://chatgpt.com/',
                               headers={'ChatGPT-Account-Id': account_id, 'User-Agent': _FETCH_UA}, timeout=30)
        if response.status_code != 200:
            raise FrontendSessionError('Official frontend is unavailable')
        match = _BOOTSTRAP.search(response.text)
        bootstrap = json.loads(match.group(1)) if match else {}
        source = bootstrap.get('session') or {}
        if source.get('error'):
            raise FrontendSessionError('Website session renewal failed')
        if (bootstrap.get('authStatus') != 'logged_in'
                or source.get('account', {}).get('id') != account_id
                or _account_id(source.get('accessToken', '')) != account_id):
            raise FrontendSessionError('Official frontend is not authenticated for this account')
        if (source.get('user') or {}).get('id') != (auth.get('user') or {}).get('id'):
            raise FrontendSessionError('Official frontend user does not match the website session')
        if not _access_token_current(source.get('accessToken', '')):
            raise FrontendSessionError('Website access token has expired or invalid expiry')
        return {'html': response.text, 'session': source, 'cookies': dict(session.cookies.items())}
    except FrontendSessionError:
        raise
    except Exception:
        # HTTP library exceptions may contain proxies or cookies; never expose them.
        raise FrontendSessionError('Could not load this account website session') from None
    finally:
        session.close()


def invalidate_frontend_cache():
    _template_cache.clear()
    _static_shell_cache.clear()
    # Running HTTP calls may finish, but must no longer publish their results.
    _inflight.clear()


def get_cached_frontend(req_token, access_token):
    key = hashlib.sha256(req_token.encode()).hexdigest()
    entry = _template_cache.get(key)
    if (entry and entry['account_id'] == _account_id(access_token)
            and time.monotonic() - entry['at'] < TEMPLATE_TTL):
        return copy.deepcopy(entry)
    return None


def get_session_cookie(req_token, access_token):
    """Only return cookies of an already verified, current account context."""
    entry = get_cached_frontend(req_token, access_token)
    return entry['cookies'] if entry else {}


async def refresh_cached_frontend(req_token, access_token, fingerprint, *, refresh=False):
    """Revalidate expired browser contexts; standalone bearer API clients stay standalone."""
    key = hashlib.sha256(req_token.encode()).hexdigest()
    if refresh or key in _template_cache:
        await get_frontend_template(req_token, access_token, fingerprint, refresh=refresh)
    return get_cached_frontend(req_token, access_token)


async def verified_access_token(req_token):
    """Reuse the page's account authentication in every chat/metadata path."""
    key = hashlib.sha256(req_token.encode()).hexdigest()
    entry = _template_cache.get(key)
    if entry is None:
        return None
    access_token = entry['session']['accessToken']
    entry = await refresh_cached_frontend(
        req_token, access_token, entry['fingerprint'],
        refresh=not _access_token_current(access_token))
    if entry is None:
        raise FrontendSessionError('Website session needs revalidation')
    access_token = entry['session']['accessToken']
    if not _access_token_current(access_token):
        raise FrontendSessionError('Website access token has expired')
    return access_token


async def get_frontend_template(req_token, access_token, fingerprint, *, refresh=False):
    account_id = _account_id(access_token)
    if not account_id:
        raise FrontendSessionError('Bound account identity is unavailable')
    key = hashlib.sha256(req_token.encode()).hexdigest()
    try:
        cookies = _credentials(req_token, account_id)
    except FrontendSessionError:
        if key in _template_cache:
            _template_cache[key]['at'] = float('-inf')
        raise
    context_hash = hashlib.sha256(json.dumps(
        [cookies, fingerprint, account_id], sort_keys=True).encode()).hexdigest()
    flight_key = (asyncio.get_running_loop(), key, context_hash, refresh)
    # Ordinary reads join a running renewal; they must not supersede it with
    # an exchange that is allowed to return the old access token.
    renewal = _inflight.get((*flight_key[:3], True))
    if renewal is not None:
        return await asyncio.shield(renewal)
    cached = get_cached_frontend(req_token, access_token)
    if not refresh and cached and cached['context_hash'] == context_hash:
        return cached['html']
    previous_context = _template_cache.get(key)
    if previous_context and previous_context['context_hash'] == context_hash:
        # Keep the verified website's device and rotated session across TTL
        # refreshes. A changed credential, workspace or proxy starts a new jar.
        cookies = dict(previous_context['cookies'])
    task = _inflight.get(flight_key)
    if task is None:
        for previous in list(_inflight):
            if previous[:2] == flight_key[:2]:
                _inflight.pop(previous)
        async def load():
            if key in _template_cache:
                # Failed revalidation must not restore stale browser state.
                _template_cache[key]['at'] = float('-inf')
            options = {'refresh': True} if refresh else {}
            result = await run_in_threadpool(
                _fetch_official_html_sync, cookies, account_id, fingerprint, **options)
            if _inflight.get(flight_key) is not asyncio.current_task():
                raise FrontendSessionError('Website context changed during authentication')
            build_key = _build_key(result['html'])
            static_shell = _static_shell_cache.setdefault(build_key, _static_shell(result['html']))
            result.update(account_id=account_id, at=time.monotonic(), context_hash=context_hash,
                          revision=hashlib.sha256(result['html'].encode()).hexdigest(),
                          fingerprint=dict(fingerprint), build_key=build_key,
                          static_shell=static_shell)
            if len(_template_cache) >= 128:
                oldest = min(_template_cache, key=lambda k: _template_cache[k]['at'])
                _template_cache.pop(oldest)
            _template_cache[key] = result
            return result['html']

        task = asyncio.create_task(load())
        _inflight[flight_key] = task
        def finished(done):
            if _inflight.get(flight_key) is done:
                _inflight.pop(flight_key, None)
            if not done.cancelled():
                done.exception()  # also observe failure if every waiter disconnected
        task.add_done_callback(finished)
    return await asyncio.shield(task)
