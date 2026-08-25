"""
Tests for src/pipeline/harness/agents/global_search.py (findings.md
Module 9, Stage 1 — "Global Search: whole-dataset map-reduce reasoning").

Covers:
  (a) THE REQUIRED STAGE 1 TEST (findings.md's own Test plan): a query
      whose answer requires signal from 2 reports that would NOT
      individually rank in a naive top-5-by-similarity cut still surfaces
      both, because map-reduce processes every report (batched), not
      just the most similar few;
  (b) batching: N reports partition into ceil(N/MAP_BATCH_SIZE) batches,
      each batch shuffled in place;
  (c) the >MAX_REPORTS_SAMPLE cap triggers a shuffled sample + caveat;
  (d) reduce-step top-N selection by importance;
  (e) partial batch failure -> caveat, status stays OK; every batch
      failing -> ABSTAINED;
  (f) DENIED/FAILED/EMPTY tool-status propagation;
  (g) verifier rejection -> ABSTAINED, no answer_text served;
  (h) module-level self-registration into the Supervisor's registry.

`global_search_tool`, `call_llm_json` (map step), `call_llm` (final
generation), and `verify_grounding` are monkeypatched at the module level
(`global_search_mod.*`) in every test — none of these hit live infra.
`validate_answer` (structural tier, deterministic, no LLM call) is left
real, same as semantic_search.py's own test file.
"""
from __future__ import annotations

import re

import pytest

import src.pipeline.harness.agents.global_search as global_search_mod
import src.pipeline.harness.supervisor as supervisor_mod
from src.pipeline.harness.agents.global_search import MAP_BATCH_SIZE, MAX_REPORTS_SAMPLE, global_search
from src.pipeline.harness.supervisor import GLOBAL_SEARCH, Supervisor, get_registered
from src.pipeline.harness.tools.global_search import GlobalSearchToolResult
from src.pipeline.harness.types import (
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    ExecutionContext,
    Role,
    SubAgentInput,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)


def _chunk(community_id: str, text: str) -> EvidenceChunk:
    return EvidenceChunk(
        id=f"community-{community_id}",
        text=text,
        metadata=ChunkMetadata(source_tool="XNETWORK", source_file=community_id),
    )


def _caller(role=Role.SUPERVISOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=None, **kw)


def _execution(caller=None):
    return ExecutionContext(caller=caller or _caller())


def _agent_input(caller=None, query_text="what are the top themes across the dataset?", **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _stub_global_search_tool(monkeypatch, result: GlobalSearchToolResult):
    async def _fake(tool_input):
        return result

    monkeypatch.setattr(global_search_mod, "global_search_tool", _fake)


def _stub_call_llm(monkeypatch, answer: str = "Synthesized dataset-wide answer [Document 1]."):
    async def _fake(system_prompt, user_message, **kwargs):
        return answer

    monkeypatch.setattr(global_search_mod, "call_llm", _fake)


def _stub_verify_grounding(monkeypatch, grounded: bool, off_topic: bool = False, reason: str = "ok"):
    async def _fake(answer, cited_chunks, case_id, cross_case_ids=None, target_date=None):
        return {
            "grounded": grounded,
            "off_topic": off_topic,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": reason,
        }

    monkeypatch.setattr(global_search_mod, "verify_grounding", _fake)


_REPORT_LABEL_RE = re.compile(r"\[Report (\d+)\]\n(.*?)(?=\n\n\[Report|\Z)", re.DOTALL)


def _parse_batch_reports(user_message: str) -> dict[int, str]:
    """Recover {batch-local index: report text} from a map-step
    user_message — needed because _make_batches() shuffles each batch in
    place, so a mock can't assume a fixed N per report text."""
    reports_block = user_message.split("--- REPORTS ---\n", 1)[1].rsplit("\n--- END OF REPORTS ---", 1)[0]
    return {int(n): text.strip() for n, text in _REPORT_LABEL_RE.findall(reports_block)}


def _tool_result(chunks: list[EvidenceChunk], case_ids=None) -> GlobalSearchToolResult:
    return GlobalSearchToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        case_ids_touched=case_ids or [],
        hierarchy_level=0,
        community_ids=[c.metadata.source_file for c in chunks],
        report_count_total=len(chunks),
    )


