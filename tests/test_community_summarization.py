"""
Tests for src/graph/community_summarization.py's
_finest_levels_to_summarize() (findings.md Module 9, Stage 2 — real
hierarchy). No real Postgres — get_session monkeypatched with a fake
session (same pattern tests/test_identity_index.py establishes).

The rest of summarize_communities() (LLM calls, case metadata fetch,
Chroma upsert) is unchanged by Stage 2 and already exercised implicitly
by this module's own live-verification runs — this file's scope is the
new level cap only.
"""
import pytest

import src.graph.community_summarization as community_summarization
from src.graph.community_summarization import MAX_LEVELS_TO_SUMMARIZE, _finest_levels_to_summarize


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, level_rows: list[tuple]):
        self._level_rows = level_rows
        self.executed: list[tuple] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return _FakeResult(self._level_rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_session(session):
    def _factory():
        return session
    return _factory


async def test_fewer_levels_than_cap_returns_all_of_them(monkeypatch):
    session = _FakeSession([(0,), (1,)])
    monkeypatch.setattr(community_summarization, "get_session", _fake_get_session(session))

    result = await _finest_levels_to_summarize("RUN-1")

    assert result == [0, 1]


async def test_more_levels_than_cap_keeps_only_the_finest(monkeypatch):
    # A >=5-level fixture — findings.md's own Test plan wording — must
    # summarize only the finest MAX_LEVELS_TO_SUMMARIZE (3), not every
    # level Louvain happened to produce.
    session = _FakeSession([(0,), (1,), (2,), (3,), (4,)])
    monkeypatch.setattr(community_summarization, "get_session", _fake_get_session(session))

    result = await _finest_levels_to_summarize("RUN-1")

    assert result == [0, 1, 2]
    assert len(result) == MAX_LEVELS_TO_SUMMARIZE


async def test_exactly_at_cap_returns_all_of_them(monkeypatch):
    session = _FakeSession([(0,), (1,), (2,)])
    monkeypatch.setattr(community_summarization, "get_session", _fake_get_session(session))

    result = await _finest_levels_to_summarize("RUN-1")

    assert result == [0, 1, 2]


async def test_no_levels_at_all_returns_empty(monkeypatch):
    session = _FakeSession([])
    monkeypatch.setattr(community_summarization, "get_session", _fake_get_session(session))

    result = await _finest_levels_to_summarize("RUN-1")

    assert result == []


# ═══════════════════════════════════════════════════════════════════════
# _validate()'s Urdu-coverage check — the live false positive it caused
# ═══════════════════════════════════════════════════════════════════════
#
# community_reports.summary_text is English-only by design (embedding
# consistency), so _validate() rejects an Urdu answer. A pure Arabic-script
# COVERAGE ratio cannot tell "English sentence quoting many Urdu names"
# apart from "Urdu sentence" — both look the same by character census.
#
# Live failure this guards: a community whose members are all Urdu-named
# produced a correct English summary quoting each of them inline; the
# ratio crossed the threshold, all three local retries plus the cloud
# fallback were rejected, and the community was written with NO summary —
# invisible to Global Search/XNETWORK.

# Representative of the real failure shape: a community whose members are
# ALL Urdu-named, so the English summary must quote every one of them
# inline. Arabic coverage here is ~0.60 — far past the 0.3 ratio gate —
# while the prose is unambiguously English. (The production log truncated
# the real summary mid-name-list; a 4-name reconstruction only reaches
# ~0.17 coverage and never touches the gate, which is exactly the vacuous
# test a mutation check caught. This one is verified to exercise it.)
_NAME_HEAVY_ENGLISH = (
    "This cluster is linked to case fir-233-26. The named individuals "
    "طارق محمود, محمد اسلم, شعیب ارشد, ذیشان حیدر, محمد رمضان, عثمان خالد ملک, "
    "سائرہ نذیر, ارسلان محمود, فہد میمن, نمرہ اکرم, بلال احمد, حرا شاہد, عائشہ بی بی, "
    "اصغر علی, راحیل شہزاد, طارق جمالی, کاشف محمود, فیصل رحمان "
    "are connected as co-accused in this case."
)

_GENUINELY_URDU = (
    "یہ کلسٹر مقدمہ fir-233-26 سے منسلک ہے اور اس میں شامل افراد "
    "طارق محمود اور محمد اسلم ہیں۔"
)


def test_name_heavy_english_summary_is_accepted():
    """The regression itself. Verified to actually reach the ratio gate:
    Arabic coverage ~0.60, so a pure-ratio check rejects this correct
    English summary — which is what silently produced a community with no
    summary at all in production."""
    from src.ingestion.script_detector import _ARABIC_SCRIPT
    non_space = [c for c in _NAME_HEAVY_ENGLISH if not c.isspace()]
    coverage = sum(1 for c in non_space if _ARABIC_SCRIPT.match(c)) / len(non_space)
    assert coverage > 0.3, "fixture must exercise the gate, not bypass it"

    assert community_summarization._validate({"summary": _NAME_HEAVY_ENGLISH}) is True


def test_genuinely_urdu_summary_is_still_rejected():
    """The fix must not open the door it was narrowing — an actually-Urdu
    summary has no English function words and must still fail."""
    assert community_summarization._validate({"summary": _GENUINELY_URDU}) is False


def test_plain_english_summary_is_accepted():
    assert community_summarization._validate(
        {"summary": "This cluster spans two cases and is linked by co-accused individuals."}
    ) is True


def test_not_enough_data_sentinel_is_accepted():
    assert community_summarization._validate({"summary": "NOT_ENOUGH_DATA"}) is True


def test_non_string_summary_is_still_rejected():
    """[PRESERVE] The original live corruption: {"summary": {"members": [...]}}
    was str()'d and stored verbatim. Must stay rejected."""
    assert community_summarization._validate({"summary": {"members": []}}) is False
    assert community_summarization._validate({"summary": ""}) is False
    assert community_summarization._validate({"summary": "   "}) is False
    assert community_summarization._validate("not a dict") is False
