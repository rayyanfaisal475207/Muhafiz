"""Run every benchmark question and report the pass rate.

WHY THIS EXISTS

In one session the measured pass rate moved 6/9 -> 1/3 -> 3/3 -> 6/9, and
every move was invisible until someone asked for numbers. Each change was
tested by hand on one or two questions and reported as an improvement; two
of them broke questions nobody re-ran.

Without this, no claim about a fix means anything.

GROUND TRUTH IS QUERIED, NOT WRITTEN DOWN

Each case carries the SQL that establishes its own expected answer. Writing
`73` into this file would make the suite wrong the moment ingestion runs
again — and a test that is quietly wrong is worse than no test, because it
certifies a broken system.

So the expectation is computed live, from the same database the system
answers from. If the corpus grows, the suite grows with it.

WHAT COUNTS AS A PASS

  exact      the answer contains the expected number
  refusal    the system declined, and declining was correct
  neither    a fail, and a wrong NUMBER is reported separately from a
             refusal, because a confident wrong number is the failure this
             whole system exists to prevent

Run:
    python scripts/regression.py            # every case
    python scripts/regression.py --lang ur  # one language
    python scripts/regression.py --save     # record the run for comparison
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.retrieval.engine import dsn, run  # noqa: E402


@dataclass
class Case:
    """One benchmark question and how to establish its true answer."""

    name: str
    question: str
    lang: str
    truth_sql: str | None = None     # returns one number
    truth_sqls: list[str] = field(default_factory=list)
    expect_refusal: bool = False
    # A thematic question has no single right number. What makes its
    # answer correct is that it states how much of the corpus it rests
    # on -- summaries cover 20 of 73 cases, and a pattern claim that
    # does not say so reads as describing all of them.
    expect_coverage_note: bool = False
    # Ground truth for a question whose answer is a NAME, a place, a
    # weapon or a passage rather than a figure. The query returns one
    # column of strings; the answer must contain at least `need` of them.
    #
    # Most investigation is not counting. "Who is accused in FIR 408/26",
    # "what was recovered", "where was it registered" are the questions
    # this system is actually for, and a suite made only of counts
    # measures the SQL half while leaving retrieval, citation and
    # narrative untested.
    truth_text_sql: str | None = None
    need: int = 1
    note: str = ""

    def truths(self) -> list[str]:
        """Every figure this question asks for, in the order it asks.

        A compound question is not passed by producing one right number.
        "total, with a weapon, and theft separately" is three figures, and
        an answer carrying two of them has answered two thirds of what was
        asked while looking complete.
        """
        return self.truth_sqls or ([self.truth_sql] if self.truth_sql else [])


# The questions. Drawn from what people actually asked this system, plus
# their translations — the corpus and its users are bilingual, so a suite
# in one language measures half the product.
CASES: list[Case] = [
    Case("cases_en", "How many cases are registered?", "en",
         "SELECT count(*) FROM rebuild_cases"),
    Case("cases_ur", "کتنے مقدمات درج ہیں؟", "ur",
         "SELECT count(*) FROM rebuild_cases"),

    Case("stations_en", "How many police stations are there?", "en",
         "SELECT count(*) FROM places WHERE kind_raw = 'police_station'"),
    Case("stations_ur", "کتنے تھانے ہیں؟", "ur",
         "SELECT count(*) FROM places WHERE kind_raw = 'police_station'"),

    Case("weapons_en", "How many weapons were recovered?", "en",
         "SELECT count(*) FROM weapons WHERE valid_to IS NULL"),
    Case("weapons_ur", "کتنے ہتھیار برآمد ہوئے؟", "ur",
         "SELECT count(*) FROM weapons WHERE valid_to IS NULL"),

    Case("weapon_cases_en", "How many cases have a weapon linked to them?",
         "en",
         "SELECT count(DISTINCT case_id) FROM weapons WHERE valid_to IS NULL"),

    Case("lahore_en", "How many cases are from Lahore?", "en",
         "SELECT count(*) FROM rebuild_cases r JOIN places p"
         " ON p.place_id = r.station_place_id"
         " WHERE p.name_raw LIKE '%لاہور%'"),
    Case("lahore_ur", "لاہور میں کتنے مقدمات ہیں؟", "ur",
         "SELECT count(*) FROM rebuild_cases r JOIN places p"
         " ON p.place_id = r.station_place_id"
         " WHERE p.name_raw LIKE '%لاہور%'"),

    Case("accused_en", "How many accused persons are recorded?", "en",
         "SELECT count(DISTINCT person_id) FROM case_parties"
         " WHERE role_raw ILIKE '%ملزم%'"),
    Case("accused_ur", "کتنے ملزمان ریکارڈ میں ہیں؟", "ur",
         "SELECT count(DISTINCT person_id) FROM case_parties"
         " WHERE role_raw ILIKE '%ملزم%'"),

    Case("seized_en", "How many items were seized?", "en",
         "SELECT count(*) FROM seized_items"),

    # The one that matters most: a field that does not exist. The legacy
    # engine answered 208 here — every person in the database.
    Case("nonexistent_en",
         "How many persons have a criminal risk score above 80?", "en",
         expect_refusal=True,
         note="no such field; refusal naming it is the only correct answer"),
    Case("nonexistent_ur",
         "کتنے افراد کا کرمنل رسک سکور 80 سے زیادہ ہے؟", "ur",
         expect_refusal=True,
         note="same, asked in Urdu"),

    # Compound questions. An investigator asks for several figures in one
    # breath — "how many in total, how many with a weapon, and theft
    # separately" — and the engine answers each as its own full question.
    # Every figure must come back, or the answer is partial while looking
    # whole.
    Case("compound_en",
         "How many cases are registered? I also need how many cases have a "
         "weapon linked, and how many weapons were recovered.", "en",
         truth_sqls=[
             "SELECT count(*) FROM rebuild_cases",
             "SELECT count(DISTINCT case_id) FROM weapons"
             " WHERE valid_to IS NULL",
             "SELECT count(*) FROM weapons WHERE valid_to IS NULL"]),

    Case("compound_ur",
         "کتنے مقدمات "
         "درج ہیں؟ اور "
         "کتنے تھانے "
         "ہیں؟", "ur",
         truth_sqls=[
             "SELECT count(*) FROM rebuild_cases",
             "SELECT count(*) FROM places WHERE kind_raw = 'police_station'"]),

    # Scope must survive the split: part two is "of those", and answering
    # it without Lahore reports a corpus-wide figure under a Lahore
    # question. Both numbers would pass every check inside their own part.
    Case("compound_scope_en",
         "How many cases are from Lahore, and how many of those had a "
         "weapon?", "en",
         truth_sqls=[
             "SELECT count(*) FROM rebuild_cases r JOIN places p"
             " ON p.place_id = r.station_place_id"
             " WHERE p.name_raw LIKE '%لاہور%'",
             "SELECT count(DISTINCT w.case_id) FROM weapons w"
             " JOIN rebuild_cases r ON r.case_id = w.case_id"
             " JOIN places p ON p.place_id = r.station_place_id"
             " WHERE w.valid_to IS NULL"
             "   AND p.name_raw LIKE '%لاہور%'"]),

    # ── Investigation questions that are not counts ────────────────────
    #
    # Grounded in real rows of this corpus, not invented. fir-408-26 has
    # nine parties, three weapons and a station, so it exercises people,
    # property and place from one case.

    Case("who_accused_en", "Who is accused in FIR 408/26?", "en",
         truth_text_sql="SELECT DISTINCT pe.canonical_name_ur"
                        " FROM case_parties cp"
                        " JOIN persons pe ON pe.person_id = cp.person_id"
                        " WHERE cp.case_id = 'fir-408-26'"
                        "   AND cp.role_raw ILIKE '%ملزم%'"),

    Case("who_accused_ur",
         "اف آئی آر 408/26 میں "
         "ملزم کون ہے؟", "ur",
         truth_text_sql="SELECT DISTINCT pe.canonical_name_ur"
                        " FROM case_parties cp"
                        " JOIN persons pe ON pe.person_id = cp.person_id"
                        " WHERE cp.case_id = 'fir-408-26'"
                        "   AND cp.role_raw ILIKE '%ملزم%'"),

    Case("weapon_in_case_en",
         "What weapon was recovered in FIR 408/26?", "en",
         truth_text_sql="SELECT DISTINCT weapon_type_raw FROM weapons"
                        " WHERE case_id = 'fir-408-26'"),

    Case("weapon_in_case_ur",
         "اف آئی آر 408/26 میں "
         "کون سا ہتھیار "
         "برآمد ہوا؟", "ur",
         truth_text_sql="SELECT DISTINCT weapon_type_raw FROM weapons"
                        " WHERE case_id = 'fir-408-26'"),

    Case("where_registered_en",
         "Which police station registered FIR 408/26?", "en",
         truth_text_sql="SELECT p.name_raw FROM rebuild_cases r"
                        " JOIN places p ON p.place_id = r.station_place_id"
                        " WHERE r.case_id = 'fir-408-26'"),

    Case("complainant_ur",
         "اف آئی آر 408/26 کا "
         "مدعی کون ہے؟", "ur",
         truth_text_sql="SELECT DISTINCT pe.canonical_name_ur"
                        " FROM case_parties cp"
                        " JOIN persons pe ON pe.person_id = cp.person_id"
                        " WHERE cp.case_id = 'fir-408-26'"
                        "   AND cp.role_raw ILIKE '%مدع%'"),

    # Cross-case pattern synthesis -- the capability the old pipeline had
    # as XNETWORK. Scored on honesty about coverage, not on a figure.
    Case("thematic_en",
         "What patterns or themes connect the cases in this database?",
         "en", expect_coverage_note=True,
         note="must state the fraction of cases the summaries cover"),

    Case("smalltalk_en", "hello", "en",
         expect_refusal=False, note="conversational; must not retrieve"),
]


@dataclass
class Result:
    case: Case
    expected: list[int] | int | None
    answer: str | None
    refused: bool
    seconds: float
    verdict: str                     # pass | wrong_number | wrong_refusal | error
    detail: str = ""


_NUM = re.compile(r"\d[\d,]*")


def _numbers(text: str) -> set[int]:
    out: set[int] = set()
    for m in _NUM.finditer(text or ""):
        try:
            out.add(int(m.group(0).replace(",", "")))
        except ValueError:
            continue
    return out


async def run_case(conn: asyncpg.Connection, case: Case) -> Result:
    expected: list[int] = []
    for sql in case.truths():
        try:
            expected.append(await conn.fetchval(sql))
        except Exception as exc:                        # noqa: BLE001
            return Result(case, None, None, False, 0.0, "error",
                          f"truth query failed: {exc}"[:120])

    started = time.time()
    try:
        out = await run(conn, case.question)
    except Exception as exc:                            # noqa: BLE001
        return Result(case, expected, None, False, time.time() - started,
                      "error", str(exc)[:120])
    elapsed = time.time() - started

    answer = out.get("answer")
    answer_text = "" if answer is None else (
        answer if isinstance(answer, str)
        else "; ".join(f"{k}={v}" for k, v in answer.items())
        if isinstance(answer, dict) else str(answer))
    refused = answer is None

    # Small talk: passing means not retrieving, whatever it says.
    if case.name.startswith("smalltalk"):
        ok = not refused and "conversational" in answer_text.lower()
        return Result(case, None, answer_text, refused, elapsed,
                      "pass" if ok else "wrong_refusal",
                      "" if ok else "should answer from the prompt")

    if case.truth_text_sql:
        try:
            rows = await conn.fetch(case.truth_text_sql)
        except Exception as exc:                        # noqa: BLE001
            return Result(case, None, answer_text, False, elapsed, "error",
                          f"truth query failed: {exc}"[:120])
        wanted = [str(r[0]) for r in rows if r[0]]
        if refused:
            return Result(case, None, None, True, elapsed, "wrong_refusal",
                          f"expected one of {wanted[:3]}")
        # Normalised containment: an answer may inflect or re-order a
        # name, and scoring on exact equality would fail correct answers.
        blob = " ".join((answer_text or "").split())
        hit = [w for w in wanted if " ".join(w.split()) in blob]
        ok = len(hit) >= case.need
        return Result(case, None, answer_text, False, elapsed,
                      "pass" if ok else "wrong_number",
                      "" if ok else
                      f"expected {case.need} of {wanted[:3]}, matched {hit}")

    if case.expect_coverage_note:
        # The answer must disclose its own coverage. An unqualified
        # pattern claim over 27% of the corpus is the failure here, and
        # it is indistinguishable from a claim over all of it.
        blob = (answer_text or "") + " " + str(out.get("system_message") or "")
        said = ("of 73 cases" in blob or "not covered" in blob
                or "no cross-case pattern summaries" in blob)
        return Result(case, None, answer_text, refused, elapsed,
                      "pass" if said else "wrong_number",
                      "" if said else
                      "described patterns without stating coverage")

    if case.expect_refusal:
        # A refusal does not have to be a null. The agent often declines in
        # prose -- "the database does not contain a criminal risk score" --
        # which is the BEST form of the correct answer, because it names
        # what is missing. Scoring that as a wrong number made the suite
        # report a failure for the one behaviour this system exists to get
        # right, and sent me hunting a defect that was in the test.
        #
        # What distinguishes a refusal from a wrong answer is not its
        # shape: it is whether a FIGURE was asserted. So look for one.
        asserted = _numbers(answer_text)
        declined = refused or not asserted
        return Result(case, None, answer_text, declined, elapsed,
                      "pass" if declined else "wrong_number",
                      "" if declined else
                      "answered a question the data cannot support")

    if refused:
        return Result(case, expected, None, True, elapsed, "wrong_refusal",
                      f"expected {expected}")

    got = _numbers(answer_text)
    missing = [n for n in expected if n not in got]
    if expected and not missing:
        return Result(case, expected, answer_text, False, elapsed, "pass")

    return Result(case, expected, answer_text, False, elapsed,
                  "wrong_number",
                  f"expected {expected}, missing {missing}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", choices=["en", "ur"])
    ap.add_argument("--only", help="substring of a case name")
    ap.add_argument("--save", action="store_true",
                    help="write the run to regression_runs.jsonl")
    args = ap.parse_args()

    cases = CASES
    if args.lang:
        cases = [c for c in cases if c.lang == args.lang]
    if args.only:
        cases = [c for c in cases if args.only in c.name]

    conn = await asyncpg.connect(dsn())
    results: list[Result] = []
    try:
        print(f"Running {len(cases)} cases\n")
        for case in cases:
            r = await run_case(conn, case)
            results.append(r)
            mark = {"pass": "PASS", "wrong_number": "WRONG NUMBER",
                    "wrong_refusal": "WRONG REFUSAL",
                    "error": "ERROR"}[r.verdict]
            print(f"  [{mark:<13}] {r.seconds:6.1f}s  {case.name:<16} "
                  f"{case.question[:38]}")
            if r.verdict != "pass":
                print(f"                  got: {str(r.answer)[:90]}")
                if r.detail:
                    print(f"                  {r.detail}")
    finally:
        await conn.close()

    passed = sum(1 for r in results if r.verdict == "pass")
    wrong_num = [r for r in results if r.verdict == "wrong_number"]
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"PASS RATE: {passed}/{total} ({100.0 * passed / max(total,1):.0f}%)")
    for lang in ("en", "ur"):
        sub = [r for r in results if r.case.lang == lang]
        if sub:
            ok = sum(1 for r in sub if r.verdict == "pass")
            print(f"  {lang}: {ok}/{len(sub)}")
    # Called out separately: a wrong number is the failure this system
    # exists to prevent, and it must never be averaged into a pass rate.
    if wrong_num:
        print(f"\nCONFIDENT WRONG NUMBERS: {len(wrong_num)} "
              f"— these are disqualifying, not just failures")
        for r in wrong_num:
            print(f"  {r.case.name}: {r.detail}, said {str(r.answer)[:50]}")

    if args.save:
        path = Path("regression_runs.jsonl")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "passed": passed, "total": total,
                "results": [
                    {"name": r.case.name, "verdict": r.verdict,
                     "seconds": round(r.seconds, 1),
                     "expected": r.expected,
                     "answer": str(r.answer)[:200]}
                    for r in results
                ],
            }, ensure_ascii=False) + "\n")
        print(f"\nrecorded in {path}")


if __name__ == "__main__":
    asyncio.run(main())
