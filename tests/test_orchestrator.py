"""
Orchestrator — the full pipeline, driven end to end with a fake LLM and a fake
gateway. No network, no database.

Guards the behaviour that broke in production:
  * the session is created WITH its owner, and the exchange is persisted
  * user settings (language, context) reach EVERY answer path, not just RAG
  * a DIRECT-routed request can still produce a file ("make me a PDF of X")
  * file-generation failures are surfaced as error events, never swallowed
  * per-token events are not written to the step log (that blocked the loop)
"""
import asyncio

import pytest

import src.pipeline.orchestrator as orch


@pytest.fixture
def run_pipeline(monkeypatch, patched_gateway):
    """
    Drive process_query with every external dependency faked.
    Returns (events, gateway) for assertions.
    """
    async def _run(message="What PPC section covers mobile theft?",
                   route='{"route": "DIRECT", "output_format": "chat"}',
                   answer="Section 379 PPC.",
                   sql_params='{"category": null, "subject": null, "section_ref": null, "date": null}',
                   user_profile=None,
                   project_id=None,
                   case_id=None,
                   session_id="11111111-1111-1111-1111-111111111111",
                   user_id="22222222-2222-2222-2222-222222222222",
                   query_similar_exc=None,
                   graph_result=None,
                   agg_result=None,
                   evaluator_relevant=True,
                   generation_exc=None,
                   bm25_pool=None,
                   vector_chunks_fn=None,
                   cross_script_query=None,
                   user_role="investigator"):

        async def fake_call_llm(system_prompt, user_message, **kwargs):
            # Order matters: match the most specific prompt first (the file
            # structurer prompt also mentions "title").
            sp = (system_prompt or "").lower()
            if "structurer" in sp:
                return ('{"title": "Offense Sections", "sections": [{"type": "table", '
                        '"headers": ["Section", "Offense"], "rows": [["379", "Theft"]]}]}')
            if "routing engine" in sp:
                return route
            if "parameter extractor" in sp:
                return sql_params
            if "rewrit" in sp or "search-query" in sp:
                return message
            if "evaluat" in sp:
                if evaluator_relevant:
                    return '{"relevant": true, "reason": "covered"}'
                return '{"relevant": false, "reason": "not relevant"}'
            if "generate a short, descriptive title" in sp:
                return "Mobile Theft Section"
            # Verifier prompt: return a passing grounded verdict.
            # The verifier is matched last so it can't shadow any real prompt.
            if "grounding judge" in sp:
                return (
                    '{"grounded": true, "off_topic": false, '
                    '"leaked_case_id": null, "unsupported_claims": [], '
                    '"reason": "All claims supported."}'
                )
            # Track the last non-special call so tests can inspect the
            # generation prompt (RAG, GRAPH, etc. now use call_llm, not stream_llm).
            fake_call_llm.last_system = system_prompt
            fake_call_llm.last_user = user_message
            fake_call_llm.last_kwargs = kwargs
            if generation_exc is not None:
                raise generation_exc
            return answer

        fake_call_llm.last_system = ""
        fake_call_llm.last_user = ""
        fake_call_llm.last_kwargs = {}

        async def fake_stream_llm(system_prompt, user_message, **kwargs):
            fake_stream_llm.last_system = system_prompt
            fake_stream_llm.last_kwargs = kwargs
            for token in answer.split(" "):
                yield token + " "

        fake_stream_llm.last_system = ""
        fake_stream_llm.last_kwargs = {}

        monkeypatch.setattr(orch, "call_llm", fake_call_llm)
        monkeypatch.setattr(orch, "stream_llm", fake_stream_llm)
        monkeypatch.setattr("src.pipeline.router.call_llm", fake_call_llm)
        monkeypatch.setattr("src.pipeline.query_rewriter.call_llm", fake_call_llm)
        monkeypatch.setattr("src.pipeline.evaluator.call_llm", fake_call_llm)
        monkeypatch.setattr("src.pipeline.title_generator.call_llm", fake_call_llm)
        monkeypatch.setattr("src.pipeline.file_structurer.call_llm", fake_call_llm)
        monkeypatch.setattr("src.pipeline.sql_extractor.call_llm", fake_call_llm)
        monkeypatch.setattr("src.pipeline.verifier.call_llm", fake_call_llm)

        # Retrieval boundaries
        async def fake_embed(_text, **kwargs):
            return [0.1] * 8

        async def fake_expand(_q, n=2):
            return []

        # Fix 3: default None (no cross-script variant) so every pre-existing
        # test's behavior is unaffected — real generate_cross_script_variant()
        # would otherwise reach the real call_llm (network) in a test
        # environment. Individual tests pass cross_script_query= to simulate
        # a real variant being generated.
        async def fake_cross_script_variant(_q):
            return cross_script_query

        chunks = [
            {"id": "c1", "text": "Theft of movable property is punishable under 379 PPC.",
             "metadata": {"source": "PPC.pdf"}, "rrf_score": 0.9}
        ]
        patched_gateway.chunks = chunks

        where_calls = []
        top_k_calls = []

        async def fake_query_similar(_query, _embedding, top_k=10, where=None):
            where_calls.append(where)
            top_k_calls.append(top_k)
            if query_similar_exc is not None:
                raise query_similar_exc
            if vector_chunks_fn is not None:
                return vector_chunks_fn(top_k, where)
            return chunks

        bm25_pool_calls = []

        # Milestone A2: orchestrator.py now sources BM25's candidate pool
        # from fulltext_index.candidate_pool() (imported there as
        # bm25_candidate_pool), not vector_store.get_all_chunks() — same
        # fake behavior (records the `where` scope it was called with,
        # returns the scripted pool), just patched at its new call site so
        # every existing bm25_pool/bm25_pool_calls-based test below keeps
        # working unchanged.
        async def fake_bm25_candidate_pool(_query_text, where=None):
            bm25_pool_calls.append(where)
            return bm25_pool if bm25_pool is not None else chunks

        # get_all_chunks() itself is UNCHANGED by Milestone A2 — it's still
        # called directly (not via fulltext_index) for the unrelated
        # FIR-number auto-scope metadata scan (orchestrator.py ~line 1570),
        # which has nothing to do with BM25/keyword ranking. Faked here the
        # same way it always was (including reusing the same bm25_pool
        # override some auto-scope tests pass), so those tests are
        # unaffected by the bm25_candidate_pool split above.
        async def fake_get_all_chunks(where=None):
            return bm25_pool if bm25_pool is not None else chunks

        monkeypatch.setattr(orch, "embed_text", fake_embed)
        monkeypatch.setattr(orch, "query_similar", fake_query_similar)
        monkeypatch.setattr(orch, "bm25_candidate_pool", fake_bm25_candidate_pool)
        monkeypatch.setattr(orch, "get_all_chunks", fake_get_all_chunks)
        monkeypatch.setattr("src.pipeline.query_expander.expand_query", fake_expand)
        monkeypatch.setattr(
            "src.pipeline.cross_script_variant.generate_cross_script_variant",
            fake_cross_script_variant,
        )

        # Graph retrieval / cross-case aggregate boundaries (Phase 5).
        # Defaults are empty/no-op so tests that don't care about GRAPH/
        # XGRAPH/XAGG (i.e. every pre-Phase-5 test) fall straight through
        # to their existing RAG-fallback behavior, unchanged.
        default_graph_result = {
            "chunks": [], "hop_count": 0, "compounded_confidence": 1.0,
            "seed_entities": [], "unconfirmed_links": [],
        }

        retrieve_graph_calls = []
        run_aggregate_calls = []
        jurisdiction_case_ids_calls = []

        async def fake_retrieve_graph(_query, _target_entity, case_id=None, cross_case=False,
                                       max_hops=2, user_id=None, user_role="investigator",
                                       jurisdiction_case_ids=None):
            retrieve_graph_calls.append(user_role)
            jurisdiction_case_ids_calls.append(jurisdiction_case_ids)
            return graph_result if graph_result is not None else default_graph_result

        async def fake_run_aggregate(_query, _target_entity, _gateway, user_id=None, user_role="investigator",
                                      jurisdiction_case_ids=None):
            run_aggregate_calls.append(user_role)
            jurisdiction_case_ids_calls.append(jurisdiction_case_ids)
            return agg_result if agg_result is not None else {
                "kind": "relational_aggregate", "group_by": "police_station",
                "counts": [], "total_cases_considered": 0,
            }

        monkeypatch.setattr(orch, "retrieve_graph", fake_retrieve_graph)
        monkeypatch.setattr(orch, "run_aggregate", fake_run_aggregate)

        # SQLite audit log — irrelevant here, and it would touch disk.
        # log_retrieved_docs is captured (not just no-op'd) so Fix 2 tests
        # can inspect exactly what `semantic_results` looked like at the
        # point it was logged, without needing to parse the final answer
        # text for case attribution.
        log_retrieved_docs_calls = []

        def _capture_log_retrieved_docs(*a, **k):
            log_retrieved_docs_calls.append((a, k))
            return 1

        monkeypatch.setattr(orch.pipeline_logger, "log_retrieved_docs",
                            _capture_log_retrieved_docs, raising=False)
        for fn in ("upsert_session", "create_query", "log_step", "log_llm_call",
                   "update_retrieved_docs_relevance", "update_query"):
            monkeypatch.setattr(orch.pipeline_logger, fn,
                                lambda *a, **k: 1, raising=False)

        events = []
        async for event in orch.process_query(
            session_id, message, project_id=project_id, case_id=case_id,
            user_profile=user_profile, user_id=user_id, user_role=user_role,
        ):
            events.append(event)

        # [Regression, confirmed live] orchestrator.py's `_spawn(...)` fires
        # pipeline_logger.log_retrieved_docs()/etc. as a genuine
        # fire-and-forget asyncio task (create_task, never awaited) --
        # consuming the async generator above does NOT guarantee those
        # tasks have actually RUN, only that they've been scheduled. A bare
        # `yield` inside an async generator resumed by `async for` doesn't
        # suspend to the event loop by itself; the spawned task only gets a
        # chance to run at a REAL await/I/O suspension point somewhere in
        # process_query() (e.g. cross_rerank()'s network call when
        # RERANKER_URL is configured). Without a reachable RERANKER_URL
        # (e.g. no .env, matching CI) cross_rerank() returns synchronously
        # with no await at all, so the spawned log_retrieved_docs() task
        # never got a chance to run before this fixture returned --
        # `log_retrieved_docs_calls` would still be empty, not because
        # nothing was logged, but because the check ran before the
        # scheduled task did. Explicitly yielding back to the loop a few
        # times drains any pending spawned tasks before any test inspects
        # their side effects.
        for _ in range(5):
            await asyncio.sleep(0)

        # Expose both LLM trackers so individual tests can inspect which
        # system prompt was used on each code path:
        #   _run.stream  — last stream_llm call (DIRECT route, file structurer)
        #   _run.call    — last non-special call_llm call (RAG, GRAPH, SQL, etc.)
        _run.stream = fake_stream_llm
        _run.call = fake_call_llm
        _run.where_calls = where_calls
        _run.top_k_calls = top_k_calls
        _run.bm25_pool_calls = bm25_pool_calls
        _run.log_retrieved_docs_calls = log_retrieved_docs_calls
        _run.retrieve_graph_calls = retrieve_graph_calls
        _run.run_aggregate_calls = run_aggregate_calls
        _run.jurisdiction_case_ids_calls = jurisdiction_case_ids_calls
        return events, patched_gateway

    return _run


