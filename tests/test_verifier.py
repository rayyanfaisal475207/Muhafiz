"""
Unit tests for src.pipeline.verifier — the Phase 6 grounding gate.

All external calls (call_llm) are monkeypatched; no network, no disk.
"""
import json

import pytest

from src.pipeline.verifier import (
    CITATION_FORMAT_DEGRADED_KEY,
    EXHAUSTIVE_SCOPE_META_KEY,
    _check_fabricated_case_ids,
    _check_hedging,
    _check_leakage,
    _check_no_citation,
    _check_temporal,
    _format_chunks_for_verifier,
    _is_derived_ratio,
    _identifier_tokens,
    _numbers_in,
    exhaustive_chunk_texts,
    negative_claim_is_supported_by_exhaustive_listing,
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


# ── Global KB corpus (no case-id concept) — live-caught false positive (KB1) ─
#
# Live regression: a plain-RAG answer over the global legal-KB corpus
# (is_global=True, category="legal_procedural_reference", no case_id/
# external_id on any chunk — there is no case-linkage concept for a
# statutory-text document at all) cited "[Document 2, section 4(b)]",
# referencing the statute's own subsection, not a case id. This check
# misread the bracket's second field as an invented CASE-ID and rejected an
# otherwise-correctly-grounded KB1 answer. `final_response.txt` (the prompt
# governing plain RAG answers) never instructs the two-part bracket format
# in the first place — that shape only belongs to
# prompts/cross_case_response.txt's case-linked citations, where the cited
# chunks always carry a real case_id/external_id. Skip the check entirely
# when the chunk set has no case ids to check against.

def test_no_case_id_concept_in_corpus_means_check_is_skipped_entirely():
    chunk_no_ids = {"id": "c1", "text": "Statutory text.", "metadata": {}}
    answer = "The requirement is stated in the statute [Document 2, section 4(b)]."
    assert _check_fabricated_case_ids(answer, [chunk_no_ids]) == []


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


# ═══════════════════════════════════════════════════════════════════════
# Gold-QA fix — GOLD_QA_MASTER_FIX_PLAN.md Module 17 (RC-5): the
# claim-verifier over-rejects true claims phrased differently from the
# retrieved/computed text. Two deterministic pieces:
#   1. `_numbers_in()` must not split a decimal number ("15.0") into two
#      separate integer tokens ("15", "0") at the decimal point.
#   2. `_is_derived_ratio()` / verify_structured_aggregate_paraphrase()
#      must accept a percentage/fraction that is the exact arithmetic
#      result of two counts the source already states (Module 13's
#      rate primitive), the same "narrow, exact-arithmetic" carve-out
#      Module 4 already established for a grand total.
# ═══════════════════════════════════════════════════════════════════════

def test_numbers_in_keeps_a_decimal_number_as_one_token():
    assert _numbers_in("2024 ka ausat 15.0 minute hai") == {"2024", "15.0"}


def test_numbers_in_strip_citations_still_keeps_decimals():
    assert _numbers_in("[Document 1] average is 1401.3 minutes", strip_citations=True) == {"1401.3"}


def test_is_derived_ratio_accepts_exact_percentage_of_two_source_counts():
    # 9 / 73 -> 12.328...% -> rounds to 12.
    assert _is_derived_ratio("12", {"9", "73"}) is True


def test_is_derived_ratio_rejects_a_percentage_off_by_more_than_rounding():
    assert _is_derived_ratio("50", {"9", "73"}) is False


def test_is_derived_ratio_accepts_a_decimal_fraction_at_its_own_precision():
    assert _is_derived_ratio("0.12", {"9", "73"}) is True


def test_is_derived_ratio_false_when_source_has_no_matching_pair():
    assert _is_derived_ratio("12", {"5"}) is False


@pytest.mark.asyncio
async def test_structured_paraphrase_allows_a_correct_derived_percentage():
    """M2's shape: '9 of 73 FIRs (~12%)' — 9 and 73 are literal, 12 is the
    exact round() of their ratio, not an invented number."""
    result = await verify_structured_aggregate_paraphrase(
        answer="Specialized cybercrime units carry 9 of 73 FIRs (~12%) from just 2 stations.",
        source_text="Cybercrime stations: 9 cases. Total: 73 cases across 19 stations.",
        case_id="cross_case",
    )
    assert result["grounded"] is True


@pytest.mark.asyncio
async def test_structured_paraphrase_still_rejects_a_fabricated_percentage():
    """The leniency is narrow: a percentage that is NOT the real ratio of
    any two source counts must still fail."""
    result = await verify_structured_aggregate_paraphrase(
        answer="Specialized cybercrime units carry 9 of 73 FIRs (~85%).",
        source_text="Cybercrime stations: 9 cases. Total: 73 cases across 19 stations.",
        case_id="cross_case",
    )
    assert result["grounded"] is False
    assert "85" in result["reason"]


@pytest.mark.asyncio
async def test_structured_paraphrase_allows_restated_decimal_average_unchanged():
    """A paraphrase that restates the source's own computed average verbatim
    (down to the decimal) must pass — regression guard for the decimal-token
    split this module fixes in `_numbers_in()`."""
    result = await verify_structured_aggregate_paraphrase(
        answer="2024's average reporting delay was 15.0 minutes; 2026's was 1401.3 minutes.",
        source_text="2024 mean reporting delay: 15.0 minutes. 2026 mean reporting delay: 1401.3 minutes.",
        case_id="cross_case",
    )
    assert result["grounded"] is True


# ═══════════════════════════════════════════════════════════════════════
# Gold-QA fix — Module 61: a NEGATIVE INFERENCE over an EXHAUSTIVE listing.
#
# CR3's gold answer asserts a negative — "64/26 has a matching walk-in
# complaint; 65/26 has none" — over a CMS linkage listing that contains
# 64/26 and does not contain 65/26. Module 57 measured the verifier
# rejecting that answer on 7 of 14 live runs with one verbatim reason:
# "The claim about FIR 65/26's absence from the linkage list is inferred
# but not directly supported by Document 3, which only lists linked cases
# without…".
#
# These tests pin the three properties the fix must have SIMULTANEOUSLY —
# the third is not optional, and is the reason the fix is two guarded
# conditions rather than a relaxation:
#   (a) the negative over a listing DECLARED exhaustive is accepted;
#   (b) the same claim over a listing NOT so declared is still rejected;
#   (c) a genuinely hallucinated synthesis is STILL rejected.
# ═══════════════════════════════════════════════════════════════════════

# The real shape `render_cms_fir_linkage()` produces, as it reaches the
# Meta-Analysis verifier: an enumeration of every walk-in CMS complaint and
# the FIR it links to. 64/26 is in it; 65/26 is not.
_CMS_LINKAGE_LISTING = (
    "Of 5 walk-in CMS complaint(s), 4 link to a real FIR via a shared case "
    "tag; 1 has no matching FIR.\n"
    "  - CMS-ISB-2026-0341 → fir-64-26\n"
    "  - CMS-ISB-2026-0355 → fir-71-26\n"
    "  - CMS-ISB-2026-0362 → fir-72-26\n"
    "  - CMS-ISB-2026-0370 → fir-73-26"
)

# The judge's own verbatim rejection reason, from MODULE57_RESULT.md §1.
_CR3_VERBATIM_REJECTION = (
    "The claim about FIR 65/26's absence from the linkage list is inferred "
    "but not directly supported by Document 3, which only lists linked "
    "cases without stating which FIRs are excluded."
)


def _listing_chunk(exhaustive: bool, text: str = _CMS_LINKAGE_LISTING):
    meta = {"source": "Sub-question: which FIRs have a linked walk-in CMS complaint?"}
    if exhaustive:
        meta[EXHAUSTIVE_SCOPE_META_KEY] = True
    return {"id": "subquery-3", "text": text, "metadata": meta}


def _rejecting_llm(claims, reason="One or more claims lack support in the provided chunks."):
    async def fake_call(system_prompt, user_message, **kwargs):
        return json.dumps(
            {
                "grounded": False,
                "off_topic": False,
                "leaked_case_id": None,
                "unsupported_claims": claims,
                "reason": reason,
            }
        )

    return fake_call


# ── The deterministic rule itself ──────────────────────────────────────

def test_negative_over_exhaustive_listing_is_supported():
    assert negative_claim_is_supported_by_exhaustive_listing(
        _CR3_VERBATIM_REJECTION, [_CMS_LINKAGE_LISTING]
    ) is True


def test_negative_over_a_listing_not_declared_exhaustive_is_not_supported():
    """No exhaustive chunk anywhere = no negative inference is licensed."""
    assert negative_claim_is_supported_by_exhaustive_listing(
        _CR3_VERBATIM_REJECTION, []
    ) is False


def test_fabricated_negative_is_not_supported():
    """Claiming a record is absent that the listing ACTUALLY CONTAINS."""
    assert negative_claim_is_supported_by_exhaustive_listing(
        "FIR 64/26's absence from the linkage list is not supported by Document 3.",
        [_CMS_LINKAGE_LISTING],
    ) is False


def test_a_misattribution_claim_is_not_an_absence_claim():
    """Module 17's live hallucination shape — names and counts attributed to
    the wrong chunk. Nothing about it is absence-shaped, so the exhaustive
    rule must not touch it."""
    assert negative_claim_is_supported_by_exhaustive_listing(
        "The answer attributes the name عاصم رشید and a count of 12 cases to "
        "Document 2, which states neither.",
        [_CMS_LINKAGE_LISTING],
    ) is False


def test_a_vague_absence_claim_with_no_identifier_is_not_supported():
    """Nothing checkable in it, so the judge's own verdict stands."""
    assert negative_claim_is_supported_by_exhaustive_listing(
        "The claim that the record is missing from the list is not supported.",
        [_CMS_LINKAGE_LISTING],
    ) is False


def test_identifier_normalisation_matches_across_written_forms():
    """"FIR 65/26", "65/26" and "fir-65-26" are the same record."""
    for form in ("FIR 65/26", "65/26", "fir-65-26", "FIR 65-26"):
        assert "65-26" in _identifier_tokens(form), form


def test_a_noun_phrase_is_not_mistaken_for_an_identifier():
    """"CMS complaint" must not count as naming a checkable record — if it
    did, a vague absence claim would satisfy the identifier requirement."""
    assert _identifier_tokens("no matching FIR record for the CMS complaint") == set()


def test_exhaustive_chunk_texts_only_returns_declared_chunks():
    chunks = [_listing_chunk(exhaustive=False), _listing_chunk(exhaustive=True)]
    assert exhaustive_chunk_texts(chunks) == [_CMS_LINKAGE_LISTING]


# ── (a) accepted over an exhaustive listing ────────────────────────────

@pytest.mark.asyncio
async def test_negative_inference_over_exhaustive_listing_is_accepted(monkeypatch):
    """CR3's exact claim, CR3's exact rejection reason, over a listing the
    caller declared complete: the answer is served."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(vmod, "call_llm", _rejecting_llm([_CR3_VERBATIM_REJECTION]))

    result = await verify_grounding(
        answer=(
            "The two cases were not handled identically. FIR 64/26 has a matching "
            "walk-in complaint linked via case tag CMS-ISB-2026-0341 [Document 1], "
            "whereas FIR 65/26 does not appear in the CMS linkage list at all, so "
            "it has no corresponding walk-in complaint [Document 1]."
        ),
        cited_chunks=[_listing_chunk(exhaustive=True)],
        case_id="cross_case",
    )
    assert result["grounded"] is True
    assert result["exhaustive_negative_override"] is True
    assert result["unsupported_claims"] == []


@pytest.mark.asyncio
async def test_the_complete_listing_marker_reaches_the_judge_prompt():
    """The prompt-side half of the fix: rule 7 keys on this exact marker, so
    it has to actually appear in what the judge is shown."""
    rendered = _format_chunks_for_verifier([_listing_chunk(exhaustive=True)])
    assert "COMPLETE LISTING" in rendered
    assert "COMPLETE LISTING" not in _format_chunks_for_verifier(
        [_listing_chunk(exhaustive=False)]
    )


# ── (b) still rejected without the declaration ─────────────────────────

@pytest.mark.asyncio
async def test_same_claim_over_a_non_exhaustive_listing_is_still_rejected(monkeypatch):
    """Identical answer, identical judge verdict, identical listing text —
    the ONLY difference is that no caller declared it complete. A retrieved
    fragment licenses no negative inference."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(vmod, "call_llm", _rejecting_llm([_CR3_VERBATIM_REJECTION]))

    result = await verify_grounding(
        answer=(
            "FIR 65/26 does not appear in the CMS linkage list at all, so it has "
            "no corresponding walk-in complaint [Document 1]."
        ),
        cited_chunks=[_listing_chunk(exhaustive=False)],
        case_id="cross_case",
    )
    assert result["grounded"] is False
    assert result.get("exhaustive_negative_override") is not True


@pytest.mark.asyncio
async def test_fabricated_negative_over_exhaustive_listing_is_still_rejected(monkeypatch):
    """The listing CONTAINS fir-64-26. An answer asserting 64/26 is absent
    from it is a fabricated negative and must not be rescued."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(
        vmod,
        "call_llm",
        _rejecting_llm(
            ["FIR 64/26's absence from the CMS linkage list is not supported by "
             "Document 1, which lists CMS-ISB-2026-0341 → fir-64-26."]
        ),
    )

    result = await verify_grounding(
        answer="FIR 64/26 has no linked walk-in complaint — it does not appear "
               "in the CMS linkage list [Document 1].",
        cited_chunks=[_listing_chunk(exhaustive=True)],
        case_id="cross_case",
    )
    assert result["grounded"] is False
    assert result.get("exhaustive_negative_override") is not True


# ── (c) a genuinely hallucinated synthesis is STILL rejected ───────────

@pytest.mark.asyncio
async def test_hallucinated_synthesis_is_still_rejected_over_exhaustive_listing(monkeypatch):
    """
    Module 17's live catch, re-run against an EXHAUSTIVE chunk: a
    cross-chunk synthesis that misattributes names and case counts. This is
    the test the brief calls non-optional — the exhaustive declaration must
    buy a correct NEGATIVE and nothing else.
    """
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(
        vmod,
        "call_llm",
        _rejecting_llm(
            [
                "The answer attributes the complainant name سعد الرحمن to FIR "
                "71/26, but Document 1 associates that name with no FIR at all.",
                "The answer states 12 linked complaints; Document 1 states 4.",
            ]
        ),
    )

    result = await verify_grounding(
        answer=(
            "There are 12 linked walk-in complaints [Document 1], and the "
            "complainant سعد الرحمن is recorded against FIR 71/26 [Document 1]."
        ),
        cited_chunks=[_listing_chunk(exhaustive=True)],
        case_id="cross_case",
    )
    assert result["grounded"] is False
    assert result.get("exhaustive_negative_override") is not True
    assert len(result["unsupported_claims"]) == 2


@pytest.mark.asyncio
async def test_a_mixed_rejection_is_not_overturned(monkeypatch):
    """One valid negative AND one real hallucination: the override requires
    EVERY flagged claim to qualify, so the rejection stands."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(
        vmod,
        "call_llm",
        _rejecting_llm(
            [
                _CR3_VERBATIM_REJECTION,
                "The answer states 12 linked complaints; Document 1 states 4.",
            ]
        ),
    )

    result = await verify_grounding(
        answer="FIR 65/26 is absent from the list and there are 12 linked "
               "complaints [Document 1].",
        cited_chunks=[_listing_chunk(exhaustive=True)],
        case_id="cross_case",
    )
    assert result["grounded"] is False


