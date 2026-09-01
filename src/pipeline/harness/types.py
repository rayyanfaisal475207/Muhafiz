"""
Agent Harness — shared types (Phase 0, foundation layer).

Source of truth: docs/SUBAGENT_INTERFACES.md (the exact contracts) and
docs/AGENT_HARNESS_DESIGN.md (the *why* behind each [PRESERVE] note this
module quotes from). This file is a verbatim-as-Python-allows transcription
of SUBAGENT_INTERFACES.md's Pydantic models — no retrieval/generation logic,
no behavior beyond the one validator that enforces an invariant the
interfaces doc itself states (see ChunkMetadata below).

SCOPE OF THIS FILE. SUBAGENT_INTERFACES.md §0 ("Shared types") in full,
plus the parts of §1 ("Tool interfaces") that are genuinely common across
every tool — §1.0's `ToolInput`/`ToolResult`/`Tool` base shapes, §1.1's
`CrossCaseToolInput`/`CrossCaseToolResult` cross-case base, and §1.3.1's
`SOURCE_TOOL_DISPLAY_LABELS` display contract — plus §2 ("Sub-agent
interfaces")'s shared shapes. The SEVEN tool-SPECIFIC Input/Result pairs
(RagToolInput/Result, GraphToolInput/Result, XGraphToolInput/Result,
XAggToolInput/Result, XNetworkToolInput/Result, SqlToolInput/Result,
WebToolInput/Result, plus XAGG's `AggregateKind`) are deliberately NOT
here — each lives beside its own wrapper in src/pipeline/harness/tools/,
since each is used by exactly one tool file and its own tests, and
SUBAGENT_INTERFACES.md itself organizes them one-per-subsection (§1.2 RAG,
§1.3 GRAPH, §1.4 XGRAPH, §1.5 XAGG, §1.6 XNETWORK, §1.7 SQL, §1.8 WEB).
This split keeps exactly one canonical definition of every type (nothing to
drift between two copies) while keeping each tool wrapper file
self-contained for its own contract.

PHASE 0 BUILD NOTE. The Supervisor and all 8 sub-agents are OUT OF SCOPE
for this session (AGENT_HARNESS_IMPLEMENTATION_PLAN.md §2 restricts this
session to §2's Foundation layer only; see also §8's build checklist).
The §2 sub-agent shapes below (SubAgentResult, Citation, TimelineEvent,
CrossCaseLink, PipelineEvent, ...) are forward-declared verbatim so a later
session building those layers imports the same canonical types instead of
redefining them — this is exactly what SUBAGENT_INTERFACES.md's own
"stability contract" (no implementation types cross a boundary, contracts
survive a rewrite of the internals) calls for. Nothing in Phase 0's own
code (the tool wrappers, the compliance suite) constructs or imports these
§2 types — they are inert until the Supervisor/sub-agent phases wire them
in.

CONFIDENCE FIELD SPLIT (the one amendment to SUBAGENT_INTERFACES.md's
literal text, per AGENT_HARNESS_IMPLEMENTATION_PLAN.md §1, resolving the
gap AGENT_HARNESS_DESIGN.md §7 tracked and deliberately left open):
`ChunkMetadata.confidence` alone cannot distinguish "this tool computes no
confidence for this chunk" (flat retrieval — legitimately absent) from
"confidence computation was attempted and failed" (unknown) — both read as
`None`. The Verifier's hedging check needs the two to be distinguishable:
a genuinely low-confidence chain that failed to score must still read as
"needs hedging," not silently pass as "no confidence signal present."
`confidence_status` makes the three cases explicit and, per the validator
below, mutually consistent with `confidence` itself.

CONTRACT AMENDMENT — ExecutionContext & ConversationContext (post-Phase-2,
AGENT_HARNESS_IMPLEMENTATION_PLAN.md §10, verbatim per §10.1/§10.2):
`ToolInput.caller: CallerContext` and `SubAgentInput.caller: CallerContext`
are RENAMED to `execution: ExecutionContext` — not additive. Every call
site that read `.caller.role`, `.caller.active_case_id`, etc. now reads
`.execution.caller.role`, `.execution.caller.active_case_id`.
`ExecutionContext` wraps `CallerContext` (does not replace or flatten it)
and adds scope that sits ABOVE a single case — `project_id` (restoring the
project/global precedence rule RAG's Phase-0 tool wrapper had to drop, see
§9's Phase 0 entry) plus `workspace_id`/`organization_id`/`feature_flags`,
reserved and inert for MVP. `SubAgentInput.conversation_context` is
upgraded from `Optional[str]` to `Optional[ConversationContext]` — same
field name and slot, richer shape; still pre-bounded by the supervisor,
never full history.

CONTRACT AMENDMENT — SubAgentResult.metrics / DataQualityMetric /
DataQualityReadiness (pre-Phase-9, Data-Quality/Extraction-Coverage):
SUBAGENT_INTERFACES.md never defines a `SubAgentResult`-shaped contract for
this sub-agent at all (its §2.1 table is titled "the seven sub-agents");
AGENT_HARNESS_IMPLEMENTATION_PLAN.md §7.3 specifies six metric groups, raw
counts, and a per-capability readiness state, but no field to carry them.
Resolved via AskUserQuestion as a new typed field (mirroring the `events`/
`links` precedent for Timeline Building/Cross-Case Linkage) rather than
structured prose in `answer_text`. See the block comment directly above
`DataQualityReadiness`, below, for the full readiness-state rule.

CONTRACT AMENDMENT — SubAgentResult.validation_status / .validation_claims,
ValidationStatus / ClaimSupport / ValidationClaimResult (pre-Validation-module):
every `# TODO(validation-gate)` marker left across Phases 2-8 says the same
thing: `validation.py` (AGENT_HARNESS_IMPLEMENTATION_PLAN.md §5/§7.1/§7.2)
does not exist yet, and `SubAgentResult` has no field to carry its result.
Resolved via AskUserQuestion, before any `validation.py` code was written,
as a status enum PLUS a per-claim result list (mirroring the
`DataQualityMetric` precedent of a small typed list over folding detail into
`caveats` as plain strings) — see the block comment directly above
`ValidationStatus`, below, for the full status-value rule and the
caveat-only (never-blocking) outcome decision.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Literal, Optional, Protocol

from pydantic import BaseModel, Field, model_validator

from src.data_gateway.base import DataGateway

# ═══════════════════════════════════════════════════════════════════════
# §0 — Roles
# ═══════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════
# §0 — Identity / scope, threaded through every hop
# ═══════════════════════════════════════════════════════════════════════


class CallerContext(BaseModel):
    """
    Who is asking, and under what case scope. Threaded supervisor → sub-agent
    → tool, unchanged, at every hop.

    [PRESERVE — design §4.4] `role` MUST originate from the authenticated
    user's real RBAC role (`current_user.role` / `process_query()`'s
    `user_role` parameter today). It must NEVER be read out of a
    user-profile / preferences object (`user_profile` —
    `{context_text, preferred_language, llm_mode}`, no role key at all).
    Those objects carry no role key, and a historical bug that read one
    silently defaulted every cross-case check to `investigator`, denying
    real supervisors/admins their own access. This model exists as a
    separate type from any profile/preferences model specifically so the
    two cannot be confused at a call site.

    [PRESERVE — design §4.1, §4.2] Construction of this object does NOT
    grant access. Case-access authorization (hard 403 — `main.py`'s
    `chat_endpoint()` calling `gateway.check_case_access()`) and RLS scope
    arming (`src.auth.rls_context.set_case_scope()`) are the API boundary's
    responsibility and happen strictly BEFORE the supervisor is invoked. A
    `CallerContext` that reaches a tool is assumed already authorized for
    `active_case_id` — it is not re-checked below the boundary. Any NEW
    entry point (scheduled job, internal API, batch runner) must perform
    both steps itself; they are not inherited.
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


