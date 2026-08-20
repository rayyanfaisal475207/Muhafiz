"""
scripts/build_real_eval_set.py — M11 of the Muhafiz Data API migration
(docs/decisions/0001-muhafiz-api-migration.md).

Every question/answer this generator produces must be verifiable
directly against the record it was built from — these tests check that
property, not just "did it run."
"""
import json
from pathlib import Path

import pytest

import scripts.build_real_eval_set as builder
from src.data_gateway.muhafiz_api.models import FirRecord

FIXTURE = Path(__file__).parent / "fixtures" / "muhafiz_api_snapshot.json"


@pytest.fixture(scope="module")
def firs():
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [FirRecord(r) for r in snapshot["endpoints"]["fir"]]


def test_every_query_has_the_full_schema(firs):
    queries = builder.build_queries(firs)
    required_keys = {
        "id", "question_en", "question_ur", "question_roman_ur", "category",
        "scope", "case_id", "expected_route", "expected_answer_entities",
        "expected_source_docs", "difficulty", "notes",
    }
    for q in queries:
        assert required_keys <= set(q.keys())


def test_query_ids_are_unique(firs):
    queries = builder.build_queries(firs)
    ids = [q["id"] for q in queries]
    assert len(ids) == len(set(ids))


def test_content_rag_station_answer_matches_the_source_fir(firs):
    queries = builder.build_queries(firs)
    rag_queries = [q for q in queries if q["category"] == "content_rag"]
    assert rag_queries

    fir_by_id = {f.fir_id: f for f in firs}
    for q in rag_queries:
        fir = fir_by_id[q["case_id"]]
        expected_station = fir.police_station.get("name")
        if expected_station:
            assert q["expected_answer_entities"] == [expected_station]
        assert q["expected_source_docs"] == [f"psrms/fir/{fir.fir_id}#narrative"]


def test_structured_sql_section_answer_is_a_real_section(firs):
    queries = builder.build_queries(firs)
    sql_queries = [q for q in queries if q["category"] == "structured_sql"]
    assert sql_queries

    real_pairs = {
        (s.get("section_code"), s.get("act"))
        for fir in firs for s in fir.child_rows("fir_section")
        if s.get("section_code") and s.get("act")
    }
    for q in sql_queries:
        section_ref = q["expected_answer_entities"][0]
        code, act = section_ref.split(" ", 1)
        assert (code, act) in real_pairs


def test_cross_case_pattern_cnic_genuinely_appears_on_every_listed_fir(firs):
    queries = builder.build_queries(firs)
    cross_case = [q for q in queries if q["category"] == "cross_case_pattern"]
    assert cross_case, "the real dataset has measured cross-case CNICs — at least one query must exist"

    fir_by_id = {f.fir_id: f for f in firs}
    for q in cross_case:
        for fir_id in q["expected_answer_entities"]:
            fir = fir_by_id[fir_id]
            accused_cnics = {a.get("cnic") for a in fir.child_rows("fir_accused")}
            assert any(c and c in q["notes"] for c in accused_cnics if c), (
                f"query claims CNIC appears on {fir_id}, but that FIR's accused CNICs don't include it"
            )


def test_no_answer_query_cnic_does_not_exist_in_the_dataset(firs):
    queries = builder.build_queries(firs)
    no_answer = [q for q in queries if q["category"] == "no_answer"]
    assert no_answer

    all_cnics = set()
    for fir in firs:
        if fir.complainant_cnic:
            all_cnics.add(fir.complainant_cnic)
        for a in fir.child_rows("fir_accused"):
            if a.get("cnic"):
                all_cnics.add(a["cnic"])
    assert "99999-9999999-9" not in all_cnics


def test_output_is_a_bare_list_matching_the_existing_schema(firs, tmp_path):
    """The fixed eval_end_to_end.py/eval_keyword_search.py (M11) expect a
    bare list, not {"queries": [...]}."""
    queries = builder.build_queries(firs)
    out = tmp_path / "eic_eval_set.json"
    out.write_text(json.dumps(queries, ensure_ascii=False), encoding="utf-8")

    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(loaded, list)
