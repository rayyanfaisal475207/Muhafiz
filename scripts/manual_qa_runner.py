# -*- coding: utf-8 -*-
"""
Manual QA runner — execute real questions and inspect the whole pipeline.

    PYTHONPATH=. python scripts/manual_qa_runner.py
    PYTHONPATH=. python scripts/manual_qa_runner.py --limit 3
    PYTHONPATH=. python scripts/manual_qa_runner.py -q "How many cases?"
    PYTHONPATH=. python scripts/manual_qa_runner.py --questions my_set.txt
    PYTHONPATH=. python scripts/manual_qa_runner.py --json-only

DIAGNOSTIC ONLY. Reads the live database and calls a model for spec
generation and the AGE route. It writes one report file and changes
nothing else: no production routing, no writes to application data, no
schema changes.

WHAT IT IS FOR. `answer_question()` returns a complete audit trail, but a
dataclass in a Python process is not something a tester can read. This
prints every stage the pipeline went through — the generated spec, the
validation verdict, which triggers fired, which routes actually ran, what
each returned, and how they reconciled — so a wrong answer can be traced
to the stage that produced it rather than guessed at.

WHAT IT DELIBERATELY DOES NOT DO. It does not grade. There is no expected
value in the smoke set and no pass/fail column, because the questions it
runs have no independently-established ground truth — that is what
`scripts/run_aggregate_three_way.py` has, against the 42-case corpus with
figures derived from direct SQL. A runner that printed PASS next to a
number it got from the same pipeline that produced the number would be
measuring nothing. Correctness here is the tester's judgement, recorded in
MANUAL_QA_TEMPLATE.md.

IT CALLS THE SAME PATH THE PRODUCT WOULD. `orchestrator.answer_question()`
is invoked exactly as a request handler would invoke it. Nothing is
special-cased for QA, so a defect seen here is a defect in the real path.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline.aggregate import orchestrator as orch  # noqa: E402
from src.pipeline.aggregate import route_age  # noqa: E402
from src.pipeline.aggregate.registry import build_registry  # noqa: E402
from src.pipeline.aggregate.spec import Scope  # noqa: E402

logging.basicConfig(level=logging.ERROR)

REPORT_PATH = Path("docs/aggregate-shadow/manual_qa_report.json")
SMOKE_SET_PATH = Path("docs/aggregate-shadow/smoke_qa_set.json")

#: The QA caller. Cross-case aggregates need supervisor or higher, and the
#: orchestrator refuses anything less BEFORE calling a model — so a tester
#: lowering this is a legitimate test of the scope gate, not a broken run.
QA_SCOPE = Scope(kind="cross_case", user_role="supervisor", user_id="qa-runner")


# ══════════════════════════════════════════════════════════════════════
# Smoke QA Set — 15 questions
#
# NOT the QA corpus. This is a SMOKE set: enough to prove the pipeline
# executes end to end across the shapes it claims to support, and enough
# to expose an obvious regression. It is far too small to characterise
# accuracy, and no figure derived from 15 questions should be reported as
# a rate.
#
# `expectation` is what the ARCHITECTURE should do — which routes run, and
# whether the answer may be called verified. It is deliberately not an
# expected VALUE: these questions have no independently-established truth,
# and asserting a value the pipeline itself produced would be circular.
# ══════════════════════════════════════════════════════════════════════
SMOKE_SET: tuple[dict, ...] = (
    # ── simple count ──────────────────────────────────────────────────
    {
        "id": "smoke_01_simple_count",
        "category": "simple count",
        "question": "How many cases are registered?",
        "expectation": "Structured only. No mandatory trigger; answer marked unverified.",
    },
    {
        "id": "smoke_02_simple_count_stations",
        "category": "simple count",
        "question": "How many police stations are there?",
        "expectation": "Structured only. Plain ENTITY count.",
    },
    {
        "id": "smoke_03_simple_count_documents",
        "category": "simple count",
        "question": "How many documents are in the system?",
        "expectation": "Structured only, or a refusal if no such label exists.",
    },
    # ── filters ───────────────────────────────────────────────────────
    {
        "id": "smoke_04_numeric_filter",
        "category": "filters",
        "question": "How many people are older than 30?",
        "expectation": (
            "Person.age is present on 19 of 430. Expect an answer carrying an "
            "INSUFFICIENT_DATA_COVERAGE warning, NOT a bare number."
        ),
    },
    {
        "id": "smoke_05_status_filter",
        "category": "filters",
        "question": "How many cases have an open status?",
        "expectation": (
            "A string-literal filter, so M3 should fire and all three routes run."
        ),
    },
    # ── relationships ─────────────────────────────────────────────────
    {
        "id": "smoke_06_traversal_distinct",
        "category": "relationships",
        "question": "How many distinct people are involved in cases?",
        "expectation": (
            "BELONGS_TO_CASE fans (449 edges over 208 people), so M2 fires and "
            "all three routes run. A value near 449 would indicate row inflation."
        ),
    },
    {
        "id": "smoke_07_role_filtered_traversal",
        "category": "relationships",
        "question": "How many distinct people were accused across all cases?",
        "expectation": (
            "Role lives on Person-[INVOLVED_IN]->Incident, NOT on "
            "BELONGS_TO_CASE (which carries no role property). Verified "
            "answer: 92 distinct accused over 94 accused edges. A result of "
            "208 means the role filter was dropped and this is the same "
            "query as smoke_06."
        ),
    },
    {
        "id": "smoke_08_relationship_grain",
        "category": "relationships",
        "question": "How many accusations were recorded in total?",
        "expectation": (
            "RELATIONSHIP grain counts edges, not people. M4 should fire on the "
            "non-ENTITY grain."
        ),
    },
    # ── multi-source ──────────────────────────────────────────────────
    {
        "id": "smoke_09_multi_source_filtered",
        "category": "multi-source",
        "question": "How many cases from 2024 involve more than one officer?",
        "expectation": (
            "Graph population plus a Postgres-authoritative date. M1 fires; "
            "PARTIAL, because AGE cannot evaluate the window."
        ),
    },
    # ── temporal ──────────────────────────────────────────────────────
    {
        "id": "smoke_10_temporal_2024",
        "category": "temporal",
        "question": "How many cases were registered in 2024?",
        "expectation": (
            "M1 fires. PARTIAL: AGE does not run, semantic does, and the answer "
            "must say it is not independently verified."
        ),
    },
    {
        "id": "smoke_11_temporal_future",
        "category": "temporal",
        "question": "How many cases were registered in 2026?",
        "expectation": (
            "Same shape as smoke_10. Phase 6 measured a 73-vs-51 disagreement "
            "on this shape, where 51 was correct."
        ),
    },
    # ── value literal ─────────────────────────────────────────────────
    {
        "id": "smoke_12_value_literal_urdu",
        "category": "value literal",
        "question": "How many unlicensed weapons are there?",
        "expectation": (
            "The data holds 'بغیر لائسنس', not 'unlicensed'. M3 fires. A value "
            "of 0 means the model guessed an English literal — the exact defect "
            "M3 exists to catch."
        ),
    },
    {
        "id": "smoke_13_value_literal_record_type",
        "category": "value literal",
        "question": "How many malkhana records are there?",
        "expectation": (
            "The literal is 'malkhana_register'. M3 fires; all three routes run."
        ),
    },
    # ── unsupported ───────────────────────────────────────────────────
    {
        "id": "smoke_14_unsupported_median",
        "category": "unsupported",
        "question": "What is the median age of accused persons?",
        "expectation": (
            "AGE cannot compute a median, so PARTIAL at best. A refusal is also "
            "acceptable. What is NOT acceptable is a number described as verified."
        ),
    },
    {
        "id": "smoke_15_unsupported_nonsense",
        "category": "unsupported",
        "question": "How many unicorns were seized last year?",
        "expectation": (
            "No such entity exists. Expect REFUSED with spec_invalid or "
            "spec_generation_failed. A number here would be a serious defect."
        ),
    },
)


# ══════════════════════════════════════════════════════════════════════
# Report assembly
# ══════════════════════════════════════════════════════════════════════
def _spec_summary(spec: Any) -> Optional[dict]:
    """The spec in the terms a reviewer checks it in.

    `to_dict()` is the full nested form and goes into the JSON report; this
    is the flattened view a human reads to answer "did it understand the
    question?" without decoding a tree.
    """
    if spec is None:
        return None
    return {
        "measure": spec.measure,
        "entity": spec.population.entity,
        "grain": spec.grain,
        "distinct_key": spec.distinct_key,
        "value_field": spec.value_field,
        "traversals": [
            {
                "rel": t.rel, "target": t.target, "direction": t.direction,
                "role_field": t.role_field, "role_value": t.role_value,
            }
            for t in spec.population.traversals
        ],
        "predicates": [
            {k: v for k, v in dataclasses.asdict(p).items()}
            for p in spec.population.predicates
        ],
        "group_by": [g.field for g in spec.group_by],
        "time_window": (
            dataclasses.asdict(spec.time_window)
            if spec.time_window is not None else None
        ),
        "has_ratio": spec.ratio is not None,
        "spec_hash": spec.spec_hash(),
    }


def _route_summary(result: Any) -> Optional[dict]:
    """One route's outcome, flattened."""
    if result is None:
        return None
    numeric = getattr(result, "numeric", None)
    out = {
        "status": result.status,
        "shape": result.result_shape,
        "value": numeric.value if numeric is not None else None,
        "interpretation": numeric.interpretation if numeric is not None else None,
        "grain": numeric.grain if numeric is not None else None,
        "refusal_code": result.refusal_code,
        "refusal_reason": result.refusal_reason,
        "warnings": list(result.warnings or ()),
    }
    coverage = getattr(result, "coverage", None)
    if coverage is not None:
        out["coverage_verdict"] = getattr(coverage, "verdict", None)
    # Evidence routes carry no value; record what they did retrieve so a
    # tester can see the semantic route ran and found something.
    evidence = getattr(result, "result", None)
    if hasattr(evidence, "evidence_count"):
        out["evidence_count"] = evidence.evidence_count
        out["semantic_interpretation"] = getattr(evidence, "interpretation", None)
    return out


