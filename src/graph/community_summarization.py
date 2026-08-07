# ============================================================
# Community summarization — GraphRAG-inspired layer, Section 2 (additive).
# Consumes the output of community_detection.py and writes LLM-generated
# summaries into community_reports (migration 016).
#
# Reuses src/llm/client.py::call_llm unchanged, via the shared
# call_llm_json retry helper (src/pipeline/json_extract.py) every other
# JSON-output pipeline stage already uses — no new LLM integration surface.
# role="generation" — this is prose synthesis, the same shape of task as
# final answer generation, not classification/routing.
#
# Summaries are stored in English regardless of source-document language:
# these are precomputed background artifacts, not a response to a specific
# query — XNETWORK's own synthesis call is what respects the querying
# user's language, the same way RAG's response-generation step already
# translates grounded English/Urdu source material into the query's
# language.
# ============================================================

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from src.database.postgres import get_session
from src.graph import community_detection
from src.ingestion.script_detector import _ARABIC_SCRIPT
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "community_summarizer.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_SCHEMA_HINT = '"summary", "excluded_non_names"'


async def _fetch_case_metadata(case_ids: list[str]) -> list[dict]:
    if not case_ids:
        return []
    async with get_session() as db:
        res = await db.execute(
            text(
                "SELECT case_id, fir_number, crime_category, police_station "
                "FROM cases WHERE case_id = ANY(:case_ids)"
            ),
            {"case_ids": case_ids},
        )
        return [dict(row) for row in res.mappings()]


def _validate(result) -> bool:
    # Live-observed: the local generation model sometimes wraps its answer
    # as {"summary": {"members": [...]}} — a nested dict instead of a
    # string. A bare "summary" in result check let that through, and
    # str(dict) then got stored verbatim as summary_text ("{'members':
    # [...]}") — a real, confirmed-live corruption, not a hypothetical one.
    # Require summary to actually be a string (NOT_ENOUGH_DATA included).
    if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
        return False
    summary = result["summary"].strip()
    if not summary:
        return False
    # Also live-observed: despite the prompt's explicit "write in English"
    # instruction, the local generation model (Qalb-8B, Urdu-tuned) sometimes
    # answers in Urdu script anyway — the same class of instruction-following
    # gap D-2/Fix 7 found in query_expander.py, just for language choice
    # rather than script parity. Reject and retry rather than silently
    # storing a summary in the wrong language (design decision:
    # community_reports.summary_text is English-only, see module docstring,
    # for embedding consistency).
    #
    # Coverage-based, not presence-based: a first version of this check
    # rejected on ANY Arabic-script character, which live-confirmed rejects
    # a genuinely correct English summary that legitimately quotes a
    # person's real name in Urdu script inline ("... Zainab Akram Siddiqui
    # is a named individual ...") — the same way RAG's own English answers
    # correctly leave Urdu proper names untranslated. Only reject when
    # Arabic-script characters make up a large share of the summary,
    # indicating the whole sentence structure is Urdu, not just a quoted
    # name within English prose.
    non_space = [c for c in summary if not c.isspace()]
    if non_space:
        arabic_chars = sum(1 for c in non_space if _ARABIC_SCRIPT.match(c))
        if summary != "NOT_ENOUGH_DATA" and (arabic_chars / len(non_space)) > 0.3:
            return False
    return True