def _text_of(events):
    return "".join(e["detail"] for e in events
                   if e["step"] == "response" and e["status"] == "streaming")


# ── Persistence ───────────────────────────────────────────────────────────────

async def test_pipeline_creates_the_session_with_its_owner(run_pipeline):
    events, gateway = await run_pipeline()

    session = await gateway.get_session("11111111-1111-1111-1111-111111111111")
    assert session is not None
    assert session["user_id"] == "22222222-2222-2222-2222-222222222222"


async def test_pipeline_persists_the_exchange(run_pipeline):
    events, gateway = await run_pipeline(message="hello", answer="hi there")

    history = await gateway.get_session_history("11111111-1111-1111-1111-111111111111")
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "hello"
    assert "hi there" in history[1]["content"]
    assert any(e["step"] == "memory" and e["status"] == "done" for e in events)


async def test_pipeline_derives_user_id_from_the_profile(run_pipeline):
    """The chat endpoint passes a profile; user_id must still land on the session."""
    events, gateway = await run_pipeline(
        user_id=None,
        user_profile={"id": "33333333-3333-3333-3333-333333333333",
                      "context_text": "", "preferred_language": "English", "llm_mode": "cloud"},
    )

    session = await gateway.get_session("11111111-1111-1111-1111-111111111111")
    assert session["user_id"] == "33333333-3333-3333-3333-333333333333"


async def test_streaming_tokens_are_not_written_to_the_step_log(run_pipeline):
    """
    Regression: every token was logged as a pipeline step — hundreds of blocking
    writes per answer, stalling the stream.
    """
    events, gateway = await run_pipeline(answer="one two three four five")

    assert not any(s["status"] == "streaming" for s in gateway.steps)


# ── Personalization reaches every path ────────────────────────────────────────

async def test_direct_answers_honour_the_language_setting(run_pipeline):
    """Regression: language/context applied only to the RAG path."""
    # message= is route-neutral on purpose: the fixture's default message
    # ("What PPC section covers mobile theft?") now matches router.py's
    # own deterministic SQL override (added after this test was written),
    # which short-circuits BEFORE the fixture's mocked call_llm is ever
    # consulted — the mocked route='DIRECT' below would otherwise be
    # silently ignored. This test cares about DIRECT-route generation
    # behavior, not routing itself, hence a message the override can't match.
    await run_pipeline(
        message="Tell me about yourself.",
        route='{"route": "DIRECT", "output_format": "chat"}',
        user_profile={"id": "u", "context_text": "I run a textile SME",
                      "preferred_language": "Urdu", "llm_mode": "cloud"},
    )

    system_prompt = run_pipeline.stream.last_system
    assert "Urdu" in system_prompt
    assert "textile SME" in system_prompt


async def test_rag_answers_honour_the_language_setting(run_pipeline):
    # Phase 6: RAG generation now uses call_llm (not stream_llm) because the
    # response is buffered before the verifier gate. Check via run_pipeline.call.
    await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        message="What PPC section covers mobile theft?",
        user_profile={"id": "u", "context_text": "I run a textile SME",
                      "preferred_language": "Urdu", "llm_mode": "cloud"},
    )

    system_prompt = run_pipeline.call.last_system
    assert "Urdu" in system_prompt
    assert "textile SME" in run_pipeline.call.last_user


async def test_llm_mode_setting_is_passed_to_the_client(run_pipeline):
    """The llm_mode setting used to save but never be read by anything."""
    # DIRECT route still uses stream_llm — the mode flag is captured there.
    # message= kept route-neutral — see test_direct_answers_honour_the_language_setting's
    # comment for why the fixture's default message can no longer be used here.
    await run_pipeline(
        message="Tell me about yourself.",
        route='{"route": "DIRECT", "output_format": "chat"}',
        user_profile={"id": "u", "context_text": "", "preferred_language": "English",
                      "llm_mode": "local"},
    )

    assert run_pipeline.stream.last_kwargs.get("llm_mode") == "local"


async def test_project_context_is_injected(run_pipeline, patched_gateway):
    project = await patched_gateway.create_project(
        {"user_id": "u", "name": "Textile Co", "domain_context": "Client exports towels"}
    )
    await patched_gateway.upsert_project_memory(project["id"], "Prior year turnover was 50M")

    # message= kept route-neutral — see test_direct_answers_honour_the_language_setting's
    # comment for why the fixture's default message can no longer be used here.
    await run_pipeline(message="Tell me about yourself.", project_id=project["id"])

    # DIRECT route (the default) uses stream_llm, so project context
    # is captured in run_pipeline.stream.last_system.
    system_prompt = run_pipeline.stream.last_system
    assert "Client exports towels" in system_prompt
    assert "Prior year turnover was 50M" in system_prompt


# ── File generation ───────────────────────────────────────────────────────────

async def test_direct_route_can_still_produce_a_file(run_pipeline):
    """
    Regression: the DIRECT path returned before the file-generation block, so
    "make me a PDF of X" produced no file and no error.
    """
    events, gateway = await run_pipeline(
        route='{"route": "DIRECT", "output_format": "file_xlsx"}',
        message="Make me an excel of the dividend rates",
    )

    done = [e for e in events if e["step"] == "file_generation" and e["status"] == "done"]
    assert done, "DIRECT route produced no file"
    assert gateway.files, "no file record was persisted"

    import os
    for record in gateway.files.values():
        assert record["file_name"].endswith(".xlsx"), "download name must carry its extension"
        os.remove(record["storage_path"])


async def test_generated_file_is_owned_by_the_user(run_pipeline):
    # message= kept route-neutral — see test_direct_answers_honour_the_language_setting's
    # comment for why the fixture's default message can no longer be used here.
    events, gateway = await run_pipeline(
        message="Tell me about yourself.",
        route='{"route": "DIRECT", "output_format": "file_pdf"}',
    )

    import os
    record = next(iter(gateway.files.values()))
    assert record["user_id"] == "22222222-2222-2222-2222-222222222222"
    os.remove(record["storage_path"])


async def test_file_generation_failure_is_surfaced_not_swallowed(run_pipeline, monkeypatch):
    """
    Regression: builder crashes were logged server-side and the user just saw
    an answer with no file and no explanation.
    """
    def _boom(_payload):
        raise RuntimeError("reportlab exploded")

    monkeypatch.setattr(orch, "build_pdf", _boom)

    # message= kept route-neutral — see test_direct_answers_honour_the_language_setting's
    # comment for why the fixture's default message can no longer be used here.
    events, _ = await run_pipeline(
        message="Tell me about yourself.",
        route='{"route": "DIRECT", "output_format": "file_pdf"}',
    )

    errors = [e for e in events if e["step"] == "file_generation" and e["status"] == "error"]
    assert errors, "file generation failed silently"
    assert "reportlab exploded" in errors[0]["detail"]


# ── Routing ───────────────────────────────────────────────────────────────────

async def test_rag_route_retrieves_and_answers(run_pipeline):
    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        message="What PPC section covers mobile theft?",
        answer="Fifteen percent.",
    )

    steps = {e["step"] for e in events}
    assert {"retrieval", "reranker", "evaluator", "response"} <= steps
    assert "Fifteen" in _text_of(events)


