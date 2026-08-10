"""
Regression tests for the three retrieval gaps the first real-data run exposed.

Each of these passed its unit tests before the fix, because the stub tools
returned rich synthetic evidence that masked what the real ones do. They are
written against the SHAPE of the real failure, not the stub's convenience.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness.contracts import (
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    SubAgentInput,
    SubAgentStatus,
    ToolStatus,
)


# ══════════════════════════════════════════════════════════════════════════
# 1. target_entity threading (Cross-Case Linkage found nothing without it)
# ══════════════════════════════════════════════════════════════════════════

def test_sub_agent_input_carries_target_entity():
    """
    Dropping it does not fail closed — it fails SILENT, as an EMPTY cross-case
    result an investigator reads as "no connections exist".
    """
    agent_input = SubAgentInput(
        query_text="what other cases",
        caller=CallerContext(role="supervisor"),
        target_entity="Hina Malik",
    )
    assert agent_input.target_entity == "Hina Malik"
    # Optional: most sub-agents never set it.
    assert SubAgentInput(
        query_text="q", caller=CallerContext(role="investigator")
    ).target_entity is None


@pytest.mark.asyncio
async def test_supervisor_threads_target_entity_from_route_result():
    """The router extracts it; the supervisor is the only place holding both."""
    from src.pipeline.harness import supervisor

    seen = {}

    async def fake_node(agent_input, events=None, **kwargs):
        seen["target_entity"] = agent_input.target_entity
        from src.pipeline.harness.contracts import SubAgentResult

        return SubAgentResult(status=SubAgentStatus.OK, answer_text="ok")

    supervisor._NODES["probe_agent"] = fake_node
    supervisor._route = lambda agent_input, route_result: "probe_agent"

    await supervisor.invoke(
        SubAgentInput(
            query_text="what other cases is Hina Malik in",
            caller=CallerContext(role="supervisor"),
        ),
        {"route": "XGRAPH", "target_entity": "Hina Malik"},
    )

    assert seen["target_entity"] == "Hina Malik"


@pytest.mark.asyncio
async def test_explicit_target_entity_beats_the_routers_guess():
    """A caller that named an entity meant it; routing must not override."""
    from src.pipeline.harness import supervisor
    from src.pipeline.harness.contracts import SubAgentResult

    seen = {}

    async def fake_node(agent_input, events=None, **kwargs):
        seen["target_entity"] = agent_input.target_entity
        return SubAgentResult(status=SubAgentStatus.OK, answer_text="ok")

    supervisor._NODES["probe_agent"] = fake_node
    supervisor._route = lambda agent_input, route_result: "probe_agent"

    await supervisor.invoke(
        SubAgentInput(
            query_text="q",
            caller=CallerContext(role="supervisor"),
            target_entity="Explicit Choice",
        ),
        {"route": "XGRAPH", "target_entity": "Router Guess"},
    )

    assert seen["target_entity"] == "Explicit Choice"


def test_target_entity_recovered_when_the_router_supplies_none():
    """
    `router.py`'s deterministic XGRAPH override hardcodes target_entity=None,
    and one of its patterns matches "What other cases is X involved in?" — the
    archetypal cross-case question. Recovery covers exactly that hole.
    """
    from src.pipeline.harness.agents.cross_case_linkage import _recover_target_entity

    assert _recover_target_entity(
        "What other cases is Hina Malik involved in?"
    ) == "Hina Malik"

    # A genuinely open-ended question names nobody, and inventing a seed for it
    # would narrow a search the user asked to be broad.
    assert _recover_target_entity(
        "Which cases share suspects or entities with other cases?"
    ) is None


def test_unconfirmed_link_descriptions_carry_the_real_names():
    """
    The keys are `entity`/`candidate`, not `from`/`to`. Reading the wrong ones
    rendered every link as "between an entity and another entity" — which tells
    a reviewer a match exists while withholding the names needed to act on it.
    """
    from src.pipeline.harness.agents.cross_case_linkage import _links_from_xgraph
    from src.pipeline.harness.contracts import XGraphToolResult

    result = XGraphToolResult(
        status=ToolStatus.EMPTY,
        unconfirmed_links=[{
            "entity": "Hina Malik",
            "candidate": "Bilal Malik",
            "basis": "matched on near-identical name",
            "tier": "flagged_unverified",
            "confidence": 0.61,
        }],
    )

    links = _links_from_xgraph(result, limit=5)

    assert len(links) == 1
    assert links[0].is_unconfirmed
    assert "Hina Malik" in links[0].description
    assert "Bilal Malik" in links[0].description
    assert "an entity and another entity" not in links[0].description
    assert "matched on near-identical name" in links[0].description


# ══════════════════════════════════════════════════════════════════════════
# 2. Timeline's RAG supplement
# ══════════════════════════════════════════════════════════════════════════

def test_timeline_reports_both_contributing_legs():
    """
    The first argument is GRAPH's STATUS, not its row count: a successful GRAPH
    call returning zero events still contributed, because "no events are
    recorded for this case" is itself the answer to a timeline question.
    """
    from src.pipeline.harness.agents.timeline import _contributing_tools

    assert _contributing_tools(True, True) == ["GRAPH", "RAG"]
    assert _contributing_tools(True, False) == ["GRAPH"]
    # GRAPH failed outright; RAG carried the timeline.
    assert _contributing_tools(False, True) == ["RAG"]
    assert _contributing_tools(False, False) == []


@pytest.mark.asyncio
async def test_timeline_merges_rag_without_duplicating_graph_chunks(monkeypatch):
    """
    Both legs can legitimately return the same passage. A timeline that lists
    one event twice is worse than one that lists it once.
    """
    from src.pipeline.harness.agents import timeline
    from src.pipeline.harness.contracts import GraphToolResult, ToolResult

    shared = EvidenceChunk(
        id="shared-1", text="2026-04-02 incident occurred",
        metadata=ChunkMetadata(source_tool="GRAPH", case_id="CASE-1"),
    )
    rag_only = EvidenceChunk(
        id="rag-1", text="2026-04-03 statement recorded",
        metadata=ChunkMetadata(source_tool="RAG", case_id="CASE-1"),
    )

    async def fake_graph(tool_input, events=None, **kwargs):
        return GraphToolResult(status=ToolStatus.OK, chunks=[shared])

    async def fake_rag(tool_input, events=None, **kwargs):
        return ToolResult(status=ToolStatus.OK, chunks=[shared, rag_only])

    monkeypatch.setattr(timeline.registry, "graph_tool", fake_graph)
    monkeypatch.setattr(timeline.registry, "rag_tool", fake_rag)

    result = await timeline.run(
        SubAgentInput(
            query_text="timeline",
            caller=CallerContext(role="investigator", active_case_id="CASE-1"),
        )
    )

    ids = [ev.event_id for ev in result.timeline]
    assert ids.count("shared-1") == 1, "the shared chunk must appear once"
    assert "rag-1" in ids, "RAG must contribute events GRAPH did not supply"
    # GRAPH stays primary: its chunk keeps the lower [Document N] index.
    assert ids[0] == "shared-1" or result.timeline[0].event_id == "shared-1"


@pytest.mark.asyncio
async def test_timeline_survives_graph_failure_when_rag_covers(monkeypatch):
    """
    With usable RAG evidence there IS a timeline to build, so degrading beats
    abstaining. Before the RAG leg existed, a GRAPH failure meant no timeline.
    """
    from src.pipeline.harness.agents import timeline
    from src.pipeline.harness.contracts import GraphToolResult, ToolError, ToolResult

    async def failing_graph(tool_input, events=None, **kwargs):
        return GraphToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message="graph down"),
        )

    async def fake_rag(tool_input, events=None, **kwargs):
        return ToolResult(
            status=ToolStatus.OK,
            chunks=[EvidenceChunk(
                id="rag-1", text="2026-04-02 incident",
                metadata=ChunkMetadata(source_tool="RAG", case_id="CASE-1"),
            )],
        )

    monkeypatch.setattr(timeline.registry, "graph_tool", failing_graph)
    monkeypatch.setattr(timeline.registry, "rag_tool", fake_rag)

    result = await timeline.run(
        SubAgentInput(
            query_text="timeline",
            caller=CallerContext(role="investigator", active_case_id="CASE-1"),
        )
    )

    assert result.status is not SubAgentStatus.ABSTAINED
    assert result.timeline, "RAG evidence must still produce a timeline"


# ══════════════════════════════════════════════════════════════════════════
# 3. report_draft's session requirement
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_report_draft_refuses_upfront_without_a_session():
    """
    `generated_files.session_id` is NOT NULL, so a report produced without a
    session cannot be recorded — and an unrecorded report is undownloadable.
    Checking upfront turns a ~45s retrieve/generate/verify/build-then-fail into
    an immediate, explainable refusal.
    """
    from src.pipeline.harness.agents import report_draft

    result = await report_draft.run(
        SubAgentInput(
            query_text="draft a report",
            caller=CallerContext(role="investigator", active_case_id="CASE-1"),
            output_format="file_pdf",
            session_id=None,
        )
    )

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.error is not None
    assert result.error.kind == "invalid_input"
    assert "session_id" in result.error.message
    # Nothing was retrieved: the refusal must precede the pipeline.
    assert result.tools_used == []


def test_backend_names_the_missing_session_rather_than_raising_bare_uuid_error():
    """
    Bare `uuid.UUID(None)` reports "one of the hex, bytes, bytes_le, fields, or
    int arguments must be given", which names neither the field nor why it
    mattered.
    """
    import asyncio

    from src.data_gateway.direct_backend import DirectGateway

    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(DirectGateway().log_generated_file({
            "session_id": None,
            "user_id": "4939e74f-543b-4143-a997-49a86bc98da6",
            "file_type": "pdf",
            "file_name": "x.pdf",
            "file_size_bytes": 1,
            "storage_path": "/tmp/x.pdf",
        }))
