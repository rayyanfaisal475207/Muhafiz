"""
Tests for the progressive-relaxation retry loop in
src/data_gateway/direct_backend.py's DirectGateway.query_police_reference_data()
(findings.md Module 5 — SQL extractor phrasing brittleness).

The exact-match query itself (_query_police_reference_data_exact) is
untouched verbatim code moved out of the old method body, so these tests
mock it directly rather than faking a SQLAlchemy session/ORM round trip —
what's new here is the retry/drop-order loop around it, so that's what's
under test. (Live behavior against the real Postgres police_reference_data
table was verified separately — see findings.md Module 5's verification
notes.)
"""
import pytest

from src.data_gateway.direct_backend import DirectGateway


def _gateway_with_fake_exact(monkeypatch, responses: dict):
    """
    responses maps a frozenset of the kwargs' (name, value) pairs that
    _query_police_reference_data_exact was called with to the rows it
    should return. Any call not present in `responses` returns [].
    """
    gateway = DirectGateway()
    calls: list[dict] = []

    async def _fake_exact(**kwargs):
        calls.append(dict(kwargs))
        key = frozenset(kwargs.items())
        return responses.get(key, [])

    monkeypatch.setattr(gateway, "_query_police_reference_data_exact", _fake_exact)
    return gateway, calls


def _key(**kwargs):
    return frozenset(kwargs.items())


# ── No filters extracted at all: unchanged short-circuit ───────────────────

@pytest.mark.asyncio
async def test_no_filters_returns_empty_without_any_query(monkeypatch):
    gateway, calls = _gateway_with_fake_exact(monkeypatch, {})

    result = await gateway.query_police_reference_data()

    assert result == []
    assert calls == []


# ── Regression guard: an already-successful full-AND query is untouched ────

@pytest.mark.asyncio
async def test_full_filter_set_already_matches_issues_exactly_one_query(monkeypatch):
    """A query whose full filter set matches today must return exactly what
    it returns today — the relaxation loop must never engage when the first
    exact query already found rows."""
    full = _key(category="theft", subject="movable property", section_ref="379")
    rows = [{"ref_id": "1", "section_ref": "379", "category": "theft"}]
    gateway, calls = _gateway_with_fake_exact(monkeypatch, {full: rows})

    result = await gateway.query_police_reference_data(
        category="theft", subject="movable property", section_ref="379",
    )

    assert result == rows
    assert calls == [{"category": "theft", "subject": "movable property", "section_ref": "379"}]


@pytest.mark.asyncio
async def test_single_filter_that_matches_issues_exactly_one_query(monkeypatch):
    only = _key(section_ref="379")
    rows = [{"ref_id": "1", "section_ref": "379"}]
    gateway, calls = _gateway_with_fake_exact(monkeypatch, {only: rows})

    result = await gateway.query_police_reference_data(section_ref="379")

    assert result == rows
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_single_filter_with_no_match_returns_empty_with_no_relaxation(monkeypatch):
    """Already at a single filter with 0 rows — nothing left to drop, must
    not loop, must not fabricate a result."""
    gateway, calls = _gateway_with_fake_exact(monkeypatch, {})

    result = await gateway.query_police_reference_data(section_ref="302")

    assert result == []
    assert calls == [{"section_ref": "302"}]


# ── Relaxation: the module's real repro shape ───────────────────────────────

@pytest.mark.asyncio
async def test_relaxation_drops_subject_first_then_finds_via_section_ref(monkeypatch):
    """The module's exact repro: category+subject+section_ref all extracted,
    full-AND zero-matches, but section_ref alone matches PPC 379."""
    only_section = _key(category="theft", section_ref="379")
    rows = [
        {"ref_id": "1", "section_ref": "379"},
        {"ref_id": "2", "section_ref": "380"},
    ]
    gateway, calls = _gateway_with_fake_exact(monkeypatch, {only_section: rows})

    result = await gateway.query_police_reference_data(
        category="theft", subject="theft of movable property", section_ref="379",
    )

    assert result == rows
    assert calls == [
        {"category": "theft", "subject": "theft of movable property", "section_ref": "379"},
        {"category": "theft", "section_ref": "379"},
    ], "must drop subject first, keeping category+section_ref, before dropping further"


@pytest.mark.asyncio
async def test_relaxation_falls_all_the_way_to_section_ref_alone(monkeypatch):
    """category+subject+section_ref all extracted; neither the full set nor
    dropping subject alone matches, but section_ref alone does — must drop
    category too (2nd relaxation step) before finding it."""
    section_only = _key(section_ref="379")
    rows = [{"ref_id": "1", "section_ref": "379"}]
    gateway, calls = _gateway_with_fake_exact(monkeypatch, {section_only: rows})

    result = await gateway.query_police_reference_data(
        category="theft", subject="theft of movable property", section_ref="379",
    )

    assert result == rows
    assert calls == [
        {"category": "theft", "subject": "theft of movable property", "section_ref": "379"},
        {"category": "theft", "section_ref": "379"},
        {"section_ref": "379"},
    ]
    assert len(calls) == 3, "at most 2 extra queries beyond the original exact match"


@pytest.mark.asyncio
async def test_no_match_at_any_relaxation_level_returns_empty_never_fabricates(monkeypatch):
    """PPC 302 shape: genuinely absent from the table at every relaxation
    level — must return [] (falls through to RAG upstream), never invent a
    match just because relaxation ran out of filters to drop."""
    gateway, calls = _gateway_with_fake_exact(monkeypatch, {})

    result = await gateway.query_police_reference_data(
        category="murder", subject="murder with a firearm", section_ref="302",
    )

    assert result == []
    assert calls == [
        {"category": "murder", "subject": "murder with a firearm", "section_ref": "302"},
        {"category": "murder", "section_ref": "302"},
        {"section_ref": "302"},
    ]


@pytest.mark.asyncio
async def test_relaxation_without_subject_drops_straight_to_category(monkeypatch):
    """Only category+section_ref extracted (no subject) — subject isn't in
    the filter set at all, so the loop must skip straight to dropping
    category (not waste a query on a no-op drop), leaving section_ref alone
    as the last, strongest filter."""
    section_only = _key(section_ref="379")
    rows = [{"ref_id": "1", "section_ref": "379"}]
    gateway, calls = _gateway_with_fake_exact(monkeypatch, {section_only: rows})

    result = await gateway.query_police_reference_data(category="theft-nomatch", section_ref="379")

    assert result == rows
    assert calls == [
        {"category": "theft-nomatch", "section_ref": "379"},
        {"section_ref": "379"},
    ]


@pytest.mark.asyncio
async def test_relaxation_without_section_ref_stops_at_category_alone(monkeypatch):
    """Only category+subject extracted (no section_ref) — after dropping
    subject, category alone is a single filter; must stop there, never
    relax to zero filters."""
    category_only = _key(category="theft")
    gateway, calls = _gateway_with_fake_exact(monkeypatch, {})  # nothing matches anywhere

    result = await gateway.query_police_reference_data(category="theft", subject="theft of movable property")

    assert result == []
    assert calls == [
        {"category": "theft", "subject": "theft of movable property"},
        {"category": "theft"},
    ], "must stop after dropping subject; never issue a zero-filter query"