class ExecutionContext(BaseModel):
    """
    The environment a query executes in. Threaded through supervisor ->
    sub-agent -> tool in place of a bare CallerContext.

    [PRESERVE] Wraps CallerContext rather than replacing or flattening it —
    every existing [PRESERVE] rule on CallerContext (above: role must
    originate from the authenticated user's real RBAC role, never a
    profile/preferences object; construction does not grant access) applies
    unchanged to the nested `caller` field. This type only adds scope that
    sits ABOVE a single case.

    [PRESERVE, extends design §4.4] Threaded unchanged at every hop, exactly
    like CallerContext was before it — not reconstructed, not merged with
    any profile/preferences object, not partially copied.
    """

    caller: CallerContext
    project_id: Optional[str] = Field(
        default=None,
        description=(
            "Project-level scope. Restores the project/global precedence "
            "rule RAG's Phase-0 tool wrapper had to drop for lack of a "
            "carrier (AGENT_HARNESS_IMPLEMENTATION_PLAN.md §9, Phase 0 "
            "entry). None = no project scoping applied — same behavior as "
            "the Phase 0/1/2 code shipped with."
        ),
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "[AMENDMENT — pre-Phase-8 contract amendment] The chat session "
            "this query belongs to. Needed by Report Drafting to persist a "
            "generated file via DataGateway.log_generated_file(), which "
            "requires session_id alongside user_id (already carried on "
            "`caller`) — mirrors _generate_file()'s existing "
            "session_id/user_id/case_id shape. None where no session exists "
            "(e.g. a standalone harness invocation or most existing tests) "
            "— every sub-agent besides Report Drafting is unaffected."
        ),
    )
    workspace_id: Optional[str] = None
    organization_id: Optional[str] = None
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    # workspace_id / organization_id / feature_flags are RESERVED, UNUSED
    # for MVP. Nothing may branch on them until a real spec exists for
    # each — the point of adding them now is to avoid a second
    # signature-wide change later, not to start building against them today.


class ConversationContext(BaseModel):
    """
    Bounded, pre-summarized conversation/session context for a sub-agent.

    [PRESERVE, carried forward from the original conversation_context
    field] Bounding is the supervisor's job — a sub-agent never receives
    full history, full project memory, or raw attachment bytes. This
    object being richer than a bare string does not relax that rule; every
    field on it must already be pre-bounded/summarized by the time it
    reaches a sub-agent.
    """

    summary: Optional[str] = None
    project_memory: Optional[str] = None
    attachment_refs: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════
# §0 — Evidence
# ═══════════════════════════════════════════════════════════════════════

SourceTool = Literal["RAG", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "XNETWORK", "SQL", "WEB"]

ConfidenceStatus = Literal["computed", "not_computed", "check_failed"]


class EvidenceChunk(BaseModel):
    """
    The single evidence currency of the entire harness. Every tool emits
    these; every sub-agent hands these to the Verifier.

    [PRESERVE — design §5] This flat shape is the Verifier's contract and
    must not be replaced with a richer nested/grouped payload. The
    Verifier's deterministic checks parse `[Document N]` citations out of
    the generated answer and index positionally into the chunk list
    (`chunks[n-1]`). A sub-agent composing several tools MUST flatten to
    one ordered list, and the list it hands the Verifier MUST be exactly
    the list the generator was shown — same objects, same order. Generator
    and Verifier disagreeing about what evidence was displayed is a
    correctness regression, not a cosmetic one.
    """

    id: str
    text: str
    metadata: "ChunkMetadata"
    score: Optional[float] = Field(
        default=None,
        description=(
            "Opaque relevance score. Higher is better WITHIN one result set "
            "only. Never threshold on an absolute value; never compare "
            "across tools."
        ),
    )


class ChunkMetadata(BaseModel):
    """
    Open-ended per-chunk metadata. Consumers read known keys, ignore
    unknown ones. Adding keys is non-breaking.

    [PRESERVE — design §5 tradeoff] `source_tool` is REQUIRED on every
    chunk emitted by any tool. It is what preserves per-tool provenance
    once several tools' output is flattened into one list — the Verifier
    sees one merged list and would otherwise have no way to tell
    RAG-sourced from GRAPH-sourced evidence. This extends the existing
    per-source metadata convention (graph-confidence, conflict-basis, etc.)
    by one field; it is not a new mechanism.
    """

    model_config = {"extra": "allow"}

    source_tool: SourceTool = Field(
        description="[PRESERVE] Which tool produced this chunk. Required on every chunk."
    )
    case_id: Optional[str] = Field(
        default=None,
        description=(
            "Owning case. [PRESERVE — design §4.6] Read by the Verifier's "
            "leakage backstop, which re-derives cited chunks' case_id from "
            "the answer text and checks it against the active case plus any "
            "explicitly allowed cross-case IDs. A chunk that omits this "
            "cannot be leak-checked."
        ),
    )
    source_file: Optional[str] = None
    confidence: Optional[float] = Field(
        default=None,
        description=(
            "Per-chunk confidence, set ONLY when confidence_status=='computed' "
            "(graph traversal attaches chain confidence when it succeeds; "
            "flat retrieval never computes one at all — legitimately absent). "
            "[PRESERVE] Drives the Verifier's hedging check: low-confidence "
            "evidence requires confidence-appropriate hedging in the "
            "generated answer.\n\n"
            "[AMENDMENT — design §7 / plan §1] Do not read this field alone "
            "to decide whether hedging is required: a `None` here is "
            "ambiguous on its own (nothing to compute vs. a failed "
            "computation) unless disambiguated by `confidence_status`. Use "
            "`confidence_status`, not `confidence is None`, as the branch "
            "condition."
        ),
    )
    confidence_status: ConfidenceStatus = Field(
        default="not_computed",
        description=(
            "[AMENDMENT — design §7 / plan §1] Disambiguates why `confidence` "
            "is `None` (or, for 'computed', asserts it is a real value).\n"
            "  'computed'      — confidence was actually computed; "
            "`confidence` is a real, non-None number.\n"
            "  'not_computed'  — this tool/route does not compute a "
            "confidence for this chunk at all (e.g. flat RAG retrieval). "
            "Legitimately absent, not a failure. DEFAULT.\n"
            "  'check_failed'  — confidence computation was attempted and "
            "raised/errored. `confidence` is `None` here NOT because there "
            "is nothing to hedge about, but because the check that would "
            "tell the Verifier whether to hedge never completed.\n\n"
            "This closes the exact gap design §7 flags: before this field "
            "existed, a genuinely low-confidence chain that failed to score "
            "read identically to 'no confidence signal present' and could "
            "pass the hedging check unhedged. A consumer (the Verifier's "
            "hedging check, once wired to read this field) MUST treat "
            "'check_failed' at least as cautiously as a known-low "
            "confidence score — never as 'no signal, proceed unhedged'."
        ),
    )

    @model_validator(mode="after")
    def _confidence_status_consistency(self) -> "ChunkMetadata":
        """
        Enforces the invariant the confidence_status split exists for:
        'computed' and a real value always travel together, never apart.
        Without this check, a tool could set confidence_status='computed'
        with confidence=None (or vice versa) and silently reopen the exact
        ambiguity this amendment was written to close.
        """
        if self.confidence_status == "computed" and self.confidence is None:
            raise ValueError(
                "ChunkMetadata.confidence_status=='computed' requires a "
                "non-None confidence value — a tool that has not actually "
                "computed a confidence must use 'not_computed' or "
                "'check_failed' instead."
            )
        if self.confidence_status != "computed" and self.confidence is not None:
            raise ValueError(
                "ChunkMetadata.confidence is set but confidence_status is "
                f"{self.confidence_status!r}, not 'computed' — a real "
                "confidence value must be flagged 'computed' or downstream "
                "consumers (the Verifier's hedging check) cannot trust it."
            )
        return self