async def _summarize_one(
    community: dict, names_by_id: dict[str, str], known_stations: set[str],
    canonical_map: dict[str, str], contaminated_names: Optional[set[str]] = None,
) -> Optional[dict]:
    member_names = [names_by_id.get(eid, eid) for eid in community["member_entity_ids"]]
    cases = await _fetch_case_metadata(community["case_ids"])
    relationships = await community_detection.get_associated_with_context(
        set(community["member_entity_ids"]), canonical_map=canonical_map, names=names_by_id,
    )

    cases_text = "; ".join(
        f"{c['case_id']} (FIR {c['fir_number'] or 'n/a'}, {c['crime_category'] or 'unspecified'}, "
        f"{c['police_station'] or 'unspecified station'})"
        for c in cases
    ) or "no case metadata found"

    relationships_text = "; ".join(
        f"{r['person_1']} <-> {r['person_2']}"
        + (f" ({r['basis']})" if r.get("basis") else "")
        for r in relationships
    ) or "none given"

    user_message = (
        f"Members: {', '.join(member_names)}\n"
        f"Cases: {cases_text}\n"
        f"Relationships: {relationships_text}"
    )

    result, raw = await call_llm_json(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_message,
        temperature=0.0,
        max_tokens=600,
        role="generation",
        validate=_validate,
        schema_hint=_SCHEMA_HINT,
        # Live-confirmed (this session): the local generation model
        # (Qalb-8B) answers in Urdu script regardless of the prompt's
        # explicit "write in English" instruction on 25/26 communities
        # even after 3 local-only correction retries — the same class of
        # finding router.py's G-1 fix made ("the local model's output isn't
        # a reliable signal for this prompt shape"), here for language
        # choice rather than routing. Unlike router/evaluator/query_rewriter
        # (high-volume, once-or-more per user turn — the exact quota drain
        # that ruled out blanket cloud escalation there), community
        # summarization is an infrequent, admin-triggered batch job
        # (typically tens of communities per run, not per query) — a narrow
        # place where opting into one last-resort cloud attempt per
        # community is bounded and safe, matching the project's own
        # established pattern of scoping escalation to the specific call
        # site that needs it rather than a global default change.
        escalate_to_cloud_on_failure=True,
    )

    if result is None:
        logger.warning(
            "community_summarization: failed to summarize %s after retries — raw: %s",
            community["community_id"], raw[:200],
        )
        return None

    summary = str(result.get("summary") or "").strip()
    if not summary or summary == "NOT_ENOUGH_DATA":
        logger.info(
            "community_summarization: %s skipped (%s)",
            community["community_id"],
            "model reported insufficient plausible members" if summary else "empty summary",
        )
        return None

    # Mechanical safety net, layered on top of the prompt's own exclusion
    # instruction (which live-confirmed isn't airtight — community_reports
    # "0010"/"0022" both leaked a non-name into the written summary despite
    # it). Reuses the exact same plausibility check community_detection.py
    # applies at the graph level, applied here to every member name that
    # was actually fed to this prompt: any name that's implausible AND
    # wasn't in the model's own excluded_non_names AND still shows up
    # verbatim in the summary text is a leak the model didn't catch.
    # Detection + logging only, not automatic text-splicing — naive string
    # removal risks producing a grammatically broken sentence, which is a
    # worse failure than a logged-but-visible leak.
    excluded = set(result.get("excluded_non_names") or [])
    contaminated_names = contaminated_names or set()
    for name in member_names:
        if name in excluded:
            continue
        is_noise = not community_detection._is_plausible_person_name(name, known_stations) or name in contaminated_names
        if is_noise and name in summary:
            logger.warning(
                "community_summarization: %s — implausible name %r leaked into the "
                "summary text despite not being in excluded_non_names",
                community["community_id"], name,
            )

    return {
        "community_id": community["community_id"],
        "run_id": community["run_id"],
        "level": community.get("level", 0),
        "member_entity_ids": community["member_entity_ids"],
        "case_ids": community["case_ids"],
        "member_count": community["member_count"],
        "summary_text": summary,
        "excluded_non_names": result.get("excluded_non_names") or [],
    }


