"""Same-origin proxy for upstream-minted resource URLs (M4 tool assets).

Why this exists
---------------
Tool flows hand the browser URLs that live on hosts the mirror does **not**
otherwise rewrite.  The concrete case that motivated this module is file upload:
``POST /backend-api/files`` answers with

    upload_url = https://sdmntpr<region>.oaiusercontent.com/files/<uuid>/raw?<SAS>

an Azure Blob URL carrying a write-capable SAS signature.  ``reverseProxy``'s
rewrite table only covers ``files.oaiusercontent.com``, so this host reaches the
browser verbatim and the upload PUT leaves the user's machine **directly**,
bypassing the gateway and the account's egress proxy entirely.  Everything else
in this project is proxied on purpose (``FILE_HOST`` / ``VOICE_HOST`` exist for
exactly this reason), so a mirror user on a network that cannot reach
``*.oaiusercontent.com`` gets a working chat and a silently broken upload — and
the download direction is already same-origin, making the asymmetry the bug.

Design
------
Registering a URL returns an opaque handle; the browser only ever sees
``/backend-api/resource/upload/<handle>``.  The signed URL never leaves the
process, which also stops a write-capable SAS token from being handed to a
mirror user.

The handle registry is the SSRF guard: a handle can only be minted from a URL
that an upstream response actually gave us, it is bound to the seed that created
it, it expires, and the proxy route will only send to a registered URL on a
host that passes :func:`is_proxyable_asset_host`.  A user-supplied URL can never
reach this proxy.
"""
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import Request, HTTPException
from fastapi.responses import Response

from app import app
from chatgpt.fp import get_fp
from gateway.reverseProxy import resolve_seed_token
from utils.Client import Client
from utils.Logger import logger

# Upstream mints upload targets on per-region Azure Blob hosts under
# oaiusercontent.com (sdmntpr*, plus the stable files./videos. names).  Anything
# outside this suffix set is refused rather than proxied.
_ALLOWED_ASSET_SUFFIXES = (".oaiusercontent.com",)

# A SAS upload URL is short-lived; the handle must not outlive it.
_HANDLE_TTL = 3600.0
_MAX_HANDLES = 4096

# Request headers meaningful to Azure Blob that must survive the hop.  Anything
# else (cookies, our own authorization, mirror-user headers) is deliberately
# dropped: the SAS signature is self-sufficient and extra credentials make the
# upstream reject the PUT.
_FORWARD_REQUEST_HEADERS = (
    "content-type",
    "x-ms-blob-type",
    "x-ms-blob-content-type",
    "x-ms-version",
)
_FORWARD_RESPONSE_HEADERS = ("content-type", "etag", "x-ms-request-id", "x-ms-version")

_handles = {}
_lock = threading.Lock()


def is_proxyable_asset_host(url: str) -> bool:
    """True when *url* is an https URL on an upstream asset host we may proxy to."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return any(host == suffix.lstrip(".") or host.endswith(suffix)
               for suffix in _ALLOWED_ASSET_SUFFIXES)


def _purge_locked(now):
    for handle, entry in list(_handles.items()):
        if now - entry["created_at"] > _HANDLE_TTL:
            del _handles[handle]
    if len(_handles) > _MAX_HANDLES:
        for handle, _ in sorted(_handles.items(), key=lambda kv: kv[1]["created_at"])[
                :len(_handles) - _MAX_HANDLES]:
            _handles.pop(handle, None)


def register_upload_url(url: str, seed_token: str, req_token: str):
    """Mint an opaque handle for an upstream upload URL, or None if not proxyable."""
    if not is_proxyable_asset_host(url):
        return None
    handle = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _handles[handle] = {"url": url, "seed": seed_token,
                            "req_token": req_token, "created_at": now}
        # Purge after inserting, so the cap bounds the registry including the new
        # entry rather than leaving it at _MAX_HANDLES + 1.
        _purge_locked(now)
    return handle


def _resolve(handle: str, seed_token: str):
    now = time.time()
    with _lock:
        entry = _handles.get(handle)
        if entry is None:
            return None, "unknown"
        if now - entry["created_at"] > _HANDLE_TTL:
            del _handles[handle]
            return None, "expired"
    # Cross-seed reuse of someone else's upload slot is an isolation breach, not a 404.
    if entry["seed"] != seed_token:
        return None, "forbidden"
    return entry, None


@app.put("/backend-api/resource/upload/{handle}")
async def proxy_resource_upload(request: Request, handle: str):
    """Stream a browser PUT to the upstream-minted upload URL behind *handle*."""
    seed_token = resolve_seed_token(request)
    entry, problem = _resolve(handle, seed_token)
    if problem == "forbidden":
        raise HTTPException(status_code=403, detail="Upload handle belongs to another session")
    if problem == "expired":
        raise HTTPException(status_code=410, detail="Upload handle expired")
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown upload handle")

    # Re-check at send time: the allowlist is what makes a registered handle safe
    # to dereference, so it is enforced on the way out too, not only at mint time.
    url = entry["url"]
    if not is_proxyable_asset_host(url):
        raise HTTPException(status_code=502, detail="Upload target is not an allowed asset host")

    headers = {key: value for key, value in request.headers.items()
               if key.lower() in _FORWARD_REQUEST_HEADERS}
    headers.setdefault("x-ms-blob-type", "BlockBlob")

    # Use the account's own egress (proxy + impersonation), the same path every
    # other upstream call takes; that is the whole point of proxying the upload.
    fp = get_fp(entry["req_token"]).copy()
    proxy_url = fp.pop("proxy_url", None)
    impersonate = fp.pop("impersonate", "safari15_3")
    client = Client(proxy=proxy_url, impersonate=impersonate, timeout=120)
    try:
        body = await request.body()
        r = await client.put(url, headers=headers, data=body, timeout=120)
    except Exception as e:
        logger.error(f"[resource_proxy] upload failed: {type(e).__name__}")
        await client.discard()
        raise HTTPException(status_code=502, detail="Upload to storage failed") from None
    out = {key: value for key, value in r.headers.items()
           if key.lower() in _FORWARD_RESPONSE_HEADERS}
    logger.info(f"[resource_proxy] upload status={r.status_code} bytes={len(body)}")
    # Not a streamed request, so the body is already buffered; acontent() would
    # assert "stream mode is not enabled".
    content = r.content
    await client.close()
    return Response(content=content, status_code=r.status_code, headers=out)