@pytest.mark.asyncio
async def test_override_never_beats_a_deterministic_pre_check(monkeypatch):
    """Cross-case leakage, a fabricated CASE-ID, a missing hedge — none of
    those are negotiable, exhaustive listing or not."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(vmod, "call_llm", _rejecting_llm([_CR3_VERBATIM_REJECTION]))

    chunk = _listing_chunk(exhaustive=True)
    chunk["metadata"]["case_id"] = "CASE-999"
    result = await verify_grounding(
        answer="FIR 65/26 is absent from the linkage list [Document 1].",
        cited_chunks=[chunk],
        case_id="CASE-001",
    )
    assert result["grounded"] is False
    assert result["leaked_case_id"] == "CASE-999"


@pytest.mark.asyncio
async def test_an_off_topic_answer_is_never_overturned(monkeypatch):
    import src.pipeline.verifier as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return json.dumps(
            {
                "grounded": False,
                "off_topic": True,
                "leaked_case_id": None,
                "unsupported_claims": [_CR3_VERBATIM_REJECTION],
                "reason": "Generic non-answer.",
            }
        )

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    result = await verify_grounding(
        answer="FIR 65/26 is absent from the linkage list [Document 1].",
        cited_chunks=[_listing_chunk(exhaustive=True)],
        case_id="cross_case",
    )
    assert result["grounded"] is False


# ── The Verifier and the Validation gate must agree ────────────────────

@pytest.mark.asyncio
async def test_validation_gate_agrees_with_the_verifier_on_the_same_claim(monkeypatch):
    """
    Module 61's second half. Before the fix these two gates disagreed about
    the identical claim: the Verifier refused to serve the answer, while the
    Validation gate (on the runs where the Verifier passed) attached "could
    only be partially confirmed … does not mention FIR 65/26 or its absence
    from the CMS list". Now one imported rule governs both.
    """
    import src.pipeline.validation as valmod
    from src.pipeline.harness.types import ValidationStatus

    async def fake_call(system_prompt, user_message, **kwargs):
        return json.dumps(
            [
                {
                    "pair_id": 1,
                    "support": "partially_supported",
                    "reason": (
                        "The source confirms the CMS linkage for FIR 64/26 but does "
                        "not mention FIR 65/26 or its absence from the CMS list."
                    ),
                }
            ]
        )

    monkeypatch.setattr(valmod, "call_llm", fake_call)

    answer = "FIR 65/26 does not appear in the CMS linkage list [Document 1]."
    status, claims = await valmod.validate_answer(
        answer_text=answer,
        cited_chunks=[_listing_chunk(exhaustive=True)],
        tier="full",
    )
    assert status == ValidationStatus.PASSED
    assert valmod.caveats_for_validation(status, claims) == []


@pytest.mark.asyncio
async def test_validation_gate_still_caveats_without_the_exhaustive_marker(monkeypatch):
    """The counterpart: no declaration, no upgrade, caveat preserved."""
    import src.pipeline.validation as valmod
    from src.pipeline.harness.types import ValidationStatus

    async def fake_call(system_prompt, user_message, **kwargs):
        return json.dumps(
            [{"pair_id": 1, "support": "partially_supported",
              "reason": "Does not mention FIR 65/26 or its absence."}]
        )

    monkeypatch.setattr(valmod, "call_llm", fake_call)

    status, claims = await valmod.validate_answer(
        answer_text="FIR 65/26 does not appear in the CMS linkage list [Document 1].",
        cited_chunks=[_listing_chunk(exhaustive=False)],
        tier="full",
    )
    assert status == ValidationStatus.ISSUES_FOUND
    assert valmod.caveats_for_validation(status, claims)


@pytest.mark.asyncio
async def test_validation_gate_still_caveats_a_real_hallucination(monkeypatch):
    """An exhaustive listing does not silence the validation gate generally."""
    import src.pipeline.validation as valmod
    from src.pipeline.harness.types import ValidationStatus

    async def fake_call(system_prompt, user_message, **kwargs):
        return json.dumps(
            [{"pair_id": 1, "support": "not_supported",
              "reason": "The source states 4 linked complaints, not 12."}]
        )

    monkeypatch.setattr(valmod, "call_llm", fake_call)

    status, claims = await valmod.validate_answer(
        answer_text="There are 12 linked walk-in complaints [Document 1].",
        cited_chunks=[_listing_chunk(exhaustive=True)],
        tier="full",
    )
    assert status == ValidationStatus.ISSUES_FOUND
    assert valmod.caveats_for_validation(status, claims)


# ── Direction: which record does the absence phrase name? ──────────────
# Measured live (MODULE61_RESULT.md §4): the judge writes absence both ways
# round — "…does not mention FIR 65/26" names its record AFTER the phrase,
# "FIR 64/26 … does not appear" names it BEFORE. Resolving both the same way
# would, on a sentence naming one present and one absent record, pick the
# wrong one — which is precisely how a fabricated negative would get
# confirmed.

@pytest.mark.parametrize(
    "claim",
    [
        # The verbatim reason Module 57 recorded, subject BEFORE the phrase.
        _CR3_VERBATIM_REJECTION,
        # Both live pre-fix rejection reasons captured on this branch's own
        # control run (§4's before-table).
        "Claims about FIR 65/26 absence from the linkage list and the "
        "discrepancy in handling are not supported by any cited chunk.",
        "The claim about FIR 65/26 lacking a complaint linkage relies on "
        "absence from Document 3's list, which is not explicitly stated in "
        "the chunk.",
        # The live validation-gate reasons, subject AFTER the phrase.
        "The source confirms FIR 64/26 is linked to a CMS complaint but does "
        "not mention FIR 65/26 or its absence from the linkage list.",
        "The source confirms FIR 64/26 is linked to a CMS complaint but does "
        "not mention FIR 65/26 at all, making the claim about FIR 65/26 "
        "unsupported.",
    ],
)
def test_every_live_captured_absence_reason_resolves_to_the_absent_record(claim):
    assert negative_claim_is_supported_by_exhaustive_listing(
        claim, [_CMS_LINKAGE_LISTING]
    ) is True


@pytest.mark.parametrize(
    "claim",
    [
        # Subject BEFORE, and it is a record the listing CONTAINS.
        "FIR 64/26 absence from the CMS linkage list is not supported by "
        "Document 1, which lists CMS-ISB-2026-0341 to fir-64-26.",
        # Subject AFTER, same fabrication the other way round.
        "Document 1 does not mention FIR 64/26.",
        # A live validation reason about a STATUTE, not a listing membership:
        # "PECA 2016" must not be mistaken for a record id.
        "The source confirms the accused is linked to both FIR 64/26 and "
        "65/26, but does not mention PECA 2016 or any statute in the "
        "provided text.",
        # The live G1 rejection reason (§7) — no absence phrase resolves.
        "Two claims lack explicit support in the cited chunks: the alleged "
        "data discrepancy and the 73-case total for seized property.",
    ],
)
def test_reasons_that_must_not_be_rescued(claim):
    assert negative_claim_is_supported_by_exhaustive_listing(
        claim, [_CMS_LINKAGE_LISTING]
    ) is False


def test_a_statute_year_is_never_treated_as_the_absent_record():
    """"PECA 2016" is a statute, not a record id. Only composite identifiers
    ("65-26", "cms-isb-2026-0341") can be the subject of an absence claim —
    otherwise any bare four-digit year in the claim would resolve as a
    record that happens not to be in the listing."""
    from src.pipeline.verifier import _identifier_spans

    assert _identifier_spans("does not mention PECA 2016") == []
    assert _identifier_spans("does not mention FIR 65/26")


# ── Absence is absence FROM A PARTICULAR REGISTER ──────────────────────
# The live bug this pins: CR3 hands the gates THREE complete listings at
# once. FIR 65/26 legitimately appears in the filtered-FIR listing and is
# genuinely absent from the CMS linkage listing. Checking them pooled made
# every correct negative look fabricated, and the validation caveat stayed
# on 5 of 5 runs (MODULE61_RESULT.md §4b).

_FILTERED_FIR_LISTING = (
    "2 FIR(s) match statute PECA 2016 at the cyber-crime circles: "
    "fir-64-26, fir-65-26."
)


def _cr3_chunks():
    """The real three-chunk shape, in the real order."""
    return [
        {"id": "subquery-1", "text": _FILTERED_FIR_LISTING,
         "metadata": {"source": "Sub-question: which FIRs?",
                      EXHAUSTIVE_SCOPE_META_KEY: True}},
        {"id": "subquery-2", "text": "4 recurring Person(s): عاصم رشید appears in 2 cases.",
         "metadata": {"source": "Sub-question: recurring persons?",
                      EXHAUSTIVE_SCOPE_META_KEY: True}},
        {"id": "subquery-3", "text": _CMS_LINKAGE_LISTING,
         "metadata": {"source": "Sub-question: CMS linkage?",
                      EXHAUSTIVE_SCOPE_META_KEY: True}},
    ]


def test_a_claim_is_checked_against_the_listing_it_names():
    from src.pipeline.verifier import listings_a_claim_is_about

    chunks = _cr3_chunks()
    assert listings_a_claim_is_about(_CR3_VERBATIM_REJECTION, chunks) == [
        _CMS_LINKAGE_LISTING
    ]
    # No document named: fall back to every listing, which is strictly more
    # conservative than guessing one.
    assert len(listings_a_claim_is_about("FIR 65/26 is absent.", chunks)) == 3


def test_a_claim_pinned_to_a_non_exhaustive_document_licenses_nothing():
    from src.pipeline.verifier import listings_a_claim_is_about

    chunks = _cr3_chunks()
    chunks[2]["metadata"].pop(EXHAUSTIVE_SCOPE_META_KEY)
    assert listings_a_claim_is_about(_CR3_VERBATIM_REJECTION, chunks) == []


@pytest.mark.asyncio
async def test_the_negative_survives_a_sibling_listing_that_names_the_record(monkeypatch):
    """65/26 IS in Document 1 and is NOT in Document 3. The claim is about
    Document 3, so it is supported — even though a sibling listing names the
    same record for a different question."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(vmod, "call_llm", _rejecting_llm([_CR3_VERBATIM_REJECTION]))

    result = await verify_grounding(
        answer="FIR 65/26 does not appear in the CMS linkage list [Document 3].",
        cited_chunks=_cr3_chunks(),
        case_id="cross_case",
    )
    assert result["grounded"] is True
    assert result["exhaustive_negative_override"] is True


