# ============================================================
# XAGG — cross-case aggregate queries (Phase 5.4).
#
# Deliberately NOT a general text-to-SQL/Cypher system — mirrors
# sql_extractor.py's scoped, template-based approach rather than
# inventing a new paradigm. Two canned aggregate families, selected by a
# simple keyword match on the query:
#   - relational: group Case rows (police_station / investigation_status /
#     crime_category) via the existing gateway.get_cases() — no new
#     DataGateway surface needed.
#   - graph: count how many distinct cases each Vehicle/Person node
#     touches via BELONGS_TO_CASE, for "top recurring X across cases"
#     questions — one Cypher query via age_client.execute_cypher.
# Every result is inherently cross-case (that's the point of XAGG), so
# the caller labels it as a cross-case finding same as XGRAPH.
# ============================================================

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Optional

from src.graph import age_client
from src.database.postgres import current_cross_case, current_rls_active
from src.graph.community_detection import build_canonical_map, canon, fetch_confirmed_same_as
from src.ingestion.muhafiz_cases import split_crime_category

logger = logging.getLogger(__name__)

_VEHICLE_KEYWORDS = (
    "vehicle", "car", "motorcycle", "plate", "gari", "gaari", "motorcycle",
    "گاڑی", "موٹرسائیکل", "نمبر پلیٹ",
)
_PERSON_KEYWORDS = (
    "person", "people", "suspect", "offender", "recidivist", "accused",
    "mulzim", "shakhs",
    "شخص", "افراد", "لوگ", "ملزم",
)
# [findings.md Module 4] Weapon never had a keyword family at all — even
# once the router pattern gap is fixed, run_aggregate()'s graph_recurrence
# dispatch below only had Vehicle/Person branches, so a correctly-routed
# weapon query fell through to _station_or_category_counts() and returned
# an unrelated station/category breakdown instead of a weapon ranking.
_WEAPON_KEYWORDS = (
    "weapon", "firearm", "pistol", "gun", "rifle",
    "hathiyar", "hathyar",
    "ہتھیار", "پستول", "بندوق",
)
# [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Root cause 2] A bare "how many
# accused/people in total" question was silently answered by the
# person-recurrence path below (it matches _PERSON_KEYWORDS on "accused"),
# which structurally only returns people appearing in MORE than one case
# (_top_recurring_nodes's own `if len(cases) > 1` filter) — e.g. "how many
# accused persons in total" returned 4 (the repeat offenders) instead of the
# real total (~94 accused entries), with no caveat that the number was
# actually a recurrence count, not a total. This distinguishes the two
# question shapes so a bare total gets a real total instead of being
# silently answered by the recurrence path.
_RECURRENCE_SIGNAL_KEYWORDS = (
    "recurring", "repeat", "multiple cases", "more than one case",
    "several cases", "across cases", "across all cases", "bar bar",
    "دوبارہ", "بار بار", "ایک سے زیادہ",
)
_ACCUSED_TOTAL_KEYWORDS = (
    "total", "grand total", "how many accused", "how many people",
    "how many suspects", "how many mulzim", "kitne mulzim", "kul kitne",
    "کل ملزم", "ملزمان کی تعداد", "کل تعداد",
)
# [Gold-QA fix] Topics the Gold-QA report confirmed the aggregate engine has
# no data path for at all (gender, age, officer assignment, reporting-delay/
# trend-over-time) — matched EARLY, before any entity-recurrence keyword
# family below, so a query naming one of these topics gets an honest "can't
# answer that" instead of silently falling through to an unrelated family
# (e.g. a gender question matching "accused"/"mulzim" in _PERSON_KEYWORDS).
# Gender is handled separately (see _GENDER_KEYWORDS below) since it has a
# real, if not-yet-backfilled, data path; age/officer/trend genuinely have
# none today.
_AGE_KEYWORDS = ("age of", "how old", "average age", "عمر", "اوسط عمر")
_OFFICER_KEYWORDS = (
    "investigating officer", "officer assignment", "assigned officer",
    "which officer", "تفتیشی افسر", "افسر تفتیش",
)
_TREND_KEYWORDS = (
    "trend", "over time", "month over month",
    "year over year", "rate of increase", "رجحان",
)
# [Gold-QA fix — A7] "How many FIRs recorded a reason for a reporting
# delay?" is a COUNT question, now answerable from the Incident node's
# `reporting_delay_reason` property (structured_projection.py). It is
# distinct from a reporting-delay TREND over time (month-over-month),
# which remains unsupported (no time-series) — so "reporting delay" was
# removed from _TREND_KEYWORDS above and the answerable count shape is
# matched here instead. Bilingual (en / Urdu-script / Roman-Urdu),
# covering the A7 phrasing ("wajah batai ... waqe ke kuch arse baad").
_REPORTING_DELAY_KEYWORDS = (
    "reporting delay", "delay reason", "reason for delay", "delay in reporting",
    "late report", "reported late", "delay in reporting",
    "arse baad", "der se", "takheer", "takhir", "der ki wajah",
    "waqe ke kuch arse baad", "foran aane ke",
    "تاخیر", "تاخیر کی وجہ", "دیر سے", "دیر سے رپورٹ",
)
_GENDER_KEYWORDS = (
    "gender", "women", "woman", "female", "male accused", "men accused",
    "عورت", "عورتیں", "خواتین", "مرد", "جنس",
)
_STATION_KEYWORDS = ("station", "thana", "تھانہ", "چوکی")
# [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 2a] Distinct from
# _STATION_KEYWORDS above, which only ever drives the group-by dimension in
# _station_or_category_counts ("how many CASES per station") — a bare "how
# many police stations are there" wants a count of STATIONS themselves,
# which that grouped path can't produce (it counts cases, not distinct
# station values, and would answer "0 groups" for an empty corpus rather
# than the real station count). Router.py's own deterministic override for
# this shape only got this query to XAGG at all; this is what lets XAGG
# actually answer it once it arrives.
_STATION_TOTAL_KEYWORDS = (
    "how many stations", "how many police stations", "total stations",
    "kitne thanay", "kitni thana", "تھانے کتنے", "کتنے تھانے",
)
_DISTRICT_KEYWORDS = ("district", "zila", "zilay", "ضلع")
# Previously English-only, unlike the three keyword sets above — an Urdu
# query mentioning "بند" (closed) or "چوری" (theft) silently skipped the
# status/category filter entirely rather than applying it, since none of
# these matched. Same class of gap as verifier.py's _HEDGE_PHRASES.
# 2026-08-04: extended again — the Urdu-script fix above still had zero
# Roman-Urdu coverage in any of the six lists (confirmed live: Roman-Urdu
# queries never matched any keyword and silently fell through to the
# generic category-count default), and "زیر تفتیش" (under investigation)
# was missing from both language sides.
_STATUS_KEYWORDS = (
    "open", "closed", "status", "pending", "under investigation",
    "band", "khula", "khuli", "zair-e-tafteesh", "zair e tafteesh", "kholay",
    "کھلا", "بند", "حالت", "زیر التواء", "زیر تفتیش",
)
_CATEGORY_KEYWORDS = (
    "theft", "burglary", "fraud", "category", "type of case",
    "chori", "dhoka",
    "چوری", "ڈکیتی", "نقب زنی", "دھوکہ دہی", "قسم",
)
# [Legal-code semantic layer] Additional per-act category keywords, derived
# ONLY from a real, sourced act-level description in police_reference_data
# (category="legal_code_act", populated by scripts/load_legal_code_acts.py
# — see that script's own _KNOWN_ACT_DESCRIPTIONS docstring for why
# description text, and therefore any keyword implying a claim about what
# the act covers, is never invented ahead of a real source). An act gets an
# entry here ONLY in the same change that adds its real description, never
# speculatively ahead of it — PPC and Arms Ordinance 1965 both HAVE real
# descriptions today but deliberately have NO entry here: PPC has no narrow
# vocabulary that wouldn't over-match every query, and Arms Ordinance's own
# natural vocabulary is entirely shadowed by _WEAPON_KEYWORDS below (see
# this block's own CAVEAT). Static, not a live per-query DB lookup, for the
# same zero-runtime-cost reason _WEAPON_KEYWORDS/_VEHICLE_KEYWORDS/
# _PERSON_KEYWORDS above are static tuples rather than a query.
#
# Existing gap this closes once populated: _CATEGORY_KEYWORDS above filters
# by checking whether the keyword itself appears AS A SUBSTRING of the raw
# crime_category string (see _filtered_cases() below) — but this corpus's
# real crime_category values are legal-code names ("PPC, Arms Ordinance
# 1965"), never descriptive words like "theft"/"چوری", so that check
# structurally never fires for a real case today. This dict is matched
# differently (see _filtered_cases()): against a query keyword mapping to
# an ACT NAME, then filtered via crime_category's actual per-act
# membership (split_crime_category), not substring containment.
#
# CAVEAT for whoever populates an entry here: run_aggregate() below checks
# _VEHICLE_KEYWORDS/_PERSON_KEYWORDS/_WEAPON_KEYWORDS BEFORE ever reaching
# _station_or_category_counts()/_filtered_cases() — a keyword here that
# overlaps one of those three (e.g. "weapon"/"pistol" for an Arms Ordinance
# entry) will dispatch to that graph-based recurrence path instead and
# never reach this filter at all. Confirmed live (test_xagg.py). Usually
# the right outcome anyway — Module 4's weapon-type recurrence reads real
# extracted Weapon-node data, a more precise signal than this crime_category
# text field — but pick keyword vocabulary that's actually reachable for
# acts with no existing entity-type family of their own (PECA 2016, CNSA
# 1997, Illegal Dispossession Act 2005), not vocabulary that's shadowed by
# an earlier, better-served dispatch branch.
# "<exact act string, matching a police_reference_data.subject row exactly>": (keyword, ...)
_LEGAL_CODE_ACT_KEYWORDS: dict[str, tuple[str, ...]] = {
    # [Bug fix — eval finding, DeepEval xagg-01] "Arms Ordinance 1965" was
    # missing from this dict entirely, despite being one of the most common
    # acts in the corpus. A query naming the act directly ("how many cases
    # involve the Arms Ordinance") is a statute-name question, not an
    # entity-type one — it doesn't seed a graph weapon-node traversal the
    # way "how many cases involve a pistol" does, so it belongs here, on
    # the relational/crime_category path, same as CNSA 1997/PECA 2016
    # below. Deliberately uses "arms"/"ordinance" vocabulary, never
    # "weapon"/"pistol"/"gun"/"firearm" (already _WEAPON_KEYWORDS) — see
    # this dict's own CAVEAT comment just above: that overlap would still
    # be shadowed by the earlier, better-served graph-recurrence dispatch,
    # which is the right outcome for THOSE queries, just not this one.
    "Arms Ordinance 1965": (
        "arms ordinance", "illegal arms", "unlicensed arms", "arms act",
        "اسلحہ آرڈیننس", "غیر قانونی اسلحہ",
    ),
    "CNSA 1997": ("narcotics", "drug trafficking", "narcotic substances", "منشیات"),
    # "online fraud" deliberately excluded — it contains "fraud" as a
    # substring, which _CATEGORY_KEYWORDS above already claims (checked
    # first in _filtered_cases()); that check would fire first and filter
    # to zero cases, since "fraud" never appears literally in this
    # corpus's real crime_category values — confirmed live via a
    # collision check across every existing keyword tuple in this module
    # before shipping this entry.
    "PECA 2016": ("cybercrime", "cyber crime", "hacking", "cyber harassment"),
    "Illegal Dispossession Act 2005": ("land grabbing", "illegal dispossession", "property grabbing", "قبضہ"),
    "Punjab Domestic Violence Act": ("domestic violence", "گھریلو تشدد"),
}
# A plain "list/show every case" request — distinct from the grouped-count
# queries below (which always answer "counts of cases by X", never the raw
# records). Router previously had nowhere to send this ("list of all cases"
# names no entity, so it isn't XGRAPH's people/vehicle/org enumeration
# either) — it fell through to XGRAPH by wording proximity to "list of all
# PEOPLE mentioned in the cases" and traversed nothing, since "a case" isn't
# a graph node XGRAPH can seed from.
_LIST_ALL_KEYWORDS = (
    "list", "show", "all cases", "every case", "dikhao", "sab cases",
    "فہرست", "تمام مقدمات", "دکھائیں",
)
# A bare "how many total" request — distinct from _LIST_ALL_KEYWORDS (which
# wants the raw records) and from the grouped-count default below (which
# always breaks the answer down by station/category). Live-observed gap
# (demotestfinal.md §7): "کل کتنے کیسز ہیں؟" ("how many cases in total")
# had no keyword family routing to a single number, so it fell through to
# _station_or_category_counts and came back as a category-by-category
# breakdown instead of one total. Bilingual from the start (English +
# Urdu-script + Roman-Urdu), matching the established pattern the other
# keyword sets in this module were each retrofitted to after being found
# English-only first.
_TOTAL_KEYWORDS = (
    "total", "grand total", "how many cases", "how many cases are there",
    "how many cases in total", "overall count",
    "kitne cases", "kul kitne", "kitne kul", "total kitne",
    "کل کتنے", "کتنے کیسز", "کل تعداد", "مجموعی تعداد", "کل کیسز",
    # [Gold-QA fix — D1] "How many FIRs are currently registered?" names
    # FIRs, not "cases", so it missed every phrase above and fell through
    # to _station_or_category_counts's group-by-statute default — which is
    # exactly D1's run-to-run flakiness (a bare number one run, a statute
    # breakdown the next). An FIR record IS a case record in this corpus
    # (get_cases() enumerates the same rows), so a bare "how many FIRs /
    # total number of FIRs / how many FIRs registered" wants one number,
    # the same as "how many cases in total". Added English + Urdu-script +
    # Roman-Urdu, matching the bilingual pattern of the phrases above.
    "how many firs", "how many fir", "how many f.i.r",
    "number of firs", "total firs", "total number of firs",
    "how many firs are registered", "how many firs registered",
    "kitni firs", "kitne firs", "kitni fir", "kul firs",
    "کتنی ایف آئی آر", "کتنے ایف آئی آر", "ایف آئی آر کی تعداد",
    "کل ایف آئی آر",
)