EvidenceChunk.model_rebuild()


# ═══════════════════════════════════════════════════════════════════════
# §0 — Outcome discriminator
# ═══════════════════════════════════════════════════════════════════════


class ToolStatus(str, Enum):
    """
    Why a tool call ended the way it did. The discriminator every caller
    branches on. `OK` and `EMPTY` are distinct on purpose: `EMPTY` is a
    successful call that legitimately found nothing, and for several tools
    that is a materially different outcome from an error (see
    `fallback_to_rag`).
    """

    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"
    DENIED = "denied"


class ToolError(BaseModel):
    """
    Structured failure detail. Present iff status is FAILED or DENIED.

    [PRESERVE — design §4.3] A DENIED result means the caller's role failed
    a cross-case gate. That check happens INSIDE the tool, before the tool
    does anything else that touches cross-case scope, and writes an
    authorization-violation audit record. See `CrossCaseToolInput`.
    """

    kind: Literal["permission_denied", "upstream_failure", "invalid_input", "timeout"]
    message: str = Field(description="Operator-facing. Not shown verbatim to the end user.")


# ═══════════════════════════════════════════════════════════════════════
# §1.0 — Common tool shape (shared across every tool wrapper)
# ═══════════════════════════════════════════════════════════════════════


class ToolInput(BaseModel):
    """Base input. Every tool receives the execution context unchanged.

    [RENAMED — AGENT_HARNESS_IMPLEMENTATION_PLAN.md §10.1] Was `caller:
    CallerContext`; every call site now reads `.execution.caller.*`.
    """

    query_text: str = Field(description="The rewritten, standalone query.")
    execution: ExecutionContext


class ToolResult(BaseModel):
    """
    Base result. Every tool returns this shape — no tool raises to signal a
    routine outcome (empty result, permission denial, upstream failure).
    Those are all values, so composing sub-agents can branch without
    exception handling scattered through composition logic.
    """

    status: ToolStatus
    chunks: list[EvidenceChunk] = Field(
        default_factory=list,
        description=(
            "Evidence, ordered. Non-empty iff status is OK. Every chunk "
            "carries metadata.source_tool identifying its producer."
        ),
    )
    error: Optional[ToolError] = Field(
        default=None, description="Present iff status is FAILED or DENIED."
    )
    fallback_to_rag: bool = Field(
        default=False,
        description=(
            "[PRESERVE — design §2.2, §2.6, §2.7] Whether the CALLER should "
            "now substitute the RAG tool for this tool's result. Only ever "
            "True for GRAPH, GRAPH_HYBRID, SQL, and WEB. PERMANENTLY False "
            "for XGRAPH, XAGG, and XNETWORK — see CrossCaseToolResult. The "
            "tool does not perform the fallback itself; it reports that one "
            "is warranted and the calling sub-agent acts on it. In today's "
            "orchestrator this is implicit in the branch structure, so the "
            "harness needs it made explicit."
        ),
    )


class Tool(Protocol):
    """Structural contract every primitive satisfies."""

    name: SourceTool

    async def __call__(self, tool_input: ToolInput) -> ToolResult: ...


# ═══════════════════════════════════════════════════════════════════════
# §1.1 — Cross-case tool base (the role gate)
# ═══════════════════════════════════════════════════════════════════════


class CrossCaseToolInput(ToolInput):
    """
    Input to XGRAPH / XAGG / XNETWORK.

    [PRESERVE — design §2.3, §4.3] Ordering inside every cross-case tool is
    load-bearing and must be preserved verbatim:

        1. Check caller.role against CROSS_CASE_ROLES.
        2. On failure: write an authorization-violation audit record,
           return status=DENIED. Nothing else runs.
        3. ONLY on success: arm cross-case / RLS-bypass scope, then query.

    This ordering is the fix for a documented historical bug in which the
    RLS cross-case bypass flag was armed as soon as the router classified a
    query as cross-case — before the role check ran, and never reset on
    denial. Arming strictly after the check means an unauthorized caller
    never arms it at all: there is no window to close because none is
    opened.

    A harness restructuring that hoists scope resolution "up" to the
    supervisor or a shared middleware, so that scope is armed before
    dispatch, REINTRODUCES THIS BUG. Do not do it. Each of the three tools
    carries its own independent copy of this check today; the harness must
    keep them independent (design §4.3 — none of the enforcement points
    supersedes another).
    """


class CrossCaseToolResult(ToolResult):
    """
    [PRESERVE — design §2.3, §2.4, §2.5] Cross-case tools NEVER fall back to
    RAG. Cross-case evidence must never blend into a case-scoped RAG answer
    stream — that is the structural separation the whole cross-case design
    rests on. `fallback_to_rag` is pinned False and callers must not
    reintroduce a fallback at their own level.
    """

    fallback_to_rag: Literal[False] = False
    case_ids_touched: list[str] = Field(
        default_factory=list,
        description=(
            "Every case ID contributing to this result. [PRESERVE — design "
            "§4.6] Feeds the Verifier's allowed-cross-case-ID list; without "
            "it the leakage backstop cannot distinguish legitimate "
            "cross-case evidence from a genuine leak, and would reject "
            "valid cross-case answers."
        ),
    )


