"""Run the questions people actually asked this system, and record what it says.

The 60 questions come from the live chat history -- `messages` where
role='user' -- ordered by how often each was asked. They are not questions
anyone wrote for a benchmark, which is the point: they include analytical
prompts ("acting as a crime analyst, flag anything unusual"), multi-part
comparisons, and Roman Urdu, none of which any test here anticipated.

WHAT IS AND IS NOT SCORED

Correctness is NOT scored. There is no reviewed answer for most of these,
and a model grading its own output is not evidence. What is recorded, per
question, is what can be checked mechanically:

    status          answered / partial / clarification / unavailable
    figures         values from validated quantitative result claims
    coverage        whether the answer stated what population it rests on
    citations       whether spans were returned
    seconds         latency

Claim validation failures are recorded separately. Digits in quotations,
identifiers and statute references are not counted as aggregate claims.
Full answers and diagnostics go to a new run directory. Original results
are never overwritten. This is a context-free component probe, not an API
session replay; use evidence_replay.py for authenticated conversations.
No result here is assigned a semantic correctness score automatically.

    python scripts/evidence_history_check.py
    python scripts/evidence_history_check.py --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import hashlib
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
from src.evidence.types import Scope  # noqa: E402
from src.evidence.evaluation import inspect_answer
from datetime import datetime,timezone

QUESTIONS = Path("data/qa/history_questions.json")
OUT = Path("data/qa/history_results.json")

_NUM = re.compile(r"\b\d[\d,]*\b")
_ARABIC = re.compile(r"[؀-ۿ]")
_ROMAN = re.compile(r"\b(kya|hai|ke|ki|mein|hamare|kaun|kitne|se|ko|shuda|"
                    r"karne|wale|rahe|nahin)\b", re.I)

COVERAGE_MARKERS = ("Documents checked", "جانچی گئی دستاویزات",
                    "Partial evidence", "not been fully extracted",
                    "مکمل استخراج", "Jaanchi gayi", "population")


def language_of(question: str) -> str:
    if _ARABIC.search(question):
        return "ur"
    return "roman_ur" if _ROMAN.search(question) else "en"


async def all_case_ids() -> list[str]:
    conn = await asyncpg.connect(os.getenv("EVIDENCE_INGEST_DATABASE_URL"),timeout=10,command_timeout=15)
    for name in ("json", "jsonb"):
        await conn.set_type_codec(name, encoder=json.dumps, decoder=json.loads,
                                  schema="pg_catalog")
    rows = await conn.fetch(
        "SELECT DISTINCT unnest(case_ids) c FROM evidence.documents "
        "WHERE source_metadata->>'endpoint'='fir'")
    await conn.close()
    return [r["c"] for r in rows]


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--lang", choices=["en", "ur", "roman_ur"])
    ap.add_argument("--ids", help="Comma-separated 1-based question IDs from the frozen history file")
    ap.add_argument("--output-dir",type=Path)
    args = ap.parse_args()

    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    questions=[{**q,"question_id":i} for i,q in enumerate(questions,1)]
    if args.ids:
        selected={int(i) for i in args.ids.split(',')}
        questions=[q for q in questions if q["question_id"] in selected]
    if args.lang:
        questions = [q for q in questions
                     if language_of(q["question"]) == args.lang]
    if args.limit:
        questions = questions[:args.limit]

    ids = await all_case_ids()
    directory=args.output_dir or Path("data/qa/runs")/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"_"+uuid4().hex[:8])
    directory.mkdir(parents=True,exist_ok=False)
    output=directory/"results.json"
    scope = Scope(user_id=uuid4(), session_id=uuid4(), case_ids=ids)
    root=Path(__file__).resolve().parents[1]
    source_hashes={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted((root/'src/evidence').glob('*.py'))}
    (directory/"manifest.json").write_text(json.dumps({"mode":"context_free_runtime_probe",
        "deadline_seconds":90,"original_session_context":False,
        "source_hashes":source_hashes,"question_file_sha256":hashlib.sha256(QUESTIONS.read_bytes()).hexdigest(),
        "scope":scope.model_dump(mode='json'),
        "question_ids":[q["question_id"] for q in questions]},indent=2),encoding="utf-8")
    # A supervisor: conversation.prepare() grants a case list only to
    # supervisor and above, so this is the role these questions imply.
    print(f"{len(questions)} question(s) from the chat history, "
          f"scope grants {len(ids)} case(s)\n")

    results = []
    unsupported = 0
    for index, item in enumerate(questions, start=1):
        question = item["question"]
        lang = language_of(question)
        started = time.monotonic()
        record = {"question": question, "times_asked": item["times_asked"],
                  "language": lang,"question_id":item["question_id"],"mode":"context_free_runtime_probe"}
        changed=[name for name,digest in source_hashes.items()
                 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest]
        if changed:
            raise RuntimeError('Runtime source changed during frozen evaluation: '+', '.join(changed))
        try:
            answer = await Runtime(ScopedStore(scope)).run(
                uuid4(), question, scope, deadline=time.monotonic() + 90)
            d = answer.model_dump(mode="json")
            text = str(d.get("text") or "")
            checks=inspect_answer(answer,scope)
            figures = [str(m["value"]) for m in checks["measurements"]]
            stated = any(m in text for m in COVERAGE_MARKERS)
            record.update(
                seconds=round(time.monotonic() - started, 1),
                status=d.get("status"),
                claims=len(d.get("claims") or []),
                citations=sum(len(r.get("citations") or [])
                              for r in (d.get("results") or [])),
                figures=figures, stated_coverage=stated,
                unsupported_figure=bool(checks["claim_failures"]),
                text=text,health=answer.health,checks=checks,answer=d)
        except Exception as exc:                        # noqa: BLE001
            record.update(seconds=round(time.monotonic() - started, 1),
                          status="error",
                          error=type(exc).__name__)
        if record.get("unsupported_figure"):
            unsupported += 1
        results.append(record)

        flag = "!!" if record.get("unsupported_figure") else "  "
        print(f"{flag}[{index:>2}/{len(questions)}] {lang:<8} "
              f"{record.get('seconds', 0):>5.1f}s "
              f"{str(record.get('status')):<14} "
              f"claims={record.get('claims', 0):<3} "
              f"{question[:60]}", flush=True)
        output.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                       encoding="utf-8")

    by_status: dict[str, int] = {}
    by_lang: dict[str, dict] = {}
    for r in results:
        by_status[str(r.get("status"))] = by_status.get(str(r.get("status")), 0) + 1
        bucket = by_lang.setdefault(r["language"], {"n": 0, "answered": 0})
        bucket["n"] += 1
        if r.get("status") in {"answered", "partial"}:
            bucket["answered"] += 1

    print(f"\n{'=' * 66}")
    print("status:", json.dumps(by_status))
    for lang, bucket in sorted(by_lang.items()):
        print(f"  {lang:<9} answered-or-partial {bucket['answered']}/{bucket['n']}")
    print(f"\nCLAIM VALIDATION FAILURES: {unsupported}")
    print(f"full answers and diagnostics written to {output} for review")
    print("\nThis does NOT score correctness. Read the answers.")


if __name__ == "__main__":
    asyncio.run(main())