async def test_rag_retrieval_failure_degrades_to_safe_response(run_pipeline):
    """
    A retrieval-infrastructure failure (e.g. a ChromaDB query error) must
    degrade to the safe response, NOT abort the whole request as a hard
    "Chat pipeline error". Regression guard for the previously-unguarded
    retrieval stage (audit finding A9): process_query must not raise, must
    emit a retrieval 'error' event, and must still yield the safe answer.
    """
    class _ChromaInternalError(Exception):
        pass

    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        message="What documents are required for a certified copy of an FIR?",
        query_similar_exc=_ChromaInternalError(
            "Error executing plan: Internal error: Error finding id"
        ),
    )

    # The pipeline completed (did not raise) and reported the failure...
    assert any(e["step"] == "retrieval" and e["status"] == "error" for e in events)
    # ...and still produced the safe response instead of crashing.
    assert orch._SAFE_RESPONSE.split(" ")[0] in _text_of(events)
    # It must NOT have gone on to re-rank / evaluate on non-existent results.
    assert not any(e["step"] == "reranker" for e in events)


async def test_rag_generation_failure_degrades_to_safe_response(run_pipeline):
    """
    Module 6.2: the RAG route's final generation/verification call had no
    exception guard — unlike every sibling route (SQL/WEB/GRAPH/GRAPH_HYBRID),
    which catch failures and fall back to RAG. RAG itself has nowhere further
    to fall back to, so a generation failure (e.g. an LLM call erroring out)
    must degrade to the safe response instead of raising out of process_query.
    """
    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        message="What documents are required for a certified copy of an FIR?",
        generation_exc=RuntimeError("LLM call failed"),
    )

    assert any(e["step"] == "response" and e["status"] == "error" for e in events)
    assert orch._SAFE_RESPONSE.split(" ")[0] in _text_of(events)
    # It must have gotten past retrieval/evaluation before failing here.
    assert any(e["step"] == "evaluator" and e["status"] == "done" for e in events)


async def test_rag_case_scoped_query_filters_on_case_id_alone(run_pipeline):
    """
    Module 4.1: a case-scoped query with no project_id must filter on
    case_id alone, never fall back to is_global=True ANDed with case_id —
    real case evidence is ingested with is_global=False, so the old
    fallback structurally excluded it from its own case's retrieval.
    """
    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        case_id="CASE-014",
    )
    assert run_pipeline.where_calls, "query_similar was never called"
    for where in run_pipeline.where_calls:
        assert where == {"case_id": "CASE-014"}


async def test_rag_no_case_or_project_falls_back_to_is_global(run_pipeline):
    """Pre-Phase-1 behavior preserved: no case_id and no project_id still means is_global=True."""
    events, _ = await run_pipeline(route='{"route": "RAG", "output_format": "chat"}')
    assert run_pipeline.where_calls, "query_similar was never called"
    for where in run_pipeline.where_calls:
        assert where == {"is_global": True}


async def test_rag_project_scoped_query_unaffected_by_case_fix(run_pipeline):
    """A project-scoped query (no case_id) keeps its existing project_id filter."""
    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        project_id="11111111-1111-1111-1111-111111111111",
    )
    assert run_pipeline.where_calls, "query_similar was never called"
    for where in run_pipeline.where_calls:
        assert where == {"project_id": "11111111-1111-1111-1111-111111111111"}


async def test_fir_number_in_query_auto_scopes_retrieval_even_with_no_case_active(run_pipeline):
    """
    2026-08-03 fix: a query naming an explicit FIR number (e.g. "Trace the
    full case history for FIR-2026-THEFT-001...") must auto-scope retrieval
    to that case even when no case is selected in the UI — otherwise the
    query is diluted across the entire unscoped corpus and the case's own
    documents can lose the fusion/rerank cut to unrelated cases entirely
    (confirmed live against the real corpus).
    """
    bm25_pool = [
        {"id": "other-case", "text": "unrelated theft case",
         "metadata": {"source": "FIR-2026-THEFT-011.pdf", "case_id": "CASE-B0-THEFT-011"}},
        {"id": "target-case", "text": "Complaint text naming the actual case.",
         "metadata": {"source": "FIR-2026-THEFT-001.pdf", "case_id": "CASE-B0-THEFT-001"}},
    ]
    events, _ = await run_pipeline(
        message="Trace the full case history for FIR-2026-THEFT-001 from the initial complaint through to the charge sheet.",
        route='{"route": "RAG", "output_format": "chat"}',
        case_id=None,
        bm25_pool=bm25_pool,
    )
    assert run_pipeline.where_calls, "query_similar was never called"
    for where in run_pipeline.where_calls:
        assert where == {"case_id": "CASE-B0-THEFT-001"}, (
            f"expected the FIR number in the query to auto-scope retrieval "
            f"to its case, got {where}"
        )


async def test_real_fir_display_code_in_query_auto_scopes_via_dedicated_metadata_field(run_pipeline):
    """
    M11 (Muhafiz Data API migration, docs/decisions/0001-muhafiz-api-migration.md):
    a real FIR display code ("891/24") never matches extract_fir_numbers()'s
    synthetic-only shape, AND the substring-against-`source` trick above
    can't find one even if extracted (API-sourced `source` is the slug id,
    "psrms/fir/fir-891-24#narrative", not the human-readable code a user
    types) — auto-scope now also checks the dedicated fir_display_code
    chunk metadata field (src/ingestion/muhafiz_records.py) for exactly
    this case.
    """
    bm25_pool = [
        {"id": "other-fir", "text": "unrelated FIR",
         "metadata": {"source": "psrms/fir/fir-201-26#narrative", "case_id": "fir-201-26",
                      "fir_display_code": "201/26"}},
        {"id": "target-fir", "text": "Narrative naming the actual case.",
         "metadata": {"source": "psrms/fir/fir-891-24#narrative", "case_id": "fir-891-24",
                      "fir_display_code": "891/24"}},
    ]
    events, _ = await run_pipeline(
        message="Trace the full case history for FIR 891/24 from the initial complaint through to the charge sheet.",
        route='{"route": "RAG", "output_format": "chat"}',
        case_id=None,
        bm25_pool=bm25_pool,
    )
    assert run_pipeline.where_calls, "query_similar was never called"
    for where in run_pipeline.where_calls:
        assert where == {"case_id": "fir-891-24"}, (
            f"expected the real FIR display code in the query to auto-scope "
            f"retrieval to its case, got {where}"
        )


async def test_no_fir_number_in_query_leaves_case_scoping_untouched(run_pipeline):
    """
    Regression guard: a query with no FIR-number-shaped identifier must not
    trigger the auto-scope lookup at all — no case_id is invented out of
    thin air, and behavior for a genuinely unscoped query is unchanged.
    """
    events, _ = await run_pipeline(
        message="What is the procedure to get a certified copy of an FIR?",
        route='{"route": "RAG", "output_format": "chat"}',
        case_id=None,
    )
    assert run_pipeline.where_calls, "query_similar was never called"
    for where in run_pipeline.where_calls:
        assert where == {"is_global": True}


async def test_fir_number_auto_scope_never_overrides_an_already_active_case(run_pipeline):
    """
    Regression guard: if a case IS already active in the UI, a FIR number
    mentioned in the query text (e.g. referring to a different case) must
    never override the user's explicit selection.
    """
    bm25_pool = [
        {"id": "other-case", "text": "a different case entirely",
         "metadata": {"source": "FIR-2026-THEFT-001.pdf", "case_id": "CASE-B0-THEFT-001"}},
    ]
    events, _ = await run_pipeline(
        message="Trace the full case history for FIR-2026-THEFT-001 from the initial complaint through to the charge sheet.",
        route='{"route": "RAG", "output_format": "chat"}',
        case_id="CASE-014",
        bm25_pool=bm25_pool,
    )
    assert run_pipeline.where_calls, "query_similar was never called"
    for where in run_pipeline.where_calls:
        assert where == {"case_id": "CASE-014"}


# ── BM25 full-corpus pool (RETRIEVAL_DIVERSITY_FIX_PROMPT.md, Fix 1) ───────────

async def test_bm25_pool_is_fetched_with_the_same_scope_as_vector_search(run_pipeline):
    """
    Fix 1: BM25 must search the full scoped corpus, not just
    `semantic_results`. The pool fetch must still be scoped by the exact
    same project/case/is_global filter query_similar uses — widening BM25's
    pool must never widen its access-control scope.
    """
    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        case_id="CASE-014",
    )
    assert run_pipeline.bm25_pool_calls, "get_all_chunks (BM25's full-corpus pool) was never called"
    for where in run_pipeline.bm25_pool_calls:
        assert where == {"case_id": "CASE-014"}
    # And it must match query_similar's scope call-for-call.
    assert run_pipeline.bm25_pool_calls == run_pipeline.where_calls


async def test_bm25_surfaces_a_keyword_match_vector_search_missed(run_pipeline):
    """
    The actual bug this fix targets: a chunk that vector search's top-k
    never returned (it's not in `semantic_results`) but that lexically
    matches the query must still be able to surface in the final grounded
    answer, because BM25 now searches the full corpus pool
    (`get_all_chunks`), not just what semantic search already found.

    Before Fix 1, `retrieve_bm25` was called with `semantic_results` as its
    candidate set — a chunk absent from that list could never be scored by
    BM25 at all, no matter how strong the keyword match.
    """
    vector_missed_chunk = {
        "id": "vector-missed-1",
        "text": "Falcon-Nine-Zulu evidence locker mobile phone recovered from the accused",
        "metadata": {"source": "supplementary.pdf"},
    }
    # semantic_results (what query_similar returns) stays the default single
    # chunk — `vector_missed_chunk` is ONLY present in the full BM25 pool,
    # simulating a chunk vector search's top-k never surfaced. The pool has
    # a few unrelated docs alongside it (not just a 2-doc corpus) so BM25's
    # IDF for the matching terms doesn't clamp to zero on a too-tiny corpus
    # (see test_bm25_contributes_nothing_on_a_tiny_corpus in
    # test_retrieval_and_memory.py for that quirk).
    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        message="Falcon-Nine-Zulu evidence locker mobile phone",
        bm25_pool=[
            {"id": "c1", "text": "Theft of movable property is punishable under 379 PPC.",
             "metadata": {"source": "PPC.pdf"}},
            vector_missed_chunk,
            {"id": "c3", "text": "FIR registration threshold requires an original CNIC for verification",
             "metadata": {"source": "sop.pdf"}},
            {"id": "c4", "text": "Penalty for late filing of an FIR complaint under section 182 of the code",
             "metadata": {"source": "sop.pdf"}},
        ],
    )

    generation_prompt = run_pipeline.call.last_system + run_pipeline.call.last_user
    assert "Falcon-Nine-Zulu" in generation_prompt, (
        "a chunk absent from semantic_results but present in the full BM25 "
        "pool must still reach the generation prompt via RRF fusion"
    )