@pytest.mark.asyncio
async def test_a_negative_about_a_listing_that_contains_the_record_still_fails(monkeypatch):
    """The same three chunks, but the claim is about Document 1 — which DOES
    contain fir-65-26. That is a fabricated negative and must stand rejected."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(
        vmod,
        "call_llm",
        _rejecting_llm(
            ["The claim about FIR 65/26's absence is not supported by Document 1."]
        ),
    )

    result = await verify_grounding(
        answer="FIR 65/26 does not appear in the FIR listing [Document 1].",
        cited_chunks=_cr3_chunks(),
        case_id="cross_case",
    )
    assert result["grounded"] is False


@pytest.mark.asyncio
async def test_validation_upgrade_uses_the_claims_own_document(monkeypatch):
    """The live shape from §4b: the flagged claim cites [Document 3], and its
    reason is the CMS-linkage negative. The caveat must disappear."""
    import src.pipeline.validation as valmod
    from src.pipeline.harness.types import ValidationStatus

    async def fake_call(system_prompt, user_message, **kwargs):
        return json.dumps(
            [{"pair_id": 1, "support": "partially_supported",
              "reason": ("The source confirms FIR 64/26 is linked to "
                         "CMS-ISB-2026-0341 but does not mention FIR 65/26 at "
                         "all, so the claim about its absence is unsupported.")}]
        )

    monkeypatch.setattr(valmod, "call_llm", fake_call)

    status, claims = await valmod.validate_answer(
        answer_text="FIR 65/26 does not appear in the CMS linkage list [Document 3].",
        cited_chunks=_cr3_chunks(),
        tier="full",
    )
    assert status == ValidationStatus.PASSED
    assert valmod.caveats_for_validation(status, claims) == []


# ============================================================
# [Gold-QA fix - Module 101] Attribution by source name
#
# The live KB9 answer that `_check_no_citation()` discarded is reproduced
# below VERBATIM (`docs/gold-qa-wave2-results/module101_before.json`, run 1),
# together with the real source labels of the window it was generated from.
# The three "is served" tests fail on the code as it stood before this module
# and pass after; the five "is still rejected" tests pass in BOTH directions,
# which is what makes them a guard rather than a restatement of the change.
# ============================================================

_M101_KB9_SOURCES = [
    "1_1898_Code_of_Criminal_Procedure_(Pakistan).pdf",
    "4_Punjab-Police-Rules-III.pdf",
    "our own case records (cross-case aggregate)",
]

# The composed data-half chunk's own rendering, from
# `xagg.py::render_fir_section_case_count` on the live corpus.
_M101_DATA_HALF_TEXT = (
    "10 of the 73 FIR(s) that carry a recorded section cite PPC 302.\n"
    "  - fir-202-26, fir-213-26, fir-214-26, fir-218-26, fir-340-25\n"
    "For context, the sections most often cited:\n"
    "  - PPC 34: 40 FIR(s)\n"
    "  - PPC 302: 10 FIR(s)\n"
)

# Verbatim, the answer the shipped verifier threw away.
_M101_KB9_ANSWER = (
    "### Legal Requirement:\n"
    "According to the **Code of Criminal Procedure (Pakistan)**, when a person "
    "dies while in the custody of the police or under certain other conditions, "
    "the police are required to conduct an inquiry into the cause of death. This "
    "is outlined in **Section 174** of the Code of Criminal Procedure. "
    "Additionally, **Section 176** provides that in cases where a person dies "
    "while in police custody, the nearest magistrate empowered to hold inquests "
    "shall conduct an inquiry into the cause of death.\n\n"
    "### Case Records Summary:\n"
    "According to **our own case records (cross-case aggregate)**, **10 of the 73 "
    "FIR(s)** that carry a recorded section cite **PPC 302**, which pertains to "
    "murder. However, the summary does **not** provide specific information on "
    "whether the police conducted detailed inquiries into the cause of death."
)


def _m101_chunks():
    return [
        {"id": "cr-pc-35",
         "text": "174. Police to inquire and report on suicide, etc.",
         "metadata": {"source": _M101_KB9_SOURCES[0]}},
        {"id": "ppr-1561",
         "text": "25.31. Information of a death in suspicious circumstances.",
         "metadata": {"source": _M101_KB9_SOURCES[1]}},
        {"id": "kb-data-half:death_investigation_charging",
         "text": _M101_DATA_HALF_TEXT,
         "metadata": {"source": _M101_KB9_SOURCES[2], "source_tool": "XAGG"}},
    ]


def _m101_passing_llm(reason="All claims are directly supported by cited chunks."):
    async def fake_call(system_prompt, user_message, **kwargs):
        return json.dumps(
            {
                "grounded": True,
                "off_topic": False,
                "leaked_case_id": None,
                "unsupported_claims": [],
                "reason": reason,
            }
        )

    return fake_call


def test_module101_normalises_an_ingest_filename_to_the_form_prose_writes():
    from src.pipeline.verifier import _normalise_source_label

    assert (
        _normalise_source_label("1_1898_Code_of_Criminal_Procedure_(Pakistan).pdf")
        == "code of criminal procedure (pakistan)"
    )
    assert _normalise_source_label("4_Punjab-Police-Rules-III.pdf") == "punjab police rules iii"
    assert (
        _normalise_source_label("our own case records (cross-case aggregate)")
        == "our own case records (cross case aggregate)"
    )


def test_module101_a_label_too_short_or_generic_is_refused():
    """The floor is what stops the exemption being trivially satisfiable: an
    answer containing the word "unknown" must never count as attribution."""
    from src.pipeline.verifier import _normalise_source_label

    assert _normalise_source_label("unknown") is None
    assert _normalise_source_label("entity_graph") is None
    assert _normalise_source_label("") is None
    assert _normalise_source_label(None) is None
    # Three tokens, but under the character floor.
    assert _normalise_source_label("a_b_c.pdf") is None


def test_module101_finds_the_named_source_across_separator_differences():
    from src.pipeline.verifier import _answer_names_a_cited_source

    assert (
        _answer_names_a_cited_source(_M101_KB9_ANSWER, _m101_chunks())
        == "code of criminal procedure (pakistan)"
    )
    assert (
        _answer_names_a_cited_source(
            "As Punjab Police Rules-III requires, the register is permanent.",
            _m101_chunks(),
        )
        == "punjab police rules iii"
    )
    # The composed data-half chunk's own label is recognised like any other.
    assert (
        _answer_names_a_cited_source(
            "According to our own case records (cross-case aggregate), 10 FIRs cite PPC 302.",
            _m101_chunks(),
        )
        == "our own case records (cross case aggregate)"
    )


def test_module101_an_answer_naming_no_cited_source_is_not_attribution():
    from src.pipeline.verifier import _answer_names_a_cited_source

    assert (
        _answer_names_a_cited_source(
            "Under the Anti-Terrorism Act 1997 the accused must be produced within 24 hours.",
            _m101_chunks(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_module101_kb9_answer_is_served_instead_of_discarded(monkeypatch):
    """The defect, as a test. Before this module the identical call returned
    grounded=False / off_topic=True and the sub-agent discarded the answer."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(vmod, "call_llm", _m101_passing_llm())

    result = await verify_grounding(
        answer=_M101_KB9_ANSWER,
        cited_chunks=_m101_chunks(),
        case_id=None,
    )
    assert result["grounded"] is True
    assert result["off_topic"] is False
    assert result[CITATION_FORMAT_DEGRADED_KEY] is True
    assert result["named_source"] == "code of criminal procedure (pakistan)"
    assert result["refusal_detected"] is False