def build_record(case: dict, answer: Any, elapsed_ms: float) -> dict:
    """One QA record, in the shape the brief specifies."""
    decision = answer.decision
    routes_executed = [
        name for name, r in (
            ("structured", answer.structured),
            ("age", answer.age),
            ("semantic", answer.semantic),
        ) if r is not None
    ]
    validation = answer.validation
    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "question": answer.question,
        "expectation": case.get("expectation"),
        "aggregate_spec": _spec_summary(answer.spec),
        "aggregate_spec_full": (
            answer.spec.to_dict() if answer.spec is not None else None
        ),
        "validation": {
            "verdict": getattr(validation, "verdict", None),
            "ok": bool(getattr(validation, "ok", False)),
            "issues": [
                {"code": i.code, "message": i.message, "field": i.field}
                for i in getattr(validation, "issues", ()) or ()
            ],
        } if validation is not None else None,
        "verification_level": getattr(decision, "level", None),
        "verification_detail": decision.to_dict() if decision is not None else None,
        "routes_executed": routes_executed,
        "structured_result": _route_summary(answer.structured),
        "age_result": _route_summary(answer.age),
        "semantic_result": _route_summary(answer.semantic),
        "reconciliation": (
            answer.reconciliation.to_dict()
            if answer.reconciliation is not None else None
        ),
        "final_answer": {
            "status": answer.status,
            "value": answer.value,
            "interpretation": answer.interpretation,
            "grain": answer.grain,
            "independently_verified": answer.independently_verified,
            "verification_note": answer.verification_note,
            "refusal_code": answer.refusal_code,
            "refusal_reason": answer.refusal_reason,
        },
        "warnings": list(answer.warnings or ()),
        "latency": {
            "total_ms": round(elapsed_ms),
            **{k: round(v) for k, v in (answer.timings_ms or {}).items()},
        },
    }


