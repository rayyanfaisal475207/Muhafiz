"""
Tests for src/graph/community_detection.py's Person-node noise filter.

Priority 1 of the 2026-08-06 open-gaps audit: the XNETWORK community-noise
filter had been patched three separate times with new exact-phrase/suffix
blocklist entries, each round finding a new category of junk. This test
guards the fourth-round audit's structural fix — a document-rendering
artifact (a table/field boundary collapsing with no separating punctuation)
that appends an adjacent field's text directly onto a real extracted name,
live-confirmed against the real graph on 2026-08-06 (see the fix commit):
"Inspector Fariha Saeed Bhara" (1 occurrence) sitting alongside the
correctly-extracted "Inspector Fariha Saeed" (7 occurrences).

Also covers [findings.md Module 9, "Global Search"]:
get_community_reports_for_level()/get_available_report_levels() — no real
Postgres, get_session monkeypatched with a fake session (same pattern
tests/test_identity_index.py already establishes).
"""
import pytest

import src.graph.community_detection as community_detection
from src.graph.community_detection import (
    _compute_prefix_contaminated_names,
    _is_plausible_person_name,
)


class TestStructuralArtifactChars:
    """A real extracted person name never contains rendering-boundary
    characters — newline, carriage return, a markdown table pipe, or a
    parenthesis."""

    def test_rejects_newline_bleed(self):
        # Live-observed: "Inspector Tariq Khan\n\nStatus" — the next
        # field's label bled across a blank line into the captured span.
        assert _is_plausible_person_name("Inspector Tariq Khan\n\nStatus") is False

    def test_rejects_another_newline_bleed(self):
        assert _is_plausible_person_name("Yusra Nawaz\n\nApplicant") is False

    def test_rejects_parenthetical(self):
        # Live-observed: "Inspector (Golra)" — the officer's real name was
        # dropped entirely and a station name substituted in parens.
        assert _is_plausible_person_name("Inspector (Golra)") is False

    def test_rejects_table_pipe(self):
        assert _is_plausible_person_name("Inspector Fariha | Saeed") is False

    def test_accepts_ordinary_two_word_name(self):
        assert _is_plausible_person_name("Irfan Mirza") is True

    def test_accepts_ordinary_role_plus_name(self):
        assert _is_plausible_person_name("Inspector Fariha Saeed") is True


class TestPrefixContamination:
    """A candidate name is a contaminated superstring of a real name, not a
    real longer name in its own right, when dropping its last token yields
    a shorter string that both exists in the corpus and occurs more often."""

    def test_flags_low_frequency_superstring_of_common_name(self):
        # "Inspector Fariha Saeed" appears 7x; the station-name-suffixed
        # variant appears once — the exact live-observed shape.
        person_names = {f"p{i}": "Inspector Fariha Saeed" for i in range(7)}
        person_names["p_bad"] = "Inspector Fariha Saeed Bhara"
        contaminated = _compute_prefix_contaminated_names(person_names)
        assert "Inspector Fariha Saeed Bhara" in contaminated
        assert "Inspector Fariha Saeed" not in contaminated

    def test_does_not_flag_when_no_shorter_prefix_exists(self):
        # No independently-common shorter prefix in the corpus — nothing to
        # compare against, so this must NOT be treated as contamination.
        person_names = {"p1": "Inspector Rare Standalone Name"}
        contaminated = _compute_prefix_contaminated_names(person_names)
        assert contaminated == set()

    def test_does_not_flag_when_longer_name_is_more_common(self):
        # A genuinely more common longer name isn't contamination even if
        # a rarer shorter prefix happens to coincide.
        person_names = {f"p{i}": "Irfan Mirza Junior" for i in range(5)}
        person_names["p_short"] = "Irfan Mirza"
        contaminated = _compute_prefix_contaminated_names(person_names)
        assert contaminated == set()

    def test_two_word_names_never_flagged(self):
        # The check requires 3+ tokens (a role/first/last minimum) —
        # ordinary two-word names have no "trailing fragment" to drop.
        person_names = {"p1": "Irfan Mirza", "p2": "Irfan"}
        contaminated = _compute_prefix_contaminated_names(person_names)
        assert contaminated == set()

    def test_multiple_contaminated_variants_across_population(self):
        # Mirrors the live audit: several different officers, each with
        # both a clean, frequent name and a rarer contaminated variant.
        person_names = {}
        for i in range(11):
            person_names[f"hamza{i}"] = "Inspector Hamza Latif"
        person_names["hamza_bad1"] = "Inspector Hamza Latif Kohsar"
        person_names["hamza_bad2"] = "Inspector Hamza Latif Kohsar"
        for i in range(8):
            person_names[f"rashid{i}"] = "Inspector Rashid Gondal"
        person_names["rashid_bad1"] = "Inspector Rashid Gondal Shahzad"
        person_names["rashid_bad2"] = "Inspector Rashid Gondal Shahzad"

        contaminated = _compute_prefix_contaminated_names(person_names)
        assert contaminated == {
            "Inspector Hamza Latif Kohsar",
            "Inspector Rashid Gondal Shahzad",
        }


