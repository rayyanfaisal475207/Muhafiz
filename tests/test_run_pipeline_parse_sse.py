"""
Unit tests for evaluation/run_pipeline.py's `_parse_sse()` —
GOLD_QA_MASTER_FIX_PLAN.md Module 19.

Pure string-in/dict-out parsing, no network, no monkeypatching needed.
"""
import json

from evaluation.run_pipeline import _parse_sse


def _sse(*events: dict) -> str:
    return "\n".join(f"data: {json.dumps(e)}" for e in events)


def test_captures_route_and_subagent_from_dispatch_detail():
    sse = _sse(
        {"step": "supervisor:dispatch", "status": "active",
         "detail": "Classified query as route='GRAPH' -> sub-agent='Case Summarization'"},
        {"step": "response", "status": "done", "detail": "The weapon was a 30-bore pistol."},
    )
    result = _parse_sse(sse)
    assert result["route"] == "GRAPH"
    assert result["subagent"] == "Case Summarization"
    assert "30-bore pistol" in result["actual_answer"]


def test_no_documents_field_anywhere_yields_empty_context_not_a_crash():
    """The dead condition this fix replaced (`step in ("retrieval",
    "retrieved_docs") and documents`) never matched anything on this
    codebase's real event shapes — confirm a stream with none of the new
    fields either still parses cleanly to an empty context."""
    sse = _sse(
        {"step": "supervisor", "status": "active", "detail": "Routing..."},
        {"step": "response", "status": "done", "detail": "An answer with no attribution."},
    )
    result = _parse_sse(sse)
    assert result["retrieval_context"] == []


def test_captures_bounded_citation_metadata_as_provenance_strings():
    sse = _sse(
        {"step": "citations", "status": "done", "detail": "1 citation(s)",
         "citations": [{"document_index": 1, "source_tool": "GRAPH",
                        "case_id": "fir-891-24", "source_file": "case_record",
                        "confidence": 0.95}]},
        {"step": "response", "status": "done", "detail": "The weapon was a 30-bore pistol [Document 1]."},
    )
    result = _parse_sse(sse)
    assert len(result["retrieval_context"]) == 1
    ctx = result["retrieval_context"][0]
    assert "[Document 1]" in ctx
    assert "tool=GRAPH" in ctx
    assert "case=fir-891-24" in ctx
    assert "source=case_record" in ctx


def test_citations_with_no_optional_fields_still_captured():
    sse = _sse(
        {"step": "citations", "status": "done", "detail": "1 citation(s)",
         "citations": [{"document_index": 1, "source_tool": "RAG"}]},
        {"step": "response", "status": "done", "detail": "An answer."},
    )
    result = _parse_sse(sse)
    assert result["retrieval_context"] == ["[Document 1] tool=RAG"]


def test_captures_source_references_from_web_and_file_generation_events():
    sse = _sse(
        {"step": "web_search", "status": "done", "detail": "Retrieved 1 web results",
         "sources": [{"filename": "https://example.com/article", "type": "web"}]},
        {"step": "response", "status": "done", "detail": "Per the article, ..."},
    )
    result = _parse_sse(sse)
    assert "source: https://example.com/article" in result["retrieval_context"]


def test_malformed_json_lines_are_skipped_not_raised():
    sse = "data: {not valid json\n" + _sse(
        {"step": "response", "status": "done", "detail": "A perfectly fine and complete answer."}
    )
    result = _parse_sse(sse)
    assert "A perfectly fine and complete answer." in result["actual_answer"]