# ═══════════════════════════════════════════════════════════════════════
# §1.3.1 — source_tool display contract (GRAPH_HYBRID is user-visible)
# ═══════════════════════════════════════════════════════════════════════

# Investigator-facing labels for metadata.source_tool. Consumers MUST NOT
# invent their own mapping, collapse distinct values, or fall back to the
# raw enum name in user-facing surfaces.
#
# [RESOLVED-1a] GRAPH_HYBRID is a DISTINCT label — never displayed as
# "GRAPH", never omitted. Wording below is indicative; see SUBAGENT_
# INTERFACES.md §3's sign-off note.
SOURCE_TOOL_DISPLAY_LABELS: dict[SourceTool, str] = {
    "RAG": "document search",
    "GRAPH": "case-graph search",
    "GRAPH_HYBRID": "combined document + case-graph search",
    "XGRAPH": "cross-case entity search",
    "XAGG": "cross-case aggregate",
    "XNETWORK": "cross-case pattern synthesis",
    "SQL": "penal-code reference lookup",
    "WEB": "external web search",
}


# ═══════════════════════════════════════════════════════════════════════
# §2 — Sub-agent interfaces (forward-declared; unused until the Supervisor
# / sub-agent phases wire them in — see the PHASE 0 BUILD NOTE at the top
# of this file). Included here, not deferred, so every later phase imports
# ONE canonical definition instead of each re-deriving its own.
# ═══════════════════════════════════════════════════════════════════════


class SubAgentStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"  # Degraded but useful.
    EMPTY = "empty"  # Legitimately nothing to report. NOT an error.
    ABSTAINED = "abstained"  # Could not answer safely. No answer text is served.
    DENIED = "denied"
    """
    [RESOLVED-6] A role gate refused the request (cross-case sub-agents
    only).

    DENIED PROPAGATES AS ITS OWN STATUS — it must never be collapsed into
    ABSTAINED or EMPTY. "Blocked by permissions" and "searched and found
    nothing" are different facts about the system, and flattening them
    destroys the audit and monitoring signal that distinguishes them: a
    spike in denials is a security-relevant event, a spike in empties is a
    data-coverage problem. They must stay separable downstream.
    """


# ═══════════════════════════════════════════════════════════════════════
# [CONTRACT AMENDMENT — pre-Validation-module]
#
# Every `# TODO(validation-gate)` marker left across Phases 2-8 points here:
# `validation.py` (AGENT_HARNESS_IMPLEMENTATION_PLAN.md §5/§7.1/§7.2) reuses
# the Verifier's own `[Document N]` citation parse and runs, per cited
# (claim, chunk) pair, a three-way entailment check — SUPPORTED /
# PARTIALLY_SUPPORTED / NOT_SUPPORTED + a one-line reason — in one batched
# call per answer, AFTER the Verifier has already passed. Two tiers exist
# (plan §5's table): the FULL semantic tier (one local-LLM entailment call,
# all three labels) is mandatory on Cross-Case Linkage / Investigative
# Analysis / Report Drafting; the STRUCTURAL-ONLY tier (Semantic Search /
# Large-Scale Aggregate / Case Summarization) is resolved — via
# AskUserQuestion, before any validation.py code was written — as
# DETERMINISTIC-ONLY: no LLM call at all, a regex/string-level check that
# numbers/dates/named entities appearing in a claim also appear in (or are
# not contradicted by) its cited chunk's text. This doubles as a concrete
# reading of plan §7.2's own fallback plan ("narrow the check's scope to
# numeric/name/date contradiction only") for the tier that already asks for
# something narrower by design, not merely what every sub-agent would fall
# back to if the local model fails the pre-work eval.
#
# OUTCOME ON A FLAGGED CLAIM — resolved via AskUserQuestion, CAVEAT-ONLY,
# NEVER BLOCKING: a PARTIALLY_SUPPORTED or NOT_SUPPORTED claim adds an entry
# to `SubAgentResult.caveats` (human-readable) and to `validation_claims`
# (machine-readable) but never changes `status`/`answer_text`. Validation is
# a second-opinion signal layered on the Verifier's already-fail-closed
# gate, not a second independent hard gate — an answer that already passed
# full grounding verification is still served. This is consistent with
# plan §7.1's fail-OPEN posture on a Validation-call ERROR; this extends the
# same non-blocking posture to a Validation call that SUCCEEDS and finds an
# issue, so a flaky or overly strict second-opinion check can never be the
# reason a grounded, cited answer never reaches the investigator. The one
# documented exception is unchanged and unaffected by this rule: Report
# Drafting renders a caveat's substance into the document body via the
# EXISTING §2.1.1-§2.1.3 disclosure mechanism (a generated document outlives
# the session, per plan §7.1) — not a second, competing disclosure path.
# ═══════════════════════════════════════════════════════════════════════


class ClaimSupport(str, Enum):
    """
    Three-way entailment verdict for one (claim, cited-chunk) pair. Three
    labels, not a bool, for the same reason `ConflictState`/
    `ConfidenceStatus` are: a binary cannot distinguish "checked and fine"
    from "checked, and it's shaky but not wrong" from "checked, and it's
    wrong" — collapsing the last two together would treat an overstated
    claim the same as a fabricated one, and collapsing the first two would
    treat a fully-supported claim the same as a shaky one.
    """

    SUPPORTED = "supported"  # The chunk fully supports the claim as stated.
    PARTIALLY_SUPPORTED = "partially_supported"  # The chunk supports part of the claim, or supports it with a qualification the claim omits.
    NOT_SUPPORTED = "not_supported"  # The chunk does not support the claim, or contradicts it.


class ValidationStatus(str, Enum):
    """
    Rollup of the Validation check for one `SubAgentResult`. See the block
    comment above `ClaimSupport` for the full status-value rule and the
    caveat-only outcome decision this drives.
    """

    NOT_RUN = "not_run"
    """
    [PRESERVE — plan §7.1, fail OPEN] The Validation call was ATTEMPTED
    (this sub-agent has a real Verifier-boundary insertion point and an
    answer worth checking) but the call itself errored — a parse failure,
    an LLM-client exception, a timeout. Per plan §7.1, the opposite posture
    from the Verifier: the answer has ALREADY passed full grounding
    verification, so a flaky second-opinion service must not block it.
    Serve the answer unchanged; attach a caveat naming the check as
    not-run, never a fabricated/guessed result.
    """
    PASSED = "passed"
    """The check ran successfully and every cited claim was SUPPORTED."""
    ISSUES_FOUND = "issues_found"
    """
    The check ran successfully and found >=1 PARTIALLY_SUPPORTED or
    NOT_SUPPORTED claim. [PRESERVE — caveat-only outcome decision] Does NOT
    by itself change `status`/`answer_text` — see the block comment above
    `ClaimSupport`.
    """
    SKIPPED = "skipped"
    """
    DEFAULT. Validation was never attempted for this result — either this
    sub-agent has no generated evidentiary text / no Verifier-boundary
    insertion point at all (Timeline Building, Data-Quality — the permanent
    value for both), or this particular result has no `answer_text` to
    check (EMPTY/ABSTAINED/DENIED — nothing was generated, so there is
    nothing for Validation to entail-check). Distinct from `NOT_RUN`:
    `SKIPPED` means "not applicable here," `NOT_RUN` means "applicable, and
    attempted, but the check itself failed."
    """


