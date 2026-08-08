"""
Tests for src/pipeline/harness/types.py (Phase 0, foundation layer).

Focused on the ONE piece of behavior this module adds beyond a literal
transcription of SUBAGENT_INTERFACES.md: ChunkMetadata's confidence /
confidence_status consistency validator (the confidence-field-split
amendment, AGENT_HARNESS_IMPLEMENTATION_PLAN.md §1). Also smoke-tests
that every shape constructs the way its own docstring says it should,
since a Phase 0 regression here would silently break every tool wrapper
built on top of it.
"""
import pytest
from pydantic import ValidationError

from src.pipeline.harness.types import (
    CROSS_CASE_ROLES,
    CallerContext,
    ChunkMetadata,
    ConflictState,
    CrossCaseLink,
    CrossCaseToolInput,
    CrossCaseToolResult,
    EvidenceChunk,
    ExecutionContext,
    Role,
    SOURCE_TOOL_DISPLAY_LABELS,
    SubAgentResult,
    SubAgentStatus,
    TimelineEvent,
    ToolError,
    ToolInput,
    ToolResult,
    ToolStatus,
)


# ── Role / CallerContext ────────────────────────────────────────────────

def test_cross_case_roles_excludes_investigator():
    assert Role.INVESTIGATOR not in CROSS_CASE_ROLES
    assert CROSS_CASE_ROLES == {Role.SUPERVISOR, Role.STATION_ADMIN, Role.PLATFORM_ADMIN}


def test_caller_context_role_is_required_and_typed():
    # role must be a real Role, not an arbitrary string that happens to
    # look like one — this is the type-level backstop for the
    # user_profile.get("role", "investigator") historical bug (design §4.4).
    ctx = CallerContext(user_id="u1", role="supervisor", active_case_id="CASE-001")
    assert ctx.role == Role.SUPERVISOR
    with pytest.raises(ValidationError):
        CallerContext(user_id="u1", role="not-a-real-role")


# ── ChunkMetadata: confidence / confidence_status split ─────────────────

def test_confidence_metadata_defaults_to_not_computed():
    meta = ChunkMetadata(source_tool="RAG")
    assert meta.confidence is None
    assert meta.confidence_status == "not_computed"


def test_confidence_computed_requires_a_value():
    with pytest.raises(ValidationError):
        ChunkMetadata(source_tool="GRAPH", confidence_status="computed", confidence=None)


def test_confidence_value_requires_computed_status():
    # A real confidence value with a non-'computed' status is exactly the
    # ambiguity this amendment exists to prevent — must be rejected.
    with pytest.raises(ValidationError):
        ChunkMetadata(source_tool="GRAPH", confidence_status="not_computed", confidence=0.8)
    with pytest.raises(ValidationError):
        ChunkMetadata(source_tool="GRAPH", confidence_status="check_failed", confidence=0.8)


def test_confidence_computed_with_value_is_valid():
    meta = ChunkMetadata(source_tool="GRAPH", confidence_status="computed", confidence=0.73)
    assert meta.confidence == 0.73
    assert meta.confidence_status == "computed"


def test_confidence_check_failed_is_distinguishable_from_not_computed():
    # The exact case design §7 flags: a low-confidence chain that failed to
    # score must NOT read identically to "this tool never computes a score".
    failed = ChunkMetadata(source_tool="GRAPH", confidence_status="check_failed")
    absent = ChunkMetadata(source_tool="RAG", confidence_status="not_computed")
    assert failed.confidence is None and absent.confidence is None
    assert failed.confidence_status != absent.confidence_status


def test_chunk_metadata_source_tool_is_required():
    with pytest.raises(ValidationError):
        ChunkMetadata()


def test_chunk_metadata_allows_extra_keys():
    # [PRESERVE — design §5 tradeoff] metadata is open; adding a key (e.g.
    # graph_confidence, conflict_basis) must never be a breaking change.
    meta = ChunkMetadata(source_tool="GRAPH", graph_confidence=0.5, conflict_basis="x")
    assert meta.model_dump()["graph_confidence"] == 0.5


# ── EvidenceChunk ────────────────────────────────────────────────────────

def test_evidence_chunk_round_trip():
    chunk = EvidenceChunk(
        id="c1", text="some evidence text",
        metadata=ChunkMetadata(source_tool="RAG", case_id="CASE-001"),
        score=0.42,
    )
    assert chunk.metadata.case_id == "CASE-001"
    assert chunk.score == 0.42


# ── ToolResult base shape ────────────────────────────────────────────────

def test_tool_result_default_is_empty_ok_no_fallback():
    result = ToolResult(status=ToolStatus.EMPTY)
    assert result.chunks == []
    assert result.error is None
    assert result.fallback_to_rag is False


def test_tool_error_requires_kind_and_message():
    with pytest.raises(ValidationError):
        ToolError(message="oops")  # kind is required
    err = ToolError(kind="permission_denied", message="denied")
    assert err.kind == "permission_denied"


