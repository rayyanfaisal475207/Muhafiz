"""
Executable interface contracts for the agent harness.

SOURCE OF TRUTH: `docs/SUBAGENT_INTERFACES.md`. This module is a verbatim
transcription of that document's §0–§2 type definitions into importable code.
The document remains the human-readable specification with the full rationale
for every [PRESERVE] and [RESOLVED-n] marker; this module is the machine-
readable half of the same contract.

**The two must change together.** If you edit a type here, update the
corresponding section of SUBAGENT_INTERFACES.md in the same commit, and vice
versa. Docstrings below carry the doc's preservation markers so the reasoning
travels with the code — they are not decoration, and a change that silently
drops one is a contract change.

Nothing in this module imports from `src.retrieval`, `src.graph`,
`src.pipeline.orchestrator`, or any other production pipeline code. That
isolation is deliberate: the harness must be buildable and testable without
the live pipeline.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════════
# §0 — Shared types
# ══════════════════════════════════════════════════════════════════════════

class Role(str, Enum):
    """
    The four RBAC roles, ordered least- to most-privileged.

    Cross-case tools (XGRAPH / XAGG / XNETWORK) require SUPERVISOR or above.
    """
    INVESTIGATOR = "investigator"
    SUPERVISOR = "supervisor"
    STATION_ADMIN = "station-admin"
    PLATFORM_ADMIN = "platform-admin"


CROSS_CASE_ROLES: frozenset[Role] = frozenset(
    {Role.SUPERVISOR, Role.STATION_ADMIN, Role.PLATFORM_ADMIN}
)


class CallerContext(BaseModel):
    """
    Who is asking, and under what case scope. Threaded supervisor → sub-agent
    → tool, unchanged, at every hop.

    [PRESERVE — design §4.4] `role` MUST originate from the authenticated
    user's real RBAC role. It must NEVER be read out of a user-profile /
    preferences object. Those objects carry no role key, and a historical bug
    that read one silently defaulted every cross-case check to `investigator`,
    denying real supervisors their own access. This model exists as a separate
    type from any profile/preferences model specifically so the two cannot be
    confused at a call site.

    [PRESERVE — design §4.1, §4.2] Construction of this object does NOT grant
    access. Case-access authorization (hard 403) and RLS scope arming are the
    API boundary's responsibility and happen strictly BEFORE the supervisor is
    invoked. A `CallerContext` that reaches a tool is assumed already
    authorized for `active_case_id` — it is not re-checked below the boundary.
    Any NEW entry point (scheduled job, internal API, batch runner) must
    perform both steps itself; they are not inherited.
    """
    user_id: Optional[str] = None
    role: Role
    active_case_id: Optional[str] = Field(
        default=None,
        description="The case this query is scoped to. None for queries with no active case.",
    )
    preferred_language: Optional[str] = Field(
        default=None,
        description="Drives generation language on every route. Opaque here.",
    )


SourceTool = Literal["RAG", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "XNETWORK", "SQL", "WEB"]


class ChunkMetadata(BaseModel):
    """
    Open-ended per-chunk metadata. Consumers read known keys, ignore unknown
    ones. Adding keys is non-breaking.

    [PRESERVE — design §5 tradeoff] `source_tool` is REQUIRED on every chunk
    emitted by any tool. It is what preserves per-tool provenance once several
    tools' output is flattened into one list — the Verifier sees one merged
    list and would otherwise have no way to tell RAG-sourced from GRAPH-sourced
    evidence.

    NOTE — known open item (AGENT_HARNESS_DESIGN.md §7): `confidence` conflates
    "checked, and it's low" with "never computed". Left unchanged deliberately;
    do not "fix" it here without resolving that item first.
    """
    model_config = {"extra": "allow"}

    source_tool: SourceTool = Field(
        description="[PRESERVE] Which tool produced this chunk. Required on every chunk."
    )
    case_id: Optional[str] = Field(
        default=None,
        description=(
            "Owning case. [PRESERVE — design §4.6] Read by the Verifier's leakage "
            "backstop, which re-derives cited chunks' case_id from the answer text and "
            "checks it against the active case plus any explicitly allowed cross-case "
            "IDs. A chunk that omits this cannot be leak-checked."
        ),
    )
    source_file: Optional[str] = None
    confidence: Optional[float] = Field(
        default=None,
        description=(
            "Per-chunk confidence where the producing tool computes one. [PRESERVE] "
            "Drives the Verifier's hedging check."
        ),
    )


class EvidenceChunk(BaseModel):
    """
    The single evidence currency of the entire harness. Every tool emits these;
    every sub-agent hands these to the Verifier.

    [PRESERVE — design §5] This flat shape is the Verifier's contract and must
    not be replaced with a richer nested/grouped payload. The Verifier's
    deterministic checks parse `[Document N]` citations out of the generated
    answer and index positionally into the chunk list (`chunks[n-1]`). A
    sub-agent composing several tools MUST flatten to one ordered list, and the
    list it hands the Verifier MUST be exactly the list the generator was
    shown — same objects, same order.

    [PRESERVE — design §3] This type MUST NOT appear in `SubAgentResult`. It is
    the sub-agent's internal working currency; only `Citation` crosses the
    handoff boundary upward.
    """
    id: str
    text: str
    metadata: ChunkMetadata
    score: Optional[float] = Field(
        default=None,
        description=(
            "Opaque relevance score. Higher is better WITHIN one result set only. "
            "Never threshold on an absolute value; never compare across tools."
        ),
    )


class ToolStatus(str, Enum):
    """
    Why a tool call ended the way it did. The discriminator every caller
    branches on. `OK` and `EMPTY` are distinct on purpose: `EMPTY` is a
    successful call that legitimately found nothing.
    """
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"
    DENIED = "denied"


class ToolError(BaseModel):
    """
    Structured failure detail. Present iff status is FAILED or DENIED.

    [PRESERVE — design §4.3] A DENIED result means the caller's role failed a
    cross-case gate. That check happens INSIDE the tool, before the tool does
    anything else that touches cross-case scope, and writes an
    authorization-violation audit record.
    """
    kind: Literal["permission_denied", "upstream_failure", "invalid_input", "timeout"]
    message: str = Field(description="Operator-facing. Not shown verbatim to the end user.")


# ══════════════════════════════════════════════════════════════════════════
# §1 — Tool interfaces
# ══════════════════════════════════════════════════════════════════════════

class ToolInput(BaseModel):
    """Base input. Every tool receives the caller context unchanged."""
    query_text: str = Field(description="The rewritten, standalone query.")
    caller: CallerContext


class ToolResult(BaseModel):
    """
    Base result. Every tool returns this shape — no tool raises to signal a
    routine outcome (empty result, permission denial, upstream failure).
    """
    status: ToolStatus
    chunks: list[EvidenceChunk] = Field(default_factory=list)
    error: Optional[ToolError] = None
    fallback_to_rag: bool = Field(
        default=False,
        description=(
            "[PRESERVE — design §2.2, §2.6, §2.7] Whether the CALLER should now "
            "substitute the RAG tool. Only ever True for GRAPH, GRAPH_HYBRID, SQL, "
            "and WEB. PERMANENTLY False for XGRAPH, XAGG, XNETWORK. The tool does not "
            "perform the fallback itself; it reports that one is warranted."
        ),
    )


class Tool(Protocol):
    """Structural contract every primitive satisfies."""
    name: SourceTool

    async def __call__(self, tool_input: ToolInput) -> ToolResult: ...


# ── §1.1 Cross-case tool base ────────────────────────────────────────────

class CrossCaseToolInput(ToolInput):
    """
    Input to XGRAPH / XAGG / XNETWORK.

    [PRESERVE — design §2.3, §4.3] Ordering inside every cross-case tool is
    load-bearing:

        1. Check caller.role against CROSS_CASE_ROLES.
        2. On failure: write an authorization-violation audit record, return
           status=DENIED. Nothing else runs.
        3. ONLY on success: arm cross-case / RLS-bypass scope, then query.

    A harness restructuring that hoists scope resolution up to the supervisor,
    so scope is armed before dispatch, REINTRODUCES a documented historical
    bug. Do not do it.
    """


class CrossCaseToolResult(ToolResult):
    """
    [PRESERVE — design §2.3, §2.4, §2.5] Cross-case tools NEVER fall back to
    RAG. Cross-case evidence must never blend into a case-scoped RAG answer
    stream.
    """
    fallback_to_rag: Literal[False] = False
    case_ids_touched: list[str] = Field(
        default_factory=list,
        description=(
            "Every case ID contributing to this result. [PRESERVE — design §4.6] "
            "Feeds the Verifier's allowed-cross-case-ID list."
        ),
    )


# ── §1.2 RAG ─────────────────────────────────────────────────────────────

class RagToolInput(ToolInput):
    """
    Retrieve passages relevant to `query_text` within the caller's scope.

    Deliberately says nothing about HOW. [PRESERVE — design §2.1] Scoping is
    case-assignment-based, NOT role-based: RAG carries no role gate.
    """
    top_k: Optional[int] = None
    include_global: bool = True


class RagToolResult(ToolResult):
    """
    [PRESERVE — design §2.1] RAG is the FALLBACK TARGET for GRAPH,
    GRAPH_HYBRID, SQL and WEB — it has no onward fallback of its own.
    Exhausting its internal retry budget abstains; it does NOT reach for web
    search. That removal was deliberate. Do not "restore" it.
    """
    fallback_to_rag: Literal[False] = False
    retries_used: int = 0
    evaluator_verdict: Optional[Literal["relevant", "not_relevant", "unavailable"]] = Field(
        default=None,
        description=(
            "Outcome of the relevance gate. FOUR distinct states, and collapsing any "
            "two of them loses information a caller needs:\n"
            "  'relevant'     — gate ran, evidence passed.\n"
            "  'not_relevant' — gate ran, evidence rejected. Status is EMPTY.\n"
            "  'unavailable'  — THE GATE COULD NOT RUN (evaluator raised/timed out). "
            "Chunks are passed through UNVETTED so a flaky evaluator does not take "
            "retrieval down with it, but the evidence carries no relevance guarantee. "
            "A caller that treats this as equivalent to 'relevant' has silently "
            "dropped the gate. Always accompanied by a caveat in "
            "`degradation_caveats` — see rag_tool.\n"
            "  None           — the gate was never reached (retrieval failed, or "
            "returned nothing to evaluate). Distinct from 'unavailable': nothing was "
            "skipped, there was simply nothing to judge."
        ),
    )
    degradation_caveats: list[str] = Field(
        default_factory=list,
        description=(
            "User-facing qualifications about HOW this result was produced — "
            "currently, that the relevance gate could not run. The composing "
            "sub-agent MUST propagate these into `SubAgentResult.caveats`, which is "
            "where the contract's existing caveat pattern lives (the same treatment "
            "Cross-Case Linkage gives unconfirmed identity links: surfaced, never "
            "silently dropped)."
        ),
    )


# ── §1.3 GRAPH / GRAPH_HYBRID ────────────────────────────────────────────

class GraphToolInput(ToolInput):
    """
    Within-case traversal of the evidence graph from an entity seed.

    [PRESERVE — design §2.2] Within-case only. Cross-case traversal is XGRAPH —
    a separate tool with a role gate, NOT a flag on this one.
    """
    target_entity: Optional[str] = None
    max_hops: int = Field(default=2, ge=1, le=3)
    hybrid: bool = Field(
        default=False,
        description=(
            "False → GRAPH. True → GRAPH_HYBRID. [RESOLVED-1] Fusion STRATEGY is "
            "retrieval-internal; sub-agents request hybrid behavior, they do not "
            "implement it. [RESOLVED-1a] But hybrid is NOT invisible: chunks carry "
            "source_tool='GRAPH_HYBRID' and consumers MUST surface it distinctly."
        ),
    )


class GraphToolResult(ToolResult):
    """
    [PRESERVE — design §2.2] Sets `fallback_to_rag=True` when traversal fails
    or yields nothing usable.
    """
    hop_count: int = 0
    chain_confidence: Optional[float] = None
    seed_entities: list[dict[str, Any]] = Field(default_factory=list)
    conflicts_included: bool = False


# ── §1.3.1 source_tool display contract ──────────────────────────────────

SOURCE_TOOL_DISPLAY_LABELS: dict[str, str] = {
    "RAG": "document search",
    "GRAPH": "case-graph search",
    "GRAPH_HYBRID": "combined document + case-graph search",
    "XGRAPH": "cross-case entity search",
    "XAGG": "cross-case aggregate",
    "XNETWORK": "cross-case pattern synthesis",
    "SQL": "penal-code reference lookup",
    "WEB": "external web search",
}
"""
[RESOLVED-1a] Investigator-facing labels for `metadata.source_tool`.