class ValidationClaimResult(BaseModel):
    """
    One (claim, cited-chunk) entailment verdict. Bounded like every other
    §2 payload element — no chunk text, no raw evidence, only what a
    consumer needs to render or audit the finding.
    """

    document_index: int = Field(
        description=(
            "1-based index matching the `[Document N]` marker the claim "
            "was cited against — the same positional index as "
            "Citation.document_index, since Validation reuses the "
            "Verifier's own citation parse (plan §7.1)."
        )
    )
    claim_excerpt: str = Field(
        description=(
            "A short, bounded excerpt of the claim text this verdict is "
            "about — enough to identify which sentence/claim in "
            "answer_text is being described, not the full answer."
        )
    )
    support: ClaimSupport
    reason: str = Field(
        description=(
            "One-line reason. From the LLM judge on the full semantic tier; "
            "a fixed, deterministic message (naming the specific "
            "number/date/name mismatch found) on the structural-only tier."
        )
    )


class SubAgentInput(BaseModel):
    """
    [PRESERVE — design §4.4] `execution` (and its nested `caller`) is
    threaded through UNCHANGED to every tool the sub-agent invokes. Do not
    reconstruct it, do not merge it with a preferences/profile object, do
    not default its role.

    [RENAMED — AGENT_HARNESS_IMPLEMENTATION_PLAN.md §10.1] Was `caller:
    CallerContext`; every call site now reads `.execution.caller.*`.
    """

    query_text: str = Field(description="Rewritten, standalone query.")
    execution: ExecutionContext
    output_format: Literal["chat", "file_pdf", "file_xlsx", "file_docx"] = "chat"
    conversation_context: Optional[ConversationContext] = Field(
        default=None,
        description=(
            "Pre-bounded conversation context, if the supervisor supplies "
            "any. Bounding is the supervisor's job — a sub-agent never "
            "receives full history. [UPGRADED — plan §10.2] Was "
            "Optional[str]; same field/slot, richer shape."
        ),
    )
    target_entity: Optional[str] = Field(
        default=None,
        description=(
            "[Reconciliation fix — harness-reconciliation Unit 4/7/8] The "
            "entity the query is about, as identified by the router — the "
            "same `route_result['target_entity']` the legacy orchestrator "
            "reads and forwards into every `retrieve_graph()`/aggregate "
            "call it makes. PER-QUERY ROUTING METADATA, which is why it "
            "lives here and NOT on `CallerContext` alongside `project_id`: "
            "caller scope is a property of who is asking and stays "
            "constant for the session, while this changes with each "
            "question and is derived from the query text.\n\n"
            "Dropping it does NOT fail closed — it fails SILENT, and "
            "differently per tool. XGRAPH/XAGG with `target_entity=None` "
            "return a case-count/generic-recurrence result even when the "
            "user asked about one specific entity, which reads as a "
            "correct-but-wrong answer rather than an obvious failure. "
            "`Supervisor.handle()` populates this from `route_result` "
            "before dispatch when the caller did not already set it "
            "explicitly (an explicit value on the input always wins)."
        ),
    )


class Citation(BaseModel):
    """
    One citation in the bounded handoff payload. Deliberately NOT an
    EvidenceChunk: it carries no chunk text, so the supervisor can render
    provenance without absorbing the evidence set.
    """

    document_index: int = Field(
        description=(
            "1-based index matching the `[Document N]` marker in "
            "answer_text. [PRESERVE — design §5] Positional correspondence "
            "with the evidence list shown to the generator; the Verifier "
            "depends on it."
        )
    )
    source_tool: SourceTool
    case_id: Optional[str] = None
    source_file: Optional[str] = None
    confidence: Optional[float] = None


class GeneratedFileRef(BaseModel):
    """Reference to a produced file. No file bytes cross the boundary."""

    file_id: str
    file_name: str
    storage_path: str
    disclosure_rendered: bool = Field(
        default=False,
        description=(
            "[RESOLVED-3] True iff a partial-evidence disclosure was "
            "written INTO THE DOCUMENT BODY. Set only by the builder that "
            "actually rendered it — this field is an assertion about the "
            "file's contents, so it must never be set optimistically by a "
            "caller that merely intended a disclosure.\n\n"
            "[RESOLVED-3a] Set at step 5 of SUBAGENT_INTERFACES.md §2.1.3's "
            "ordering — AFTER the Verifier has passed and the document is "
            "assembled. It is therefore never True on a report that failed "
            "verification: such a report is never built at all."
        ),
    )


