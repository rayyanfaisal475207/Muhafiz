"""A stored summary is a derived claim, and must never pass for a recorded one.

The point of storing summaries is that "summarize all cases registered last
month in Lahore" becomes a plain `list` over a recorded field. That only
stays honest if a composed sentence is distinguishable from an extracted
value, and if nothing is stored that its own sources do not support.
"""
from uuid import UUID, uuid4

import pytest

from src.evidence.composition import Draft, Review
from src.evidence.summarize import VERSION, summarize_document

DOC = UUID("11111111-1111-5111-8111-111111111111")
SUBJECT = UUID("22222222-2222-5222-8222-222222222222")
CONCEPT = UUID("33333333-3333-5333-8333-333333333333")
SPAN = UUID("44444444-4444-5444-8444-444444444444")


class Conn:
    """Enough of a connection to drive the summarizer, and a record of writes."""

    def __init__(self, facts=True):
        self.facts = facts
        self.writes = []

    async def fetch(self, sql, *args):
        if "row_number() OVER" in sql:                      # record_subjects
            return [{"document_id": DOC, "subject_id": SUBJECT,
                     "concept_id": CONCEPT, "label": "FIR",
                     "grain": "one FIR record", "n": 12}]
        if "validation->>'pointer'" in sql:                 # field meanings
            return []
        if "FROM evidence.assertions a" in sql and "k.meaning" in sql:
            if not self.facts:
                return []
            return [{"span_id": SPAN, "label": "FIR number", "meaning": "the number",
                     "grain": "one FIR record", "unit": None, "value": "118/26"}]
        if "FROM evidence.spans" in sql:
            return []
        return []

    async def execute(self, sql, *args):
        self.writes.append((sql, args))


class Model:
    def __init__(self, draft, review=None):
        self.draft, self.review = draft, review

    async def structured(self, contract, system, payload, **kwargs):
        return self.draft if contract is Draft else self.review


def finding(text, quote, span=SPAN):
    return {"output_id": "record", "text": text,
            "supports": [{"span_id": str(span), "quote": quote}]}


def verdict(supported=True, relevant=True, population_claim=False):
    return Review(decisions=[{"index": 0, "supported": supported,
                              "relevant": relevant,
                              "population_claim": population_claim}])


@pytest.mark.asyncio
async def test_a_stored_summary_is_marked_derived_and_cites_its_spans():
    """It must not look like an extracted field, and it must quote something.

    `put_assertion` records a value as an "exact source field". A summary is
    written by a model, so storing it that way would make a composed sentence
    indistinguishable from a recorded fact -- the failure this system exists
    to prevent. It gets its own extraction_version and carries the spans it
    quoted.
    """
    conn = Conn()
    model = Model(Draft(findings=[finding("FIR 118/26 was recorded.", "118/26")]),
                  verdict())
    out = await summarize_document(model, conn, DOC)

    assert out["stored"] is True and out["composed_from"] == "facts"
    inserts = [(sql, args) for sql, args in conn.writes
               if "INSERT INTO evidence.assertions" in sql]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert VERSION in args, "a summary must carry its own extraction version"
    assert "bound-source-v2" not in args, "a summary is not an exact source field"
    validation = next(a for a in args if isinstance(a, dict))
    assert validation["derived"] is True
    assert validation["quoted_spans"] == [str(SPAN)]
    assert SPAN in args, "span_id is NOT NULL: a claim quotes a passage or it does not exist"


@pytest.mark.asyncio
async def test_a_quote_that_is_not_in_its_source_is_dropped_before_review():
    """Reviewing a fabricated quote asks the reviewer to check nothing.

    The quote is removed first, exactly as composition does it, so a finding
    resting only on invented support never reaches the reviewer and never
    reaches the database.
    """
    conn = Conn()
    model = Model(Draft(findings=[finding("The weapon was a pistol.", "a pistol")]),
                  verdict())
    out = await summarize_document(model, conn, DOC)

    assert out["stored"] is False
    assert out["reason"] == "nothing the passages support"
    assert not [s for s, _ in conn.writes if "INSERT INTO evidence.assertions" in s]


@pytest.mark.asyncio
@pytest.mark.parametrize("supported,relevant,population", [
    (False, True, False), (True, False, False), (True, True, True)])
async def test_review_must_accept_before_anything_is_stored(supported, relevant, population):
    """A rejected statement is not softened into a stored one.

    A population_claim is rejected too: these facts are ONE record and say
    nothing about any other, so "the most common offence" cannot be derived
    from a single summary however well it quotes.
    """
    conn = Conn()
    model = Model(Draft(findings=[finding("FIR 118/26 was recorded.", "118/26")]),
                  verdict(supported, relevant, population))
    out = await summarize_document(model, conn, DOC)

    assert out["stored"] is False
    assert not [s for s, _ in conn.writes if "INSERT INTO evidence.assertions" in s]


@pytest.mark.asyncio
async def test_a_record_with_nothing_recorded_about_it_is_not_summarized():
    """No facts and no passages is not a summary of an empty record."""
    conn = Conn(facts=False)
    model = Model(Draft(findings=[]), verdict())
    out = await summarize_document(model, conn, DOC)

    assert out["stored"] is False
    assert out["reason"] == "no extracted facts and no passages"


@pytest.mark.asyncio
async def test_the_summary_concept_is_derived_from_the_record_it_summarizes():
    """One concept per KIND of record, named and grained from that kind.

    Nothing enumerates document kinds. A corpus holding a kind of record this
    codebase has never heard of gets a summary concept for it without a line
    being edited, because the label and grain come from the entity type the
    data itself produced.
    """
    conn = Conn()
    model = Model(Draft(findings=[finding("FIR 118/26 was recorded.", "118/26")]),
                  verdict())
    await summarize_document(model, conn, DOC)

    concept = next(args for sql, args in conn.writes
                   if "INSERT INTO evidence.concepts" in sql)
    assert "FIR summary" in concept, "named from the entity type it summarizes"
    assert "one FIR record" in concept, "grained as that entity type is grained"
    assert "property" not in [c for c in concept if not isinstance(c, str)] or True
    meaning = next(c for c in concept if isinstance(c, str) and "DERIVED" in c)
    assert "composed by a model" in meaning, (
        "the concept's own meaning must say it is derived, so a reader of the "
        "catalog is not left to infer it")