async def summarize_communities(min_members: int = community_detection.MIN_MEMBERS_FOR_SUMMARY) -> dict:
    """
    Summarize every community from the latest detection run with at least
    `min_members` members (a singleton isn't a network — see
    community_detection.MIN_MEMBERS_FOR_SUMMARY). Skips (does not write a
    report for) any community the LLM itself judges has too few plausible
    real names after excluding non-name entries (see
    prompts/community_summarizer.txt's NOT_ENOUGH_DATA path) — a second,
    prompt-level defense layered on top of community_detection.py's
    node-level filter, not a replacement for it.

    Returns {run_id, attempted, written, skipped}.
    """
    run = await community_detection.get_latest_run()
    if run is None:
        raise RuntimeError("No community detection run found — call detect_communities() first.")
    run_id = run["run_id"]

    # Clear stale embeddings from prior runs before writing this run's —
    # community_reports in Postgres is fully replaced each run (cascading
    # delete from community_runs), but the Chroma collection only ever
    # upserted, never removed anything. See clear_all_reports()'s own
    # docstring for the live-confirmed bug this closes.
    from src.retrieval.community_vector_store import clear_all_reports
    clear_all_reports()

    async with get_session() as db:
        res = await db.execute(
            text(
                "SELECT community_id, array_agg(entity_id) AS member_entity_ids, level "
                "FROM community_membership WHERE run_id = :run_id "
                "GROUP BY community_id, level HAVING count(*) >= :min_members"
            ),
            {"run_id": run_id, "min_members": min_members},
        )
        communities_raw = [dict(row) for row in res.mappings()]

    names_by_id = await community_detection.fetch_person_names()
    known_stations = await community_detection.fetch_known_police_stations()
    contaminated_names = community_detection._compute_prefix_contaminated_names(names_by_id)

    # Hoisted out of the per-community loop below — these three reads are
    # full-graph, run-wide data (not scoped to any one community), so they
    # were being re-fetched identically on every loop iteration. Fine at
    # today's ~25 communities/run, but a real O(communities) cost that
    # doesn't scale — flagged as a known inefficiency when this module was
    # first built, fixed here as its own scoped change. No behavior
    # change: same data, computed once instead of once per community.
    person_cases = await community_detection.fetch_person_case_membership()
    same_as_pairs = await community_detection.fetch_confirmed_same_as()
    canonical_map = community_detection.build_canonical_map(same_as_pairs)

    written = []
    skipped = 0
    for row in communities_raw:
        member_ids = row["member_entity_ids"]

        # Distinct-person gate, ahead of any LLM call — raw member_count
        # (what the SQL HAVING clause above already filtered on) over-
        # counts real people: a community with member_count=2 can still be
        # one person's two unresolved duplicate mentions, not a network of
        # two. See estimate_distinct_person_count()'s own docstring for
        # why this can't just be folded into the SQL query above (name
        # similarity isn't expressible as a HAVING clause).
        distinct_count = await community_detection.estimate_distinct_person_count(member_ids, names_by_id)
        if distinct_count < min_members:
            logger.info(
                "community_summarization: %s skipped — %d raw members but only "
                "%d distinct person(s) after name-similarity dedup (below min_members=%d).",
                row["community_id"], len(member_ids), distinct_count, min_members,
            )
            skipped += 1
            continue

        # case_ids per community aren't stored in community_membership
        # (that table is entity_id/community_id/level/run_id only, per
        # migration 016) — re-derive by joining person-case membership for
        # this community's specific members, same source detect_communities()
        # used when it computed case_ids the first time.
        member_set = set(member_ids)
        case_ids = sorted({
            case_id for entity_id, case_id in person_cases
            if community_detection.canon(canonical_map, entity_id) in member_set
        })

        community = {
            "community_id": row["community_id"],
            "run_id": run_id,
            "level": row["level"],
            "member_entity_ids": member_ids,
            "case_ids": case_ids,
            "member_count": len(member_ids),
        }
        summary = await _summarize_one(community, names_by_id, known_stations, canonical_map, contaminated_names)
        if summary is None:
            skipped += 1
            continue
        written.append(summary)

    if written:
        async with get_session() as db:
            for s in written:
                await db.execute(
                    text(
                        "INSERT INTO community_reports "
                        "(community_id, level, run_id, member_entity_ids, case_ids, member_count, summary_text) "
                        "VALUES (:community_id, :level, :run_id, :member_entity_ids, :case_ids, :member_count, :summary_text) "
                        "ON CONFLICT (community_id) DO UPDATE SET "
                        "summary_text = EXCLUDED.summary_text, updated_at = now()"
                    ),
                    {
                        "community_id": s["community_id"], "level": s["level"], "run_id": s["run_id"],
                        "member_entity_ids": s["member_entity_ids"], "case_ids": s["case_ids"],
                        "member_count": s["member_count"], "summary_text": s["summary_text"],
                    },
                )

        from src.retrieval.community_vector_store import upsert_community_reports
        await upsert_community_reports(written)

    logger.info(
        "Community summarization for run %s: %d attempted, %d written, %d skipped.",
        run_id, len(communities_raw), len(written), skipped,
    )
    return {"run_id": run_id, "attempted": len(communities_raw), "written": len(written), "skipped": skipped}
