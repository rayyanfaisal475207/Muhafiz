"""
Unit tests for src.pipeline.validation — the Validation trust-layer check
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §5/§7.1/§7.2).

All external calls (call_llm) are monkeypatched; no network, no disk beyond
reading prompts/validation.txt at import time (matching verifier.py's own
test-file posture for its prompt file).
"""

import pytest

from src.pipeline.harness.types import ClaimSupport, ValidationStatus
from src.pipeline.validation import (
    _extract_claim_chunk_pairs,
    validate_answer,
)


def _chunk(text: str, chunk_id: str = "c1") -> dict:
    return {"id": chunk_id, "text": text, "metadata": {"source_tool": "RAG"}}


# ── _extract_claim_chunk_pairs ────────────────────────────────────────────


def test_extract_pairs_splits_on_sentence_boundaries():
    answer = "First claim here [Document 1]. Second claim here [Document 2]."
    chunks = [_chunk("chunk one text"), _chunk("chunk two text", "c2")]
    pairs = _extract_claim_chunk_pairs(answer, chunks)
    assert len(pairs) == 2
    assert pairs[0].document_index == 1
    assert "First claim" in pairs[0].claim_text
    assert pairs[1].document_index == 2
    assert "Second claim" in pairs[1].claim_text


def test_extract_pairs_urdu_sentence_terminator():
    answer = "پہلا دعوی [Document 1]۔ دوسرا دعوی [Document 2]۔"
    chunks = [_chunk("متن ایک"), _chunk("متن دو", "c2")]
    pairs = _extract_claim_chunk_pairs(answer, chunks)
    assert len(pairs) == 2


def test_extract_pairs_one_claim_citing_two_documents_yields_two_pairs():
    answer = "This is supported by both [Document 1] and [Document 2]."
    chunks = [_chunk("first source"), _chunk("second source", "c2")]
    pairs = _extract_claim_chunk_pairs(answer, chunks)
    assert len(pairs) == 2
    assert {p.document_index for p in pairs} == {1, 2}


def test_extract_pairs_out_of_range_citation_is_skipped_not_raised():
    answer = "This cites a document that doesn't exist [Document 5]."
    chunks = [_chunk("only one chunk")]
    pairs = _extract_claim_chunk_pairs(answer, chunks)
    assert pairs == []


def test_extract_pairs_no_citations_yields_empty():
    pairs = _extract_claim_chunk_pairs("No citations in this answer at all.", [_chunk("x")])
    assert pairs == []


# ── validate_answer: no-citations -> SKIPPED ──────────────────────────────


@pytest.mark.asyncio
async def test_validate_answer_skipped_when_no_citations():
    status, results = await validate_answer(
        "Plain uncited text.", [_chunk("irrelevant")], tier="structural"
    )
    assert status == ValidationStatus.SKIPPED
    assert results == []


# ── structural-only tier (deterministic, no LLM) ──────────────────────────


@pytest.mark.asyncio
async def test_structural_tier_passes_when_numbers_and_ids_match():
    answer = "A car was reported stolen in case CASE-011 [Document 1]."
    chunks = [_chunk("A car was reported stolen in case CASE-011.")]
    status, results = await validate_answer(answer, chunks, tier="structural")
    assert status == ValidationStatus.PASSED
    assert results[0].support == ClaimSupport.SUPPORTED


@pytest.mark.asyncio
async def test_structural_tier_flags_unsupported_number():
    answer = "The accused stole property worth 500000 rupees [Document 1]."
    chunks = [_chunk("The accused stole several valuable items.")]
    status, results = await validate_answer(answer, chunks, tier="structural")
    assert status == ValidationStatus.ISSUES_FOUND
    assert results[0].support == ClaimSupport.NOT_SUPPORTED
    assert "500000" in results[0].reason


@pytest.mark.asyncio
async def test_structural_tier_flags_unsupported_case_id():
    answer = "This is also linked to CASE-099 [Document 1]."
    chunks = [_chunk("This case involves a burglary at a residence.")]
    status, results = await validate_answer(answer, chunks, tier="structural")
    assert status == ValidationStatus.ISSUES_FOUND
    assert results[0].support == ClaimSupport.NOT_SUPPORTED
    assert "CASE-099" in results[0].reason


@pytest.mark.asyncio
async def test_structural_tier_never_returns_partially_supported():
    # [PRESERVE -- module docstring] A purely lexical check has no
    # principled basis for "partial" -- only SUPPORTED or NOT_SUPPORTED.
    answer = "The vehicle plate was AB-123 and the case is CASE-050 [Document 1]."
    chunks = [_chunk("The vehicle plate was AB-123 in case CASE-050.")]
    status, results = await validate_answer(answer, chunks, tier="structural")
    assert all(r.support in (ClaimSupport.SUPPORTED, ClaimSupport.NOT_SUPPORTED) for r in results)