class SubAgentResult(BaseModel):
    """
    THE BOUNDED HANDOFF PAYLOAD — what the supervisor receives.

    [PRESERVE — design §3] Carries NO EvidenceChunk list, no raw rows, no
    graph rows. Adding one would be the specific regression this layer
    exists to prevent. Evidence stays below this boundary.
    """

    status: SubAgentStatus
    answer_text: Optional[str] = Field(
        default=None,
        description=(
            "The synthesized, Verifier-passed answer. None when status is "
            "ABSTAINED or DENIED. [PRESERVE] An answer that failed "
            "verification is NEVER served — the safe abstention is served "
            "instead, never the ungrounded draft."
        ),
    )
    citations: list[Citation] = Field(default_factory=list)
    tools_used: list[SourceTool] = Field(
        default_factory=list,
        description=(
            "[RESOLVED-4] Tools that ACTUALLY CONTRIBUTED DATA to "
            "answer_text, measured AFTER all fallbacks resolved — NOT tools "
            "attempted. A tool that was invoked and then degraded past does "
            "not appear here; it appears in `degraded_from`. Uniform across "
            "all seven sub-agents. Deduplicated: if two tools both degrade "
            "to RAG, RAG appears once."
        ),
    )
    degraded_from: list[SourceTool] = Field(
        default_factory=list,
        description=(
            "[RESOLVED-4] Tools that were ATTEMPTED but failed, returned "
            "empty, or fell back — the counterpart to `tools_used`. "
            "Non-empty implies status=PARTIAL for every sub-agent that can "
            "degrade."
        ),
    )
    caveats: list[str] = Field(
        default_factory=list,
        description=(
            "[PRESERVE — design §3] User-facing qualifications that MUST "
            "survive to the final response — notably unconfirmed identity "
            "links, which are presented as caveats and never as confirmed "
            "fact. The supervisor may reorder or reformat these; it may not "
            "drop them."
        ),
    )
    generated_file: Optional[GeneratedFileRef] = Field(
        default=None, description="Set only by Report Drafting."
    )
    # [AMENDMENT — AGENT_HARNESS_IMPLEMENTATION_PLAN.md §11] `TimelineEvent`
    # and `CrossCaseLink` (below) were forward-declared in Phase 0 as the
    # per-item payload elements for Timeline Building / Cross-Case Linkage,
    # per SUBAGENT_INTERFACES.md §2.1, but `SubAgentResult` was never given
    # a field to actually carry either — the same gap `generated_file`
    # above already closed for Report Drafting. This applies that same
    # precedent to the two that were missed. Additive, default-empty: zero
    # effect on any already-shipped sub-agent (Semantic Search, Large-Scale
    # Aggregate, Case Summarization use neither field).
    events: list["TimelineEvent"] = Field(
        default_factory=list,
        description="Set only by Timeline Building. Ordered event list, each carrying its own conflict_state.",
    )
    links: list["CrossCaseLink"] = Field(
        default_factory=list,
        description="Set only by Cross-Case Linkage. Ranked cross-case connections.",
    )
    # [CONTRACT AMENDMENT — pre-Phase-9, Data-Quality/Extraction-Coverage]
    # Same forward-ref pattern as `events`/`links` above — `DataQualityMetric`
    # is defined below `SubAgentResult` in this file (near `CrossCaseLink`),
    # resolved by the same `SubAgentResult.model_rebuild()` call further
    # down. See the block comment above `DataQualityReadiness` for the full
    # rationale (AskUserQuestion-resolved: a new typed field, not prose in
    # `answer_text`).
    metrics: list["DataQualityMetric"] = Field(
        default_factory=list,
        description="Set only by Data-Quality/Extraction-Coverage. The six metric groups, per plan §7.3.",
    )
    # [CONTRACT AMENDMENT — pre-Validation-module] Unlike `events`/`links`/
    # `metrics` above, `ValidationStatus`/`ValidationClaimResult` are defined
    # ABOVE `SubAgentResult` (immediately after `SubAgentStatus`), not below
    # it — no forward ref needed, and it lets `validation_status` default to
    # the real enum member (`ValidationStatus.SKIPPED`) directly, the same
    # way `TimelineEvent.conflict_state` defaults to `ConflictState.UNKNOWN`
    # (ConflictState is likewise defined before its user). See the block
    # comment above `ValidationStatus` for the full rationale and the
    # status-value rule.
    validation_status: ValidationStatus = Field(
        default=ValidationStatus.SKIPPED,
        description=(
            "[AMENDMENT — plan §5/§7.1] Rollup of the Validation trust-layer "
            "check (src/pipeline/validation.py), run AFTER the Verifier "
            "passes. Default 'skipped' — additive, zero effect on any "
            "sub-agent that has not been retrofitted to set it yet, and the "
            "permanent value for Timeline Building/Data-Quality (no "
            "generated evidentiary text, no Verifier-boundary insertion "
            "point to check). See ValidationStatus for the full four-value "
            "rule."
        ),
    )
    validation_claims: list[ValidationClaimResult] = Field(
        default_factory=list,
        description=(
            "Per-(claim, cited-chunk) entailment verdicts from the "
            "Validation check. Empty whenever validation_status is "
            "'not_run' or 'skipped' — there is nothing to list. "
            "[PRESERVE — caveat-only outcome decision, see ValidationStatus] "
            "A NOT_SUPPORTED or PARTIALLY_SUPPORTED entry here does NOT by "
            "itself change `status`/`answer_text` — Validation is a "
            "second-opinion signal layered on the Verifier's hard gate, not "
            "a second hard gate. Consumers that want the human-readable "
            "form should read `caveats`, which the check also populates."
        ),
    )
    error: Optional[ToolError] = None


# [AMENDMENT — pre-Phase-7] The exact callback shape `Supervisor.handle()`
# has accepted since Phase 1 (supervisor.py), named here so `SubAgent` and
# every sub-agent module can reference one canonical alias instead of each
# spelling out `Optional[Callable[[PipelineEvent], None]]` itself.
# `PipelineEvent` is defined further below in this file (§2.2) — a plain
# forward-reference string, the same pattern `TimelineEvent`/`CrossCaseLink`
# already use for their own forward refs onto `SubAgentResult`.
OnEventCallback = Callable[["PipelineEvent"], None]


class SubAgent(Protocol):
    """
    Structural contract every sub-agent satisfies.

    [AMENDMENT — pre-Phase-7 contract amendment, mirroring
    AGENT_HARNESS_IMPLEMENTATION_PLAN.md §10/§11's pattern] `on_event` is a
    new, additive, keyword-only parameter. It reuses the exact
    `Optional[Callable[[PipelineEvent], None]]` shape `Supervisor.handle()`
    has always accepted (supervisor.py, Phase 1) — no new type, no new
    transport, no delivery mechanism invented. It closes the gap
    SUBAGENT_INTERFACES.md §2.1.4/RESOLVED-4a assumed already closed:
    `Supervisor.handle()` had an `on_event` sink but nothing to hand it to,
    since `SubAgent.__call__` took only `agent_input`. A sub-agent that
    composes a single tool (every sub-agent through Phase 6) has nothing
    granular to report and may ignore the parameter entirely — it is
    additive with a `None` default, so every already-shipped sub-agent
    keeps working once retrofitted to accept-and-ignore it. Investigative
    Analysis (Phase 7) is the first to actually use it, per
    SUBAGENT_INTERFACES.md §2.1.4's requirement that it emit one
    `PipelineEvent` per source-tool outcome as it resolves.

    [AMENDMENT — pre-Phase-8 contract amendment, same widen-not-replace
    pattern as `on_event` above] `gateway` is a new, additive, keyword-only
    parameter carrying an optional `DataGateway` (an already-abstract
    `Protocol`, `src/data_gateway/base.py` — no concrete implementation type
    crosses this boundary). It exists so Report Drafting can persist a
    generated file via `gateway.log_generated_file()`, matching
    `_generate_file()`'s existing behavior, without inventing a new
    ad hoc side-channel. Defaults to `None`; every sub-agent besides Report
    Drafting accepts-and-ignores it, exactly like `on_event` before Phase 7.
    """

    name: str

    async def __call__(
        self,
        agent_input: SubAgentInput,
        *,
        on_event: Optional[OnEventCallback] = None,
        gateway: Optional[DataGateway] = None,
    ) -> SubAgentResult: ...


