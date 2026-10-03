"""What may leave the machine, and what a search result may do when it returns.

The web lookup exists because a term the reader could not resolve used to be
written to the trace and consumed by nothing. It sends data to a third party,
so the properties that make it safe are pinned here rather than left to the
reading of the code.
"""
import importlib

import httpx
import pytest

from src.evidence import web

BODY = {
    "Heading": "Bhara Kahu",
    "AbstractText": "Bhara Kahu, also spelled Barakahu, is a town near Islamabad. "
                    "In Urdu it is written بھارہ کہو.",
    "RelatedTopics": [{"Text": "Islamabad Capital Territory administers the area."}],
}


def enabled(monkeypatch, value="true"):
    monkeypatch.setenv("EVIDENCE_WEB_LOOKUP", value)
    return importlib.reload(web)


def transport(seen):
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=BODY)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_nothing_leaves_unless_it_is_switched_on(monkeypatch):
    """Off unless explicitly enabled, and off means no request at all."""
    module = enabled(monkeypatch, "false")
    seen: list[str] = []
    readings, queries = await module.readings_for("Bhara Kahu", transport=transport(seen))
    assert readings == [] and queries == []
    assert seen == [], "a disabled lookup must not reach the network"


@pytest.mark.asyncio
async def test_only_the_unresolved_term_is_sent_and_it_is_recorded(monkeypatch):
    """The question, the case and the records never leave; the term does.

    The returned queries are what the trace records, so they must be the
    literal strings that went out -- an audit of what was disclosed is
    worthless if it is reconstructed rather than captured.
    """
    module = enabled(monkeypatch)
    seen: list[str] = []
    readings, queries = await module.readings_for("Bhara Kahu", transport=transport(seen))

    assert queries == ["Bhara Kahu", "Bhara Kahu meaning in Urdu"]
    assert len(seen) == 2
    for url in seen:
        assert "Bhara" in url
        # Nothing about the question, the case or the records travels with it.
        for leaked in ("FIR", "1001", "کاشف", "appear anywhere", "case_id"):
            assert leaked not in url
    assert readings, "a usable response should yield candidate spellings"


@pytest.mark.asyncio
async def test_a_reading_is_offered_never_believed(monkeypatch):
    """The module RETURNS candidates; it never decides one is correct.

    MEASURED: asked about "Bhara Kahu" the live endpoint offered "Islamabad
    Capital Territory", "Khyber Pakhtunkhwa" and "Indian Rosewood". Two of
    those occur in the corpus, so an occurrence check alone accepted them.
    Deciding which candidate is a READING of the term belongs to
    `ScopedStore.same_name_forms`, and deciding whether it is USABLE belongs
    to `ground_forms`; this module must return the noise rather than filter
    it, so that both gates see everything.
    """
    module = enabled(monkeypatch)
    readings, _ = await module.readings_for("Bhara Kahu", transport=transport([]))
    assert "Islamabad Capital Territory" in readings, (
        "the module must not pre-filter; the corpus gates decide")
    assert all(r.lower() != "bhara kahu" for r in readings), (
        "the term itself is not a new reading of the term")


@pytest.mark.asyncio
async def test_an_unreachable_network_leaves_the_term_where_it_started(monkeypatch):
    """A failed lookup is not a failed run."""
    module = enabled(monkeypatch)

    def refuse(request):
        raise httpx.ConnectError("no route")

    readings, queries = await module.readings_for(
        "Bhara Kahu", transport=httpx.MockTransport(refuse))
    assert readings == []
    assert queries, "what was attempted is still recorded"


@pytest.mark.asyncio
async def test_how_many_terms_may_be_asked_about_is_bounded(monkeypatch):
    """A question full of unknown words is not repaired by disclosing them all.

    Bounding this bounds the disclosure: the limit is on how much leaves,
    not on how long it takes.
    """
    module = enabled(monkeypatch)
    seen: list[str] = []
    readings, queries = await module.readings_for_all(
        [f"term{i}" for i in range(12)], limit=4, transport=transport(seen))
    assert len({q.split(" meaning")[0] for q in queries}) <= 4
    assert len(seen) <= 8, "four terms, two queries each, and no more"
