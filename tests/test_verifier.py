"""
Unit tests for src.pipeline.verifier — the Phase 6 grounding gate.

All external calls (call_llm) are monkeypatched; no network, no disk.
"""
import pytest

from src.pipeline.verifier import (
    _check_fabricated_case_ids,
    _check_hedging,
    _check_leakage,
    _check_no_citation,
    _check_temporal,
    _format_chunks_for_verifier,
    verify_grounding,
    verify_structured_aggregate_paraphrase,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _chunk(
    chunk_id="c1",
    text="Section 379 PPC: theft is punishable by up to three years.",
    source="PPC.pdf",
    case_id=None,
    graph_confidence=None,
):
    meta = {"source": source}
    if case_id:
        meta["case_id"] = case_id
    chunk = {"id": chunk_id, "text": text, "metadata": meta}
    if graph_confidence is not None:
        chunk["graph_confidence"] = graph_confidence
    return chunk


def _harness_chunk(
    chunk_id="c1",
    text="Section 379 PPC: theft is punishable by up to three years.",
    source="PPC.pdf",
    case_id=None,
    confidence=None,
    confidence_status="not_computed",
):
    """
    The OTHER real call shape `_effective_confidence()` must handle —
    an `EvidenceChunk` flattened per SUBAGENT_INTERFACES.md §2's
    "Verifier boundary" note (`{"id", "text", "metadata": chunk.metadata.
    model_dump()}`), confidence living at `metadata.confidence`/
    `metadata.confidence_status`, never at a top-level `graph_confidence`
    key. Every sub-agent's own `_chunk_to_verifier_dict()` builds exactly
    this shape.
    """
    meta = {"source": source, "confidence": confidence, "confidence_status": confidence_status}
    if case_id:
        meta["case_id"] = case_id
    return {"id": chunk_id, "text": text, "metadata": meta}


# ── _format_chunks_for_verifier ───────────────────────────────────────────────

def test_format_chunks_numbers_from_one():
    chunks = [_chunk("c1"), _chunk("c2", source="FIR.pdf")]
    out = _format_chunks_for_verifier(chunks)
    assert "[1] Source: PPC.pdf" in out
    assert "[2] Source: FIR.pdf" in out


def test_format_chunks_includes_graph_confidence():
    chunks = [_chunk(graph_confidence=0.72)]
    out = _format_chunks_for_verifier(chunks)
    assert "graph_confidence: 0.72" in out


def test_format_chunks_includes_case_id():
    chunks = [_chunk(case_id="CASE-001")]
    out = _format_chunks_for_verifier(chunks)
    assert "case_id: CASE-001" in out


def test_format_chunks_no_optional_fields_when_absent():
    chunks = [_chunk()]
    out = _format_chunks_for_verifier(chunks)
    # Neither graph_confidence nor case_id should appear for a plain chunk
    assert "graph_confidence" not in out
    assert "case_id" not in out


# ── _check_temporal ────────────────────────────────────────────────────────────

def test_temporal_no_date_skips():
    chunks = [_chunk()]
    assert _check_temporal(chunks, None) == []


def test_temporal_future_effective_from():
    chunk = _chunk()
    chunk["metadata"]["effective_from"] = 2030
    issues = _check_temporal([chunk], target_date=2025)
    assert issues
    assert "not yet effective" in issues[0]


def test_temporal_expired_effective_to():
    chunk = _chunk()
    chunk["metadata"]["effective_to"] = 2010
    issues = _check_temporal([chunk], target_date=2025)
    assert issues
    assert "expired" in issues[0]


def test_temporal_valid_range_no_issues():
    chunk = _chunk()
    chunk["metadata"]["effective_from"] = 2000
    chunk["metadata"]["effective_to"] = 2030
    assert _check_temporal([chunk], target_date=2025) == []


# ── _check_leakage ─────────────────────────────────────────────────────────────

def test_leakage_cross_case_always_clean():
    """cross_case scope is never checked for leakage."""
    chunks = [_chunk(case_id="CASE-999")]
    # Citing a foreign case in a cross_case context is fine
    result = _check_leakage(
        "[Document 1] something", [chunks[0]],
        active_case_id="cross_case", cross_case_ids=[]
    )
    assert result is None


def test_leakage_no_case_id_in_answer_is_clean():
    chunks = [_chunk(case_id="CASE-001")]
    # Answer doesn't cite anything
    result = _check_leakage("No citations here.", chunks, "CASE-001", [])
    assert result is None


def test_leakage_own_case_is_clean():
    chunks = [_chunk(case_id="CASE-001")]
    result = _check_leakage("[Document 1] info from own case.", chunks, "CASE-001", [])
    assert result is None


def test_leakage_foreign_case_is_detected():
    chunks = [_chunk(case_id="CASE-999")]
    result = _check_leakage(
        "[Document 1] info from another case.",
        chunks, active_case_id="CASE-001", cross_case_ids=[]
    )
    assert result == "CASE-999"


def test_leakage_allowed_cross_case_is_clean():
    """A cross-case chunk is clean if its case_id is in cross_case_ids."""
    chunks = [_chunk(case_id="CASE-002")]
    result = _check_leakage(
        "[Document 1] info from CASE-002.",
        chunks, active_case_id="CASE-001", cross_case_ids=["CASE-002"]
    )
    assert result is None


def test_leakage_out_of_range_citation_is_ignored():
    """[Document 99] when there are only 2 chunks must not raise."""
    chunks = [_chunk(), _chunk("c2")]
    result = _check_leakage("[Document 99] oops.", chunks, "CASE-001", [])
    assert result is None


# ── _check_hedging ─────────────────────────────────────────────────────────────

def test_hedging_high_confidence_no_check():
    """Chunks with graph_confidence >= 0.85 don't need hedging."""
    chunks = [_chunk(graph_confidence=0.90)]
    answer = "[Document 1] This is certain."
    assert _check_hedging(answer, chunks) == []


def test_hedging_low_confidence_missing_hedge():
    chunks = [_chunk(graph_confidence=0.70)]
    answer = "[Document 1] Ali Hassan is the same person as Ali H. in CASE-003."
    issues = _check_hedging(answer, chunks)
    assert issues, "Should flag missing hedge for low-confidence chunk"
    assert "0.70" in issues[0]


def test_hedging_low_confidence_hedge_present():
    chunks = [_chunk(graph_confidence=0.70)]
    answer = "[Document 1] Ali Hassan is possibly the same person (UNCONFIRMED, pending review)."
    assert _check_hedging(answer, chunks) == []


def test_hedging_multiple_hedge_phrases():
    for phrase in ("unconfirmed", "possible", "pending", "not yet verified",
                   "under review", "flagged", "uncertain", "may be"):
        chunks = [_chunk(graph_confidence=0.50)]
        answer = f"[Document 1] The link is {phrase} at this stage."
        assert _check_hedging(answer, chunks) == [], f"phrase '{phrase}' should satisfy hedging"


# ── Milestone D2 — pending-identity disclosure extension ────────────────────
# (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md: extends _check_hedging(), never a
# parallel check — the tag is keyed independently of the numeric confidence
# so it still fires even if a future edit changes how confidence compounds.)

def _same_as_pending_chunk(chunk_id="c1", graph_confidence=0.95):
    """A chunk reached via graph_retriever.py's opened pending-SAME_AS traversal — tagged in metadata regardless of its (already-capped, but tested here at a deliberately HIGH value) numeric confidence."""
    chunk = _chunk(chunk_id=chunk_id, graph_confidence=graph_confidence)
    chunk["metadata"]["same_as_status"] = "pending"
    chunk["metadata"]["same_as_basis"] = "matched on near-identical name + shared case"
    return chunk


def test_hedging_required_for_pending_identity_tag_even_at_high_confidence():
    """The tag alone must trigger the hedge requirement — not just a low graph_confidence number."""
    chunks = [_same_as_pending_chunk(graph_confidence=0.95)]
    answer = "[Document 1] This is the same person."
    issues = _check_hedging(answer, chunks)
    assert issues, "a pending-identity-tagged chunk must require a hedge even at high numeric confidence"
    assert "unconfirmed identity link" in issues[0]


def test_hedging_satisfied_for_pending_identity_tag_with_hedge_phrase():
    chunks = [_same_as_pending_chunk(graph_confidence=0.95)]
    answer = "[Document 1] This is possibly the same person, unconfirmed."
    assert _check_hedging(answer, chunks) == []


def test_hedging_no_false_positive_for_untagged_high_confidence_chunk():
    """No pending tag, no low confidence — must not require a hedge (no false positives on the disclosure itself)."""
    chunks = [_chunk(graph_confidence=0.95)]
    answer = "[Document 1] This is confirmed."
    assert _check_hedging(answer, chunks) == []


def test_hedging_no_graph_confidence_key_skipped():
    """Chunks without a graph_confidence key are not checked."""
    chunk = _chunk()  # no graph_confidence
    answer = "[Document 1] Definitive statement without hedge."
    assert _check_hedging(answer, [chunk]) == []


# ── _check_hedging via the HARNESS chunk shape [AMENDMENT — design §7] ──────────
#
# The exact gap AGENT_HARNESS_DESIGN.md §7 tracked as open and deliberately
# unresolved: `ChunkMetadata.confidence`/`confidence_status`, not a
# top-level `graph_confidence` key, is what every sub-agent (Case
# Summarization, Cross-Case Linkage, Investigative Analysis) actually
# passes for GRAPH-derived evidence. Before this fix, `_check_hedging()`
# only ever read `graph_confidence`, so the hedging check silently never
# fired for any harness-sourced chunk regardless of its real confidence.


def test_hedging_harness_shape_computed_low_confidence_missing_hedge():
    chunks = [_harness_chunk(confidence=0.60, confidence_status="computed")]
    answer = "[Document 1] Ali Hassan is the same person as Ali H. in CASE-003."
    issues = _check_hedging(answer, chunks)
    assert issues, "Should flag missing hedge for a harness-shaped low-confidence chunk"
    assert "0.60" in issues[0]


def test_hedging_harness_shape_computed_low_confidence_hedge_present():
    chunks = [_harness_chunk(confidence=0.60, confidence_status="computed")]
    answer = "[Document 1] Ali Hassan is possibly the same person (unconfirmed)."
    assert _check_hedging(answer, chunks) == []


def test_hedging_harness_shape_computed_high_confidence_no_check():
    chunks = [_harness_chunk(confidence=0.95, confidence_status="computed")]
    answer = "[Document 1] This is certain."
    assert _check_hedging(answer, chunks) == []


def test_hedging_harness_shape_not_computed_skipped():
    # RAG's own case (types.py's own docstring example): flat retrieval
    # never computes a confidence at all. Legitimately absent, not a
    # failure -- must not be flagged.
    chunks = [_harness_chunk(confidence=None, confidence_status="not_computed")]
    answer = "[Document 1] Definitive statement without hedge."
    assert _check_hedging(answer, chunks) == []


def test_hedging_harness_shape_check_failed_requires_hedge_unconditionally():
    # [AMENDMENT — design §7's exact fix] check_failed must be treated at
    # LEAST as cautiously as a known-low confidence score, even though
    # `confidence` itself is None here -- never "no signal, proceed
    # unhedged," which is the false-all-clear this check exists to catch.
    chunks = [_harness_chunk(confidence=None, confidence_status="check_failed")]
    answer = "[Document 1] Definitive statement without hedge."
    issues = _check_hedging(answer, chunks)
    assert issues
    assert "confidence check failed" in issues[0]


def test_hedging_harness_shape_check_failed_hedge_present_passes():
    chunks = [_harness_chunk(confidence=None, confidence_status="check_failed")]
    answer = "[Document 1] This link is unconfirmed pending further review."
    assert _check_hedging(answer, chunks) == []


def test_format_chunks_check_failed_shown_as_unknown_not_a_number():
    chunks = [_harness_chunk(confidence=None, confidence_status="check_failed")]
    out = _format_chunks_for_verifier(chunks)
    assert "confidence: unknown (check failed)" in out


def test_format_chunks_harness_shape_computed_confidence_displayed():
    chunks = [_harness_chunk(confidence=0.72, confidence_status="computed")]
    out = _format_chunks_for_verifier(chunks)
    assert "graph_confidence: 0.72" in out


def test_format_chunks_harness_shape_not_computed_shows_nothing():
    chunks = [_harness_chunk(confidence=None, confidence_status="not_computed")]
    out = _format_chunks_for_verifier(chunks)
    assert "confidence" not in out


# ── _check_no_citation ───────────────────────────────────────────────────────

def test_no_citation_bracket_form_passes():
    answer = "[Document 1] " + "This is a substantial, properly cited answer. " * 5
    assert _check_no_citation(answer) is None


def test_no_citation_short_answer_is_not_flagged():
    """Too short to be 'substantial' — a legitimate one-line no-match answer."""
    assert _check_no_citation("Not found in the documents.") is None


def test_no_citation_flags_a_substantial_answer_with_no_document_reference():
    answer = "This is a long answer with no reference to any source at all. " * 5
    issue = _check_no_citation(answer)
    assert issue is not None
    assert "cites no" in issue


def test_no_citation_accepts_bold_markdown_document_reference():
    """
    Regression (2026-08-03, live query_id 889): the generator wrote
    "**Document 1**" (markdown bold, no brackets) in an otherwise correctly
    grounded answer — "In Document 1, it is mentioned that 'the stolen
    items were recovered'..." — and this deterministic check rejected it as
    if it cited nothing, burning two regeneration attempts before falling
    back to a generic abstention. The check's job is to catch answers that
    never engage with a specific source, not to enforce exact bracket
    syntax.
    """
    answer = (
        "Based on the information provided in the documents, the following "
        "items have been recovered in the investigation:\n\n"
        "- In **Document 1**, it is mentioned that the stolen items were "
        "recovered. However, the specific items are not listed in the text."
    )
    assert _check_no_citation(answer) is None


def test_no_citation_accepts_bare_document_reference_without_brackets_or_bold():
    answer = "As stated in Document 2, the vehicle was recovered near the site. " * 3
    assert _check_no_citation(answer) is None


def test_no_citation_flags_short_uncited_enumerated_list():
    """
    Regression, confirmed live: an XGRAPH cross-case query asked in Urdu
    returned a numbered list of 8 names with zero [Document N] citations —
    a direct violation of cross_case_response.txt's mandatory citation
    rule. The whole answer was 92 characters, under
    _SUBSTANTIAL_ANSWER_LEN, so the length-only check let it straight
    through while an equivalent English answer (longer prose, same lack of
    citations) was correctly flagged. This is the Urdu-shaped repro
    (Latin transliteration here only so the test file stays ASCII-only;
    the character-count property that mattered is preserved).
    """
    answer = "Log yeh hain:\n1. Ahmed\n2. Ayesha\n3. Ali\n4. Zainab\n5. Hassan\n6. Sara\n7. Muhammad\n8. Fatima"
    assert len(answer) < 150  # the exact gap this test guards against
    issue = _check_no_citation(answer)
    assert issue is not None
    assert "cites no" in issue


def test_no_citation_does_not_flag_a_short_non_list_answer():
    """A short, honest, non-enumerated answer must still pass — the list
    heuristic must not turn into a blanket "any 2 lines fails" rule."""
    answer = "Ahmed appears in one case.\nAyesha appears in another case."
    assert _check_no_citation(answer) is None


def test_no_citation_flags_long_enumerated_list_same_as_before():
    """The pre-existing length-based path must still work unchanged for a
    list that's also long enough to trip _SUBSTANTIAL_ANSWER_LEN on its own."""
    answer = (
        "The following individuals are mentioned across the case evidence:\n"
        "1. Ahmed Khan\n2. Ayesha Malik\n3. Ali Raza\n4. Zainab Bibi\n"
        "5. Hassan Sheikh\n6. Sara Iqbal\n7. Muhammad Tariq\n8. Fatima Noor"
    )
    assert len(answer) >= 150
    issue = _check_no_citation(answer)
    assert issue is not None


# ── _check_fabricated_case_ids (Scenario-test Finding J) ─────────────────────
#
# A cross-case answer cited "CASE-ID: CR-C101-1" / "CR-C102-1" / "CR-C105-1"
# alongside real fir-NNN-26 ids. None of those CR-* ids exist anywhere in the
# corpus — not as a case_id, external_id, source, or chunk-text substring.
# Invented provenance is worse than an uncited claim: it looks verifiable and
# resolves to nothing.

def _id_chunk(case_id=None, external_id=None):
    """Minimal chunk carrying only the id metadata this check looks at.
    Named distinctly from this module's existing `_chunk()` fixture so it
    doesn't shadow it."""
    meta = {}
    if case_id:
        meta["case_id"] = case_id
    if external_id:
        meta["external_id"] = external_id
    return {"id": "c1", "text": "Some evidence.", "metadata": meta}


def test_fabricated_case_id_in_citation_is_flagged():
    answer = "Faisal appears in [Document 1, CASE-ID: CR-C101-1]."
    issues = _check_fabricated_case_ids(answer, [_id_chunk(case_id="fir-201-26")])
    assert len(issues) == 1
    assert "CR-C101-1" in issues[0]


def test_real_case_id_in_citation_is_not_flagged():
    answer = "Faisal appears in [Document 1, CASE-ID: fir-201-26]."
    assert _check_fabricated_case_ids(answer, [_id_chunk(case_id="fir-201-26")]) == []


def test_case_id_matching_is_case_insensitive():
    answer = "Cited as [Document 1, CASE-ID: FIR-201-26]."
    assert _check_fabricated_case_ids(answer, [_id_chunk(case_id="fir-201-26")]) == []


def test_external_id_also_counts_as_known():
    answer = "Cited as [Document 1, CASE-ID: rz-fir-465-26]."
    assert _check_fabricated_case_ids(answer, [_id_chunk(external_id="rz-fir-465-26")]) == []


def test_each_fabricated_id_reported_once_even_if_cited_repeatedly():
    answer = (
        "A [Document 1, CASE-ID: CR-C101-1] and "
        "B [Document 2, CASE-ID: CR-C101-1] and "
        "C [Document 3, CASE-ID: CR-C102-1]."
    )
    issues = _check_fabricated_case_ids(answer, [_id_chunk(case_id="fir-201-26")])
    assert len(issues) == 2


def test_plain_document_citations_without_case_ids_are_ignored():
    answer = "The weapon was seized [Document 2]."
    assert _check_fabricated_case_ids(answer, [_id_chunk(case_id="fir-430-26")]) == []


# ── Unlabeled "[Document N, <case-id>]" form — no "CASE-ID:" text ────────────
#
# prompts/cross_case_response.txt's OWN rules (2, 8, 11) and every one of its
# worked examples ("Phone X also appears in CASE-005 [Document 3, CASE-005]",
# "X appears in CASE-001 [Document 1, CASE-001]") instruct exactly this
# unlabeled form — the "CASE-ID:" label never appears anywhere in the actual
# prompt. Confirmed live by reading the prompt directly: the tests above,
# which only ever exercise the labeled form, would all still pass even if
# this function stopped recognizing the unlabeled form entirely — a model
# correctly following its own system prompt could fabricate a case id in
# exactly this shape and this check would silently miss it. These tests
# guard the format the prompt actually produces, not just the one incidental
# example the original "Found live" bug happened to show.

def test_fabricated_case_id_in_unlabeled_citation_is_flagged():
    answer = "Faisal appears in CR-C101-1 [Document 1, CR-C101-1]."
    issues = _check_fabricated_case_ids(answer, [_id_chunk(case_id="fir-201-26")])
    assert len(issues) == 1
    assert "CR-C101-1" in issues[0]


def test_real_case_id_in_unlabeled_citation_is_not_flagged():
    answer = "Faisal appears in CASE-005 [Document 1, fir-201-26]."
    assert _check_fabricated_case_ids(answer, [_id_chunk(case_id="fir-201-26")]) == []


def test_unlabeled_citation_matching_is_case_insensitive():
    answer = "Cited as [Document 1, FIR-201-26]."
    assert _check_fabricated_case_ids(answer, [_id_chunk(case_id="fir-201-26")]) == []


# ── verify_grounding (async, with LLM monkeypatched) ─────────────────────────

@pytest.mark.asyncio
async def test_verify_returns_not_grounded_for_empty_chunks():
    result = await verify_grounding(
        answer="Section 379 applies.",
        cited_chunks=[],
        case_id="CASE-001",
    )
    assert result["grounded"] is False
    assert "No source chunks" in result["reason"]


# [Scenario-test Finding G] An empty answer must never be reported as
# grounded. call_llm() can return empty content without raising, and before
# this guard the LLM verifier concluded "empty answer, no claims to verify"
# -> grounded=True, so a 0-char answer was served to the user as a verified
# success. Confirmed live on the XAGG route.
@pytest.mark.asyncio
@pytest.mark.parametrize("empty_answer", ["", "   ", "\n\n", "\t "])
async def test_verify_returns_not_grounded_for_empty_answer(empty_answer, monkeypatch):
    import src.pipeline.verifier as vmod

    async def _must_not_be_called(*a, **kw):  # pragma: no cover
        raise AssertionError("verifier must short-circuit before the LLM call")

    monkeypatch.setattr(vmod, "call_llm", _must_not_be_called)

    result = await verify_grounding(
        answer=empty_answer,
        cited_chunks=[{"id": "c1", "text": "Some evidence.", "metadata": {}}],
        case_id="CASE-001",
    )
    assert result["grounded"] is False
    assert "empty" in result["reason"].lower()


@pytest.mark.asyncio
async def test_verify_propagates_llm_grounded_verdict(monkeypatch):
    import src.pipeline.verifier as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return '{"grounded": true, "off_topic": false, "leaked_case_id": null, "unsupported_claims": [], "reason": "All fine."}'

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    result = await verify_grounding(
        answer="Section 379 PPC governs theft [Document 1].",
        cited_chunks=[_chunk()],
        case_id="CASE-001",
    )
    assert result["grounded"] is True
    assert result["reason"] == "All fine."


@pytest.mark.asyncio
async def test_verify_propagates_llm_not_grounded_verdict(monkeypatch):
    import src.pipeline.verifier as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return (
            '{"grounded": false, "off_topic": false, "leaked_case_id": null, '
            '"unsupported_claims": ["Claim X not in chunk."], '
            '"reason": "Claim X is unsupported."}'
        )

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    result = await verify_grounding(
        answer="Section 379 and also cybercrime [Document 1].",
        cited_chunks=[_chunk()],
        case_id="CASE-001",
    )
    assert result["grounded"] is False
    assert "Claim X" in result["unsupported_claims"][0]


@pytest.mark.asyncio
async def test_verify_fail_closed_on_bad_json(monkeypatch):
    """If the LLM returns unparseable JSON twice, default to not-grounded."""
    import src.pipeline.verifier as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return "THIS IS NOT JSON"

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    result = await verify_grounding(
        answer="Some answer.",
        cited_chunks=[_chunk()],
        case_id="CASE-001",
    )
    assert result["grounded"] is False
    assert "fail-closed" in result["reason"].lower()


@pytest.mark.asyncio
async def test_verify_deterministic_leakage_overrides_llm_pass(monkeypatch):
    """
    Even if the LLM judge says grounded=True, a deterministic leakage
    detection must override it to grounded=False.
    """
    import src.pipeline.verifier as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return '{"grounded": true, "off_topic": false, "leaked_case_id": null, "unsupported_claims": [], "reason": "Fine."}'

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    # Chunk belongs to CASE-999 but active case is CASE-001
    foreign_chunk = _chunk(case_id="CASE-999")
    result = await verify_grounding(
        answer="Evidence from [Document 1] shows the suspect.",
        cited_chunks=[foreign_chunk],
        case_id="CASE-001",
    )
    assert result["grounded"] is False
    assert result["leaked_case_id"] == "CASE-999"
    assert "leakage" in result["reason"].lower()


@pytest.mark.asyncio
async def test_verify_deterministic_hedging_overrides_llm_pass(monkeypatch):
    """
    Even if the LLM judge says grounded=True, a missing hedge on a
    low-confidence chunk must override it to grounded=False.
    """
    import src.pipeline.verifier as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return '{"grounded": true, "off_topic": false, "leaked_case_id": null, "unsupported_claims": [], "reason": "Fine."}'

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    low_conf_chunk = _chunk(graph_confidence=0.60)
    result = await verify_grounding(
        answer="The suspect is [Document 1] confirmed to be the same person.",
        cited_chunks=[low_conf_chunk],
        case_id="CASE-001",
    )
    assert result["grounded"] is False
    assert any("hedging" in issue.lower() or "graph_confidence" in issue.lower()
               for issue in result["unsupported_claims"])


@pytest.mark.asyncio
async def test_verify_cross_case_scope_skips_leakage_check(monkeypatch):
    """cross_case routes must not be flagged for leakage — all cases are allowed."""
    import src.pipeline.verifier as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return '{"grounded": true, "off_topic": false, "leaked_case_id": null, "unsupported_claims": [], "reason": "Fine."}'

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    foreign_chunk = _chunk(case_id="CASE-999")
    result = await verify_grounding(
        answer="Evidence from [Document 1].",
        cited_chunks=[foreign_chunk],
        case_id="cross_case",
    )
    assert result["grounded"] is True
    assert result["leaked_case_id"] is None


# ═══════════════════════════════════════════════════════════════════════
# Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 4:
# verify_structured_aggregate_paraphrase() — relaxed grounding for a
# paraphrase of a deterministic, code-computed aggregate/cluster result.
# No call_llm() involved at all — purely deterministic, no monkeypatching
# needed.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_structured_paraphrase_passes_when_numbers_match_source():
    result = await verify_structured_aggregate_paraphrase(
        answer="4 people appear across multiple cases, each in 2 cases.",
        source_text="4 matching Person(s) found, each appears in 2 cases.",
        case_id="cross_case",
    )
    assert result["grounded"] is True


@pytest.mark.asyncio
async def test_structured_paraphrase_fails_when_a_number_is_invented():
    """The over-rejection Module 4 fixes is one direction; this guards the
    opposite failure mode — a paraphrase must not pass with a fabricated
    number the source never stated."""
    result = await verify_structured_aggregate_paraphrase(
        answer="94 people appear across multiple cases.",
        source_text="4 matching Person(s) found, each appears in 2 cases.",
        case_id="cross_case",
    )
    assert result["grounded"] is False
    assert "94" in result["reason"]


@pytest.mark.asyncio
async def test_structured_paraphrase_passes_regardless_of_hedging_phrasing():
    """The exact bug this fixes: verify_grounding()'s free-text judge/
    hedging checks are tuned for narrative claims and reject an accurate,
    confidently-phrased paraphrase of a deterministic count. This check
    only cares whether the numbers match."""
    result = await verify_structured_aggregate_paraphrase(
        answer="There are definitely 3 recurring vehicles across these cases.",
        source_text="3 Vehicle(s) found recurring across cases: V-001, V-002, V-003.",
        case_id="cross_case",
    )
    assert result["grounded"] is True


@pytest.mark.asyncio
async def test_structured_paraphrase_empty_answer_fails_closed():
    result = await verify_structured_aggregate_paraphrase(
        answer="", source_text="4 matching Person(s) found.", case_id="cross_case",
    )
    assert result["grounded"] is False


@pytest.mark.asyncio
async def test_structured_paraphrase_allows_real_cross_case_ids_in_citations():
    """A paraphrase legitimately citing several real case ids from
    cross_case_ids must not be flagged as a fabricated citation."""
    result = await verify_structured_aggregate_paraphrase(
        answer="This person appears in [Document 1, CASE-100] and [Document 1, CASE-101].",
        source_text="2 cases: CASE-100, CASE-101.",
        case_id="cross_case",
        cross_case_ids=["CASE-100", "CASE-101"],
    )
    assert result["grounded"] is True


@pytest.mark.asyncio
async def test_structured_paraphrase_flags_a_fabricated_case_citation():
    result = await verify_structured_aggregate_paraphrase(
        answer="This person appears in [Document 1, CASE-999].",
        source_text="2 cases: CASE-100, CASE-101.",
        case_id="cross_case",
        cross_case_ids=["CASE-100", "CASE-101"],
    )
    assert result["grounded"] is False


# ═══════════════════════════════════════════════════════════════════════
# Gold-QA fix — Module 4, question D1: a paraphrase stating the GRAND
# TOTAL that equals the sum of the source's own per-category breakdown
# must pass, even though that summed number is not literally present in
# the source text.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_structured_paraphrase_allows_a_correct_grand_total_of_the_breakdown():
    result = await verify_structured_aggregate_paraphrase(
        answer="There are 79 FIRs in total.",
        source_text=(
            "- PPC: 25 cases\n- PPC, Arms Ordinance 1965: 21 cases\n"
            "- PECA 2016, PPC: 9 cases\n- CNSA 1997, Arms Ordinance 1965: 8 cases\n"
            "- unknown: 6 cases\n- CNSA 1997: 4 cases\n"
            "- PPC, Punjab Domestic Violence Act: 4 cases\n"
            "- PPC, Illegal Dispossession Act 2005: 2 cases"
        ),
        case_id="cross_case",
    )
    assert result["grounded"] is True


@pytest.mark.asyncio
async def test_structured_paraphrase_still_rejects_a_fabricated_total_close_to_the_real_sum():
    """The leniency is narrow: a number that is merely IN THE VICINITY of
    the real total, but not exactly it, must still fail — this is not a
    fuzzy-match relaxation."""
    result = await verify_structured_aggregate_paraphrase(
        answer="There are 999 FIRs in total.",
        source_text="- PPC: 25 cases\n- CNSA 1997: 4 cases",
        case_id="cross_case",
    )
    assert result["grounded"] is False
    assert "999" in result["reason"]


@pytest.mark.asyncio
async def test_structured_paraphrase_years_inside_statute_names_are_not_summed_in():
    """A source naming "Arms Ordinance 1965" or "PECA 2016" must not let
    those years leak into the grand-total sum — only counts immediately
    followed by case/FIR/record are summed, never a bare number. The real
    sum here is 25 + 4 = 29 (the years are not case counts); a paraphrase
    stating a total that INCLUDES the years (e.g. 4010) must still fail."""
    source_text = "- PPC, Arms Ordinance 1965: 25 cases\n- PECA 2016: 4 cases"

    result_correct = await verify_structured_aggregate_paraphrase(
        answer="There are 29 FIRs in total.", source_text=source_text, case_id="cross_case",
    )
    assert result_correct["grounded"] is True

    result_with_years_leaked_in = await verify_structured_aggregate_paraphrase(
        answer="There are 4010 FIRs in total.", source_text=source_text, case_id="cross_case",
    )
    assert result_with_years_leaked_in["grounded"] is False


@pytest.mark.asyncio
async def test_structured_paraphrase_grand_total_ignores_the_overlapping_per_act_breakdown():
    """The narrowing this fix requires: `_render_aggregate_text()`'s
    "Breakdown by individual legal code" section is a SEPARATE, OVERLAPPING
    per-act count (a case can carry more than one act) - summing past that
    marker would produce a plausible-looking but WRONG number, and must
    never be accepted as a legitimate grand total. Only the first
    (partition) breakdown sums to the real total (79 here); the per-act
    section below it sums to something else entirely and must be ignored."""
    source_text = (
        "- PPC: 25 cases\n- PPC, Arms Ordinance 1965: 21 cases\n"
        "- PECA 2016, PPC: 9 cases\n- CNSA 1997, Arms Ordinance 1965: 8 cases\n"
        "- unknown: 6 cases\n- CNSA 1997: 4 cases\n"
        "- PPC, Punjab Domestic Violence Act: 4 cases\n"
        "- PPC, Illegal Dispossession Act 2005: 2 cases\n\n"
        "Breakdown by individual legal code (a case can involve more than one):\n"
        "- PPC: 61 cases\n- Arms Ordinance 1965: 29 cases\n- CNSA 1997: 12 cases\n"
        "- PECA 2016: 9 cases\n- Punjab Domestic Violence Act: 4 cases\n"
        "- Illegal Dispossession Act 2005: 2 cases"
    )
    # The correct partition total (79) still passes.
    result_correct = await verify_structured_aggregate_paraphrase(
        answer="There are 79 FIRs in total.", source_text=source_text, case_id="cross_case",
    )
    assert result_correct["grounded"] is True

    # The per-act section's own sum (61+29+12+9+4+2=117) is NOT a real
    # total (acts overlap) and must not be treated as one.
    result_wrong = await verify_structured_aggregate_paraphrase(
        answer="There are 117 FIRs in total.", source_text=source_text, case_id="cross_case",
    )
    assert result_wrong["grounded"] is False