# [RESOLVED-3] PLACEHOLDER — EXACT WORDING PENDING PRODUCT SIGN-OFF.
# The MECHANISM is settled: when Report Drafting builds from a degraded
# summary, this line is rendered into the document body itself, naming the
# unavailable source(s). The sentence below is a stand-in; replace it
# wholesale once product signs off. Do not ship this string to
# investigators as-is.
#
# [RESOLVED-3a] INJECTED POST-VERIFICATION, AND NEVER VERIFIED. See the
# ordering contract in SUBAGENT_INTERFACES.md §2.1.3 — the disclosure is a
# meta-statement ABOUT the generation process, not an evidentiary claim
# drawn from the case. It has nothing to cite, so passing it through
# grounding or citation verification could trip the no-citation check and
# cause abstention — withholding the whole report BECAUSE it was honest
# about being partial.
#
# Consequences for anyone editing this constant:
#   - It is a FIXED, REVIEWED TEMPLATE. Only `{unavailable_sources}` is
#     substituted, from `degraded_from`. Never LLM-generated, never
#     paraphrased, never regenerated per-report.
#   - Because it bypasses verification, its trustworthiness rests entirely
#     on this string being human-reviewed. That is the tradeoff that makes
#     the bypass safe — do not make it dynamic.
PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE = (
    "[PLACEHOLDER — PENDING PRODUCT SIGN-OFF] This report was generated from "
    "partial evidence. The following source(s) were unavailable: {unavailable_sources}. "
    "Findings below reflect only the evidence that could be retrieved and should not "
    "be read as a complete account."
)


# [RESOLVED-2a] Case Summarization's own in-text disclosure, for the
# GRAPH-only case. Same mechanism as PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE
# above and governed by the same shared rules (SUBAGENT_INTERFACES.md
# §2.1.1); ordering for this one is in §2.1.2: fixed reviewed string,
# injected AFTER verification, never itself verified, never
# model-generated.
#
# Unlike the report template this takes no substitution — it names one
# specific gap. `status=PARTIAL` + `degraded_from` remain the
# machine-readable signal; this is the human-readable one, and it travels
# WITH THE TEXT rather than alongside it, so it survives being read,
# quoted, or pasted somewhere the payload metadata does not follow.
#
# [Phase 4 — Case Summarization] WORDING UPDATED from the bare
# "[PLACEHOLDER]" stand-in to the PROVISIONAL wording
# AGENT_HARNESS_IMPLEMENTATION_PLAN.md §7.4 proposes, per this session's
# explicit instruction to use §7.4's text (clearly marked provisional)
# rather than SUBAGENT_INTERFACES.md's own placeholder. STILL NOT FINAL —
# §7.4 itself lists "the actual wording sign-off" as still genuinely open.
# Do not ship this string to investigators as final, product-approved copy.
GRAPH_ONLY_SUMMARY_DISCLOSURE = (
    "[PROVISIONAL — PENDING PRODUCT SIGN-OFF; wording per "
    "AGENT_HARNESS_IMPLEMENTATION_PLAN.md §7.4] This summary was generated "
    "from case record links only — no document text was available for "
    "this case. It may omit details contained in case documents."
)


class ConflictState(str, Enum):
    """
    [RESOLVED-5] Three-state, deliberately NOT a bool.

    `UNKNOWN` is the correct value whenever conflict detection did not
    successfully run for an event — it is distinct from `NONE`, which
    asserts the check ran and found nothing. A bool cannot represent that
    difference, so a failed check would render as "no conflicts found":
    the timeline would silently assert an all-clear it never verified. In
    an investigative context that is a correctness defect, not a
    stylistic one.

    `UNKNOWN` is the DEFAULT: an event is not-yet-checked until a check
    succeeds. Anything constructing a TimelineEvent must set NONE
    explicitly, and may only do so on a successful check that found
    nothing.
    """

    CONFLICT = "conflict"  # Checked; a contradictory record exists.
    NONE = "none"  # Checked; no conflict found.
    UNKNOWN = "unknown"  # Not successfully checked. Assert nothing.


class TimelineEvent(BaseModel):
    """Timeline Building's per-event payload element."""

    event_id: str
    description: str
    occurred_on: Optional[str] = Field(default=None, description="ISO-8601. None if undated.")
    conflict_state: ConflictState = Field(
        default=ConflictState.UNKNOWN,
        description=(
            "Whether a contradictory record exists for this event. "
            "[PRESERVE] Flagged, never silently resolved — surfacing the "
            "contradiction IS the value. [RESOLVED-5] Three-state; see "
            "ConflictState. Renderers MUST distinguish UNKNOWN from NONE in "
            "user-facing output — presenting UNKNOWN as an unqualified "
            "all-clear reintroduces exactly the defect the third state "
            "exists to prevent."
        ),
    )
    conflict_basis: Optional[str] = Field(
        default=None,
        description="Human-readable basis. Present iff conflict_state is CONFLICT.",
    )
    locked: bool = Field(
        default=False, description="Investigator-locked against further automatic revision."
    )


class CrossCaseLink(BaseModel):
    """Cross-Case Linkage's per-item payload element."""

    description: str
    case_ids: list[str] = Field(description="Cases this connection spans.")
    confidence: Optional[float] = None
    source_tool: Literal["XGRAPH", "XNETWORK"]
    is_unconfirmed: bool = Field(
        default=False,
        description=(
            "[PRESERVE] True for pending identity links. Such an item MUST "
            "be presented as a caveat and must contribute a matching entry "
            "to SubAgentResult.caveats — never asserted as confirmed fact."
        ),
    )


