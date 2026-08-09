# Agent Harness — Interface Contracts

**Status:** Extracted from [`docs/AGENT_HARNESS_DESIGN.md`](AGENT_HARNESS_DESIGN.md). Contracts
only — no implementation. Companion to that design doc, not a replacement for it: the *why*
behind each preservation requirement lives there, the *shape* lives here.

**Stability contract.** Everything below is written to survive a rewrite of the RAG and graph
internals. Concretely, that means:

- **No implementation types cross a boundary.** No ChromaDB objects, no AGE/Cypher row shapes, no
  SQLAlchemy models, no `asyncpg` records. Every boundary carries plain data.
- **Retrieval mechanism is never named in a contract.** `RagToolInput` says "retrieve relevant
  passages for this query within this scope" — it does not say embed→RRF→cross-rerank. That the
  current implementation does RRF at k=60 is an implementation fact, free to change.
- **Scores are opaque and non-comparable.** Any `score` field is "higher is better, within this
  one result set." No consumer may threshold on an absolute value, or compare a score from one
  tool against a score from another.
- **`metadata` is open.** Consumers read known keys and ignore unknown ones. Adding a key is
  never a breaking change; removing one listed as **required** below is.

Where a field's presence or value depends on behavior the design doc pinned down as a
preservation requirement, it is marked **[PRESERVE]** with a pointer to the design-doc section.

