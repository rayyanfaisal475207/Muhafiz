"""
Tests for scripts/collapse_same_document_duplicate_persons.py (findings.md
Module 11). No real AGE/Postgres — age_client.execute_cypher and
src.api.graph_review.confirm_match are monkeypatched.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import scripts.collapse_same_document_duplicate_persons as collapse_script


def _edge(edge_id=1, tier="flagged_unverified", case_id="CASE-001", doc_id="DOC-1"):
    return {
        "edge_id": edge_id, "mention_id": f"P-{edge_id}a", "mention_name": "محمد رمضان",
        "candidate_id": f"P-{edge_id}b", "candidate_name": "محمد رمضان ساکنہ محلہ",
        "source_doc_id": doc_id, "case_id": case_id, "tier": tier,
    }


@pytest.mark.asyncio
async def test_dry_run_makes_no_confirm_calls(monkeypatch, capsys):
    async def fake_execute_cypher(query, columns=None, params=None):
        return [_edge(1), _edge(2)]

    monkeypatch.setattr(collapse_script.age_client, "execute_cypher", fake_execute_cypher)
    monkeypatch.setattr(sys, "argv", ["collapse_same_document_duplicate_persons.py", "--dry-run"])

    confirm_calls = []
    import src.api.graph_review as graph_review

    async def fake_confirm_match(edge_id, action, admin):
        confirm_calls.append(edge_id)
        return {"status": "confirmed", "new_edge_id": 999}

    monkeypatch.setattr(graph_review, "confirm_match", fake_confirm_match)

    await collapse_script.main()

    assert confirm_calls == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "2" in out


@pytest.mark.asyncio
async def test_apply_confirms_every_qualifying_edge(monkeypatch, capsys):
    edges = [_edge(1), _edge(2), _edge(3)]
    call_count = {"n": 0}

    async def fake_execute_cypher(query, columns=None, params=None):
        # First call: the initial fetch. Second call (after --apply):
        # the "remaining" re-check, which should now be empty.
        call_count["n"] += 1
        return edges if call_count["n"] == 1 else []

    monkeypatch.setattr(collapse_script.age_client, "execute_cypher", fake_execute_cypher)
    monkeypatch.setattr(sys, "argv", ["collapse_same_document_duplicate_persons.py", "--apply"])

    import src.api.graph_review as graph_review

    confirm_calls = []

    async def fake_confirm_match(edge_id, action, admin):
        confirm_calls.append(edge_id)
        return {"status": "confirmed", "new_edge_id": edge_id + 1000}

    monkeypatch.setattr(graph_review, "confirm_match", fake_confirm_match)

    await collapse_script.main()

    assert sorted(confirm_calls) == [1, 2, 3]
    out = capsys.readouterr().out
    assert "Confirmed: 3 / 3" in out
    assert "remaining (should be 0" in out


@pytest.mark.asyncio
async def test_apply_reports_per_edge_errors_without_aborting(monkeypatch, capsys):
    edges = [_edge(1), _edge(2)]
    call_count = {"n": 0}

    async def fake_execute_cypher(query, columns=None, params=None):
        call_count["n"] += 1
        return edges if call_count["n"] == 1 else []

    monkeypatch.setattr(collapse_script.age_client, "execute_cypher", fake_execute_cypher)
    monkeypatch.setattr(sys, "argv", ["collapse_same_document_duplicate_persons.py", "--apply"])

    import src.api.graph_review as graph_review

    async def fake_confirm_match(edge_id, action, admin):
        if edge_id == 1:
            raise RuntimeError("already superseded")
        return {"status": "confirmed", "new_edge_id": 999}

    monkeypatch.setattr(graph_review, "confirm_match", fake_confirm_match)

    await collapse_script.main()

    out = capsys.readouterr().out
    assert "Confirmed: 1 / 2" in out
    assert "already superseded" in out


@pytest.mark.asyncio
async def test_no_qualifying_edges_reports_nothing_to_confirm(monkeypatch, capsys):
    async def fake_execute_cypher(query, columns=None, params=None):
        return []

    monkeypatch.setattr(collapse_script.age_client, "execute_cypher", fake_execute_cypher)
    monkeypatch.setattr(sys, "argv", ["collapse_same_document_duplicate_persons.py", "--dry-run"])

    await collapse_script.main()

    out = capsys.readouterr().out
    assert "Nothing to confirm" in out