@pytest.mark.asyncio
async def test_structural_tier_citation_marker_digit_is_not_treated_as_a_claimed_number():
    # The "1" inside "[Document 1]" itself must never be extracted as a
    # number the source has to contain.
    answer = "The complainant filed a report [Document 1]."
    chunks = [_chunk("The complainant filed a report at the police station.")]
    status, results = await validate_answer(answer, chunks, tier="structural")
    assert status == ValidationStatus.PASSED


@pytest.mark.asyncio
async def test_structural_tier_number_embedded_in_case_id_is_not_double_flagged():
    # The "011" inside "CASE-011" must not be separately checked as a bare
    # number once the CASE-011 token itself matches.
    answer = "This concerns case CASE-011 [Document 1]."
    chunks = [_chunk("Records related to case CASE-011 are on file.")]
    status, results = await validate_answer(answer, chunks, tier="structural")
    assert status == ValidationStatus.PASSED


# ── full semantic tier (LLM call monkeypatched) ───────────────────────────


@pytest.mark.asyncio
async def test_full_tier_all_supported(monkeypatch):
    import src.pipeline.validation as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return '[{"pair_id": 1, "support": "supported", "reason": "Matches directly."}]'

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    status, results = await validate_answer(
        "The accused broke into the house [Document 1].",
        [_chunk("The accused broke into the house on the reported date.")],
        tier="full",
    )
    assert status == ValidationStatus.PASSED
    assert results[0].support == ClaimSupport.SUPPORTED
    assert results[0].reason == "Matches directly."


@pytest.mark.asyncio
async def test_full_tier_issues_found_on_not_supported(monkeypatch):
    import src.pipeline.validation as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return '[{"pair_id": 1, "support": "not_supported", "reason": "Contradicts the source."}]'

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    status, results = await validate_answer(
        "The accused was arrested on the spot [Document 1].",
        [_chunk("The complainant was away when the burglary happened.")],
        tier="full",
    )
    assert status == ValidationStatus.ISSUES_FOUND
    assert results[0].support == ClaimSupport.NOT_SUPPORTED


@pytest.mark.asyncio
async def test_full_tier_partially_supported_counts_as_issues_found(monkeypatch):
    import src.pipeline.validation as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return '[{"pair_id": 1, "support": "partially_supported", "reason": "Adds an unconfirmed detail."}]'

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    status, results = await validate_answer(
        "The stolen jewelry was worth five lakh rupees [Document 1].",
        [_chunk("Several valuable items were reported stolen.")],
        tier="full",
    )
    assert status == ValidationStatus.ISSUES_FOUND
    assert results[0].support == ClaimSupport.PARTIALLY_SUPPORTED


@pytest.mark.asyncio
async def test_full_tier_multiple_pairs_mapped_by_pair_id_not_order(monkeypatch):
    import src.pipeline.validation as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        # Deliberately returned out of order to prove pair_id-based mapping,
        # not positional mapping.
        return (
            '[{"pair_id": 2, "support": "not_supported", "reason": "r2"}, '
            '{"pair_id": 1, "support": "supported", "reason": "r1"}]'
        )

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    status, results = await validate_answer(
        "First claim [Document 1]. Second claim [Document 2].",
        [_chunk("source one"), _chunk("source two", "c2")],
        tier="full",
    )
    assert status == ValidationStatus.ISSUES_FOUND
    assert results[0].document_index == 1
    assert results[0].support == ClaimSupport.SUPPORTED
    assert results[0].reason == "r1"
    assert results[1].document_index == 2
    assert results[1].support == ClaimSupport.NOT_SUPPORTED


# ── fail-OPEN posture (plan §7.1, the opposite of the Verifier) ──────────


@pytest.mark.asyncio
async def test_full_tier_fails_open_on_unparseable_llm_response(monkeypatch):
    import src.pipeline.validation as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        return "THIS IS NOT JSON, EVEN AFTER RETRIES"

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    status, results = await validate_answer(
        "Some claim [Document 1].", [_chunk("some source")], tier="full"
    )
    assert status == ValidationStatus.NOT_RUN
    assert results == []


@pytest.mark.asyncio
async def test_full_tier_fails_open_when_response_omits_a_pair(monkeypatch):
    import src.pipeline.validation as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        # Only answers pair 1 of 2 -- a partial response is not partially
        # trusted, per validate_answer()'s own docstring.
        return '[{"pair_id": 1, "support": "supported", "reason": "r1"}]'

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    status, results = await validate_answer(
        "First [Document 1]. Second [Document 2].",
        [_chunk("source one"), _chunk("source two", "c2")],
        tier="full",
    )
    assert status == ValidationStatus.NOT_RUN
    assert results == []


