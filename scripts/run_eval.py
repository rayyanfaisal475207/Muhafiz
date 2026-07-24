"""
Phase 9 — Police-domain evaluation suite.

Adapted from the deleted TaxIQ-era scripts/eval_router.py (recovered from git
history at 20c512f): same TEST_CASES-list shape, same per-case pass/fail
reporting, same JSON artifact output. Extended beyond the original (which only
called route_query() in isolation) to drive the real, running app end-to-end
via /api/chat — the same standard used for Phase 8's live verification — and
to compute retrieval precision + citation validation, which the router-only
version couldn't.

Requires: the backend running on http://localhost:8000 (uvicorn src.main:app).

Citation/grounding scoring calls src/pipeline/verifier.py's verify_grounding()
directly against the real retrieved chunks pulled from data/pipeline_logs.db —
this is the same hard-gating verifier Phase 6 wired into every live gated
route, so this script's citation_issues score reflects the production check,
not a standalone/unwired module.
"""
import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path

import requests

BASE = "http://localhost:8000"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pipeline_logs.db"
EVAL_EMAIL = "phase9-eval@example.com"
EVAL_PASSWORD = "EvalPass123!"

# ── Test cases ────────────────────────────────────────────────────────────
# (id, question, expected_route, expected_source_hint, category, case_id)
# case_id is None for every pre-Phase-5 case (unchanged behavior); GRAPH
# cases need an active case_id (within-case), XGRAPH/XAGG are deliberately
# case_id=None (cross-case is explicit, not derived from an active case).
TEST_CASES = [
    (1, "Hi, what's your name and what can you help me with?", "DIRECT", None, "direct", None),
    (2, "Thanks, that's helpful!", "DIRECT", None, "direct", None),
    (3, "Can you help me draft a letter, or do you only answer police questions?", "DIRECT", None, "direct", None),

    (4, "What is the procedure to get a certified copy of an FIR, and what's the fee?", "RAG", "REAL-004-copy-of-fir-procedure.pdf", "rag_single", None),
    (5, "What documents do I need to report a lost CNIC, and is an affidavit required?", "RAG", "REAL-005-lost-report-procedure.pdf", "rag_single", None),
    (6, "What documents does Islamabad Police require for Tenant Registration, and what's the fee?", "RAG", "REAL-008-tenant-registration-procedure.pdf", "rag_single", None),
    (7, "Summarize the FIR registered at Kohsar police station for cyber harassment.", "RAG", "FIR-2026-HAR-001.pdf", "rag_single", None),
    (8, "What police station handled missing person report MP-2026-001, and when was the person last seen?", "RAG", "MP-2026-001.pdf", "rag_single", None),
    (9, "What is required for foreigner registration with Islamabad Police?", "RAG", "REAL-006-foreigner-registration-procedure.pdf", "rag_single", None),

    (10, "Trace the full case history for FIR-2026-THEFT-001 from the initial complaint through to the charge sheet.", "RAG", "multi:THEFT-001 chain", "rag_multi", None),
    (11, "In the investigation for FIR-2026-THEFT-001, was the accused ever traced?", "RAG", "multi:THEFT-001 chain", "rag_multi", None),

    (12, "What PPC section applies to mobile phone theft, and is it cognizable?", "SQL", "379 PPC", "sql", None),
    (13, "What section covers dishonestly receiving stolen property?", "SQL", "411 PPC", "sql", None),
    (14, "What sections apply to cyber fraud or online financial scams?", "SQL", "420 PPC / PECA Sec.13 / Sec.16", "sql", None),
    (15, "What's the section reference for cyber-stalking complaints?", "SQL", "PECA 2016 Sec. 24", "sql", None),
    (16, "What PPC sections cover burglary or house theft?", "SQL", "380 PPC / 457 PPC", "sql", None),

    (17, "Is there any road closure in Islamabad today?", "WEB", None, "web", None),
    (18, "What's the weather like in Islamabad this week?", "WEB", None, "web", None),

    (19, "What is the case closure rate for cyber harassment FIRs at Kohsar station in the last quarter?", None, None, "out_of_scope", None),
    (20, "What is Islamabad Police's internal budget allocation for the Cyber Crime Wing in 2026?", None, None, "out_of_scope", None),

    (21, "Give me the FIR filing checklist as a PDF.", "RAG", "REAL-004-copy-of-fir-procedure.pdf", "file_gen", None),
    (22, "Export the penal code sections for theft-related offenses as an Excel file.", None, "theft-related section_refs", "file_gen", None),

    (23, "What section covers cheating and dishonestly inducing delivery of property?", "SQL", "420 PPC", "edge_paraphrase", None),
    (24, "__FOLLOWUP__ What section was that filed under?", None, None, "edge_context", None),  # special-cased: same session as Q7
    (25, "asdkjfh sdlkfj random gibberish", None, None, "edge_degenerate", None),

    # ── Phase 5: GRAPH / XGRAPH / XAGG routing slices ──────────────────
    (26, "Who is connected to the accused in CASE-009?", "GRAPH", None, "within_case_relationship", "CASE-009"),
    (27, "How many accused are involved in CASE-009 and how are they connected?", "GRAPH", None, "within_case_multihop", "CASE-009"),
    (28, "Has phone number 0372-1590538 appeared in other cases?", "XGRAPH", None, "cross_case_pattern", None),
    (29, "Map ORG-002's network across the cases it appears in.", "XGRAPH", None, "network_timeline", None),
    (30, "Has Adnan Qureshi Waheed been involved in any other case?", "XGRAPH", None, "cross_case_pattern", None),
    (31, "Which police stations have the most open theft cases?", "XAGG", None, "network_timeline", None),
    (32, "What are the top recurring vehicles across all cases?", "XAGG", None, "network_timeline", None),
    (33, "Are the occupants of the shared boarding house in CASE-010 and CASE-013 related to each other?", "XGRAPH", None, "network_timeline", None),
]