Consumers MUST NOT invent their own mapping, collapse distinct values, or fall
back to the raw enum name in user-facing surfaces. GRAPH_HYBRID is a DISTINCT
label — never displayed as "GRAPH", never omitted.
"""


# ── §1.4 XGRAPH ──────────────────────────────────────────────────────────

class XGraphToolInput(CrossCaseToolInput):
    """
    Cross-case traversal for entity connections and recurrence.

    Covers BOTH "find connections for this named entity" AND "has any entity of
    type X recurred across cases" — both are structured, graph-typed queries.
    See SUBAGENT_INTERFACES.md §1.4 for the full dispatch precedence chain
    against XNETWORK.
    """
    target_entity: Optional[str] = None
    max_hops: int = Field(default=2, ge=1, le=3)


class XGraphToolResult(CrossCaseToolResult):
    """
    [PRESERVE — design §2.3] Never falls back to RAG. An empty result with no
    unconfirmed links is a definite "no connections found" answer.
    """
    unconfirmed_links: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "[PRESERVE — design §3] CAVEATS — surfaced as such, never asserted as "
            "confirmed fact."
        ),
    )
    hop_count: int = 0
    chain_confidence: Optional[float] = None


# ── §1.5 XAGG ────────────────────────────────────────────────────────────

AggregateKind = Literal["graph_recurrence", "relational_aggregate", "case_listing"]


class XAggToolInput(CrossCaseToolInput):
    """
    [PRESERVE — design §2.4] TWO CANNED AGGREGATE FAMILIES, keyword-dispatched.
    Deliberately NOT a general text-to-SQL/Cypher system.
    """
    target_entity: Optional[str] = None


class XAggToolResult(CrossCaseToolResult):
    """
    [PRESERVE — design §2.4] Never falls back to RAG. Verifier rejection means
    the PARAPHRASE failed, not that the evidence is unsound — serve
    `raw_summary_text`, NOT a generic abstention.
    """
    aggregate_kind: Optional[AggregateKind] = None
    raw_summary_text: Optional[str] = None


# ── §1.6 XNETWORK ────────────────────────────────────────────────────────

class XNetworkToolInput(CrossCaseToolInput):
    """
    Open-ended cross-case pattern/theme synthesis over precomputed community
    summaries. CATEGORICALLY DIFFERENT from XGRAPH — no graph traversal, no
    entity typing. Results reflect the last offline summarization run, not live
    graph state.
    """
    top_k: int = Field(default=5, ge=1)


class XNetworkToolResult(CrossCaseToolResult):
    """
    [PRESERVE — design §2.5] Never falls back to RAG. XNETWORK-specific: ONE
    forced-cloud regeneration retry on first Verifier rejection, then raw text.
    Do NOT generalize that retry to XAGG/XGRAPH.
    """
    community_ids: list[str] = Field(default_factory=list)
    raw_summary_text: Optional[str] = None


# ── §1.7 SQL ─────────────────────────────────────────────────────────────

class SqlToolInput(ToolInput):
    """
    [PRESERVE — design §2.6] The ONLY in-scope SQL path. The standalone MCP
    Postgres integration is unrelated and not part of this harness. Reference
    data, not case evidence: NO case scoping, NO role gate.
    """


class SqlToolResult(ToolResult):
    """[PRESERVE — design §2.6] `fallback_to_rag=True` on empty rows or any exception."""
    row_count: int = 0
    extracted_params: Optional[dict[str, Any]] = None


# ── §1.8 WEB ─────────────────────────────────────────────────────────────

class WebToolInput(ToolInput):
    """
    [PRESERVE — design §2.7] AIR-GAP MODE DISABLES THIS ENTIRELY, checked at
    BOTH provider call sites BEFORE either is reached. No case scope, no role
    gate. Web results are NEVER cited as case evidence.
    """
    max_results: int = Field(default=5, ge=1)


class WebToolResult(ToolResult):
    """
    [PRESERVE — design §2.7] `fallback_to_rag=True` only after BOTH tiers have
    failed. Preserve the two-tier shape.
    """
    provider_used: Optional[Literal["primary_search", "grounded_search_fallback"]] = None


# ══════════════════════════════════════════════════════════════════════════
# §2 — Sub-agent interfaces
# ══════════════════════════════════════════════════════════════════════════

class SubAgentStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    EMPTY = "empty"
    ABSTAINED = "abstained"
    DENIED = "denied"
    """
    [RESOLVED-6] DENIED PROPAGATES AS ITS OWN STATUS — never collapsed into
    ABSTAINED or EMPTY. "Blocked by permissions" and "searched and found
    nothing" are different facts, and flattening them destroys the audit
    signal that distinguishes them.
    """


class SubAgentInput(BaseModel):
    """
    [PRESERVE — design §4.4] `caller` is threaded through UNCHANGED to every
    tool the sub-agent invokes.
    """
    query_text: str
    caller: CallerContext
    output_format: Literal["chat", "file_pdf", "file_xlsx", "file_docx"] = "chat"
    conversation_context: Optional[str] = None


class Citation(BaseModel):
    """
    One citation in the bounded handoff payload.

    [PRESERVE — design §3] Deliberately NOT an EvidenceChunk: it carries NO
    chunk text, so the supervisor can render provenance without absorbing the
    evidence set. This omission is the boundary.
    """
    document_index: int = Field(
        description=(
            "1-based index matching the `[Document N]` marker in answer_text. "
            "[PRESERVE — design §5] Positional correspondence with the evidence list "
            "shown to the generator."
        )
    )
    source_tool: SourceTool
    case_id: Optional[str] = None
    source_file: Optional[str] = None
    confidence: Optional[float] = None

    def display_label(self) -> str:
        """[RESOLVED-1a] Investigator-facing source label. Never the raw enum name."""
        return SOURCE_TOOL_DISPLAY_LABELS[self.source_tool]


class ConflictState(str, Enum):
    """
    [RESOLVED-5] Three-state, deliberately NOT a bool.

    `UNKNOWN` is correct whenever conflict detection did not successfully run —
    distinct from `NONE`, which asserts the check ran and found nothing. A bool
    would render a failed check as an all-clear the system never verified: a
    correctness defect in an investigative context, not a stylistic choice.
    """
    CONFLICT = "conflict"
    NONE = "none"
    UNKNOWN = "unknown"


class TimelineEvent(BaseModel):
    """Timeline Building's per-event payload element."""
    event_id: str
    description: str
    occurred_on: Optional[str] = None
    conflict_state: ConflictState = Field(
        default=ConflictState.UNKNOWN,
        description=(
            "[RESOLVED-5] Renderers MUST distinguish UNKNOWN from NONE in user-facing "
            "output — presenting UNKNOWN as an unqualified all-clear reintroduces the "
            "defect the third state exists to prevent."
        ),
    )
    conflict_basis: Optional[str] = None
    locked: bool = False


