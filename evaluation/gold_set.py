"""
Gold set for RAG evaluation — queries spanning all routes and sub-agents,
each with a ground-truth answer DERIVED FROM THE RAW MUHAFIZ DATA API
(the structured source the app ingests from), NOT from the app itself.

This is deliberately non-circular: ground truth = raw structured facts
(cases/roznamcha/cms/pkm/criminal-records endpoints); system-under-test =
the RAG pipeline's natural-language answer about those facts. Comparing the
two measures faithfulness/correctness of generation, not the app vs itself.

Each gold item carries:
  - id, route, subagent   : coverage bookkeeping
  - query                 : what we send to /api/chat
  - case_id               : anchor case (exists in both API and app DB), or None
  - expected_facts        : the verifiable ground-truth facts (from the API)
  - expected_answer       : a reference NL answer built from those facts
  - retrieval_context     : the raw source text the answer should be grounded in
  - eval_notes            : what a correct answer must contain / must not claim

The builder fetches live ground truth from the API so expected_facts reflect
the real record, not a hand-typed guess that could drift.

Run:  .venv/Scripts/python.exe evaluation/gold_set.py   # writes gold_set.json
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_set.json")


# ── Query design: route × sub-agent coverage ────────────────────────────────
# Each entry names the ROUTE it should classify as and the SUB-AGENT that
# should handle it under the harness, so the report can show coverage. The
# `fetch` field names an API-derived ground-truth builder run at build time.

GOLD_TEMPLATES = [
    # DIRECT — general knowledge / conversational, no retrieval, no Verifier gate
    {
        "id": "direct-01", "route": "DIRECT", "subagent": "(none — legacy DIRECT)",
        "query": "What does FIR stand for and what is it, in one sentence?",
        "case_id": None, "fetch": None,
        "expected_answer": "FIR stands for First Information Report — the initial written record police make of a reported cognizable offence.",
        "eval_notes": "General knowledge. Should answer directly, not abstain, not fabricate case-specific detail.",
    },
    # RAG — semantic search over case evidence
    {
        "id": "rag-01", "route": "RAG", "subagent": "Semantic Search",
        "query": "In case fir-891-24, what weapon was involved?",
        "case_id": "fir-891-24", "fetch": ("fir", "fir-891-24", "weapon"),
        "eval_notes": "Must state the weapon from weapon_register (a .30 bore pistol). Must not invent a different weapon.",
    },
    {
        "id": "rag-02", "route": "RAG", "subagent": "Semantic Search",
        "query": "Who is the complainant in case fir-891-24?",
        "case_id": "fir-891-24", "fetch": ("fir", "fir-891-24", "complainant"),
        "eval_notes": "Must name the complainant from complainant_full_name. Name fidelity matters.",
    },
    # RAG — reference / global KB (procedural)
    {
        "id": "rag-global-01", "route": "RAG", "subagent": "Semantic Search",
        "query": "What is the procedure for registering an FIR under Pakistani law?",
        "case_id": None, "fetch": None,
        "expected_answer": "An FIR for a cognizable offence is registered by police on receiving information; it is recorded, signed, and a copy given to the informant free of charge.",
        "eval_notes": "Global KB / reference. Should ground in reference material, not a specific case.",
    },
    # GRAPH — within-case entity/relationship
    {
        "id": "graph-01", "route": "GRAPH", "subagent": "Case Summarization",
        "query": "Who are the people involved in case fir-891-24?",
        "case_id": "fir-891-24", "fetch": ("fir", "fir-891-24", "people"),
        "eval_notes": "Should list complainant + accused from the record. Must not pull people from other cases.",
    },
    # GRAPH_HYBRID — graph + vector fused
    {
        "id": "graphhybrid-01", "route": "GRAPH_HYBRID", "subagent": "Case Summarization",
        "query": "Summarize case fir-891-24: what happened, who was involved, and what was recovered?",
        "case_id": "fir-891-24", "fetch": ("fir", "fir-891-24", "summary"),
        "eval_notes": "End-to-end case summary. Must be faithful to the record across all three aspects.",
    },
    # Timeline Building
    {
        "id": "timeline-01", "route": "GRAPH", "subagent": "Timeline Building",
        "query": "Build a timeline of events for case fir-891-24.",
        "case_id": "fir-891-24", "fetch": ("fir", "fir-891-24", "timeline"),
        "eval_notes": "Must include the incident datetime (2024-09-14 22:00). Dated events only; no invented dates.",
    },
    # Investigative Analysis
    {
        "id": "invanalysis-01", "route": "GRAPH_HYBRID", "subagent": "Investigative Analysis",
        "query": "What are the key investigative leads in case fir-891-24 based on the evidence?",
        "case_id": "fir-891-24", "fetch": ("fir", "fir-891-24", "summary"),
        "eval_notes": "Analytical. Must ground claims in evidence; hedge where evidence is thin; not fabricate leads.",
    },
    # Data-Quality / Extraction-Coverage
    {
        "id": "dataquality-01", "route": "DIRECT", "subagent": "Data-Quality/Extraction-Coverage",
        "query": "How complete is the extracted data for case fir-891-24? Are there any witnesses recorded?",
        "case_id": "fir-891-24", "fetch": ("fir", "fir-891-24", "coverage"),
        "eval_notes": "Ground truth: 0 witnesses, 1 accused. Should report coverage honestly, not invent witnesses.",
    },
    # Report Drafting (file output)
    {
        "id": "report-01", "route": "GRAPH_HYBRID", "subagent": "Report Drafting",
        "query": "Draft a brief case report for fir-891-24 as a document.",
        "case_id": "fir-891-24", "fetch": ("fir", "fir-891-24", "summary"),
        "eval_notes": "Report Drafting path. Content must be faithful; a PDF/file output is expected.",
    },
    # Abstention — a question with no answer in the data (correctness = refusing)
    {
        "id": "abstain-01", "route": "RAG", "subagent": "Semantic Search",
        "query": "In case fir-891-24, what was the DNA lab result and the getaway car's license plate?",
        "case_id": "fir-891-24", "fetch": None,
        "expected_answer": "The evidence does not contain a DNA lab result or a getaway car license plate for this case.",
        "eval_notes": "CORRECTNESS = ABSTAINING. There is no such data. Must NOT fabricate a DNA result or plate.",
    },
    # SQL — reference data lookup
    {
        "id": "sql-01", "route": "SQL", "subagent": "(legacy SQL route)",
        "query": "What PPC section covers robbery?",
        "case_id": None, "fetch": None,
        "expected_answer": "Robbery is covered under PPC Section 392.",
        "eval_notes": "Reference lookup. 392 is the robbery section (matches fir-891-24's own section).",
    },

    # ── Cross-case routes — require supervisor role (role="supervisor") ──
    # XGRAPH — cross-case link/traversal
    {
        "id": "xgraph-01", "route": "XGRAPH", "subagent": "Cross-Case Linkage",
        "role": "supervisor",
        "query": "Are there any suspects who appear across more than one case? Show the links.",
        "case_id": None, "fetch": None,
        "expected_answer": "Cross-case identity links exist where the same CNIC appears in multiple FIRs; the answer should list linked cases and hedge unconfirmed links.",
        "eval_notes": "Cross-case. Must only assert confirmed (SAME_AS) links as fact; hedge inferred ones. Investigator role would be DENIED — this uses supervisor.",
    },
    # XAGG — cross-case aggregate
    {
        "id": "xagg-01", "route": "XAGG", "subagent": "Large-Scale Aggregate",
        "role": "supervisor",
        "query": "How many cases involve the Arms Ordinance across all cases? Give a count.",
        "case_id": None, "fetch": ("agg", "arms-ordinance", "count"),
        "eval_notes": "Aggregate. Must state a total count. Ground truth computed from the DB at build time.",
    },
    # XNETWORK — cross-case network/theme synthesis
    {
        "id": "xnetwork-01", "route": "XNETWORK", "subagent": "Meta-Analysis",
        "role": "supervisor",
        "query": "What are the common themes or patterns across the cases in the system?",
        "case_id": None, "fetch": None,
        "expected_answer": "A thematic synthesis over community-clustered cases (e.g. arms/robbery patterns), grounded in the cluster summaries.",
        "eval_notes": "Open-ended synthesis over precomputed clusters. Must ground themes in real clusters, not invent patterns.",
    },
    # Local Search — entity-centric within/near a case
    {
        "id": "localsearch-01", "route": "RAG", "subagent": "Local Search",
        "query": "Tell me everything about the accused شہزیب in case fir-891-24.",
        "case_id": "fir-891-24", "fetch": ("fir", "fir-891-24", "people"),
        "eval_notes": "Entity-centric. Must ground in the accused record (full_name شہزیب عرف شابی). Name fidelity.",
    },
    # Global Search — reference/global synthesis
    {
        "id": "globalsearch-01", "route": "RAG", "subagent": "Global Search",
        "role": "supervisor",
        "query": "Across all cases, summarize the types of crimes being investigated.",
        "case_id": None, "fetch": None,
        "expected_answer": "A synthesis of crime categories across cases (PPC robbery/theft, Arms Ordinance, etc.), grounded in the case records.",
        "eval_notes": "Global synthesis. Must reflect actual crime categories present, not fabricate categories.",
    },
]


async def _fetch_aggregate_truth(spec: tuple) -> dict:
    """Compute an aggregate ground-truth directly from the app DB (the
    ingested truth), for cross-case count questions."""
    from sqlalchemy import text
    from src.database.postgres import AsyncSessionLocal
    _, kind, _ = spec
    async with AsyncSessionLocal() as db:
        if kind == "arms-ordinance":
            n = (await db.execute(text(
                "SELECT count(*) FROM cases WHERE crime_category ILIKE '%Arms Ordinance%'"
            ))).scalar()
            return {"aspect": "count", "arms_ordinance_case_count": n}
    return {"aspect": "count"}


async def _fetch_ground_truth(client, spec: tuple) -> dict:
    """Fetch the raw record and extract the verifiable facts for a query."""
    endpoint, record_id, aspect = spec
    if endpoint == "agg":
        return await _fetch_aggregate_truth(spec)
    rec = await client.get_one(endpoint, record_id)
    facts: dict = {"aspect": aspect}
    if aspect == "weapon":
        wr = rec.get("weapon_register") or []
        facts["weapons"] = [w.get("item_detail") for w in wr] if isinstance(wr, list) else wr
    elif aspect == "complainant":
        facts["complainant_full_name"] = rec.get("complainant_full_name")
        facts["complainant_cnic"] = rec.get("complainant_cnic")
    elif aspect in ("people", "summary"):
        facts["complainant_full_name"] = rec.get("complainant_full_name")
        acc = rec.get("fir_accused") or []
        facts["accused"] = [a.get("full_name") for a in acc if a.get("full_name")] if isinstance(acc, list) else acc
        wr = rec.get("weapon_register") or []
        facts["weapons"] = [w.get("item_detail") for w in wr] if isinstance(wr, list) else wr
        secs = rec.get("fir_section") or []
        facts["sections"] = [f"{s.get('section_code')} {s.get('act')}" for s in secs] if isinstance(secs, list) else secs
        facts["location"] = rec.get("crime_scene_location")
        facts["incident_datetime"] = rec.get("incident_datetime")
    elif aspect == "timeline":
        facts["incident_datetime"] = rec.get("incident_datetime")
        facts["report_datetime"] = rec.get("report_datetime")
    elif aspect == "coverage":
        wit = rec.get("fir_witness") or []
        acc = rec.get("fir_accused") or []
        facts["witness_count"] = len(wit) if isinstance(wit, list) else 0
        facts["accused_count"] = len(acc) if isinstance(acc, list) else 0
    # a compact ground-truth text for use as retrieval_context / reference
    facts["source_narrative"] = (rec.get("narrative_text") or "")[:800]
    return facts


async def build() -> list[dict]:
    from src.data_gateway.muhafiz_api.client import MuhafizApiClient
    gold = []
    async with MuhafizApiClient() as client:
        for t in GOLD_TEMPLATES:
            item = dict(t)
            if t.get("fetch"):
                item["expected_facts"] = await _fetch_ground_truth(client, t["fetch"])
                item["retrieval_context"] = item["expected_facts"].get("source_narrative", "")
                # derive a reference answer from the facts if none hand-written
                if "expected_answer" not in item:
                    item["expected_answer"] = _reference_answer(item["expected_facts"])
            else:
                item["expected_facts"] = {}
                item["retrieval_context"] = item.get("expected_answer", "")
            item.pop("fetch", None)
            gold.append(item)
    return gold


def _reference_answer(facts: dict) -> str:
    a = facts.get("aspect")
    if a == "weapon":
        w = facts.get("weapons") or []
        return f"The weapon involved was: {', '.join(str(x) for x in w) if w else 'none recorded'}."
    if a == "complainant":
        return f"The complainant is {facts.get('complainant_full_name')}."
    if a in ("people", "summary"):
        parts = []
        if facts.get("complainant_full_name"): parts.append(f"complainant {facts['complainant_full_name']}")
        if facts.get("accused"): parts.append(f"accused {', '.join(str(x) for x in facts['accused'])}")
        if facts.get("weapons"): parts.append(f"weapon(s) {', '.join(str(x) for x in facts['weapons'])}")
        if facts.get("sections"): parts.append(f"sections {', '.join(facts['sections'])}")
        return "Involved: " + "; ".join(parts) + "."
    if a == "timeline":
        return f"The incident occurred at {facts.get('incident_datetime')}; the report was recorded at {facts.get('report_datetime')}."
    if a == "coverage":
        return f"The record has {facts.get('accused_count')} accused and {facts.get('witness_count')} witnesses recorded."
    if a == "count":
        return f"There are {facts.get('arms_ordinance_case_count')} cases involving the Arms Ordinance."
    return ""


if __name__ == "__main__":
    gold = asyncio.run(build())
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(gold, f, ensure_ascii=False, indent=2)
    routes = {}
    for g in gold:
        routes.setdefault(g["route"], 0)
        routes[g["route"]] += 1
    print(f"wrote {len(gold)} gold items to {OUT}")
    print("route coverage:", dict(sorted(routes.items())))
    print("sub-agents:", sorted({g["subagent"] for g in gold}))
