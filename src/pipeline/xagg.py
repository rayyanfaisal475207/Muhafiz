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
_STATION_KEYWORDS = ("station", "thana", "تھانہ", "چوکی")
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

    if _matches_any(query_lower, _VEHICLE_KEYWORDS):
        top = await _top_recurring_nodes("Vehicle", jurisdiction_case_ids=jurisdiction_case_ids)
        return {"kind": "graph_recurrence", "entity_type": "Vehicle", "results": top}

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
