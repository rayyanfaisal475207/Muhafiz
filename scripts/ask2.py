"""Ask questions end to end: fast path, or agent when the planner escalates."""
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

async def main():
    qs = sys.argv[1:]
    conn = await asyncpg.connect(dsn())
    try:
        for q in qs:
            r = await run(conn, q)
            p = r["plan"]
            print("=" * 74)
            print(f"Q: {q}")
            print(f"   route: {p.route.value} | tables={p.tables or '-'}")
            if r.get("escalated"):
                print(f"   escalated: {'; '.join(r['escalated'])[:150]}")
            ag = r.get("agent")
            if ag:
                for s in ag.steps:
                    print(f"   [{s.turn}] {s.tool}({s.argument[:60]}) ok={s.ok}")
                for e in ag.evidence():
                    print(f"   QUERY({e['rows']} rows, {e['ms']}ms): {e['sql'][:120]}")
            e = r.get("enhanced")
            if e is not None:
                print(f"   enhanced: {e.summary()}")
            for p_ in (r.get("passages") or [])[:2]:
                print(f"   PASSAGE span={p_.span_id} [{p_.score}] {p_.brief(90)}")
            if r.get("retrieval_note"):
                print(f"   retrieval: {r['retrieval_note'][:90]}")
            if r.get("sql"):
                print("   SQL: " + r["sql"].replace("\n", " "))
            if r.get("refusal"):
                print(f"   REFUSE: {r['refusal'][:200]}")
            h = r.get("health") or {}
            if h.get("status") and h["status"] != "healthy":
                print(f"   HEALTH: {h['status']}")
                for ev in h.get("events", []):
                    print(f"      - {ev['component']} ({ev['severity']}): "
                          f"{ev['consequence'][:80]}")
            if r.get("system_message"):
                print("   TO USER: " + r["system_message"].replace(chr(10), " ")[:200])
            print(f"   ANSWER: {str(r['answer'])[:400]}")
    finally:
        await conn.close()

asyncio.run(main())