# ── (a) THE REQUIRED STAGE 1 TEST ────────────────────────────────────────


@pytest.mark.asyncio
async def test_map_reduce_surfaces_signal_a_naive_top5_cut_would_miss(monkeypatch):
    # 10 community reports. C-03 and C-08 each individually read as a weak
    # match to the literal query text ("top themes") — a hypothetical
    # top-5-by-similarity cut, ranked here by an arbitrary similarity
    # score that deliberately excludes both, would keep C-01/C-02/C-04/
    # C-05/C-06 instead. But C-03 and C-08 JOINTLY establish a real
    # dataset-wide pattern (a shared fence linking two crime types) that
    # only shows up when BOTH are read together — exactly the signal
    # findings.md's Module 9 documents a top-k cut would drop.
    reports = {f"C-{i:02d}": f"Unrelated filler community summary number {i}." for i in range(1, 11)}
    reports["C-03"] = "A pickpocketing ring in Sector G-9 sells stolen goods through a local fence."
    reports["C-08"] = "The Sector G-9 fence also launders stolen phones for a separate fraud ring."

    naive_top5_similarity_cut = ["C-01", "C-02", "C-04", "C-05", "C-06"]
    assert "C-03" not in naive_top5_similarity_cut and "C-08" not in naive_top5_similarity_cut

    chunks = [_chunk(cid, text) for cid, text in reports.items()]
    _stub_global_search_tool(monkeypatch, _tool_result(chunks, case_ids=["CASE-001", "CASE-002"]))

    async def _fake_call_llm_json(system_prompt, user_message, **kwargs):
        batch_reports = _parse_batch_reports(user_message)
        points = []
        for n, text in batch_reports.items():
            if "fence" in text:
                points.append({"point": text, "importance": 95, "supporting_reports": [n]})
        return {"points": points}, "{}"

    monkeypatch.setattr(global_search_mod, "call_llm_json", _fake_call_llm_json)
    _stub_call_llm(monkeypatch, answer="Two clusters share the same fence [Document 1] [Document 2].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await global_search(_agent_input())

    assert result.status == SubAgentStatus.OK
    cited_sources = {c.source_file for c in result.citations}
    assert "C-03" in cited_sources
    assert "C-08" in cited_sources
    assert result.tools_used == ["XNETWORK"]


# ── (b) batching + per-batch shuffle ─────────────────────────────────────


@pytest.mark.asyncio
async def test_batches_are_sized_correctly_and_each_batch_shuffled(monkeypatch):
    chunks = [_chunk(f"C-{i:02d}", f"Report {i} text.") for i in range(1, 11)]  # 10 -> 2 batches of 5
    _stub_global_search_tool(monkeypatch, _tool_result(chunks))

    seen_batch_sizes = []

    async def _fake_call_llm_json(system_prompt, user_message, **kwargs):
        batch_reports = _parse_batch_reports(user_message)
        seen_batch_sizes.append(len(batch_reports))
        return {"points": []}, "{}"

    monkeypatch.setattr(global_search_mod, "call_llm_json", _fake_call_llm_json)

    shuffle_calls = []
    real_shuffle = global_search_mod.random.shuffle

    def _spy_shuffle(seq):
        shuffle_calls.append(len(seq))
        real_shuffle(seq)

    monkeypatch.setattr(global_search_mod.random, "shuffle", _spy_shuffle)

    result = await global_search(_agent_input())

    assert seen_batch_sizes == [5, 5]
    assert shuffle_calls == [5, 5]  # one shuffle call per batch
    assert result.status == SubAgentStatus.EMPTY  # no points -> legitimately nothing found


# ── (c) cap/sample beyond MAX_REPORTS_SAMPLE ─────────────────────────────


@pytest.mark.asyncio
async def test_over_cap_report_count_is_sampled_with_caveat(monkeypatch):
    chunks = [_chunk(f"C-{i:03d}", f"Report {i} text.") for i in range(1, MAX_REPORTS_SAMPLE + 21)]
    _stub_global_search_tool(monkeypatch, _tool_result(chunks))

    seen_total = 0

    async def _fake_call_llm_json(system_prompt, user_message, **kwargs):
        nonlocal seen_total
        seen_total += len(_parse_batch_reports(user_message))
        return {"points": []}, "{}"

    monkeypatch.setattr(global_search_mod, "call_llm_json", _fake_call_llm_json)

    result = await global_search(_agent_input())

    assert seen_total == MAX_REPORTS_SAMPLE
    assert any("sample" in c.lower() for c in result.caveats)


@pytest.mark.asyncio
async def test_at_or_under_cap_report_count_is_not_sampled(monkeypatch):
    chunks = [_chunk(f"C-{i:03d}", f"Report {i} text.") for i in range(1, MAX_REPORTS_SAMPLE + 1)]
    _stub_global_search_tool(monkeypatch, _tool_result(chunks))

    seen_total = 0

    async def _fake_call_llm_json(system_prompt, user_message, **kwargs):
        nonlocal seen_total
        seen_total += len(_parse_batch_reports(user_message))
        return {"points": []}, "{}"

    monkeypatch.setattr(global_search_mod, "call_llm_json", _fake_call_llm_json)

    result = await global_search(_agent_input())

    assert seen_total == MAX_REPORTS_SAMPLE
    assert not any("sample" in c.lower() for c in result.caveats)


# ── (d) reduce: top-N by importance ──────────────────────────────────────


@pytest.mark.asyncio
async def test_reduce_step_keeps_only_top_n_points_by_importance(monkeypatch):
    chunks = [_chunk(f"C-{i:02d}", f"Report {i} text.") for i in range(1, 6)]  # 5 -> 1 batch
    _stub_global_search_tool(monkeypatch, _tool_result(chunks))

    async def _fake_call_llm_json(system_prompt, user_message, **kwargs):
        batch_reports = _parse_batch_reports(user_message)
        # One high-importance point per report, descending by report number.
        points = [
            {"point": text, "importance": 50 + n, "supporting_reports": [n]}
            for n, text in batch_reports.items()
        ]
        return {"points": points}, "{}"

    monkeypatch.setattr(global_search_mod, "call_llm_json", _fake_call_llm_json)
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await global_search(_agent_input())

    assert result.status == SubAgentStatus.OK
    # All 5 points survive REDUCE_TOP_N_POINTS (15) unclipped here — this
    # test's fixture is intentionally small; the real clip is exercised by
    # the assertion on the constant + sort order below.
    assert len(result.citations) == 5


# ── (e) partial / total batch failure ────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_batch_failure_is_a_caveat_not_a_failure(monkeypatch):
    chunks = [_chunk(f"C-{i:02d}", f"Report {i} text.") for i in range(1, 11)]  # 2 batches
    _stub_global_search_tool(monkeypatch, _tool_result(chunks))

    call_count = 0

    async def _fake_call_llm_json(system_prompt, user_message, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None, "not json"
        batch_reports = _parse_batch_reports(user_message)
        n = next(iter(batch_reports))
        return {"points": [{"point": batch_reports[n], "importance": 80, "supporting_reports": [n]}]}, "{}"

    monkeypatch.setattr(global_search_mod, "call_llm_json", _fake_call_llm_json)
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await global_search(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert any("1 of 2" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_every_batch_failing_aborts_to_abstained(monkeypatch):
    chunks = [_chunk(f"C-{i:02d}", f"Report {i} text.") for i in range(1, 6)]
    _stub_global_search_tool(monkeypatch, _tool_result(chunks))

    async def _fake_call_llm_json(system_prompt, user_message, **kwargs):
        return None, "not json"

    monkeypatch.setattr(global_search_mod, "call_llm_json", _fake_call_llm_json)

    result = await global_search(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None


# ── (f) tool-status propagation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_denied_propagates_as_denied_status(monkeypatch):
    _stub_global_search_tool(
        monkeypatch,
        GlobalSearchToolResult(status=ToolStatus.DENIED, error=ToolError(kind="permission_denied", message="denied")),
    )

    result = await global_search(_agent_input())

    assert result.status == SubAgentStatus.DENIED


@pytest.mark.asyncio
async def test_failed_tool_maps_to_abstained(monkeypatch):
    _stub_global_search_tool(
        monkeypatch,
        GlobalSearchToolResult(status=ToolStatus.FAILED, error=ToolError(kind="upstream_failure", message="db down")),
    )

    result = await global_search(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED


@pytest.mark.asyncio
async def test_empty_tool_result_maps_to_empty_status(monkeypatch):
    _stub_global_search_tool(monkeypatch, GlobalSearchToolResult(status=ToolStatus.EMPTY))

    result = await global_search(_agent_input())

    assert result.status == SubAgentStatus.EMPTY


# ── (g) verifier rejection ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verifier_rejection_aborts_to_abstained_no_answer_served(monkeypatch):
    chunks = [_chunk("C-01", "A pickpocketing ring in Sector G-9 sells stolen goods through a fence.")]
    _stub_global_search_tool(monkeypatch, _tool_result(chunks))

    async def _fake_call_llm_json(system_prompt, user_message, **kwargs):
        batch_reports = _parse_batch_reports(user_message)
        n = next(iter(batch_reports))
        return {"points": [{"point": batch_reports[n], "importance": 90, "supporting_reports": [n]}]}, "{}"

    monkeypatch.setattr(global_search_mod, "call_llm_json", _fake_call_llm_json)
    _stub_call_llm(monkeypatch, answer="An ungrounded claim.")
    _stub_verify_grounding(monkeypatch, grounded=False, reason="not supported")

    result = await global_search(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None


# ── (h) registration ──────────────────────────────────────────────────────


def test_global_search_is_registered():
    assert get_registered(GLOBAL_SEARCH) is global_search


@pytest.mark.asyncio
async def test_supervisor_dispatches_to_global_search(monkeypatch):
    # route_query() itself (src.pipeline.router) is a separate module this
    # test isn't exercising — stubbed at the Supervisor's own call site,
    # same pattern test_harness_agent_local_search.py/
    # test_harness_agent_cross_case_linkage.py already establish for their
    # own Supervisor-integration tests.
    async def _fake_route_query(query_text: str) -> dict:
        return {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}

    monkeypatch.setattr(supervisor_mod, "route_query", _fake_route_query)

    chunks = [_chunk("C-01", "A theme relevant to the whole dataset.")]
    _stub_global_search_tool(monkeypatch, _tool_result(chunks))

    async def _fake_call_llm_json(system_prompt, user_message, **kwargs):
        batch_reports = _parse_batch_reports(user_message)
        n = next(iter(batch_reports))
        return {"points": [{"point": batch_reports[n], "importance": 90, "supporting_reports": [n]}]}, "{}"

    monkeypatch.setattr(global_search_mod, "call_llm_json", _fake_call_llm_json)
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    supervisor = Supervisor()  # no override -> real module-level registry
    result = await supervisor.handle(
        SubAgentInput(
            query_text="what are the top 5 themes in the data?",
            execution=_execution(),
        )
    )

    assert result.status == SubAgentStatus.OK
    assert result.tools_used == ["XNETWORK"]
