"""Check what credential material the mirror hands to the browser.

M5's checklist includes "Session/Refresh must not leak" and "static assets must
not carry credentials". This probe answers that by fetching the rendered page
over HTTP and classifying every credential-shaped string it finds -- comparing
*hashes*, never values, so the check can run and be reported without a token
ever reaching a log, a JSON file, or this process's stdout.

Classification matters more than detection here. The official ChatGPT frontend
legitimately needs *an* ``accessToken`` in its bootstrap to call ``/backend-api``
from the browser; the question is whether it is the pooled account's real
upstream token (a cross-user credential leak: any mirror user could lift it and
drive that ChatGPT account directly, outside the mirror) or a mirror-scoped
stand-in. A boolean "a JWT was present" cannot tell those apart, so we compare
against the account's actual token by digest.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")


def _digest(value: str) -> str:
    """Comparison handle for a secret. One-way, and truncated so that even the
    digest is useless to an attacker who somehow reads the evidence file."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:12]


def _jwt_claims(token: str) -> Dict[str, Any]:
    """Decode a JWT payload for *shape* inspection (issuer, audience, expiry).

    No signature check: we are not authenticating the token, only asking which
    system issued it. Returns {} on anything malformed.
    """
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
    except Exception:
        return {}


def classify_jwt(token: str, upstream_digests: Dict[str, str]) -> Dict[str, Any]:
    """Describe one JWT found in delivered content, without revealing it."""
    claims = _jwt_claims(token)
    digest = _digest(token)
    matched = [name for name, dig in upstream_digests.items() if dig == digest]
    return {
        "digest": digest,
        "length": len(token),
        "issuer": claims.get("iss"),
        "audience": claims.get("aud"),
        "has_openai_auth_claim": any(
            str(k).startswith("https://api.openai.com/") for k in claims
        ),
        "expires_at": claims.get("exp"),
        # The verdict that matters: does this equal a real pooled credential?
        "matches_upstream": matched or None,
        "is_upstream_account_credential": bool(matched),
    }


def scan_page(html: str, upstream_digests: Dict[str, str]) -> Dict[str, Any]:
    """Find and classify every JWT in a delivered HTML document."""
    tokens = set(JWT_RE.findall(html))
    # The bootstrap blob escapes `<` `>` `&` but not the token body, so a plain
    # regex over the raw HTML sees the same strings the browser's JSON.parse will.
    findings = [classify_jwt(t, upstream_digests) for t in tokens]
    return {
        "jwt_count": len(findings),
        "upstream_credential_exposed": any(f["is_upstream_account_credential"] for f in findings),
        "findings": findings,
    }


def _account_digests(seed: str) -> Dict[str, str]:
    """Digests of the credentials the pooled account actually holds.

    Read through the application loader and reduced to digests immediately --
    the raw values never leave this function.
    """
    import utils.globals as globals_mod
    import utils.store as store

    entry = globals_mod.seed_map.get(seed) or {}
    token = entry.get("token") or ""
    out: Dict[str, str] = {}
    if token:
        out["accounts.token"] = _digest(token)
    row = store.get_account(token) if token else None
    if row:
        refresh = row.get("refresh_info") or ""
        if refresh:
            out["accounts.refresh_info"] = _digest(refresh)
            # refresh_info is JSON in practice; digest the inner secrets too so a
            # nested refresh/session token appearing in the page is still caught.
            try:
                blob = json.loads(refresh)
                for key, value in (blob or {}).items():
                    if isinstance(value, str) and len(value) > 20:
                        out[f"refresh_info.{key}"] = _digest(value)
            except Exception:
                pass
    return out


def run(base_url: str, seed: str, runtime: str, paths: List[str]) -> Dict[str, Any]:
    from scripts.mirror_acceptance.alias_check import _load_runtime
    from scripts.mirror_acceptance.redact import anon_id, assert_clean

    _load_runtime(runtime)
    digests = _account_digests(seed)

    # curl_cffi is already a dependency (the project uses it for upstream calls),
    # so no new package is introduced for the probe's own HTTP.
    from curl_cffi import requests as cffi_requests

    session = cffi_requests.Session()
    results = []
    # Establish the token cookie exactly as the Dashboard entry does.
    session.get(f"{base_url}/?token={seed}", impersonate="chrome", timeout=60)

    for path in paths:
        # `/` redirects to the Dashboard without an explicit token, so the chat
        # page -- the one that carries the bootstrap blob -- is only reachable
        # via `?token=`. Templating the seed in keeps that out of the caller.
        path = path.replace("{seed}", seed)
        try:
            resp = session.get(f"{base_url}{path}", impersonate="chrome", timeout=60)
            body = resp.text or ""
            scan = scan_page(body, digests)
            results.append({
                "path": path.split("?")[0],   # never record the seed in evidence
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type", "").split(";")[0],
                "bytes": len(body),
                "cache_control": resp.headers.get("cache-control"),
                **scan,
            })
        except Exception as exc:
            results.append({"path": path.split("?")[0], "error": type(exc).__name__})

    summary = {
        "seed_ref": anon_id(seed, "seed"),
        "checked_paths": len(results),
        "upstream_credential_exposed_anywhere": any(
            r.get("upstream_credential_exposed") for r in results),
        "paths_exposing_upstream_credential": [
            r["path"] for r in results if r.get("upstream_credential_exposed")],
        # A static asset must never carry a credential, and must be cacheable
        # without becoming a cross-account leak -- both are checked per path.
        "results": results,
    }
    assert_clean(summary)
    return summary


DEFAULT_PATHS = ["/?token={seed}", "/backend-api/me", "/cdn/assets/manifest-bdb6d9e7.js"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5025")
    parser.add_argument("--seed", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--path", action="append", dest="paths")
    parser.add_argument("--out")
    args = parser.parse_args()

    summary = run(args.base_url, args.seed, os.path.abspath(args.runtime),
                  args.paths or DEFAULT_PATHS)
    blob = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(blob + "\n")
    print(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