@pytest.mark.asyncio
async def test_module101_the_exemption_is_never_silent(monkeypatch):
    """What the exemption gives up (claim-level traceability) has to reach the
    caller, or the reader is told the answer is better sourced than it is."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(vmod, "call_llm", _m101_passing_llm())
    result = await verify_grounding(
        answer=_M101_KB9_ANSWER, cited_chunks=_m101_chunks(), case_id=None
    )
    assert result.get(CITATION_FORMAT_DEGRADED_KEY) is True


@pytest.mark.asyncio
async def test_module101_a_cited_answer_is_not_flagged_as_degraded(monkeypatch):
    """The ordinary case is unchanged: an answer that DOES carry [Document N]
    never reaches this code at all."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(vmod, "call_llm", _m101_passing_llm())
    result = await verify_grounding(
        answer=(
            "Section 174 of the Code of Criminal Procedure (Pakistan) requires an "
            "inquiry into the cause of death [Document 1]. Our own records show 10 "
            "of 73 FIRs cite PPC 302 [Document 3]."
        ),
        cited_chunks=_m101_chunks(),
        case_id=None,
    )
    assert result["grounded"] is True
    assert CITATION_FORMAT_DEGRADED_KEY not in result


@pytest.mark.asyncio
async def test_module101_an_evasive_answer_naming_nothing_is_still_rejected(monkeypatch):
    """Condition (a). The check's original target, a long answer that names no
    source it was given, is untouched."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(vmod, "call_llm", _m101_passing_llm())
    result = await verify_grounding(
        answer=(
            "Police procedure in such matters is generally governed by the "
            "applicable criminal statutes and departmental standing orders. In "
            "practice the officer in charge would open an inquiry, record the "
            "circumstances, and forward the matter onward for further action by "
            "the competent authority as the situation may require."
        ),
        cited_chunks=_m101_chunks(),
        case_id=None,
    )
    assert result["grounded"] is False
    assert result["off_topic"] is True
    assert "cites no [Document N]" in result["reason"]


@pytest.mark.asyncio
async def test_module101_a_fabrication_is_still_rejected_even_when_it_names_a_source(monkeypatch):
    """Condition (b), and the brief's non-negotiable. Module 82's forced
    fabrication shape, an invented rule number and an invented FIR id,
    rewritten so it ALSO names a real cited source and therefore satisfies
    (a). The judge flags it, so it must still be rejected."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(
        vmod,
        "call_llm",
        _rejecting_llm(
            ["Rule 27.41(3) appears in no cited chunk.",
             "FIR 512/26 is not in any cited chunk."]
        ),
    )
    result = await verify_grounding(
        answer=(
            "Under rule 27.41(3) of the Punjab Police Rules-III, every article of "
            "case property must be destroyed exactly seven years after the register "
            "is closed. Our own case records (cross-case aggregate) are fully "
            "compliant: the audit confirmed no entry has ever been retained past "
            "that limit."
        ),
        cited_chunks=_m101_chunks(),
        case_id=None,
    )
    assert result["grounded"] is False
    assert CITATION_FORMAT_DEGRADED_KEY not in result
    assert result["unsupported_claims"]