class CrossCaseLink(BaseModel):
    """Cross-Case Linkage's per-item payload element."""
    description: str
    case_ids: list[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    source_tool: Literal["XGRAPH", "XNETWORK"]
    is_unconfirmed: bool = Field(
        default=False,
        description=(
            "[PRESERVE] True for pending identity links. MUST be presented as a caveat "
            "and MUST contribute a matching entry to SubAgentResult.caveats."
        ),
    )


class GeneratedFileRef(BaseModel):
    """Reference to a produced file. No file bytes cross the boundary."""
    file_id: str
    file_name: str
    storage_path: str
    disclosure_rendered: bool = Field(
        default=False,
        description=(
            "[RESOLVED-3] True iff a partial-evidence disclosure was written INTO THE "
            "DOCUMENT BODY. [RESOLVED-3a] Set at step 5 of the §2.1.3 ordering — after "
            "the Verifier passed and the document was assembled."
        ),
    )


class SubAgentResult(BaseModel):
    """
    THE BOUNDED HANDOFF PAYLOAD — what the supervisor receives.

    [PRESERVE — design §3] Carries NO EvidenceChunk list, no raw rows, no graph
    rows. Adding one would be the specific regression this layer exists to
    prevent. Evidence stays below this boundary.
    """
    status: SubAgentStatus
    answer_text: Optional[str] = Field(
        default=None,
        description=(
            "The synthesized, Verifier-passed answer. None when ABSTAINED or DENIED. "
            "[PRESERVE] An answer that failed verification is NEVER served."
        ),
    )
    citations: list[Citation] = Field(default_factory=list)
    tools_used: list[SourceTool] = Field(
        default_factory=list,
        description=(
            "[RESOLVED-4] Tools that ACTUALLY CONTRIBUTED DATA, measured AFTER all "
            "fallbacks resolved — NOT tools attempted. Deduplicated. Uniform across "
            "ALL sub-agents: a field meaning different things per producer would be "
            "unreadable at the consuming end."
        ),
    )
    degraded_from: list[SourceTool] = Field(
        default_factory=list,
        description=(
            "[RESOLVED-4] Tools ATTEMPTED but failed, empty, or fell back. A tool never "
            "appears in both lists for the same call. Non-empty implies PARTIAL."
        ),
    )
    caveats: list[str] = Field(
        default_factory=list,
        description=(
            "[PRESERVE — design §3] User-facing qualifications that MUST survive to the "
            "final response. The supervisor may reorder or reformat; it may not drop."
        ),
    )
    generated_file: Optional[GeneratedFileRef] = None
    error: Optional[ToolError] = None


class SubAgent(Protocol):
    """Structural contract every sub-agent satisfies."""
    name: str

    async def __call__(self, agent_input: SubAgentInput) -> SubAgentResult: ...


# ══════════════════════════════════════════════════════════════════════════
# §2.2 — Logging contract
# ══════════════════════════════════════════════════════════════════════════

class PipelineEvent(BaseModel):
    """
    The live SSE trace event. Renders in the chat UI's trace panel; never
    touches a database.

    [PRESERVE — design §6] Granularity must not regress: ONE event per
    meaningful transition — supervisor dispatch, sub-agent start/end, tool
    fallback-triggered — NOT collapsed into a single "sub-agent ran" event.
    """
    model_config = {"extra": "allow"}

    step: str
    status: Literal["active", "done", "error", "retry", "skipped"]
    detail: str
    ms: Optional[int] = None
    sources: Optional[list[dict[str, Any]]] = None


_STEP_STATUS_MAP: dict[str, str] = {
    "active": "success",
    "done": "success",
    "error": "failed",
    "retry": "retry",
    "skipped": "skipped",
}
"""
[PRESERVE — design §6] SSE's five-value vocabulary remapped onto the four
values the Postgres `pipeline_steps` CHECK constraint allows. `active` has no
durable equivalent; `done` maps to `success`.
"""


def to_step_status(sse_status: str) -> str:
    """Map an SSE event status to its durable `log_step` equivalent."""
    return _STEP_STATUS_MAP[sse_status]