def _matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


# ── Unsupported-filter disclosures ────────────────────────────────────────
# Investigator-facing strings, served when a filter the query asked for
# cannot be evaluated against the corpus actually present. They exist so an
# unanswerable filter degrades to a STATED limitation instead of a silently
# wrong count — the same principle Large-Scale Aggregate already applies to
# XAGG's missing time-wise grouping, applied one layer down at the source.
_UNSUPPORTED_STATUS_FILTER = (
    "Case status could not be filtered: the case records in this corpus do not "
    "carry a structured open/closed status, so the figures below cover all "
    "matching cases regardless of status."
)
_UNSUPPORTED_CRIME_TYPE_FILTER = (
    "Cases could not be filtered by crime type: these records classify offences "
    "by statute (e.g. PPC, CNSA 1997, Arms Ordinance 1965) rather than by crime "
    "category, so the figures below are not narrowed to the requested type."
)
_UNSUPPORTED_JURISDICTION = (
    "The named area could not be matched to a police station or district on "
    "record, so these figures cover all jurisdictions rather than the one asked "
    "about."
)
_STATUTE_GROUPING_NOTE = (
    "Grouped by the statute(s) each case was registered under (e.g. PPC, "
    "CNSA 1997), not by crime type — these records carry no crime-type "
    "classification."
)
# [Gold-QA fix — Root cause 2] Explicit "I can't answer that" strings for
# topics with genuinely no data path yet, returned via
# {"kind": "unsupported_aggregate", ...} rather than letting the query fall
# through to _station_or_category_counts's generic default, which would
# answer a question it was never asked (the report's worst finding: e.g. a
# gender question silently returning a crime-category breakdown).
_UNSUPPORTED_AGE = (
    "Age-based aggregates are not available: accused/witness age is not "
    "currently extracted into this system's data model."
)
_UNSUPPORTED_OFFICER = (
    "Officer-assignment aggregates are not available: investigating-officer "
    "identity is not currently modeled as a queryable field in this system."
)
_UNSUPPORTED_TREND = (
    "Trend/time-series aggregates (month-over-month, year-over-year, etc.) "
    "are not available: this system does not currently compute date-based "
    "aggregates."
)
_REPORTING_DELAY_NOT_YET_POPULATED = (
    "Reporting-delay reasons are not yet recorded as a queryable field in "
    "this deployment's data — the source system carries a "
    "reporting_delay_reason field, but it has not been synced into the graph "
    "yet, so a count of FIRs with a recorded delay reason cannot be produced."
)
_GENDER_NOT_YET_POPULATED = (
    "Gender is not yet recorded against accused/witness records in this "
    "deployment's data — the source system carries a gender field, but it "
    "has not been synced into this system yet, so a gender breakdown cannot "
    "be produced."
)