# ═══════════════════════════════════════════════════════════════════════
# [findings.md Module 9, "Global Search"] get_community_reports_for_level()
# / get_available_report_levels() — the two new direct community_reports
# reads Stage 1's map-reduce sub-agent fetches from (NOT the Chroma top-k
# path — see src/pipeline/global_search.py's own module docstring).
# ═══════════════════════════════════════════════════════════════════════


class _FakeMappingsResult:
    """Mimics SQLAlchemy's `.mappings()` iteration over dict-like rows —
    dict(row) on each already-dict item is a no-op copy, matching real
    RowMapping behavior closely enough for this module's own `[dict(row)
    for row in res.mappings()]` usage."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def mappings(self):
        return list(self._rows)

    def fetchall(self):
        return [tuple(row.values()) for row in self._rows]


class _FakeSession:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.executed: list[tuple] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return _FakeMappingsResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_session(session):
    def _factory():
        return session
    return _factory


async def test_get_community_reports_for_level_with_explicit_run_id(monkeypatch):
    rows = [
        {
            "community_id": "C-1", "level": 0, "run_id": "RUN-1",
            "member_entity_ids": ["p1", "p2"], "case_ids": ["CASE-001"],
            "member_count": 2, "summary_text": "A pattern of vehicle theft.",
        },
    ]
    session = _FakeSession(rows)
    monkeypatch.setattr(community_detection, "get_session", _fake_get_session(session))

    result = await community_detection.get_community_reports_for_level(level=0, run_id="RUN-1")

    assert result == rows
    # run_id given explicitly -> no separate get_latest_run() lookup needed,
    # exactly one query executed.
    assert len(session.executed) == 1
    assert session.executed[0][1] == {"run_id": "RUN-1", "level": 0}


async def test_get_community_reports_for_level_defaults_to_latest_run(monkeypatch):
    async def fake_get_latest_run():
        return {"run_id": "RUN-LATEST"}

    monkeypatch.setattr(community_detection, "get_latest_run", fake_get_latest_run)

    rows = [
        {
            "community_id": "C-9", "level": 0, "run_id": "RUN-LATEST",
            "member_entity_ids": ["p9"], "case_ids": [],
            "member_count": 1, "summary_text": "A single-member community.",
        },
    ]
    session = _FakeSession(rows)
    monkeypatch.setattr(community_detection, "get_session", _fake_get_session(session))

    result = await community_detection.get_community_reports_for_level(level=0)

    assert result == rows
    assert session.executed[0][1]["run_id"] == "RUN-LATEST"


async def test_get_community_reports_for_level_no_run_at_all_returns_empty(monkeypatch):
    async def fake_get_latest_run():
        return None

    monkeypatch.setattr(community_detection, "get_latest_run", fake_get_latest_run)

    result = await community_detection.get_community_reports_for_level(level=0)

    assert result == []


async def test_get_available_report_levels_with_explicit_run_id(monkeypatch):
    rows = [{"level": 0}, {"level": 1}, {"level": 2}]
    session = _FakeSession(rows)
    monkeypatch.setattr(community_detection, "get_session", _fake_get_session(session))

    result = await community_detection.get_available_report_levels(run_id="RUN-1")

    assert result == [0, 1, 2]


async def test_get_available_report_levels_no_run_at_all_returns_empty(monkeypatch):
    async def fake_get_latest_run():
        return None

    monkeypatch.setattr(community_detection, "get_latest_run", fake_get_latest_run)

    result = await community_detection.get_available_report_levels()

    assert result == []


# ═══════════════════════════════════════════════════════════════════════
# [findings.md Module 9, Stage 2 — real hierarchy] detect_communities()
# end-to-end against a fixture graph with enough structure to produce
# >=2 genuinely different Louvain levels — Zachary's karate club, the
# same real test graph this session's own plan.md live-reconfirmed
# louvain_partitions() against (finest -> coarsest, [5, 4] communities).
# No real AGE/Postgres: every graph-read function is monkeypatched with
# fixture data, get_session with a fake session (same pattern as the
# fetch-helper tests above / tests/test_identity_index.py).
# ═══════════════════════════════════════════════════════════════════════


class _FakePersistResult:
    def fetchall(self):
        return []

    def mappings(self):
        return []


class _FakePersistSession:
    def __init__(self):
        self.executed: list[tuple] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return _FakePersistResult()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _karate_club_shared_case_pairs() -> list[tuple[str, str, int]]:
    """Zachary's karate club, node ids remapped to synthetic person
    entity_ids ("p0".."p33") — the same real graph this session
    live-confirmed `louvain_partitions()` yields >=2 genuinely different
    levels for ([5, 4] communities, finest -> coarsest)."""
    import networkx as nx

    g = nx.karate_club_graph()
    return [(f"p{u}", f"p{v}", 1) for u, v in g.edges()]


async def test_detect_communities_produces_at_least_two_genuinely_different_levels(monkeypatch):
    shared_case_pairs = _karate_club_shared_case_pairs()
    person_ids = sorted({pid for u, v, _ in shared_case_pairs for pid in (u, v)})
    # 2-token names so _is_plausible_person_name() keeps every node —
    # single-token names are filtered as extraction noise (see this
    # module's own "Non-name filter" section above).
    names = {pid: f"Person {pid.upper()}" for pid in person_ids}

    async def fake_same_as():
        return []

    async def fake_names():
        return names

    async def fake_stations():
        return set()

    async def fake_associated_with():
        return []

    async def fake_shared_case():
        return shared_case_pairs

    async def fake_person_cases():
        return []

    monkeypatch.setattr(community_detection, "fetch_confirmed_same_as", fake_same_as)
    monkeypatch.setattr(community_detection, "fetch_person_names", fake_names)
    monkeypatch.setattr(community_detection, "fetch_known_police_stations", fake_stations)
    monkeypatch.setattr(community_detection, "_fetch_associated_with", fake_associated_with)
    monkeypatch.setattr(community_detection, "_fetch_shared_case_pairs", fake_shared_case)
    monkeypatch.setattr(community_detection, "fetch_person_case_membership", fake_person_cases)

    session = _FakePersistSession()
    monkeypatch.setattr(community_detection, "get_session", _fake_get_session(session))

    result = await community_detection.detect_communities()

    # >=2 levels in the function's own return shape...
    assert len(result["levels"]) >= 2
    assert result["community_count"] == result["levels"][0]

    # ...and the SAME real, distinct level values actually persisted to
    # community_membership (not merely computed and discarded) — inspect
    # the recorded INSERT params rather than a live table, per this
    # file's established fake-session convention.
    membership_inserts = [
        params for _, params in session.executed
        if params is not None and "level" in params and "entity_id" in params
    ]
    levels_seen = {p["level"] for p in membership_inserts}
    assert len(levels_seen) >= 2

    # Genuinely two different partitions, not one partition duplicated
    # under two level numbers: compare each level's actual member-set
    # partition (community_id -> frozenset(members)).
    from collections import defaultdict

    community_level: dict[str, int] = {}
    community_members: dict[str, set[str]] = defaultdict(set)
    for p in membership_inserts:
        community_level[p["community_id"]] = p["level"]
        community_members[p["community_id"]].add(p["entity_id"])

    per_level_partition: dict[int, set[frozenset]] = defaultdict(set)
    for community_id, members in community_members.items():
        per_level_partition[community_level[community_id]].add(frozenset(members))

    levels_sorted = sorted(per_level_partition)
    finest, next_finest = levels_sorted[0], levels_sorted[1]
    assert per_level_partition[finest] != per_level_partition[next_finest]
