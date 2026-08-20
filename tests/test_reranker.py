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