async def test_graph_hybrid_case_scoped_query_filters_on_case_id_alone(run_pipeline):
    """Same Module 4.1 fix, at the GRAPH_HYBRID route's separate where_clause site."""
    events, _ = await run_pipeline(
        route='{"route": "GRAPH_HYBRID", "case_scope": "within_case", "target_entity": null, "output_format": "chat"}',
        case_id="CASE-014",
    )
    assert run_pipeline.where_calls, "query_similar was never called"
    for where in run_pipeline.where_calls:
        assert where == {"case_id": "CASE-014"}


# ── Cross-case diversity in vector retrieval (RETRIEVAL_DIVERSITY_FIX_PROMPT.md, Fix 2) ──

def _semantic_results_from(log_calls):
    """Pull the `semantic_results` list logged for the "semantic" stage out
    of captured pipeline_logger.log_retrieved_docs(...) calls — see
    orchestrator.py's `_spawn(... pipeline_logger.log_retrieved_docs(query_id,
    semantic_results, "semantic", ...))` call site."""
    for args, _kwargs in log_calls:
        if len(args) >= 3 and args[2] == "semantic":
            return args[1]
    return None


async def test_cross_case_query_widens_the_vector_fetch(run_pipeline, monkeypatch):
    """
    Fix 2: a query with no case_id filter (more than one case could
    legitimately match) must fetch a wider candidate pool from vector
    search than TOP_K_RETRIEVAL — the over-fetch that makes room for a
    second/third case's chunks to be seen at all, before diversity capping
    trims back down. See orchestrator.py's `is_cross_case` /
    `fetch_top_k` logic and config.CROSS_CASE_RETRIEVAL_MULTIPLIER.
    """
    from src import config
    monkeypatch.setattr(config, "TOP_K_RETRIEVAL", 4)
    monkeypatch.setattr(config, "CROSS_CASE_RETRIEVAL_MULTIPLIER", 3)

    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        case_id=None, project_id=None,
    )
    assert run_pipeline.top_k_calls, "query_similar was never called"
    assert all(k == 12 for k in run_pipeline.top_k_calls), (
        f"expected every unscoped-query fetch to widen to "
        f"TOP_K_RETRIEVAL(4) * CROSS_CASE_RETRIEVAL_MULTIPLIER(3) = 12, "
        f"got {run_pipeline.top_k_calls}"
    )


async def test_case_scoped_query_fetch_is_unchanged_by_the_diversity_fix(run_pipeline, monkeypatch):
    """
    Regression guard: a query already scoped to a single case via
    where_clause's case_id must behave EXACTLY as it did before Fix 2 — it
    fetches exactly TOP_K_RETRIEVAL from vector search (no over-fetch), and
    the diversity cap never runs (nothing to diversify across within one
    case's own evidence).
    """
    from src import config
    monkeypatch.setattr(config, "TOP_K_RETRIEVAL", 4)
    monkeypatch.setattr(config, "CROSS_CASE_RETRIEVAL_MULTIPLIER", 3)

    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        case_id="CASE-014",
    )
    assert run_pipeline.top_k_calls, "query_similar was never called"
    assert all(k == 4 for k in run_pipeline.top_k_calls), (
        "a case-scoped query must fetch exactly TOP_K_RETRIEVAL, unaffected "
        "by the cross-case over-fetch multiplier"
    )


@pytest.mark.xfail(
    reason=(
        "Test-design race, not a product defect. This asserts on "
        "pipeline_logger.log_retrieved_docs(...) calls, but orchestrator.py fires "
        "those via _spawn(asyncio.to_thread(...)) — fire-and-forget, never awaited "
        "— so the assertion runs before the background thread records anything and "
        "_semantic_results_from() returns None. Deterministic (fails 5/5 in "
        "isolation), and confirmed pre-existing on main, independent of the agent "
        "harness work. The sibling test above passes because it asserts on "
        "run_pipeline.top_k_calls, which is captured synchronously.\n\n"
        "NOT fixed here deliberately: the observable it depends on is "
        "pipeline_logger — the SQLite side-log that docs/AGENT_HARNESS_DESIGN.md §6 "
        "already schedules for removal (confirmed zero live readers). Rewriting this "
        "test against that component means writing code against something being "
        "deleted. Re-point it at a synchronous signal, or drop it, when §6 lands.\n\n"
        "The cross-case diversity-cap behaviour it targets is NOT known to be broken "
        "— it is merely unobservable by this test."
    ),
    strict=False,
)
async def test_cross_case_query_result_set_includes_more_than_one_case(run_pipeline, monkeypatch):
    """
    The actual bug this fix targets: for an unscoped (cross-case) query,
    simulate a case (CASE-A) whose chunks are nearest in embedding space
    and would fill the entire narrow top-k window, plus a second case
    (CASE-B) with one relevant chunk that only shows up once the fetch
    widens past the narrow window. Before Fix 2, CASE-B's chunk would never
    even be fetched (top_k=4 only ever returns CASE-A's 4 nearest chunks) —
    after Fix 2, the wider fetch plus per-case cap must let CASE-B survive
    into `semantic_results`.
    """
    from src import config
    monkeypatch.setattr(config, "TOP_K_RETRIEVAL", 4)
    monkeypatch.setattr(config, "CROSS_CASE_RETRIEVAL_MULTIPLIER", 3)
    monkeypatch.setattr(config, "CROSS_CASE_PER_CASE_CAP", 2)

    case_a_chunks = [
        {"id": f"a{i}", "text": f"case A chunk {i}",
         "metadata": {"case_id": "CASE-A"}, "rrf_score": 1.0 - i * 0.01}
        for i in range(9)
    ]
    case_b_chunk = {
        "id": "b1", "text": "case B chunk",
        "metadata": {"case_id": "CASE-B"}, "rrf_score": 0.5,
    }
    # Nearest-neighbor order: all 9 of CASE-A's chunks rank ahead of
    # CASE-B's single chunk. A plain top_k=4 slice (pre-Fix-2 behavior)
    # would be 100% CASE-A. Only the widened top_k=12 fetch reaches index 9
    # and surfaces CASE-B at all.
    full_pool = case_a_chunks + [case_b_chunk]

    def vector_chunks_fn(top_k, _where):
        return full_pool[:top_k]

    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        case_id=None, project_id=None,
        vector_chunks_fn=vector_chunks_fn,
    )

    semantic_results = _semantic_results_from(run_pipeline.log_retrieved_docs_calls)
    assert semantic_results is not None, "no 'semantic' stage was logged"
    case_ids = {c["metadata"]["case_id"] for c in semantic_results}
    assert case_ids == {"CASE-A", "CASE-B"}, (
        f"expected the diversity-capped semantic pool to include both "
        f"cases, got {case_ids}"
    )
    # And CASE-A must not have been allowed to occupy the whole window —
    # the per-case cap (2) must have been applied.
    case_a_count = sum(1 for c in semantic_results if c["metadata"]["case_id"] == "CASE-A")
    assert case_a_count <= 2, f"CASE-A should be capped at 2 chunks, got {case_a_count}"


# ── Cross-script retrieval variant (RETRIEVAL_CROSS_LINGUAL_FIX_PROMPT.md, Fix 3) ──

async def test_cross_script_variant_none_leaves_behavior_unchanged(run_pipeline):
    """
    Regression guard: when generate_cross_script_variant() returns None
    (its failure/no-op contract — see cross_script_variant.py), the pipeline
    must behave exactly as it did before Fix 3 existed. This is the fixture's
    default (cross_script_query=None), so every pre-Fix-3 test in this file
    is itself already a regression guard for this — this test just makes
    that explicit.
    """
    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
    )
    assert any(e["step"] == "response" and e["status"] == "done" for e in events)


async def test_cross_script_variant_widens_the_bm25_pool(run_pipeline):
    """
    The actual bug this fix targets: a chunk lexically matchable only via
    the cross-script variant's vocabulary (simulating a same-script token
    the original-language query never used) must still be able to surface
    in the final grounded answer — because the variant is folded into
    `all_queries`, which feeds BOTH the embedding step and BM25's
    `combined_query` (see orchestrator.py's RAG route, just after
    `expand_query`).

    The message itself never mentions "Falcon-Nine-Zulu" — only the
    (mocked) cross-script variant does, standing in for a real translation
    that would carry an identifier through verbatim per
    prompts/cross_script_query.txt's rule 1. Before Fix 3, nothing in
    `all_queries` would contain that token, so BM25 could never match
    this chunk at all, no matter how strong the keyword.
    """
    vector_missed_chunk = {
        "id": "cross-script-missed-1",
        "text": "Falcon-Nine-Zulu evidence locker mobile phone recovered from the accused",
        "metadata": {"source": "supplementary.pdf"},
    }
    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        message="What happened in this case?",
        cross_script_query="Falcon-Nine-Zulu evidence locker mobile phone",
        bm25_pool=[
            {"id": "c1", "text": "Theft of movable property is punishable under 379 PPC.",
             "metadata": {"source": "PPC.pdf"}},
            vector_missed_chunk,
            {"id": "c3", "text": "FIR registration threshold requires an original CNIC for verification",
             "metadata": {"source": "sop.pdf"}},
            {"id": "c4", "text": "Penalty for late filing of an FIR complaint under section 182 of the code",
             "metadata": {"source": "sop.pdf"}},
        ],
    )

    generation_prompt = run_pipeline.call.last_system + run_pipeline.call.last_user
    assert "Falcon-Nine-Zulu" in generation_prompt, (
        "a chunk matchable only via the cross-script variant's vocabulary "
        "must still reach the generation prompt via RRF fusion, since the "
        "variant is folded into all_queries / BM25's combined_query"
    )