@pytest.mark.asyncio
async def test_module101_a_genuine_refusal_is_never_exempted(monkeypatch):
    """`_check_refusal()` is evaluated separately and is not part of the
    exemption at all, so a refusal that happens to name a source still fails."""
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(vmod, "call_llm", _m101_passing_llm())
    result = await verify_grounding(
        answer=(
            "I cannot answer this question. The Code of Criminal Procedure "
            "(Pakistan) material is not publicly available and I do not have "
            "access to the information required to respond to this request in any "
            "meaningful way whatsoever."
        ),
        cited_chunks=_m101_chunks(),
        case_id=None,
    )
    assert result["grounded"] is False
    assert result["refusal_detected"] is True
    assert CITATION_FORMAT_DEGRADED_KEY not in result


@pytest.mark.asyncio
async def test_module101_a_deterministic_pre_check_still_overrules_the_exemption(monkeypatch):
    """A temporal finding (the chunk is not yet in force) is a deterministic
    pre-check, and it must beat the exemption even though the answer names a
    real cited source and the judge cleared every claim.

    `_check_fabricated_case_ids()` cannot be used for this test and that is a
    finding, not a shortcut: it only inspects `[Document N, CASE-ID]`
    citations, so by construction it can never fire on an answer that carries
    no `[Document N]` marker at all. That is why the exemption is gated on the
    judge as well as on the source name -- the one deterministic check aimed
    at invented identifiers is blind on exactly this path (filed as Module 102).
    """
    import src.pipeline.verifier as vmod

    monkeypatch.setattr(vmod, "call_llm", _m101_passing_llm())
    chunks = _m101_chunks()
    chunks[0]["metadata"]["effective_from"] = 2030

    result = await verify_grounding(
        answer=_M101_KB9_ANSWER,
        cited_chunks=chunks,
        case_id=None,
        target_date=2026,
    )
    assert result["grounded"] is False
    assert CITATION_FORMAT_DEGRADED_KEY not in result