# ═══════════════════════════════════════════════════════════════════════
# [CONTRACT AMENDMENT — pre-Phase-9, Data-Quality/Extraction-Coverage]
#
# SUBAGENT_INTERFACES.md §2.1's table is titled "The seven sub-agents" and
# never lists Data-Quality/Extraction-Coverage at all — AGENT_HARNESS_
# IMPLEMENTATION_PLAN.md §4/§7.3 adds it as an eighth, with a "final shape"
# (§7.3) but no SubAgentResult-shaped contract. supervisor.py's own module
# docstring already flags this discrepancy; this amendment is what actually
# closes the "no bounded-payload shape" half of it, per this session's
# explicit brief. (The "unreachable via classify_to_subagent()" half is a
# separate, still-open, tracked gap — see supervisor.py, unchanged here.)
#
# §7.3 requires SIX metric groups (document coverage, entity extraction,
# timeline readiness, identity health, conflict coverage, embedding
# coverage), each reporting raw counts plus a per-capability readiness
# state, with NO absolute "enough" thresholds for MVP ("no calibration data
# exists yet to set them correctly, and a wrong threshold is worse than
# none"). Resolved via AskUserQuestion, before any sub-agent code was
# written, as a NEW TYPED FIELD (`SubAgentResult.metrics`) rather than
# structured prose in `answer_text` — mirrors the `events`/`links`/
# `generated_file` precedent (§11's own amendment did the identical thing
# for Timeline Building/Cross-Case Linkage): six groups' worth of
# {name, readiness, counts, explains, error} is machine-legible tabular
# data, not a narrative, and a future consumer (even the explicitly-parked
# "auto-attach to another sub-agent's degraded output" idea, §7.3's own
# "Routable-only for MVP" note) would need structured access, not regex
# over prose. Additive, default-empty — zero effect on any already-shipped
# sub-agent, same as §11's own amendment.
#
# READINESS RULE (this session's own provisional resolution, per §7.3's
# explicit "no absolute thresholds" instruction — not silently picked):
#   UNAVAILABLE — the group's own defining raw count is exactly 0. Checked,
#     and there is genuinely nothing there.
#   READY       — the group's own defining raw count is > 0. No further
#     grading (e.g. "5 is thin, 50 is ready") is attempted this session —
#     that grading needs calibration data that does not exist yet, per
#     §7.3, and a wrong threshold is worse than none.
#   THIN        — RESERVED, deliberately UNUSED this session. The enum
#     value exists so a future calibration pass can start using it without
#     another contract change; nothing in Phase 9's own code ever
#     constructs it.
#   UNKNOWN     — the group's own underlying query raised, rather than
#     returning (possibly zero) rows. This is the SAME "checked-and-empty
#     vs. check-failed" ambiguity RESOLVED-5 (ConflictState) and the
#     `confidence`/`confidence_status` split (design §7) already exist to
#     fix elsewhere in this contract — reported as UNAVAILABLE, a query
#     failure would silently read as "checked, nothing there," which is
#     the exact false-all-clear defect those two precedents were written
#     to prevent. `error` is set iff `readiness == UNKNOWN` and carries the
#     operator-facing exception detail (never shown verbatim to the end
#     user, same posture as `ToolError.message`).
# ═══════════════════════════════════════════════════════════════════════


class DataQualityReadiness(str, Enum):
    """[CONTRACT AMENDMENT — pre-Phase-9] See the block comment above this
    class for the full readiness rule and why UNKNOWN is a distinct fourth
    state rather than folded into UNAVAILABLE."""

    READY = "ready"
    THIN = "thin"  # Reserved for a future calibration pass. Unused for MVP.
    UNAVAILABLE = "unavailable"  # Checked; the defining raw count is 0.
    UNKNOWN = "unknown"  # The underlying query itself raised. Assert nothing.


class DataQualityMetric(BaseModel):
    """
    [CONTRACT AMENDMENT — pre-Phase-9] One of Data-Quality's six metric
    groups (AGENT_HARNESS_IMPLEMENTATION_PLAN.md §7.3's table). Never
    carries raw Postgres rows or graph rows — `counts` is a small, named,
    already-aggregated dict (e.g. {"person": 12, "vehicle": 3}), consistent
    with the "no raw rows cross a sub-agent boundary" discipline
    SUBAGENT_INTERFACES.md establishes everywhere else, even though this
    sub-agent has no contract written for it there at all.
    """

    name: Literal[
        "document_coverage",
        "entity_extraction",
        "timeline_readiness",
        "identity_health",
        "conflict_coverage",
        "embedding_coverage",
    ] = Field(description="Stable machine key. See §7.3's table for the canonical six.")
    label: str = Field(description="Human-facing name, e.g. 'Document coverage'.")
    readiness: DataQualityReadiness
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Raw, already-aggregated counts. Empty iff readiness is UNKNOWN.",
    )
    explains: str = Field(
        description=(
            "What this metric group explains about another sub-agent's thin "
            "results, per §7.3's own 'Explains' column (e.g. 'Sparse graph "
            "traversal' for entity_extraction). Static, per `name` — not "
            "computed from this call's own data."
        )
    )
    error: Optional[str] = Field(
        default=None,
        description="Operator-facing exception detail. Present iff readiness is UNKNOWN.",
    )


# `SubAgentResult.events`/`.links` (§11 amendment above) reference
# `TimelineEvent`/`CrossCaseLink` as forward refs (both are defined below
# `SubAgentResult` in this file) — same pattern `EvidenceChunk.model_rebuild()`
# already uses for its own forward ref to `ChunkMetadata`. Must run after
# both referenced classes exist, hence placed here rather than immediately
# after `SubAgentResult`'s own definition.
SubAgentResult.model_rebuild()


# ═══════════════════════════════════════════════════════════════════════
# §2.2 — Logging contract (PipelineEvent type only; log_step() itself is
# an existing DataGateway method, not a harness type — see
# gateway.log_step() / DataGateway.log_step in src/data_gateway/.)
# ═══════════════════════════════════════════════════════════════════════


class PipelineEvent(BaseModel):
    """
    The live SSE trace event. Renders in the chat UI's trace panel; never
    touches a database.

    [PRESERVE] Granularity must not regress: ONE event per meaningful
    transition — supervisor dispatch, sub-agent start/end, tool
    fallback-triggered — NOT collapsed into a single "sub-agent ran" event.
    Collapsing makes the live trace strictly less informative than today's.
    """

    model_config = {"extra": "allow"}

    step: str
    status: Literal["active", "done", "error", "retry", "skipped"]
    detail: str
    ms: Optional[int] = None
    sources: Optional[list[dict[str, Any]]] = None


# ── Shared prompt fragments ─────────────────────────────────────────────────

# Every sub-agent that generates user-facing prose MUST include this in its
# system prompt.
#
# The same person was rendered under four different spellings across separate
# answers from one identical source record — رابعہ (the actual text in the
# case file) came back as رابعع, "Rabeeha", "Rabia", "رَبَعَه" and once
# "Raheela" (verify-log Finding L). To an investigator that reads as several
# different people and breaks matching an answer against the case file.
#
# The rule previously lived only in `prompts/final_response.txt`, which is the
# LEGACY orchestrator's prompt. With the harness enabled, answers are written
# by these sub-agents instead — none of which carried the rule — so the fix
# never reached the path actually serving traffic. Defining it once here and
# importing it means a new sub-agent cannot silently miss it, and the wording
# cannot drift between copies.
NAME_FIDELITY_RULE = (
    "\n\nNAMES AND IDENTIFIERS — REPRODUCE, NEVER RE-SPELL: Write every "
    "person, place, and station name EXACTLY as it appears in the documents "
    "above, in the source's own script. Do NOT translate, transliterate, "
    "romanize, anglicise, or 'correct' a name, and never substitute a "
    "similar-sounding one. If you add a romanization for the reader, put it "
    "in parentheses after the original — e.g. \"رابعہ (Rabia)\" — never "
    "instead of it. Identifiers (CNIC, FIR/case numbers, phone numbers, "
    "vehicle plates) must be reproduced character-for-character. The same "
    "person appearing under different spellings reads as different people "
    "and breaks matching your answer against the case file."
)