async def test_greeting_short_circuits_retrieval(run_pipeline):
    """Greetings must not pay for retrieval — it's pure latency."""
    events, _ = await run_pipeline(message="hello", answer="Hi! How can I help?")

    skipped = {e["step"] for e in events if e["status"] == "skipped"}
    assert "retrieval" in skipped


# ── SQL route (Phase 3: police_reference_data direct fast path) ────────────────

async def test_sql_route_answers_from_direct_query_on_match(run_pipeline, patched_gateway):
    """
    A structured-data match must be answered from the direct SQL fast
    path (gateway.query_police_reference_data), not MCP — MCP is the
    separately-demonstrated path (POST /api/admin/mcp-demo), not the
    default mechanism for every SQL-routed question.
    """
    patched_gateway.police_reference_data = [{
        "ref_id": "r1", "category": "penal_code", "subject": "Mobile/Vehicle Theft",
        "description": "Theft of movable property.", "fine_amount": None,
        "section_ref": "379 PPC", "source_document": "offense_sections.csv",
        "source_type": "synthetic", "effective_from": None,
    }]

    events, _ = await run_pipeline(
        route='{"route": "SQL", "output_format": "chat"}',
        message="What PPC section applies to mobile phone theft?",
        sql_params='{"category": "penal_code", "subject": "Mobile/Vehicle Theft", "section_ref": null, "date": null}',
        answer="That falls under 379 PPC.",
    )

    assert "379 PPC" in _text_of(events)
    assert not any(e["step"] == "retrieval" and "Falling back to RAG" in (e.get("detail") or "") for e in events)


async def test_sql_route_falls_back_to_rag_on_no_match(run_pipeline, patched_gateway):
    """
    The named fallback contract from the old tax_rates branch: no
    structured row match → RAG, not an error and not a dead end.
    """
    patched_gateway.police_reference_data = []  # nothing seeded — no match possible

    events, _ = await run_pipeline(
        route='{"route": "SQL", "output_format": "chat"}',
        message="What section covers arson?",
        sql_params='{"category": "penal_code", "subject": "Arson", "section_ref": null, "date": null}',
        answer="Arson is covered under PPC Section 435.",
    )

    fallback_events = [e for e in events if e["step"] == "retrieval" and "Falling back to RAG" in (e.get("detail") or "")]
    assert fallback_events, "no-match must fall back to RAG, same as the old tax_rates behavior"

    steps = {e["step"] for e in events}
    assert {"reranker", "evaluator"} <= steps, "fallback must actually run the RAG path, not just log the intent"
    assert "PPC Section 435" in _text_of(events)


# ── GRAPH / GRAPH_HYBRID / XGRAPH / XAGG routes (Phase 5) ──────────────────────

def _graph_chunk(chunk_id="g1", hop=1, confidence=0.81, case_id="CASE-009"):
    return {
        "id": chunk_id,
        "text": "Waqas is named as a known associate of the accused.",
        "metadata": {"source": "case_diary.pdf", "case_id": case_id},
        "rrf_score": confidence,
        "hop": hop,
        "graph_confidence": confidence,
        "via_entity": "Waqas Ali Niazi",
    }


async def test_graph_route_answers_with_hop_and_confidence_surfaced(run_pipeline):
    """
    A GRAPH-routed query must run through the same cross_rerank ->
    evaluate_relevance -> generation gate as RAG, and the hop count /
    compounded confidence must be surfaced on the retrieval event (spec:
    "confidence degradation is surfaced, not hidden"), not dropped.
    """
    events, _ = await run_pipeline(
        route='{"route": "GRAPH", "case_scope": "within_case", "target_entity": "the accused", "output_format": "chat"}',
        message="Who is connected to the accused in CASE-009?",
        case_id="CASE-009",
        answer="Waqas Ali Niazi is a known associate of the accused.",
        graph_result={
            "chunks": [_graph_chunk(hop=2, confidence=0.81)],
            "hop_count": 2, "compounded_confidence": 0.81,
            "seed_entities": [{"entity_id": "P-002", "type": "Person", "name": "Waqas Ali Niazi"}],
            "unconfirmed_links": [],
        },
    )

    steps = {e["step"] for e in events}
    assert {"retrieval", "cross_reranker", "evaluator", "response"} <= steps
    assert "Waqas" in _text_of(events)

    retrieval_done = next(e for e in events if e["step"] == "retrieval" and e["status"] == "done")
    assert retrieval_done["hop_count"] == 2
    assert retrieval_done["graph_confidence"] == 0.81


async def test_graph_route_falls_back_to_rag_when_traversal_finds_nothing(run_pipeline):
    """No connected graph evidence must degrade to the ordinary RAG path, not error out."""
    events, _ = await run_pipeline(
        route='{"route": "GRAPH", "case_scope": "within_case", "target_entity": "nobody", "output_format": "chat"}',
        message="Who is connected to nobody in CASE-009?",
        case_id="CASE-009",
        graph_result={"chunks": [], "hop_count": 0, "compounded_confidence": 1.0,
                      "seed_entities": [], "unconfirmed_links": []},
    )

    fallback = [e for e in events if e["step"] == "retrieval" and "Falling back to RAG" in (e.get("detail") or "")]
    assert fallback
    steps = {e["step"] for e in events}
    assert {"reranker", "evaluator"} <= steps


# ── Module 3 regression (findings.md): enumeration / list-synthesis refusal ──

_MODULE_3_REAL_NAMES = ["طارق محمود", "شعیب ارشد", "ذیشان بٹ", "محمد اسلم", "حمزہ طارق"]


def _enumeration_graph_result(case_id="fir-233-26"):
    """
    Mirrors fir-233-26's real graph_result shape, live-traced in findings.md
    Module 3: 5 distinct real-person chunks, plus 6 byte-identical
    placeholder-officer chunks (one placeholder name written as 7 distinct
    graph nodes — a real, separate structured_projection.py/entity-
    resolution oddity, out of scope here). Used to guard both fixes: the
    response-generation fix (documents must actually reach the LLM) and
    the dedupe/rerank-budget fix (the 6 duplicates must not crowd the 5
    real chunks out of the reranked set).
    """
    chunks = [
        {"id": f"dup{i}", "text": "(نامزد ASI) appears in fir_structured record fir-233-26.",
         "metadata": {"source": "psrms/fir/fir-233-26#structured", "case_id": case_id},
         "rrf_score": 1.0}
        for i in range(6)
    ] + [
        {"id": f"real{i}", "text": f"{name} appears in fir_structured record fir-233-26.",
         "metadata": {"source": "psrms/fir/fir-233-26#structured", "case_id": case_id},
         "rrf_score": 1.0}
        for i, name in enumerate(_MODULE_3_REAL_NAMES)
    ]
    return {
        "chunks": chunks, "hop_count": 0, "compounded_confidence": 1.0,
        "seed_entities": [
            {"entity_id": f"P-{i}", "type": "Person", "name": n}
            for i, n in enumerate(_MODULE_3_REAL_NAMES)
        ],
        "unconfirmed_links": [],
    }


async def test_graph_route_enumeration_evidence_reaches_the_llm(run_pipeline):
    """
    Before this fix, GRAPH's response-generation step built system_prompt =
    _FINAL_PROMPT_TEMPLATE.format(documents=documents_text, ...) — a
    format() call whose kwargs final_response.txt's template no longer has
    placeholders for (silently dropped, no error, since the RAG route was
    already migrated to carry documents in the USER turn instead) — and
    then sent the BARE user_message (just the question, no evidence at
    all) as the LLM's user turn. The generation LLM received zero evidence
    and hallucinated a fabricated answer, which the Verifier then
    (correctly) rejected. Live-confirmed (trace_module3.py) fabricated
    output: "Ahmad Khan", "Sara Bibi", "Mohammad Ali" — none of them real.

    Assert the real user turn sent to call_llm() now actually carries the
    retrieved evidence, and that none of the 5 real people were crowded
    out by the 6 byte-identical placeholder chunks at the rerank cut.
    """
    events, _ = await run_pipeline(
        route='{"route": "GRAPH", "case_scope": "within_case", "target_entity": null, "output_format": "chat"}',
        message="List every person mentioned in this case file.",
        case_id="fir-233-26",
        graph_result=_enumeration_graph_result(),
    )

    steps = {e["step"] for e in events}
    assert "response" in steps

    user_turn = run_pipeline.call.last_user
    assert "PROVIDED DOCUMENTS" in user_turn
    for name in _MODULE_3_REAL_NAMES:
        assert name in user_turn, f"{name} missing from the evidence reaching the LLM"

    # The max_tokens headroom bump (a genuinely correct, complete
    # enumeration answer was confirmed live to occasionally truncate
    # mid-sentence under the old 1000-token default).
    assert run_pipeline.call.last_kwargs.get("max_tokens") == orch._GRAPH_ANSWER_MAX_TOKENS


