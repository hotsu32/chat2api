"""M5 acceptance matrix: the grid of user paths, and what is actually measured.

The matrix is declared here as data rather than being implied by whichever
probes happened to run. That inversion is the point: a cell that nobody measured
must show up as ``NOT_MEASURED`` in the report, instead of silently not
appearing. The failure mode this guards against is a report that lists four
green rows and omits the twenty that were never attempted.

Dimensions (from the approved plan's M5 section):
  tier      free / plus / pro
  form      desktop / mobile
  thread    new / continue          (new conversation vs.续聊)
  cache     cold / warm             (first load vs. repeat load)
  feature   chat / buttons / deep_research / web_search / file_analysis / image_gen
  fault     none / switch_account / 403 / 429 / proxy_fail / stream_break /
            reload / cancel

Status vocabulary is deliberately narrow, and none of the values mean "probably
fine":
  NOT_MEASURED  no evidence exists for this cell
  PASS          measured, met its assertion
  FAIL          measured, did not meet its assertion
  BLOCKED       attempted, stopped by an external dependency (Cloudflare, quota,
                account permission) -- not a product verdict either way
  UNMEASURABLE  the probe lacks an instrument for this cell (e.g. no selector)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

TIERS = ("free", "plus", "pro")
FORMS = ("desktop", "mobile")
THREADS = ("new", "continue")
CACHES = ("cold", "warm")
FEATURES = ("chat", "buttons", "delivery", "deep_research", "web_search",
            "file_analysis", "image_gen")
FAULTS = ("none", "switch_account", "http_403", "http_429", "proxy_fail",
          "stream_break", "reload", "cancel")

STATUSES = ("NOT_MEASURED", "PASS", "FAIL", "BLOCKED", "UNMEASURABLE")

# Generation budget. The approved plan caps total real generations at 24 across
# all of M2-M5; the matrix runner refuses to start a cell that would exceed the
# remaining allowance rather than discovering it afterwards.
TOTAL_GENERATION_BUDGET = 24


@dataclass
class Cell:
    tier: str
    form: str
    thread: str
    cache: str
    feature: str
    fault: str = "none"
    status: str = "NOT_MEASURED"
    evidence: Optional[str] = None      # path to the probe JSON, relative to evidence/
    note: Optional[str] = None
    generations: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.tier}/{self.form}/{self.thread}/{self.cache}/{self.feature}/{self.fault}"

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r} for {self.key}")


def default_matrix() -> List[Cell]:
    """Every cell the plan asks for, all starting at NOT_MEASURED.

    Not the full cartesian product: fault injection is only meaningful on the
    chat and buttons paths (a 429 during deep research is a deep-research cell
    in its own right, declared explicitly), and tools are measured on the
    desktop/new/cold path only, because the plan asks for one real completion per
    tool rather than a sweep.
    """
    cells: List[Cell] = []

    # Core chat + buttons + delivery: the full tier x form x thread x cache grid.
    # All three come from one generation -- they are three assertions about the
    # same turn, not three turns -- so the extra rows cost nothing and stop a
    # streaming pass from covering for a turn that showed the user nothing.
    for tier in TIERS:
        for form in FORMS:
            for thread in THREADS:
                for cache in CACHES:
                    for feature in ("chat", "buttons", "delivery"):
                        cells.append(Cell(tier, form, thread, cache, feature))

    # Fault injection: desktop, both threads, cold cache only. Repeating every
    # fault across mobile and warm cache would multiply the generation cost
    # without testing a different code path -- the fault is server-side.
    for tier in TIERS:
        for fault in FAULTS:
            if fault == "none":
                continue
            cells.append(Cell(tier, "desktop", "new", "cold", "chat", fault))

    # Tools: one real completion per tool per tier that plausibly has access.
    # Free is included on purpose -- "no permission, stated clearly" is a result
    # the plan asks for, not a cell to skip.
    for tier in TIERS:
        for feature in ("deep_research", "web_search", "file_analysis", "image_gen"):
            cells.append(Cell(tier, "desktop", "new", "cold", feature))
    # Refresh-recovery for the long-running tools, which is where resume matters.
    for tier in TIERS:
        for feature in ("deep_research", "image_gen"):
            cells.append(Cell(tier, "desktop", "continue", "warm", feature, "reload"))

    return cells


class Matrix:
    """Load / update / summarise the matrix, persisted as one JSON document."""

    def __init__(self, cells: Optional[List[Cell]] = None) -> None:
        self.cells: List[Cell] = cells if cells is not None else default_matrix()

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "Matrix":
        if not os.path.exists(path):
            return cls()
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls([Cell(**c) for c in raw["cells"]])

    def save(self, path: str) -> None:
        from scripts.mirror_acceptance.redact import assert_clean
        payload = {"summary": self.summary(), "cells": [asdict(c) for c in self.cells]}
        assert_clean(payload)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    # -- mutation ------------------------------------------------------------

    def get(self, key: str) -> Optional[Cell]:
        for cell in self.cells:
            if cell.key == key:
                return cell
        return None

    def record(self, key: str, status: str, evidence: Optional[str] = None,
               note: Optional[str] = None, generations: int = 0,
               metrics: Optional[Dict[str, Any]] = None) -> Cell:
        cell = self.get(key)
        if cell is None:
            raise KeyError(f"no such matrix cell: {key}")
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        cell.status = status
        cell.evidence = evidence
        cell.note = note
        cell.generations = generations
        cell.metrics = metrics or {}
        return cell

    # -- reporting -----------------------------------------------------------

    def generations_used(self) -> int:
        return sum(c.generations for c in self.cells)

    def budget_remaining(self) -> int:
        return TOTAL_GENERATION_BUDGET - self.generations_used()

    def summary(self) -> Dict[str, Any]:
        counts = {s: 0 for s in STATUSES}
        for cell in self.cells:
            counts[cell.status] += 1
        return {
            "total_cells": len(self.cells),
            "by_status": counts,
            "coverage_pct": round(
                100.0 * (len(self.cells) - counts["NOT_MEASURED"]) / max(1, len(self.cells)), 1),
            "generations_used": self.generations_used(),
            "generation_budget": TOTAL_GENERATION_BUDGET,
            # Stated explicitly so no reader can mistake partial coverage for a pass.
            "verdict": "INCOMPLETE" if counts["NOT_MEASURED"] else (
                "FAIL" if counts["FAIL"] else "SEE_CELLS"),
        }

    def markdown(self) -> str:
        """A review-navigable table: grouped by feature, NOT_MEASURED included."""
        icon = {"PASS": "PASS", "FAIL": "FAIL", "BLOCKED": "BLOCKED",
                "NOT_MEASURED": "-- not measured --", "UNMEASURABLE": "unmeasurable"}
        lines = ["| tier | form | thread | cache | feature | fault | status | evidence | note |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for cell in sorted(self.cells, key=lambda c: (c.feature, c.tier, c.form, c.thread, c.cache, c.fault)):
            lines.append(
                f"| {cell.tier} | {cell.form} | {cell.thread} | {cell.cache} | {cell.feature} "
                f"| {cell.fault} | {icon[cell.status]} | {cell.evidence or '-'} | {cell.note or '-'} |"
            )
        s = self.summary()
        lines.append("")
        lines.append(f"coverage {s['coverage_pct']}% | " + " | ".join(
            f"{k}={v}" for k, v in s["by_status"].items() if v))
        lines.append(f"generations {s['generations_used']}/{s['generation_budget']} | "
                     f"verdict {s['verdict']}")
        return "\n".join(lines)
