"""
Tests for scripts/collapse_cross_document_duplicate_persons.py — pass 2 of
findings.md Module 11's duplicate-Person cleanup. No real AGE/Postgres:
age_client.execute_cypher and src.api.graph_review.confirm_match are
monkeypatched.

The tests that matter here are the GUARD tests. This script acts on an
exact-name match across two documents, which is a weaker signal than pass
1's same-document bar — guards 4 and 5 are the entire reason that weaker
signal is safe to act on, so each one is tested for what it REJECTS, not
just for what it lets through.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import scripts.collapse_cross_document_duplicate_persons as collapse2


def _row(edge_id=1, name="فیصل", a_cnic=None, b_cnic=None,
         case_id="fir-1001-26",
         a_doc="psrms_fir_fir-1001-26#narrative_c8bf2613",
         b_doc="psrms/fir/fir-1001-26#structured",
         tier="flagged_unverified"):
    return {
        "edge_id": edge_id, "a_id": f"PERSON-{edge_id}a", "b_id": f"PERSON-{edge_id}b",
        "name": name, "a_cnic": a_cnic, "b_cnic": b_cnic,
        "a_doc": a_doc, "b_doc": b_doc, "case_id": case_id, "tier": tier,
    }


# ── Guard 5: CNIC conflict is a hard veto ────────────────────────────────

def test_conflicting_cnics_are_rejected_not_confirmed():
    """Two same-named people in one FIR with DIFFERENT national IDs are two
    people. This is the guard that makes an exact-name match safe."""
    assert collapse2._cnic_conflict(
        _row(a_cnic="00000-1111111-1", b_cnic="00000-2222222-2")
    ) is True


def test_matching_cnics_are_not_a_conflict():
    assert collapse2._cnic_conflict(
        _row(a_cnic="00000-1111111-1", b_cnic="00000-1111111-1")
    ) is False


@pytest.mark.parametrize("a_cnic,b_cnic", [
    (None, None), ("00000-1111111-1", None), (None, "00000-2222222-2"),
    ("", "00000-2222222-2"), ("   ", "00000-2222222-2"),
])
def test_missing_cnic_on_either_side_is_not_a_conflict(a_cnic, b_cnic):
    """Absence of evidence is not evidence of a different person — a
    narrative-extracted mention normally carries no CNIC at all, which is
    the common case this script exists to handle. Only a real, populated
    DISAGREEMENT vetoes."""
    assert collapse2._cnic_conflict(_row(a_cnic=a_cnic, b_cnic=b_cnic)) is False


# ── Guard 4: both documents must be the SAME FIR ─────────────────────────

def test_same_fir_accepts_the_two_real_namespaces():
    """The clean structured projection and the sanitized narrative chunk of
    one FIR — the exact pair this script targets."""
    assert collapse2._same_fir(_row()) is True


def test_different_fir_documents_are_rejected():
    """Same name, same case row, but one document belongs to another FIR:
    not the same source record, so not this script's case."""
    assert collapse2._same_fir(
        _row(b_doc="psrms/fir/fir-2222-26#structured")
    ) is False


def test_missing_case_id_is_rejected():
    """Fails closed — no case id means guard 4 cannot be evaluated at all."""
    assert collapse2._same_fir(_row(case_id="")) is False
    assert collapse2._same_fir(_row(case_id=None)) is False


# ── Partitioning: every examined row lands in exactly one bucket ─────────

@pytest.mark.asyncio
async def test_rows_are_partitioned_into_qualifying_and_rejected(monkeypatch):
    rows = [
        _row(edge_id=1),                                                   # qualifies
        _row(edge_id=2, a_cnic="00000-1-1", b_cnic="00000-2-2"),           # guard 5
        _row(edge_id=3, b_doc="psrms/fir/fir-9999-26#structured"),         # guard 4
    ]

    async def fake_execute_cypher(query, columns=None, params=None):
        return rows

    monkeypatch.setattr(collapse2.age_client, "execute_cypher", fake_execute_cypher)

    qualifying, cnic_rejected, fir_rejected = await collapse2._fetch_candidates()

    assert [e["edge_id"] for e in qualifying] == [1]
    assert [e["edge_id"] for e in cnic_rejected] == [2]
    assert [e["edge_id"] for e in fir_rejected] == [3]
    # nothing silently vanishes
    assert len(qualifying) + len(cnic_rejected) + len(fir_rejected) == len(rows)


def test_cnic_veto_takes_precedence_over_the_same_fir_check():
    """A row failing BOTH guards must be reported as a CNIC conflict — the
    more serious finding (two real people) outranks a provenance mismatch."""
    row = _row(a_cnic="00000-1-1", b_cnic="00000-2-2",
               b_doc="psrms/fir/fir-9999-26#structured")
    assert collapse2._cnic_conflict(row) is True


# ── The mutation gate ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_makes_no_confirm_calls(monkeypatch, capsys):
    async def fake_execute_cypher(query, columns=None, params=None):
        return [_row(1), _row(2)]

    monkeypatch.setattr(collapse2.age_client, "execute_cypher", fake_execute_cypher)
    monkeypatch.setattr(sys, "argv", ["collapse_cross_document_duplicate_persons.py", "--dry-run"])

    confirm_calls = []
    import src.api.graph_review as graph_review

    async def fake_confirm_match(edge_id, action, admin):
        confirm_calls.append(edge_id)
        return {"status": "confirmed", "new_edge_id": 999}

    monkeypatch.setattr(graph_review, "confirm_match", fake_confirm_match)

    await collapse2.main()

    assert confirm_calls == [], "dry run must not mutate the graph"
    assert "DRY RUN" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_apply_confirms_only_qualifying_edges(monkeypatch, capsys):
    """The CNIC-conflict row must never reach confirm_match()."""
    rows = [
        _row(edge_id=1),
        _row(edge_id=2, a_cnic="00000-1-1", b_cnic="00000-2-2"),
    ]
    calls = {"n": 0}

    async def fake_execute_cypher(query, columns=None, params=None):
        # second call (the post-apply re-check) sees nothing left
        calls["n"] += 1
        return rows if calls["n"] == 1 else []

    monkeypatch.setattr(collapse2.age_client, "execute_cypher", fake_execute_cypher)
    monkeypatch.setattr(sys, "argv", ["collapse_cross_document_duplicate_persons.py", "--apply"])

    confirmed = []
    import src.api.graph_review as graph_review

    async def fake_confirm_match(edge_id, action, admin):
        confirmed.append(edge_id)
        return {"status": "confirmed", "new_edge_id": 999}

    monkeypatch.setattr(graph_review, "confirm_match", fake_confirm_match)

    await collapse2.main()

    assert confirmed == [1], "only the qualifying edge may be confirmed"
    out = capsys.readouterr().out
    assert "REJECTED, CNIC conflict" in out, "a rejection is a finding and must be printed"
