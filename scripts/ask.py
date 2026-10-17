"""Run questions through the new retrieval architecture, showing every step."""
from __future__ import annotations
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncpg
from dotenv import load_dotenv
load_dotenv(".env")
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
from src.retrieval.engine import dsn, run

QUESTIONS = sys.argv[1:] or [
    "hello",
    "How many cases are registered?",
    "How many persons have a criminal risk score above 80?",
    "What does CNSA 1997 mean?",
    "How many weapons are recorded?",
    "How many accused persons are linked to cases?",
]

async def main():
    conn = await asyncpg.connect(dsn())
    try:
        for q in QUESTIONS:
            r = await run(conn, q)
            p = r["plan"]
            print("=" * 72)
            print(f"Q: {q}")
            print(f"   {p.summary()}")
            for b in p.bindings[:4]:
                v = f" = {b.value!r}" if b.value else ""
                print(f"   bind: {b.term!r} -> {b.table}.{b.column}{v} "
                      f"({b.match_kind} {b.score})")
            for reason in p.escalation.reasons():
                print(f"   escalate: {reason}")
            for note in p.coverage_notes[:2]:
                print(f"   coverage: {note[:110]}")
            if r["sql"]:
                print("   SQL: " + r["sql"].replace("\n", "\n        "))
            if r["error"]:
                print(f"   ERROR: {r['error']}")
            print(f"   ANSWER: {r['answer']}")
    finally:
        await conn.close()

asyncio.run(main())
