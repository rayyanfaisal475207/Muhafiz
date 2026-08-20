"""
scripts/load_real_offense_sections.py — M7 of the Muhafiz Data API
migration (docs/decisions/0001-muhafiz-api-migration.md).
"""
from pathlib import Path

import pytest

import scripts.load_real_offense_sections as loader
from src.data_gateway.muhafiz_api.models import FirRecord

FIXTURE = Path(__file__).parent / "fixtures" / "muhafiz_api_snapshot.json"


async def test_fetch_firs_from_snapshot():
    firs = await loader.fetch_firs(str(FIXTURE))
    assert len(firs) == 73


def test_distinct_section_act_pairs_matches_measured_count():
    """Locks in the measured finding from the decision record: 36 distinct
    (section_code, act) pairs across 6 acts."""
    import json
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    firs = [FirRecord(r) for r in snapshot["endpoints"]["fir"]]

    pairs = loader.distinct_section_act_pairs(firs)

    assert len(pairs) == 36
    acts = {act for _, act in pairs}
    assert acts == {
        "PPC", "Arms Ordinance 1965", "CNSA 1997", "PECA 2016",
        "Illegal Dispossession Act 2005", "Provincial Act",
    }


def test_distinct_section_act_pairs_ignores_rows_missing_either_field():
    fir = FirRecord({
        "fir_id": "fir-1-26",
        "fir_section": [
            {"section_code": "379", "act": "PPC"},
            {"section_code": None, "act": "PPC"},
            {"section_code": "34", "act": None},
        ],
    })
    pairs = loader.distinct_section_act_pairs([fir])
    assert pairs == [("379", "PPC")]


def test_distinct_section_act_pairs_dedupes_across_firs():
    firs = [
        FirRecord({"fir_id": "fir-1-26", "fir_section": [{"section_code": "379", "act": "PPC"}]}),
        FirRecord({"fir_id": "fir-2-26", "fir_section": [{"section_code": "379", "act": "PPC"}]}),
    ]
    pairs = loader.distinct_section_act_pairs(firs)
    assert pairs == [("379", "PPC")]


async def test_load_rows_upserts_idempotently(monkeypatch):
    added = []

    class _FakeScalars:
        def __init__(self, present):
            self._present = present
        def first(self):
            return object() if self._present else None

    class _FakeResult:
        def __init__(self, present):
            self._present = present
        def scalars(self):
            return _FakeScalars(self._present)

    class _FakeSession:
        def __init__(self):
            self.already_present = set()
        async def execute(self, stmt):
            # We can't easily introspect the compiled WHERE values without
            # a real engine, so this fake session simply tracks call count.
            self.calls = getattr(self, "calls", 0) + 1
            return _FakeResult(present=False)
        def add(self, obj):
            added.append(obj)
        async def commit(self):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(loader, "get_session", lambda: _FakeSession())

    inserted, skipped = await loader.load_rows([("379", "PPC"), ("13", "Arms Ordinance 1965")])

    assert inserted == 2
    assert skipped == 0
    assert {row.section_ref for row in added} == {"379 PPC", "13 Arms Ordinance 1965"}
    assert all(row.source_type == "scraped" for row in added)
    assert all(row.description is None for row in added)


async def test_load_rows_skips_already_present(monkeypatch):
    class _FakeScalars:
        def first(self):
            return object()  # always "found"

    class _FakeResult:
        def scalars(self):
            return _FakeScalars()

    class _FakeSession:
        async def execute(self, stmt):
            return _FakeResult()
        def add(self, obj):
            raise AssertionError("must not add a row that's already present")
        async def commit(self):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(loader, "get_session", lambda: _FakeSession())

    inserted, skipped = await loader.load_rows([("379", "PPC")])
    assert inserted == 0
    assert skipped == 1
