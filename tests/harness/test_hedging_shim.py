"""
The hedging check must actually fire on harness-produced chunks.

`verifier.py::_check_hedging()` reads `chunk["graph_confidence"]` off the TOP
LEVEL of the chunk dict. The harness normalizes confidence into
`metadata.confidence`, which is the contract's correct shape — and which
SILENTLY DISABLED the check for every harness chunk: the verifier's lookup
returned None, hit its `if gc is None: continue` guard, and low-confidence
graph evidence passed unhedged.

Verified before the fix: the identical chunk in legacy shape was flagged, and
in harness shape produced zero issues. It had not bitten only because nothing
routes to the harness yet.

These tests exist to prove the gate is live, and to make any future removal of
the `graph_confidence` shim fail loudly instead of silently reopening the hole.
The hedging gate has no backstop — the LLM judge does not reliably catch a
missing hedge — so "silently skipped" is a safety hole, not a cosmetic mismatch.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness.contracts import ChunkMetadata, EvidenceChunk
from src.pipeline.harness.tools.real import _to_evidence
from src.pipeline.verifier import _check_hedging

# Below the verifier's 0.85 hedging threshold.
_LOW = 0.34
_HIGH = 0.95

_UNHEDGED = "The vehicle is registered to the suspect [Document 1]."
_HEDGED = (
    "The vehicle may be registered to the suspect [Document 1], though this "
    "connection is unconfirmed."
)


def _graph_chunk(confidence: float) -> dict:
    """A graph chunk as the real adapter produces it, serialized for the verifier."""
    return _to_evidence(
        {
            "id": "g1",
            "text": "Vehicle VEH-0091 linked to the suspect.",
            "graph_confidence": confidence,
            "metadata": {"case_id": "CASE-A"},
        },
        "GRAPH",
        1,
    ).model_dump()


# ── The test that matters: the gate now gates ────────────────────────────

def test_low_confidence_chunk_without_hedging_is_flagged():
    """
    THE REGRESSION THIS FIX EXISTS FOR. Before the shim this returned no
    issues — the check skipped every harness chunk. It must now flag.
    """
    issues = _check_hedging(_UNHEDGED, [_graph_chunk(_LOW)])

    assert issues, (
        "the hedging check silently skipped a low-confidence harness chunk — "
        "the graph_confidence shim is missing or broken"
    )
    assert "Document 1" in issues[0]


def test_low_confidence_chunk_with_hedging_passes():
    """The check must accept a properly hedged answer, not flag unconditionally."""
    assert _check_hedging(_HEDGED, [_graph_chunk(_LOW)]) == []


def test_high_confidence_chunk_needs_no_hedging():
    """Above the threshold, an unhedged citation is fine."""
    assert _check_hedging(_UNHEDGED, [_graph_chunk(_HIGH)]) == []


# ── The shim's mechanics ─────────────────────────────────────────────────

def test_adapter_emits_graph_confidence_at_top_level():
    """
    The verifier's lookup is positional — top-level, not nested. A future
    change that drops this field would silently disable the check again.
    """
    chunk = _graph_chunk(_LOW)

    assert chunk.get("graph_confidence") == pytest.approx(_LOW)


def test_shim_mirrors_metadata_confidence_exactly():
    """
    Two views of one value. If they diverge, harness code and the verifier
    would disagree about the same chunk's confidence.
    """
    chunk = _graph_chunk(_LOW)

    assert chunk["graph_confidence"] == chunk["metadata"]["confidence"]


def test_chunks_without_confidence_leave_the_shim_unset():
    """
    RAG/SQL/WEB compute no confidence, and `None` there is correct rather than
    a failure. The shim must not invent a value — the verifier's `is None`
    guard is the right behaviour for those.
    """
    chunk = _to_evidence(
        {"id": "r1", "text": "A document passage.", "metadata": {"case_id": "CASE-A"}},
        "RAG",
        1,
    ).model_dump()

    assert chunk.get("graph_confidence") is None
    assert chunk["metadata"]["confidence"] is None


def test_the_gap_this_fixes_is_real():
    """
    Pins the original defect: a chunk carrying confidence ONLY in metadata —
    the pre-shim harness shape — is invisible to the check. Documents why the
    duplication exists, so it is not "cleaned up" without reading this.
    """
    metadata_only = EvidenceChunk(
        id="g1",
        text="Vehicle linked to the suspect.",
        metadata=ChunkMetadata(source_tool="GRAPH", case_id="CASE-A", confidence=_LOW),
    ).model_dump()
    metadata_only.pop("graph_confidence", None)

    assert _check_hedging(_UNHEDGED, [metadata_only]) == [], (
        "metadata-only confidence is now visible to the verifier — if this "
        "passes differently, the verifier was changed and the shim may be "
        "removable (see §7 Part B)"
    )


# ── End to end through a sub-agent ───────────────────────────────────────

async def test_low_confidence_graph_evidence_is_gated_through_a_subagent(monkeypatch):
    """
    The path that actually matters in production: a sub-agent's own
    verify_grounding() call must see the confidence the tool computed.
    """
    from src.pipeline.harness.agents import timeline as timeline_agent
    from src.pipeline.harness.contracts import CallerContext, Role, SubAgentInput
    from src.pipeline.harness.tools import registry

    registry.use_real()

    async def _retrieve(*a, **k):
        return {
            "chunks": [{
                "id": "g1", "text": "Event on 2026-03-14.",
                "graph_confidence": _LOW,
                "metadata": {"case_id": "CASE-A", "occurred_on": "2026-03-14"},
            }],
            "hop_count": 1, "compounded_confidence": _LOW,
            "seed_entities": [{"entity_id": "E1"}], "unconfirmed_links": [],
        }

    monkeypatch.setattr("src.retrieval.graph_retriever.retrieve_graph", _retrieve)

    seen: dict = {}
    real_verify = timeline_agent.verify_grounding

    async def _spy(answer, cited_chunks, case_id, **kw):
        seen["chunks"] = cited_chunks
        return await real_verify(answer, cited_chunks, case_id, **kw)

    monkeypatch.setattr(timeline_agent, "verify_grounding", _spy)

    await timeline_agent.run(
        SubAgentInput(
            query_text="timeline",
            caller=CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-A"),
        )
    )

    assert seen["chunks"], "the verifier received no chunks"
    assert seen["chunks"][0].get("graph_confidence") == pytest.approx(_LOW), (
        "confidence did not survive to the verifier — the hedging check would "
        "silently skip this chunk in production"
    )