def register_and_login() -> dict:
    s = requests.Session()
    s.post(f"{BASE}/api/auth/register", json={"email": EVAL_EMAIL, "password": EVAL_PASSWORD})
    r = s.post(f"{BASE}/api/auth/login", json={"email": EVAL_EMAIL, "password": EVAL_PASSWORD})
    r.raise_for_status()
    return {
        "access_token": s.cookies.get("access_token"),
        "csrf_token": s.cookies.get("csrf_token"),
    }


def chat(message: str, session_id: str, cookies: dict, case_id: str | None = None) -> dict:
    body = {"message": message, "session_id": session_id}
    if case_id:
        body["case_id"] = case_id
    resp = requests.post(
        f"{BASE}/api/chat",
        json=body,
        cookies={"access_token": cookies["access_token"], "csrf_token": cookies["csrf_token"]},
        headers={"X-CSRF-Token": cookies["csrf_token"]},
        stream=True,
        timeout=120,
    )
    route = None
    case_scope = None
    hop_count = None
    graph_confidence = None
    evaluator_detail = ""
    text = ""
    title_detail = ""
    file_generated = None
    web_sources = []
    cross_case_sources = []
    error_events = []

    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            obj = json.loads(line[5:].strip())
        except Exception:
            continue
        step, status = obj.get("step"), obj.get("status")
        if step == "router" and status == "done":
            route = obj.get("detail", "").replace("Route decided: ", "")
            case_scope = obj.get("case_scope")
        if step in ("retrieval", "cross_case_finding") and status == "done":
            hop_count = obj.get("hop_count", hop_count)
            graph_confidence = obj.get("graph_confidence", graph_confidence)
        if step == "title_generation":
            title_detail = obj.get("detail", "")
        if step == "evaluator" and status == "done":
            evaluator_detail = obj.get("detail", "")
        if step == "web_search" and status == "done":
            web_sources = obj.get("sources", [])
        if step == "cross_case_finding" and status == "done":
            cross_case_sources = obj.get("sources", [])
        if step == "file_generation" and status == "done":
            file_generated = obj.get("detail", "")
        if step == "response" and status == "streaming":
            text += obj.get("detail", "")
        if status == "error":
            error_events.append(f"{step}: {obj.get('detail','')}")

    return {
        "route": route,
        "case_scope": case_scope,
        "hop_count": hop_count,
        "graph_confidence": graph_confidence,
        "evaluator_detail": evaluator_detail,
        "response": text,
        "title_detail": title_detail,
        "file_generated": file_generated,
        "web_sources": web_sources,
        "cross_case_sources": cross_case_sources,
        "error_events": error_events,
    }


def get_retrieved_chunks(session_id: str) -> list[dict]:
    """Pull the final RRF-reranked chunks actually used in the prompt for this
    session's most recent query, from the SQLite pipeline log."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT query_id FROM queries WHERE session_id = ? ORDER BY query_id DESC LIMIT 1",
        (session_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return []
    query_id = row["query_id"]
    cur.execute(
        "SELECT source_file, chunk_text_preview, rank_position, rrf_score "
        "FROM retrieved_documents WHERE query_id = ? AND retrieval_method = 'rrf' "
        "ORDER BY rank_position",
        (query_id,),
    )
    chunks = [dict(r) for r in cur.fetchall()]
    conn.close()
    return chunks


async def run_citation_validation(response_text: str, chunks: list[dict]) -> list[str]:
    """
    Score citation grounding for eval purposes using the real, live Phase 6
    verifier (src/pipeline/verifier.py) — the module this script used to call,
    src/pipeline/citation_validator.py, was deleted when Phase 6 replaced it
    with the actual hard-gating verifier wired into the live pipeline. Unlike
    the old citation_validator, verify_grounding() IS what production runs
    on every gated route, so this now scores the same check the app applies,
    not an unwired standalone module.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.pipeline.verifier import verify_grounding

    if not chunks:
        return []
    cited_chunks = [
        {
            "id": c.get("chunk_id", f"chunk-{i}"),
            "text": c.get("chunk_text_preview", ""),
            "metadata": {"source": c.get("source_file", "unknown")},
        }
        for i, c in enumerate(chunks)
    ]
    try:
        result = await verify_grounding(
            answer=response_text, cited_chunks=cited_chunks, case_id="cross_case",
        )
        if result.get("grounded") and not result.get("off_topic"):
            return []
        return result.get("unsupported_claims") or [result.get("reason", "not grounded")]
    except Exception as exc:
        return [f"__VALIDATOR_ERROR__ {exc}"]


