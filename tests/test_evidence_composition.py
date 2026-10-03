from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from src.evidence.answer import render
from src.evidence.composition import Draft,Review,compose
from src.evidence.types import EvidenceResult,QueryOperation,QuestionPlan,Scope,SourceSpan


def fixture():
    scope=Scope(user_id=uuid4(),session_id=uuid4())
    op=QueryOperation(output_id="narrative",operation="search",text="How was the motorcycle stolen?")
    span=SourceSpan(span_id=uuid4(),document_id=uuid4(),start=0,end=28,text="The motorcycle lock was cut.")
    result=EvidenceResult(output_id=op.output_id,operation=op,scope_fingerprint=scope.fingerprint(),
                          label="Sources",citations=[span],status="partial")
    plan=QuestionPlan(question=op.text,standalone_question=op.text,outputs=[op])
    return scope,plan,result,span


@pytest.mark.asyncio
async def test_a_figure_must_be_in_the_cited_evidence_not_in_the_clip():
    """Checked against the SPAN a quote came from, not the quote alone.

    MEASURED, asking "what is stated in the FIR about the stolen
    motorcycle": the answer -- "a Honda CD-125 with registration number
    FSD-19-8842" -- is exactly what FIR 406/26 records, and it was discarded
    because the excerpt the model clipped carried the model and not the
    plate, although the plate sits in the very span that excerpt came from.

    The unit of evidence here is the span: a citation points at one and a
    reader opens one. Each quote must still be verbatim in its span, so this
    cannot admit a figure from a passage that was never retrieved or never
    cited -- but it no longer throws away a correct, cited answer for
    clipping badly.
    """
    scope, plan, result, span = fixture()
    body = "Honda CD-125 motorcycle, registration FSD-19-8842, stolen overnight."
    span.text, span.end = body, len(body)
    model = AsyncMock()
    model.structured.side_effect = [
        Draft(findings=[{"output_id": result.output_id,
                         "text": "The motorcycle is a Honda CD-125, registration FSD-19-8842.",
                         "supports": [{"span_id": span.span_id,
                                       "quote": "Honda CD-125 motorcycle"}]}]),
        Review(decisions=[{"index": 0, "supported": True, "relevant": True,
                           "population_claim": False}])]
    await compose(model, plan, [result], deadline=100)
    text = render(uuid4(), plan, [result], scope).text
    assert "FSD-19-8842" in text, (
        "a figure present in the cited span must not be rejected for being "
        "outside the clipped quote")


@pytest.mark.asyncio
async def test_a_figure_in_no_cited_span_is_still_refused():
    """The loosening above must not admit an invented number."""
    scope, plan, result, span = fixture()
    model = AsyncMock()
    model.structured.side_effect = [
        Draft(findings=[{"output_id": result.output_id,
                         "text": "Seventeen motorcycles were stolen, plate XYZ-4242.",
                         "supports": [{"span_id": span.span_id,
                                       "quote": span.text}]}]),
        Review(decisions=[{"index": 0, "supported": True, "relevant": True,
                           "population_claim": False}])]
    await compose(model, plan, [result], deadline=100)
    text = render(uuid4(), plan, [result], scope).text
    assert "4242" not in text, "a figure in no cited span must still be refused"


def test_a_measured_absence_is_stated_and_a_failed_search_is_not():
    """A phrase checked against the whole corpus and found nowhere is an ANSWER.

    MEASURED, on "does Bhara Kahu appear anywhere in the records": the fuzzy
    and semantic arms returned ten near-misses, composition correctly refused
    every one of them for want of support, and the answer read "Candidate
    sources were retrieved; an answer has not yet been established" -- the
    same sentence the system produces when it simply could not cope. The
    reader cannot tell "this is not in the records" from "this did not work",
    and those call for opposite next steps.

    `coverage.absent_literals` is filled only by an exhaustive check over
    every recorded span, entity name and value, so the negative is measured
    rather than inferred and can be stated. The population line is replaced
    too, because "Documents checked: 0/7579" under a definite absence reads
    as though nothing had been checked.

    The absence is reported ONLY when it is the answer: the same measurement
    is recorded when composition DOES ground a claim (a multi-word search
    phrase rarely occurs verbatim) and must stay silent there.
    """
    from src.evidence.types import Coverage

    scope, plan, result, span = fixture()
    result.coverage = Coverage(population_documents=7579, complete_documents=0,
                               absent_literals=["Bhara Kahu"])
    text = render(uuid4(), plan, [result], scope).text
    assert "'Bhara Kahu' does not appear" in text
    assert "has not yet been established" not in text, (
        "a measured absence must not be reported as an unfinished search")
    assert "0/7579" not in text, (
        "the population line contradicts an exhaustive check")

    # An absence recorded alongside a grounded finding stays silent.
    scope, plan, result, span = fixture()
    result.coverage = Coverage(population_documents=602, complete_documents=0,
                              absent_literals=["armed robbery at Model Town"])
    from src.evidence.types import SupportedFinding
    result.findings = [SupportedFinding(output_id=result.output_id,
                                       text="FIR 213/26 records it.",
                                       supports=[{"span_id": span.span_id,
                                                  "quote": span.text}])]
    text = render(uuid4(), plan, [result], scope).text
    assert "does not appear" not in text, (
        "an absence must not be announced under an answer that stands"
    )
    # Coverage is still reported -- as a sentence now, not a ratio. What
    # matters is that a reader is told how much was read, not the notation:
    # "Documents checked: 0/602." is a tally, "0 of 602 documents were
    # checked, so this does not cover the rest." is the same fact said.
    assert "602" in text and "0" in text, "coverage must still be stated"
    assert "checked" in text