def _status_filter_supported(cases: list[dict]) -> bool:
    """
    True when investigation_status is actually filterable on THIS corpus.

    Deliberately data-driven rather than a schema-version check: the field
    is free text, so the only honest test is whether any row carries a
    value the open/closed substring match could ever hit. A corpus that
    later regains parseable statuses re-enables the filter with no code
    change; one that never had them stops fabricating answers.
    """
    return any("closed" in (c.get("investigation_status") or "").lower() for c in cases)


def _crime_type_filter_supported(cases: list[dict]) -> bool:
    """
    True when crime_category holds crime TYPES rather than statute names.

    Same data-driven reasoning as _status_filter_supported(). Statute-only
    values ("PPC", "CNSA 1997") mean a crime-type filter can only ever
    return nothing, which must be disclosed rather than reported as zero.
    """
    crime_type_terms = ("theft", "burglary", "fraud", "چوری", "ڈکیتی", "نقب زنی", "دھوکہ دہی")
    return any(
        any(t in (c.get("crime_category") or "").lower() for t in crime_type_terms)
        for c in cases
    )


async def _top_recurring_nodes(
    label: str, limit: int = 10, jurisdiction_case_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    Count distinct cases each node of `label` touches via BELONGS_TO_CASE,
    descending. [Milestone E1] `jurisdiction_case_ids`, when given,
    restricts the match to that case set before the recurrence count is
    computed — the same candidate-set-narrowing role
    `graph_retriever._find_recurring_entities_for_query()`'s own
    `jurisdiction_case_ids` param plays for XGRAPH.
    """
    if jurisdiction_case_ids is not None:
        rows = await age_client.execute_cypher(
            f"MATCH (n:{label})-[:BELONGS_TO_CASE]->(c:Case) WHERE c.case_id IN $case_ids "
            "RETURN n, c",
            params={"case_ids": jurisdiction_case_ids}, columns=["n", "c"],
        )
    else:
        rows = await age_client.execute_cypher(
            f"MATCH (n:{label})-[:BELONGS_TO_CASE]->(c:Case) "
            "RETURN n, c",
            columns=["n", "c"],
        )
    # Physical Person duplicates (unresolved CNIC-less name mentions minted
    # as fresh entity_ids — see same_as_integrity.py's module docstring)
    # would otherwise fragment one real person's cross-case footprint across
    # several low-count entity_id buckets, hiding genuine recurrence. Fold
    # confirmed-duplicate ids to one canonical id first, same mechanism
    # community_summarization.py already uses at read time. Vehicle has no
    # SAME_AS/CNIC-merge concept, so skip the extra query for that label.
    canonical_map: dict[str, str] = {}
    if label == "Person":
        canonical_map = build_canonical_map(await fetch_confirmed_same_as())

    per_entity_cases: dict[str, set[str]] = {}
    display: dict[str, str] = {}
    for row in rows:
        n_props = (row.get("n") or {}).get("properties", {}) or {}
        c_props = (row.get("c") or {}).get("properties", {}) or {}
        entity_id = n_props.get("entity_id")
        case_id = c_props.get("case_id")
        if not entity_id or not case_id:
            continue
        entity_id = canon(canonical_map, entity_id)
        per_entity_cases.setdefault(entity_id, set()).add(case_id)
        display[entity_id] = n_props.get("canonical_name") or n_props.get("plate") or n_props.get("name") or entity_id

    ranked = sorted(per_entity_cases.items(), key=lambda kv: len(kv[1]), reverse=True)
    return [
        {"entity_id": eid, "name": display.get(eid, eid), "case_count": len(cases), "case_ids": sorted(cases)}
        for eid, cases in ranked[:limit]
        if len(cases) > 1  # "recurring" — appearing in only one case isn't a cross-case pattern
    ]


# [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 1a] A bare "how many accused
# persons in total" was previously answered by _top_recurring_nodes("Person")
# above (it matches _PERSON_KEYWORDS on "accused") — which only ever returns
# the subset appearing in MORE than one case ("recurring" — the `if
# len(cases) > 1` filter just above). Live-confirmed: that returned 4 for
# "how many accused in total" when the real cross-case headcount is far
# higher. This counts every DISTINCT accused Person node instead — no
# `len(cases) > 1` filter — reusing the exact same canonicalization
# (build_canonical_map/canon) so a person who is the same real individual
# across cases is still counted once, but a person appearing in only ONE
# case is counted too (unlike the recurrence path above).
async def _total_accused_count(jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    if jurisdiction_case_ids is not None:
        rows = await age_client.execute_cypher(
            "MATCH (p:Person)-[r:INVOLVED_IN]->(i:Incident)-[:BELONGS_TO_CASE]->(c:Case) "
            "WHERE r.role = 'accused' AND c.case_id IN $case_ids "
            "RETURN p, c",
            params={"case_ids": jurisdiction_case_ids}, columns=["p", "c"],
        )
    else:
        rows = await age_client.execute_cypher(
            "MATCH (p:Person)-[r:INVOLVED_IN]->(i:Incident)-[:BELONGS_TO_CASE]->(c:Case) "
            "WHERE r.role = 'accused' "
            "RETURN p, c",
            columns=["p", "c"],
        )
    canonical_map = build_canonical_map(await fetch_confirmed_same_as())
    per_entity_cases: dict[str, set[str]] = {}
    for row in rows:
        p_props = (row.get("p") or {}).get("properties", {}) or {}
        c_props = (row.get("c") or {}).get("properties", {}) or {}
        entity_id = p_props.get("entity_id")
        case_id = c_props.get("case_id")
        if not entity_id or not case_id:
            continue
        entity_id = canon(canonical_map, entity_id)
        per_entity_cases.setdefault(entity_id, set()).add(case_id)
    return {
        "kind": "total_accused_count",
        "total_accused": len(per_entity_cases),
        "total_case_scoped_entries": sum(len(v) for v in per_entity_cases.values()),
    }


# [Gold-QA fix — Module 1d] Gender breakdown of accused Person nodes.
# `gender` is written onto the Person node by structured_projection.py's
# _write_accused()/_write_witnesses() ONLY once that ingestion change has
# landed and the corpus has been re-synced — see ROOT_CAUSE_AND_FIXES.md
# Module 1's own data-dependency note. Deliberately checks whether ANY node
# actually carries the property before claiming a breakdown, the same
# data-driven "can this filter even fire on this corpus" pattern
# _status_filter_supported()/_crime_type_filter_supported() above use — a
# corpus with the property populated self-heals with no code change; one
# that doesn't states that honestly instead of returning an all-zero or
# fabricated breakdown.
async def _gender_breakdown(jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    if jurisdiction_case_ids is not None:
        rows = await age_client.execute_cypher(
            "MATCH (p:Person)-[r:INVOLVED_IN]->(i:Incident)-[:BELONGS_TO_CASE]->(c:Case) "
            "WHERE r.role = 'accused' AND c.case_id IN $case_ids "
            "RETURN p",
            params={"case_ids": jurisdiction_case_ids}, columns=["p"],
        )
    else:
        rows = await age_client.execute_cypher(
            "MATCH (p:Person)-[r:INVOLVED_IN]->(i:Incident)-[:BELONGS_TO_CASE]->(c:Case) "
            "WHERE r.role = 'accused' "
            "RETURN p",
            columns=["p"],
        )
    seen_entities: dict[str, Optional[str]] = {}
    for row in rows:
        p_props = (row.get("p") or {}).get("properties", {}) or {}
        entity_id = p_props.get("entity_id")
        if not entity_id:
            continue
        seen_entities[entity_id] = (p_props.get("gender") or "").strip() or None

    if not any(v for v in seen_entities.values()):
        return {"kind": "gender_breakdown", "unsupported": True, "message": _GENDER_NOT_YET_POPULATED}

    counts = Counter((v or "unknown").lower() for v in seen_entities.values())
    return {
        "kind": "gender_breakdown",
        "unsupported": False,
        "counts": [{"key": k, "count": v} for k, v in counts.most_common()],
        "total_accused": len(seen_entities),
    }


async def _reporting_delay_count(jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    """
    [Gold-QA fix — A7] Count FIRs that recorded a reporting-delay reason,
    against the total FIR count, from the Incident node's
    `reporting_delay_reason` property (structured_projection.py projects it;
    a blank/absent reason writes no property, so a node HAVING the property
    is exactly "this FIR recorded a delay reason").

    Modeled on `_gender_breakdown` above: one Cypher read, a graceful
    "not yet populated" fallback when the property is absent everywhere
    (i.e. the graph predates this projection and hasn't been re-ingested),
    so the answer degrades to a stated limitation instead of a wrong "0".

    Counts DISTINCT Incidents (one per FIR) so a multi-chunk FIR is not
    double-counted. `with_delay` is FIRs carrying a reason; `total` is all
    FIRs reachable as Incidents in scope — the denominator the A7 gold
    answer uses ("8 of 73").
    """
    case_filter = ""
    params: dict = {}
    if jurisdiction_case_ids is not None:
        case_filter = "WHERE c.case_id IN $case_ids"
        params = {"case_ids": jurisdiction_case_ids}

    # Total FIRs (Incidents) in scope — the denominator.
    total_rows = await age_client.execute_cypher(
        f"MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case) {case_filter} "
        "RETURN count(DISTINCT i) AS n",
        params=params, columns=["n"],
    )
    total = int((total_rows[0] or {}).get("n") or 0) if total_rows else 0

    # FIRs whose Incident carries a reporting_delay_reason property.
    where_delay = "WHERE i.reporting_delay_reason IS NOT NULL"
    if jurisdiction_case_ids is not None:
        where_delay += " AND c.case_id IN $case_ids"
    delay_rows = await age_client.execute_cypher(
        f"MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case) {where_delay} "
        "RETURN count(DISTINCT i) AS n",
        params=params, columns=["n"],
    )
    with_delay = int((delay_rows[0] or {}).get("n") or 0) if delay_rows else 0

    # Not-yet-populated: the property exists nowhere AND there are FIRs to
    # check — distinguish "graph predates the projection" (honest can't-
    # answer) from a real zero on a re-ingested corpus. If total is 0 too,
    # there is simply no data; report the honest zero rather than a
    # misleading "not populated".
    if with_delay == 0 and total > 0:
        # Confirm the property is genuinely absent everywhere (not just a
        # real zero) before disclaiming — one probe for ANY node carrying it.
        probe = await age_client.execute_cypher(
            "MATCH (i:Incident) WHERE i.reporting_delay_reason IS NOT NULL "
            "RETURN count(i) AS n LIMIT 1",
            columns=["n"],
        )
        any_populated = int((probe[0] or {}).get("n") or 0) if probe else 0
        if any_populated == 0:
            return {
                "kind": "reporting_delay_count",
                "unsupported": True,
                "message": _REPORTING_DELAY_NOT_YET_POPULATED,
            }

    return {
        "kind": "reporting_delay_count",
        "unsupported": False,
        "with_delay_reason": with_delay,
        "total_firs": total,
    }


# [Gold-QA fix — Module 1c] District-level rollup — District/PoliceStation
# graph nodes already exist (structured_projection.py's District writes),
# so this is a graph traversal, NOT a Postgres GROUP BY over the case rows
# the way _station_or_category_counts() works — case rows only carry a
# free-text `police_station` name, no district. Optionally filtered to a
# named recurring-entity label (e.g. "Weapon") for "which district recovers
# the most weapons" style questions; None counts cases per district instead.
async def _top_districts_by(
    entity_label: Optional[str] = None, jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    case_filter = "WHERE c.case_id IN $case_ids " if jurisdiction_case_ids is not None else ""
    params = {"case_ids": jurisdiction_case_ids} if jurisdiction_case_ids is not None else {}
    if entity_label:
        query = (
            f"MATCH (n:{entity_label})-[:BELONGS_TO_CASE]->(c:Case)-[:FILED_AT]->(:PoliceStation)"
            f"-[:PART_OF]->(d:District) {case_filter}"
            "RETURN d.name AS district, count(DISTINCT n) AS n_count"
        )
    else:
        query = (
            f"MATCH (c:Case)-[:FILED_AT]->(:PoliceStation)-[:PART_OF]->(d:District) {case_filter}"
            "RETURN d.name AS district, count(DISTINCT c) AS n_count"
        )
    rows = await age_client.execute_cypher(query, params=params, columns=["district", "n_count"])
    ranked = sorted(
        ({"district": r.get("district") or "unknown", "count": r.get("n_count") or 0} for r in rows),
        key=lambda r: r["count"], reverse=True,
    )
    return {"kind": "district_breakdown", "entity_label": entity_label, "counts": ranked}


# [Gold-QA fix — Module 2a] "How many police stations are there" — a count
# of distinct PoliceStation graph nodes, not of cases per station (that's
# _station_or_category_counts's job, a different question). Reads directly
# from the graph's PoliceStation node set (written once per real station by
# structured_projection.py's _write_jurisdiction()) rather than counting
# distinct police_station strings off case rows, since a station with zero
# currently-open cases is still a real station.
async def _station_total_count() -> dict:
    rows = await age_client.execute_cypher(
        "MATCH (s:PoliceStation) RETURN DISTINCT s.station_id AS station_id",
        columns=["station_id"],
    )
    station_ids = {r.get("station_id") for r in rows if r.get("station_id")}
    return {"kind": "station_total_count", "total_stations": len(station_ids)}


# [findings.md Module 4] Strips a trailing ammunition-count clause shaped
# "بمعہ N گولیاں" ("with N bullets") — e.g. "30 بور پستول بمعہ 3 گولیاں"
# and "...بمعہ 6 گولیاں" are the SAME weapon type as bare "30 بور پستول",
# differing only by how many rounds happened to be recovered with it.
# Verified against real sampled canonical_name values from the live graph
# (structured_projection._write_weapons() writes w.get("item_detail")
# verbatim as canonical_name — see that function's own docstring for why
# Weapon nodes can't be grouped by entity_id/node-identity at all).
# `[0-9۰-۹]+` covers both ASCII and Urdu-Indic digit scripts defensively
# (observed samples are ASCII-only, but this corpus mixes both scripts
# elsewhere). Anchored at end-of-string and requires the literal
# "بمعہ ... گولیاں" shape, so it can't strip a caliber/model token that
# isn't actually an ammunition-count clause — deliberately narrow to
# avoid merging genuinely distinct weapon types together.
_WEAPON_SUFFIX_RE = re.compile(r"\s*بمعہ\s*[0-9۰-۹]+\s*گولیاں\s*$")


def _normalize_weapon_type(name: str) -> str:
    return _WEAPON_SUFFIX_RE.sub("", name or "").strip()


async def _top_recurring_weapon_types(
    limit: int = 10, jurisdiction_case_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    Group Weapon nodes by a normalized weapon-type string (see
    _normalize_weapon_type) and count DISTINCT CASES per group — NOT by
    node identity the way _top_recurring_nodes() does.

    [findings.md Module 4, root cause #3] Weapon entity_ids are FIR-scoped
    by construction: structured_projection._write_weapons() builds each
    one as f"WEAPON-{w.get('id') or w.get('sr_no')}-{fir.fir_id}" and
    weapons never go through entity_resolution.resolve_and_write()'s
    CNIC-style cross-case merge tier the way Person/Vehicle do. Every real
    Weapon node therefore belongs to exactly one case, permanently, so
    _top_recurring_nodes("Weapon", ...)'s per-entity_id grouping would
    always return an empty list — confirmed against the live graph, not
    just inferred. This function groups by weapon TYPE instead, closer in
    shape to _station_or_category_counts() than to _top_recurring_nodes().
    """
    if jurisdiction_case_ids is not None:
        rows = await age_client.execute_cypher(
            "MATCH (w:Weapon)-[:BELONGS_TO_CASE]->(c:Case) WHERE c.case_id IN $case_ids "
            "RETURN w.canonical_name AS weapon_name, c.case_id AS case_id",
            params={"case_ids": jurisdiction_case_ids}, columns=["weapon_name", "case_id"],
        )
    else:
        rows = await age_client.execute_cypher(
            "MATCH (w:Weapon)-[:BELONGS_TO_CASE]->(c:Case) "
            "RETURN w.canonical_name AS weapon_name, c.case_id AS case_id",
            columns=["weapon_name", "case_id"],
        )
    per_type_cases: dict[str, set[str]] = {}
    for row in rows:
        raw_name = row.get("weapon_name")
        case_id = row.get("case_id")
        if not raw_name or not case_id:
            continue
        normalized = _normalize_weapon_type(raw_name)
        if not normalized:
            continue
        per_type_cases.setdefault(normalized, set()).add(case_id)

    ranked = sorted(per_type_cases.items(), key=lambda kv: len(kv[1]), reverse=True)
    return [
        # Same key names _top_recurring_nodes() uses ("name"/"case_count"/
        # "case_ids") so the existing graph_recurrence renderers in
        # orchestrator.py and harness/tools/xagg.py need no changes.
        {"name": wtype, "case_count": len(cases), "case_ids": sorted(cases)}
        for wtype, cases in ranked[:limit]
        if len(cases) > 1  # "recurring" — same bar _top_recurring_nodes uses
    ]


async def _filtered_cases(
    gateway, query_text: str, jurisdiction_case_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    The open/closed + category filtering shared by both the grouped-count
    path (_station_or_category_counts) and the grand-total path
    (_total_count) below — pulled out so a query like "how many closed
    cases in total" still respects the status filter instead of the
    grand-total path bypassing it entirely.

    [Milestone E1] `jurisdiction_case_ids`, when given, narrows the case
    set to that allow-list FIRST — before the status/category filtering
    below even runs — same "cut the candidate set up front" goal as the
    graph family's own `jurisdiction_case_ids` handling in
    `_top_recurring_nodes`.
    """
    # The caller (run_aggregate) has already verified the requesting user is
    # supervisor-or-above before reaching here — a cross-case aggregate is
    # meant to cover every case platform-wide, not just ones the caller is
    # individually assigned to. gateway.get_cases() only returns everything
    # for "platform-admin"; passing anything else here (including None)
    # tries to join CaseAssignment on a non-existent user and raises.
    cases = await gateway.get_cases(user_id=None, user_role="platform-admin")
    if jurisdiction_case_ids is not None:
        allowed = set(jurisdiction_case_ids)
        cases = [c for c in cases if c.get("case_id") in allowed]

    # "open" and "closed" are opposite filters — a query naming one must
    # never silently apply the other or (worse) apply neither. Previously
    # only "open" had a branch at all: a query asking for CLOSED cases
    # (English "closed", Urdu "بند"/Roman-Urdu "band") matched
    # _STATUS_KEYWORDS (so the code below it, e.g. category filtering,
    # still ran) but the status filter itself silently no-opped, returning
    # every case regardless of status instead of just the closed ones.
    #
    # 2026-08-24 — THE UNDERLYING ASSUMPTION IS NO LONGER TRUE OF THE DATA.
    # The comment above described migrations/004_case_model.sql's seeded
    # "Closed – Convicted"/"Closed – Untraced" values. The live corpus now
    # comes from the real Muhafiz Data API, where investigation_status is
    # `_current_status()`'s projection of psrms.fir_position's latest row
    # (src/ingestion/muhafiz_cases.py:112) — free-text Urdu narrative such
    # as "ملزم ریمانڈ پر، چالان کی تیاری زیر عمل", and EMPTY STRING for the
    # majority (measured live: 52/73 cases empty, and the sync module's own
    # docstring records 65/94 fir_position rows carrying a null `position`).
    #
    # Measured against the live corpus: `"closed" in status.lower()` matches
    # 0/73 cases and `"open"` matches 0/73. So "how many closed cases" used
    # to answer 0, and "how many open cases" answered 73 — both stated as
    # fact, both wrong, and indistinguishable from a real result.
    #
    # A filter that cannot be evaluated must SAY SO rather than return a
    # confidently wrong number. `_status_filter_supported()` decides that
    # from the data actually present, not from a hardcoded schema guess, so
    # this self-heals if a future corpus does carry parseable statuses.
    _OPEN_TERMS = ("open", "khula", "khuli", "کھلا", "pending", "زیر التواء", "under investigation", "زیر تفتیش")
    _CLOSED_TERMS = ("closed", "band", "بند")

    def _is_closed(c: dict) -> bool:
        return "closed" in (c.get("investigation_status") or "").lower()

    unsupported: list[str] = []

    status_requested = _matches_any(query_text, _CLOSED_TERMS) or _matches_any(query_text, _OPEN_TERMS)
    if status_requested and not _status_filter_supported(cases):
        unsupported.append(_UNSUPPORTED_STATUS_FILTER)
    elif _matches_any(query_text, _CLOSED_TERMS):
        cases = [c for c in cases if _is_closed(c)]
    elif _matches_any(query_text, _OPEN_TERMS):
        cases = [c for c in cases if not _is_closed(c)]

    # Same treatment for crime-type filtering. `crime_category` no longer
    # holds a crime TYPE at all: `_crime_category()`
    # (src/ingestion/muhafiz_cases.py:78) joins the distinct `act` values
    # off psrms.fir_section, so live values are statute lists — "PPC",
    # "PPC, Arms Ordinance 1965", "CNSA 1997", "PECA 2016, PPC". This is
    # deliberate and documented upstream, NOT a sync bug: the real FIR
    # schema (psrms.fir_section) carries only `section_code` and `act`, and
    # has no offence-category field anywhere for the sync to have missed.
    #
    # Measured live: every crime-type keyword in _CATEGORY_KEYWORDS
    # ("theft"/"burglary"/"fraud"/"چوری"/"ڈکیتی"/...) matches 0/73 cases.
    if _matches_any(query_text, _CATEGORY_KEYWORDS):
        for kw in _CATEGORY_KEYWORDS:
            if kw in query_text.lower() and kw not in _STATUS_KEYWORDS + ("category", "type of case"):
                matched = [c for c in cases if kw in (c.get("crime_category") or "").lower()]
                if not matched and not _crime_type_filter_supported(cases):
                    unsupported.append(_UNSUPPORTED_CRIME_TYPE_FILTER)
                else:
                    cases = matched
                break

    # [Legal-code semantic layer] A query matching a known act's own
    # keyword list (_LEGAL_CODE_ACT_KEYWORDS' own comment explains why this
    # is empty until a real description exists) filters to cases whose
    # crime_category actually NAMES that act — split on the comma-joined
    # multi-act string first (split_crime_category), so this correctly
    # matches a case like "CNSA 1997, Arms Ordinance 1965" even though
    # "Arms Ordinance 1965" isn't the field's whole value. Independent of
    # the _CATEGORY_KEYWORDS substring check just above: that check matches
    # a keyword directly against the raw crime_category string, which
    # structurally never fires for this corpus's real values (legal-code
    # names like "PPC, Arms Ordinance 1965", never descriptive words like
    # "theft"/"چوری") — this is the actual fix for that gap, not a
    # duplicate of it. Deliberately does not touch `unsupported`: when this
    # matches, the filter WORKED, which is exactly what this block exists
    # to make true for the acts it knows about.
    for act, keywords in _LEGAL_CODE_ACT_KEYWORDS.items():
        if _matches_any(query_text, keywords):
            cases = [c for c in cases if act in split_crime_category(c.get("crime_category"))]
            break

    return cases, unsupported


async def _station_or_category_counts(
    gateway, query_text: str, jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    cases, unsupported = await _filtered_cases(gateway, query_text, jurisdiction_case_ids)
    group_field = "police_station" if _matches_any(query_text, _STATION_KEYWORDS) else "crime_category"
    counts = Counter(c.get(group_field) or "unknown" for c in cases)

    # Grouping by crime_category is itself misleading now — the key is a
    # statute list, not a crime type — so say what the grouping actually
    # IS rather than letting "category" imply an offence taxonomy.
    if group_field == "crime_category" and not _crime_type_filter_supported(cases):
        unsupported = unsupported + [_STATUTE_GROUPING_NOTE]
    result = {
        "group_by": group_field,
        "counts": [{"key": k, "count": v} for k, v in counts.most_common(15)],
        "total_cases_considered": len(cases),
        "unsupported_filters": unsupported,
    }
    # [Legal-code semantic layer] crime_category is a comma-joined,
    # potentially multi-act free-text field (a real FIR can carry several
    # acts — src/ingestion/muhafiz_cases.py::_crime_category()'s own
    # docstring) — the raw-string "counts" above therefore fragments a
    # single legal basis across every distinct combination it happens to
    # co-occur with (e.g. "PPC, Arms Ordinance 1965" and "CNSA 1997, Arms
    # Ordinance 1965" are two separate buckets above, even though both are
    # real Arms-Ordinance cases — 21 + 8 = 29, invisible as one number
    # anywhere before this). "counts_by_act" re-derives a per-ACT breakdown
    # instead: each case's acts are split (split_crime_category) and
    # counted individually, so a multi-act case counts under every act it
    # carries. Purely additive — "counts" above is untouched, so no
    # existing caller/renderer needs to change for this to be safe to ship.
    if group_field == "crime_category":
        act_counts: Counter = Counter()
        for c in cases:
            for act in split_crime_category(c.get("crime_category")):
                act_counts[act] += 1
        result["counts_by_act"] = [{"key": k, "count": v} for k, v in act_counts.most_common(15)]
    return result


async def _total_count(
    gateway, query_text: str, jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """A bare "how many total" answer — no grouping, one number. Still
    honors any status/category filter present (e.g. "how many closed
    cases in total"), it just skips the group-by breakdown entirely."""
    cases, unsupported = await _filtered_cases(gateway, query_text, jurisdiction_case_ids)
    return {"kind": "total_count", "total_cases": len(cases), "unsupported_filters": unsupported}


async def run_aggregate(
    query_text: str,
    target_entity: Optional[str],
    gateway,
    user_id: Optional[str] = None,
    user_role: str = "investigator",
    jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """
    Dispatch to the relational or graph aggregate family based on simple
    keyword matching, and return a small result dict the orchestrator
    formats into the cross-case-labeled response.

    Cross-case, same as XGRAPH — requires the same supervisor-or-higher
    role gate and audit logging (Phase 7 RBAC applies to every cross-case
    route uniformly, not just graph traversal).

    `jurisdiction_case_ids` [Milestone E1]: the case_id allow-list from
    `graph_retriever.resolve_jurisdiction_case_ids()`, already resolved
    (and role-gated) by the orchestrator before this call — `None` when
    the query named no station/district, in which case every family below
    runs exactly as it did before this milestone.
    """
    if user_role not in ("supervisor", "station-admin", "platform-admin"):
        logger.warning("Unauthorized cross-case aggregate query attempted by %s (user_id: %s)", user_role, user_id)
        try:
            await gateway.log_audit_event(
                event_type="authorization_violation",
                user_id=user_id,
                case_id=None,
                details={"target_entity": target_entity, "query": query_text, "role": user_role, "route": "XAGG"},
            )
        except Exception as e:
            logger.error("Failed to audit log unauthorized XAGG attempt: %s", e)
        raise PermissionError("Cross-case aggregate queries require supervisor role or higher.")

    try:
        await gateway.log_audit_event(
            event_type="cross_case_aggregate",
            user_id=user_id,
            case_id=None,
            details={"target_entity": target_entity, "query": query_text},
        )
    except Exception as e:
        logger.error("Failed to audit log cross-case aggregate query: %s", e)

    # Phase 2: arm the Postgres RLS cross-case bypass only now that the
    # role check above has passed — same fix/rationale as
    # graph_retriever.py::retrieve_graph(). See that function's comment.
    # Also self-arm rls_active here (security-review addendum): this used
    # to rely entirely on the caller (chat_endpoint's set_case_scope())
    # having already armed it, a convention enforced only by docstring —
    # a future second caller of run_aggregate() that forgets to arm RLS
    # upstream would otherwise run with app.rls_active never set, which
    # migration 010's policies treat as "RLS fully inactive" (fail-open).
    current_rls_active.set(True)
    current_cross_case.set(True)

    query_lower = query_text.lower()

    # [Gold-QA fix — Module 1b] Topics with genuinely no data path yet,
    # checked FIRST — before any entity-recurrence keyword family below —
    # so a query naming one of these gets an honest refusal instead of
    # silently falling through to an unrelated family (the report's worst
    # finding: a gender/age/officer/trend question answered with an
    # unrelated number and no caveat). Order matters: age/officer/trend
    # have no data path at all; gender has a real one but is checked
    # separately below since it degrades to an honest "not synced yet"
    # rather than a hard refusal.
    if _matches_any(query_lower, _AGE_KEYWORDS):
        return {"kind": "unsupported_aggregate", "message": _UNSUPPORTED_AGE}
    # [Gold-QA fix — A7] Reporting-delay COUNT checked before officer/trend:
    # it now has a real data path (Incident.reporting_delay_reason), and it
    # degrades to an honest "not synced yet" like gender, not a hard refusal.
    # Placed ahead of _OFFICER_KEYWORDS because A7's phrasing ("mudai ne ...
    # wajah batai") names no officer term, but ahead of _TREND_KEYWORDS is
    # what matters now that "reporting delay" was moved out of that set.
    if _matches_any(query_lower, _REPORTING_DELAY_KEYWORDS):
        return await _reporting_delay_count(jurisdiction_case_ids=jurisdiction_case_ids)
    if _matches_any(query_lower, _OFFICER_KEYWORDS):
        return {"kind": "unsupported_aggregate", "message": _UNSUPPORTED_OFFICER}
    if _matches_any(query_lower, _TREND_KEYWORDS):
        return {"kind": "unsupported_aggregate", "message": _UNSUPPORTED_TREND}
    if _matches_any(query_lower, _GENDER_KEYWORDS):
        return await _gender_breakdown(jurisdiction_case_ids=jurisdiction_case_ids)

    # [Gold-QA fix — Module 2a] A bare "how many police stations are
    # there" — checked before _STATION_KEYWORDS's own group-by dispatch
    # further below, since that path counts CASES per station, not
    # stations themselves (see _station_total_count()'s own docstring).
    if _matches_any(query_lower, _STATION_TOTAL_KEYWORDS):
        return await _station_total_count()

    # [Gold-QA fix — Module 1c] District rollup — checked before the
    # station/vehicle/person/weapon families below since "which district
    # recovers the most weapons" would otherwise be caught by
    # _WEAPON_KEYWORDS first and answer with a case-scoped weapon ranking
    # instead of the district breakdown actually asked for.
    if _matches_any(query_lower, _DISTRICT_KEYWORDS):
        entity_label = None
        if _matches_any(query_lower, _WEAPON_KEYWORDS):
            entity_label = "Weapon"
        elif _matches_any(query_lower, _VEHICLE_KEYWORDS):
            entity_label = "Vehicle"
        return await _top_districts_by(entity_label, jurisdiction_case_ids=jurisdiction_case_ids)

    if _matches_any(query_lower, _VEHICLE_KEYWORDS):
        top = await _top_recurring_nodes("Vehicle", jurisdiction_case_ids=jurisdiction_case_ids)
        return {"kind": "graph_recurrence", "entity_type": "Vehicle", "results": top}

    # [Gold-QA fix — Module 1a] A bare total ("how many accused persons in
    # total") must NOT reach the recurring-persons dispatch just below —
    # that path structurally excludes anyone appearing in only one case
    # (see _top_recurring_nodes's own `if len(cases) > 1`), which is why
    # "how many accused" used to return 4 instead of the real headcount.
    # Only fires when the query names a bare-total shape AND does not also
    # carry recurrence language ("recurring", "multiple cases", ...) — a
    # query that names both (unlikely, but e.g. "how many people appear in
    # multiple cases") still means recurrence, so the check below is
    # deliberately AND NOT, matching _TOTAL_KEYWORDS/_LIST_ALL_KEYWORDS's
    # own precedence pattern elsewhere in this function.
    if _matches_any(query_lower, _PERSON_KEYWORDS) and _matches_any(
        query_lower, _ACCUSED_TOTAL_KEYWORDS
    ) and not _matches_any(query_lower, _RECURRENCE_SIGNAL_KEYWORDS):
        return await _total_accused_count(jurisdiction_case_ids=jurisdiction_case_ids)

    if _matches_any(query_lower, _PERSON_KEYWORDS):
        top = await _top_recurring_nodes("Person", jurisdiction_case_ids=jurisdiction_case_ids)
        return {"kind": "graph_recurrence", "entity_type": "Person", "results": top}

    # [findings.md Module 4] Do NOT call _top_recurring_nodes("Weapon", ...)
    # here — see _top_recurring_weapon_types()'s own docstring for why that
    # would always return [] for real data.
    if _matches_any(query_lower, _WEAPON_KEYWORDS):
        top = await _top_recurring_weapon_types(jurisdiction_case_ids=jurisdiction_case_ids)
        return {"kind": "graph_recurrence", "entity_type": "Weapon", "results": top}

    # A plain enumeration ("list of all cases") with no station/category/status
    # grouping language present — answer with the raw case records rather than
    # forcing it through _station_or_category_counts's group-by, which would
    # silently turn "list all cases" into "counts of cases by category" instead
    # of actually listing them.
    #
    # [Bug fix — eval finding, DeepEval xagg-01] "How many cases involve the
    # Arms Ordinance ACROSS ALL CASES? Give a count." contains the literal
    # substring "all cases" (from "across all cases"), which matched
    # _LIST_ALL_KEYWORDS. The guard below only backed off for station/status/
    # category keyword collisions, never for a query that also names a
    # specific legal act — so this branch fired, returned every case in the
    # corpus completely unfiltered (79, the whole corpus), and labeled it
    # "matching" a query about one act. Confirmed live in the pipeline output:
    # cases with crime_category "PPC" only, and even "uncategorized" stub
    # cases, were listed as "matching" an Arms-Ordinance question. Ground
    # truth (SQL COUNT WHERE crime_category ILIKE '%Arms Ordinance%') is 29.
    # A query naming a specific act is asking a filtered/counted question,
    # not "list every case" — same reasoning as the existing station/status/
    # category exclusions, just extended to cover this fourth filter family.
    if _matches_any(query_lower, _LIST_ALL_KEYWORDS) and not _matches_any(
        query_lower, _STATION_KEYWORDS + _STATUS_KEYWORDS + _CATEGORY_KEYWORDS
    ) and not any(
        _matches_any(query_lower, keywords) for keywords in _LEGAL_CODE_ACT_KEYWORDS.values()
    ):
        cases = await gateway.get_cases(user_id=None, user_role="platform-admin")
        if jurisdiction_case_ids is not None:
            allowed = set(jurisdiction_case_ids)
            cases = [c for c in cases if c.get("case_id") in allowed]
        return {
            "kind": "case_listing",
            "cases": [
                {
                    "case_id": c.get("case_id"),
                    "fir_number": c.get("fir_number"),
                    "crime_category": c.get("crime_category"),
                    "investigation_status": c.get("investigation_status"),
                    "police_station": c.get("police_station"),
                }
                for c in cases
            ],
        }

    # Grand-total: "how many cases in total", with no explicit group-by
    # signal (station/category) present — a query naming a group-by
    # dimension alongside "total" (e.g. "total cases per station") still
    # wants the breakdown, not a bare number, so this only fires when no
    # grouping keyword is also present, the same precedence _LIST_ALL_KEYWORDS
    # already uses above.
    if _matches_any(query_lower, _TOTAL_KEYWORDS) and not _matches_any(
        query_lower, _STATION_KEYWORDS + _CATEGORY_KEYWORDS
    ):
        return await _total_count(gateway, query_text, jurisdiction_case_ids)

    result = await _station_or_category_counts(gateway, query_text, jurisdiction_case_ids)
    return {"kind": "relational_aggregate", **result}
