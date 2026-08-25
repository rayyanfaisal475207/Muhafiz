"""
Meta-Analysis sub-agent — src/pipeline/harness/agents/meta_analysis.py
(findings.md Module 10, "Meta-analysis — query decomposition and
aggregation" — the last of the ten findings.md modules).

Source of truth: findings.md's Module 10 section (the "Proposed approach"
three-stage design, the three design questions resolved via AskUserQuestion
before any of this was written — RBAC, cost/latency cap, trigger patterns —
and the "Relationship to Modules 7, 8, 9" note) and this session's own
approved plan.

SCOPE — THE OUTERMOST LAYER. Every other sub-agent in this package composes
one or more TOOLS (RAG, GRAPH, XGRAPH, ...). This one is different: it
composes the SUPERVISOR ITSELF — it decomposes the original question into a
bounded set of standalone sub-questions and re-enters
`src.pipeline.harness.supervisor.Supervisor.handle()` for each one,
concurrently, then synthesizes across the independently-produced
sub-answers. A sub-query can resolve to ANY of the other ten sub-agents
(Semantic Search, Local Search, Global Search, Cross-Case Linkage, ...) —
this module does not know or care which. It is complementary to, not a
replacement for, Modules 7/8/9: those improve how ONE question is answered;
this module decides whether the user's ask is actually SEVERAL questions in
the first place.

NO RECURSION BEYOND ONE LEVEL — [PRESERVE, per the approved plan and
findings.md's own "central design commitment"]. A sub-query is NEVER itself
decomposed further. Enforced structurally, not by convention: every
`Supervisor.handle()` call this module makes passes
`allow_meta_analysis=False` (see `supervisor.py::classify_to_subagent()`'s
own docstring for the full recursion-guard rationale) — a sub-query
classifies EXACTLY as if this module did not exist, even if its own text
still happens to match `_META_ANALYSIS_TRIGGER_PATTERNS`. This module is the
first sub-agent in this codebase to call `Supervisor.handle()` at all
(every prior sub-agent composes tools directly); the guard exists because
nothing before this module could ever have recursed.

RBAC — [PRESERVE, resolved via AskUserQuestion, not assumed]. This module
adds NO role check of its own — the same "no third gate" discipline
`cross_case_linkage.py`/`large_scale_aggregate.py` already document. The
actual gate is one level up, in `supervisor.py`: `META_ANALYSIS` is in
`_CROSS_CASE_SUBAGENTS`, so a query only ever reaches this module at all
when its own `case_scope` (computed by `route_query()`, the same signal
XAGG/XGRAPH/XNETWORK key on) is `"cross_case"` — a within-case compound
question demotes straight to Semantic Search before this module is ever
invoked, at zero N-way pipeline cost. Once dispatched, individual sub-queries
are gated on their own merits by whichever tool/sub-agent they resolve to
via the normal recursive `Supervisor.handle()` call — a low-privilege
caller's cross-case-shaped sub-question(s) can still come back DENIED
individually, disclosed as a caveat, never a blanket failure of the whole
meta-analysis (see STATUS MAPPING below).

THREE STAGES:

1. DECOMPOSE. `call_llm_json()` against `prompts/meta_analysis_decomposer.txt`
   — an EXTERNAL prompt file, unlike every other sub-agent's own inline
   `_SYSTEM_PROMPT_TEMPLATE` (see `local_search.py`'s own explicit note on
   why ITS generation prompt is inline). This is a deliberate, considered
   difference, not an inconsistency: the decomposer is a narrow classifier/
   judge call (bool + bounded list + one string), the same SHAPE as
   `router.py`'s/`verifier.py`'s own classifier prompts, both of which
   already use external `prompts/*.txt` files — not the "final,
   user-facing, [Document N]-citing narrative answer" shape
   `local_search.py`'s note is actually about. The synthesis prompt below
   (stage 3), which IS that shape, follows the inline-template convention
   instead, for the same reason `local_search.py` gives.

   Can return `decompose: false` — this is the real "no decomposition
   needed" decision (the deterministic trigger patterns in `supervisor.py`
   are a cheap FAST-PATH INTO this call, not the decision itself; they can
   and do over-match, e.g. a long single-topic question that happens to
   contain both "summarize" and "across all" nearby). On `decompose: false`,
   or on a parse failure after `call_llm_json()`'s own retries, this module
   falls back to ONE non-decomposed dispatch of the ORIGINAL query via
   `Supervisor().handle(..., allow_meta_analysis=False)` and returns exactly
   that `SubAgentResult` — untouched, per every prior sub-agent's own
   "[PRESERVE] returned exactly as received" ethos applied one level up.
   The parse-failure path adds one caveat disclosing that automatic
   decomposition could not run; `decompose: false` adds none (the system
   correctly recognized no decomposition was needed — nothing degraded).

2. DISPATCH. One `SubAgentInput` per sub-query (`execution` threaded
   UNCHANGED per [PRESERVE — design §4.4]; `target_entity=None`, letting
   each sub-query's own `route_query()` derive its own rather than
   inheriting the original query's entity onto an unrelated sub-question).
   Each dispatch is wrapped by `_dispatch_one()`, which NEVER RAISES — it
   returns a `_SubQueryOutcome` carrying either a real `SubAgentResult` or a
   failure reason (`"timeout"` or the exception text), mirroring
   `ToolResult`'s own "no tool raises to signal a routine outcome" doctrine
   one level up. All N dispatches run concurrently via `asyncio.gather` over
   these non-raising wrappers — one bad sub-query can never take the others
   down, and every outcome (success, empty, denied, abstained, timed-out)
   reaches stage 3, disclosed, never silently dropped. Bounded at
   `_MAX_SUB_QUERIES` (5, per findings.md's own suggested cap and the
   approved plan) — the decomposer prompt is instructed to stay within this,
   and this module hard-truncates defensively if it doesn't.

3. AGGREGATE / SYNTHESIZE. See `_bucket_outcomes()`/`meta_analysis()` for
   the full status-mapping bucket list. In short: a sub-query that produced
   real content (OK/PARTIAL with `answer_text`) OR a legitimate EMPTY
   ("nothing found for this sub-question" — a real finding, not a failure,
   mirroring `cross_case_linkage.py`'s own EMPTY philosophy) CONTRIBUTES a
   pseudo-`[Document N]` entry to the synthesis pass; ABSTAINED/DENIED/
   timeout/exception sub-queries do not contribute a document, but DO
   contribute one `caveats` entry each, naming the sub-question and the
   reason. If EVERY sub-query contributed and none are real EMPTY-only
   (i.e. there is at least one substantive answer), one `call_llm()` pass
   synthesizes across the contributing sub-answers into the decomposer's own
   `synthesis_goal`, verified through the EXISTING `verify_grounding()` —
   **confirmed by reading `verifier.py` before writing this module: its
   signature needs NO extension.** It already accepts an arbitrary flat
   `list[dict]` "documents" list to check `[Document N]` claims against;
   this module's pseudo-chunks (`text=` a contributing sub-query's own
   `answer_text`) are built with the exact same `{"id", "text", "metadata"}`
   shape every other sub-agent's own `_chunk_to_verifier_dict()` produces —
   just sourced from an already-synthesized, already-Verifier-passed
   sub-answer instead of raw evidence text. This is the same "map over
   already-synthesized documents" shape `global_search.py`'s reduce step
   established, adapted for sub-*answers* rather than community reports:
   Global Search has its own tool's raw `EvidenceChunk`s to reduce over
   because it composes ONE TOOL internally and never crosses a
   `SubAgentResult` boundary until the very end; this module composes OTHER
   SUB-AGENTS via `Supervisor.handle()`, so all it ever receives back is
   each one's own BOUNDED `SubAgentResult` (`answer_text` + `citations`,
   [PRESERVE — design §3] never raw chunk text) — the pseudo-chunk *is* the
   sub-answer text, by construction, not a shortcut around fetching real
   evidence.

   If ALL contributing sub-queries are legitimate EMPTY (nothing found
   anywhere, and nothing genuinely failed either), this module returns
   `EMPTY` with a deterministic templated `answer_text` naming every
   sub-question checked — NO LLM call, mirroring `cross_case_linkage.py`'s
   own cheap-and-deterministic EMPTY handling (nothing substantive exists to
   synthesize or hallucinate about).

   If NOTHING contributed at all (every sub-query ABSTAINED/DENIED/timed
   out/raised), this module returns `ABSTAINED` (or `DENIED` in the one case
   every single sub-query came back DENIED specifically — mirrors
   `cross_case_linkage.py`'s "both DENIED -> DENIED" bucket, generalized to
   N; [RESOLVED-6] a mix of DENIED + something-else is `PARTIAL`, DENIED is
   NEVER collapsed into ABSTAINED/EMPTY when it's the SOLE outcome).

VALIDATION GATE — FULL semantic tier (plan §5's higher-stakes tier), same
reasoning as Cross-Case Linkage: a cross-case-shaped synthesis-of-syntheses
is the same high-stakes claim-recurrence-across-cases shape, run here over
the pseudo-chunks the synthesis generator was actually shown.

`tools_used`/`degraded_from` ARE THE UNION (deduplicated) of every
CONTRIBUTING sub-query's own `tools_used`/`degraded_from` lists — real
`SourceTool` values genuinely propagated up from what each sub-dispatch
actually used, never an invented tag (`SourceTool` is a `Literal` type; a
made-up `"META_ANALYSIS"` value would not validate against it). Meta-level-
only failures (decomposer parse failure, a sub-query that itself
ABSTAINED/DENIED/timed out) surface as free-text `caveats` instead, which
carries no such typing constraint.

`citations`: one `Citation` per CONTRIBUTING sub-query, `document_index`
matching its `[Document N]` position in the synthesis pass — this keeps
`Citation`'s own [PRESERVE] positional-correspondence contract intact: for
this module, `[Document N]` genuinely does mean "the Nth sub-answer",
consistent with what the synthesis generator was actually shown.
`source_tool=` that sub-query's own first `tools_used` entry (documented
simplification — a sub-answer can have used more than one tool;
`Citation.source_tool` is a single value). `case_id`/`source_file=None`,
deliberately not asserted: a sub-answer can span multiple cases/files, and
this layer has no raw evidence left to re-flatten and resolve one from.

NO `types.py` CHANGES NEEDED — confirmed by design, not by omission: this
module's payload (`answer_text` + `citations` + `caveats` +
`tools_used`/`degraded_from`) fits the EXISTING `SubAgentResult` shape
without a new field, unlike Timeline Building's `events` or Cross-Case
Linkage's `links`.

NOT IN SCOPE THIS MODULE: live wiring into main.py/orchestrator.py/router.py
(same posture every prior sub-agent module through Module 9 shipped with —
`config.HARNESS_CUTOVER_ROUTES` defaults empty); file-output handling
(`classify_to_subagent()`'s file-output check already wins outright before
the meta-analysis trigger is ever checked, so this module is never dispatched
to for a file-generation request in the first place).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src import config
from src.data_gateway.base import DataGateway
from src.llm.client import call_llm
from src.pipeline.harness.supervisor import META_ANALYSIS, Supervisor, register
from src.pipeline.harness.types import (
    Citation,
    OnEventCallback,
    SourceTool,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ValidationStatus,
)
from src.pipeline.json_extract import call_llm_json
from src.pipeline.validation import caveats_for_validation, validate_answer
from src.pipeline.verifier import verify_grounding

logger = logging.getLogger(__name__)

_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent.parent / "prompts" / "meta_analysis_decomposer.txt"
)
_DECOMPOSER_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# [findings.md Module 10] Hard cap on N, per the approved plan (findings.md's
# own suggested cap). The decomposer prompt is instructed to stay within
# this; this module hard-truncates defensively if it doesn't, rather than
# rejecting the whole decomposition outright over a prompt-compliance slip.
_MAX_SUB_QUERIES = 5

# Self-contained, sub-agent-scoped synthesis prompt — inline template, NOT
# an external prompts/*.txt file. See module docstring's stage-1 note for
# why the decomposer (a classifier/judge call) and this synthesis prompt (a
# final, user-facing, [Document N]-citing narrative) are deliberately
# treated differently, following local_search.py's own established
# distinction rather than findings.md's literal "own prompts/*.txt" phrasing
# for every new prompt this module uses.
_SYNTHESIS_SYSTEM_PROMPT_TEMPLATE = (
    "You are a police investigative-analysis assistant. The user asked one "
    "broad question, which was split into several sub-questions, each "
    "already answered independently below. Your job: {synthesis_goal}\n\n"
    "Write ONE coherent answer to the user's original question by combining "
    "these sub-answers — do not just concatenate them. Every factual claim "
    "MUST cite its source as [Document N], where N is the sub-answer's "
    "1-based position below. Do not invent anything beyond what the "
    "sub-answers state.\n\n"
    "Respond in {preferred_language}.\n\n"
    "--- SUB-ANSWERS ---\n{documents}\n--- END OF SUB-ANSWERS ---"
)

_NO_INFO_SUBANSWER_TEXT = "No information was found for this sub-question."


def _generation_role(preferred_language: Optional[str]) -> str:
    """
    Mirrors every prior sub-agent module's own inline `_generation_role()`.
    Same finding all of them document: the Urdu-fine-tuned local
    generation-slot model ignores an explicit "reply in English" instruction,
    so non-Urdu responses route through the reasoning slot instead.
    """
    return "generation" if preferred_language == "Urdu" else "reasoning"


def _validate_decomposer_result(result) -> bool:
    if not isinstance(result, dict) or "decompose" not in result:
        return False
    if result["decompose"] is not True:
        return True  # decompose:false (or falsy) needs nothing else.
    sub_queries = result.get("sub_queries")
    return (
        isinstance(sub_queries, list)
        and 1 <= len(sub_queries) <= _MAX_SUB_QUERIES
        and all(isinstance(q, str) and q.strip() for q in sub_queries)
        and isinstance(result.get("synthesis_goal"), str)
        and bool(result.get("synthesis_goal", "").strip())
    )


@dataclass
class _DecomposerResult:
    decompose: bool
    sub_queries: list[str]
    synthesis_goal: str
    parse_failed: bool = False


async def _decompose(query_text: str) -> _DecomposerResult:
    """
    Stage 1. Never raises — a parse failure after `call_llm_json()`'s own
    retries is reported via `parse_failed=True`, not an exception; the
    caller (`meta_analysis()`) treats that the same as `decompose=False`
    plus one caveat, per this module's docstring.
    """
    result, raw = await call_llm_json(
        system_prompt=_DECOMPOSER_SYSTEM_PROMPT.replace("{query}", query_text),
        user_message=query_text,
        max_tokens=1500,
        cloud_max_tokens=600,
        role="reasoning",
        validate=_validate_decomposer_result,
        schema_hint='"decompose" (true/false), "sub_queries" (array of strings), "synthesis_goal" (string)',
        _call_llm=call_llm,
    )
    if result is None:
        logger.warning(
            "Meta-Analysis: decomposer failed to return valid JSON after retries. Raw: %s",
            raw[:150],
        )
        return _DecomposerResult(decompose=False, sub_queries=[], synthesis_goal="", parse_failed=True)

    if result.get("decompose") is not True:
        return _DecomposerResult(decompose=False, sub_queries=[], synthesis_goal="")

    sub_queries = [q.strip() for q in result["sub_queries"] if isinstance(q, str) and q.strip()]
    sub_queries = sub_queries[:_MAX_SUB_QUERIES]
    if not sub_queries:
        # Defensive — validate() already requires a non-empty list, but a
        # future prompt/model drift that slips an all-blank list through
        # must not silently synthesize over zero sub-questions.
        return _DecomposerResult(decompose=False, sub_queries=[], synthesis_goal="", parse_failed=True)

    return _DecomposerResult(
        decompose=True, sub_queries=sub_queries, synthesis_goal=str(result.get("synthesis_goal") or "").strip()
    )


@dataclass
class _SubQueryOutcome:
    """
    Internal only, never crosses this module's boundary. Never constructed
    via a raised exception reaching the caller — see `_dispatch_one()`.
    """

    sub_query: str
    result: Optional[SubAgentResult]
    failure_reason: Optional[str] = None  # Set iff `result` is None.


async def _dispatch_one(sub_query: str, agent_input: SubAgentInput, on_event, gateway) -> _SubQueryOutcome:
    """
    One sub-query's full pipeline pass, re-entering the Supervisor with
    `allow_meta_analysis=False` — [PRESERVE] the one-level-only recursion
    guard; see module docstring. NEVER RAISES: a timeout or any exception
    from the dispatched sub-agent is caught here and reported as a
    non-contributing outcome, so `asyncio.gather` over N of these can never
    have one bad sub-query take the others down.

    KNOWN, DOCUMENTED GAP: `on_event` is forwarded to every concurrent
    sub-query dispatch unchanged, so a caller watching the live trace sees
    one "supervisor:dispatch" `PipelineEvent` per sub-query without a
    sub-query-identifying tag on it (`PipelineEvent` carries no such field
    today). Not a correctness issue — every event still fires — but a
    trace consumer can't yet tell which sub-question a given event belongs
    to. Same class of gap SUBAGENT_INTERFACES.md §2.1.4 already tracks as
    future work for Cross-Case Linkage's own per-source-tool trace; not
    addressed here either, for the same reason (out of this module's scope,
    no live SSE consumer of this harness yet).
    """
    sub_input = agent_input.model_copy(update={"query_text": sub_query, "target_entity": None})
    try:
        result = await asyncio.wait_for(
            Supervisor().handle(sub_input, on_event=on_event, gateway=gateway, allow_meta_analysis=False),
            timeout=config.META_ANALYSIS_SUBQUERY_TIMEOUT,
        )
        return _SubQueryOutcome(sub_query=sub_query, result=result)
    except asyncio.TimeoutError:
        logger.warning("Meta-Analysis: sub-query timed out after %ss: %r", config.META_ANALYSIS_SUBQUERY_TIMEOUT, sub_query[:80])
        return _SubQueryOutcome(sub_query=sub_query, result=None, failure_reason="timeout")
    except Exception as exc:
        logger.error("Meta-Analysis: sub-query dispatch raised: %s", exc)
        return _SubQueryOutcome(sub_query=sub_query, result=None, failure_reason=str(exc))


def _pseudo_chunk(index: int, sub_query: str, text: str) -> dict:
    """Same flat `{"id", "text", "metadata"}` shape every other sub-agent's
    own `_chunk_to_verifier_dict()` produces — see module docstring's stage-3
    note for why the source text here is a sub-answer, not raw evidence."""
    return {
        "id": f"subquery-{index}",
        "text": text,
        "metadata": {"source": f"Sub-question: {sub_query}", "case_id": None},
    }


def _format_subanswers_for_prompt(entries: list[tuple[str, str]]) -> str:
    """`entries` is `[(sub_query, sub_answer_text), ...]`, same order as the
    pseudo-chunks handed to the Verifier — [PRESERVE — design §5] positional
    correspondence."""
    parts = []
    for i, (sub_query, text) in enumerate(entries, start=1):
        parts.append(f"[Document {i}] Sub-question: {sub_query}\n{text}")
    return "\n\n".join(parts)


async def meta_analysis(
    agent_input: SubAgentInput,
    *,
    on_event: Optional[OnEventCallback] = None,
    gateway: Optional[DataGateway] = None,
) -> SubAgentResult:
    """The Meta-Analysis sub-agent. See module docstring for the full contract."""
    execution = agent_input.execution
    caller = execution.caller

    decomposition = await _decompose(agent_input.query_text)

    if not decomposition.decompose:
        # Stage 1's own "no decomposition needed" outcome, OR a decomposer
        # parse failure — both fall back to ONE non-decomposed dispatch of
        # the original query, per module docstring.
        fallback_input = agent_input.model_copy(update={"target_entity": None})
        result = await Supervisor().handle(
            fallback_input, on_event=on_event, gateway=gateway, allow_meta_analysis=False
        )
        if decomposition.parse_failed:
            result = result.model_copy(
                update={
                    "caveats": [
                        "Automatic question decomposition could not run; this question was "
                        "answered as a single query instead.",
                        *result.caveats,
                    ]
                }
            )
        return result

    outcomes = await asyncio.gather(
        *[
            _dispatch_one(sq, agent_input, on_event, gateway)
            for sq in decomposition.sub_queries
        ]
    )

    contributing: list[tuple[str, str, SubAgentResult]] = []  # (sub_query, text_for_synthesis, result)
    caveats: list[str] = []
    tools_used: set[SourceTool] = set()
    degraded_from: set[SourceTool] = set()
    denied_count = 0
    failed_count = 0
    empty_only = True

    for outcome in outcomes:
        if outcome.result is None:
            failed_count += 1
            reason = "timed out" if outcome.failure_reason == "timeout" else "encountered an error"
            caveats.append(f"Could not answer sub-question ({reason}): {outcome.sub_query}")
            continue

        result = outcome.result
        if result.status == SubAgentStatus.DENIED:
            denied_count += 1
            caveats.append(f"Access denied for sub-question: {outcome.sub_query}")
            continue
        if result.status == SubAgentStatus.ABSTAINED:
            failed_count += 1
            caveats.append(f"Could not answer sub-question: {outcome.sub_query}")
            continue

        # OK / PARTIAL (with answer_text) / EMPTY all CONTRIBUTE — see
        # module docstring's EMPTY-is-a-real-finding note.
        tools_used.update(result.tools_used)
        degraded_from.update(result.degraded_from)
        caveats.extend(result.caveats)
        if result.status == SubAgentStatus.EMPTY or not result.answer_text:
            contributing.append((outcome.sub_query, _NO_INFO_SUBANSWER_TEXT, result))
        else:
            empty_only = False
            contributing.append((outcome.sub_query, result.answer_text, result))

    total_dispatched = len(outcomes)

    if not contributing:
        if denied_count == total_dispatched:
            return SubAgentResult(
                status=SubAgentStatus.DENIED,
                error=ToolError(kind="permission_denied", message="Every sub-question required elevated access."),
                caveats=caveats,
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=ToolError(kind="upstream_failure", message="No sub-question could be answered."),
            caveats=caveats,
        )

    degraded = failed_count > 0 or denied_count > 0

    if empty_only:
        # Every contributing sub-query legitimately found nothing, and
        # nothing genuinely failed alongside it — deterministic text, no LLM
        # call, mirroring cross_case_linkage.py's own cheap EMPTY handling.
        named = "; ".join(sq for sq, _text, _r in contributing)
        return SubAgentResult(
            status=SubAgentStatus.PARTIAL if degraded else SubAgentStatus.EMPTY,
            answer_text=(
                "No information was found for any part of this question. "
                f"Checked: {named}."
            ),
            tools_used=sorted(tools_used),
            degraded_from=sorted(degraded_from),
            caveats=caveats,
        )

    # ── Synthesis pass ────────────────────────────────────────────────
    entries = [(sq, text) for sq, text, _r in contributing]
    resolved_language = caller.preferred_language or "the same language as the user's question"
    system_prompt = _SYNTHESIS_SYSTEM_PROMPT_TEMPLATE.format(
        synthesis_goal=decomposition.synthesis_goal or "combine these sub-answers into one complete answer",
        preferred_language=resolved_language,
        documents=_format_subanswers_for_prompt(entries),
    )

    try:
        answer = await call_llm(
            system_prompt, agent_input.query_text, role=_generation_role(caller.preferred_language)
        )
    except Exception as exc:
        logger.error("Meta-Analysis: synthesis generation failed: %s", exc)
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
            caveats=["Synthesizing the sub-answers into a final answer failed.", *caveats],
        )

    pseudo_chunks = [_pseudo_chunk(i, sq, text) for i, (sq, text) in enumerate(entries, start=1)]

    verification = await verify_grounding(answer=answer, cited_chunks=pseudo_chunks, case_id="cross_case")
    verifier_passed = bool(verification.get("grounded", False)) and not verification.get("off_topic", False)

    if not verifier_passed:
        logger.warning(
            "Meta-Analysis: verifier rejected synthesized answer: %s",
            (verification.get("reason") or "")[:150],
        )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=[
                "The synthesized answer could not be verified as grounded in the sub-answers.",
                *caveats,
            ],
        )

    # Validation gate — FULL semantic tier, same reasoning as Cross-Case
    # Linkage (see module docstring).
    validation_status, validation_claims = await validate_answer(
        answer_text=answer, cited_chunks=pseudo_chunks, tier="full"
    )
    caveats = caveats + caveats_for_validation(validation_status, validation_claims)

    citations = [
        Citation(
            document_index=i,
            source_tool=(result.tools_used[0] if result.tools_used else "RAG"),
            case_id=None,
            source_file=None,
            confidence=None,
        )
        for i, (_sq, _text, result) in enumerate(contributing, start=1)
    ]

    return SubAgentResult(
        status=SubAgentStatus.PARTIAL if degraded else SubAgentStatus.OK,
        answer_text=answer,
        citations=citations,
        tools_used=sorted(tools_used),
        degraded_from=sorted(degraded_from),
        caveats=caveats,
        validation_status=validation_status,
        validation_claims=validation_claims,
    )


meta_analysis.name = META_ANALYSIS

# Import-time self-registration — the same pattern every prior sub-agent
# module established (supervisor.py's own module docstring documents it).
register(meta_analysis)
