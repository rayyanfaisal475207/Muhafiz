# -*- coding: utf-8 -*-
"""
Phase 4F — evaluate the three-way aggregate architecture.

    PYTHONPATH=. python scripts/run_aggregate_three_way.py
    PYTHONPATH=. python scripts/run_aggregate_three_way.py --limit 8
    PYTHONPATH=. python scripts/run_aggregate_three_way.py --no-semantic

DIAGNOSTIC ONLY. Reads the live database and calls a model for the AGE
route. Changes nothing: no production routing, no writes, no schema
changes.

WHAT IT MEASURES. Each corpus case is run through every applicable route,
the results are reconciled deterministically, and BOTH are then compared
against the case's independently-established ground truth — the figure from
direct SQL, a differently-shaped hand-written traversal, or Python over raw
rows. That last step is what keeps this an evaluation rather than a
consistency check: neither route is graded against the other.

THE HARNESS DOES NOT HELP EITHER ROUTE. The AGE route is given the question
and a schema card, never the spec, the expected answer, or what the other
routes produced. If it gets a case wrong, that is recorded as wrong — a
disagreement the architecture surfaced is a successful evaluation, not a
failure to be tuned away.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline.aggregate import reconcile as rec  # noqa: E402
from src.pipeline.aggregate import route_age, route_semantic, route_structured  # noqa: E402
from src.pipeline.aggregate import routes  # noqa: E402
from src.pipeline.aggregate.corpus import build_corpus  # noqa: E402
from src.pipeline.aggregate.registry import build_registry  # noqa: E402
from src.pipeline.aggregate.routes import AggregateRouteRequest  # noqa: E402

logging.basicConfig(level=logging.ERROR)

REPORT_PATH = Path("docs/aggregate-shadow/phase4_three_way_report.json")

# ── Evaluation classifications (the brief's vocabulary) ───────────────
ALL_AGREE = "ALL_AGREE"
STRUCTURED_AND_AGE_AGREE = "STRUCTURED_AND_AGE_AGREE"
STRUCTURED_CORRECT_AGE_WRONG = "STRUCTURED_CORRECT_AGE_WRONG"
AGE_CORRECT_STRUCTURED_WRONG = "AGE_CORRECT_STRUCTURED_WRONG"
BOTH_WRONG = "BOTH_WRONG"
CONFLICT = "CONFLICT"
CORRECT_REFUSAL = "CORRECT_REFUSAL"
INCORRECT_REFUSAL = "INCORRECT_REFUSAL"
EXECUTION_ERROR = "EXECUTION_ERROR"
UNSUPPORTED = "UNSUPPORTED"
NO_GROUND_TRUTH = "NO_GROUND_TRUTH"


def _close(a, b, tol: float = 1e-6) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))
    return a == b


def classify_against_truth(case, structured, age, reconciliation) -> tuple[str, str]:
    """Grade the routes against INDEPENDENT ground truth, not each other.

    Ordering matters. A refusal case is graded on whether the right thing
    was refused before any value is considered, because "the structured
    route correctly refused and the AGE route produced a number" is a
    meaningful outcome that a value-first comparison would mangle.
    """
    truth = case.truth.value if case.truth else None

    s_val = structured.numeric.value if (structured and structured.numeric) else None
    a_val = age.numeric.value if (age and age.numeric) else None
    s_ok = structured is not None and structured.ok
    a_ok = age is not None and age.ok

    # Cases the corpus expects to be refused.
    if case.outcome == "refused":
        if s_ok:
            return (
                INCORRECT_REFUSAL,
                f"structured route returned {s_val!r} where a refusal "
                f"({case.refusal_code}) was expected",
            )
        note = f"structured refused as expected ({structured.refusal_code})"
        if a_ok:
            note += (
                f"; the AGE route produced {a_val!r} for the same question, "
                f"which the architecture surfaces rather than hides"
            )
        return CORRECT_REFUSAL, note

    if truth is None:
        return NO_GROUND_TRUTH, "no independent ground truth for this case"

    s_correct = s_ok and _close(s_val, truth)
    a_correct = a_ok and _close(a_val, truth)

    if s_correct and a_correct:
        return (
            ALL_AGREE if reconciliation.semantic_support == "SUPPORTS"
            else STRUCTURED_AND_AGE_AGREE,
            f"both routes independently computed {truth} "
            f"({case.truth.route} ground truth)",
        )
    if s_correct and not a_ok:
        return (
            STRUCTURED_CORRECT_AGE_WRONG,
            f"structured = {s_val} (correct); AGE route did not produce a "
            f"value ({age.refusal_code if age else 'not run'})",
        )
    if s_correct and a_ok:
        return (
            STRUCTURED_CORRECT_AGE_WRONG,
            f"structured = {s_val} (correct); AGE = {a_val} (wrong, truth {truth})",
        )
    if a_correct and not s_ok:
        return (
            AGE_CORRECT_STRUCTURED_WRONG,
            f"AGE = {a_val} (correct); structured refused "
            f"({structured.refusal_code if structured else 'not run'})",
        )
    if a_correct and s_ok:
        return (
            AGE_CORRECT_STRUCTURED_WRONG,
            f"AGE = {a_val} (correct); structured = {s_val} (wrong, truth {truth})",
        )
    if s_ok and a_ok:
        return BOTH_WRONG, f"structured = {s_val}, AGE = {a_val}, truth = {truth}"
    if structured and structured.status == "EXECUTION_ERROR":
        return EXECUTION_ERROR, f"structured: {structured.refusal_reason}"
    return (
        INCORRECT_REFUSAL,
        f"no route produced the expected value {truth}; structured: "
        f"{structured.refusal_code if structured else 'n/a'}, AGE: "
        f"{age.refusal_code if age else 'n/a'}",
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="evaluate only the first N cases")
    ap.add_argument("--no-age", action="store_true", help="skip the AGE text-to-Cypher route")
    ap.add_argument("--no-semantic", action="store_true", help="skip the semantic route")
    args = ap.parse_args()

    print("Building registry from live data ...")
    snapshot = await build_registry()
    print(
        f"  {len(snapshot.entities)} labels, {len(snapshot.relationships)} "
        f"relationship triples\n"
    )

    cases = build_corpus()
    if args.limit:
        cases = cases[: args.limit]

    schema_card = route_age.build_schema_card(snapshot)
    report: dict = {
        "registry_as_of": snapshot.as_of,
        "case_count": len(cases),
        "routes_run": {
            "structured": True,
            "age_text2cypher": not args.no_age,
            "semantic": not args.no_semantic,
        },
        "schema_card_chars": len(schema_card),
        "cases": [],
    }

    eval_counts: dict[str, int] = {}
    recon_counts: dict[str, int] = {}

    print("=" * 78)
    print(f"PHASE 4 — THREE-WAY EVALUATION ({len(cases)} cases)")
    print("=" * 78)

    for case in cases:
        request = AggregateRouteRequest(
            question=case.spec.question_text, scope=case.spec.scope, spec=case.spec
        )
        started = time.perf_counter()

        structured = await route_structured.run(snapshot, request, spec=case.spec)

        age = None
        if not args.no_age:
            try:
                age = await route_age.run(snapshot, request, schema_card=schema_card)
            except Exception as exc:  # noqa: BLE001 — a crash is a result
                age = routes.refusal(
                    routes.ROUTE_AGE, "route_crashed", str(exc),
                    status=routes.EXECUTION_ERROR,
                )

        semantic = None
        if not args.no_semantic:
            try:
                semantic = await route_semantic.run(snapshot, request)
            except Exception as exc:  # noqa: BLE001
                semantic = routes.refusal(
                    routes.ROUTE_SEMANTIC, "route_crashed", str(exc),
                    status=routes.EXECUTION_ERROR,
                )

        reconciliation = rec.reconcile(structured, age, semantic)
        verdict, why = classify_against_truth(case, structured, age, reconciliation)
        elapsed = (time.perf_counter() - started) * 1000.0

        eval_counts[verdict] = eval_counts.get(verdict, 0) + 1
        recon_counts[reconciliation.classification] = (
            recon_counts.get(reconciliation.classification, 0) + 1
        )

        report["cases"].append({
            "name": case.name,
            "question": case.spec.question_text,
            "expected_outcome": case.outcome,
            "ground_truth": case.truth.value if case.truth else None,
            "ground_truth_route": case.truth.route if case.truth else None,
            "structured": structured.to_dict() if structured else None,
            "age": age.to_dict() if age else None,
            "semantic": semantic.to_dict() if semantic else None,
            "reconciliation": reconciliation.to_dict(),
            "evaluation": verdict,
            "explanation": why,
            "elapsed_ms": round(elapsed, 1),
        })

        mark = {
            ALL_AGREE: "OK  ", STRUCTURED_AND_AGE_AGREE: "OK  ",
            CORRECT_REFUSAL: "OK  ",
        }.get(verdict, "--  ")
        print(f"\n[{mark}] {case.name}  ({elapsed:.0f} ms)")
        print(f"       {verdict}  |  reconciliation: {reconciliation.classification}")
        print(f"       {why}")
        if reconciliation.diagnosis:
            print(f"       diagnosis: {reconciliation.diagnosis[:150]}")

    report["evaluation_summary"] = eval_counts
    report["reconciliation_summary"] = recon_counts

    print("\n" + "=" * 78)
    print("EVALUATION (routes vs INDEPENDENT ground truth)")
    print("=" * 78)
    for k, v in sorted(eval_counts.items()):
        print(f"  {k:32s} {v}")

    print("\nRECONCILIATION (routes vs each other)")
    for k, v in sorted(recon_counts.items()):
        print(f"  {k:32s} {v}")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nReport written to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
