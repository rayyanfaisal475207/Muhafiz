"""
Tests for src/pipeline/citation_consistency.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §5's trust-layer table, built as
part of Phase 8 -- see report_drafting.py's module docstring for why this
module exists this session at all).
"""
from __future__ import annotations

from src.pipeline.citation_consistency import check_citation_consistency


def test_all_citations_in_range_is_consistent():
    result = check_citation_consistency(
        "Claim A [Document 1]. Claim B [Document 2].", valid_citation_count=2
    )
    assert result.consistent is True
    assert result.invalid_indices == []


def test_out_of_range_citation_is_inconsistent():
    result = check_citation_consistency(
        "Claim A [Document 1]. Claim B [Document 2].", valid_citation_count=1
    )
    assert result.consistent is False
    assert result.invalid_indices == [2]
    assert "2" in result.reason


def test_zero_index_is_always_invalid():
    result = check_citation_consistency("See [Document 0].", valid_citation_count=3)
    assert result.consistent is False
    assert result.invalid_indices == [0]


def test_no_citations_at_all_is_consistent():
    # Uncited text is the Verifier's problem, not this check's.
    result = check_citation_consistency("A generic, uncited statement.", valid_citation_count=1)
    assert result.consistent is True


def test_duplicate_invalid_indices_are_deduplicated_and_sorted():
    result = check_citation_consistency(
        "[Document 5] and again [Document 5] and [Document 3].", valid_citation_count=1
    )
    assert result.invalid_indices == [3, 5]


def test_zero_valid_citations_rejects_every_marker():
    result = check_citation_consistency("Cites [Document 1].", valid_citation_count=0)
    assert result.consistent is False
    assert result.invalid_indices == [1]


def test_case_insensitive_marker_matching():
    result = check_citation_consistency("cites [document 1].", valid_citation_count=1)
    assert result.consistent is True
