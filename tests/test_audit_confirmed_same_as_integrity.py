"""
Tests for scripts/audit_confirmed_same_as_integrity.py — the read-only
CNIC-conflict audit over already-CONFIRMED Person SAME_AS edges.

No real AGE/Postgres: age_client.execute_cypher is monkeypatched. This
script never mutates, so there is no admin/apply flow to stub, unlike the
collapse scripts' test suites.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import scripts.audit_confirmed_same_as_integrity as audit


def _row(edge_id=1, a_cnic=None, b_cnic=None, a_name="فیصل", b_name="فیصل",
         a_doc="psrms_fir_fir-1001-26#narrative_c8bf2613",
         b_doc="psrms/fir/fir-1001-26#structured",
         tier="flagged_unverified", reviewed_by="admin"):
    return {
        "edge_id": edge_id, "a_id": f"PERSON-{edge_id}a", "b_id": f"PERSON-{edge_id}b",
        "a_name": a_name, "b_name": b_name, "a_cnic": a_cnic, "b_cnic": b_cnic,
        "a_doc": a_doc, "b_doc": b_doc, "tier": tier, "reviewed_by": reviewed_by,
    }


# ── The conflict predicate itself ────────────────────────────────────────

def test_conflicting_cnics_are_flagged():
    assert audit._cnic_conflict(
        _row(a_cnic="00000-1111111-1", b_cnic="00000-2222222-2")
    ) is True


def test_matching_cnics_are_not_a_conflict():
    assert audit._cnic_conflict(
        _row(a_cnic="00000-1111111-1", b_cnic="00000-1111111-1")
    ) is False


@pytest.mark.parametrize("a_cnic,b_cnic", [
    (None, None), ("00000-1111111-1", None), (None, "00000-2222222-2"),
    ("", "00000-2222222-2"), ("   ", "00000-2222222-2"),
])
def test_missing_cnic_on_either_side_is_not_a_conflict(a_cnic, b_cnic):
    """Absence of evidence is not evidence of a different person — same
    rule the collapse scripts' own guard 5 uses."""
    assert audit._cnic_conflict(_row(a_cnic=a_cnic, b_cnic=b_cnic)) is False


# ── End-to-end: main() over a fetched row set ────────────────────────────

@pytest.mark.asyncio
async def test_main_returns_zero_when_no_violations(monkeypatch, capsys):
    async def _fake(query, params=None, columns=None):
        return [_row(a_cnic="00000-1111111-1", b_cnic="00000-1111111-1")]

    monkeypatch.setattr(audit.age_client, "execute_cypher", _fake)
    monkeypatch.setattr(sys, "argv", ["audit_confirmed_same_as_integrity.py"])

    exit_code = await audit.main()

    assert exit_code == 0
    assert "No CNIC-conflict violations" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_main_returns_one_and_reports_violations(monkeypatch, capsys):
    good = _row(edge_id=1, a_cnic="00000-1111111-1", b_cnic="00000-1111111-1")
    bad = _row(edge_id=2, a_cnic="00000-1111111-1", b_cnic="00000-2222222-2")

    async def _fake(query, params=None, columns=None):
        return [good, bad]

    monkeypatch.setattr(audit.age_client, "execute_cypher", _fake)
    monkeypatch.setattr(sys, "argv", ["audit_confirmed_same_as_integrity.py"])

    exit_code = await audit.main()

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "CNIC-CONFLICT VIOLATIONS: 1" in out
    assert "edge_id=2" in out
    assert "edge_id=1" not in out.split("VIOLATIONS")[1]  # only the bad edge listed


@pytest.mark.asyncio
async def test_main_scopes_to_case_when_flag_given(monkeypatch):
    """--case fir-1001-26 must route through the case-scoped query with the
    right param, not the unscoped all-cases query."""
    seen = {}

    async def _fake(query, params=None, columns=None):
        seen["query"] = query
        seen["params"] = params
        return []

    monkeypatch.setattr(audit.age_client, "execute_cypher", _fake)
    monkeypatch.setattr(sys, "argv", ["audit_confirmed_same_as_integrity.py", "--case", "fir-1001-26"])

    await audit.main()

    assert seen["params"] == {"case_id": "fir-1001-26"}
    assert "BELONGS_TO_CASE" in seen["query"]


def test_case_arg_supports_space_and_equals_forms(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog", "--case", "fir-1001-26"])
    assert audit._case_arg() == "fir-1001-26"

    monkeypatch.setattr(sys, "argv", ["prog", "--case=fir-1001-26"])
    assert audit._case_arg() == "fir-1001-26"

    monkeypatch.setattr(sys, "argv", ["prog"])
    assert audit._case_arg() is None
