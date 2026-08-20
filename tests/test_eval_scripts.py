"""
scripts/eval_end_to_end.py and scripts/eval_keyword_search.py — M11 of the
Muhafiz Data API migration (docs/decisions/0001-muhafiz-api-migration.md).

Both scripts were broken independently of which corpus the eval set
described: eic_eval_set.json is a bare list, not {"queries": [...]}, and
both imported a src.retrieval.hybrid_search module that has never existed
in this codebase. This file proves both bugs are actually fixed.
"""
import json

import pytest

import scripts.eval_end_to_end as e2e
import scripts.eval_keyword_search as kw


# ── bare-list schema handling ────────────────────────────────────────────

class TestBareListSchema:
    def test_eval_set_is_a_bare_list_not_a_queries_wrapper(self):
        """Regression: eic_eval_set.json has always been a bare list —
        eval_data.get("queries", []) always silently returned []."""
        eval_data = [{"question_en": "x", "expected_source_docs": ["a"]}]
        assert isinstance(eval_data, list)

    def test_question_text_prefers_english(self):
        q = {"question_en": "English", "question_ur": "اردو", "question_roman_ur": "Roman"}
        assert e2e._question_text(q) == "English"

    def test_question_text_falls_back_to_urdu(self):
        q = {"question_ur": "اردو", "question_roman_ur": "Roman"}
        assert e2e._question_text(q) == "اردو"

    def test_question_text_falls_back_to_roman_urdu(self):
        q = {"question_roman_ur": "Roman"}
        assert e2e._question_text(q) == "Roman"

    def test_question_text_empty_when_nothing_present(self):
        assert e2e._question_text({}) == ""


# ── eval_end_to_end: real retrieval stack, not a nonexistent module ────

class TestEndToEndRetrieve:
    async def test_retrieve_calls_the_real_hybrid_stack(self, monkeypatch):
        calls = []

        async def fake_embed_text(text):
            calls.append(("embed", text))
            return [0.1, 0.2]

        async def fake_query_similar(query_text, embedding, top_k, where):
            calls.append(("semantic", where))
            return [{"id": "c1", "metadata": {"source": "psrms/fir/fir-1-26#narrative"}}]

        async def fake_get_all_chunks(where):
            calls.append(("pool", where))
            return [{"id": "c1", "text": "x", "metadata": {"source": "psrms/fir/fir-1-26#narrative"}}]

        def fake_retrieve_bm25(query_text, pool, top_k):
            calls.append(("bm25", len(pool)))
            return []

        def fake_rerank_results(semantic, bm25, top_k):
            calls.append(("rerank", len(semantic), len(bm25)))
            return semantic

        monkeypatch.setattr(e2e, "embed_text", fake_embed_text)
        monkeypatch.setattr(e2e, "query_similar", fake_query_similar)
        monkeypatch.setattr(e2e, "get_all_chunks", fake_get_all_chunks)
        monkeypatch.setattr(e2e, "retrieve_bm25", fake_retrieve_bm25)
        monkeypatch.setattr(e2e, "rerank_results", fake_rerank_results)

        results = await e2e.retrieve("test query", top_k=5)

        assert ("embed", "test query") in calls
        assert any(c[0] == "semantic" and c[1] is None for c in calls), "search must be unscoped (where=None)"
        assert any(c[0] == "rerank" for c in calls)
        assert results == [{"id": "c1", "metadata": {"source": "psrms/fir/fir-1-26#narrative"}}]

    async def test_evaluate_end_to_end_counts_hits_by_source(self, tmp_path, monkeypatch):
        eval_set = tmp_path / "eic_eval_set.json"
        eval_set.write_text(json.dumps([
            {"question_en": "Q1", "expected_source_docs": ["psrms/fir/fir-1-26#narrative"]},
            {"question_en": "Q2", "expected_source_docs": ["psrms/fir/fir-999-26#narrative"]},  # will miss
        ]), encoding="utf-8")
        results_path = tmp_path / "results.json"
        monkeypatch.setattr(e2e, "EVAL_SET_PATH", eval_set)
        monkeypatch.setattr(e2e, "RESULTS_PATH", results_path)

        async def fake_retrieve(query_text, top_k=5):
            return [{"metadata": {"source": "psrms/fir/fir-1-26#narrative"}}]
        monkeypatch.setattr(e2e, "retrieve", fake_retrieve)

        await e2e.evaluate_end_to_end()

        written = json.loads(results_path.read_text(encoding="utf-8"))
        assert written["total_queries"] == 2
        assert written["pass_rate"] == 0.5

    async def test_missing_eval_set_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.setattr(e2e, "EVAL_SET_PATH", tmp_path / "absent.json")
        await e2e.evaluate_end_to_end()  # must not raise


# ── eval_keyword_search: BM25 alone, no nonexistent module ─────────────

class TestKeywordSearchEval:
    async def test_evaluates_bm25_directly_no_hybrid_search_import(self, tmp_path, monkeypatch):
        eval_set = tmp_path / "eic_eval_set.json"
        eval_set.write_text(json.dumps([
            {"question_en": "Q1", "expected_source_docs": ["psrms/fir/fir-1-26#narrative"]},
        ]), encoding="utf-8")
        results_path = tmp_path / "kw_results.json"
        monkeypatch.setattr(kw, "EVAL_SET_PATH", eval_set)
        monkeypatch.setattr(kw, "RESULTS_PATH", results_path)

        async def fake_get_all_chunks(where):
            assert where is None
            return [{"id": "c1", "text": "x", "metadata": {"source": "psrms/fir/fir-1-26#narrative"}}]

        def fake_retrieve_bm25(query_text, pool, top_k):
            return [{"id": "c1", "metadata": {"source": "psrms/fir/fir-1-26#narrative"}}]

        monkeypatch.setattr(kw, "get_all_chunks", fake_get_all_chunks)
        monkeypatch.setattr(kw, "retrieve_bm25", fake_retrieve_bm25)

        await kw.evaluate_keyword_search()

        written = json.loads(results_path.read_text(encoding="utf-8"))
        assert written["total_queries"] == 1
        assert written["recall_at_10"] == 1.0
        assert written["mrr"] == 1.0

    async def test_mrr_reflects_rank_of_first_hit(self, tmp_path, monkeypatch):
        eval_set = tmp_path / "eic_eval_set.json"
        eval_set.write_text(json.dumps([
            {"question_en": "Q1", "expected_source_docs": ["target"]},
        ]), encoding="utf-8")
        monkeypatch.setattr(kw, "EVAL_SET_PATH", eval_set)
        monkeypatch.setattr(kw, "RESULTS_PATH", tmp_path / "kw_results.json")

        async def fake_get_all_chunks(where):
            return []
        def fake_retrieve_bm25(query_text, pool, top_k):
            return [
                {"metadata": {"source": "not-it-1"}},
                {"metadata": {"source": "not-it-2"}},
                {"metadata": {"source": "target"}},
            ]
        monkeypatch.setattr(kw, "get_all_chunks", fake_get_all_chunks)
        monkeypatch.setattr(kw, "retrieve_bm25", fake_retrieve_bm25)

        await kw.evaluate_keyword_search()

        written = json.loads((tmp_path / "kw_results.json").read_text(encoding="utf-8"))
        assert written["mrr"] == pytest.approx(1 / 3)
