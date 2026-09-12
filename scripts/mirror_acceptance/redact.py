"""Redaction primitives shared by every M5 probe.

The acceptance harness reads real account/user rows through the application
loader, but nothing that identifies an account may reach disk or stdout. These
helpers collapse a row to a stable anonymous ordinal plus the few non-sensitive
fields the matrix actually needs (tier, status, consistency booleans).

A stable ordinal matters: the same account must get the same label across runs
so a report can say "acct#3 was reused by two seeds" without ever naming it.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

# Keys that must never be serialised, logged, or printed. Every name here is
# unambiguously credential- or identity-bearing, because ``assert_clean`` runs
# over *all* evidence payloads and a name that is merely ambiguous would reject
# legitimate output. ``accounts.note`` is deliberately absent for that reason:
# "note" is also an ordinary field on a matrix cell, and the real defence
# against the account column leaking is ``safe_account``'s allowlist, which
# emits only the four fields it names.
SENSITIVE_COLUMNS = frozenset({
    "token", "refresh_info", "fingerprint", "real_email", "nickname",
    "proxy_url", "password_hash", "seed", "current_account", "session", "cookie",
})

# Fields safe to keep verbatim: they describe capability, not identity.
_SAFE_ACCOUNT_FIELDS = ("plan_type", "status", "group_name", "token_type")


def anon_id(value: Optional[str], prefix: str = "acct") -> str:
    """Stable, non-reversible label for a credential-bearing identifier.

    Truncated SHA-256 rather than an incrementing counter so the label survives
    across processes and across DB row reordering. 10 hex chars is far too short
    to brute-force a token but wide enough that collisions are not a practical
    concern at our pool size (hundreds of accounts).
    """
    if not value:
        return f"{prefix}#none"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}#{digest}"


def safe_account(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce an ``accounts`` row to its publishable projection."""
    if not row:
        return {"account": anon_id(None), "present": False}
    out: Dict[str, Any] = {
        "account": anon_id(row.get("token")),
        "present": True,
    }
    for field in _SAFE_ACCOUNT_FIELDS:
        if row.get(field) is not None:
            out[field] = row[field]
    return out


def safe_user(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce a ``users`` row to its publishable projection."""
    if not row:
        return {"user": anon_id(None, "user"), "present": False}
    return {
        "user": anon_id(row.get("seed"), "user"),
        "present": True,
        "plan_type": row.get("plan_type"),
        "status": row.get("status"),
        "bound_account": anon_id(row.get("current_account")),
    }


def assert_clean(payload: Any, _path: str = "$") -> None:
    """Raise if a sensitive key survived into a payload about to be written.

    Called on every evidence blob before it touches disk. Cheap insurance: the
    probes build their dicts by hand, and a copy-paste that carries ``token``
    through would otherwise only be caught by a human reading the JSON.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in SENSITIVE_COLUMNS:
                raise AssertionError(f"sensitive key {key!r} at {_path}")
            assert_clean(value, f"{_path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            assert_clean(value, f"{_path}[{index}]")
