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
                   evaluator_relevant=True):

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
            fake_call_llm.last_kwargs = kwargs
            return answer

        fake_call_llm.last_system = ""
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

        chunks = [
            {"id": "c1", "text": "Theft of movable property is punishable under 379 PPC.",
             "metadata": {"source": "PPC.pdf"}, "rrf_score": 0.9}
        ]
        patched_gateway.chunks = chunks

        async def fake_query_similar(_query, _embedding, top_k=10, where=None):
            if query_similar_exc is not None:
                raise query_similar_exc
            return chunks

        monkeypatch.setattr(orch, "embed_text", fake_embed)
        monkeypatch.setattr(orch, "query_similar", fake_query_similar)
        monkeypatch.setattr("src.pipeline.query_expander.expand_query", fake_expand)

        # Graph retrieval / cross-case aggregate boundaries (Phase 5).
        # Defaults are empty/no-op so tests that don't care about GRAPH/
        # XGRAPH/XAGG (i.e. every pre-Phase-5 test) fall straight through
        # to their existing RAG-fallback behavior, unchanged.
        default_graph_result = {
            "chunks": [], "hop_count": 0, "compounded_confidence": 1.0,
            "seed_entities": [], "unconfirmed_links": [],
        }

        async def fake_retrieve_graph(_query, _target_entity, case_id=None, cross_case=False,
                                       max_hops=2, user_id=None, user_role="investigator"):
            return graph_result if graph_result is not None else default_graph_result

        async def fake_run_aggregate(_query, _target_entity, _gateway, user_id=None, user_role="investigator"):
            return agg_result if agg_result is not None else {
                "kind": "relational_aggregate", "group_by": "police_station",
                "counts": [], "total_cases_considered": 0,
            }

        monkeypatch.setattr(orch, "retrieve_graph", fake_retrieve_graph)
        monkeypatch.setattr(orch, "run_aggregate", fake_run_aggregate)

        # SQLite audit log — irrelevant here, and it would touch disk
        for fn in ("upsert_session", "create_query", "log_step", "log_llm_call",
                   "log_retrieved_docs", "update_retrieved_docs_relevance", "update_query"):
            monkeypatch.setattr(orch.pipeline_logger, fn,
                                lambda *a, **k: 1, raising=False)

        events = []
        async for event in orch.process_query(
            session_id, message, project_id=project_id, case_id=case_id,
            user_profile=user_profile, user_id=user_id,
        ):
            events.append(event)

        # Expose both LLM trackers so individual tests can inspect which
        # system prompt was used on each code path:
        #   _run.stream  — last stream_llm call (DIRECT route, file structurer)
        #   _run.call    — last non-special call_llm call (RAG, GRAPH, SQL, etc.)
        _run.stream = fake_stream_llm
        _run.call = fake_call_llm
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
    await run_pipeline(
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
    assert "textile SME" in system_prompt


async def test_llm_mode_setting_is_passed_to_the_client(run_pipeline):
    """The llm_mode setting used to save but never be read by anything."""
    # DIRECT route still uses stream_llm — the mode flag is captured there.
    await run_pipeline(
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

    await run_pipeline(project_id=project["id"])

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
    events, gateway = await run_pipeline(
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

    events, _ = await run_pipeline(route='{"route": "DIRECT", "output_format": "file_pdf"}')

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