@pytest.mark.asyncio
async def test_full_tier_fails_open_when_call_llm_raises(monkeypatch):
    import src.pipeline.validation as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        raise RuntimeError("local model unreachable")

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    status, results = await validate_answer(
        "Some claim [Document 1].", [_chunk("some source")], tier="full"
    )
    assert status == ValidationStatus.NOT_RUN
    assert results == []


@pytest.mark.asyncio
async def test_full_tier_never_raises_out_of_validate_answer(monkeypatch):
    import src.pipeline.validation as vmod

    async def fake_call(system_prompt, user_message, **kwargs):
        raise ValueError("unexpected failure shape")

    monkeypatch.setattr(vmod, "call_llm", fake_call)

    # Must not raise -- fail-open means a caller always gets a status back.
    status, results = await validate_answer(
        "Some claim [Document 1].", [_chunk("some source")], tier="full"
    )
    assert status == ValidationStatus.NOT_RUN


# ── local-only posture (plan §7.2) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_tier_never_passes_force_cloud_or_escalate(monkeypatch):
    """
    [PRESERVE -- plan §7.2 "no cloud-escalation path, not even opt-in"]
    Confirms validate_answer()'s full tier calls call_llm_json() without
    force_cloud/escalate_to_cloud_on_failure, mirroring verifier.py's own
    call shape -- see validation.py's own "LOCAL-ONLY, PERMANENTLY"
    docstring section for why this is the correct enforcement point.
    """
    import src.pipeline.validation as vmod

    captured_kwargs = {}

    async def fake_call_llm_json(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return (
            [{"pair_id": 1, "support": "supported", "reason": "ok"}],
            "raw",
        )

    monkeypatch.setattr(vmod, "call_llm_json", fake_call_llm_json)

    await validate_answer("Some claim [Document 1].", [_chunk("some source")], tier="full")

    assert captured_kwargs.get("force_cloud") in (None, False)
    assert captured_kwargs.get("escalate_to_cloud_on_failure") in (None, False)


# ── Finding S regression: number tokenisation false positives ────────────────
# The structural tier compares figures in a claim against its cited chunk by
# literal token. `_NUMBER_RE` used to allow a trailing separator into the
# token, so "arrested on 2024-09-22, the suspect" produced "22," and
# "PECA 2016, PPC" produced "2016," — neither of which can ever match the
# source's own "22"/"2016". Every date or statute year followed by a comma was
# therefore reported as an unverifiable identifier, plastering correct answers
# with "could not be confirmed against its source" warnings (scenario-verify
# Finding S). These tests pin both halves: no false positives on separator-
# adjacent figures, and genuine mismatches still caught.

import pytest

from src.pipeline.validation import _extract_numbers_and_ids


def _missing(claim: str, chunk: str) -> set[str]:
    claim_numbers, claim_ids = _extract_numbers_and_ids(claim, strip_citation_markers=True)
    chunk_numbers, chunk_ids = _extract_numbers_and_ids(chunk, strip_citation_markers=False)
    return (claim_numbers - chunk_numbers) | (claim_ids - chunk_ids)


@pytest.mark.parametrize(
    "claim, chunk",
    [
        # Trailing comma after a date component (the literal Finding S repro).
        ("The suspect was arrested on 2024-09-22, then released.",
         "Arrest recorded 2024-09-22 at the checkpoint."),
        # Statute years in a comma-separated list.
        ("Charged under PECA 2016, PPC and Arms Ordinance 1965, 2005.",
         "Sections cited: PECA 2016; Arms Ordinance 1965; Act 2005."),
        # Trailing comma after a phone number.
        ("Contact 0332-4000032, CNIC 00000-9000034-1.",
         "phone 0332-4000032 CNIC 00000-9000034-1"),
        # Sentence-final period must not be swallowed into the token either.
        ("Six bullets were recovered in 2024.",
         "recovered 6 bullets during the 2024 raid"),
        # Thousands separators are presentation, not a different figure.
        ("A fine of 1,200 rupees was imposed.",
         "fine imposed: 1200 rupees"),
    ],
)
def test_separator_adjacent_figures_are_not_false_positives(claim, chunk):
    assert _missing(claim, chunk) == set()


@pytest.mark.parametrize(
    "claim, chunk, expected",
    [
        ("6 bullets were seized.", "4 bullets were seized.", "6"),
        ("A fine of 5000 rupees applied.", "No fine amount was recorded.", "5000"),
        ("Linked to CASE-999.", "Linked to CASE-111.", "CASE-999"),
    ],
)
def test_genuine_figure_mismatches_are_still_flagged(claim, chunk, expected):
    """The false-positive fix must not weaken real mismatch detection."""
    assert expected in _missing(claim, chunk)
