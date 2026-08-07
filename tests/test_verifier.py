"""
Unit tests for src.pipeline.verifier — the Phase 6 grounding gate.

All external calls (call_llm) are monkeypatched; no network, no disk.
"""
import pytest

from src.pipeline.verifier import (
    _check_hedging,
    _check_leakage,
    _check_no_citation,
    _check_temporal,
    _format_chunks_for_verifier,
    verify_grounding,
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


def test_hedging_no_graph_confidence_key_skipped():
    """Chunks without a graph_confidence key are not checked."""
    chunk = _chunk()  # no graph_confidence
    answer = "[Document 1] Definitive statement without hedge."
    assert _check_hedging(answer, [chunk]) == []


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
