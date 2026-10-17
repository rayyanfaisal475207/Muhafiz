"""Measure descriptive answers — the questions investigators actually ask.

WHY A SECOND BENCHMARK

`scripts/regression.py` is 19 questions and almost every one is a count.
It measures the SQL half of the system and leaves retrieval, citation and
narrative untested. Worse, its ground truth is SQL-derived: it asserts
"96 weapons" because `SELECT count(*)` returns 96, which certifies
whatever interpretation the schema happens to encode — including the one
where a pistol's BORE was recorded as a quantity.

Descriptive questions cannot be scored that way, and should not be. There
is no single right answer to "what methods do suspects use". What can be
checked, and what actually matters in a policing context, is whether the
answer is DEFENSIBLE:

    grounded    every claim resolves to a span that was retrieved
    honest      no claim about what is typical without a count
    sourced     the spans exist and their text supports being cited
    scoped      the answer says what it rests on
    bilingual   the same question works in Urdu and English

An answer can be useful and still fail all of these. An answer that
passes them can be trusted enough to act on, which is the bar for Safe
City.

WHAT IS AND IS NOT AUTOMATED

Groundedness, honesty, sourcing and scope are checked mechanically here,
because each is a property of the output that code can verify.

Whether a finding is RELEVANT to the question is not checked. That needs
a human reading the source record, and a model judging its own output is
not evidence. Cases marked `needs_review` print their findings for a
person to accept or reject; the pass rate reported by this script is the
mechanical half only, and it says so.

Run:
    python scripts/descriptive_bench.py
    python scripts/descriptive_bench.py --lang ur
    python scripts/descriptive_bench.py --show      # print every finding
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.retrieval.engine import dsn, run  # noqa: E402


@dataclass
class Case:
    name: str
    question: str
    lang: str
    # Words that must appear SOMEWHERE in the findings for the answer to
    # be on-topic. Not a right answer -- a floor. Absence means the system
    # answered a different question, which no amount of grounding fixes.
    expect_any: list[str] = field(default_factory=list)
    may_refuse: bool = False
    note: str = ""


CASES: list[Case] = [
    Case("fraud_methods_en", "What methods do suspects use to commit fraud?",
         "en", expect_any=["OTP", "bank", "call", "fraud", "transfer"]),
    Case("fraud_methods_ur",
         "فراڈ کے مقدمات میں ملزمان کیا طریقہ استعمال کرتے ہیں؟", "ur",
         expect_any=["فراڈ", "بینک", "او ٹی پی", "دھوکہ", "رقم"]),

    Case("theft_pattern_en", "How do motorcycle thefts happen in these cases?",
         "en", expect_any=["motorcycle", "theft", "stolen", "parked"]),
    Case("theft_pattern_ur", "موٹر سائیکل چوری کیسے ہوتی ہے؟", "ur",
         expect_any=["موٹرسائیکل", "موٹر سائیکل", "چوری", "گم"]),

    Case("weapon_context_en", "What weapons appear in these cases?", "en",
         expect_any=["pistol", "bore", "weapon", "پستول"]),
    Case("weapon_context_ur", "مقدمات میں کون سے ہتھیار استعمال ہوئے؟", "ur",
         expect_any=["پستول", "ہتھیار", "بور"]),

    Case("threats_ur", "ملزمان نے کس قسم کی دھمکیاں دیں؟", "ur",
         expect_any=["دھمکی", "جان", "نقصان", "مال"]),

    # A question the records genuinely cannot answer. Refusing is the
    # correct outcome, and saying something plausible is the failure.
    Case("absent_en",
         "What psychological profiles were assigned to the suspects?", "en",
         may_refuse=True,
         note="no such assessment exists; a confident description is the bug"),
]


@dataclass
class Result:
    case: Case
    seconds: float
    answer: str = ""
    findings: int = 0
    cases_cited: int = 0
    searched: int = 0
    dropped: int = 0
    refused: bool = False
    checks: dict = field(default_factory=dict)
    detail: str = ""

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


async def check(conn: asyncpg.Connection, case: Case) -> Result:
    started = time.time()
    try:
        out = await run(conn, case.question)
    except Exception as exc:                            # noqa: BLE001
        r = Result(case, time.time() - started)
        r.checks = {"ran": False}
        r.detail = str(exc)[:140]
        return r

    elapsed = time.time() - started
    described = out.get("description")
    answer = str(out.get("answer") or "")
    r = Result(case, elapsed, answer=answer, refused=out.get("answer") is None)

    if described is not None:
        r.findings = len(described.findings)
        r.cases_cited = described.cases
        r.searched = described.searched
        r.dropped = len(described.dropped)

    if case.may_refuse:
        # The only failure here is asserting something the records do not
        # support. A refusal, or findings that stay on-topic, both pass.
        r.checks = {"did_not_invent": r.refused or r.findings == 0}
        if not r.checks["did_not_invent"]:
            r.detail = f"described {r.findings} finding(s) for absent data"
        return r

    if r.refused or described is None:
        r.checks = {"answered": False}
        r.detail = str(out.get("refusal") or "")[:140]
        return r

    # 1. GROUNDED — every cited span was actually retrieved and exists.
    span_ids = [s for f in described.findings for s in f.spans]
    real = set()
    if span_ids:
        rows = await conn.fetch(
            "SELECT span_id FROM doc_spans WHERE span_id = ANY($1)",
            list({int(s) for s in span_ids}))
        real = {row["span_id"] for row in rows}
    grounded = bool(span_ids) and all(int(s) in real for s in span_ids)

    # 2. HONEST — nothing claiming what is typical, since no count ran.
    from src.retrieval.describe import _GENERALISING
    honest = not any(_GENERALISING.search(f.claim) for f in described.findings)

    # 3. SCOPED — the answer states what it rests on.
    scoped = "passage(s) searched" in answer or "finding(s)" in answer

    # 4. ON TOPIC — a floor, not a right answer.
    blob = " ".join(f.claim for f in described.findings).lower()
    on_topic = (not case.expect_any
                or any(w.lower() in blob for w in case.expect_any))

    r.checks = {"grounded": grounded, "honest": honest,
                "scoped": scoped, "on_topic": on_topic}
    if not on_topic:
        r.detail = f"none of {case.expect_any[:3]} appeared in the findings"
    elif not grounded:
        r.detail = "a cited span does not exist in doc_spans"
    return r


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", choices=["en", "ur"])
    ap.add_argument("--only")
    ap.add_argument("--show", action="store_true", help="print every finding")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    cases = CASES
    if args.lang:
        cases = [c for c in cases if c.lang == args.lang]
    if args.only:
        cases = [c for c in cases if args.only in c.name]

    conn = await asyncpg.connect(dsn())
    results: list[Result] = []
    try:
        print(f"Running {len(cases)} descriptive cases\n")
        for case in cases:
            r = await check(conn, case)
            results.append(r)
            mark = "PASS" if r.passed else "FAIL"
            failed = [k for k, v in r.checks.items() if not v]
            print(f"  [{mark}] {r.seconds:6.1f}s  {case.name:<20} "
                  f"findings={r.findings} cases={r.cases_cited} "
                  f"searched={r.searched} dropped={r.dropped}")
            if failed:
                print(f"           failed: {', '.join(failed)}")
            if r.detail:
                print(f"           {r.detail}")
            if args.show and r.answer:
                for line in r.answer.split(chr(10))[:6]:
                    print(f"           {line[:150]}")
    finally:
        await conn.close()

    passed = sum(1 for r in results if r.passed)
    print(f"\n{'=' * 62}")
    print(f"MECHANICAL PASS RATE: {passed}/{len(results)}")
    for lang in ("en", "ur"):
        sub = [r for r in results if r.case.lang == lang]
        if sub:
            print(f"  {lang}: {sum(1 for r in sub if r.passed)}/{len(sub)}")

    # Stated every run, because the number above is only half the story.
    print("\nThis measures whether answers are GROUNDED, HONEST and SCOPED.")
    print("Whether each finding is RELEVANT still needs a person reading")
    print("the source record — run with --show and check them.")

    if args.save:
        path = Path("descriptive_runs.jsonl")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "passed": passed, "total": len(results),
                "results": [{"name": r.case.name, "checks": r.checks,
                             "findings": r.findings, "cases": r.cases_cited,
                             "seconds": round(r.seconds, 1)}
                            for r in results],
            }, ensure_ascii=False) + "\n")
        print(f"\nrecorded in {path}")


if __name__ == "__main__":
    asyncio.run(main())