# ══════════════════════════════════════════════════════════════════════
# Console rendering
# ══════════════════════════════════════════════════════════════════════
def _fmt_predicate(p: dict) -> str:
    if "rel" in p:
        return (
            f"count({p.get('rel')}->{p.get('target')}) "
            f"{p.get('op')} {p.get('value')} [{p.get('count_grain')}]"
        )
    return f"{p.get('field')} {p.get('op')} {p.get('value')!r}"


def print_record(record: dict, index: int, total: int) -> None:
    """Print every stage. Verbose on purpose: this is the inspection tool."""
    print("=" * 78)
    print(f"[{index}/{total}]  {record['id']}   ({record['category']})")
    print(f"QUESTION : {record['question']}")
    if record.get("expectation"):
        print(f"EXPECTED : {record['expectation']}")
    print("-" * 78)

    spec = record["aggregate_spec"]
    if spec is None:
        print("SPEC     : (none generated)")
    else:
        print(
            f"SPEC     : {spec['measure']}({spec['entity']}) "
            f"grain={spec['grain']} key={spec['distinct_key']}"
        )
        if spec["value_field"]:
            print(f"           value_field={spec['value_field']}")
        for t in spec["traversals"]:
            role = (
                f" role={t['role_field']}={t['role_value']!r}"
                if t["role_field"] else ""
            )
            print(f"           hop: -[{t['rel']}]-> {t['target']} ({t['direction']}){role}")
        for p in spec["predicates"]:
            print(f"           where: {_fmt_predicate(p)}")
        if spec["time_window"]:
            tw = spec["time_window"]
            print(f"           window: {tw['field']} {tw['start']} .. {tw['end']}")
        if spec["group_by"]:
            print(f"           group_by: {', '.join(spec['group_by'])}")

    val = record["validation"]
    if val is not None:
        print(f"VALIDATE : {val['verdict']}")
        for issue in val["issues"]:
            print(f"           - {issue['code']}: {issue['message'][:100]}")

    detail = record["verification_detail"]
    if detail is not None:
        print(
            f"DECISION : {detail['level']} "
            f"(deterministic={detail['deterministic_level']}, "
            f"llm={detail['llm_recommendation']})"
        )
        if detail["mandatory_triggers"]:
            print(f"           mandatory: {', '.join(detail['mandatory_triggers'])}")
        if detail["advisory_triggers"]:
            print(f"           advisory : {', '.join(detail['advisory_triggers'])}")
        for t in detail["triggers"]:
            if t["mandatory"] and t.get("detail"):
                print(f"           {t['code']}: {t['detail'][:90]}")

    print(f"ROUTES   : {', '.join(record['routes_executed']) or '(none)'}")
    for name in ("structured", "age", "semantic"):
        r = record[f"{name}_result"]
        if r is None:
            continue
        if r["value"] is not None:
            extra = f" [{r['interpretation']}/{r['grain']}]"
            print(f"  {name:<10}: {r['status']}  value={r['value']}{extra}")
        elif r.get("evidence_count") is not None:
            print(f"  {name:<10}: {r['status']}  evidence={r['evidence_count']}")
        else:
            print(f"  {name:<10}: {r['status']}  {r['refusal_code'] or ''}")
        if r.get("coverage_verdict"):
            print(f"              coverage={r['coverage_verdict']}")
        for w in r["warnings"][:3]:
            print(f"              ! {w[:88]}")

    recon = record["reconciliation"]
    if recon is not None:
        print(
            f"RECONCILE: {recon.get('classification')}  "
            f"value={recon.get('value')}  "
            f"semantic={recon.get('semantic_support')}"
        )
        if recon.get("diagnosis"):
            print(f"           diagnosis: {recon['diagnosis'][:88]}")

    final = record["final_answer"]
    print("-" * 78)
    print(f"ANSWER   : {final['status']}  value={final['value']}")
    print(f"VERIFIED : {final['independently_verified']}  — {final['verification_note']}")
    if final["refusal_code"]:
        print(f"REFUSAL  : {final['refusal_code']}: {(final['refusal_reason'] or '')[:150]}")
    for w in record["warnings"]:
        print(f"WARNING  : {w[:140]}")
    lat = record["latency"]
    print(
        "LATENCY  : total=%sms  %s" % (
            lat["total_ms"],
            "  ".join(f"{k}={v}ms" for k, v in lat.items() if k != "total_ms"),
        )
    )
    print()