# ── Cross-case base: fallback_to_rag permanently pinned False ───────────

def test_cross_case_tool_result_fallback_pinned_false():
    result = CrossCaseToolResult(status=ToolStatus.OK, case_ids_touched=["CASE-001", "CASE-002"])
    assert result.fallback_to_rag is False
    with pytest.raises(ValidationError):
        # [PRESERVE — design §2.3/§2.4/§2.5] literal-False Literal type;
        # a caller trying to flip this must fail loudly at construction.
        CrossCaseToolResult(status=ToolStatus.OK, fallback_to_rag=True)


def test_cross_case_tool_input_is_a_tool_input_subclass():
    ctx = CallerContext(role="supervisor")
    inp = CrossCaseToolInput(query_text="q", execution=ExecutionContext(caller=ctx))
    assert isinstance(inp, ToolInput)


# ── ExecutionContext — contract retrofit (plan §10.1) ────────────────────

def test_execution_context_wraps_caller_and_defaults_reserved_fields_inert():
    ctx = CallerContext(user_id="u1", role="supervisor", active_case_id="CASE-001")
    execution = ExecutionContext(caller=ctx)
    assert execution.caller is ctx
    assert execution.project_id is None
    assert execution.workspace_id is None
    assert execution.organization_id is None
    assert execution.feature_flags == {}


def test_execution_context_project_id_is_optional_and_settable():
    ctx = CallerContext(role="investigator")
    execution = ExecutionContext(caller=ctx, project_id="PROJ-1")
    assert execution.project_id == "PROJ-1"


# ── source_tool display labels ───────────────────────────────────────────

def test_source_tool_display_labels_cover_every_source_tool():
    # [RESOLVED-1a] GRAPH_HYBRID must be present and distinct — never
    # collapsed into "GRAPH" or omitted.
    expected = {"RAG", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "XNETWORK", "SQL", "WEB"}
    assert set(SOURCE_TOOL_DISPLAY_LABELS.keys()) == expected
    assert SOURCE_TOOL_DISPLAY_LABELS["GRAPH_HYBRID"] != SOURCE_TOOL_DISPLAY_LABELS["GRAPH"]


# ── SubAgentResult.events / .links — contract amendment (plan §11) ──────

def test_sub_agent_result_events_and_links_default_empty():
    # Additive, default-empty: zero effect on any sub-agent that doesn't
    # set either field (Semantic Search, Large-Scale Aggregate, Case
    # Summarization use neither).
    result = SubAgentResult(status=SubAgentStatus.OK)
    assert result.events == []
    assert result.links == []


def test_timeline_event_conflict_state_defaults_to_unknown_not_none():
    # [RESOLVED-5] UNKNOWN is the default -- "not yet checked," never
    # silently "no conflict." NONE may only be set on an explicit,
    # successful check that found nothing.
    event = TimelineEvent(event_id="e1", description="suspect seen leaving")
    assert event.conflict_state == ConflictState.UNKNOWN
    assert event.occurred_on is None
    assert event.locked is False
    assert event.conflict_basis is None


def test_timeline_event_conflict_state_all_three_values_constructible():
    checked_clear = TimelineEvent(
        event_id="e1", description="d", conflict_state=ConflictState.NONE
    )
    assert checked_clear.conflict_state == ConflictState.NONE

    conflicting = TimelineEvent(
        event_id="e2",
        description="d",
        conflict_state=ConflictState.CONFLICT,
        conflict_basis="contradicts witness statement in Document 3",
    )
    assert conflicting.conflict_state == ConflictState.CONFLICT
    assert conflicting.conflict_basis is not None


def test_cross_case_link_source_tool_restricted_to_xgraph_or_xnetwork():
    link = CrossCaseLink(
        description="Vehicle ABC-123 recurs across 3 cases",
        case_ids=["CASE-001", "CASE-002", "CASE-003"],
        source_tool="XGRAPH",
    )
    assert link.is_unconfirmed is False
    assert link.confidence is None
    with pytest.raises(ValidationError):
        CrossCaseLink(description="d", case_ids=["CASE-001"], source_tool="RAG")


def test_sub_agent_result_carries_events_and_links_together():
    event = TimelineEvent(event_id="e1", description="d")
    link = CrossCaseLink(description="d", case_ids=["CASE-001"], source_tool="XNETWORK")
    result = SubAgentResult(status=SubAgentStatus.OK, events=[event], links=[link])
    assert result.events[0] is event
    assert result.links[0] is link
    # Round-trips through serialization (forward-ref resolution actually
    # worked -- model_rebuild() ran after both referenced classes existed).
    dumped = result.model_dump()
    assert dumped["events"][0]["conflict_state"] == ConflictState.UNKNOWN
    assert dumped["links"][0]["source_tool"] == "XNETWORK"
