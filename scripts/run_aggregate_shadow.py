# -*- coding: utf-8 -*-
"""
Run the Phase 1 aggregate shadow comparison and write a report.

    PYTHONPATH=. python scripts/run_aggregate_shadow.py

OFFLINE / DIAGNOSTIC. Reads the live database, runs both engines, writes a
JSON report and prints a summary. Changes nothing: no production dispatch,
no writes, no schema changes. Safe to run against a live instance — every
query it issues is a read, and the legacy calls go through the same
`run_aggregate()` a normal request would use.

The old engine is not the oracle; see `src/pipeline/aggregate/shadow.py`'s
module docstring for why, and for how disagreements are classified.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline.aggregate import shadow  # noqa: E402
from src.pipeline.aggregate.executor import execute  # noqa: E402
from src.pipeline.aggregate.registry import build_registry  # noqa: E402

logging.basicConfig(level=logging.WARNING)

REPORT_PATH = Path("docs/aggregate-shadow/phase1_shadow_report.json")


async def main() -> int:
    print("Building registry from live data ...")
    t0 = time.perf_counter()
    snapshot = await build_registry()
    registry_ms = (time.perf_counter() - t0) * 1000.0
    print(
        f"  {len(snapshot.entities)} labels, {len(snapshot.relationships)} "
        f"relationship triples, {len(snapshot.logical_fields)} logical fields "
        f"({registry_ms:.0f} ms)\n"
    )

    report: dict = {
        "registry_as_of": snapshot.as_of,
        "registry_build_ms": round(registry_ms, 1),
        "comparisons": [],
        "adversarial": [],
    }

    # ── Old vs new, per GENERIC kind ──────────────────────────────────
    print("=" * 78)
    print("OLD vs NEW — generic aggregate kinds")
    print("=" * 78)
    specs = shadow.build_generic_specs()
    for kind, (old_query, spec) in specs.items():
        cmp = await shadow.compare_kind(snapshot, kind, old_query, spec)
        report["comparisons"].append(cmp.to_row())
        old_s = "None" if cmp.old_result is None else str(cmp.old_result)
        new_s = "refused" if cmp.new_result is None else str(cmp.new_result)
        print(f"\n{kind}")
        print(f"  Q:   {cmp.question}")
        print(f"  OLD: {old_s:>10}   ({cmp.old_ms:.0f} ms)" if cmp.old_ms else f"  OLD: {old_s}")
        print(
            f"  NEW: {new_s:>10}   "
            f"(compile {cmp.new_compile_ms:.0f} ms, exec {cmp.new_exec_ms:.0f} ms)"
            if cmp.new_exec_ms is not None
            else f"  NEW: {new_s}"
        )
        print(f"  ==>  {cmp.classification}")
        print(f"       {cmp.explanation}")

    # ── Adversarial / safety cases (new engine only) ───────────────────
    print("\n" + "=" * 78)
    print("ADVERSARIAL — safety properties (new engine)")
    print("=" * 78)
    for name, spec in shadow.build_adversarial_specs().items():
        started = time.perf_counter()
        outcome = await execute(snapshot, spec)
        ms = (time.perf_counter() - started) * 1000.0
        row = {
            "name": name,
            "question": spec.question_text,
            "status": outcome.status,
            "value": outcome.value,
            "refusal_code": outcome.receipt.refusal_code,
            "refusal_reason": outcome.receipt.refusal_reason,
            "grain": spec.grain,
            "injected": list(outcome.receipt.injected_predicates),
            "coverage_verdict": outcome.receipt.coverage_verdict,
            "caveat": outcome.receipt.caveat,
            "ms": round(ms, 1),
        }
        report["adversarial"].append(row)
        if outcome.ok:
            val = (
                f"{outcome.value:.1f}%"
                if isinstance(outcome.value, float)
                else str(outcome.value)
            )
            extra = f"  [{outcome.receipt.coverage_verdict}]" if outcome.receipt.coverage_verdict else ""
            print(f"\n{name}\n  -> {val}{extra}  ({ms:.0f} ms)")
            if outcome.receipt.caveat:
                print(f"     caveat: {outcome.receipt.caveat}")
        else:
            print(f"\n{name}\n  -> {outcome.status.upper()}: {outcome.receipt.refusal_code}")
            print(f"     {(outcome.receipt.refusal_reason or '')[:150]}")

    # ── Summary ───────────────────────────────────────────────────────
    counts: dict[str, int] = {}
    for row in report["comparisons"]:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1
    report["summary"] = counts

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for k, v in sorted(counts.items()):
        print(f"  {k:28s} {v}")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nReport written to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
