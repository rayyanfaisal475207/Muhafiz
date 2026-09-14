# -*- coding: utf-8 -*-
"""
[Gold-QA fix — Module 145] Semantic dispatch — the fallback UNDER the phrase
lists in `xagg.resolve_aggregate_kind()`.

THE DEFECT, once, at four layers. Modules 92, 100, 106, 111 and 123 each
filed the same thing: a capability is reachable only from a narrow
neighbourhood of its gold wording, because every dispatch gate in the
pipeline is a list of literal phrases. The live example this module was
filed from — *"How often is the officer who registers an FIR ALSO the
officer who investigates the case?"* — hits two of the three term lists
behind `officer_role_pair_overlap` and misses the third because "also" is
not a listed sameness word. The aggregate exists; the question is one
word away from it; it fell to Semantic Search and abstained after 108 s.

THE DESIGN. One plain sentence per aggregate kind, saying what it answers
(`CAPABILITY_DESCRIPTIONS`). The phrase lists run first and are unchanged —
they are the fast path and the guarantee that the 32 gold questions
dispatch byte-identically. ONLY when every list misses (the chain lands on
one of `xagg._GENERIC_AGGREGATE_KINDS`) is the question scored against
every description; the best wins if it clears
`SEMANTIC_DISPATCH_THRESHOLD`, otherwise dispatch falls through exactly as
before.

THE SCORER IS THE CROSS-ENCODER, NOT e5 COSINE — measured, not preferred.
The plan named the e5 `/embed` endpoint. Measured over the 96-paraphrase
corpus, 24 pre-written hazards and the 7 gold questions that land on the
generic tier, e5 cosine against full-sentence descriptions has NO usable
threshold: the lowest true positive scored 0.789 and the highest hazard
0.870 (a legal-procedure question about FIR registration, nearest to the
FIR-register-completeness description — the same TOPIC, the opposite
question TYPE). Bi-encoder similarity measures topic; dispatch needs
intent. The `/rerank` cross-encoder (bge-reranker-v2-m3, already serving
retrieval) scores (question, description) jointly: on the same items the
hazards collapse to <= 0.35 and the true positives it fires on sit at
0.46–1.00. MODULE145_RESULT.md §6 has both distributions and the margin.

SYNC/ASYNC SPLIT, and why `lookup()` is a dict read. `resolve_aggregate_kind()`
is pure and synchronous by contract (router, supervisor and `run_aggregate`
all call it, two of them from inside the event loop), so it cannot make an
HTTP call. The scoring therefore happens in the async `prepare()` at the
async chokepoints — `router.route_query()` and `xagg.run_aggregate()` — and
the decision is cached by exact query text; `lookup()` then returns that
cached decision synchronously. A question that was never prepared (unit
tests, offline callers) gets `None` from `lookup()` and the phrase result
stands — the layer is strictly additive.

FAILURE MODE. The model server is a free-tier ngrok tunnel that can be dead
while `/health` is green. Any scorer error disables the layer for
`_COOLDOWN_S` seconds and the question falls through as it always did; it
never blocks the pipeline for more than `_SCORE_TIMEOUT_S`.

EVERY DECISION IS LOGGED, including the ones that do not fire — the
`SEMANTIC-DISPATCH` line is the only evidence of what this layer did on a
live question, the same convention as the `XAGG <kind>:` lines.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from src import config

logger = logging.getLogger(__name__)


# ── The capability table ─────────────────────────────────────────────────────
#
# One sentence per kind, written from the aggregate's own docstring and the
# gold question it was built for — NEVER from its trigger words. Every kind
# `resolve_aggregate_kind()` can return has an entry, so the table cannot
# silently drift behind the chain (`tests/test_semantic_dispatch.py` pins
# that). The three generic catch-alls and the two honest refusals are
# described too, but as ABSORBING classes: a question whose nearest
# description is "the total number of cases" is not pulled anywhere — the
# phrase result stands. That is what keeps "how many cases in total" out of
# every purpose-built family.
CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    # ── purpose-built aggregates ──
    "offender_age_profile": (
        "the ages of the accused across the caseload: the age range, the average "
        "age, and how many accused have no age recorded"
    ),
    "station_caseload_by_specialisation": (
        "whether caseload is growing faster at general-purpose police stations or "
        "at specialised units such as cybercrime, anti-corruption or women's "
        "protection stations, comparing the two kinds of station"
    ),
    "placeholder_officer_count": (
        "how many cases still have no real investigating officer actually "
        "assigned, only a placeholder or unnamed officer"
    ),
    "criminal_record_local_match_gap": (
        "people who appear in the wider criminal-history records but have no "
        "matching accused record in our own FIRs"
    ),
    "chalaan_dispatch_count": (
        "how many challans, the police report to the court, have been sent to "
        "court and how many cases they cover"
    ),
    "criminal_record_court_crosscheck": (
        "how many cases in the criminal record system are completed with a "
        "verdict versus still in progress, and whether the criminal record's "
        "conviction status agrees with the court's recorded outcome"
    ),
    "dv_report_fir_match": (
        "whether a domestic violence or violence-against-women report that was "
        "recorded as converted into a formal case is confirmed by an actual FIR"
    ),
    "cms_fir_linkage": (
        "whether walk-in complaints filed at a police station are linked to a "
        "formal FIR or stay separate from the case record"
    ),
    "court_readiness_scan": (
        "preparing a case file for handover to court: which fields a prosecutor "
        "or court is most likely to find incomplete before accepting the file"
    ),
    "fir_register_completeness": (
        "whether each FIR register entry carries the details the law requires "
        "to be recorded, such as the complainant, the sections and the date"
    ),
    "case_completeness_scan": (
        "which cases have records so incomplete that they might be overlooked "
        "or buried: missing incident dates and no diary entries"
    ),
    "accused_relationship_breakdown": (
        "what relationship is recorded between the accused and the complainant "
        "or victim, and whether they were strangers or knew each other"
    ),
    "weapon_compliance_scan": (
        "record-keeping compliance for recovered weapons: how many recovered "
        "weapons are recorded without a licence"
    ),
    "weapon_evidence_chain": (
        "for a weapon logged as evidence, which FIR it belongs to, which accused "
        "it was recovered from, and what happened to that accused"
    ),
    "seized_property_disposition": (
        "what happened to seized property in the malkhana property store: how "
        "much was sent to a forensic lab, returned to owners or heirs, or kept"
    ),
    "arrest_rate": (
        "on how many FIRs an arrest is actually recorded, as a share of all "
        "cases"
    ),
    "officer_role_pair_overlap": (
        "how often the officer who registers an FIR is the same officer who "
        "then investigates it, or whether registration and investigation are "
        "handled by different officers"
    ),
    "incident_to_report_minutes_by_year": (
        "how quickly incidents are reported to the police, the time from "
        "incident to report, and whether people report as fast this year as in "
        "earlier years"
    ),
    "reporting_delay_count": (
        "how many cases record a reason for the complainant coming to the "
        "police late, some time after the incident"
    ),
    "weapon_statute_cooccurrence_by_year": (
        "in what kinds of cases weapons turn up, which legal sections weapon "
        "cases are charged under, and whether that has changed over the years"
    ),
    "statute_court_stage_join": (
        "on one side which legal sections cases are being registered under, and "
        "on the other side how far those cases have progressed in court"
    ),
    "fir_section_case_count": (
        "how many FIRs cite a particular legal section, such as the murder "
        "section, and the count of FIRs per section"
    ),
    "incident_time_of_day": (
        "at what time of day incidents happen: morning, afternoon, evening or "
        "night"
    ),
    "statute_mix_by_year": (
        "what kinds of cases we are dealing with now compared with a couple of "
        "years ago, the mix of legal sections by year"
    ),
    "gender_breakdown": (
        "the ratio of male to female among the named accused"
    ),
    "station_total_count": (
        "how many police stations there are in total"
    ),
    "filtered_fir_listing": (
        "which specific FIRs, with their numbers and status, match a given "
        "offence, statute, station or crime type"
    ),
    "top_districts_by": (
        "which district has the most cases, ranking districts by caseload"
    ),
    "weapon_recovery_rate_by_district": (
        "which district recovers the most weapons relative to its caseload, the "
        "weapon recovery rate per district"
    ),
    "graph_recurrence_person": (
        "a specific person who appears in more than one case, possibly in "
        "different roles: someone with an earlier case on record who has "
        "turned up again, been arrested more than once, or is wanted or under "
        "investigation in another matter"
    ),
    "graph_recurrence_vehicle": (
        "a car, motorcycle or other vehicle, identified by its registration "
        "plate, that turns up in more than one case"
    ),
    "graph_recurrence_weapon": (
        "a weapon that appears in more than one case"
    ),
    "total_accused_count": (
        "how many accused persons, suspects or offenders there are in total — "
        "a count of people, not of cases"
    ),
    # ── generic catch-alls: absorbing, never dispatched to ──
    "total_count": (
        "how many cases or FIRs there are in total, the overall number of "
        "registered cases"
    ),
    "case_listing": (
        "a plain list of all the cases"
    ),
    "station_or_category_counts": (
        "how many cases each police station handles, or how many cases fall in "
        "each crime category, and which station handles the most"
    ),
    # ── honest refusals: absorbing, never dispatched to ──
    "unsupported_officer": (
        "which officer is assigned to or handled a particular case"
    ),
    "unsupported_trend": (
        "a trend over time, month over month or year over year, in the rate of "
        "reporting or the rate of increase"
    ),
    # ── neighbouring capabilities that are NOT aggregates: absorbing ──
    # Described so a question that belongs to XGRAPH or to the legal KB has
    # a correct nearest neighbour instead of the least-wrong aggregate.
    "xgraph_named_entity_network": (
        "whether a particular phone number, CNIC number, organisation or named "
        "person appears in any other case or FIR, and what that entity is "
        "connected to across cases"
    ),
    "legal_norm_lookup": (
        "what the law, the rules or the police procedure say or require: the "
        "procedure for registering an FIR, what a section provides, what the "
        "police must do"
    ),
}

# Kinds a semantic match may dispatch TO. Everything else in the table is an
# absorbing class: it can WIN the nearest-description comparison, and when it
# does the phrase result stands. The first five mirror
# `xagg._GENERIC_AGGREGATE_KINDS` and `xagg._UNSUPPORTED_AGGREGATE_KINDS` —
# pinned equal by a test rather than imported, because xagg imports this
# module; the last two are neighbouring routes' capabilities (XGRAPH, the
# legal KB) that exist only so a question of that shape lands somewhere
# correct.
NON_DISPATCHABLE_KINDS: frozenset[str] = frozenset({
    "total_count",
    "case_listing",
    "station_or_category_counts",
    "unsupported_officer",
    "unsupported_trend",
    "xgraph_named_entity_network",
    "legal_norm_lookup",
})

# MEASURED, not chosen — MODULE145_RESULT.md §6. On the 69 corpus items that
# reach this layer at the router chokepoint, the cross-encoder scores every
# hazard at <= 0.351 and every true positive it fires on at >= 0.462; 0.40
# sits in that gap with 0.05 of slack on the hazard side and 0.06 on the
# true-positive side. A question whose best description scores below this
# falls through exactly as it did before this module.
SEMANTIC_DISPATCH_THRESHOLD: float = 0.40

_SCORE_TIMEOUT_S = 8.0
_COOLDOWN_S = 60.0
_CACHE_SIZE = 256


@dataclass(frozen=True)
class SemanticMatch:
    """The best-scoring capability description and its runner-up, for one query."""
    kind: str
    score: float
    runner_up: str
    runner_up_score: float

    @property
    def fires(self) -> bool:
        return (
            self.kind not in NON_DISPATCHABLE_KINDS
            and self.score >= SEMANTIC_DISPATCH_THRESHOLD
        )


def rank_scores(scores: dict[str, float]) -> SemanticMatch:
    """Pure: the best-scoring description and its runner-up."""
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_kind, best = ranked[0]
    second_kind, second = ranked[1] if len(ranked) > 1 else (best_kind, best)
    return SemanticMatch(best_kind, float(best), second_kind, float(second))


# ── Scorer ───────────────────────────────────────────────────────────────────
#
# Deliberately NOT `cross_reranker.cross_rerank()`: that client is written
# for retrieval candidates (RRF fusion, chunk-text round-tripping, a
# "keep RRF order" fallback) and none of that applies to 38 fixed sentences.
# One request, all descriptions, no retry — a dispatch decision that cannot
# be made in seconds must fall through, not wait.

async def _score_descriptions(query_text: str) -> dict[str, float]:
    import httpx

    if not config.RERANKER_URL:
        raise RuntimeError("RERANKER_URL is not configured")
    kinds = list(CAPABILITY_DESCRIPTIONS)
    documents = [CAPABILITY_DESCRIPTIONS[k] for k in kinds]
    async with httpx.AsyncClient(timeout=_SCORE_TIMEOUT_S) as client:
        response = await client.post(
            config.RERANKER_URL,
            json={"query": query_text, "documents": documents, "top_k": len(documents)},
        )
        response.raise_for_status()
        rows = response.json()  # bare list: [{"document": str, "score": float}, ...]
    by_document = {documents[i]: kinds[i] for i in range(len(kinds))}
    scores: dict[str, float] = {}
    for row in rows:
        kind = by_document.get(row.get("document"))
        if kind is not None:
            scores[kind] = float(row["score"])
    if len(scores) != len(kinds):
        raise RuntimeError(
            f"reranker returned {len(scores)} of {len(kinds)} descriptions"
        )
    return scores


# ── State ────────────────────────────────────────────────────────────────────

_decisions: "OrderedDict[str, Optional[SemanticMatch]]" = OrderedDict()
_disabled_until: float = 0.0


def _normalise(query_text: str) -> str:
    return " ".join(query_text.split()).strip()


def _remember(key: str, match: Optional[SemanticMatch]) -> None:
    _decisions[key] = match
    _decisions.move_to_end(key)
    while len(_decisions) > _CACHE_SIZE:
        _decisions.popitem(last=False)


def reset_for_tests() -> None:
    """Drop every cached decision (test isolation only)."""
    global _disabled_until
    _decisions.clear()
    _disabled_until = 0.0


def seed_for_tests(query_text: str, match: Optional[SemanticMatch]) -> None:
    """Plant a decision without a scorer (test isolation only)."""
    _remember(_normalise(query_text), match)


def lookup(query_text: str) -> Optional[SemanticMatch]:
    """Synchronous, pure: the decision `prepare()` cached for this exact
    text, or `None` when it was never prepared, did not clear the
    threshold, or landed on an absorbing class."""
    match = _decisions.get(_normalise(query_text))
    if match is None or not match.fires:
        return None
    return match


async def prepare(query_text: str) -> Optional[SemanticMatch]:
    """Score `query_text` (once per text) against every capability
    description, cache and log the decision, and return it if it fires.

    Never raises: any scorer failure disables the layer for `_COOLDOWN_S`
    and returns `None`, which every caller treats as "the phrase result
    stands".
    """
    global _disabled_until
    key = _normalise(query_text)
    if key in _decisions:
        match = _decisions[key]
        return match if (match is not None and match.fires) else None
    if not config.SEMANTIC_DISPATCH_ENABLED or time.monotonic() < _disabled_until:
        return None
    started = time.monotonic()
    try:
        match = rank_scores(await _score_descriptions(key))
    except Exception as exc:  # noqa: BLE001 - a dead tunnel must never block dispatch
        _disabled_until = time.monotonic() + _COOLDOWN_S
        logger.warning(
            "SEMANTIC-DISPATCH unavailable (%s: %s); phrase dispatch stands for "
            "the next %.0fs", type(exc).__name__, exc, _COOLDOWN_S,
        )
        return None
    _remember(key, match)
    elapsed = time.monotonic() - started
    if match.fires:
        logger.info(
            "SEMANTIC-DISPATCH %s: score=%.3f runner_up=%s(%.3f) threshold=%.2f %.2fs | %s",
            match.kind, match.score, match.runner_up, match.runner_up_score,
            SEMANTIC_DISPATCH_THRESHOLD, elapsed, key[:120],
        )
        return match
    logger.info(
        "SEMANTIC-DISPATCH no-fire: best=%s(%.3f) runner_up=%s(%.3f) threshold=%.2f %.2fs | %s",
        match.kind, match.score, match.runner_up, match.runner_up_score,
        SEMANTIC_DISPATCH_THRESHOLD, elapsed, key[:120],
    )
    return None
