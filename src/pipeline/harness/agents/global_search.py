"""
Global Search sub-agent — src/pipeline/harness/agents/global_search.py
(findings.md Module 9, "Global Search: whole-dataset map-reduce
reasoning" — Stage 1, "map-reduce over the existing flat level").

Source of truth: findings.md's Module 9 section (MS GraphRAG's Global
Search description, the confirmed "XNETWORK is top-k, not map-reduce"
gap, the staged Stage 1/Stage 2 proposal) and this session's own approved
plan. Modeled on local_search.py's/cross_case_linkage.py's conventions —
SUBAGENT_INTERFACES.md's contract shape, the
role-gate-lives-inside-the-composed-tool discipline (this sub-agent adds
NO role check of its own; see tools/global_search.py), the
self-contained-prompt-plus-verification pattern every prior sub-agent
follows.

SCOPE. Composes exactly ONE tool —
`src.pipeline.harness.tools.global_search.global_search_tool` — which
fetches EVERY community report for one hierarchy level (not a top-k
similarity cut). This module owns the actual map-reduce algorithm:

  MAP.  Cap the fetched report set at MAX_REPORTS_SAMPLE (a shuffled
        sample beyond that, for cost control — see findings.md's own
        "Design note — cost control" section; the real count this cap
        was sized against was 19 community reports, confirmed live this
        session). Partition into batches of MAP_BATCH_SIZE, SHUFFLE each
        batch (per the algorithm description — avoids position bias),
        and run one `call_llm_json()` per batch asking for a JSON list
        of importance-rated points relevant to the query, each tagged
        with which batch-local report(s) support it.
  REDUCE. Pool every batch's points, sort by importance, keep the top
        REDUCE_TOP_N_POINTS. Resolve each kept point's supporting
        report(s) back to real EvidenceChunk objects; the DISTINCT chunks
        referenced become the final [Document N] evidence list — the
        same convention every other sub-agent's generation+verification
        step uses. Generate the final answer from those chunks (with the
        extracted points as guidance for what to emphasize, never
        themselves crossing the SubAgentResult boundary as evidence —
        [PRESERVE — design §3]), verify through the EXISTING
        `verify_grounding()`.

Why fetch-all instead of top-k: this is precisely the failure mode
findings.md's Module 9 "Problem" section documents — a community report
that's a weak semantic match to the literal query string but collectively
part of a real dataset-wide pattern never surfaces in a top-5-by-
similarity cut. Map-reduce processes every report a hierarchy level has,
batched, not a similarity-ranked subset of them.

MAP STEP `role="reasoning"` UNCONDITIONALLY (not the per-module
`_generation_role()` language-routing helper every other sub-agent
carries): the map step's extracted points are intermediate scratch data,
never shown to the user directly (they never cross the SubAgentResult
boundary) — there is no "reply in the user's language" requirement to
route around the Urdu-fine-tuned generation-slot model's documented bias
for. Only the FINAL generation call (which IS user-facing) uses
`_generation_role()`, same as every other sub-agent.

VALIDATION GATE — STRUCTURAL-ONLY tier (matching Semantic Search/
Large-Scale Aggregate/Case Summarization), NOT the FULL semantic tier
Cross-Case Linkage carries. Decision, flagged per this session's own
discipline: Global Search synthesizes dataset-wide THEMES/PATTERNS from
already-grounded, offline-verified community summaries — it carries no
cross-case IDENTITY/pattern-recurrence claim the way Cross-Case Linkage's
XGRAPH composition does (the risk class that earns the FULL tier
elsewhere in this codebase), so the lighter deterministic check is the
right fit here.

NO XNETWORK-STYLE CLOUD-RETRY-THEN-RAW-FALLBACK — considered and
declined. Cross-Case Linkage's own `_generate_xnetwork_text()` implements
a verify -> one-shot cloud regeneration -> raw-summary-text fallback
sequence, but that is an explicit, scoped decision tied to
`orchestrator.py`'s own pre-existing XNETWORK route behavior (see that
module's own docstring), not a general pattern every XNETWORK-adjacent
generation step should replicate. A verifier rejection here follows the
same plain ABSTAINED path every other sub-agent (Semantic Search, Local
Search) uses.

CAVEAT-ONLY DEGRADATION. This sub-agent composes exactly one tool, so
there is nothing for it to degrade TO — `degraded_from` stays empty here,
same "nothing to degrade to" shape semantic_search.py's own docstring
documents. Two things that could read as "degraded" are surfaced as
CAVEATS instead, `status` staying OK whenever generation+verification
still succeed:
  - The >MAX_REPORTS_SAMPLE cap sampling a subset of reports (this is
    this sub-agent's own designed cost-control behavior, not a tool
    degrading — the same way RAG's own top-k retrieval isn't called
    "degraded" for not being exhaustive).
  - One or more (but not all) map batches failing (an LLM call error)
    while the rest still contribute real points.
If EVERY batch fails outright -> ABSTAINED ("map step failed for all
batches"). If every batch runs but finds nothing relevant -> EMPTY.

PARTIAL-FAILURE MAPPING:
  - global_search_tool DENIED    -> SubAgentStatus.DENIED [RESOLVED-6],
                                     never softened to ABSTAINED — same
                                     propagation Cross-Case Linkage uses.
  - global_search_tool FAILED    -> ABSTAINED.
  - global_search_tool EMPTY (no
    community reports at all for
    this level)                  -> EMPTY.
  - global_search_tool OK, but
    every map batch fails         -> ABSTAINED.
  - global_search_tool OK, every
    batch runs but finds nothing
    relevant                      -> EMPTY.
  - Final generation/verification
    fails                         -> ABSTAINED, no answer_text served.
  - Otherwise                     -> OK, tools_used=["XNETWORK"] (see
                                     tools/global_search.py's own
                                     "SourceTool TAGGING" section for why
                                     this reuses XNETWORK's tag rather
                                     than minting a new SourceTool value).

NOT IN SCOPE THIS PHASE: live wiring into main.py/orchestrator.py/
router.py (same "not yet cut over to live chat traffic" scope every prior
sub-agent phase shipped with); `hierarchy_level` is accepted by the
composed tool (GlobalSearchToolInput.hierarchy_level) but this sub-agent
always constructs it with `hierarchy_level=None` (falls through to the
tool's own middle-level default) — a live, user-facing level selector is
Stage 2 territory per findings.md's own staging, not built here.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

from src.data_gateway.base import DataGateway
from src.llm.client import call_llm
from src.pipeline.harness.supervisor import GLOBAL_SEARCH, register
from src.pipeline.harness.tools.global_search import (
    GlobalSearchToolInput,
    GlobalSearchToolResult,
    global_search_tool,
)
from src.pipeline.harness.types import (
    Citation,
    EvidenceChunk,
    OnEventCallback,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolStatus,
)
from src.pipeline.json_extract import call_llm_json
from src.pipeline.validation import caveats_for_validation, validate_answer
from src.pipeline.verifier import verify_grounding

logger = logging.getLogger(__name__)

# ── Cost-control constants — see findings.md's "Design note — cost
# control on Stage 1's map step" and this session's own approved plan.
# Real current community-report count this sizing was based on: 19
# (RUN-20260825074016, computed this session — confirmed fresh, newer
# than the latest case data). ─────────────────────────────────────────
MAP_BATCH_SIZE = 5
MAX_REPORTS_SAMPLE = 60
REDUCE_TOP_N_POINTS = 15

_MAP_SYSTEM_PROMPT = (
    "You are analyzing a batch of community pattern-summary reports from a "
    "police case-evidence dataset, looking for points relevant to a "
    "specific question. Each report below is labeled [Report N].\n\n"
    "Extract a list of distinct points relevant to the question. For each "
    "point, give an importance rating from 1 (marginally relevant) to 100 "
    "(central to answering the question), and the report number(s) "
    "(1-based, from the [Report N] labels below) that support it.\n\n"
    "Only extract points actually stated in the reports below — do not "
    "invent connections the reports do not state. If nothing in these "
    "reports is relevant to the question, return an empty points list.\n\n"
    'Respond with JSON: {"points": [{"point": string, "importance": '
    'integer 1-100, "supporting_reports": [integer, ...]}]}.'
)
_MAP_SCHEMA_HINT = '"points": [{"point", "importance", "supporting_reports"}]'

_FINAL_SYSTEM_PROMPT_TEMPLATE = (
    "You are a police cross-case pattern-synthesis assistant answering a "
    "question that requires aggregating signal across the WHOLE case "
    "dataset, not just one network or cluster. Below is a list of key "
    "points extracted from across the full dataset to guide what to "
    "emphasize, followed by the community pattern-summary reports those "
    "points were drawn from. Using ONLY the documents below, synthesize a "
    "dataset-wide answer to the question.\n\n"
    "Every factual claim MUST cite its source as [Document N], where N is "
    "that document's 1-based position below. Do not invent connections "
    "the documents do not state.\n\n"
    "Respond in {preferred_language}.\n\n"
    "--- KEY POINTS ACROSS THE DATASET ---\n{points_block}\n"
    "--- END OF KEY POINTS ---\n\n"
    "--- DOCUMENTS ---\n{documents}\n--- END OF DOCUMENTS ---"
)


def _generation_role(preferred_language: Optional[str]) -> str:
    """Final-generation-only — see module docstring's "MAP STEP role"
    note for why the map step itself does not use this. Mirrors every
    prior sub-agent module's own inline `_generation_role()` rather than
    importing one — no cross-sub-agent coupling, same Urdu-fine-tuned
    local generation-slot model finding every one of them documents."""
    return "generation" if preferred_language == "Urdu" else "reasoning"


def _validate_map_result(result) -> bool:
    """Shape-only gate for call_llm_json's retry loop — sanitization of
    individual point values (importance clamping, supporting_reports
    range-checking) happens in _resolve_batch_points below, after a
    shape-valid result is accepted. An empty `points` list is a
    legitimate "nothing relevant in this batch" outcome and must pass."""
    if not isinstance(result, dict):
        return False
    points = result.get("points")
    if not isinstance(points, list):
        return False
    for p in points:
        if not isinstance(p, dict):
            return False
        if not isinstance(p.get("point"), str) or not p["point"].strip():
            return False
        if not isinstance(p.get("importance"), (int, float)):
            return False
        if not isinstance(p.get("supporting_reports"), list):
            return False
    return True


def _resolve_batch_points(raw_points: list[dict], batch: list[EvidenceChunk]) -> list[dict]:
    """
    Sanitize one batch's validated-but-untrusted points into
    {"point", "importance", "chunks"} dicts — clamps importance to
    [1, 100], drops any `supporting_reports` index outside the batch's
    own 1-based range, and drops a point entirely if it resolves to zero
    real supporting chunks (an unsupported point can't be grounded back
    to [Document N] evidence, so it can't survive into the reduce step).
    """
    resolved: list[dict] = []
    for p in raw_points:
        try:
            importance = max(1, min(100, int(p.get("importance"))))
        except (TypeError, ValueError):
            continue
        supporting = [
            i for i in (p.get("supporting_reports") or [])
            if isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= len(batch)
        ]
        if not supporting:
            continue
        point_text = str(p.get("point") or "").strip()
        if not point_text:
            continue
        resolved.append({
            "point": point_text,
            "importance": importance,
            "chunks": [batch[i - 1] for i in supporting],
        })
    return resolved


async def _map_batch(query_text: str, batch: list[EvidenceChunk]) -> Optional[list[dict]]:
    """
    One map-step LLM call over one already-shuffled batch. Returns None
    (batch FAILED — the caller counts this separately from a batch that
    ran fine and legitimately found nothing) only if call_llm_json's own
    retries (including its escalate-on-failure policy) are exhausted;
    returns [] for a batch that ran fine and found nothing relevant.
    """
    # [findings.md GS-1] Report numbering and summary text are unchanged;
    # only each report's own deterministic case scope is now visible, so
    # the map step can tell a single-case cluster from a genuinely
    # cross-case one instead of inferring it from prose.
    documents_text = "\n\n".join(
        f"[Report {i}] ({_scope_annotation(chunk)})\n{chunk.text}"
        for i, chunk in enumerate(batch, start=1)
    )
    user_message = f"QUESTION: {query_text}\n\n--- REPORTS ---\n{documents_text}\n--- END OF REPORTS ---"

    result, raw = await call_llm_json(
        system_prompt=_MAP_SYSTEM_PROMPT,
        user_message=user_message,
        # 700 wasn't a schema mismatch (an earlier read of a truncated log
        # line got that wrong) — it's the same thinking-trace-exhaustion
        # bug fixed everywhere else this session. Confirmed live: every
        # map-batch call over a MAP_BATCH_SIZE=5 batch failed after
        # exhausting retries, both local and cloud, on JSON visibly cut
        # off mid-string. Raised to 2000 for the LOCAL budget;
        # cloud_max_tokens pinned at the old 700 so the cloud fallback's
        # own token/cost accounting is unaffected. Unrelated to this
        # module's own cost-control note above (MAP_BATCH_SIZE, call
        # COUNT) — this is a per-call completion-size floor, not a
        # call-count lever.
        max_tokens=2000,
        cloud_max_tokens=700,
        role="reasoning",
        validate=_validate_map_result,
        schema_hint=_MAP_SCHEMA_HINT,
    )
    if result is None:
        logger.warning("Global Search: map batch failed after retries — raw: %s", raw[:200])
        return None
    return _resolve_batch_points(result.get("points") or [], batch)


def _make_batches(chunks: list[EvidenceChunk], batch_size: int) -> list[list[EvidenceChunk]]:
    """Partition into fixed-size batches, then SHUFFLE EACH BATCH IN
    PLACE — per the algorithm description ("batches are shuffled ... to
    avoid position bias"). The post-shuffle order is what's actually
    shown to the map-step LLM call as [Report 1..k], so a returned
    `supporting_reports` index correctly indexes this same shuffled
    list — see _map_batch's own [Report N] numbering."""
    batches = [chunks[i:i + batch_size] for i in range(0, len(chunks), batch_size)]
    for batch in batches:
        random.shuffle(batch)
    return batches


def _scope_annotation(chunk: EvidenceChunk) -> str:
    """
    [findings.md GS-1] Deterministic, per-community scope label built
    ONLY from the chunk's own structured `case_ids` — never from
    `case_ids_touched` (the query-level union) and never inferred from
    the report's prose.

    Both the map and the reduce stage render scope through this one
    helper so the two stages cannot drift into subtly different wording.

    Without it a single-case community and a genuinely cross-case one
    were indistinguishable once several reports were flattened into one
    prompt, so the query's aggregate footprint could be read as evidence
    that any one community spanned every case it touched.
    """
    cases = list(getattr(chunk.metadata, "case_ids", None) or [])
    if not cases:
        return "scope unknown"
    joined = ", ".join(cases)
    if len(cases) == 1:
        return f"cases: {joined} — single-case"
    return f"cases: {joined} — spans {len(cases)} cases"


def _format_documents_for_prompt(chunks: list[EvidenceChunk]) -> str:
    """[Document N] numbering here MUST match Citation.document_index and
    verify_grounding()'s positional chunks[n-1] indexing — same list,
    same order, per EvidenceChunk's [PRESERVE — design §5] contract."""
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        source = chunk.metadata.source_file or "unknown"
        parts.append(
            f"[Document {i}] (community {source}; {_scope_annotation(chunk)})\n{chunk.text}"
        )
    return "\n\n".join(parts)


def _chunk_to_verifier_dict(chunk: EvidenceChunk) -> dict:
    return {"id": chunk.id, "text": chunk.text, "metadata": chunk.metadata.model_dump()}


def _citations_for(chunks: list[EvidenceChunk]) -> list[Citation]:
    return [
        Citation(
            document_index=i,
            source_tool=chunk.metadata.source_tool,
            case_id=chunk.metadata.case_id,
            source_file=chunk.metadata.source_file,
            confidence=chunk.metadata.confidence,
        )
        for i, chunk in enumerate(chunks, start=1)
    ]


async def global_search(
    agent_input: SubAgentInput,
    *,
    on_event: Optional[OnEventCallback] = None,
    gateway: Optional[DataGateway] = None,
) -> SubAgentResult:
    """The Global Search sub-agent. See module docstring for the full contract."""
    execution = agent_input.execution
    caller = execution.caller

    tool_result: GlobalSearchToolResult = await global_search_tool(
        GlobalSearchToolInput(query_text=agent_input.query_text, execution=execution)
    )

    if tool_result.status == ToolStatus.DENIED:
        return SubAgentResult(
            status=SubAgentStatus.DENIED,
            error=tool_result.error,
            caveats=["Global search access was denied for your role."],
        )
    if tool_result.status == ToolStatus.FAILED:
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=tool_result.error,
            caveats=["Global search failed; no answer could be produced."],
        )
    if tool_result.status == ToolStatus.EMPTY:
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            caveats=["No community reports are available to search at this hierarchy level."],
        )

    # ── status == OK: real community reports to run map-reduce over ─────
    chunks = tool_result.chunks
    caveats: list[str] = []

    if len(chunks) > MAX_REPORTS_SAMPLE:
        sampled = random.sample(chunks, MAX_REPORTS_SAMPLE)
        caveats.append(
            f"Answered from a sample of {MAX_REPORTS_SAMPLE} of {len(chunks)} community "
            "reports at this hierarchy level, for cost control, not the full set."
        )
        chunks = sampled

    batches = _make_batches(chunks, MAP_BATCH_SIZE)

    all_points: list[dict] = []
    failed_batches = 0
    for batch in batches:
        batch_points = await _map_batch(agent_input.query_text, batch)
        if batch_points is None:
            failed_batches += 1
            continue
        all_points.extend(batch_points)

    if failed_batches == len(batches):
        # Note: the sample-cap caveat (if any) is deliberately dropped
        # here too — the map step produced nothing usable at all, so
        # "answered from a sample" would be a misleading thing to say
        # about an answer that was never produced.
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=["The map step failed for every report batch; no answer could be produced."],
        )
    if failed_batches:
        caveats.append(
            f"{failed_batches} of {len(batches)} report batches could not be processed and were "
            "excluded from this answer."
        )

    if not all_points:
        # Preserves whatever caveats already accumulated above (the
        # sample-cap notice, a partial-batch-failure notice) — a real
        # cap/failure still happened even though the end result here is
        # "nothing relevant found", and the caller should still know.
        caveats.append("No community reports relevant to this question were found across the dataset.")
        return SubAgentResult(status=SubAgentStatus.EMPTY, caveats=caveats)

    # ── Reduce: top-N points by importance, distinct backing chunks ─────
    all_points.sort(key=lambda p: p["importance"], reverse=True)
    top_points = all_points[:REDUCE_TOP_N_POINTS]

    distinct_chunks: list[EvidenceChunk] = []
    seen_ids: set[str] = set()
    for point in top_points:
        for chunk in point["chunks"]:
            if chunk.id not in seen_ids:
                seen_ids.add(chunk.id)
                distinct_chunks.append(chunk)

    resolved_language = caller.preferred_language or "the same language as the user's question"
    points_block = "\n".join(f"- {p['point']} (importance {p['importance']})" for p in top_points)
    system_prompt = _FINAL_SYSTEM_PROMPT_TEMPLATE.format(
        preferred_language=resolved_language,
        points_block=points_block,
        documents=_format_documents_for_prompt(distinct_chunks),
    )

    try:
        answer = await call_llm(
            system_prompt, agent_input.query_text, role=_generation_role(caller.preferred_language)
        )
    except Exception as exc:
        logger.error("Global Search: final generation failed: %s", exc)
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=["Answer generation failed; no answer could be produced."],
        )

    verification = await verify_grounding(
        answer=answer,
        cited_chunks=[_chunk_to_verifier_dict(c) for c in distinct_chunks],
        case_id="cross_case",
        cross_case_ids=tool_result.case_ids_touched,
    )
    verifier_passed = verification.get("grounded", False) and not verification.get("off_topic", False)
    if not verifier_passed:
        logger.warning(
            "Global Search: verifier rejected answer: %s", (verification.get("reason") or "")[:150],
        )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=["The generated answer could not be verified as grounded in the retrieved documents."],
        )

    validation_status, validation_claims = await validate_answer(
        answer_text=answer,
        cited_chunks=[_chunk_to_verifier_dict(c) for c in distinct_chunks],
        tier="structural",
    )
    caveats.extend(caveats_for_validation(validation_status, validation_claims))

    return SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text=answer,
        citations=_citations_for(distinct_chunks),
        tools_used=["XNETWORK"],
        caveats=caveats,
        validation_status=validation_status,
        validation_claims=validation_claims,
    )


global_search.name = GLOBAL_SEARCH

# Import-time self-registration — same pattern every prior sub-agent
# module established (supervisor.py's own module docstring). Importing
# this module is what makes Global Search live in the module-level
# registry.
register(global_search)
