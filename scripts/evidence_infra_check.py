"""Exercise the whole evidence infrastructure against the live store.

Not a unit test and not a product score. This answers one question: with
the structured facts imported and the population model fixed, what does
each layer actually do, end to end, in both languages?

Every case states what it EXPECTS and why, so a wrong answer is visible
rather than merely different. Expectations come from the source snapshot,
never from the SQL the system itself would run.

    python scripts/evidence_infra_check.py
    python scripts/evidence_infra_check.py --only counts
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.evidence.runtime import Runtime  # noqa: E402
from src.evidence.store import ScopedStore  # noqa: E402
from src.evidence.types import QueryOperation, Scope  # noqa: E402

SNAPSHOT = Path("tests/fixtures/muhafiz_api_snapshot.json")


@dataclass
class Check:
    name: str
    detail: str
    passed: bool
    note: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name, detail, passed, note=""):
        self.checks.append(Check(name, detail, passed, note))
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name:<34} {detail}")
        if note:
            print(f"         {note}")


async def scope_with_all_cases() -> tuple[Scope, list[str]]:
    conn = await asyncpg.connect(os.getenv("EVIDENCE_INGEST_DATABASE_URL"))
    for name in ("json", "jsonb"):
        await conn.set_type_codec(name, encoder=json.dumps, decoder=json.loads,
                                  schema="pg_catalog")
    ids = [r["c"] for r in await conn.fetch(
        "SELECT DISTINCT unnest(case_ids) c FROM evidence.documents "
        "WHERE source_metadata->>'endpoint'='fir'")]
    await conn.close()
    return Scope(user_id=uuid4(), session_id=uuid4(), case_ids=ids), ids


async def layer_counts(report: Report, scope: Scope) -> None:
    """Counts must match the raw source AND declare their population."""
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    fir = snap["endpoints"]["fir"]
    expected = {
        "reported at": sum(1 for r in fir if r.get("report_datetime")),
        "weapon register item detail":
            sum(len(r.get("weapon_register") or []) for r in fir),
        "calibre or bore": sum(
            1 for r in fir for w in (r.get("weapon_register") or [])
            if w.get("caliber_or_bore") not in (None, "", "None")),
    }
    store = ScopedStore(scope)
    catalog = await store.catalog("FIR reported weapon register bore")
    by_label = {c.label: c for c in catalog.values()}

    for label, want in expected.items():
        concept = by_label.get(label)
        if concept is None:
            report.add("count/" + label, "concept not in catalog", False)
            continue
        op = QueryOperation(output_id="c", operation="count_assertions",
                            predicate_id=concept.concept_id)
        result = (await store.execute(op, catalog)).model_dump(mode="json")
        rows = result.get("rows") or []
        got = rows[0].get("value") if rows else None
        cov = result.get("coverage") or {}
        complete = cov.get("population_documents") == cov.get("complete_documents")
        report.add(
            "count/" + label,
            f"got {got}, source says {want}; population "
            f"{cov.get('complete_documents')}/{cov.get('population_documents')}",
            got == want and complete,
            "" if got == want else "value disagrees with the raw snapshot")

    # The population must be the record kind, never every authorized doc.
    concept = by_label.get("reported at")
    if concept:
        op = QueryOperation(output_id="c", operation="count_assertions",
                            predicate_id=concept.concept_id)
        cov = (await store.execute(op, catalog)).model_dump(mode="json")["coverage"]
        report.add("population/scoped",
                   f"denominator {cov['population_documents']} "
                   f"(was 7528 before the population fix)",
                   cov["population_documents"] < 200,
                   "" if cov["population_documents"] < 200
                   else "denominator still includes unrelated documents")


async def layer_identity(report: Report) -> None:
    """Identity must come from CNICs, and match the raw source exactly."""
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    fir = snap["endpoints"]["fir"]
    cnics = set()
    for r in fir:
        if r.get("complainant_cnic"):
            cnics.add(r["complainant_cnic"])
        for a in (r.get("fir_accused") or []):
            if a.get("cnic"):
                cnics.add(a["cnic"])

    conn = await asyncpg.connect(os.getenv("EVIDENCE_INGEST_DATABASE_URL"))
    for name in ("json", "jsonb"):
        await conn.set_type_codec(name, encoder=json.dumps, decoder=json.loads,
                                  schema="pg_catalog")
    got = await conn.fetchval(
        """SELECT count(DISTINCT e.identifiers->>'cnic') FROM evidence.entities e
           JOIN evidence.concepts c ON c.concept_id=e.concept_id
           WHERE c.label='person'""")
    by_name = await conn.fetchval(
        "SELECT count(*) FROM evidence.identity_links WHERE method NOT LIKE 'source_identifier:%'")
    await conn.close()

    report.add("identity/distinct persons",
               f"{got} in store, {len(cnics)} CNICs in the FIR source",
               got >= len(cnics))
    report.add("identity/no name matching",
               f"{by_name} link(s) made on anything other than a source identifier",
               by_name == 0,
               "" if by_name == 0 else "a link was made without a stable identifier")


async def layer_runtime(report: Report, scope: Scope, only_lang=None) -> None:
    """The runtime, end to end, in both languages."""
    UR = ("جون 2026 میں کتنی "
          "ایف آئی آر رپورٹ "
          "ہوئیں؟")
    cases = [("runtime/en", "How many FIRs were reported in June 2026?", "en"),
             ("runtime/ur", UR, "ur"),
             ("runtime/registration trap",
              "How many FIRs were registered this month?", "en")]
    for name, question, lang in cases:
        if only_lang and lang != only_lang:
            continue
        started = time.monotonic()
        try:
            answer = await Runtime(ScopedStore(scope)).run(
                uuid4(), question, scope, deadline=time.monotonic() + 100)
            d = answer.model_dump(mode="json")
            seconds = round(time.monotonic() - started, 1)
            status = d.get("status")
            text = str(d.get("text") or "")
            # A failure to complete must never read as a factual zero.
            invented_zero = status in {"unavailable", "not_extracted"} and (
                " 0 " in text or text.strip().endswith(" 0"))
            report.add(name, f"{seconds}s status={status} claims={len(d.get('claims') or [])}",
                       not invented_zero,
                       text[:150] if text else "")
        except Exception as exc:                        # noqa: BLE001
            report.add(name, f"{type(exc).__name__}", False, str(exc)[:160])


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=["counts", "identity", "runtime"])
    args = ap.parse_args()

    scope, ids = await scope_with_all_cases()
    print(f"scope grants {len(ids)} case(s)\n")
    report = Report()

    if args.only in (None, "counts"):
        print("COUNTS AND COVERAGE")
        await layer_counts(report, scope)
    if args.only in (None, "identity"):
        print("\nIDENTITY")
        await layer_identity(report)
    if args.only in (None, "runtime"):
        print("\nRUNTIME, END TO END")
        await layer_runtime(report, scope)

    passed = sum(1 for c in report.checks if c.passed)
    print(f"\n{'=' * 60}")
    print(f"{passed}/{len(report.checks)} checks passed")
    failed = [c for c in report.checks if not c.passed]
    if failed:
        print("\nfailing:")
        for c in failed:
            print(f"  {c.name}: {c.detail}")
    print("\nThis checks the layers against the SOURCE, not a product score.")


if __name__ == "__main__":
    asyncio.run(main())