def test_dedupe_chunks_by_text_collapses_exact_duplicates():
    """
    6 byte-identical placeholder chunks (the real fir-233-26 shape — one
    placeholder officer name written as 7 distinct graph nodes) collapse
    to 1; distinct chunks are untouched; first-occurrence order is
    preserved.
    """
    chunks = [
        {"id": f"dup{i}", "text": "(نامزد ASI) appears in fir_structured record fir-233-26."}
        for i in range(6)
    ] + [
        {"id": "real1", "text": "طارق محمود appears in fir_structured record fir-233-26."},
        {"id": "real2", "text": "شعیب ارشد appears in fir_structured record fir-233-26."},
    ]
    deduped = orch._dedupe_chunks_by_text(chunks)
    assert [c["id"] for c in deduped] == ["dup0", "real1", "real2"]


async def test_graph_hybrid_merges_graph_and_vector_chunks(run_pipeline):
    events, _ = await run_pipeline(
        route='{"route": "GRAPH_HYBRID", "case_scope": "within_case", "target_entity": null, "output_format": "chat"}',
        message="Tell me everything about this case, including who's connected.",
        case_id="CASE-009",
        answer="Here is the full picture of the case.",
        graph_result={
            "chunks": [_graph_chunk()],
            "hop_count": 1, "compounded_confidence": 0.9,
            "seed_entities": [], "unconfirmed_links": [],
        },
    )

    steps = {e["step"] for e in events}
    assert {"retrieval", "reranker", "cross_reranker", "evaluator", "response"} <= steps
    retrieval_done = next(e for e in events if e["step"] == "retrieval" and e["status"] == "done")
    assert retrieval_done["hop_count"] == 1
    assert "full picture" in _text_of(events)


async def test_xgraph_is_labeled_cross_case_and_never_falls_back_to_rag(run_pipeline):
    """
    Structural separation: an XGRAPH finding must be tagged cross_case_finding
    and must NOT fall back into the ordinary RAG path even if something in
    its own generation step were to fail — cross-case evidence must never be
    blended into a case-scoped answer stream.
    """
    events, _ = await run_pipeline(
        route='{"route": "XGRAPH", "case_scope": "cross_case", "target_entity": "0372-1590538", "output_format": "chat"}',
        message="Has phone 0372-1590538 appeared in other cases?",
        case_id="CASE-004",
        answer="This phone number also appears in CASE-005 and CASE-006.",
        graph_result={
            "chunks": [_graph_chunk(chunk_id="g2", case_id="CASE-005"), _graph_chunk(chunk_id="g3", case_id="CASE-006")],
            "hop_count": 1, "compounded_confidence": 0.95,
            "seed_entities": [{"entity_id": "PH-001", "type": "PhoneNumber", "name": "0372-1590538"}],
            "unconfirmed_links": [],
        },
    )

    cross_case_events = [e for e in events if e["step"] == "cross_case_finding"]
    assert cross_case_events
    assert any(e.get("case_scope") == "cross_case" for e in cross_case_events)
    assert not any(e["step"] == "retrieval" for e in events), "XGRAPH must never run the case-scoped retrieval path"
    assert "CASE-005" in _text_of(events) or "CASE-006" in _text_of(events)
    # Gap 4 regression guard: hop_count=1 here is a REAL traversed relationship
    # (phone -> CASE-005/006), so the "no relationship found" caveat must NOT
    # be injected — only the hop_count=0, independently-listed case (below)
    # gets it.
    assert "No relationship/connection edges were found" not in run_pipeline.call.last_system


async def test_xgraph_enumeration_result_warns_against_inventing_relationships(run_pipeline):
    """
    Gap 4: found live immediately after the Gap 3 enumeration fix — a
    hop_count=0 result (every entity seeded independently, no traversed
    connection between any of them, exactly what "list of all people
    mentioned in the cases" now correctly returns) was fed to the
    generation model with no signal that these entities are NOT related,
    and it invented relationship-sounding language between them. The
    Verifier caught it ("unconfirmed relationship assertions... lack
    direct support") and the whole answer got replaced with an abstention.
    The system prompt must carry an explicit caveat whenever hop_count=0
    with real chunks present, so the model never has room to invent a
    connection the evidence never showed.
    """
    events, _ = await run_pipeline(
        route='{"route": "XGRAPH", "case_scope": "cross_case", "target_entity": null, "output_format": "chat"}',
        message="List all the people mentioned in the cases.",
        graph_result={
            "chunks": [
                _graph_chunk(chunk_id="g10", case_id="CASE-700"),
                _graph_chunk(chunk_id="g11", case_id="CASE-701"),
            ],
            "hop_count": 0, "compounded_confidence": 1.0,
            "seed_entities": [
                {"entity_id": "P-700", "type": "Person", "name": "Waqas Ali Niazi"},
                {"entity_id": "P-701", "type": "Person", "name": "Bilal Shahzad"},
            ],
            "unconfirmed_links": [],
        },
    )

    assert events  # sanity — pipeline actually ran
    assert "No relationship/connection edges were found" in run_pipeline.call.last_system


async def test_xgraph_surfaces_unconfirmed_same_as_caveat(run_pipeline):
    """
    The P-006 flagship requirement: a flagged/pending SAME_AS link must be
    fed to generation as an explicit caveat, never silently dropped and
    never presented as a confirmed fact.
    """
    events, _ = await run_pipeline(
        route='{"route": "XGRAPH", "case_scope": "cross_case", "target_entity": "P-006", "output_format": "chat"}',
        message="Is this repeat fraud offender connected to any other case?",
        case_id="CASE-015",
        answer="There is a possible but UNCONFIRMED link to CASE-016 pending investigator review.",
        graph_result={
            "chunks": [],
            "hop_count": 0, "compounded_confidence": 1.0,
            "seed_entities": [{"entity_id": "P-006", "type": "Person", "name": "Adnan Qureshi Waheed"}],
            "unconfirmed_links": [{
                "entity": "Adnan Qureshi Waheed", "candidate": "Adnan Qureshi (CASE-016 mention)",
                "tier": "flagged_unverified", "confidence": 0.55, "status": "pending",
            }],
        },
    )

    cross_case_done = next(e for e in events if e["step"] == "cross_case_finding" and e["status"] == "done")
    assert cross_case_done["unconfirmed_links"], "unconfirmed SAME_AS links must reach the response envelope"
    assert "UNCONFIRMED" in _text_of(events)


async def test_xgraph_with_no_connections_still_emits_a_response_event(run_pipeline):
    """
    Bug found live: when an XGRAPH search finds zero chunks AND zero
    unconfirmed links (the default fixture graph_result — no override
    needed, this is exactly what a real "nothing found" search returns),
    the route used to set `final_response` locally and yield
    "cross_case_finding done" but NEVER yield a "response" event at all —
    every other branch in orchestrator.py follows a final_response
    assignment with `response`/streaming+done events; this one skipped
    both. The pipeline trace looked like it completed ("Memory: Saved to
    session" checked) but the chat bubble stayed completely empty, because
    nothing ever told the frontend what final_response was.
    """
    events, _ = await run_pipeline(
        route='{"route": "XGRAPH", "case_scope": "cross_case", "target_entity": null, "output_format": "chat"}',
        message="List all the people mentioned in the cases.",
        # graph_result deliberately omitted — the fixture's default is
        # exactly {"chunks": [], ..., "unconfirmed_links": []}, the shape
        # that triggers the buggy branch.
    )

    cross_case_done = next(e for e in events if e["step"] == "cross_case_finding" and e["status"] == "done")
    assert cross_case_done["detail"] == "No cross-case connections found."

    response_events = [e for e in events if e["step"] == "response"]
    assert response_events, "no 'response' event was ever emitted — the frontend never receives final_response's text"
    assert any(e["status"] == "done" for e in response_events)
    assert "No connections to other cases were found" in _text_of(events)


async def test_xagg_is_labeled_cross_case(run_pipeline):
    events, _ = await run_pipeline(
        route='{"route": "XAGG", "case_scope": "cross_case", "target_entity": null, "output_format": "chat"}',
        message="Which police stations have the most open theft cases?",
        answer="Kohsar station has the most open theft cases.",
        agg_result={
            "kind": "relational_aggregate", "group_by": "police_station",
            "counts": [{"key": "Kohsar", "count": 4}, {"key": "Ramna", "count": 2}],
            "total_cases_considered": 6,
        },
    )

    cross_case_events = [e for e in events if e["step"] == "cross_case_finding"]
    assert cross_case_events
    assert any(e.get("case_scope") == "cross_case" for e in cross_case_events)
    assert "Kohsar" in _text_of(events)


# ── Real RBAC role reaches XGRAPH/XAGG (role-threading fix) ────────────────────
#
# Bug: orchestrator.py used to compute `user_role` from `user_profile.get(
# "role", "investigator")`, but `user_profile` is
# gateway.get_user_context_profile()'s result (preferred_language/
# context_text/llm_mode only — no "role" key at all), so `user_role` always
# silently defaulted to "investigator" regardless of the caller's actual
# RBAC role. That made xagg.py's/graph_retriever.py's supervisor-or-higher
# gates check against "investigator" for every request, denying real
# supervisors/station-admins/platform-admins access rather than granting
# it. Fixed by adding a dedicated `user_role` parameter to process_query(),
# populated by main.py's chat_endpoint from current_user.role (the real
# authenticated user's role), never from user_profile.

