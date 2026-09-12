"""Matrix runner: drive probes, apply the verdict rules, record cells.

Two responsibilities, deliberately kept apart from each other and from the
verdict logic:

  * enforce the generation budget BEFORE spending one, not after;
  * ingest probe evidence that already exists on disk, so re-running the report
    never re-runs the browser and never re-spends a generation.

`ingest` is the important half. Every live turn writes a JSON record; this reads
those records and fills the matrix from them. That means the matrix can always be
regenerated from evidence, and a cell can never acquire a status that has no file
behind it.

Usage:
  python -m scripts.mirror_acceptance.runner ingest
  python -m scripts.mirror_acceptance.runner run <tier> <form> <thread> <cache> [--fault=...]
  python -m scripts.mirror_acceptance.runner report
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from scripts.mirror_acceptance import matrix as M
from scripts.mirror_acceptance import verdicts

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVIDENCE = os.path.join(ROOT, "tmp/agent-team/m5-acceptance/evidence")
MATRIX_PATH = os.path.join(EVIDENCE, "matrix.json")
PROBE = os.path.join(ROOT, "scripts/mirror_acceptance/turn_probe.js")

ALIASES = {"free": "frontend-proof-free-1",
           "plus": "frontend-proof-plus-2",
           "pro": "frontend-proof-pro-4"}

# Conversations already produced by earlier runs, reused for `continue` and
# `warm` cells so those do not each cost a fresh generation.
CONV_INDEX = os.path.join(EVIDENCE, "_conv_ids.json")


def _load_record(label: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(EVIDENCE, f"turn-{label}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _label(tier: str, form: str, thread: str, cache: str, fault: str = "none") -> str:
    base = f"{tier}-{form}-{thread}-{cache}"
    return base if fault == "none" else f"{base}-{fault}"


def ingest(mx: M.Matrix) -> List[str]:
    """Fill every cell that has a probe record on disk. Idempotent."""
    touched: List[str] = []
    for cell in mx.cells:
        if cell.feature not in verdicts.VERDICTS:
            continue
        label = _label(cell.tier, cell.form, cell.thread, cell.cache, cell.fault)
        rec = _load_record(label)
        if rec is None:
            continue
        out = verdicts.evaluate(rec, cell.feature)
        # Only the first feature of a turn is charged for the generation, so the
        # three assertions drawn from one turn cannot inflate the budget count.
        charged = 1 if cell.feature == "chat" else 0
        mx.record(cell.key, out["status"], evidence=f"turn-{label}.json",
                  note=out.get("note"), generations=charged * rec.get("generationsUsed", 1),
                  metrics=out.get("metrics", {}))
        touched.append(cell.key)
    return touched


def run_turn(mx: M.Matrix, tier: str, form: str, thread: str, cache: str,
             fault: str = "none", dry_run: bool = False) -> int:
    """Spend one generation on a live turn, budget permitting."""
    if tier not in ALIASES:
        print(f"unknown tier {tier!r}", file=sys.stderr)
        return 2
    remaining = mx.budget_remaining()
    if remaining <= 0 and not dry_run:
        print(f"refusing to run: generation budget exhausted "
              f"({mx.generations_used()}/{M.TOTAL_GENERATION_BUDGET})", file=sys.stderr)
        return 3

    label = _label(tier, form, thread, cache, fault)
    if _load_record(label) is not None:
        print(f"evidence for {label} already exists; not re-spending a generation. "
              f"Run `ingest` to fold it in.")
        return 0

    cmd = ["node", PROBE, ALIASES[tier], label, form]
    if thread == "continue":
        conv = _pick_conversation(tier)
        if conv is None:
            print(f"no existing conversation for {tier}; cannot measure a continue cell "
                  "without first creating one", file=sys.stderr)
            return 4
        cmd.append(f"--continue={conv}")
    if fault == "cancel":
        cmd.append("--cancel-after-ms=1500")

    print(f"[{mx.generations_used()}/{M.TOTAL_GENERATION_BUDGET} used, "
          f"{remaining} left] {' '.join(cmd[:1] + cmd[2:])}")
    if dry_run:
        return 0
    return subprocess.call(cmd, cwd=ROOT)


def _pick_conversation(tier: str) -> Optional[str]:
    if not os.path.exists(CONV_INDEX):
        return None
    with open(CONV_INDEX, encoding="utf-8") as fh:
        index = json.load(fh)
    convs = index.get(ALIASES[tier]) or []
    return convs[0] if convs else None


def report(mx: M.Matrix) -> None:
    print(mx.markdown())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    sub.add_parser("report")
    r = sub.add_parser("run")
    r.add_argument("tier", choices=sorted(ALIASES))
    r.add_argument("form", choices=list(M.FORMS))
    r.add_argument("thread", choices=list(M.THREADS))
    r.add_argument("cache", choices=list(M.CACHES))
    r.add_argument("--fault", default="none", choices=list(M.FAULTS))
    r.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    mx = M.Matrix.load(MATRIX_PATH)

    if args.cmd == "ingest":
        touched = ingest(mx)
        mx.save(MATRIX_PATH)
        print(f"ingested {len(touched)} cells from evidence")
        report(mx)
        return 0
    if args.cmd == "report":
        ingest(mx)
        mx.save(MATRIX_PATH)
        report(mx)
        return 0

    code = run_turn(mx, args.tier, args.form, args.thread, args.cache,
                    args.fault, args.dry_run)
    if code == 0 and not args.dry_run:
        ingest(mx)
        mx.save(MATRIX_PATH)
        report(mx)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
