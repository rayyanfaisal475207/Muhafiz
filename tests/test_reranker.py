"""Tests for src/retrieval/reranker.py's RRF fusion and year_boost (C-1)."""
from src.retrieval.reranker import reciprocal_rank_fusion


def _doc(doc_id, source):
    return {"id": doc_id, "source": source}


def test_year_boost_is_uniform_across_case_filenames():
    """
    C-1 (audit 2026-08-04): confirmed against the real corpus — every case
    filename embeds the same year (2026) by the FIR-YYYY-CATEGORY-NNN
    convention, so the boost must be identical across case documents and
    must not reorder two docs that tied on RRF score before the boost.
    """
    docs_a = [_doc("a", "FIR-2026-ARMS-003.pdf")]
    docs_b = [_doc("b", "WITNESS-FIR-2026-BUR-007-01.pdf")]
    result = reciprocal_rank_fusion([docs_a, docs_b], top_k=5)
    scores = {d["id"]: d["rrf_score"] for d in result}
    # Both appear only in their own single-item list at rank 1, so their
    # pre-boost RRF scores are identical — the year boost must not break
    # that tie since both filenames embed the same year.
    assert scores["a"] == scores["b"]


def test_year_boost_favors_case_filename_over_generic_reference_doc():
    """
    A generic procedural reference doc (no year in filename, e.g. this
    corpus's REAL-*.pdf files) gets zero boost; a case document (year in
    filename) gets a small positive one — confirmed current, intentional
    behavior, not a bug (see reranker.py's C-1 comment).
    """
    case_doc = [_doc("case", "FIR-2026-ARMS-003.pdf")]
    generic_doc = [_doc("generic", "REAL-004-copy-of-fir-procedure.pdf")]
    result = reciprocal_rank_fusion([case_doc, generic_doc], top_k=5)
    scores = {d["id"]: d["rrf_score"] for d in result}
    assert scores["case"] > scores["generic"]


def test_year_boost_does_not_fire_on_non_year_looking_filename():
    docs = [_doc("x", "REAL-004-copy-of-fir-procedure.pdf")]
    result = reciprocal_rank_fusion([docs], top_k=5)
    assert result[0]["rrf_score"] == round(1.0 / 61, 6)


# ── record_date preference (M8, docs/decisions/0001-muhafiz-api-migration.md) ──

def _api_doc(doc_id, record_date=None):
    """API-sourced chunk shape: source string carries no year at all."""
    return {"id": doc_id, "source": f"psrms/fir/{doc_id}#narrative",
            "metadata": {"source": f"psrms/fir/{doc_id}#narrative", "record_date": record_date}}


def test_api_sourced_chunk_with_no_year_in_source_gets_zero_boost_with_no_record_date():
    docs = [_api_doc("fir-1-26")]  # no record_date
    result = reciprocal_rank_fusion([docs], top_k=5)
    assert result[0]["rrf_score"] == round(1.0 / 61, 6)


def test_record_date_provides_the_boost_the_source_string_cannot():
    docs = [_api_doc("fir-1-26", record_date="2026-08-18T15:10:00Z")]
    result = reciprocal_rank_fusion([docs], top_k=5)
    assert result[0]["rrf_score"] > round(1.0 / 61, 6)


def test_record_date_is_preferred_over_filename_when_both_present():
    """A file-sourced doc with BOTH a filename year and a record_date
    (hypothetical future overlap) must use record_date, not the filename."""
    doc = {"id": "x", "source": "FIR-2020-ARMS-001.pdf",
           "metadata": {"source": "FIR-2020-ARMS-001.pdf", "record_date": "2026-01-01"}}
    result = reciprocal_rank_fusion([[doc]], top_k=5)
    # 2026, not 2020 -> boost = (2026-2020)*0.0005 = 0.003
    assert result[0]["rrf_score"] == round(1.0 / 61 + 0.003, 6)


# ── [Module 38] weights / score_key / apply_year_boost ────────────────────
#
# `cross_rerank_multi()` fuses per-query cross-encoder lists with this same
# function rather than growing a second RRF implementation. These three
# parameters are what that reuse needs; each defaults to the behaviour every
# pre-Module-38 caller already had.

import pytest

from src.retrieval.reranker import RRF_K, reciprocal_rank_fusion


def test_weights_scale_a_lists_contribution_without_touching_rank_math():
    """A weighted list contributes w/(rank+k). With one list at weight 3 the
    doc it ranks first beats the doc the other two rank first."""
    only_a = [_doc("a", "x.pdf")]
    only_b = [_doc("b", "x.pdf")]
    b_then_a = [_doc("b", "x.pdf"), _doc("a", "x.pdf")]
    lists = [only_a, only_b, b_then_a]

    equal = reciprocal_rank_fusion(lists, top_k=5)
    assert [d["id"] for d in equal] == ["b", "a"]

    weighted = reciprocal_rank_fusion(lists, top_k=5, weights=[3.0, 1.0, 1.0])
    assert [d["id"] for d in weighted] == ["a", "b"]


def test_weights_default_to_equal_and_reproduce_the_unweighted_score():
    docs = [[_doc("a", "x.pdf"), _doc("b", "x.pdf")], [_doc("b", "x.pdf")]]
    assert reciprocal_rank_fusion(docs, top_k=5) == reciprocal_rank_fusion(
        docs, top_k=5, weights=[1.0, 1.0]
    )


def test_weights_must_correspond_one_to_one_with_the_ranked_lists():
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([[_doc("a", "x.pdf")]], top_k=5, weights=[1.0, 1.0])


def test_score_key_leaves_an_existing_rrf_score_untouched():
    """The second fusion pass must not overwrite the score the candidate pool
    was built with — that number is what retrieval logging and provenance
    mean by `rrf_score`."""
    doc = {"id": "a", "source": "x.pdf", "rrf_score": 0.87}
    result = reciprocal_rank_fusion([[doc]], top_k=5, score_key="cross_rerank_rrf")
    assert result[0]["rrf_score"] == 0.87
    assert result[0]["cross_rerank_rrf"] == round(1.0 / (1 + RRF_K), 6)


def test_year_boost_can_be_switched_off_for_a_second_fusion_pass():
    """The boost is a recency prior over the CANDIDATE POOL and belongs to
    the fusion that builds it. Up to +0.003 against a 0.00026 gap between
    adjacent ranks, re-applying it would move chunks several places for
    reasons unrelated to the question."""
    case_doc = [_doc("case", "FIR-2026-ARMS-003.pdf")]
    generic_doc = [_doc("generic", "REAL-004-copy-of-fir-procedure.pdf")]
    boosted = reciprocal_rank_fusion([case_doc, generic_doc], top_k=5)
    assert boosted[0]["id"] == "case"

    plain = reciprocal_rank_fusion(
        [case_doc, generic_doc], top_k=5, apply_year_boost=False
    )
    scores = {d["id"]: d["rrf_score"] for d in plain}
    assert scores["case"] == scores["generic"] == round(1.0 / (1 + RRF_K), 6)
