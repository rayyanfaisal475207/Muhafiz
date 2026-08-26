"""
scripts/load_legal_code_acts.py — legal-code semantic layer (act-level
descriptions in police_reference_data, distinct from
scripts/load_real_offense_sections.py's section-level rows).
"""
import pytest

import scripts.load_legal_code_acts as loader


async def test_distinct_acts_matches_measured_corpus(monkeypatch):
    """Locks in the same 6-act finding scripts/load_real_offense_sections.py
    already established for this corpus (test_xagg.py's own
    test_distinct_section_act_pairs_matches_measured_count locks the
    section-level count; this is the act-level equivalent)."""
    rows = [
        ("PPC",), ("PPC, Arms Ordinance 1965",), ("PECA 2016, PPC",),
        ("CNSA 1997, Arms Ordinance 1965",), ("CNSA 1997",),
        ("PPC, Provincial Act",), ("PPC, Illegal Dispossession Act 2005",),
        (None,),
    ]

    class _FakeQueryResult:
        def all(self):
            return rows

    class _FakeSession:
        async def execute(self, stmt):
            return _FakeQueryResult()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(loader, "get_session", lambda: _FakeSession())

    acts = await loader.distinct_acts()

    assert set(acts) == {
        "PPC", "Arms Ordinance 1965", "CNSA 1997", "PECA 2016",
        "Illegal Dispossession Act 2005", "Provincial Act",
    }


class _FakeScalars:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def scalars(self):
        return _FakeScalars(self._row)


class _FakeRow:
    """Stand-in for a PoliceReferenceData ORM instance already in the DB."""
    def __init__(self, description=None, source_document=None):
        self.description = description
        self.source_document = source_document


class _FakeSession:
    def __init__(self, existing: dict[str, _FakeRow] | None = None):
        self.existing = existing or {}
        self.added = []
        self.committed = False
        self._current_lookup = None

    async def execute(self, stmt):
        # Can't introspect the compiled WHERE clause without a real engine —
        # same limitation test_load_real_offense_sections_script.py's own
        # fake session accepts. Tests call load_rows() with one act at a
        # time (or pre-seed self.existing keyed by subject) to stay
        # deterministic despite this.
        return _FakeResult(self._current_lookup)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_load_rows_dry_run_makes_no_changes(monkeypatch):
    session = _FakeSession()
    session._current_lookup = None  # nothing present yet
    monkeypatch.setattr(loader, "get_session", lambda: session)

    inserted, updated, unchanged, uncovered = await loader.load_rows(["PPC", "CNSA 1997"], apply=False)

    assert inserted == 2
    assert updated == 0
    assert unchanged == 0
    assert uncovered == ["PPC", "CNSA 1997"]  # neither is in _KNOWN_ACT_DESCRIPTIONS
    assert session.added == []  # dry run: nothing actually added
    assert session.committed is False


async def test_load_rows_apply_inserts_new_acts(monkeypatch):
    session = _FakeSession()
    session._current_lookup = None
    monkeypatch.setattr(loader, "get_session", lambda: session)

    inserted, updated, unchanged, uncovered = await loader.load_rows(["PPC"], apply=True)

    assert inserted == 1
    assert len(session.added) == 1
    row = session.added[0]
    assert row.category == "legal_code_act"
    assert row.subject == "PPC"
    assert row.description is None  # never fabricated — see module docstring
    assert row.source_type == "scraped"
    assert session.committed is True
    assert uncovered == ["PPC"]


async def test_load_rows_idempotent_on_second_run(monkeypatch):
    """Re-running against an act already present, with no description
    change, must not re-add or re-update it."""
    session = _FakeSession()
    session._current_lookup = _FakeRow(description=None, source_document="cases.crime_category")
    monkeypatch.setattr(loader, "get_session", lambda: session)

    inserted, updated, unchanged, uncovered = await loader.load_rows(["PPC"], apply=True)

    assert inserted == 0
    assert updated == 0
    assert unchanged == 1
    assert session.added == []


async def test_load_rows_updates_when_a_description_is_newly_supplied(monkeypatch):
    """A previously-uncovered act (description=NULL in the DB) whose entry
    is later added to _KNOWN_ACT_DESCRIPTIONS gets updated in place on
    re-run, not skipped as already-present."""
    monkeypatch.setitem(
        loader._KNOWN_ACT_DESCRIPTIONS, "PPC",
        ("Pakistan Penal Code 1860 — the general criminal code.", "test-source"),
    )
    session = _FakeSession()
    session._current_lookup = _FakeRow(description=None, source_document="cases.crime_category")
    monkeypatch.setattr(loader, "get_session", lambda: session)

    inserted, updated, unchanged, uncovered = await loader.load_rows(["PPC"], apply=True)

    assert inserted == 0
    assert updated == 1
    assert unchanged == 0
    assert uncovered == []


async def test_load_rows_reports_uncovered_acts_regardless_of_apply(monkeypatch):
    session = _FakeSession()
    session._current_lookup = None
    monkeypatch.setattr(loader, "get_session", lambda: session)

    _, _, _, uncovered_dry = await loader.load_rows(["PPC", "CNSA 1997"], apply=False)
    session2 = _FakeSession()
    session2._current_lookup = None
    monkeypatch.setattr(loader, "get_session", lambda: session2)
    _, _, _, uncovered_apply = await loader.load_rows(["PPC", "CNSA 1997"], apply=True)

    assert uncovered_dry == uncovered_apply == ["PPC", "CNSA 1997"]