async def test_supervisor_role_reaches_xagg(run_pipeline):
    """A real supervisor's role must reach run_aggregate() as "supervisor",
    not silently default to "investigator"."""
    await run_pipeline(
        route='{"route": "XAGG", "case_scope": "cross_case", "target_entity": null, "output_format": "chat"}',
        message="Which police stations have the most open theft cases?",
        user_role="supervisor",
    )
    assert run_pipeline.run_aggregate_calls == ["supervisor"]


async def test_platform_admin_role_reaches_xagg(run_pipeline):
    await run_pipeline(
        route='{"route": "XAGG", "case_scope": "cross_case", "target_entity": null, "output_format": "chat"}',
        message="Which police stations have the most open theft cases?",
        user_role="platform-admin",
    )
    assert run_pipeline.run_aggregate_calls == ["platform-admin"]


async def test_supervisor_role_reaches_xgraph(run_pipeline):
    """Same bug, same fix, the other cross-case route (retrieve_graph())."""
    await run_pipeline(
        route='{"route": "XGRAPH", "case_scope": "cross_case", "target_entity": "0372-1590538", "output_format": "chat"}',
        message="Has phone number 0372-1590538 appeared in other cases?",
        user_role="supervisor",
    )
    assert run_pipeline.retrieve_graph_calls == ["supervisor"]


# ── Milestone E1: query-scope preclassification ─────────────────────────────

async def test_station_classified_by_router_reaches_run_aggregate_as_case_ids(run_pipeline, monkeypatch):
    async def fake_resolve(*, station, district, query_text="", user_id=None, user_role="investigator"):
        assert station == "Iqbal Town"
        assert district is None
        return ["CASE-A", "CASE-B"]

    monkeypatch.setattr(orch, "resolve_jurisdiction_case_ids", fake_resolve)

    await run_pipeline(
        route=(
            '{"route": "XAGG", "case_scope": "cross_case", "target_entity": null, '
            '"output_format": "chat", "station": "Iqbal Town", "district": null}'
        ),
        message="Give me a case count breakdown for Iqbal Town.",
        user_role="supervisor",
    )

    assert run_pipeline.jurisdiction_case_ids_calls == [["CASE-A", "CASE-B"]]


async def test_no_station_or_district_never_calls_the_resolver(run_pipeline, monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("resolve_jurisdiction_case_ids must not be called when the router named no station/district")

    monkeypatch.setattr(orch, "resolve_jurisdiction_case_ids", fail_if_called)

    await run_pipeline(
        route='{"route": "XAGG", "case_scope": "cross_case", "target_entity": null, "output_format": "chat"}',
        message="Which police stations have the most open theft cases?",
        user_role="supervisor",
    )

    assert run_pipeline.jurisdiction_case_ids_calls == [None]


async def test_resolver_failure_degrades_to_unscoped_not_a_pipeline_error(run_pipeline, monkeypatch):
    """A jurisdiction-resolution failure (e.g. a transient graph error) must
    not take down the whole query — it degrades to unscoped, same as before E1."""
    async def fake_resolve(*, station, district, query_text="", user_id=None, user_role="investigator"):
        raise RuntimeError("simulated graph error")

    monkeypatch.setattr(orch, "resolve_jurisdiction_case_ids", fake_resolve)

    events, _ = await run_pipeline(
        route=(
            '{"route": "XAGG", "case_scope": "cross_case", "target_entity": null, '
            '"output_format": "chat", "station": "Iqbal Town", "district": null}'
        ),
        message="Give me a case count breakdown for Iqbal Town.",
        user_role="supervisor",
    )

    assert run_pipeline.jurisdiction_case_ids_calls == [None]
    assert any(e["step"] == "response" and e["status"] == "done" for e in events)


async def test_default_role_is_still_investigator_when_unset(run_pipeline):
    """Regression guard: a caller that doesn't pass user_role at all (the
    pre-fix default, and every attachments/legacy test harness call) must
    still behave exactly as before — "investigator", not None or an error."""
    await run_pipeline(
        route='{"route": "XAGG", "case_scope": "cross_case", "target_entity": null, "output_format": "chat"}',
        message="Which police stations have the most open theft cases?",
    )
    assert run_pipeline.run_aggregate_calls == ["investigator"]


# ── Phase 6: Verifier Gate ─────────────────────────────────────────────────────

async def test_verifier_citation_validator_event_emitted_on_rag(run_pipeline):
    """
    Phase 6: every gated route must emit a citation_validator event so the
    admin UI can surface pass/fail without additional wiring.
    Smoke-test: RAG route (the most common) emits exactly one such event.
    """
    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        message="What PPC section covers mobile theft?",
    )

    cv_events = [e for e in events if e["step"] == "citation_validator"]
    assert cv_events, "Expected at least one citation_validator event from RAG route"
    cv_done = [e for e in cv_events if e["status"] == "done"]
    assert cv_done, "citation_validator event must have status=done"


async def test_verifier_pass_delivers_real_answer(run_pipeline):
    """
    Phase 6: when the verifier returns grounded=True, the original LLM answer
    must reach the user unchanged.
    """
    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        answer="Section 379 PPC governs theft.",
    )

    answer_text = _text_of(events)
    assert "Section 379" in answer_text


async def test_verifier_fail_replaces_response_with_abstention(run_pipeline, monkeypatch):
    """
    Phase 6: when the verifier returns grounded=False, the pipeline must
    NOT deliver the LLM-generated answer — it must substitute the abstention
    message and emit citation_validator with grounded=False.
    """
    import src.pipeline.verifier as verifier_mod

    async def fake_verify_fail(answer, cited_chunks, case_id, **kwargs):
        return {
            "grounded": False,
            "off_topic": False,
            "leaked_case_id": None,
            "unsupported_claims": ["Claim A is fabricated."],
            "reason": "One claim is not in any cited chunk.",
        }

    monkeypatch.setattr(verifier_mod, "verify_grounding", fake_verify_fail)
    # Also patch the imported symbol in orchestrator
    import src.pipeline.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "verify_grounding", fake_verify_fail)

    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        answer="Section 379 PPC governs theft — and also covers cybercrime.",
    )

    # The original answer must NOT appear in the response stream
    answer_text = _text_of(events)
    assert "cybercrime" not in answer_text, (
        "Verifier rejected the answer — original text must not reach the user"
    )
    # The abstention message must appear instead
    assert "consult the original case documents" in answer_text.lower() or \
           "cannot provide a confident answer" in answer_text.lower(), (
        "Abstention message must be substituted when verifier rejects"
    )
    # The citation_validator event must flag failure
    cv_done = next(
        (e for e in events if e["step"] == "citation_validator" and e["status"] == "done"),
        None,
    )
    assert cv_done is not None, "citation_validator done event must be emitted even on failure"
    assert cv_done.get("grounded") is False


async def test_verifier_on_xgraph_still_never_falls_back_to_rag(run_pipeline, monkeypatch):
    """
    Phase 6 + Phase 5 structural separation: even if the verifier rejects an
    XGRAPH answer, the pipeline must NOT fall back to the case-scoped RAG path.
    The abstention message must appear and no retrieval event must be emitted.
    """
    import src.pipeline.verifier as verifier_mod
    import src.pipeline.orchestrator as orch_mod

    async def fake_verify_fail(answer, cited_chunks, case_id, **kwargs):
        return {
            "grounded": False, "off_topic": False, "leaked_case_id": None,
            "unsupported_claims": [], "reason": "Stub rejection.",
        }

    monkeypatch.setattr(verifier_mod, "verify_grounding", fake_verify_fail)
    monkeypatch.setattr(orch_mod, "verify_grounding", fake_verify_fail)

    events, _ = await run_pipeline(
        route='{"route": "XGRAPH", "case_scope": "cross_case", "target_entity": "P-007", "output_format": "chat"}',
        message="Has this entity appeared in other cases?",
        case_id="CASE-010",
        answer="This entity also appears in CASE-011.",
        graph_result={
            "chunks": [_graph_chunk(chunk_id="gx1", case_id="CASE-011")],
            "hop_count": 1, "compounded_confidence": 0.90,
            "seed_entities": [{"entity_id": "P-007", "type": "Person", "name": "Ali Raza"}],
            "unconfirmed_links": [],
        },
    )

    assert not any(e["step"] == "retrieval" for e in events), (
        "XGRAPH must NEVER run the case-scoped retrieval path, even when verifier rejects"
    )
    cv_done = next(
        (e for e in events if e["step"] == "citation_validator" and e["status"] == "done"),
        None,
    )
    assert cv_done is not None
    assert cv_done.get("grounded") is False


async def test_verifier_not_run_for_direct_route(run_pipeline):
    """
    Phase 6: the DIRECT route is not gated by the verifier (no retrieval context
    to ground against). No citation_validator event should be emitted.
    """
    events, _ = await run_pipeline(
        route='{"route": "DIRECT", "output_format": "chat"}',
        message="What is the Pakistan Penal Code?",
    )

    cv_events = [e for e in events if e["step"] == "citation_validator"]
    assert not cv_events, (
        "DIRECT route must NOT emit a citation_validator event — no evidence to ground against"
    )


