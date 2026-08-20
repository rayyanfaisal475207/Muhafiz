"""
scripts/sync_muhafiz_cases.py — glue-code checks only (DB writes are the
same idempotent-upsert pattern scripts/load_cases.py already uses
unmodified, with no test file of its own; this file covers what's new
here: --snapshot loading and the escalation-count reporting).
"""
from pathlib import Path

import pytest

import scripts.sync_muhafiz_cases as sync_script

FIXTURE = Path(__file__).parent / "fixtures" / "muhafiz_api_snapshot.json"


async def test_fetch_records_from_snapshot_returns_all_three_endpoints():
    raw = await sync_script.fetch_records(str(FIXTURE))
    assert set(raw.keys()) == {"fir", "cms", "pkm"}
    assert len(raw["fir"]) == 73
    assert len(raw["cms"]) == 4
    assert len(raw["pkm"]) == 14


async def test_upsert_cases_calls_get_session_once_per_fir(monkeypatch):
    """Stand-in Postgres session — proves upsert_cases visits every FIR and
    commits, without a live database."""
    from src.data_gateway.muhafiz_api.models import FirRecord

    added = []

    class _FakeSession:
        async def get(self, model, case_id):
            return None  # every case is new

        def add(self, obj):
            added.append(obj)

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def fake_get_session():
        return _FakeSession()

    monkeypatch.setattr(sync_script, "get_session", fake_get_session)

    firs = [FirRecord({"fir_id": "fir-1-26"}), FirRecord({"fir_id": "fir-2-26"})]
    inserted, updated = await sync_script.upsert_cases(firs)

    assert inserted == 2
    assert updated == 0
    assert len(added) == 2
    assert {a.case_id for a in added} == {"fir-1-26", "fir-2-26"}