> **Six contract questions that the design doc left unspecified have since been resolved**, plus
> follow-ups surfaced in review (RESOLVED-3a) and in supervisor review (RESOLVED-1a, -2a, -4a).
> Decisions are recorded in [§3](#3-resolved-contract-decisions) and applied inline throughout,
> marked **[RESOLVED-n]**. Several carry consequences beyond their own scope — the three-state
> conflict flag (RESOLVED-5), the `tools_used` / `degraded_from` accuracy rule (RESOLVED-4,
> binding on every sub-agent rather than only the one that surfaced it), and the shared
> disclosure rules (§2.1.1, governing both Case Summarization and Report Drafting). No open
> contract questions remain.
>
> **The three supervisor-review revisions (RESOLVED-1a, -2a, -4a) all move in one direction:
> user-facing transparency.** What the system did — which sources it combined, which it lacked,
> and how each resolved live — is treated as material to the investigator's judgement, not as
> internal detail. Where they touch an earlier decision they *narrow* it rather than reverse it.
>
> One **known gap is tracked but deliberately unresolved**, in `AGENT_HARNESS_DESIGN.md` §7:
> `ChunkMetadata.confidence` carries the same "checked versus never-checked" ambiguity that
> RESOLVED-5 fixed for conflict state. It is not addressed here and `ChunkMetadata` is
> unchanged.

---

## 0. Shared types

```python
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, Field


# ── Roles ────────────────────────────────────────────────────────────────

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


# ── Identity / scope, threaded through every hop ─────────────────────────

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


# ── Evidence ─────────────────────────────────────────────────────────────

SourceTool = Literal["RAG", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "XNETWORK", "SQL", "WEB"]


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
    shown — same objects, same order. Generator and Verifier disagreeing about
    what evidence was displayed is a correctness regression, not a cosmetic one.
    """
    id: str
    text: str
    metadata: "ChunkMetadata"
    score: Optional[float] = Field(
        default=None,
        description=(
            "Opaque relevance score. Higher is better WITHIN one result set only. "
            "Never threshold on an absolute value; never compare across tools."
        ),
    )
    graph_confidence: Optional[float] = Field(
        default=None,
        description=(
            "COMPATIBILITY SHIM for `verifier.py::_check_hedging()`, which reads "
            "`chunk['graph_confidence']` off the TOP LEVEL of the chunk dict rather "
            "than out of `metadata`.\n\n"
            "Duplicates `metadata.confidence`, deliberately. Normalizing confidence "
            "into metadata — the contract's correct shape — silently broke the hedging "
            "check for every harness chunk: the verifier's lookup returned None, hit "
            "its `if gc is None: continue` guard, and low-confidence graph evidence "
            "passed UNHEDGED. The hedging gate has no backstop, so a silently-skipped "
            "check is a safety hole rather than a cosmetic mismatch.\n\n"
            "HARNESS CODE MUST READ `metadata.confidence`, NOT THIS FIELD. This exists "
            "only to satisfy the verifier's positional expectation, and is removable by "
            "§7's Part B (`confidence_state` sentinel), which reworks how the verifier "
            "reads confidence. Removing it earlier silently disables the hedging check "
            "again."
        ),
    )


class ChunkMetadata(BaseModel):
    """
    Open-ended per-chunk metadata. Consumers read known keys, ignore unknown
    ones. Adding keys is non-breaking.

    [PRESERVE — design §5 tradeoff] `source_tool` is REQUIRED on every chunk
    emitted by any tool. It is what preserves per-tool provenance once several
    tools' output is flattened into one list — the Verifier sees one merged
    list and would otherwise have no way to tell RAG-sourced from GRAPH-sourced
    evidence. This extends the existing per-source metadata convention
    (graph-confidence, conflict-basis, etc.) by one field; it is not a new
    mechanism.
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
            "Per-chunk confidence where the producing tool computes one (graph "
            "traversal attaches chain confidence; flat retrieval generally does not). "
            "[PRESERVE] Drives the Verifier's hedging check: low-confidence evidence "
            "requires confidence-appropriate hedging in the generated answer."
        ),
    )


# ── Outcome discriminator ────────────────────────────────────────────────

class ToolStatus(str, Enum):
    """
    Why a tool call ended the way it did. The discriminator every caller
    branches on. `OK` and `EMPTY` are distinct on purpose: `EMPTY` is a
    successful call that legitimately found nothing, and for several tools that
    is a materially different outcome from an error (see `fallback_to_rag`).
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
    authorization-violation audit record. See `CrossCaseToolInput`.
    """
    kind: Literal["permission_denied", "upstream_failure", "invalid_input", "timeout"]
    message: str = Field(description="Operator-facing. Not shown verbatim to the end user.")
```

---

## 1. Tool interfaces (primitives)

Tools live under `src/pipeline/harness/tools/`. **[PRESERVE — design §1] Tools are not
independently supervisor-routable.** The supervisor selects a *sub-agent*; a tool is only ever
invoked from inside a sub-agent's composition logic. This is load-bearing: it is what keeps the
enforcement points in §4 of the design doc attached to specific call chains rather than needing
re-derivation at the supervisor level.

### 1.0 Common tool shape

```python
class ToolInput(BaseModel):
    """Base input. Every tool receives the caller context unchanged."""
    query_text: str = Field(description="The rewritten, standalone query.")
    caller: CallerContext


class ToolResult(BaseModel):
    """
    Base result. Every tool returns this shape — no tool raises to signal a
    routine outcome (empty result, permission denial, upstream failure). Those
    are all values, so composing sub-agents can branch without exception
    handling scattered through composition logic.
    """
    status: ToolStatus
    chunks: list[EvidenceChunk] = Field(
        default_factory=list,
        description=(
            "Evidence, ordered. Non-empty iff status is OK. Every chunk carries "
            "metadata.source_tool identifying its producer."
        ),
    )
    error: Optional[ToolError] = Field(
        default=None, description="Present iff status is FAILED or DENIED."
    )
    fallback_to_rag: bool = Field(
        default=False,
        description=(
            "[PRESERVE — design §2.2, §2.6, §2.7] Whether the CALLER should now "
            "substitute the RAG tool for this tool's result. Only ever True for "
            "GRAPH, GRAPH_HYBRID, SQL, and WEB. PERMANENTLY False for XGRAPH, XAGG, "
            "and XNETWORK — see CrossCaseToolResult. The tool does not perform the "
            "fallback itself; it reports that one is warranted and the calling "
            "sub-agent acts on it. In today's orchestrator this is implicit in the "
            "branch structure, so the harness needs it made explicit."
        ),
    )


class Tool(Protocol):
    """Structural contract every primitive satisfies."""
    name: SourceTool

    async def __call__(self, tool_input: ToolInput) -> ToolResult: ...
```

### 1.1 Cross-case tool base — the role gate

```python
class CrossCaseToolInput(ToolInput):
    """
    Input to XGRAPH / XAGG / XNETWORK.

    [PRESERVE — design §2.3, §4.3] Ordering inside every cross-case tool is
    load-bearing and must be preserved verbatim:

        1. Check caller.role against CROSS_CASE_ROLES.
        2. On failure: write an authorization-violation audit record, return
           status=DENIED. Nothing else runs.
        3. ONLY on success: arm cross-case / RLS-bypass scope, then query.

    This ordering is the fix for a documented historical bug in which the
    RLS cross-case bypass flag was armed as soon as the router classified a
    query as cross-case — before the role check ran, and never reset on
    denial. Arming strictly after the check means an unauthorized caller never
    arms it at all: there is no window to close because none is opened.

    A harness restructuring that hoists scope resolution "up" to the supervisor
    or a shared middleware, so that scope is armed before dispatch, REINTRODUCES
    THIS BUG. Do not do it. Each of the three tools carries its own independent
    copy of this check today; the harness must keep them independent (design
    §4.3 — none of the enforcement points supersedes another).
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
            "Every case ID contributing to this result. [PRESERVE — design §4.6] "
            "Feeds the Verifier's allowed-cross-case-ID list; without it the "
            "leakage backstop cannot distinguish legitimate cross-case evidence "
            "from a genuine leak, and would reject valid cross-case answers."
        ),
    )
```

### 1.2 RAG

```python
class RagToolInput(ToolInput):
    """
    Retrieve passages relevant to `query_text` within the caller's scope.

    Deliberately says nothing about HOW. Embedding model, fusion strategy,
    reranking, and candidate counts are all free to change without touching
    this contract.

    [PRESERVE — design §2.1] Scoping is case-assignment-based, NOT role-based:
    RAG carries no role gate. Any caller who can see the case can search it.
    """
    top_k: Optional[int] = Field(
        default=None, description="Caller's requested result count. None = tool default."
    )
    include_global: bool = Field(
        default=True,
        description=(
            "Whether shared/global reference material is eligible alongside "
            "case-scoped evidence. Composition with project/global scoping follows "
            "existing precedence rules."
        ),
    )


class RagToolResult(ToolResult):
    """
    [PRESERVE — design §2.1] RAG is the FALLBACK TARGET for GRAPH,
    GRAPH_HYBRID, SQL and WEB — it has no onward fallback of its own, so
    `fallback_to_rag` is pinned False.

    Retry is internal (evaluator-feedback-driven query rewrite, bounded by a
    retry budget). Exhausting it abstains — it does NOT silently reach for web
    search. That removal was a deliberate scope decision, not a bug: WEB is
    reachable only via explicit router classification or an explicit caller
    toggle. Do not "restore" it.
    """
    fallback_to_rag: Literal[False] = False
    retries_used: int = Field(
        default=0, description="Internal retry loop iterations consumed. Observability only."
    )
    evaluator_verdict: Optional[Literal["relevant", "not_relevant", "unavailable"]] = Field(
        default=None,
        description=(
            "[RESOLVED-7] Relevance gate outcome. FOUR distinct states; collapsing "
            "any two loses information a caller needs:\n"
            "  'relevant'     — gate ran, evidence passed.\n"
            "  'not_relevant' — gate ran, evidence rejected. status=EMPTY, not "
            "FAILED: retrieval worked, the evidence just did not answer the "
            "question. A verdict payload missing its verdict key is treated as "
            "this (fail closed) — an unjudged result is not a pass.\n"
            "  'unavailable'  — THE GATE COULD NOT RUN (evaluator raised or timed "
            "out). Chunks are passed through UNVETTED so a flaky evaluator does not "
            "take retrieval down with it, but the evidence carries no relevance "
            "guarantee. A caller treating this as equivalent to 'relevant' has "
            "silently dropped the gate. Always accompanied by `degradation_caveats`.\n"
            "  None           — gate never reached (retrieval failed, or returned "
            "nothing to judge). Distinct from 'unavailable': nothing was skipped."
        ),
    )
    degradation_caveats: list[str] = Field(
        default_factory=list,
        description=(
            "[RESOLVED-7] User-facing qualifications about HOW this result was "
            "produced — currently, that the relevance gate could not run. The "
            "composing sub-agent MUST propagate these into `SubAgentResult.caveats`, "
            "the contract's existing channel for qualifications that must survive to "
            "the final response (§3) — the same treatment unconfirmed identity links "
            "receive. A sub-agent that drops them makes the degradation invisible to "
            "everyone above it."
        ),
    )
```

### 1.3 GRAPH / GRAPH_HYBRID

```python
class GraphToolInput(ToolInput):
    """
    Within-case traversal of the evidence graph from an entity seed.

    [PRESERVE — design §2.2] Within-case only. Cross-case traversal is XGRAPH
    (§1.4) — a separate tool with a role gate, NOT a flag on this one. Keeping
    them as distinct tools is what prevents a caller from reaching cross-case
    data by flipping a boolean.

    [PRESERVE — design §4.5] Any NEW within-case Cypher template introduced
    anywhere in the harness must go through the case-scoping chokepoint, which
    refuses to execute a template that does not reference the case parameter —
    never raw Cypher execution. This turns "a template silently lost its case
    filter" from a cross-case leak into a loud failure. It applies to
    within-case templates only; cross-case tools are deliberately unscoped.
    """
    target_entity: Optional[str] = Field(
        default=None,
        description=(
            "Entity seed. None is valid and meaningful: it means case-wide "
            "enumeration ('who are the witnesses in this case') rather than "
            "single-entity lookup."
        ),
    )
    max_hops: int = Field(default=2, ge=1, le=3, description="Traversal depth cap.")
    hybrid: bool = Field(
        default=False,
        description=(
            "False → GRAPH. True → GRAPH_HYBRID: graph discovery fused with RAG "
            "retrieval. [PRESERVE — design §2.2] GRAPH_HYBRID is composition of the "
            "GRAPH and RAG tools, not a third retrieval mechanism — implement it by "
            "composing them rather than re-implementing RAG's steps inline.\n\n"
            "[RESOLVED-1] This stays a FLAG on the tool; fusion STRATEGY is "
            "retrieval-internal machinery and is NOT hoisted into sub-agent-level "
            "composition. Sub-agents request hybrid behavior, they do not implement it "
            "— which keeps the fusion strategy free to change without touching any "
            "sub-agent, and avoids duplicating it across every sub-agent that wants it."
            "\n\n"
            "[RESOLVED-1a] 'Retrieval-internal' scopes the FUSION MECHANICS ONLY — "
            "candidate counts, merge strategy, rerank order. It does NOT make the fact "
            "that hybrid search ran invisible. When hybrid=True, emitted chunks carry "
            "metadata.source_tool='GRAPH_HYBRID', which MUST reach the investigator in "
            "citations and the live trace as a distinct, named source — see §1.3.1. A "
            "consumer that folds GRAPH_HYBRID into 'GRAPH' for display, or drops it as "
            "an implementation detail, is non-conforming."
        ),
    )


class GraphToolResult(ToolResult):
    """
    [PRESERVE — design §2.2] Sets `fallback_to_rag=True` when traversal fails
    or yields nothing usable — specifically: GRAPH finding neither evidence
    chunks nor conflict records, GRAPH_HYBRID producing no combined result, or
    the relevance evaluator rejecting the graph evidence. All three degrade to
    RAG today and must continue to.
    """
    hop_count: int = Field(default=0, description="Deepest hop actually reached.")
    chain_confidence: Optional[float] = Field(
        default=None,
        description=(
            "Weakest link across returned chains. [PRESERVE] Drives the Verifier's "
            "hedging requirement — low confidence obliges hedged phrasing in the answer."
        ),
    )
    seed_entities: list[dict[str, Any]] = Field(
        default_factory=list, description="Entities traversal started from. Observability."
    )
    conflicts_included: bool = Field(
        default=False,
        description=(
            "Whether detected within-case conflicts contributed chunks. Conflict "
            "records can be the ONLY result when no entity seed matched — that is "
            "status=OK, not EMPTY."
        ),
    )
```

#### 1.3.1 `source_tool` display contract — GRAPH_HYBRID is user-visible

**[RESOLVED-1a] Hybrid search is not an invisible implementation detail.** When an answer draws on
combined document *and* case-graph retrieval, the investigator must be able to see that — it is
material to judging the answer, not internal plumbing.

`ChunkMetadata.source_tool` is already required on every chunk (§0), so no schema change is needed;
what follows is the **display obligation** on consumers of that field.

```python
# Investigator-facing labels for metadata.source_tool. Consumers MUST NOT
# invent their own mapping, collapse distinct values, or fall back to the raw
# enum name in user-facing surfaces.
#
# [RESOLVED-1a] GRAPH_HYBRID is a DISTINCT label — never displayed as "GRAPH",
# never omitted. These labels are also what the disclosure templates substitute
# in, so they reach investigators verbatim in generated documents.
SOURCE_TOOL_DISPLAY_LABELS: dict[SourceTool, str] = {
    "RAG":          "document search",
    "GRAPH":        "case-graph search",
    "GRAPH_HYBRID": "combined document + case-graph search",
    "XGRAPH":       "cross-case entity search",
    "XAGG":         "cross-case aggregate",
    "XNETWORK":     "cross-case pattern synthesis",
    "SQL":          "penal-code reference lookup",
    "WEB":          "external web search",
}
```

**Where the label must appear**

1. **Citations.** `Citation.source_tool` carries the value to the supervisor; the rendered citation
   must show its label. An investigator reading a cited claim must be able to tell whether it came
   from a document, from the case graph, or from both.
2. **The live trace panel.** The `PipelineEvent` for a hybrid retrieval step must name it as such,
   not as a generic graph step — consistent with §2.2's requirement that trace granularity never
   regresses.

**What this does not change.** Fusion mechanics stay internal and free to change (RESOLVED-1):
*how* the merge happens is not surfaced, only *that* both sources contributed. `hybrid` remains a
flag on `GraphToolInput`; no sub-agent implements fusion.

**Note on framing.** An earlier reading of RESOLVED-1 could be taken to imply hybrid mode is
invisible by design because it is "retrieval-internal." That framing was too broad and is
corrected here: **the fusion strategy is internal; the fact that hybrid retrieval ran is not.**

### 1.4 XGRAPH — cross-case entity traversal

```python
class XGraphToolInput(CrossCaseToolInput):
    """
    Cross-case traversal for entity connections and recurrence.

    ── WHEN TO USE THIS vs. XNETWORK ────────────────────────────────────────
    Both are cross-case and both answer "what connects across cases," so the
    split is stated here in full — no need to read the router to implement a
    dispatching sub-agent.

    Route to XGRAPH when the query concerns a specific entity OR a TYPE of
    entity (person / vehicle / phone / organization) to trace. It covers BOTH:

      (a) "find connections for this named entity" — a literal name, CNIC,
          phone number, or plate appears in the query; and
      (b) "has any entity of type X recurred across cases" — no literal name,
          but a typed entity is still the answer shape.

    Both are structured, graph-typed, traversal-based queries. XGRAPH is NOT
    "the named-entity case only" — (b) is squarely its job too.

    Trigger vocabulary that routes here (English, Urdu, Roman-Urdu):
        "across [multiple/other] cases", "other case(s)", "another case",
        "elsewhere", "repeat offender", "کسی اور کیس", "دوسرے کیسز",
        "kisi aur case"

    ── PRECEDENCE (matters — these vocabularies genuinely overlap) ──────────
    The full deterministic order, highest priority first:

      1. Structured penal-code / cognizability lookups → SQL. Checked before
         everything, including the named-case exclusion below: "what section
         applies to the offense in CASE-009?" is a reference-data lookup, not a
         graph query.
      2. A NAMED CASE in the query → NOT cross-case at all. A named case
         anchors the query within-case (GRAPH), and suppresses XNETWORK, XAGG
         and XGRAPH entirely.
      3. XNETWORK — checked before XAGG/XGRAPH. Its trigger vocabulary is
         deliberately narrow open-ended-synthesis phrasing that does not
         overlap the other two, so it needs no tie-break against them.
      4. XAGG — beats XGRAPH on ties. "Recurring vehicles across cases" matches
         both XAGG's recurrence-aggregate vocabulary and XGRAPH's "across
         cases" pattern; that shape is an aggregate/count job, so XAGG wins.
      5. XGRAPH — last.

    ── THE AMBIGUOUS BOUNDARY, STATED EXPLICITLY ───────────────────────────
    "network ... across cases" phrasing is genuinely ambiguous between the two
    and is deliberately EXCLUDED from XNETWORK's triggers: "map ORG-002's
    network across all cases" is XGRAPH (a named entity is present), while
    "what's the overall picture on this network of associates?" is XNETWORK.
    No regex distinguishes "an entity is named" from "no entity is named"
    reliably, so the ambiguous middle is left to the classifier rather than
    force-resolved by pattern. A dispatching sub-agent should reuse this same
    distinction rather than inventing new logic — it is tuned against live
    misclassification failures, and re-deriving it will reproduce the bugs it
    was written to fix.
    """
    target_entity: Optional[str] = Field(
        default=None,
        description=(
            "Named entity to trace, if the query names one. None means case (b) "
            "above — typed-entity recurrence discovery — which is still XGRAPH."
        ),
    )
    max_hops: int = Field(default=2, ge=1, le=3)


class XGraphToolResult(CrossCaseToolResult):
    """
    [PRESERVE — design §2.3] Never falls back to RAG (inherited, pinned False).
    An empty result with no unconfirmed links is a definite "no connections
    found" answer — status=EMPTY, and the caller must present it as a real
    finding, not degrade to a case-scoped search.
    """
    unconfirmed_links: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Pending, unconfirmed identity links. [PRESERVE — design §3] These are "
            "CAVEATS and must be surfaced as such — never asserted as confirmed fact. "
            "Only confirmed identity links may be treated as the same real-world "
            "entity. Note a result can be status=EMPTY on chunks while still carrying "
            "unconfirmed links worth reporting."
        ),
    )
    hop_count: int = 0
    chain_confidence: Optional[float] = None
```

### 1.5 XAGG — cross-case aggregate

```python
AggregateKind = Literal["graph_recurrence", "relational_aggregate", "case_listing"]


class XAggToolInput(CrossCaseToolInput):
    """
    Cross-case aggregates: recurrence counts, and grouped counts over case
    metadata (station / status / category), plus plain case enumeration.

    [PRESERVE — design §2.4] TWO CANNED AGGREGATE FAMILIES, keyword-dispatched.
    This is deliberately NOT a general text-to-SQL / text-to-Cypher system. A
    later "make it smarter" pass must not turn this into free-form query
    generation — the bounded surface is the safety property.

    Trigger vocabulary: counting/ranking language over cases — "top recurring
    vehicles across all cases", "which stations have the most open theft
    cases", "list of all cases", "how many cases are there". Beats XGRAPH on
    overlapping "across cases" phrasing (see XGraphToolInput precedence).
    """
    target_entity: Optional[str] = None


class XAggToolResult(CrossCaseToolResult):
    """
    [PRESERVE — design §2.4] Never falls back to RAG (inherited, pinned False).

    [PRESERVE — design §2.4] VERIFIER-REJECTION HANDLING IS DIFFERENT HERE, and
    the distinction must not be unified with the generic abstention path: the
    aggregate is machine-computed and correct by construction, so a Verifier
    rejection means the natural-language PARAPHRASE failed — not that the
    evidence is unsound. The correct response is to present the raw
    deterministic aggregate text, NOT a generic abstention. `raw_summary_text`
    exists to make that fallback possible.
    """
    aggregate_kind: Optional[AggregateKind] = Field(
        default=None, description="Which canned family answered. Shapes presentation."
    )
    raw_summary_text: Optional[str] = Field(
        default=None,
        description=(
            "[PRESERVE] Deterministic rendering of the computed aggregate, independent "
            "of any LLM paraphrase. This is what gets served on Verifier rejection."
        ),
    )
```

### 1.6 XNETWORK — cross-case thematic synthesis

```python
class XNetworkToolInput(CrossCaseToolInput):
    """
    Open-ended cross-case pattern/theme synthesis over precomputed community
    summaries.

    ── WHEN TO USE THIS vs. XGRAPH ─────────────────────────────────────────
    Route here when the query is open-ended thematic synthesis with NO entity
    type in view — not "which vehicles recurred" (XGRAPH) but "what is going on
    across these cases thematically."

    XNETWORK is CATEGORICALLY DIFFERENT from XGRAPH — not "XGRAPH's no-entity
    case." It performs no graph traversal at all and involves no entity typing.
    It is similarity search over precomputed, free-text community summaries
    generated offline by a separate detection/summarization process, not at
    query time. Because those summaries are already grounded LLM-written prose,
    the result shape is closer to RAG's retrieve-and-cite than to XAGG's
    reproduce-this-data-faithfully.

    Trigger vocabulary (English, Urdu, Roman-Urdu):
        "overall picture", "overall pattern", "general pattern",
        "pattern ... emerges", "give me a sense", "مجموعی طور پر",
        "نمونہ ... سامنے", and Roman-Urdu "overall" ONLY when a synthesis or
        network word co-occurs within a short span ("connection", "dikhta",
        "pattern", "network") — bare "overall" is too generic to intercept on.

    Checked BEFORE XAGG and XGRAPH: this vocabulary essentially never co-occurs
    with a genuine aggregate or entity-recurrence query, so it needs no
    tie-break. Note the deliberate exclusion of "network ... across cases" —
    see XGraphToolInput's ambiguous-boundary note.

    STALENESS: because summaries are precomputed offline, results reflect the
    corpus as of the last summarization run, not live graph state. A caller
    needing live data wants XGRAPH.
    """
    top_k: int = Field(default=5, ge=1, description="Community summaries to retrieve.")


class XNetworkToolResult(CrossCaseToolResult):
    """
    [PRESERVE — design §2.5] Never falls back to RAG (inherited, pinned False).

    [PRESERVE — design §2.5] XNETWORK-SPECIFIC RETRY, and it must stay specific
    to XNETWORK. On a FIRST Verifier rejection, XNETWORK makes exactly ONE
    regeneration attempt forced to the cloud provider, then re-verifies. Only
    if that also fails — or raises, e.g. because air-gap mode blocks cloud
    egress — does it fall back to presenting the raw community-summary text.

    This exists because live testing found the local generation model fails
    this specific task shape reliably (3/3 runs) — the same evidence standard
    that justified the narrow cloud-escalation opt-in in the summarization
    stage. DO NOT GENERALIZE this retry to XAGG or XGRAPH without equivalent
    live-failure evidence, and do not silently drop it under air-gap mode:
    the air-gap path is the documented fall-through to raw text, not an error.
    """
    community_ids: list[str] = Field(default_factory=list)
    raw_summary_text: Optional[str] = Field(
        default=None,
        description="[PRESERVE] Raw community-summary text — the final fallback above.",
    )
```

### 1.7 SQL — structured reference lookup

```python
class SqlToolInput(ToolInput):
    """
    Parameterized lookup against the structured police reference dataset
    (penal-code sections, cognizability).

    [PRESERVE — design §2.6] This is the ONLY in-scope SQL path. The standalone
    MCP Postgres integration wired to the admin demo endpoint is a separate,
    unrelated component — not called from the chat pipeline and not part of
    this harness. Do not merge the two.

    [PRESERVE — design §2.6] Reference data, not case evidence: NO case
    scoping and NO role gate. Correspondingly, emitted chunks have no owning
    case, so they are inert to the Verifier's leakage check — correct, since
    reference data belongs to no case.

    Trigger vocabulary (highest precedence of all routes — see
    XGraphToolInput): "PPC section", "penal code section", "what/which section
    ... covers/applies", "cognizable offence/offense".
    """


class SqlToolResult(ToolResult):
    """
    [PRESERVE — design §2.6] Sets `fallback_to_rag=True` on an empty row set OR
    on any exception during parameter extraction or the query itself. Both
    degrade to RAG today.
    """
    row_count: int = 0
    extracted_params: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Filters derived from the query. Observability — lets an operator see "
            "whether a miss was bad extraction or genuinely absent data."
        ),
    )
```

### 1.8 WEB — guarded external search

```python
class WebToolInput(ToolInput):
    """
    Domain-allowlisted external search, with a grounded-search provider as a
    second tier.

    [PRESERVE — design §2.7] AIR-GAP MODE DISABLES THIS ENTIRELY, checked at
    BOTH provider call sites BEFORE either is reached. This is the first thing
    to re-verify if the tool is re-wrapped — a wrapper that checks once, at the
    top, and then calls through to a second provider on failure has silently
    reopened outbound egress on the fallback path.

    [PRESERVE — design §2.7] No case scope, no role gate. Web results are NEVER
    cited as case evidence — a standing guardrail, not a default. Emitted
    chunks therefore carry no owning case.
    """
    max_results: int = Field(default=5, ge=1)


class WebToolResult(ToolResult):
    """
    [PRESERVE — design §2.7] Sets `fallback_to_rag=True` only after BOTH tiers
    have failed. Preserve the two-tier fallback-within-a-fallback shape — do
    not collapse it to a single provider check.
    """
    provider_used: Optional[Literal["primary_search", "grounded_search_fallback"]] = None
    fallback_to_rag: bool = False
```

---

## 2. Sub-agent interfaces

**[PRESERVE — design §3] A sub-agent hands the supervisor a BOUNDED, SUMMARIZED payload — never
raw retrieved chunks, raw rows, or full conversation history.** The supervisor's context budget is
the reason the layer exists; a sub-agent that leaks its working set upward defeats the
architecture. Hence the split below: `evidence` stays *inside* the sub-agent (it goes to the
Verifier and the generator, which live at the sub-agent's own level), while `SubAgentResult` —
what the supervisor actually sees — carries only bounded, summarized fields.

### 2.0 Common sub-agent shape

```python
class SubAgentStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"       # Degraded but useful — see per-sub-agent rules below.
    EMPTY = "empty"           # Legitimately nothing to report. NOT an error.
    ABSTAINED = "abstained"   # Could not answer safely. No answer text is served.
    DENIED = "denied"
    """
    [RESOLVED-6] A role gate refused the request (cross-case sub-agents only).

    DENIED PROPAGATES AS ITS OWN STATUS — it must never be collapsed into
    ABSTAINED or EMPTY. "Blocked by permissions" and "searched and found
    nothing" are different facts about the system, and flattening them destroys
    the audit and monitoring signal that distinguishes them: a spike in denials
    is a security-relevant event, a spike in empties is a data-coverage
    problem. They must stay separable downstream.
    """


class SubAgentInput(BaseModel):
    """
    [PRESERVE — design §4.4] `caller` is threaded through UNCHANGED to every
    tool the sub-agent invokes. Do not reconstruct it, do not merge it with a
    preferences/profile object, do not default its role.
    """
    query_text: str = Field(description="Rewritten, standalone query.")
    caller: CallerContext
    output_format: Literal["chat", "file_pdf", "file_xlsx", "file_docx"] = "chat"
    conversation_context: Optional[str] = Field(
        default=None,
        description=(
            "Pre-bounded conversation context, if the supervisor supplies any. "
            "Bounding is the supervisor's job — a sub-agent never receives full history."
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
            "1-based index matching the `[Document N]` marker in answer_text. "
            "[PRESERVE — design §5] Positional correspondence with the evidence list "
            "shown to the generator; the Verifier depends on it."
        )
    )
    source_tool: SourceTool
    case_id: Optional[str] = None
    source_file: Optional[str] = None
    confidence: Optional[float] = None


class SubAgentResult(BaseModel):
    """
    THE BOUNDED HANDOFF PAYLOAD — what the supervisor receives.

    [PRESERVE — design §3] Carries NO EvidenceChunk list, no raw rows, no
    graph rows. Adding one would be the specific regression this layer exists
    to prevent. Evidence stays below this boundary.
    """
    status: SubAgentStatus
    answer_text: Optional[str] = Field(
        default=None,
        description=(
            "The synthesized, Verifier-passed answer. None when status is "
            "ABSTAINED or DENIED. [PRESERVE] An answer that failed verification is "
            "NEVER served — the safe abstention is served instead, never the "
            "ungrounded draft."
        ),
    )
    citations: list[Citation] = Field(default_factory=list)
    tools_used: list[SourceTool] = Field(
        default_factory=list,
        description=(
            "[RESOLVED-4] Tools that ACTUALLY CONTRIBUTED DATA to answer_text, measured "
            "AFTER all fallbacks resolved — NOT tools attempted. A tool that was invoked "
            "and then degraded past does not appear here; it appears in `degraded_from`.\n\n"
            "This rule is uniform across ALL seven sub-agents, not only the ones that "
            "compose several tools. It exists because GRAPH and SQL both degrade TO RAG: "
            "a three-tool call where GRAPH and SQL each fell back collapses to ONE "
            "effective RAG result, and reporting `[\"RAG\", \"GRAPH\", \"SQL\"]` would "
            "overstate the evidence base to the supervisor — which then reasons, and may "
            "generate, as though three independent sources agreed. Correct reporting for "
            "that case is `tools_used=[\"RAG\"]`, `degraded_from=[\"GRAPH\", \"SQL\"]`.\n\n"
            "Deduplicated: if two tools both degrade to RAG, RAG appears once."
        ),
    )
    degraded_from: list[SourceTool] = Field(
        default_factory=list,
        description=(
            "[RESOLVED-4] Tools that were ATTEMPTED but failed, returned empty, or fell "
            "back — the counterpart to `tools_used`. Together the two reconstruct what "
            "was tried versus what actually paid off, so the payload can never overstate "
            "itself. A tool never appears in both lists for the same call.\n\n"
            "Non-empty implies status=PARTIAL for every sub-agent that can degrade. This "
            "is what lets the supervisor tell a complete answer from a usable-but-"
            "incomplete one."
        ),
    )
    caveats: list[str] = Field(
        default_factory=list,
        description=(
            "[PRESERVE — design §3] User-facing qualifications that MUST survive to "
            "the final response — notably unconfirmed identity links, which are "
            "presented as caveats and never as confirmed fact. The supervisor may "
            "reorder or reformat these; it may not drop them."
        ),
    )
    generated_file: Optional["GeneratedFileRef"] = Field(
        default=None, description="Set only by Report Drafting."
    )
    timeline: list["TimelineEvent"] = Field(
        default_factory=list,
        description=(
            "Set only by Timeline Building. The ordered event list, each entry "
            "carrying its own three-state `conflict_state` ([RESOLVED-5]).\n\n"
            "This is a BOUNDED structure, not an evidence leak: `TimelineEvent` "
            "carries a description, a date, and a conflict flag — never the raw "
            "graph edge rows behind it, and never an `EvidenceChunk`. Empty list "
            "means 'no events', which for this sub-agent is a legitimate OK "
            "outcome rather than a failure."
        ),
    )
    cross_case_links: list["CrossCaseLink"] = Field(
        default_factory=list,
        description=(
            "Set only by Cross-Case Linkage. Ranked cross-case connections, each "
            "with its own confidence and `is_unconfirmed` caveat flag. Same "
            "bounded-structure rule as `timeline`: per-item findings, never the "
            "chunks they were derived from."
        ),
    )
    error: Optional[ToolError] = None


class GeneratedFileRef(BaseModel):
    """Reference to a produced file. No file bytes cross the boundary."""
    file_id: str
    file_name: str
    storage_path: str
    disclosure_rendered: bool = Field(
        default=False,
        description=(
            "[RESOLVED-3] True iff a partial-evidence disclosure was written INTO THE "
            "DOCUMENT BODY. Set only by the builder that actually rendered it — this "
            "field is an assertion about the file's contents, so it must never be set "
            "optimistically by a caller that merely intended a disclosure.\n\n"
            "[RESOLVED-3a] Set at step 5 of the §2.1.3 ordering — AFTER the Verifier has "
            "passed and the document is assembled. It is therefore never True on a "
            "report that failed verification: such a report is never built at all."
        ),
    )


# WORDING IS FINAL (approved 2026-08-08). Both strings below are the reviewed
# text the verification exemption depends on — changing either is a product
# decision, not a refactor, because they are delivered verbatim to
# investigators and the exemption is only safe while a human has signed off on
# exactly these words.
#
# Both follow one spec: name the unavailable source, then state what the
# content IS drawn from. No hedging ("may be incomplete"), no apology, and
# deliberately no claim that the output is untrustworthy — a graph-only summary
# is thinner, not wrong, and overstating that would be its own inaccuracy. An
# investigator can see both in one document (Report Drafting inherits Case
# Summarization's line), so they share vocabulary and tone on purpose.
#
# [RESOLVED-3] The MECHANISM: when Report Drafting builds from a degraded
# summary, this line is rendered into the document body itself, naming the
# unavailable source(s).
#
# [RESOLVED-3a] INJECTED POST-VERIFICATION, AND NEVER VERIFIED. See the
# ordering contract in §2.1.3 — the disclosure is a meta-statement ABOUT the
# generation process, not an evidentiary claim drawn from the case. It has
# nothing to cite, so passing it through grounding or citation verification
# could trip the no-citation check and cause abstention — withholding the whole
# report BECAUSE it was honest about being partial.
#
# Consequences for anyone editing this constant:
#   - It is a FIXED, REVIEWED TEMPLATE. Only `{unavailable_sources}` is
#     substituted, from `degraded_from`. Never LLM-generated, never
#     paraphrased, never regenerated per-report.
#   - Because it bypasses verification, its trustworthiness rests entirely on
#     this string being human-reviewed. That is the tradeoff that makes the
#     bypass safe — do not make it dynamic.
#   - Substitute through SOURCE_TOOL_DISPLAY_LABELS, not raw `degraded_from`
#     values: an investigator reads "case-graph search", never "GRAPH"
#     ([RESOLVED-1a] establishes those labels as the user-facing vocabulary).
#     Report Drafting applies the mapping before formatting.
PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE = (
    "The following evidence sources were unavailable when this report was "
    "generated: {unavailable_sources}. The findings below are drawn only from "
    "the sources that could be retrieved."
)


# [RESOLVED-2a] Case Summarization's own in-text disclosure, for the GRAPH-only
# case. Same mechanism as PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE above and
# governed by the same shared rules (§2.1.1); ordering for this one is in
# §2.1.2: fixed reviewed string, injected AFTER verification, never itself
# verified, never model-generated.
#
# Unlike the report template this takes no substitution — it names one specific
# gap. `status=PARTIAL` + `degraded_from` remain the machine-readable signal;
# this is the human-readable one, and it travels WITH THE TEXT rather than
# alongside it, so it survives being read, quoted, or pasted somewhere the
# payload metadata does not follow.
#
# The gloss on "case graph data" is deliberate: that phrase is internal
# vocabulary, and an investigator should not have to know it to read the
# sentence.
GRAPH_ONLY_SUMMARY_DISCLOSURE = (
    "Case documents were unavailable for this summary. The findings below are "
    "drawn from case graph data only — entities, relationships, and recorded "
    "events — and do not reflect the content of case documents."
)


class SubAgent(Protocol):
    """Structural contract every sub-agent satisfies."""
    name: str

    async def __call__(self, agent_input: SubAgentInput) -> SubAgentResult: ...
```

**Verifier boundary.** Every sub-agent that produces `answer_text` from retrieved evidence runs
the grounding gate itself, at its own level, before returning. The gate covers **evidentiary
content** — text making claims drawn from the case. **[RESOLVED-2a, RESOLVED-3a]** The elements
deliberately outside it are the two fixed disclosure strings — Case Summarization's GRAPH-only
line (§2.1.2) and Report Drafting's partial-evidence line (§2.1.3) — both injected *after*
verification passes and never themselves verified, under the shared rules in §2.1.1. The existing
signature is unchanged — **[PRESERVE — design §5]**:

```python
async def verify_grounding(
    answer: str,
    cited_chunks: list[dict],              # the flattened EvidenceChunk list, as dicts
    case_id: Optional[str],
    cross_case_ids: Optional[list[str]] = None,
    target_date: Optional[int] = None,
) -> dict:
    """
    Returns: {"grounded": bool, "off_topic": bool, "leaked_case_id": str | None,
              "unsupported_claims": list[str], "reason": str, "refusal_detected": bool}

    [PRESERVE] Fails CLOSED. A parse failure or a rejected claim serves a safe
    abstention — never a best-effort guess. An empty chunk list is
    not-grounded by definition.

    `cited_chunks` MUST be exactly the list the generator was shown, in the
    same order. `cross_case_ids` comes from CrossCaseToolResult.case_ids_touched.
    """
```

### 2.1 The seven sub-agents

| Sub-agent | Composes | Bounded payload | Partial-failure behavior |
|---|---|---|---|
| **Semantic Search** | RAG | Synthesized answer + top-N citations. Never the full ranked set. | Evaluator rejection after retry exhaustion → `ABSTAINED`. **[RESOLVED-7]** Evaluator *unavailable* (could not run) → answer still served, but `PARTIAL`, `degraded_from=["RAG"]`, and the tool's `degradation_caveats` propagated into `caveats` — never a silent `OK`. |
| **Case Summarization** | RAG (case-scoped) + GRAPH (case-scoped, capped hops) | One structured summary: status, key entities, key events, open questions. Never underlying chunks or graph rows. | **Symmetric degradation, either direction** — GRAPH empty/failed → RAG-only, `PARTIAL`, `degraded_from=["GRAPH"]`; **[RESOLVED-2]** RAG empty/failed → GRAPH-only, `PARTIAL`, `degraded_from=["RAG"]`, `tools_used=["GRAPH"]`. **Never `ABSTAINED` while real evidence exists on the surviving side** — abstaining would discard genuinely useful evidence. Both empty → `EMPTY`. **[RESOLVED-2a]** In the GRAPH-only direction the **summary text itself** carries `GRAPH_ONLY_SUMMARY_DISCLOSURE`, injected post-verification per §2.1.2 — `degraded_from` alone is not sufficient, since a reader could otherwise mistake a thinner, entity-shaped summary for a full one. |
| **Report Drafting** | **Case Summarization's output** + document builders | `GeneratedFileRef`. | **[PRESERVE]** If Case Summarization degraded, draft from what it returned — **never re-invoke tools directly to fill gaps**; that bypasses the summarization boundary. **[RESOLVED-3]** Degradation is **inherited**: upstream `PARTIAL` → this sub-agent is `PARTIAL`, it propagates the upstream `degraded_from`, **and** the builder renders `PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE` into the **document body**, setting `disclosure_rendered=True`. The disclosure must never live only in the payload/status field or internal logs — the investigator reading the document is the person who needs it. **[RESOLVED-3a]** It is injected **after** verification passes and is **never itself verified** (§2.1.3). **[RESOLVED-2a]** If the gap was already disclosed in Case Summarization's inherited text, that disclosure propagates forward and Report Drafting does **not** add a second one for the same gap — see §2.1.3's suppression rule. File-build failure → `ABSTAINED` with an explicit file-generation error. |
| **Investigative Analysis** | RAG + GRAPH + SQL | One synthesized answer, citations rolled up across all three. Never three separate result sets. | Each tool degrades independently per its own rule (GRAPH→RAG, SQL→RAG) **before** this sub-agent sees it — so it needs no duplicate fallback logic and always receives final tool output. **[RESOLVED-4]** ≥1 of the three returning usable data → `PARTIAL` (or `OK` if none degraded); all three failed/empty → `ABSTAINED`. `tools_used` lists only post-fallback contributors, deduplicated — a call where GRAPH and SQL both fell back to RAG reports `tools_used=["RAG"]`, `degraded_from=["GRAPH","SQL"]`, never three. **[RESOLVED-4a]** Emits one `PipelineEvent` **per source-tool outcome as it resolves** (§2.1.4) so the live trace shows per-source status in real time — the roll-up alone is not sufficient. |
| **Timeline Building** | GRAPH filtered to date-bearing edges + conflict detection | Ordered event list with per-event conflict state. Never raw edge rows. | No date-bearing edges → **explicit empty timeline, `status=EMPTY`, not an error** — a legitimate "nothing to show", distinct from tool failure. **[RESOLVED-5]** Conflict detection failing while edges resolve fine → return the timeline with every `conflict_state=UNKNOWN`, `status=PARTIAL`, `degraded_from` recording the conflict check. Never `NONE` — that would assert an all-clear the system never verified. |
| **Cross-Case Linkage** | XGRAPH + XNETWORK (dispatch per §1.4/§1.6) | Ranked cross-case connections with per-item confidence and hedge caveats. Never raw chunks. | Both never-fall-back independently. If one returns empty/abstains, **present whichever succeeded** (`PARTIAL`). **[RESOLVED-6]** Both denied → `DENIED`, propagated as its own status, never collapsed into `ABSTAINED`/`EMPTY` (they always deny together — identical role sets). **[PRESERVE]** The role gate is enforced twice, once per tool — **do not add a third at the sub-agent level**; it would be redundant and would drift out of sync with the tools' own checks. |
| **Large-Scale Aggregate** | XAGG | Computed aggregate + its natural-language summary. Never the full case row set. | **[PRESERVE]** Verifier-rejection → raw aggregate text is XAGG's own behavior; pass it through **unchanged** and do not add a second fallback layer. |

```python
class ConflictState(str, Enum):
    """
    [RESOLVED-5] Three-state, deliberately NOT a bool.

    `UNKNOWN` is the correct value whenever conflict detection did not
    successfully run for an event — it is distinct from `NONE`, which asserts
    the check ran and found nothing. A bool cannot represent that difference,
    so a failed check would render as "no conflicts found": the timeline would
    silently assert an all-clear it never verified. In an investigative
    context that is a correctness defect, not a stylistic one.

    `UNKNOWN` is the DEFAULT: an event is not-yet-checked until a check
    succeeds. Anything constructing a TimelineEvent must set NONE explicitly,
    and may only do so on a successful check that found nothing.
    """
    CONFLICT = "conflict"   # Checked; a contradictory record exists.
    NONE = "none"           # Checked; no conflict found.
    UNKNOWN = "unknown"     # Not successfully checked. Assert nothing.


class TimelineEvent(BaseModel):
    """Timeline Building's per-event payload element."""
    event_id: str
    description: str
    occurred_on: Optional[str] = Field(default=None, description="ISO-8601. None if undated.")
    conflict_state: ConflictState = Field(
        default=ConflictState.UNKNOWN,
        description=(
            "Whether a contradictory record exists for this event. [PRESERVE] "
            "Flagged, never silently resolved — surfacing the contradiction IS the "
            "value. [RESOLVED-5] Three-state; see ConflictState. Renderers MUST "
            "distinguish UNKNOWN from NONE in user-facing output — presenting UNKNOWN "
            "as an unqualified all-clear reintroduces exactly the defect the third "
            "state exists to prevent."
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
            "[PRESERVE] True for pending identity links. Such an item MUST be "
            "presented as a caveat and must contribute a matching entry to "
            "SubAgentResult.caveats — never asserted as confirmed fact."
        ),
    )
```

### 2.1.1 Disclosure contract — shared rules

**[RESOLVED-3a, RESOLVED-2a] Two sub-agents inject a user-facing disclosure into their own output
text: Case Summarization (§2.1.2) and Report Drafting (§2.1.3). Both obey the rules below.**

1. **Injected after verification, never verified.** A disclosure is a meta-statement *about the
   generation process*, not an evidentiary claim from the case. It has nothing to cite by
   construction. Verifying it would expose it to the no-citation check and could fail the output
   closed — withholding content *because* it disclosed being partial, inverting the disclosure's
   purpose.
2. **Fixed, reviewed template strings only.** Never model-generated, never paraphrased, never
   per-invocation dynamic. Substitution is limited to structured harness-held data (e.g.
   `degraded_from`), never model output.
3. **Verification-exemption is not transferable.** The exemption rests on properties 1 and 2
   together. It is not a precedent for exempting any other element.
4. **In the text, not only in the metadata.** `status=PARTIAL` and `degraded_from` remain the
   machine-readable signal for the supervisor. The disclosure is the human-readable one, and it
   must travel *with the content* so it survives being read, quoted, exported, or pasted somewhere
   the payload never follows.
5. **One disclosure per underlying gap.** A gap already disclosed upstream is not re-disclosed
   downstream — see §2.1.3's suppression rule.

### 2.1.2 Case Summarization — GRAPH-only disclosure

**[RESOLVED-2a]** When RAG fails or returns empty and the summary is built from graph data alone,
the **summary text itself** carries the disclosure — not merely `status=PARTIAL`.

```
1. RAG and GRAPH tools resolve. RAG fails/empty; GRAPH returns usable data.
2. Sub-agent composes the summary's EVIDENTIARY content from GRAPH output.
3. Verifier runs over the EVIDENTIARY CONTENT ONLY.
     ├── fails → ABSTAINED. No summary is served. No disclosure is produced.
     └── passes ↓
4. Prepend GRAPH_ONLY_SUMMARY_DISCLOSURE to answer_text.
     → Never re-verified, never regenerated, never paraphrased.
5. Return: status=PARTIAL, degraded_from=["RAG"], tools_used=["GRAPH"].
```

The symmetric case (**GRAPH** fails, RAG succeeds) yields an ordinary document-based summary —
the deliverable a user expects by default — so it needs no in-text disclosure; `status` and
`degraded_from` carry it. Only the GRAPH-only direction produces a materially different,
entity-shaped artifact that a reader could otherwise mistake for a full summary.

### 2.1.3 Report Drafting — disclosure ordering contract

**[RESOLVED-3a] The order below is fixed. Reordering it reintroduces the failure it prevents.**

```
1. Case Summarization returns its bounded payload (status, degraded_from,
   answer_text — which ALREADY CONTAINS its own disclosure if it was
   GRAPH-only, per §2.1.2).
2. Report Drafting composes the report's EVIDENTIARY content from that payload.
3. Verifier runs over the EVIDENTIARY CONTENT ONLY.
     ├── fails → ABSTAINED. No document is built. No disclosure is produced.
     └── passes ↓
4. Document builder assembles the file (PDF / XLSX / DOCX).
5. IF status == PARTIAL *and* the gap is not already disclosed in inherited
   text (see suppression rule below): inject
   PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE into the assembled document,
   substituting `{unavailable_sources}` from `degraded_from`.
   Set GeneratedFileRef.disclosure_rendered = True.
     → The injected line is NEVER re-verified, NEVER regenerated, NEVER
       paraphrased. Fixed reviewed template string, substitution only.
```

**[RESOLVED-2a] Suppression rule — no double disclosure for one gap.** Report Drafting consumes
Case Summarization's output, so a GRAPH-only summary arrives with its disclosure *already in the
inherited text*. That disclosure **propagates forward automatically** — it is part of the content
being drafted from. Report Drafting therefore must **not** add a second disclosure for the same
underlying gap; doing so would print two statements of the same fact in one document, which reads
as an error and dilutes both.

- **Gap already disclosed upstream** (RAG missing, inherited line present) → do not re-inject.
  `disclosure_rendered=True` regardless: the field asserts *the document discloses its
  partiality*, not *who authored the line*.
- **A different or additional gap** not covered by the inherited text → inject, naming only the
  not-yet-disclosed source(s).
- **`PARTIAL` from a non-summarization cause** (e.g. file-assembly degradation) → inject normally.

**Why the disclosure is exempt from verification.** It is a meta-statement *about the generation
process* — "this report was produced from partial evidence" — not an evidentiary claim drawn from
the case. It has nothing to cite, by construction. Passing it through grounding and citation
verification would expose it to the no-citation check, which could fail the report closed: the
document would be withheld **because** it was honest about being incomplete. That is a perverse
outcome — the safety disclosure triggering the safety abstention — and step 5's placement after
step 3 is what prevents it.

**What keeps the exemption safe.** The disclosure bypasses verification *only* because it is a
fixed, human-reviewed string with a single substituted field. That is the whole basis of its
trustworthiness — there is no grounding check standing behind it. Consequently:

- It must never become LLM-generated, paraphrased, or per-report dynamic. Making it dynamic
  would create unverified generated text in a delivered document, which is exactly what the
  Verifier exists to prevent.
- `{unavailable_sources}` is filled from `degraded_from` — structured data the harness already
  holds, not model output.
- A verification-exempt element **must not** be added to any other route on this precedent.
  The exemption is justified by this element's specific properties (fixed, reviewed, non-
  evidentiary, uncitable-by-construction), not by convenience.

**Scope.** Applies to `output_format` values that produce a document (`file_pdf`, `file_xlsx`,
`file_docx`). For `chat`, Report Drafting is not the acting sub-agent and no disclosure is
injected — degradation is carried by `status` and `degraded_from`, which the supervisor already
sees.

### 2.1.4 Investigative Analysis — live per-source trace events

**[RESOLVED-4a] `tools_used` / `degraded_from` are an after-the-fact roll-up. They are not
sufficient on their own.** Investigative Analysis composes three tools and can run long enough
that a user watching the trace panel would otherwise see nothing until every source has resolved
— and then see only the bundled outcome, with no indication of *when* or *why* a source dropped
out.

**Requirement:** Investigative Analysis emits **one `PipelineEvent` per source-tool outcome, as
that outcome resolves** — not one event for the sub-agent as a whole.

```
RAG succeeds            → PipelineEvent(step="analysis:rag",   status="done",  detail=…)
GRAPH falls back to RAG → PipelineEvent(step="analysis:graph", status="retry", detail=…)
SQL fails outright      → PipelineEvent(step="analysis:sql",   status="error", detail=…)
```

- `status` uses the **existing five-value SSE vocabulary** (`active`/`done`/`error`/`retry`/
  `skipped`) — no new values. A tool that fell back to RAG is `retry`; one that failed with no
  usable result is `error`; one not attempted is `skipped`.
- Events are emitted **as each tool resolves**, not batched at the end. Batching would satisfy the
  letter of this rule and defeat its purpose.
- The final roll-up (`tools_used`, `degraded_from`) is still returned per RESOLVED-4. The two are
  complementary: events narrate *as it happens*, the payload states *what held at the end*. They
  must agree — a tool reported `done` in an event must appear in `tools_used`.

**This reuses existing infrastructure. It introduces none.** These are the same `PipelineEvent`
objects already defined in §2.2, emitted through the same SSE channel the pipeline already yields
to the chat UI, and mirrored to `log_step` by the same existing wrapper. Specifically **not**
implied: webhooks, callbacks, a subscription mechanism, a message bus, or any new transport. If an
implementation adds a delivery mechanism to satisfy this requirement, it has misread it.

**Generalization.** Any sub-agent composing more than one tool should follow this pattern
(Case Summarization and Cross-Case Linkage both qualify). It is stated here because Investigative
Analysis composes the most tools and has the most collapse-to-one-source risk, per RESOLVED-4.

#### 2.1.4.1 Two mechanisms satisfy this — choose deliberately

There are **two** ways a per-source outcome reaches the live trace, and they say different
things. A multi-tool sub-agent must pick one on purpose rather than inheriting whichever happens
to be there.

**(a) Tool-emitted events** — `tool:rag`, `tool:graph`, … Every tool already receives the
`EventRecorder` and emits `active` → `done`/`retry`/`error` as it resolves. These describe **what
the tool did**.

*Sufficient when a sub-agent's legs are independent and cannot degrade into one another.*
**Case Summarization** is this case: RAG and GRAPH are separate sources, neither falls back into
the other, so `tool:rag` and `tool:graph` map one-to-one onto its two legs and the trace is
unambiguous without anything further. It emits no per-source events of its own, deliberately —
adding them would produce two events per source for one transition, which §2.2 forbids just as
much as collapsing them into none.

**(b) Sub-agent-interpreted events** — `analysis:rag`, `analysis:graph`, … Emitted by the
sub-agent after reading each tool result. These describe **what the sub-agent concluded**.

*Necessary when legs CAN collapse into one another*, because the tool-level event is then
ambiguous. **Investigative Analysis** is this case: GRAPH and SQL both degrade *to RAG*, so a
`tool:graph retry` says graph fell back but not whether the sub-agent ended up with one effective
source or three. Only the sub-agent knows that, and RESOLVED-4 requires the distinction be
visible — so it states its own interpretation (`contributed` vs `fell back`) alongside the tool's.

**Deciding for a new sub-agent:** ask whether any two of its legs can end up as the same
effective source. If no, (a) alone is correct and (b) is duplication. If yes, (b) is required and
(a) alone would understate the collapse.

**Open for Cross-Case Linkage.** XGRAPH and XNETWORK are structurally independent (§3.1 — no
graph traversal in XNETWORK at all, and neither falls back to anything: both are pinned
never-fall-back by `CrossCaseToolResult`), which points at (a). **Confirm rather than assume**
before implementing its trace behaviour — the never-fall-back property makes collapse impossible
by construction, but that reasoning should be checked against the built sub-agent, not inherited
from this note.

### 2.2 Logging contract

**[PRESERVE — design §6] Both of the following fire at every meaningful state transition,
regardless of how control flow is restructured.**

```python
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


async def log_step(
    run_id: str, step_name: str, step_order: int,
    status: Literal["success", "skipped", "retry", "failed"],
    duration_ms: int, output_summary: str,
) -> None:
    """
    [PRESERVE — design §6] Durable per-step record. The admin Run History page
    has NO OTHER SOURCE for step traces — dropping a call here silently blanks
    that page for the affected step.

    Note the vocabulary mismatch: SSE has five statuses, this has four
    (constrained by a database CHECK). The existing remap must be preserved —
    `active` has no durable equivalent and `done` maps to `success`.
    """
```

**Dropped: the SQLite side-log.** No live readers — the write-only local debug log has zero
callers outside one isolated test. **Accepted tradeoff:** this was the only no-Postgres-required
way to inspect per-step timings locally. The replacement is the Postgres step table via the admin
dashboard, which requires a running database where the SQLite file did not.

---

## 3. Resolved contract decisions

Six questions the design doc left unspecified, now settled. Each is applied inline above and
marked **[RESOLVED-n]** at the point it constrains a type. Recorded here with rationale so the
reasoning is not lost and the decisions are not silently relitigated during implementation.

**[RESOLVED-1] — GRAPH_HYBRID stays a flag on the GRAPH tool.**
`GraphToolInput.hybrid: bool` is retained; fusion remains retrieval-internal machinery and is not
hoisted into sub-agent-level composition. Sub-agents *request* hybrid behavior, they do not
implement it. Keeps the fusion strategy free to change without touching any sub-agent, and avoids
duplicating it across every sub-agent that wants it. No schema change.
*Design-doc note:* §7's open item — whether the current inline duplication gets cleaned up — is
untouched by this and remains a follow-up. This decision fixes the **contract**; it does not
mandate when the internals are de-duplicated.

**[RESOLVED-1a] — GRAPH_HYBRID is user-visible; only the fusion strategy is internal.**
*(Supervisor review, revises RESOLVED-1.)* `hybrid` stays a flag and sub-agents still do not
implement fusion — but "retrieval-internal" scopes the fusion *mechanics*, not the *fact that
hybrid retrieval ran*. Chunks carry `source_tool="GRAPH_HYBRID"`, and consumers must surface it as
a distinct named source ("combined document + case-graph search") in citations and the live trace.
Folding it into `"GRAPH"` for display, or dropping it as an implementation detail, is
non-conforming. See §1.3.1 for the display contract and label map.
Rationale: whether an answer rests on documents, on the case graph, or on both is material to an
investigator judging it — that is a transparency property, not plumbing. No schema change:
`source_tool` was already required on every chunk.

**[RESOLVED-2] — Case Summarization degrades symmetrically.**
RAG empty/failed → GRAPH-only, `PARTIAL`, `degraded_from=["RAG"]`, mirroring the existing
GRAPH-fails case. Never `ABSTAINED` while real evidence exists on the surviving side — abstaining
would discard genuinely useful evidence. Both sides empty → `EMPTY`.
A GRAPH-only summary *is* a thinner, entity-shaped artifact than a retrieval-backed one; that
difference is communicated through `degraded_from` rather than by withholding the summary.

**[RESOLVED-2a] — The GRAPH-only summary discloses in its own text, and that disclosure
propagates forward.**
*(Supervisor review, extends RESOLVED-2.)* When RAG fails/empty and the summary is GRAPH-only,
`answer_text` itself carries `GRAPH_ONLY_SUMMARY_DISCLOSURE` — `status=PARTIAL` and
`degraded_from` alone are not sufficient. Same mechanism as Report Drafting's disclosure and the
same shared rules (§2.1.1): fixed reviewed string, injected after verification, never re-verified,
never model-generated. Ordering in §2.1.2.
Rationale: a GRAPH-only summary is a materially thinner, entity-shaped artifact that a reader
could mistake for a complete one; metadata does not travel with text that gets quoted, exported,
or pasted elsewhere. The symmetric case (GRAPH fails, RAG succeeds) needs no in-text disclosure —
it produces the document-based summary a user expects by default.
**Propagation, and the double-disclosure it would otherwise cause:** because Report Drafting
consumes Case Summarization's output, the disclosure is part of the inherited text and carries
forward automatically. RESOLVED-3a would *also* fire on `status=PARTIAL` for the same underlying
gap, printing two statements of one fact in a single document. Resolved by the **suppression rule**
in §2.1.3: a gap already disclosed upstream is not re-disclosed, while `disclosure_rendered` stays
`True` either way — it asserts *the document discloses its partiality*, not *who wrote the line*.

**[RESOLVED-3] — Report Drafting inherits degradation, and discloses it in the document.**
Upstream `PARTIAL` → this sub-agent is `PARTIAL`, propagates the upstream `degraded_from`, and the
builder renders `PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE` into the **document body**, naming the
unavailable source(s), then sets `disclosure_rendered=True`.
The disclosure must not live only in the payload, status field, or run logs: a generated report
outlives the response that carried it and is read by people who never see the pipeline metadata.
An investigator holding a PDF that silently omits graph-derived findings has no way to know it is
partial. **Wording is final (approved 2026-08-08)** — changing it is a product decision, not a
refactor, since the string is delivered verbatim and its exemption from the grounding gate rests
on a human having reviewed exactly those words. `{unavailable_sources}` substitutes through
`SOURCE_TOOL_DISPLAY_LABELS`, so a report names "case-graph search", never "GRAPH".

**[RESOLVED-3a] — The disclosure is injected post-verification and is never itself verified.**
Full ordering in §2.1.3: sub-agent output → Verifier over **evidentiary content only** → on pass,
builder assembles → disclosure injected if `status=PARTIAL`, as a fixed reviewed template string.
The disclosure is a meta-statement about the generation process, not an evidentiary claim from the
case; it has nothing to cite by construction. Verifying it would expose it to the no-citation
check and could fail the report closed — withholding the document *because* it disclosed being
partial, which inverts the disclosure's entire purpose.
The exemption is safe **only** because the string is fixed and human-reviewed with a single
structured substitution (`{unavailable_sources}`, from `degraded_from`). It must never become
model-generated or per-report dynamic, and this is **not** a precedent for exempting any other
element from verification — the justification is this element's specific properties, not
convenience.
*Resolves the Verifier-interaction gap raised in review of RESOLVED-3; no change to
`verify_grounding()`'s signature or to design §5.*

**[RESOLVED-4] — Investigative Analysis thresholds, and a uniform `tools_used` rule.**
≥1 of RAG/GRAPH/SQL returning usable data → `PARTIAL` (`OK` if none degraded); all three
failed/empty → `ABSTAINED`.
The accompanying accuracy fix is **not local to this sub-agent**: `tools_used` now means "tools
that actually contributed data, measured after all fallbacks resolved," and `degraded_from` means
"attempted but failed, empty, or fell back." Because GRAPH and SQL both degrade *to RAG*, a
three-tool call can collapse to one effective RAG result; reporting three tools would tell the
supervisor that three independent sources agreed when only one did — and the supervisor may
generate on that basis. Since `tools_used` is defined once on `SubAgentResult` and read uniformly
by the supervisor, the rule applies to **all seven sub-agents**: a field that meant different
things depending on its producer would be unreadable at the consuming end.

**[RESOLVED-4a] — Investigative Analysis emits live per-source trace events.**
*(Supervisor review, extends RESOLVED-4.)* One `PipelineEvent` per source-tool outcome, emitted
**as that outcome resolves** — `done` for a tool that contributed, `retry` for one that fell back,
`error` for one that failed, `skipped` for one not attempted. The existing five-value SSE status
vocabulary; no new values. The roll-up (`tools_used`/`degraded_from`) is still returned and the two
must agree.
Rationale: the roll-up is after-the-fact, so a user watching a multi-tool analysis sees nothing
until everything resolves, then only the bundled result — with no signal about when or why a
source dropped out. Contract in §2.1.4.
**Explicitly reuses existing infrastructure and requires none.** Same `PipelineEvent` type (§2.2),
same SSE channel the pipeline already yields, same `log_step` mirroring. Webhooks, callbacks,
subscriptions, and message buses are **not** implied — an implementation that adds a delivery
mechanism here has misread the requirement.

**[RESOLVED-5] — `conflict_state` is three-state, not a bool.** *(Settled; not open to
relitigation.)*
`TimelineEvent.conflict_flag: bool` is replaced by `conflict_state: ConflictState`
(`CONFLICT` / `NONE` / `UNKNOWN`), defaulting to `UNKNOWN`. Conflict detection failing while date
edges resolve fine → return the timeline with every event `UNKNOWN`, `status=PARTIAL`.
`NONE` asserts "checked, nothing found" and may only be set on a successful check. A bool cannot
distinguish "checked, clear" from "never checked," so a failed check would render as an all-clear
the system never verified — a correctness defect in an investigative context, not a stylistic
choice. Renderers must carry the distinction through to user-facing output; collapsing `UNKNOWN`
to "no conflicts" in presentation reintroduces the same defect one layer later.

**[RESOLVED-7] — The relevance gate distinguishes "rejected" from "could not run."**
*(Surfaced while wiring the real RAG tool.)* `evaluator_verdict` gains a third value,
`'unavailable'`, and `RagToolResult` gains `degradation_caveats`.

An evaluator that **cannot run** is not evidence of relevance. Chunks are still passed through —
a flaky local model endpoint should not take retrieval down with it — but the degradation is
explicit rather than silent: a distinct verdict value, a user-facing caveat, and `PARTIAL` status
at the sub-agent. Reporting `'relevant'` in that case would drop the gate without trace.

**The Verifier does not backstop this.** It checks *grounding* — whether claims trace to cited
chunks — not *relevance*, so it will pass a well-grounded answer built entirely from off-topic
evidence. Relevance screening has no second line of defence, which is why its absence has to be
visible.

Separately, a malformed verdict (a payload with no verdict key) now **fails closed**, matching
the legacy GRAPH route. It previously defaulted to a pass, which silently admitted unjudged
evidence — a defect, not a design choice.

*See AGENT_HARNESS_DESIGN.md §7 for why this is a deliberate deviation from legacy RAG's
crash-on-evaluator-error behaviour.*

**[RESOLVED-6] — `DENIED` propagates as its own sub-agent status.**
Cross-Case Linkage returns `DENIED` when both tools deny (always together — identical role sets).
It never collapses into `ABSTAINED` or `EMPTY`. "Blocked by permissions" and "searched and found
nothing" are different facts: a spike in denials is a security-relevant signal, a spike in empties
is a data-coverage problem, and flattening them destroys the audit and monitoring ability to tell
them apart. The per-tool authorization-violation audit records (design §4.3) remain the durable
record; this status keeps the same distinction visible in the live payload.
*Applies to any future cross-case sub-agent, not only this one.*
