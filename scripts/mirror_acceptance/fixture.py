"""Bind acceptance aliases to accounts of the tier their name claims.

Why this exists: ``_resolve_seed_account`` is deliberately *sticky* -- once a
seed holds a usable account it is reused regardless of tier. That is correct
product behaviour, but it means an alias that was first bound while the Pro pool
was unreachable stays on a Plus account forever, and every "Pro" measurement
taken through it is silently a Plus measurement. Rebinding has to happen at the
fixture layer, not by changing the routing rule.

Safety: refuses to run against any database outside the ``--runtime`` directory
passed in, so it cannot touch production. It also refuses to bind two aliases to
the same account, because a shared binding makes account-isolation assertions
unfalsifiable -- which is exactly the hole that hid the Pro problem.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List

from scripts.mirror_acceptance.alias_check import (
    DEFAULT_ALIASES,
    _load_runtime,
    expected_tier,
    inspect_alias,
)
from scripts.mirror_acceptance.redact import anon_id, assert_clean


def _rebind(alias: str, tier: str, taken: set) -> Dict[str, Any]:
    """Point one alias at an unused healthy account of ``tier``."""
    import utils.globals as globals_mod
    import utils.store as store

    candidates = [
        row for row in store.get_account_by_plan(tier, status="healthy")
        if row.get("token") and row["token"] not in taken
    ]
    if not candidates:
        return {
            "alias": alias,
            "action": "skipped",
            "reason": "no_unused_healthy_account_for_tier",
            "tier": tier,
        }

    # Deterministic pick (first by rowid) so reruns land on the same account and
    # a matrix result stays comparable across runs.
    token = candidates[0]["token"]
    entry = globals_mod.seed_map.get(alias)
    previous = entry.get("token", "") if isinstance(entry, dict) else ""
    if isinstance(entry, dict):
        entry["token"] = token
        entry["plan_type"] = tier
        entry.setdefault("conversations", [])
    else:
        globals_mod.seed_map[alias] = {"token": token, "plan_type": tier, "conversations": []}
    globals_mod.persist_seed_map()
    taken.add(token)
    return {
        "alias": alias,
        "action": "rebound",
        "tier": tier,
        "from_account": anon_id(previous),
        "to_account": anon_id(token),
    }


def run(aliases: List[str], runtime: str) -> Dict[str, Any]:
    _load_runtime(runtime)

    before = [inspect_alias(a) for a in aliases]
    # Seed the "taken" set with bindings that are already correct so a healthy
    # alias is never moved off the account its existing conversations live on.
    taken = set()
    import utils.globals as globals_mod
    for record, alias in zip(before, aliases):
        if record["verdict"] == "consistent":
            entry = globals_mod.seed_map.get(alias)
            if isinstance(entry, dict) and entry.get("token"):
                taken.add(entry["token"])

    actions = []
    for record, alias in zip(before, aliases):
        if record["verdict"] == "consistent":
            actions.append({"alias": alias, "action": "kept", "tier": record["actual_account_plan_type"]})
            continue
        tier = expected_tier(alias)
        if not tier:
            actions.append({"alias": alias, "action": "skipped", "reason": "alias_name_claims_no_tier"})
            continue
        actions.append(_rebind(alias, tier, taken))

    after = [inspect_alias(a) for a in aliases]
    # Distinct-binding check: two aliases on one account makes cross-account
    # isolation tests pass vacuously, so surface it as a first-class field.
    bound = [r["bound_account"]["account"] for r in after if r["bound_account"].get("present")]
    summary = {
        "actions": actions,
        "after": [
            {
                "alias": r["alias"],
                "claims": r["alias_claims_tier"],
                "actual": r["actual_account_plan_type"],
                "verdict": r["verdict"],
                "account": r["bound_account"]["account"],
            }
            for r in after
        ],
        "all_consistent": all(r["verdict"] == "consistent" for r in after),
        "bindings_distinct": len(set(bound)) == len(bound),
    }
    assert_clean(summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--alias", action="append", dest="aliases")
    parser.add_argument("--out")
    args = parser.parse_args()

    runtime = os.path.abspath(args.runtime)
    # Guard rail: the runtime must be a private acceptance copy, never the repo's
    # own data/ directory. Checked before any import binds the DB path.
    if os.path.basename(runtime) != "runtime" or "m5-acceptance" not in runtime:
        raise SystemExit(f"refusing to mutate a runtime outside m5-acceptance: {runtime}")

    summary = run(args.aliases or DEFAULT_ALIASES, runtime)
    blob = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(blob + "\n")
    print(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