def print_summary(records: list[dict]) -> None:
    """Counts only — deliberately no pass/fail.

    There is no ground truth for these questions, so a PASS column would be
    the pipeline grading itself. Correctness is recorded by the tester in
    MANUAL_QA_TEMPLATE.md.
    """
    total = len(records)
    answered = sum(1 for r in records if r["final_answer"]["status"] == "ANSWERED")
    refused = sum(1 for r in records if r["final_answer"]["status"] == "REFUSED")
    conflict = sum(1 for r in records if r["final_answer"]["status"] == "CONFLICT")
    verified = sum(1 for r in records if r["final_answer"]["independently_verified"])

    levels: dict[str, int] = {}
    triggers: dict[str, int] = {}
    for r in records:
        levels[r["verification_level"] or "n/a"] = (
            levels.get(r["verification_level"] or "n/a", 0) + 1
        )
        detail = r["verification_detail"] or {}
        for code in detail.get("mandatory_triggers", []):
            triggers[code] = triggers.get(code, 0) + 1

    print("=" * 78)
    print("SMOKE QA SET — EXECUTION SUMMARY")
    print("=" * 78)
    print(f"  questions run          : {total}")
    print(f"  answered               : {answered}")
    print(f"  refused                : {refused}")
    print(f"  conflict               : {conflict}")
    print(f"  independently verified : {verified}")
    print(f"  verification levels    : {dict(sorted(levels.items()))}")
    print(f"  mandatory triggers     : {dict(sorted(triggers.items()))}")
    latencies = [r["latency"]["total_ms"] for r in records]
    if latencies:
        ordered = sorted(latencies)
        print(
            f"  latency ms             : min={ordered[0]} "
            f"median={ordered[len(ordered) // 2]} max={ordered[-1]}"
        )
    print()
    print("  NOT a pass rate. These questions have no independently-established")
    print("  ground truth; correctness is the tester's judgement, recorded in")
    print("  MANUAL_QA_TEMPLATE.md. For graded evaluation against known figures,")
    print("  use scripts/run_aggregate_three_way.py.")
    print()


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════
def load_questions(path: Path) -> list[dict]:
    """Read a tester's own question set.

    Accepts a .txt file (one question per line, # for comments) or a .json
    file of objects carrying at least `question`.
    """
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        cases = data.get("questions", data) if isinstance(data, dict) else data
        return [
            c if isinstance(c, dict) else {"question": str(c)}
            for c in cases
        ]
    cases = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append({"id": f"custom_{n:02d}", "category": "custom",
                          "question": line})
    return cases


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run questions through the full three-way pipeline and "
                    "print every stage.",
    )
    ap.add_argument("-q", "--question", action="append", default=[],
                    help="Run one ad-hoc question (repeatable).")
    ap.add_argument("--questions", type=Path,
                    help="File of questions (.txt one per line, or .json).")
    ap.add_argument("--limit", type=int,
                    help="Run only the first N of the smoke set.")
    ap.add_argument("--category", action="append", default=[],
                    help="Run only these smoke categories (repeatable).")
    ap.add_argument("--role", default="supervisor",
                    help="Caller role. Lower it to exercise the scope gate.")
    ap.add_argument("--report", type=Path, default=REPORT_PATH,
                    help=f"Where to write the JSON report (default {REPORT_PATH}).")
    ap.add_argument("--json-only", action="store_true",
                    help="Suppress the per-question console output.")
    args = ap.parse_args()

    # ── Assemble the question set.
    if args.question:
        cases = [
            {"id": f"adhoc_{i:02d}", "category": "ad-hoc", "question": q}
            for i, q in enumerate(args.question, 1)
        ]
    elif args.questions:
        cases = load_questions(args.questions)
    else:
        cases = list(SMOKE_SET)
        if args.category:
            wanted = {c.lower() for c in args.category}
            cases = [c for c in cases if c["category"].lower() in wanted]
        if args.limit:
            cases = cases[: args.limit]

    if not cases:
        print("No questions selected.", file=sys.stderr)
        return 2

    scope = dataclasses.replace(QA_SCOPE, user_role=args.role)

    print(f"Building registry snapshot ...")
    snapshot = await build_registry()
    print(
        f"  as_of={snapshot.as_of}  {len(snapshot.entities)} labels, "
        f"{len(snapshot.relationships)} relationships, "
        f"{len(snapshot.logical_fields)} logical fields"
    )

    # The schema card costs one read per candidate property, so it is built
    # ONCE and passed to every question — both for spec generation and for
    # the AGE route, which is also what a real deployment would do.
    print("Building schema card (value examples) ...")
    try:
        examples = await route_age.collect_value_examples(snapshot)
    except Exception as exc:  # noqa: BLE001 — examples are an aid
        print(f"  value examples unavailable: {exc}")
        examples = {}
    schema_card = route_age.build_schema_card(snapshot, examples)
    print(f"  card: {len(schema_card)} chars, {len(examples)} property examples\n")

    records: list[dict] = []
    for i, case in enumerate(cases, 1):
        if not args.json_only:
            print(f"--> [{i}/{len(cases)}] {case['question']}", flush=True)
        started = time.perf_counter()
        try:
            answer = await orch.answer_question(
                snapshot, case["question"], scope, schema_card=schema_card,
            )
        except Exception as exc:  # noqa: BLE001 — one question failing must
            # not end the run; a crash IS a QA finding and is recorded.
            elapsed = (time.perf_counter() - started) * 1000.0
            records.append({
                "id": case.get("id"), "category": case.get("category"),
                "question": case["question"],
                "expectation": case.get("expectation"),
                "aggregate_spec": None, "aggregate_spec_full": None,
                "validation": None, "verification_level": None,
                "verification_detail": None, "routes_executed": [],
                "structured_result": None, "age_result": None,
                "semantic_result": None, "reconciliation": None,
                "final_answer": {
                    "status": "RUNNER_ERROR", "value": None,
                    "interpretation": None, "grain": None,
                    "independently_verified": False,
                    "verification_note": "The runner raised before an answer existed.",
                    "refusal_code": "runner_exception",
                    "refusal_reason": f"{type(exc).__name__}: {exc}",
                },
                "warnings": [], "latency": {"total_ms": round(elapsed)},
            })
            if not args.json_only:
                print(f"    RUNNER ERROR: {type(exc).__name__}: {exc}\n")
            continue

        elapsed = (time.perf_counter() - started) * 1000.0
        record = build_record(case, answer, elapsed)
        records.append(record)
        if not args.json_only:
            print_record(record, i, len(cases))

    print_summary(records)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "registry_as_of": snapshot.as_of,
        "caller_role": args.role,
        "set": "Smoke QA Set" if not (args.question or args.questions) else "custom",
        "question_count": len(records),
        "note": (
            "Smoke set. Not a QA corpus, not a pass rate. No ground truth is "
            "attached to these questions; correctness is recorded by the "
            "tester in MANUAL_QA_TEMPLATE.md."
        ),
        "records": records,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"Report written: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