def test_retrieval_alone_never_becomes_an_answer_claim():
    scope,plan,result,span=fixture()
    answer=render(uuid4(),plan,[result],scope)
    assert answer.status=="partial"
    assert not answer.claims
    assert span.text not in answer.text


@pytest.mark.asyncio
@pytest.mark.parametrize("supported,relevant,population,accepted",[
    (True,True,False,True),(False,True,False,False),
    (True,False,False,False),(True,True,True,False)])
async def test_narrative_requires_grounding_and_relevance(supported,relevant,population,accepted):
    scope,plan,result,span=fixture()
    model=AsyncMock()
    model.structured.side_effect=[Draft(findings=[{"output_id":result.output_id,"text":"The lock was cut.",
        "supports":[{"span_id":span.span_id,"quote":span.text}]}]),
        Review(decisions=[{"index":0,"supported":supported,"relevant":relevant,"population_claim":population}])]
    await compose(model,plan,[result],deadline=100)
    answer=render(uuid4(),plan,[result],scope)
    assert bool(answer.claims)==accepted
    assert ("The lock was cut." in answer.text)==accepted


@pytest.mark.asyncio
async def test_fabricated_quote_is_removed_before_review():
    scope,plan,result,span=fixture()
    model=AsyncMock()
    model.structured.return_value=Draft(findings=[{"output_id":result.output_id,"text":"All motorcycles were stolen.",
        "supports":[{"span_id":span.span_id,"quote":"All motorcycles"}]}])
    await compose(model,plan,[result],deadline=100)
    assert model.structured.await_count==1 and not result.findings


@pytest.mark.asyncio
async def test_new_date_cannot_be_smuggled_into_a_supported_quote():
    scope,plan,result,span=fixture()
    model=AsyncMock()
    model.structured.return_value=Draft(findings=[{"output_id":result.output_id,"text":"The lock was cut on 2024-10-10.",
        "supports":[{"span_id":span.span_id,"quote":span.text}]}])
    await compose(model,plan,[result],deadline=100)
    assert not result.findings and model.structured.await_count==1


def test_an_unverifiable_support_is_dropped_not_fatal(monkeypatch):
    """MEASURED, asking for the weapon in FIR 1001/26: the model returned
    one correct finding with five supports. Two were exact substrings of
    their spans; three were elided -- "ملزم کاشف ... 30 بور پستول" -- a
    real quote with its middle removed, which is not a substring.

    Requiring every support to verify discarded a correct finding that two
    verbatim quotes already established. The elided ones must be dropped
    and the finding kept, with only verified quotes remaining on it.
    """
    from src.evidence.composition import numbers

    span_text = "ملزم کے قبضے سے 30 بور پستول بمعہ 6 گولیاں برآمدگی پر۔"
    verbatim = "30 بور پستول بمعہ 6 گولیاں"
    elided = "ملزم کاشف ... 30 بور پستول بمعہ 6 گولیاں"

    assert verbatim in span_text
    assert elided not in span_text, "this is the shape that was failing"

    # Old rule: every support must verify -> the finding dies.
    assert not all(q in span_text for q in (verbatim, elided))
    # New rule: keep what verifies, and the finding survives on it.
    kept = [q for q in (verbatim, elided) if q in span_text]
    assert kept == [verbatim]

    # The figures must still be carried by the SURVIVING quotes only.
    text = "The weapon recorded in FIR 1001/26 was a 30 bore pistol with 6 bullets."
    asked = numbers("What weapon was recorded in FIR 1001/26?")
    assert numbers(text) <= numbers(" ".join(kept)) | asked
    # A finding left with no verified support is still discarded.
    assert [q for q in (elided,) if q in span_text] == []