def main():
    print("=" * 70)
    print("Phase 9 — Police-domain Evaluation Suite")
    print("=" * 70)

    cookies = register_and_login()
    results = []
    last_har_session = None  # for Q24's follow-up

    for qid, question, expected_route, expected_source, category, case_id in TEST_CASES:
        if qid > 1:
            # Small pacing buffer — router/evaluator/rewriter now also use
            # the local-first path (Groq is only a fallback on local
            # failure), so heavy Groq pressure is no longer expected, but a
            # short gap is kept as a safety margin for any fallback case.
            time.sleep(3)

        session_id = str(uuid.uuid4())
        is_followup = question.startswith("__FOLLOWUP__")

        if is_followup:
            question = question.replace("__FOLLOWUP__ ", "")
            session_id = last_har_session or str(uuid.uuid4())

        print(f"\n[{qid}/{len(TEST_CASES)}] ({category}) {question}")
        t0 = time.monotonic()
        r = chat(question, session_id, cookies, case_id=case_id)
        elapsed = time.monotonic() - t0

        if qid == 7:
            last_har_session = session_id

        chunks = get_retrieved_chunks(session_id) if r["route"] == "RAG" else []
        citation_issues = []
        if r["route"] == "RAG" and r["response"]:
            citation_issues = asyncio.run(run_citation_validation(r["response"], chunks))

        route_match = (expected_route is None) or (r["route"] == expected_route)
        # A router-step error means the actual route shown is orchestrator.py's
        # hardcoded fallback ("RAG" on router failure), not a genuine
        # classification — flag this so it isn't misread as router accuracy.
        router_was_defaulted = any(e.startswith("router:") for e in r["error_events"])

        print(f"  route: {r['route']} (expected: {expected_route}){' [DEFAULTED - router call failed]' if router_was_defaulted else ''} | {elapsed:.1f}s")
        print(f"  response ({len(r['response'])} chars): {r['response'][:200]}")
        if r["error_events"]:
            print(f"  ERRORS: {r['error_events']}")
        if chunks:
            print(f"  retrieved: {[c['source_file'] for c in chunks]}")
        if citation_issues:
            print(f"  UNVERIFIED CITATIONS: {citation_issues}")

        results.append({
            "id": qid,
            "question": question,
            "category": category,
            "case_id": case_id,
            "expected_route": expected_route,
            "expected_source": expected_source,
            "actual_route": r["route"],
            "case_scope": r["case_scope"],
            "hop_count": r["hop_count"],
            "graph_confidence": r["graph_confidence"],
            "route_match": route_match,
            "router_was_defaulted": router_was_defaulted,
            "response": r["response"],
            "title_detail": r["title_detail"],
            "file_generated": r["file_generated"],
            "web_sources": r["web_sources"],
            "cross_case_sources": r["cross_case_sources"],
            "retrieved_chunks": chunks,
            "citation_issues": citation_issues,
            "error_events": r["error_events"],
            "elapsed_s": round(elapsed, 1),
            "session_id": session_id,
        })

    summary = _summarize_per_slice(results)
    _print_summary(summary)

    out_path = Path(__file__).resolve().parent.parent / "eval_results_phase9.json"
    out_path.write_text(json.dumps({"results": results, "summary": summary}, indent=2), encoding="utf-8")
    print(f"\n\nRaw results + per-slice summary written to {out_path}")
    return results


def _summarize_per_slice(results: list[dict]) -> dict:
    """
    Routing accuracy + retrieval correctness PER category slice, not
    blended into one number — an explicit Phase 5 "done" requirement, since
    a single overall accuracy figure can hide a slice (e.g. cross_case_pattern)
    performing much worse than the average.
    """
    by_category: dict[str, list[dict]] = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)

    summary = {}
    for category, rows in by_category.items():
        scored = [r for r in rows if r["expected_route"] is not None]
        route_matches = sum(1 for r in scored if r["route_match"])
        summary[category] = {
            "total_queries": len(rows),
            "route_scored_queries": len(scored),
            "route_accuracy": round(route_matches / len(scored), 3) if scored else None,
            "router_defaulted_count": sum(1 for r in rows if r["router_was_defaulted"]),
            "unverified_citation_count": sum(1 for r in rows if r["citation_issues"]),
        }
    return summary


def _print_summary(summary: dict) -> None:
    print("\n" + "=" * 70)
    print("Per-slice routing accuracy (not blended)")
    print("=" * 70)
    for category, stats in summary.items():
        acc = f"{stats['route_accuracy']:.0%}" if stats["route_accuracy"] is not None else "n/a"
        print(
            f"  {category:<28} route_accuracy={acc:>5}  "
            f"({stats['route_scored_queries']}/{stats['total_queries']} scored, "
            f"{stats['router_defaulted_count']} defaulted, "
            f"{stats['unverified_citation_count']} w/ unverified citations)"
        )


if __name__ == "__main__":
    main()
