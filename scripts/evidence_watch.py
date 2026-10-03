"""Run one question and print everything the runtime did, step by step.

Every stage prints what it received, what it decided and what came back:
the concepts that matched, the names that resolved and how, the plan in
concept LABELS rather than UUIDs, the SQL each operation compiled to, the
rows and citations returned, the findings composition proposed and exactly
which ones were dropped and why.

    python scripts/evidence_watch.py "who is Ahmed?"
    python scripts/evidence_watch.py "..." --case fir-1001-26
    python scripts/evidence_watch.py "..." --json   # the raw trace
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
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

RULE = "─" * 78

# Stages worth calling out, in the order they normally happen.
HEADING = {
    "question": "QUESTION",
    "classification": "QUESTION KIND",
    "catalog": "CATALOG",
    "entity_candidates": "NAME RESOLUTION",
    "value_locations": "WHERE VALUES LIVE",
    "web_lookup": "OPEN-WEB LOOKUP",
    "retrieval": "RETRIEVAL ARMS",
    "planning": "PLANNER",
    "planner_call": "PLANNER",
    "plan": "PLAN",
    "plan_rejected": "PLAN REJECTED",
    "strategy": "NEW STRATEGY",
    "payload": "REQUEST SIZE",
    "entity_lookup": "LOOKUP",
    "catalog_lookup": "LOOKUP",
    "clarification": "CLARIFICATION",
    "execute": "EXECUTE",
    "sql": "SQL",
    "search_scope": "SEARCH SCOPE",
    "graph": "GRAPH",
    "graph_seeds": "GRAPH",
    "result": "RESULT",
    "composition": "COMPOSITION",
    "answer": "ANSWER",
}


def show(value, indent="      "):
    """Print a detail value so a person can read it."""
    if isinstance(value, list):
        if not value:
            print(f"{indent}(none)")
            return
        for item in value[:20]:
            if isinstance(item, dict):
                parts = [f"{k}={v}" for k, v in item.items()
                         if v not in (None, "", [], {})]
                print(f"{indent}- " + "  ".join(str(p) for p in parts))
            else:
                print(f"{indent}- {item}")
        if len(value) > 20:
            print(f"{indent}… and {len(value)-20} more")
    elif isinstance(value, dict):
        for key, inner in value.items():
            if inner in (None, "", [], {}):
                continue
            if isinstance(inner, (list, dict)):
                print(f"{indent}{key}:")
                show(inner, indent + "  ")
            else:
                print(f"{indent}{key}: {inner}")
    else:
        print(f"{indent}{value}")


def render(step):
    stage = step.get("stage", "?")
    head = HEADING.get(stage, stage.upper())
    print(f"\n[{step.get('seq'):>2}] {head:<16} {step.get('at_ms'):>6} ms   "
          f"{step.get('title')}")
    detail = step.get("detail") or {}
    if stage == "sql":
        print("      " + str(detail.get("sql", ""))[:1500])
        if detail.get("params"):
            print("      params: " + ", ".join(str(p) for p in detail["params"]))
        return
    for key, value in detail.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, dict)):
            print(f"      {key}:")
            show(value, "        ")
        else:
            print(f"      {key}: {value}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question")
    ap.add_argument("--case", help="restrict the scope to one case id")
    ap.add_argument("--deadline", type=int, default=0,
                    help="seconds before the run is cut off; 0 means no limit")
    ap.add_argument("--json", action="store_true", help="print the raw trace")
    ap.add_argument("--local", action="store_true",
                    help="plan on the local model, leaving the cloud quota alone")
    args = ap.parse_args()
    # Applied AFTER load_dotenv, which overrides the process environment, and
    # never by editing .env.
    if args.local:
        os.environ["EVIDENCE_PLANNER_CLOUD"] = "false"

    conn = await asyncpg.connect(os.getenv("EVIDENCE_INGEST_DATABASE_URL"))
    for name in ("json", "jsonb"):
        await conn.set_type_codec(name, encoder=json.dumps, decoder=json.loads,
                                  schema="pg_catalog")
    cases = ([args.case] if args.case else
             [r["c"] for r in await conn.fetch(
                 "SELECT DISTINCT unnest(case_ids) c FROM evidence.documents "
                 "WHERE source_metadata->>'endpoint'='fir'")])
    await conn.close()

    scope = Scope(user_id=uuid4(), session_id=uuid4(), case_ids=cases)
    print(RULE)
    print(f"QUESTION : {args.question}")
    print(f"SCOPE    : {len(cases)} case(s)")
    print(RULE)

    seen = 0

    async def progress(value):
        """Print each step the moment it is recorded."""
        nonlocal seen
        steps = (value or {}).get("steps") or []
        for step in steps[seen:]:
            render(step)
            sys.stdout.flush()
        seen = max(seen, len(steps))

    started = time.monotonic()
    runtime = Runtime(ScopedStore(scope))
    answer = await runtime.run(uuid4(), args.question, scope,
                               deadline=(time.monotonic() + args.deadline
                                         if args.deadline else None),
                               progress=progress)
    data = answer.model_dump(mode="json")
    health = answer.health or {}

    print("\n" + RULE)
    print(f"STATUS   : {data.get('status')}")
    print(f"CLAIMS   : {len(data.get('claims') or [])}")
    print(f"SECONDS  : {time.monotonic()-started:.1f}")
    if health.get("failure"):
        print(f"FAILURE  : {health['failure']}  (stage: {health.get('stage')})")
    print(RULE)
    print(data.get("text") or "(no text)")
    print(RULE)

    if args.json:
        print(json.dumps(health.get("trace") or [], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
