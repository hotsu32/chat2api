"""Verify what tier an isolated-runtime alias *actually* resolves to.

Background: the acceptance aliases are named ``frontend-proof-free-1`` ...
``frontend-proof-pro-4``, and a previous run reported the "pro" alias serving a
Plus account. A name is not evidence, so this probe walks the real resolution
chain the gateway walks -- seed -> user_auth tier -> tier_account_plan_types ->
accounts.plan_type -- and reports where the chain diverges from the alias label.

It is strictly read-only: it calls the store's getters and the tier resolver,
never the seed-map writers, so running it cannot rebind an alias to a new
account. Output is passed through ``redact`` and carries only anonymous ordinals,
tier strings, and booleans.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from scripts.mirror_acceptance.redact import anon_id, assert_clean, safe_account, safe_user

ALIAS_PREFIX = "frontend-proof-"
DEFAULT_ALIASES = [
    ALIAS_PREFIX + "free-1",
    ALIAS_PREFIX + "plus-2",
    ALIAS_PREFIX + "plus-3",
    ALIAS_PREFIX + "pro-4",
]

# The tier the alias *name* promises. Used only to compute the mismatch flag --
# never to assert the alias is correct.
_EXPECTED_FROM_NAME = {"free": "free", "plus": "plus", "pro": "pro"}


def expected_tier(alias: str) -> Optional[str]:
    """The tier implied by the alias name, or None if the name says nothing."""
    tail = alias[len(ALIAS_PREFIX):] if alias.startswith(ALIAS_PREFIX) else alias
    head = tail.split("-", 1)[0]
    return _EXPECTED_FROM_NAME.get(head)


def inspect_alias(alias: str) -> Dict[str, Any]:
    """Resolve one alias through the live loader and report the chain."""
    import utils.globals as globals_mod
    import utils.store as store

    record: Dict[str, Any] = {
        "alias": alias,
        "alias_claims_tier": expected_tier(alias),
        "seed_ref": anon_id(alias, "seed"),
    }

    # Layer 1: the users table (seed -> current_account).
    user_row = store.get_user(alias)
    record["user_row"] = safe_user(user_row)

    # Layer 2: the in-memory seed_map the request path actually reads first.
    entry = globals_mod.seed_map.get(alias)
    if isinstance(entry, dict):
        record["seed_map"] = {
            "present": True,
            "bound_account": anon_id(entry.get("token")),
            "plan_type": entry.get("plan_type"),
            "conversation_count": len(entry.get("conversations") or []),
        }
    else:
        record["seed_map"] = {"present": False}

    # Layer 3: user_auth -> tier -> permitted account.plan_type set. This is the
    # half-dedicated pooling rule; if there is no user_auth row it returns None
    # and the pool falls back to legacy behaviour, which is exactly how a "pro"
    # alias can legitimately end up holding a Plus account.
    tier_id = None
    permitted: Optional[List[str]] = None
    tier_error = None
    try:
        from utils.tiers import resolve_user_tier, tier_account_plan_types
        tier_id = resolve_user_tier(alias)
        permitted = tier_account_plan_types(tier_id) if tier_id else None
    except Exception as exc:  # pragma: no cover - defensive, reported not raised
        tier_error = type(exc).__name__
    record["tier_resolution"] = {
        "user_tier": tier_id,
        "permitted_account_plan_types": list(permitted) if permitted else None,
        "error": tier_error,
    }

    # Layer 4: the account row the binding currently points at.
    bound_token = ""
    if isinstance(entry, dict) and entry.get("token"):
        bound_token = entry["token"]
    elif user_row and user_row.get("current_account"):
        bound_token = user_row["current_account"]
    account_row = store.get_account(bound_token) if bound_token else None
    record["bound_account"] = safe_account(account_row)

    # Verdict. Note the deliberate three-way split: a mismatch we can see is very
    # different from "we could not resolve anything", and neither is a pass.
    actual = (account_row or {}).get("plan_type")
    claims = record["alias_claims_tier"]
    if not bound_token or account_row is None:
        verdict = "unresolved"
    elif claims is None:
        verdict = "no_claim"
    elif actual == claims:
        verdict = "consistent"
    else:
        verdict = "mismatch"
    record["actual_account_plan_type"] = actual
    record["verdict"] = verdict
    record["usable_for_tier_evidence"] = verdict == "consistent"
    return record


def _load_runtime(runtime: str) -> None:
    """Point the application loader at the isolated runtime, then import it.

    ``utils.store`` snapshots the DB path at import time from ``configs``, so the
    environment has to be set before the first import -- doing it after would
    silently read the production database.
    """
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault("ENABLE_GATEWAY", "true")
    os.environ.setdefault("ENABLE_ANTIBAN", "false")
    os.environ.setdefault("SCHEDULED_REFRESH", "false")
    os.environ.setdefault("INIT_APPLY_ON_EMPTY", "false")
    os.environ.setdefault("INIT_FORCE", "false")
    os.environ["FLEET_DB_PATH"] = os.path.join(runtime, "data", "chat2api.db")
    os.environ["SESSION_DB_PATH"] = os.path.join(runtime, "data", "sessions.db")
    os.chdir(runtime)

    import utils.globals as globals_mod  # noqa: F401  (import triggers the load)


def run(aliases: List[str], runtime: str) -> Dict[str, Any]:
    _load_runtime(runtime)
    import utils.store as store

    results = [inspect_alias(a) for a in aliases]
    summary = {
        "runtime_db": "<isolated>",
        "alias_count": len(results),
        "consistent": sum(1 for r in results if r["verdict"] == "consistent"),
        "mismatch": sum(1 for r in results if r["verdict"] == "mismatch"),
        "unresolved": sum(1 for r in results if r["verdict"] == "unresolved"),
        # Pool depth explains a mismatch: a "pro" alias cannot hold a Pro account
        # if the isolated DB has no healthy Pro accounts at all.
        "healthy_pool_depth": {
            tier: len(store.get_account_by_plan(tier, status="healthy"))
            for tier in ("free", "plus", "pro")
        },
        "aliases": results,
    }
    assert_clean(summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, help="isolated runtime directory")
    parser.add_argument("--alias", action="append", dest="aliases")
    parser.add_argument("--out", help="write redacted JSON here")
    args = parser.parse_args()

    summary = run(args.aliases or DEFAULT_ALIASES, os.path.abspath(args.runtime))
    blob = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(blob + "\n")
    print(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
