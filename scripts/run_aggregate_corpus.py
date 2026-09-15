# -*- coding: utf-8 -*-
"""
Run the Phase 2 aggregate evaluation corpus against live data.

    PYTHONPATH=. python scripts/run_aggregate_corpus.py

DIAGNOSTIC ONLY. Reads the live database; changes nothing. Production
routing, `xagg.py` dispatch and the RAG path are untouched.

WHAT IT MEASURES. Each corpus case carries an expected outcome and, where a
value is expected, a `GroundTruth` established by a route that does not
reuse this package's compiler (direct SQL, a differently-shaped hand-written
Cypher traversal, or Python over raw rows). This runner executes the case
through the real pipeline — validate -> compile -> execute -> shape-check ->
coverage -> receipt — and compares against that independent figure.

Classification is deterministic; no LLM adjudicates anything. A case whose
expected value could not be independently established is reported
UNRESOLVED rather than scored, per the Phase 2 brief.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline.aggregate.corpus import Case, build_corpus, coverage_summary  # noqa: E402
from src.pipeline.aggregate.executor import execute  # noqa: E402
from src.pipeline.aggregate.registry import build_registry  # noqa: E402
from src.pipeline.aggregate.validator import validate  # noqa: E402

logging.basicConfig(level=logging.ERROR)

REPORT_PATH = Path("docs/aggregate-shadow/phase2_corpus_report.json")

# Classifications. `NEW_REFUSED_CORRECTLY` / `NEW_REFUSED_INCORRECTLY` are
# the two the brief adds for this phase, and they are the reason a refusal
# is scored rather than skipped: refusing for the wrong reason is its own
# defect, and refusing a case that should compute is a capability gap.
PASS = "PASS"
WRONG_VALUE = "WRONG_VALUE"
REFUSED_CORRECTLY = "NEW_REFUSED_CORRECTLY"
REFUSED_INCORRECTLY = "NEW_REFUSED_INCORRECTLY"
REFUSED_WRONG_REASON = "REFUSED_WRONG_REASON"
UNEXPECTED_VALUE = "UNEXPECTED_VALUE"
UNRESOLVED = "UNRESOLVED"
ERROR = "ERROR"


def _close(a, b, tol: float = 1e-6) -> bool:
    """Numeric comparison that does not pretend floats are integers."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))
    return a == b


def classify(case: Case, outcome) -> tuple[str, str]:
    """Score one case against its own expectation. Deterministic."""
    if outcome.status == "failed":
        return ERROR, f"execution failed: {outcome.receipt.refusal_reason}"

    refused = outcome.status == "refused"
    code = outcome.receipt.refusal_code

    if case.outcome == "refused":
        if not refused:
            return (
                REFUSED_INCORRECTLY,
                f"expected refusal {case.refusal_code!r} but the engine "
                f"returned {outcome.value!r} — a guard did not fire",
            )
        if case.refusal_code and code != case.refusal_code:
            return (
                REFUSED_WRONG_REASON,
                f"refused with {code!r}, expected {case.refusal_code!r}; the "
                f"guard fired for a different reason than the case tests",
            )
        return REFUSED_CORRECTLY, f"refused with {code!r}, as specified"

    if case.outcome == "unresolved":
        return UNRESOLVED, "no independent ground truth established"

    # Expected a value.
    if refused:
        return (
            REFUSED_INCORRECTLY,
            f"refused with {code!r} but an independently-established value "
            f"({case.truth.value if case.truth else '?'}) exists — capability gap",
        )
    if case.truth is None:
        return UNRESOLVED, "value returned but no independent ground truth to check it against"

    got = outcome.value
    # Grouped results are scored on group COUNT; the corpus records the
    # number of groups plus a spot-checked top group in the note.
    if isinstance(got, list):
        got = len(got)

    if _close(got, case.truth.value):
        return PASS, f"{got} matches {case.truth.route} ground truth"
    return (
        WRONG_VALUE,
        f"engine returned {got!r}, {case.truth.route} ground truth is "
        f"{case.truth.value!r} ({case.truth.note})",
    )


async def main() -> int:
    print("Building registry from live data ...")
    t0 = time.perf_counter()
    snapshot = await build_registry()
    print(
        f"  {len(snapshot.entities)} labels, {len(snapshot.relationships)} "
        f"relationships ({(time.perf_counter() - t0) * 1000:.0f} ms)\n"
    )

    cases = build_corpus()
    report: dict = {
        "registry_as_of": snapshot.as_of,
        "case_count": len(cases),
        "coverage": coverage_summary(cases),
        "cases": [],
    }

    counts: dict[str, int] = {}
    print("=" * 78)
    print(f"PHASE 2 CORPUS — {len(cases)} cases")
    print("=" * 78)

    for case in cases:
        started = time.perf_counter()
        try:
            outcome = await execute(snapshot, case.spec)
            verdict, why = classify(case, outcome)
        except Exception as exc:  # noqa: BLE001 - a crash is a result
            outcome = None
            verdict, why = ERROR, f"{type(exc).__name__}: {exc}"
        ms = (time.perf_counter() - started) * 1000

        counts[verdict] = counts.get(verdict, 0) + 1
        row = {
            "name": case.name,
            "description": case.description,
            "expected_outcome": case.outcome,
            "expected_value": case.truth.value if case.truth else None,
            "ground_truth_route": case.truth.route if case.truth else None,
            "ground_truth_note": case.truth.note if case.truth else None,
            "expected_refusal_code": case.refusal_code,
            "verdict": verdict,
            "explanation": why,
            "ms": round(ms, 1),
            "axes": {
                "measure": case.measure_axis,
                "population": case.population_axis,
                "grouping": case.grouping_axis,
                "derived": case.derived_axis,
                "guard": case.guard_axis,
            },
        }
        if outcome is not None:
            row["status"] = outcome.status
            row["value"] = outcome.value
            row["refusal_code"] = outcome.receipt.refusal_code
            row["coverage_verdict"] = outcome.receipt.coverage_verdict
            row["caveat"] = outcome.receipt.caveat
            row["injected"] = list(outcome.receipt.injected_predicates)
            row["compiled"] = [q.text for q in outcome.receipt.queries]
        report["cases"].append(row)

        mark = {
            PASS: "OK  ", REFUSED_CORRECTLY: "OK  ",
        }.get(verdict, "FAIL")
        print(f"\n[{mark}] {case.name}  ({ms:.0f} ms)")
        print(f"       {case.description}")
        print(f"       {verdict}: {why}")

    report["summary"] = counts

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for k, v in sorted(counts.items()):
        print(f"  {k:26s} {v}")

    good = counts.get(PASS, 0) + counts.get(REFUSED_CORRECTLY, 0)
    print(f"\n  proven (value verified or guard fired correctly): {good}/{len(cases)}")

    print("\nCoverage by axis:")
    for axis, vals in report["coverage"].items():
        print(f"  {axis:12s} " + ", ".join(f"{k}={v}" for k, v in vals.items()))

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nReport written to {REPORT_PATH}")

    bad = len(cases) - good - counts.get(UNRESOLVED, 0)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