async def test_rag_retry_exhausted_abstains_without_web_fallback(run_pipeline, monkeypatch):
    """
    Regression: RAG retry-exhaustion must abstain (_SAFE_RESPONSE), not fall
    back to a live Gemini web search. That automatic fallback was removed by
    design (scope change, not a bug fix) — web search is now reachable only
    via the router's own WEB classification or the explicit
    `enable_web_search` per-query toggle, both decided up-front before
    retrieval, never as a reactive fallback from a failed RAG attempt. See
    the comment at the retry-exhaustion branch in orchestrator.py.
    """
    from src.llm import client as llm_client

    async def fake_call_gemini_with_search(user_message, max_tokens=1500):
        raise AssertionError(
            "RAG retry exhaustion must not reach for a live Gemini web search"
        )

    monkeypatch.setattr(llm_client, "call_gemini_with_search", fake_call_gemini_with_search)

    events, _ = await run_pipeline(
        route='{"route": "RAG", "output_format": "chat"}',
        message="What PPC section covers mobile theft?",
        evaluator_relevant=False,  # forces every retry to be rejected, exhausting MAX_RETRIES
    )

    assert not any(e["step"] == "citation_validator" for e in events), (
        "no verification should run — there is nothing to verify when we abstain"
    )
    assert orch._SAFE_RESPONSE.split(" ")[0] in _text_of(events)


# ── Module 7 [findings.md]: adaptive multi-method retrieval ─────────────────
#
# Before this, the router picked exactly one route and whichever retrieval
# method wasn't picked contributed nothing to the answer, even for a
# genuinely compound question (measured live in this module's own
# mini-sweep). route_query()'s new "secondary_methods" field lets the
# router flag additional methods a within-case primary route (SQL/GRAPH/
# GRAPH_HYBRID) should ALSO fetch and fold into the same answer.
#
# The regression risk this section exists to cover: every test ABOVE this
# point predates secondary_methods and never sets it — those must all keep
# behaving exactly as before (byte-identical route decisions, no new LLM/
# retrieval calls, since `route` JSON strings above never include the new
# field and route_query() defaults it to [] whenever it's absent).

async def test_graph_route_with_no_secondary_methods_is_unaffected(run_pipeline):
    """
    Direct regression guard: a GRAPH route JSON with no "secondary_methods"
    key (i.e. every pre-Module-7 test) must fetch nothing extra — same
    single retrieve_graph() call as always.
    """
    events, _ = await run_pipeline(
        route='{"route": "GRAPH", "case_scope": "within_case", "target_entity": "Waqas", "output_format": "chat"}',
        message="Who is connected to Waqas in CASE-009?",
        case_id="CASE-009",
        answer="Waqas is connected to the accused.",
        graph_result={
            "chunks": [_graph_chunk()], "hop_count": 1, "compounded_confidence": 0.9,
            "seed_entities": [], "unconfirmed_links": [],
        },
    )
    assert len(run_pipeline.retrieve_graph_calls) == 1, "no secondary_methods -> exactly one retrieve_graph() call, same as before"
    assert "Waqas" in _text_of(events)


async def test_sql_route_with_graph_secondary_merges_case_evidence(run_pipeline, patched_gateway):
    """
    Compound shape from the mini-sweep: 'what does 379 PPC cover, and what
    item was stolen in this case?' -> primary SQL, secondary GRAPH. The
    graph's case-specific evidence must reach the generation prompt
    alongside the SQL reference rows, not be silently dropped.
    """
    patched_gateway.police_reference_data = [{
        "ref_id": "r1", "category": "penal_code", "subject": "Mobile/Vehicle Theft",
        "description": "Theft of movable property.", "fine_amount": None,
        "section_ref": "379 PPC", "source_document": "offense_sections.csv",
        "source_type": "synthetic", "effective_from": None,
    }]

    events, _ = await run_pipeline(
        route='{"route": "SQL", "output_format": "chat", "secondary_methods": ["GRAPH"]}',
        message="What does 379 PPC cover, and what item was stolen in this case?",
        case_id="CASE-009",
        sql_params='{"category": "penal_code", "subject": "Mobile/Vehicle Theft", "section_ref": null, "date": null}',
        answer="379 PPC covers theft of movable property; a motorcycle was stolen in this case.",
        graph_result={
            "chunks": [_graph_chunk(chunk_id="g-secondary", case_id="CASE-009")],
            "hop_count": 1, "compounded_confidence": 0.9,
            "seed_entities": [], "unconfirmed_links": [],
        },
    )

    assert len(run_pipeline.retrieve_graph_calls) == 1, "secondary GRAPH fetch must run exactly once"
    # SQL's evidence (db rows + supplemental) rides in the SYSTEM prompt.
    assert "known associate" in run_pipeline.call.last_system, (
        "the secondary GRAPH chunk's own text must reach the SQL branch's generation prompt"
    )
    assert "379 PPC" in _text_of(events)


async def test_graph_route_with_sql_secondary_merges_reference_data(run_pipeline, patched_gateway):
    """
    Compound shape from the mini-sweep: 'what is this weapon's condition,
    and what PPC section covers unlicensed possession?' -> primary GRAPH,
    secondary SQL. The SQL reference row must reach the generation prompt
    alongside the graph's own case evidence.
    """
    patched_gateway.police_reference_data = [{
        "ref_id": "r1", "category": "penal_code", "subject": "Mobile/Vehicle Theft",
        "description": "Theft of movable property.", "fine_amount": None,
        "section_ref": "379 PPC", "source_document": "offense_sections.csv",
        "source_type": "synthetic", "effective_from": None,
    }]

    events, _ = await run_pipeline(
        route='{"route": "GRAPH", "case_scope": "within_case", "target_entity": null, '
              '"output_format": "chat", "secondary_methods": ["SQL"]}',
        message="What is this weapon's condition, and what PPC section covers this?",
        case_id="CASE-009",
        sql_params='{"category": "penal_code", "subject": "Mobile/Vehicle Theft", "section_ref": null, "date": null}',
        answer="The weapon is unlicensed, which falls under 379 PPC.",
        graph_result={
            "chunks": [_graph_chunk()], "hop_count": 1, "compounded_confidence": 0.9,
            "seed_entities": [], "unconfirmed_links": [],
        },
    )

    # GRAPH's own evidence (documents_text) rides in the USER turn (Module 3 fix).
    assert "379 PPC" in run_pipeline.call.last_user, (
        "the secondary SQL row's own text must reach the GRAPH branch's generation prompt"
    )
    assert "unlicensed" in _text_of(events)


async def test_graph_hybrid_route_with_xgraph_secondary_passes_cross_case_ids_to_verifier(run_pipeline, monkeypatch):
    """
    Compound shape from the mini-sweep: 'summarize this case, and is this a
    repeat-offender pattern?' -> primary GRAPH_HYBRID, secondary XGRAPH.
    The other case's chunk must reach the generation prompt AND its case_id
    must be passed to verify_grounding()'s cross_case_ids allowlist — the
    exact mechanism (src/pipeline/verifier.py's _check_leakage()) that lets
    a legitimately-cited cross-case chunk avoid being flagged as leakage.
    """
    captured_kwargs = {}

    async def fake_verify(answer, cited_chunks, case_id, **kwargs):
        captured_kwargs.update(kwargs)
        captured_kwargs["cited_chunks"] = cited_chunks
        return {"grounded": True, "off_topic": False, "leaked_case_id": None,
                "unsupported_claims": [], "reason": "All claims supported."}

    monkeypatch.setattr(orch, "verify_grounding", fake_verify)

    events, _ = await run_pipeline(
        route='{"route": "GRAPH_HYBRID", "case_scope": "within_case", "target_entity": null, '
              '"output_format": "chat", "secondary_methods": ["XGRAPH"]}',
        message="Summarize this case, and is this a repeat-offender pattern?",
        case_id="CASE-009",
        answer="This case involves the accused; the same person also appears in CASE-777.",
        graph_result={
            "chunks": [_graph_chunk(chunk_id="g-xgraph", case_id="CASE-777")],
            "hop_count": 1, "compounded_confidence": 0.9,
            "seed_entities": [], "unconfirmed_links": [],
        },
    )

    assert captured_kwargs.get("cross_case_ids") == ["CASE-777"], (
        "the OTHER case's id must reach verify_grounding()'s cross_case_ids allowlist"
    )
    cited_ids = {c["id"] for c in captured_kwargs.get("cited_chunks", [])}
    assert "g-xgraph" in cited_ids, "the secondary XGRAPH chunk must be part of the cited evidence, not dropped"
    assert "CASE-777" in _text_of(events)


async def test_xgraph_primary_route_ignores_secondary_methods(run_pipeline):
    """
    Scoping guard: secondary_methods is only ever honored for a within-case
    primary route (SQL/GRAPH/GRAPH_HYBRID). If the router ever returns
    secondary_methods alongside an XGRAPH/XAGG/XNETWORK primary route
    (should never happen per prompts/router.txt, but must not be trusted
    blindly), it must be silently ignored — those three routes' own
    structurally-separate, never-blended contract must stay untouched.
    """
    events, _ = await run_pipeline(
        route='{"route": "XGRAPH", "case_scope": "cross_case", "target_entity": "0372-1590538", '
              '"output_format": "chat", "secondary_methods": ["SQL"]}',
        message="Has phone 0372-1590538 appeared in other cases?",
        answer="This phone number also appears in CASE-005.",
        graph_result={
            "chunks": [_graph_chunk(chunk_id="g2", case_id="CASE-005")],
            "hop_count": 1, "compounded_confidence": 0.95,
            "seed_entities": [], "unconfirmed_links": [],
        },
    )
    # Exactly one retrieve_graph() call (XGRAPH's own) — no secondary SQL
    # fetch, and critically no SECOND retrieve_graph()/gateway call either.
    assert len(run_pipeline.retrieve_graph_calls) == 1
    assert not any(e["step"] == "retrieval" for e in events), "XGRAPH must still never run the case-scoped retrieval path"
