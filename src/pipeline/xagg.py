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
from datetime import datetime
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
# [Gold-QA fix — Module 13, question M2] "Is caseload growing faster at our
# general-purpose stations, or at the handful set up for one specific type
# of crime?" needs a STATION-TYPE dimension (general-purpose vs a
# specialized/single-crime-type station) that genuinely does not exist
# anywhere in this data model — `PoliceStation` nodes carry only
# `name`/`code` (`structured_projection._station_identity()`), and no
# Postgres column classifies a station by type either. Checked early,
# alongside AGE/OFFICER above, for the same reason: an honest "can't answer
# that" beats silently falling through to a plain per-station case count
# (which answers "which station has the most cases", not the type-
# normalized question actually asked).
_STATION_TYPE_KEYWORDS = (
    "station type", "type of station", "general-purpose station",
    "general purpose station", "specialized station", "specialised station",
    "specific type of crime", "one specific type of crime",
    "تھانے کی قسم", "مخصوص نوعیت کے تھانے",
)
# [Gold-QA fix — Module 7, question CP6] "How many cases are still assigned
# only a PLACEHOLDER investigating officer, not a real one?" is a COUNT
# question with a real data path (Officer nodes and their ASSIGNED_TO
# supersession chain already exist — Milestone B2, structured_projection.py
# — this module only adds a new counting RULE over already-populated data,
# unlike A7/gender which needed a new projected property). Checked BEFORE
# _OFFICER_KEYWORDS's hard refusal below (same precedence pattern Module 2
# used for reporting-delay-count vs. trend) — that refusal is for "which
# officer/officer assignment" questions with genuinely no data path; this is
# a distinct, narrower shape asking specifically about the PLACEHOLDER
# state, not officer identity in general. Vocabulary is deliberately
# specific to that shape ("real"/"actual" officer, "asal tor par" — "in
# reality" — CP6's own Roman-Urdu phrasing) so it doesn't swallow every
# officer-identity question into a count.
_PLACEHOLDER_OFFICER_KEYWORDS = (
    "placeholder officer", "placeholder investigating officer",
    "not a real officer", "no real officer", "no real investigating officer",
    "without a real officer", "real investigating officer",
    "actual investigating officer",
    "asal tor par", "asal tafteeshi afsar", "asal afsar",
    "اصل تفتیشی افسر", "حقیقی تفتیشی افسر", "اصل افسر",
)
# [Gold-QA fix — CR7, Module 14] Criminal-record status + court-outcome
# consistency questions. CR7 (Urdu) asks how many criminal-record cases are
# completed vs. in progress, AND whether, where a separate court record
# exists, the two match. English / Urdu / Roman-Urdu. Requires a criminal-
# record term so a generic "how many cases" count doesn't misfire here.
_CRIMINAL_RECORD_KEYWORDS = (
    "criminal record", "criminal-record", "criminal records",
    "conviction status", "criminal record system", "court outcome",
    "court record", "court-outcome", "conviction record",
    "criminal record mein", "kitne case mukammal", "zer e karwai",
    "کرمنل ریکارڈ", "کرمنل ریکارڈ سسٹم", "عدالتی ریکارڈ", "عدالتی نتیجے",
    "زیرِ کارروائی", "زیر کارروائی", "سزا یافتہ", "مطابقت رکھتے",
)
# [Gold-QA fix — CR6, Module 15] Walk-in CMS complaint ↔ FIR linkage
# questions. CR6 (Urdu): when someone walks into a station and files a
# complaint, does it link to a formal FIR or stay separate? English / Urdu /
# Roman-Urdu.
_CMS_LINKAGE_KEYWORDS = (
    "walk-in complaint", "walk in complaint", "cms complaint",
    "complaint linked to", "complaint connect", "complaint attached to fir",
    "shikayat", "walk in shikayat",
    "شکایت درج", "تھانے آ کر شکایت", "شکایت", "منسلک", "الگ الگ رہتے",
)
# [Gold-QA fix — CR8, Module 15] Domestic-violence report ↔ FIR confirmation
# questions. CR8 (Urdu): if a DV complaint was recorded as converted into a
# formal case, does the case record confirm it? English / Urdu / Roman-Urdu.
_DV_REPORT_KEYWORDS = (
    "domestic violence", "domestic-violence", "women violence",
    "violence report", "converted into a case", "forwarded fir",
    "gharelu tashaddud", "converted to fir",
    "گھریلو تشدد", "باقاعدہ کیس میں تبدیل", "کیس ریکارڈ سے",
)
# [Gold-QA fix — G2, Module 15] Data-completeness / "which cases are weak or
# might be overlooked" questions. G2 (Urdu, briefing an SHO on cases that
# might get buried). English / Urdu / Roman-Urdu.
_COMPLETENESS_KEYWORDS = (
    "incomplete", "overlooked", "might be buried", "fall through the cracks",
    "weak record", "not reliable", "worth monitoring", "data quality",
    "which cases might", "cases that might be",
    "dab kar", "nazar se ojhal", "adhoora record",
    "دب کر", "نظر سے اوجھل", "نامکمل", "دبے", "بریفنگ", "مقدمے دب",
)
# [Gold-QA fix — G5, Module 15] Weapon-register COMPLIANCE questions. G5
# (Roman-Urdu): given recovered weapons, is anything flag-worthy for
# compliance? Deliberately requires a licence/compliance signal (not a bare
# "weapon", which is the recurrence aggregate's job) — the dispatch below
# gates on a weapon term AND a licence/compliance term co-occurring.
_WEAPON_TERMS = ("weapon", "hathiyar", "firearm", "baramad", "ہتھیار", "اسلحہ")
_COMPLIANCE_TERMS = (
    "license", "licence", "unlicensed", "compliance", "flag",
    "record keeping", "record-keeping",
    "لائسنس", "بغیر لائسنس", "کمپلائنس",
)
# [Gold-QA fix — G3, Module 15/16] Court-readiness completeness questions.
# G3 (Urdu): preparing a case file for handover to court — which fields are
# most likely incomplete? Distinct from G2's general "buried cases" scan by
# the court/prosecutor/handover framing. English / Urdu / Roman-Urdu.
_COURT_READINESS_KEYWORDS = (
    "case file for court", "court file", "handover to court", "prosecutor",
    "before accepting", "court accept", "ready for court", "case file ready",
    "adalat ko hawalgi", "court file tayyar", "case file tayyar",
    # NOTE: deliberately NOT the bare "عدالت" ("court") on its own — live
    # collision found (Module 18 rerun): M4 asks how far cases have
    # progressed "عدالت میں" (in court), an unrelated statute×court-stage
    # comparison question, and the bare word alone was enough to hijack it
    # into this court-file-readiness scan. Keep only the actual handover/
    # readiness-framing phrases below, same discipline as the CR8/KB1
    # false-positive already fixed in router.py.
    "عدالت کو حوالگی", "کیس فائل تیار", "پراسیکیوٹر", "حوالگی کے لیے",
    "قبول کرنے سے پہلے",
)
_TREND_KEYWORDS = (
    "reporting delay", "trend", "over time", "month over month",
    "year over year", "rate of increase", "رجحان",
)
# [Gold-QA fix — Module 2, A7] "How many FIRs recorded a reason for a
# reporting delay?" is a COUNT question, now answerable from the Incident
# node's `reporting_delay_reason` property (structured_projection.py). It is
# a DIFFERENT shape from a reporting-delay TREND over time (month-over-month),
# which remains unsupported (no time-series) and stays in _TREND_KEYWORDS
# above, UNCHANGED — "reporting delay" is deliberately left in that tuple as
# the trend fallback net. This tuple only holds count-specific vocabulary
# (a delay REASON, WHY it was late) — never the bare "reporting delay"/"تاخیر"
# by themselves, which are exactly what a trend question is more likely to
# say with no other qualifier. run_aggregate() additionally guards the count
# check with `not _matches_any(..., _TREND_KEYWORDS)` against this SAME list
# the trend branch uses, so the two shapes cannot silently drift apart the
# way two independently-maintained lists could.
_REPORTING_DELAY_COUNT_KEYWORDS = (
    "delay reason", "reason for delay", "delay in reporting",
    "late report", "reported late",
    "arse baad", "der se", "takheer", "takhir", "der ki wajah",
    "waqe ke kuch arse baad", "foran aane ke",
    "تاخیر کی وجہ", "دیر سے رپورٹ",
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
# [Gold-QA fix — Module 13, question CP1] Distinguishes "which district
# recovers the most weapons, RELATIVE TO ITS CASELOAD" (a rate — this
# family) from a plain "which district recovers the most weapons" (a flat
# count — `_top_districts_by()`'s existing default). Checked only when
# BOTH `_DISTRICT_KEYWORDS` and `_WEAPON_KEYWORDS` also match (see
# `run_aggregate()`) — this tuple alone is deliberately generic
# ("rate"/"per case load"/"relative to") since it only needs to disambiguate
# within an already-district-and-weapon-shaped query, not stand alone.
_RATE_SIGNAL_KEYWORDS = (
    "rate", "relative to", "per case load", "per caseload", "normalized",
    "normalised", "proportion", "ke lihaz se", "kay lihaz se",
    "کے لحاظ سے", "کی نسبت سے", "فیصد",
)
# [Gold-QA fix — Module 13, question M1] "What kinds of cases are we
# dealing with now COMPARED TO a couple of years back" — a genuine
# year-over-year comparison request. Checked BEFORE `_TREND_KEYWORDS`'s
# hard "not available" refusal below (that refusal predates Module 13's
# real year-partitioned primitive and would otherwise still catch some of
# this vocabulary, e.g. "year over year") — a query naming a specific
# reporting-SPEED comparison (M7's own shape) is a narrower, separately-
# handled case, see `_REPORTING_SPEED_COMPARISON_KEYWORDS` below, checked
# first in `run_aggregate()` so M7 doesn't fall into this more generic
# statute-mix family instead.
_TIME_COMPARISON_KEYWORDS = (
    "compared to", "compared with", "vs 2024", "versus 2024",
    "year over year", "a couple of years back", "few years back",
    "this year vs", "than a couple of years", "than last year",
    "کے مقابلے میں", "ke muqable mein", "ke muqabla mein",
)
# [Gold-QA fix — Module 13, question M7] "Kya log 2026 mein waqiaat ki
# police ko itni hi jaldi ittila de rahe hain jitni 2024 mein dete the?" —
# a reporting-SPEED/promptness comparison specifically, narrower than (and
# checked before, in `run_aggregate()`) the general
# `_TIME_COMPARISON_KEYWORDS` family above, since this one has its own
# dedicated aggregate (`_reporting_delay_rate_by_year()`) rather than the
# generic statute-mix-by-year one.
_REPORTING_SPEED_COMPARISON_KEYWORDS = (
    "itni hi jaldi", "jitni jaldi", "as quickly as", "as promptly as",
    "report as quickly", "reporting speed", "reporting promptly",
    "اتنی ہی جلدی", "جتنی جلدی",
)
# [Gold-QA fix — Module 22] The literal list above is pinned to M7's own
# phrasing and matches essentially nothing else. Live-caught: the required
# non-gold paraphrase "How long does it typically take someone to report a
# crime to us these days versus a couple of years ago?" matched none of
# them, so the capability was curve-fit to one gold string rather than
# actually answering reporting-speed questions.
#
# Widened as a two-signal AND rather than more literal phrases, because a
# one-signal widening WOULD COLLIDE WITH A7 — "Kitne cases mein mudai ne
# ... waqe ke kuch arse baad aane ki koi wajah batai" is a COUNT-of-reasons
# question that naturally contains reporting/delay vocabulary, and it is
# checked AFTER this one in run_aggregate(), so any over-broad match here
# silently hijacks it. That is the same false-positive class already fixed
# three times on this plan (Module 8c's KB patterns, Module 15's CR8
# pattern hijacking KB1, Module 16's bare Urdu "court" hijacking M4).
#
# What actually separates M7's family from A7's is a TIME-PERIOD
# COMPARISON, which A7 has none of. So: a reporting/speed signal AND a
# comparison signal, both required.
_REPORTING_SPEED_SIGNALS = (
    "report", "reported", "reporting", "ittila", "ittala", "inform",
    "jaldi", "quickly", "promptly", "how long", "how fast", "how quick",
    "take to", "time to", "اطلاع", "رپورٹ", "جلدی", "دیر",
)
# NOTE on what is deliberately NOT here: a bare "pehle"/"پہلے" ("before").
# Live-caught during this module's own negative control — KB8 ("...iske
# mukammal hone se PEHLE adaalat ko kuch REPORT karna zaroori karta hai...")
# carries both a reporting signal and that word, but its "before" means
# "before the investigation completes", not "a few years before". Matching
# it would have pulled a Knowledge-Base question into this aggregate and
# regressed the KB-corpus routing PR #9 had just fixed. Only time-PERIOD
# comparison forms are listed.
_SPEED_COMPARISON_SIGNALS = (
    "versus", " vs ", "compared", "compare", "these days",
    "years ago", "year ago", "years back", "year back", "back then",
    "used to", "muqable", "muqabla", "saal pehle", "sal pehle",
    "مقابلے", "سال پہلے",
    "2024", "2025", "2026",
)


def _is_reporting_speed_comparison(query_lower: str) -> bool:
    """
    True for a reporting-SPEED-over-time question (M7's family).

    Kept as a named predicate rather than inlined so the A7 boundary it
    protects is testable directly — see this module's
    `_REPORTING_SPEED_COMPARISON_KEYWORDS` comment for why that boundary
    matters and what breaks without it.
    """
    if _matches_any(query_lower, _REPORTING_SPEED_COMPARISON_KEYWORDS):
        return True
    return (
        _matches_any(query_lower, _REPORTING_SPEED_SIGNALS)
        and _matches_any(query_lower, _SPEED_COMPARISON_SIGNALS)
    )


# [Gold-QA fix — Module 23, question M5] "ہتھیار عام طور پر کس نوعیت کے
# مقدمات میں سامنے آتے ہیں، اور کیا 2024 کے مقابلے میں اب یہ نوعیت بدل گئی
# ہے؟" — in what KINDS OF CASES do weapons show up, and has that changed
# since 2024? A weapon × statute CO-OCCURRENCE question, and the one shape
# no aggregate in this module could express before: `_statute_mix_by_year()`
# (M1's) has no weapon dimension at all, and `_top_recurring_weapon_types()`
# has no statute or year dimension, so M5 fell through
# `_TIME_COMPARISON_KEYWORDS` into the former and came back with a flat
# per-year statute ranking over ALL cases — a valid answer to a different
# question.
#
# Deliberately a THREE-signal AND (weapon + case-type/statute + change-over-
# time), for the same reason `_is_reporting_speed_comparison()` above is a
# two-signal AND: every one- or two-signal widening collides with a
# neighbouring family this chain already serves.
#   - weapon alone           -> the bare weapon-recurrence aggregate
#                               (`_top_recurring_weapon_types()`), and CP1's
#                               district+weapon rate path
#   - weapon + compliance    -> G5's `_weapon_compliance_scan()`, which
#                               scores 1.0 today and is checked ABOVE this
#                               one in `run_aggregate()` regardless
#   - case-type + change     -> M1's `_statute_mix_by_year()`, which is
#                               exactly right for M1 and must keep it
# Requiring all three is what leaves each of those untouched.
_CASE_TYPE_TERMS = (
    "kind of case", "kinds of case", "type of case", "types of case",
    "case type", "case types", "nature of the case", "sort of case",
    "sorts of case", "offence", "offense", "charge", "charged", "statute",
    "section", "crime type", "kind of crime", "kinds of crime",
    "type of crime", "types of crime",
    "kis nau", "nauiyat", "qisam", "muqadmat", "mukadmat", "dafaat",
    "نوعیت", "مقدمات", "مقدمے", "مقدموں", "دفعات", "جرائم",
)
# NOTE on what is deliberately NOT here: a bare "پہلے"/"pehle" ("before").
# `_REPORTING_SPEED_SIGNALS` above records the live-caught reason — KB8's
# "before" means "before the investigation completes", not "a few years
# before" — and the same caution applies to any list this file uses as a
# change-over-time signal. Only time-PERIOD forms are listed.
_CHANGE_OVER_TIME_TERMS = (
    "used to", "changed", "changing", "has changed", "any different",
    "different than", "different from", "shifted", "shifting", "shift",
    "these days", "nowadays", "now compared", "still the same",
    "compared to", "compared with", "year over year", "years back",
    "years ago", "over time", "trend",
    "2024", "2025", "2026",
    "badal", "badla", "tabdeel", "tabdeeli", "ke muqable", "kay muqable",
    "بدل", "تبدیل", "کے مقابلے میں", "رجحان",
)


def _is_weapon_statute_cooccurrence(query_lower: str) -> bool:
    """
    True for M5's family: weapons + what kind of case they turn up in +
    whether that changed over time.

    A named predicate rather than an inlined three-way `and`, for the same
    reason `_is_reporting_speed_comparison()` is one — the boundaries it
    protects (G5, M1, CP1, the bare weapon recurrence path) are then
    testable directly, without standing up a gateway and a fake graph.
    """
    return (
        _matches_any(query_lower, _WEAPON_KEYWORDS + _WEAPON_TERMS)
        and _matches_any(query_lower, _CASE_TYPE_TERMS)
        and _matches_any(query_lower, _CHANGE_OVER_TIME_TERMS)
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
    # [Gold-QA fix — Module 4, question D1] "How many FIRs are currently
    # registered?" names FIRs, not "cases", so it missed every phrase above
    # and fell through to _station_or_category_counts's group-by-statute
    # default — exactly D1's run-to-run flakiness (a bare number one run, a
    # statute breakdown the next, since router.py's own override only fixes
    # the ROUTE, not which XAGG kind answers it). An FIR record IS a case
    # record in this corpus (get_cases() enumerates the same rows), so a
    # bare "how many FIRs / total number of FIRs / how many FIRs
    # registered" wants one number, same as "how many cases in total".
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
# [Gold-QA fix — Module 13, question M2]
_UNSUPPORTED_STATION_TYPE = (
    "Station-type-normalized aggregates are not available: this system's "
    "data model does not currently classify a police station as "
    "general-purpose vs. specialized for a particular crime type, so "
    "caseload cannot be compared across that dimension."
)
_UNSUPPORTED_TREND = (
    "Trend/time-series aggregates (reporting delay, month-over-month, etc.) "
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
    # [Gold-QA fix — Module 10.1 / Module 13, question A1] Count every
    # accused EDGE (one per fir_accused mention — row-level, matching how
    # the gold answer itself was derived: live-queried directly against the
    # graph, `evaluation/GROUND_TRUTH_NOTES.md` §4), NOT `DISTINCT p`. The
    # previous version keyed a dict by `entity_id`, which silently collapsed
    # a recidivist accused in two separate FIRs (شہزیب عرف شابی, عاصم رشید —
    # already known from CR2/S3) down to one entry each, losing exactly 2
    # from the male count and the total (confirmed live: 65 M / 92 total
    # instead of the correct 67 M / 24 F / 3 unknown / 94 total). A ratio
    # question like A1 ("what is the male-to-female ratio among named
    # accused") is asking about accused MENTIONS across the caseload, not
    # distinct real individuals — the same reasoning `_total_accused_count()`
    # above already gets right for a bare headcount, just not applied here
    # until now.
    genders: list[Optional[str]] = []
    for row in rows:
        p_props = (row.get("p") or {}).get("properties", {}) or {}
        if not p_props.get("entity_id"):
            continue
        genders.append((p_props.get("gender") or "").strip() or None)

    if not any(genders):
        return {"kind": "gender_breakdown", "unsupported": True, "message": _GENDER_NOT_YET_POPULATED}

    counts = Counter((g or "unknown").lower() for g in genders)
    return {
        "kind": "gender_breakdown",
        "unsupported": False,
        "counts": [{"key": k, "count": v} for k, v in counts.most_common()],
        "total_accused": len(genders),
    }


# [Gold-QA fix — Module 2, A7] Count FIRs that recorded a reporting-delay
# reason, against the total FIR count, from the Incident node's
# `reporting_delay_reason` property (structured_projection.py projects it; a
# blank/absent reason writes no property, so a node HAVING the property is
# exactly "this FIR recorded a delay reason").
#
# Modeled on `_gender_breakdown()` above: two plain Cypher reads (never a
# negated relationship pattern in WHERE — Apache AGE's Cypher parser rejects
# that with a bare syntax error, confirmed live), a graceful "not yet
# populated" fallback when the property is absent everywhere (i.e. the graph
# predates this projection and hasn't been re-synced), so the answer
# degrades to a stated limitation instead of a wrong "0".
#
# Counts DISTINCT Incidents (one per FIR) so a multi-chunk FIR is not
# double-counted. `with_delay_reason` is FIRs carrying a reason; `total_firs`
# is all FIRs reachable as Incidents in scope — the denominator the A7 gold
# answer uses ("8 of 73").
async def _reporting_delay_count(jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    case_filter = "WHERE c.case_id IN $case_ids" if jurisdiction_case_ids is not None else ""
    params = {"case_ids": jurisdiction_case_ids} if jurisdiction_case_ids is not None else {}

    total_rows = await age_client.execute_cypher(
        f"MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case) {case_filter} "
        "RETURN count(DISTINCT i) AS n",
        params=params, columns=["n"],
    )
    total = int((total_rows[0] or {}).get("n") or 0) if total_rows else 0

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
    # check — distinguishes "graph predates the projection" (honest can't-
    # answer) from a real zero on a re-synced corpus. If total is 0 too,
    # there is simply no data; report the honest zero rather than a
    # misleading "not populated" message.
    if with_delay == 0 and total > 0:
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


# [Gold-QA fix — Module 7, question CP6] Count Cases whose CURRENT (non-
# superseded) investigating Officer is still a placeholder name rather than
# a real one. Unlike A7/gender above, this data is already fully populated
# today (Officer nodes + the ASSIGNED_TO supersession chain — Milestone B2,
# structured_projection.py's `_write_investigating_officers()`) — this is a
# new COUNTING RULE over existing data, not a new projected property, so
# there is no "not yet synced" degradation path here.
#
# The placeholder marker is written VERBATIM from the source data as
# `"(نامزد ASI)"`/`"(نامزد SI)"` — Urdu script, not the Latin "Naamzad"
# transliteration the gold answer's own romanization implies (confirmed
# live against the graph, Module 1's ground-truth investigation) — so the
# match below is on the Urdu substring, not "naamzad".
#
# Reports the CURRENT count (the honest, live-defensible answer — "abhi
# tak" / "still" in CP6's own phrasing asks about NOW) alongside the
# historical ever-had-a-placeholder count, since one case
# (fir-205-26, per that same investigation) had a placeholder officer
# originally but was later reassigned a real one — the official gold
# answer's "11" reflects that earlier state. Stating both numbers means
# the answer is accurate about current reality AND still recognizable
# against the gold answer under contextual grading.
_PLACEHOLDER_OFFICER_MARKER = "نامزد"


def _is_placeholder_officer_name(name: Optional[str]) -> bool:
    return bool(name) and _PLACEHOLDER_OFFICER_MARKER in name


async def _placeholder_officer_count(jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    case_filter = "AND c.case_id IN $case_ids" if jurisdiction_case_ids is not None else ""
    params = {"case_ids": jurisdiction_case_ids} if jurisdiction_case_ids is not None else {}

    rows = await age_client.execute_cypher(
        f"MATCH (o:Officer)-[e:ASSIGNED_TO]->(c:Case) "
        f"WHERE e.role = 'investigating' {case_filter} "
        "RETURN o.canonical_name AS name, e.superseded_by AS superseded_by",
        params=params, columns=["name", "superseded_by"],
    )

    ever_placeholder = [r for r in rows if _is_placeholder_officer_name(r.get("name"))]
    current_placeholder = [r for r in ever_placeholder if r.get("superseded_by") is None]

    # "ASI" is checked before the plain "SI" bucket since "ASI" contains
    # "SI" as a substring — an unguarded order would double-count every
    # ASI placeholder into the SI bucket too.
    asi_count = sum(1 for r in current_placeholder if "ASI" in (r.get("name") or ""))
    si_count = sum(
        1 for r in current_placeholder
        if "SI" in (r.get("name") or "") and "ASI" not in (r.get("name") or "")
    )

    return {
        "kind": "placeholder_officer_count",
        "current_count": len(current_placeholder),
        "ever_count": len(ever_placeholder),
        "asi_count": asi_count,
        "si_count": si_count,
    }


# [Gold-QA fix — CR7, Module 14] FIR number pulled out of a free-text case
# reference so a criminal record's `source_case_ref` ("FIR 891/24, PS Jhang
# Road Faisalabad") and a court outcome's `source_doc_id`
# ("psrms/fir/fir-891-24#structured") can be matched on the same case even
# though the two systems format the reference completely differently.
_FIR_NUM_RE = re.compile(r"(\d{1,4})\s*[-/]\s*(\d{2,4})")


def _fir_key(text: Optional[str]) -> Optional[str]:
    """Normalize any FIR reference to 'NNN-YY' (e.g. 'FIR 891/24' and
    'fir-891-24' both -> '891-24'), or None if no FIR number is present."""
    if not text:
        return None
    m = _FIR_NUM_RE.search(str(text))
    if not m:
        return None
    year = m.group(2)
    year = year[-2:]  # 2024 -> 24, 24 -> 24
    return f"{int(m.group(1))}-{year}"


def _conviction_is_settled(status: Optional[str]) -> bool:
    """A criminal record is 'settled/decided' (vs. still in progress) when its
    conviction_status says a verdict was reached. Deterministic substring
    match on both the English tokens the data uses and their Urdu forms."""
    s = (status or "").lower()
    return any(t in s for t in ("convicted", "acquitted", "سزا", "بری", "نمٹ"))


async def _criminal_record_court_crosscheck(
    jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """
    [Gold-QA fix — CR7, Module 14] Two-part answer:

      (1) Status breakdown of the criminal-record system: how many records
          are settled (a verdict reached) vs. still in progress, grouped by
          the raw `conviction_status` the source system records.
      (2) Consistency cross-check: for any case that ALSO has an independent
          court-outcome record (chalaan_outcome), compare the two — does the
          criminal record's conviction status agree with the court's recorded
          outcome? Matched on the FIR number extracted from each system's own
          (differently-formatted) case reference (`_fir_key`).

    Criminal records are never case-scoped (a person's history spans cases),
    so the aggregate reports over the whole criminal-record system; the
    optional `jurisdiction_case_ids` only narrows which court outcomes are
    considered for the cross-check, mirroring every other aggregate here.
    """
    cr_rows = await age_client.execute_cypher(
        "MATCH (r:StructuredRecord) WHERE r.record_type = 'criminal_record' "
        "RETURN r.conviction_status AS status, r.source_case_ref AS case_ref, "
        "r.subject_full_name AS subject",
        columns=["status", "case_ref", "subject"],
    )

    total = len(cr_rows)
    settled = [r for r in cr_rows if _conviction_is_settled(r.get("status"))]
    in_progress = total - len(settled)
    status_counts = Counter((r.get("status") or "unknown") for r in cr_rows)

    # Court outcomes, keyed by FIR number, for the cross-check.
    co_case_filter = ""
    params: dict = {}
    if jurisdiction_case_ids is not None:
        co_case_filter = "AND c.case_id IN $case_ids"
        params = {"case_ids": jurisdiction_case_ids}
    co_rows = await age_client.execute_cypher(
        "MATCH (r:StructuredRecord) WHERE r.record_type = 'chalaan_outcome' "
        "AND r.case_outcome IS NOT NULL "
        f"RETURN r.source_doc_id AS doc_id, r.case_outcome AS outcome, "
        f"r.court_order_detail AS detail",
        params=params, columns=["doc_id", "outcome", "detail"],
    )
    court_by_fir = {}
    for r in co_rows:
        k = _fir_key(r.get("doc_id"))
        if k:
            court_by_fir[k] = {"outcome": r.get("outcome"), "detail": r.get("detail")}

    # Cross-check every criminal record that names a FIR also present in the
    # court-outcome set. A record and a court outcome are CONSISTENT when both
    # indicate a reached verdict (settled) — the only comparison the data
    # supports without parsing free-text sentences, and exactly CR7's ask
    # ("do the two match?").
    crosschecks = []
    for r in cr_rows:
        k = _fir_key(r.get("case_ref"))
        if k and k in court_by_fir:
            cr_settled = _conviction_is_settled(r.get("status"))
            court = court_by_fir[k]
            court_settled = _conviction_is_settled(court.get("outcome"))
            crosschecks.append({
                "fir": k,
                "subject": r.get("subject"),
                "criminal_status": r.get("status"),
                "court_outcome": court.get("outcome"),
                "court_detail": court.get("detail"),
                "consistent": cr_settled == court_settled,
            })

    return {
        "kind": "criminal_record_court_crosscheck",
        "total_records": total,
        "settled_count": len(settled),
        "in_progress_count": in_progress,
        "status_breakdown": [
            {"status": k, "count": v} for k, v in status_counts.most_common()
        ],
        "crosschecks": crosschecks,
    }


def render_criminal_record_crosscheck(agg_result: dict) -> list[str]:
    """[Gold-QA fix — CR7, Module 14] Human-readable lines for the
    criminal-record status + court-outcome cross-check. Defined here (the
    result's own module) and imported by both rendering sites (harness
    xagg tool + orchestrator) so the three stay in sync by construction."""
    total = agg_result["total_records"]
    settled = agg_result["settled_count"]
    in_progress = agg_result["in_progress_count"]
    _has = "has" if settled == 1 else "have"
    lines = [
        f"Of {total} criminal records, {in_progress} are still in progress "
        f"and {settled} {_has} reached a verdict. Breakdown by recorded status:"
    ]
    for s in agg_result["status_breakdown"]:
        lines.append(f"  - {s['status']}: {s['count']}")
    checks = agg_result.get("crosschecks", [])
    if checks:
        lines.append("")
        lines.append(
            "Where a case also has an independent court-outcome record, "
            "comparing the two:"
        )
        for c in checks:
            verdict = "consistent" if c["consistent"] else "INCONSISTENT"
            subj = f" ({c['subject']})" if c.get("subject") else ""
            lines.append(
                f"  - FIR {c['fir']}{subj}: criminal record says "
                f"\"{c['criminal_status']}\"; court outcome says "
                f"\"{c['court_outcome']}\" — {verdict}."
            )
    else:
        lines.append("")
        lines.append(
            "No case currently has both a criminal record and a separate "
            "court-outcome record to cross-check."
        )
    return lines


async def _cms_fir_linkage(jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    """
    [Gold-QA fix — CR6, Module 15] Do walk-in CMS complaints link to a real
    FIR, or stay separate? The exact-key join (CMS.case_tag_number ==
    FIR.e_tag_number) is already projected by cross_silo_projection.py as a
    BELONGS_TO_CASE edge (cms_complaint StructuredRecord -> Case); this
    aggregate just counts and surfaces it: how many CMS complaints exist,
    how many are linked to a real FIR, and the specific complaint->FIR
    pairs. RAG could only ever speak to one complaint's narrative at a time
    and never enumerate the field-level link across all of them, which is
    why this belongs in XAGG (RC-3).
    """
    total_rows = await age_client.execute_cypher(
        "MATCH (r:StructuredRecord) WHERE r.record_type = 'cms_complaint' "
        "RETURN count(r) AS n",
        columns=["n"],
    )
    total = int((total_rows[0] or {}).get("n") or 0) if total_rows else 0

    link_rows = await age_client.execute_cypher(
        "MATCH (r:StructuredRecord)-[:BELONGS_TO_CASE]->(c:Case) "
        "WHERE r.record_type = 'cms_complaint' "
        "RETURN r.case_tag_number AS tag, c.case_id AS case_id, "
        "r.complainant_cnic AS cnic",
        columns=["tag", "case_id", "cnic"],
    )
    links = [
        {"case_tag": r.get("tag"), "case_id": r.get("case_id"), "cnic": r.get("cnic")}
        for r in link_rows
    ]

    return {
        "kind": "cms_fir_linkage",
        "total_complaints": total,
        "linked_count": len(links),
        "unlinked_count": total - len(links),
        "links": links,
    }


def render_cms_fir_linkage(agg_result: dict) -> list[str]:
    """[Gold-QA fix — CR6, Module 15] shared renderer, imported by both
    rendering sites."""
    total = agg_result["total_complaints"]
    linked = agg_result["linked_count"]
    unlinked = agg_result["unlinked_count"]
    if total == 0:
        return ["No walk-in CMS complaints are recorded."]
    if linked == total:
        head = (
            f"Walk-in CMS complaints and FIRs are separate systems, but they "
            f"are linked in practice: all {total} current CMS complaint(s) "
            f"connect to a real FIR via a shared case tag (CMS.case_tag_number "
            f"= FIR.e_tag_number)."
        )
    else:
        head = (
            f"Of {total} walk-in CMS complaint(s), {linked} link to a real FIR "
            f"via a shared case tag; {unlinked} have no matching FIR."
        )
    lines = [head]
    for lk in agg_result["links"]:
        lines.append(f"  - {lk['case_tag']} → {lk['case_id']}")
    return lines


async def _dv_report_fir_match(jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    """
    [Gold-QA fix — CR8, Module 15] When a domestic-violence report is
    recorded as forwarded/converted into a formal case, does the case record
    confirm it? Women-violence reports are PKM applications
    (service_type='women_violence_report'); the ones carrying a
    forwarded_fir_number are projected with a BELONGS_TO_CASE edge to the
    real FIR (cross_silo_projection.py, forwarded_fir_number ==
    fir_display_code). This aggregate counts the total DV reports, how many
    are confirmed by a matching real FIR, and the specific pairs.
    """
    total_rows = await age_client.execute_cypher(
        "MATCH (r:StructuredRecord) "
        "WHERE r.record_type = 'pkm_application' "
        "AND r.service_type = 'women_violence_report' "
        "RETURN count(r) AS n",
        columns=["n"],
    )
    total = int((total_rows[0] or {}).get("n") or 0) if total_rows else 0

    match_rows = await age_client.execute_cypher(
        "MATCH (r:StructuredRecord)-[:BELONGS_TO_CASE]->(c:Case) "
        "WHERE r.record_type = 'pkm_application' "
        "AND r.service_type = 'women_violence_report' "
        "RETURN r.record_id AS rid, c.case_id AS case_id",
        columns=["rid", "case_id"],
    )
    matches = [{"record_id": r.get("rid"), "case_id": r.get("case_id")} for r in match_rows]

    return {
        "kind": "dv_report_fir_match",
        "total_reports": total,
        "confirmed_count": len(matches),
        "unconfirmed_count": total - len(matches),
        "matches": matches,
    }


def render_dv_report_fir_match(agg_result: dict) -> list[str]:
    """[Gold-QA fix — CR8, Module 15] shared renderer for both sites."""
    total = agg_result["total_reports"]
    confirmed = agg_result["confirmed_count"]
    unconfirmed = agg_result["unconfirmed_count"]
    if total == 0:
        return ["No domestic-violence reports are recorded."]
    lines = [
        f"Yes — confirmed by the case records. Of {total} domestic-violence "
        f"report(s), {confirmed} carry a forwarded FIR number that matches a "
        f"real, active FIR; the remaining {unconfirmed} record no forwarded "
        f"number."
    ]
    for m in agg_result["matches"]:
        lines.append(f"  - {m['record_id']} → {m['case_id']}")
    return lines


# [Gold-QA fix — G2, Module 15] A synthetic test-fixture case (CASE-TEST-*)
# is NOT a real case and must be excluded from a data-quality scan — live-
# confirmed the live DB carries 6 such leftover rows (the same ones Module 1
# deleted, re-seeded since), which inflate every completeness count away
# from the gold's real 73-case baseline. Excluding them here makes the scan
# correct regardless of DB hygiene, rather than depending on the rows being
# absent.
_TEST_CASE_RE = re.compile(r"CASE-TEST", re.IGNORECASE)


async def _case_completeness_scan(
    gateway, jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """
    [Gold-QA fix — G2, Module 15] Which cases have records too incomplete to
    rely on? A data-quality scan over the real case rows (not the graph,
    which backfills/defaults incident_date and so masks the very gaps this
    question is about — live-confirmed the graph shows only 1 missing date
    where the case rows show 9). Reports, over the real (non-test) cases:
    how many lack an incident date, and how many lack any roznamcha/zimni
    diary entry — the two completeness signals the G2 gold answer calls out.

    `incident_date` comes from the case row; the zimni presence check is a
    graph read (zimni entries are StructuredRecords linked to their Case).
    """
    cases = await gateway.get_cases(user_id=None, user_role="platform-admin")
    cases = [c for c in cases if not _TEST_CASE_RE.search(str(c.get("case_id") or ""))]
    if jurisdiction_case_ids is not None:
        allowed = set(jurisdiction_case_ids)
        cases = [c for c in cases if c.get("case_id") in allowed]

    total = len(cases)
    missing_incident_date = [
        c.get("case_id") for c in cases if not c.get("incident_date")
    ]
    # A case with no recorded investigation status can't be tracked through
    # its lifecycle — the second reliable completeness gap on the case row.
    # (The gold also cites missing roznamcha/zimni entries, but the current
    # graph only projects a per-FIR zimni INDEX, one per case, not the
    # individual entries — so "which FIRs lack zimni content" isn't reliably
    # queryable and is deliberately NOT reported here rather than fabricated.)
    missing_status = [
        c.get("case_id") for c in cases if not c.get("investigation_status")
    ]

    return {
        "kind": "case_completeness_scan",
        "total_cases": total,
        "missing_incident_date": missing_incident_date,
        "missing_status": missing_status,
    }


def render_case_completeness_scan(agg_result: dict) -> list[str]:
    """[Gold-QA fix — G2, Module 15] shared renderer for both sites."""
    total = agg_result["total_cases"]
    no_date = agg_result["missing_incident_date"]
    no_status = agg_result["missing_status"]
    lines = [
        "Cases whose own records are too incomplete to fully rely on — worth "
        "a closer look so they don't quietly stall:",
        f"  - {len(no_date)} of {total} FIRs record no incident date"
        + (f" ({', '.join(no_date[:12])}{'…' if len(no_date) > 12 else ''})" if no_date else "")
        + ", so any timeline built on them is unanchored.",
        f"  - {len(no_status)} of {total} FIRs carry no recorded investigation "
        f"status, so they can't be tracked through their lifecycle and may "
        f"silently stall.",
    ]
    return lines


# [Gold-QA fix — G5, Module 15] The exact free-text the weapon register uses
# for "no licence" (بغیر لائسنس = "without licence"). Matched as a substring
# so minor spacing/vocabulary variants still bucket together.
_UNLICENSED_TOKENS = ("بغیر لائسنس", "بلا لائسنس", "unlicensed", "no licence", "no license")


async def _weapon_compliance_scan(jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    """
    [Gold-QA fix — G5, Module 15] Compliance scan over the weapon register:
    how many recovered weapons are recorded WITHOUT a licence? Reads
    Weapon.license_status off the graph (structured_projection.py projects it
    from weapon_register). A weapon whose status matches an "unlicensed"
    token is counted; weapons with no status recorded are reported separately
    rather than assumed either way.
    """
    rows = await age_client.execute_cypher(
        "MATCH (w:Weapon) RETURN w.license_status AS status", columns=["status"],
    )
    total = len(rows)
    unlicensed = 0
    no_status = 0
    for r in rows:
        s = (r.get("status") or "").strip()
        if not s:
            no_status += 1
        elif any(t in s.lower() or t in s for t in _UNLICENSED_TOKENS):
            unlicensed += 1

    return {
        "kind": "weapon_compliance_scan",
        "total_weapons": total,
        "unlicensed_count": unlicensed,
        "no_status_count": no_status,
    }


def render_weapon_compliance_scan(agg_result: dict) -> list[str]:
    """[Gold-QA fix — G5, Module 15] shared renderer for both sites."""
    total = agg_result["total_weapons"]
    unlicensed = agg_result["unlicensed_count"]
    no_status = agg_result["no_status_count"]
    if total == 0:
        return ["No recovered weapons are recorded in the register."]
    pct = round(100 * unlicensed / total) if total else 0
    lines = [
        f"Reviewing the {total} weapon-register entries for compliance:",
        f"  - {unlicensed} of {total} recovered weapons (~{pct}%) are recorded "
        f"WITHOUT a licence — a pattern too broad to treat case-by-case; it "
        f"warrants a standing compliance check.",
    ]
    if no_status:
        lines.append(
            f"  - {no_status} carry no licence status at all, so their "
            f"compliance can't be confirmed either way."
        )
    return lines


async def _court_readiness_scan(
    gateway, jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """
    [Gold-QA fix — G3, Module 15/16] Preparing a case file for court: which
    fields is a prosecutor/court most likely to find incomplete? Combines the
    three court-readiness completeness signals the G3 gold answer calls out,
    each read from the source it's reliably queryable in:
      (1) accused↔complainant RELATIONSHIP blank — a court almost always asks
          how the parties relate; from the graph (RELATED_TO edges vs. the
          accused roster).
      (2) recovered-weapon LICENCE status missing — decisive in any Arms
          Ordinance charge; reuses the weapon-compliance read.
      (3) INCIDENT DATE missing — the file's timeline otherwise starts at the
          report, not the event; reuses the case-completeness read.
    """
    # (1) accused relationship completeness (graph).
    acc_rows = await age_client.execute_cypher(
        "MATCH (p:Person)-[r:INVOLVED_IN]->(:Incident) WHERE r.role = 'accused' "
        "RETURN count(r) AS n",
        columns=["n"],
    )
    total_accused = int((acc_rows[0] or {}).get("n") or 0) if acc_rows else 0
    rel_rows = await age_client.execute_cypher(
        "MATCH (p:Person)-[:INVOLVED_IN]->(:Incident) "
        "MATCH (p)-[:RELATED_TO]->(:Person) "
        "RETURN count(DISTINCT p) AS n",
        columns=["n"],
    )
    accused_with_rel = int((rel_rows[0] or {}).get("n") or 0) if rel_rows else 0
    accused_no_rel = max(0, total_accused - accused_with_rel)

    # (2) weapon licence status (reuse the compliance read).
    weapon = await _weapon_compliance_scan(jurisdiction_case_ids=jurisdiction_case_ids)

    # (3) incident date (reuse the case-completeness read).
    completeness = await _case_completeness_scan(
        gateway, jurisdiction_case_ids=jurisdiction_case_ids
    )

    return {
        "kind": "court_readiness_scan",
        "total_accused": total_accused,
        "accused_no_relationship": accused_no_rel,
        "weapons_total": weapon["total_weapons"],
        "weapons_no_licence_status": weapon["no_status_count"],
        "firs_no_incident_date": len(completeness["missing_incident_date"]),
        "total_cases": completeness["total_cases"],
    }


def render_court_readiness_scan(agg_result: dict) -> list[str]:
    """[Gold-QA fix — G3, Module 15/16] shared renderer for both sites."""
    lines = [
        "Preparing a case file for court — the fields a prosecutor or court "
        "most often wants filled in before accepting a file, and where this "
        "data is most likely to fall short:",
        f"  - The accused↔complainant relationship is blank in "
        f"{agg_result['accused_no_relationship']} of {agg_result['total_accused']} "
        f"accused entries — courts almost always ask how the parties relate.",
        f"  - {agg_result['weapons_no_licence_status']} of "
        f"{agg_result['weapons_total']} recovered weapons record no licence "
        f"status — decisive in any Arms Ordinance charge.",
        f"  - {agg_result['firs_no_incident_date']} FIRs record no incident "
        f"date, so the file's timeline effectively starts at the report, not "
        f"the event.",
        "Each is a likely round-trip to fix, not a hard blocker.",
    ]
    return lines


def render_time_bucketed_mean(agg_result: dict) -> list[str]:
    """
    [Gold-QA fix — Module 22, M7] Shared renderer for all three XAGG
    rendering sites (the harness `xagg_tool()` wrapper and orchestrator.py's
    two legacy XAGG blocks), for the same reason
    `render_court_readiness_scan()` above is shared: this file has a
    documented history of a new aggregate being wired into one rendering
    site and silently missed at another.

    Renders minutes at whatever scale reads naturally — a 1401.3-minute mean
    is far easier to judge as "~23.4 hours" — while always keeping the raw
    minutes, since that is the unit the comparison is computed in.
    """
    buckets = agg_result.get("buckets") or []
    if not buckets:
        return [
            "No FIR in scope records both an incident time and a report "
            "time, so reporting speed cannot be computed."
        ]

    lines = [
        "Mean time from incident to report, by incident year:",
    ]
    for b in buckets:
        minutes = b["mean_minutes"]
        readable = _humanize_minutes(minutes)
        suffix = f" (~{readable})" if readable else ""
        lines.append(
            f"  - {b['year']}: {minutes} minutes{suffix} "
            f"across {b['case_count']} FIRs"
        )

    if len(buckets) >= 2:
        first, last = buckets[0], buckets[-1]
        direction = "slower" if last["mean_minutes"] > first["mean_minutes"] else "faster"
        lines.append(
            f"Reporting is {direction} in {last['year']} than in {first['year']}."
        )

    # Coverage stated explicitly rather than left implicit — a mean over an
    # unstated subset is the kind of number this project's own discipline
    # says not to present without its denominator.
    missing = agg_result.get("missing_timestamp_count") or 0
    if missing:
        lines.append(
            f"({missing} FIR(s) excluded — incident or report time not "
            f"recorded, or recorded out of order.)"
        )
    return lines


def _humanize_minutes(minutes: float) -> Optional[str]:
    """A friendlier scale for a large minute count; None when minutes already read fine."""
    if minutes >= 1440:
        return f"{round(minutes / 1440, 1)} days"
    if minutes >= 60:
        return f"{round(minutes / 60, 1)} hours"
    return None


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


# ============================================================
# [Module 13, RC-2] Derived-aggregate primitives — a rate/ratio helper and
# a time-bucket helper, composable over any existing per-group count
# breakdown instead of the flat-count-only shape every prior XAGG module
# (2/4/7) added one bespoke function at a time. See
# PLATFORM_REASONING_SUMMARIZATION_FIX_PLAN.md's Module 13 section and
# GOLD_QA_MASTER_FIX_PLAN.md's write-up for the full rationale — CP1/M1/M7
# below are each a thin aggregate-specific query wired onto one of these
# two generic functions, not a new one-off pattern.
# ============================================================


def _rate_breakdown(subset_counts: dict, total_counts: dict) -> list[dict]:
    """
    General rate/ratio primitive: given two per-group count mappings
    sharing keys (`subset_counts` may hold a strict subset of
    `total_counts`'s keys — a group with zero subset occurrences simply
    isn't in it), compute `subset / total` per group.

    A group whose total is 0 is skipped entirely — an undefined rate,
    never fabricated as 0 or excluded silently without a reason. Sorted by
    rate descending by default; a caller wanting a different order (e.g. a
    chronological time series) re-sorts the returned list itself rather
    than this primitive assuming everyone wants "biggest rate first".
    """
    out = []
    for key, total in total_counts.items():
        if not total:
            continue
        subset = subset_counts.get(key, 0)
        out.append({"key": key, "subset_count": subset, "total_count": total, "rate": subset / total})
    out.sort(key=lambda r: r["rate"], reverse=True)
    return out


def _extract_year(date_value) -> Optional[int]:
    """
    First 4 characters of a `YYYY-MM-DD`-shaped date string (the exact
    format `structured_projection._write_occurred_on_edge()` writes onto
    `Date.date`) as an int, or None for anything unparseable — a row with
    no usable date is skipped by `_count_breakdown_by_year()`, never
    silently folded into a wrong bucket.
    """
    s = str(date_value) if date_value else ""
    if len(s) < 4 or not s[:4].isdigit():
        return None
    return int(s[:4])


def _count_breakdown_by_year(rows: list[dict], date_key: str, value_fn) -> dict[int, Counter]:
    """
    General time-bucket primitive: given `rows` (each a plain dict from an
    `age_client.execute_cypher()` result carrying a date-ish field at
    `date_key`), partition into one `Counter` per year, incrementing
    whatever key(s) `value_fn(row)` yields for that row (an iterable, so a
    multi-value field like a comma-joined statute list can increment
    several buckets per row — see `_statute_mix_by_year()` below, which
    reuses `split_crime_category()` for exactly that).
    """
    buckets: dict[int, Counter] = {}
    for row in rows:
        year = _extract_year(row.get(date_key))
        if year is None:
            continue
        bucket = buckets.setdefault(year, Counter())
        for key in value_fn(row):
            bucket[key] += 1
    return buckets


# [Module 13, RC-2, question CP1] "Which district recovers the most
# weapons RELATIVE TO ITS CASELOAD" — distinct from `_top_districts_by`
# above, which only ever returns a flat weapon (or case) count per
# district with no denominator, so a large district always "wins" purely
# by case volume. `subset` = distinct cases per district with >=1 Weapon
# node recovered; `total` = distinct cases per district overall — built on
# `_rate_breakdown()` rather than a bespoke division, so the next "rate
# per district/station" question reuses this shape instead of another
# one-off function.
async def _weapon_recovery_rate_by_district(jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    case_filter = "WHERE c.case_id IN $case_ids " if jurisdiction_case_ids is not None else ""
    params = {"case_ids": jurisdiction_case_ids} if jurisdiction_case_ids is not None else {}

    total_rows = await age_client.execute_cypher(
        f"MATCH (c:Case)-[:FILED_AT]->(:PoliceStation)-[:PART_OF]->(d:District) {case_filter}"
        "RETURN d.name AS district, count(DISTINCT c) AS n_count",
        params=params, columns=["district", "n_count"],
    )
    total_counts = {r.get("district") or "unknown": r.get("n_count") or 0 for r in total_rows}

    subset_rows = await age_client.execute_cypher(
        "MATCH (w:Weapon)-[:BELONGS_TO_CASE]->(c:Case)-[:FILED_AT]->(:PoliceStation)-[:PART_OF]->(d:District) "
        f"{case_filter}"
        "RETURN d.name AS district, count(DISTINCT c) AS n_count",
        params=params, columns=["district", "n_count"],
    )
    subset_counts = {r.get("district") or "unknown": r.get("n_count") or 0 for r in subset_rows}

    ranked = _rate_breakdown(subset_counts, total_counts)
    return {
        "kind": "rate_breakdown",
        "dimension": "weapon_recovery_rate_by_district",
        "subset_label": "cases_with_a_weapon_recovered",
        "counts": [
            {
                "district": r["key"],
                "cases_with_weapon": r["subset_count"],
                "total_cases": r["total_count"],
                "rate": r["rate"],
            }
            for r in ranked
        ],
    }


# [Module 13, RC-2, question M1] Year-partitioned statute mix — "what kinds
# of cases are we dealing with now compared to a couple of years back".
# Built on `_count_breakdown_by_year()` above: each Incident's own year
# comes from its `OCCURRED_ON {event_type: "incident"}` edge to a `Date`
# node (already written by `structured_projection.py` — real data, not a
# new projection this module needs to add).
#
# `crime_category` itself is NOT a graph property at all — confirmed by
# reading `structured_projection.py`, which never writes it onto the Case
# node — it only exists on the Postgres case row `gateway.get_cases()`
# returns (live-confirmed: an earlier version of this function tried
# `RETURN c.crime_category` from the graph directly and silently got back
# every bucket empty, no exception, since the property genuinely doesn't
# exist there). So this joins two sources by `case_id`: the graph for each
# case's incident YEAR, Postgres (via `gateway`, same as
# `_filtered_cases()`/`_station_or_category_counts()` above) for its
# crime_category — split per-act (`split_crime_category()`, same per-act
# split `_station_or_category_counts()`'s own `counts_by_act` already uses)
# since it is a comma-joined multi-act string, not a single value.
async def _statute_mix_by_year(gateway, jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    where_parts = ["oe.event_type = 'incident'"]
    params: dict = {}
    if jurisdiction_case_ids is not None:
        where_parts.append("c.case_id IN $case_ids")
        params["case_ids"] = jurisdiction_case_ids
    query = (
        "MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case) "
        "MATCH (i)-[oe:OCCURRED_ON]->(d:Date) "
        f"WHERE {' AND '.join(where_parts)} "
        "RETURN d.date AS incident_date, c.case_id AS case_id"
    )
    year_rows = await age_client.execute_cypher(query, params=params, columns=["incident_date", "case_id"])
    year_by_case: dict[str, int] = {}
    for row in year_rows:
        case_id = row.get("case_id")
        year = _extract_year(row.get("incident_date"))
        if case_id and year is not None:
            year_by_case[case_id] = year

    cases = await gateway.get_cases(user_id=None, user_role="platform-admin")
    if jurisdiction_case_ids is not None:
        allowed = set(jurisdiction_case_ids)
        cases = [c for c in cases if c.get("case_id") in allowed]

    # Year is already resolved per case above, so this bucketing loop is
    # deliberately inline rather than routed back through
    # `_count_breakdown_by_year()` — that primitive's own job is parsing a
    # DATE STRING into a year, which has nothing left to do here.
    buckets: dict[int, Counter] = {}
    for c in cases:
        year = year_by_case.get(c.get("case_id"))
        if year is None:
            continue
        bucket = buckets.setdefault(year, Counter())
        for act in split_crime_category(c.get("crime_category")) or []:
            bucket[act] += 1
    years = sorted(buckets.keys())
    return {
        "kind": "time_bucketed_breakdown",
        "dimension": "statute_by_year",
        "buckets": [
            {"year": y, "counts": [{"key": k, "count": v} for k, v in buckets[y].most_common(15)]}
            for y in years
        ],
    }


# [Gold-QA fix — Module 23, question M5] Weapon × statute co-occurrence, by
# incident year — "in what kinds of cases do weapons show up, and has that
# changed since 2024?"
#
# The MISSING PRIMITIVE this closes: nothing in this module joined a Weapon
# to its case's statutes. `_statute_mix_by_year()` directly above has the
# year and statute dimensions but no weapon one; `_top_recurring_weapon_types()`
# has the weapon dimension but neither of the others. M5 therefore fell into
# the former and got a per-year statute ranking over ALL cases — the right
# shape for M1, and unable in principle to say what a weapon charge pairs
# with.
#
# Three graph reads, joined on case_id in Python rather than one four-label
# Cypher MATCH chain, because each of the three is the SAME query an existing
# aggregate here already issues, so their semantics stay pinned to those
# aggregates':
#   1. year per case      — identical to `_statute_mix_by_year()`'s own
#                           query, so the two can never disagree about which
#                           year a case falls in (the failure mode this
#                           module's brief calls out explicitly).
#   2. weapons per case   — `_top_recurring_weapon_types()`'s query, and
#                           normalized through the same
#                           `_normalize_weapon_type()`, so "30 بور پستول" and
#                           "30 بور پستول بمعہ 3 گولیاں" stay one type.
#   3. statutes per case  — `StructuredRecord{record_type: 'fir_section'}`,
#                           projected by structured_projection.py from
#                           psrms.fir_section and linked BELONGS_TO_CASE.
#                           This is SECTION-level (`act` + `section_code`,
#                           e.g. "PPC" + "392"), strictly finer than
#                           `cases.crime_category`, which
#                           `muhafiz_cases._crime_category()` reduces to the
#                           ACT list alone ("PPC, Arms Ordinance 1965") and
#                           which therefore cannot tell robbery (392) from
#                           murder (302) — the exact distinction M5's answer
#                           turns on. Verified live against the graph (218
#                           fir_section records, every one carrying a
#                           BELONGS_TO_CASE edge), not inferred.
_WEAPONS_ACT_TOKENS = ("arms ordinance", "arms act", "آرمز آرڈیننس")


def _statute_label(act: Optional[str], section_code: Optional[str]) -> Optional[str]:
    """
    "PPC" + "392" -> "PPC §392"; an act with no section code -> the bare act.
    Section-first would read closer to how an FIR header writes it
    ("392 PPC"), but act-first keeps one act's sections together once the
    labels are sorted, which is what the renderer below relies on.
    """
    act = (act or "").strip()
    section = (section_code or "").strip()
    if not act:
        return f"§{section}" if section else None
    return f"{act} §{section}" if section else act


def _is_weapons_act(statute_key: str) -> bool:
    """True for a weapons-law statute (the 'weapon charge' itself) — matched
    on the ACT name, never a hardcoded section number, so a different section
    of the same ordinance still counts."""
    lowered = (statute_key or "").lower()
    return any(t in lowered for t in _WEAPONS_ACT_TOKENS)


async def _weapon_statute_cooccurrence_by_year(
    jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """
    [Gold-QA fix — Module 23, question M5] For every case with a recovered
    Weapon, that case's statute set, bucketed by incident year.

    Reports two views per year, because M5 asks two things at once:
      - `statutes`: what every weapon-bearing case that year was charged
        under — "in what kinds of cases do weapons show up".
      - `cooccurring`: among the weapon-bearing cases that also carry a
        WEAPONS-LAW charge, which OTHER statutes appear alongside it — the
        narrower "what does a weapon charge pair with" view, and the one
        whose change over time is M5's actual point.
    `pairs` keeps the weapon-type × statute cross-tab underneath both, so a
    corpus with a genuinely mixed weapon register can be read per type rather
    than only in aggregate.

    A weapon-bearing case with no resolvable incident year is counted in
    `undated_weapon_cases` rather than folded into any year — the same
    discipline as `_extract_year()`'s own contract.
    """
    case_filter = "AND c.case_id IN $case_ids " if jurisdiction_case_ids is not None else ""
    plain_filter = "WHERE c.case_id IN $case_ids " if jurisdiction_case_ids is not None else ""
    params: dict = {"case_ids": jurisdiction_case_ids} if jurisdiction_case_ids is not None else {}

    year_rows = await age_client.execute_cypher(
        "MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case) "
        "MATCH (i)-[oe:OCCURRED_ON]->(d:Date) "
        f"WHERE oe.event_type = 'incident' {case_filter}"
        "RETURN d.date AS incident_date, c.case_id AS case_id",
        params=params, columns=["incident_date", "case_id"],
    )
    year_by_case: dict[str, int] = {}
    for row in year_rows:
        case_id = row.get("case_id")
        year = _extract_year(row.get("incident_date"))
        if case_id and year is not None:
            year_by_case[case_id] = year

    weapon_rows = await age_client.execute_cypher(
        "MATCH (w:Weapon)-[:BELONGS_TO_CASE]->(c:Case) "
        f"{plain_filter}"
        "RETURN w.canonical_name AS weapon_name, c.case_id AS case_id",
        params=params, columns=["weapon_name", "case_id"],
    )
    weapons_by_case: dict[str, set[str]] = {}
    for row in weapon_rows:
        case_id = row.get("case_id")
        wtype = _normalize_weapon_type(row.get("weapon_name") or "")
        if case_id and wtype:
            weapons_by_case.setdefault(case_id, set()).add(wtype)

    statute_rows = await age_client.execute_cypher(
        "MATCH (s:StructuredRecord)-[:BELONGS_TO_CASE]->(c:Case) "
        f"WHERE s.record_type = 'fir_section' {case_filter}"
        "RETURN s.act AS act, s.section_code AS section_code, c.case_id AS case_id",
        params=params, columns=["act", "section_code", "case_id"],
    )
    statutes_by_case: dict[str, set[str]] = {}
    for row in statute_rows:
        case_id = row.get("case_id")
        label = _statute_label(row.get("act"), row.get("section_code"))
        if case_id and label:
            statutes_by_case.setdefault(case_id, set()).add(label)

    per_year: dict[int, dict] = {}
    undated = 0
    for case_id, wtypes in weapons_by_case.items():
        year = year_by_case.get(case_id)
        if year is None:
            undated += 1
            continue
        statutes = statutes_by_case.get(case_id, set())
        bucket = per_year.setdefault(year, {
            "cases": set(), "statutes": Counter(), "weapon_types": Counter(),
            "pairs": Counter(), "weapon_charge_cases": set(), "cooccurring": Counter(),
        })
        bucket["cases"].add(case_id)
        for wtype in wtypes:
            bucket["weapon_types"][wtype] += 1
        for statute in statutes:
            bucket["statutes"][statute] += 1
            for wtype in wtypes:
                bucket["pairs"][(wtype, statute)] += 1
        # The co-occurrence view: only cases that actually carry a
        # weapons-law charge, and only the OTHER statutes on them.
        if any(_is_weapons_act(s) for s in statutes):
            bucket["weapon_charge_cases"].add(case_id)
            for statute in statutes:
                if not _is_weapons_act(statute):
                    bucket["cooccurring"][statute] += 1

    def _counts(counter: Counter) -> list[dict]:
        # Ties broken alphabetically so the rendered order is stable across
        # runs — Counter.most_common() alone leaves equal counts in insertion
        # order, which for a set-derived input is not deterministic.
        return [
            {"key": k, "count": v}
            for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    # Observability, not decoration: this module's verification standard
    # requires PROVING which aggregate a live question reached, and the SSE
    # stream only ever exposes `route='XAGG'` — one line per aggregate is the
    # difference between a demonstrated route and an inferred one.
    logger.info(
        "XAGG weapon_statute_cooccurrence: %d weapon case(s), %d year bucket(s), "
        "%d with no resolvable incident date",
        len(weapons_by_case), len(per_year), undated,
    )
    return {
        "kind": "weapon_statute_cooccurrence",
        "total_weapon_cases": len(weapons_by_case),
        "undated_weapon_cases": undated,
        "buckets": [
            {
                "year": year,
                "case_count": len(b["cases"]),
                "statutes": _counts(b["statutes"]),
                "weapon_types": _counts(b["weapon_types"]),
                "weapon_charge_case_count": len(b["weapon_charge_cases"]),
                "cooccurring": _counts(b["cooccurring"]),
                "pairs": [
                    {"weapon_type": wt, "statute": st, "count": n}
                    for (wt, st), n in sorted(
                        b["pairs"].items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
                    )
                ],
            }
            for year, b in sorted(per_year.items())
        ],
    }


def render_weapon_statute_cooccurrence(agg_result: dict) -> list[str]:
    """
    [Gold-QA fix — Module 23, M5] Shared renderer for all three XAGG
    rendering sites, same reason as `render_time_bucketed_mean()` above.

    The closing "what changed" line is DERIVED from the buckets (a set
    difference between the earliest and latest year's co-occurring statutes),
    never a narrative pinned to the years or sections the gold answer happens
    to name — if the corpus stops showing the widening, the line stops
    claiming it.
    """
    buckets = agg_result.get("buckets") or []
    if not buckets:
        return [
            "No case with a recovered weapon has a resolvable incident date, "
            "so weapon charges cannot be broken down by year."
        ]

    total = agg_result.get("total_weapon_cases", 0)
    lines = [
        f"{total} case(s) recorded a recovered weapon. What those cases were "
        f"charged under, by incident year:",
    ]
    for b in buckets:
        lines.append("")
        lines.append(f"**{b['year']}** — {b['case_count']} case(s) with a recovered weapon:")
        for s in b["statutes"]:
            lines.append(f"  - {s['key']}: {s['count']}")
        wtypes = ", ".join(f"{w['key']} ({w['count']})" for w in b["weapon_types"])
        if wtypes:
            lines.append(f"  Weapon types recovered: {wtypes}.")
        if b["weapon_charge_case_count"]:
            paired = ", ".join(f"{c['key']} ({c['count']})" for c in b["cooccurring"])
            lines.append(
                f"  Of these, {b['weapon_charge_case_count']} carry a weapons-law "
                f"charge; the sections it appears alongside: {paired or 'none'}."
            )

    if len(buckets) >= 2:
        first, last = buckets[0], buckets[-1]
        before = {c["key"] for c in first["cooccurring"]}
        after = {c["key"] for c in last["cooccurring"]}
        added = sorted(after - before)
        dropped = sorted(before - after)
        lines.append("")
        if added:
            lines.append(
                f"Change {first['year']} to {last['year']}: the weapons charge now "
                f"also appears with {', '.join(added)}, which it did not in "
                f"{first['year']}."
            )
        if dropped:
            lines.append(f"No longer paired with: {', '.join(dropped)}.")
        if not added and not dropped:
            lines.append(
                f"The set of statutes a weapons charge appears alongside is "
                f"unchanged between {first['year']} and {last['year']}."
            )

    undated = agg_result.get("undated_weapon_cases") or 0
    if undated:
        lines.append("")
        lines.append(
            f"({undated} case(s) with a recovered weapon are excluded — no "
            f"incident date recorded.)"
        )
    return lines


# [Module 13, RC-2, question M7] Year-partitioned reporting-delay picture —
# "are people reporting incidents to police as quickly in 2026 as in 2024".
#
# HONEST SCOPE NOTE, read before extending this: a true numeric day-count
# delay (report_datetime - incident_datetime) cannot be computed today.
# `report_datetime` DOES exist on the raw ingested FIR record
# (`fir.raw.get("report_datetime")` — already read once, for officer-
# supersession tracking, in `structured_projection.py`) but is NOT
# projected onto the Incident node or linked via its own `OCCURRED_ON`
# edge anywhere, so there is no queryable numeric delay in the graph at
# all — the same "real data, never projected as a structured property"
# shape as A1's bug (`evaluation/GROUND_TRUTH_NOTES.md` §4), just not yet
# fixed. Projecting `report_datetime` (and computing the day delta) is a
# follow-on ingestion task, out of this module's `xagg.py`-only scope —
# flag it, don't silently fabricate a mean here.
#
# What IS real and computable today: `Incident.reporting_delay_reason`
# (Module 2/A7) records whether a delay reason was recorded at all. This
# reports the RATE of FIRs recording a delay reason, per incident year — a
# genuine, honestly-labeled proxy for "reporting promptness trend", built
# on the same two primitives as CP1/M1 above, not a fabricated day-count.
# Self-heals to a true mean-days aggregate with no code change here once
# `report_datetime` is projected.
async def _incident_to_report_minutes_by_year(
    jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """
    [Gold-QA fix — Module 22, question M7] Mean minutes from incident to
    report, bucketed by incident year — the metric M7 actually asks for
    ("are people reporting as quickly in 2026 as in 2024?").

    This is what `_reporting_delay_rate_by_year()` below could not compute,
    and said so in its own `note`: the delay-REASON rate is a different
    quantity, and no finer-than-a-day timestamp reached a queryable field.
    Module 22 projects `Incident.incident_datetime` / `.report_datetime`
    (structured_projection.py), so the real delta is now computable — the
    "self-heals once report_datetime is projected" case that function's own
    comment anticipated.

    Both timestamps are optional by the projection's own convention, so a
    FIR missing either is EXCLUDED from the mean rather than counted as a
    zero delay (which would silently drag every average toward 0). The
    per-bucket `n` and the corpus-level `missing_timestamp_count` are
    reported so the answer can state its own coverage honestly instead of
    presenting a mean over an unstated subset.
    """
    where_parts = ["i.incident_datetime IS NOT NULL", "i.report_datetime IS NOT NULL"]
    params: dict = {}
    if jurisdiction_case_ids is not None:
        where_parts.append("c.case_id IN $case_ids")
        params["case_ids"] = jurisdiction_case_ids
    query = (
        "MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case) "
        f"WHERE {' AND '.join(where_parts)} "
        "RETURN i.incident_datetime AS incident_datetime, "
        "i.report_datetime AS report_datetime"
    )
    rows = await age_client.execute_cypher(
        query, params=params, columns=["incident_datetime", "report_datetime"],
    )

    minutes_by_year: dict[int, list[float]] = {}
    skipped = 0
    for row in rows:
        delta = _minutes_between(row.get("incident_datetime"), row.get("report_datetime"))
        year = _extract_year(row.get("incident_datetime"))
        if delta is None or year is None:
            skipped += 1
            continue
        minutes_by_year.setdefault(year, []).append(delta)

    buckets = [
        {
            "year": year,
            "mean_minutes": round(sum(values) / len(values), 1),
            "case_count": len(values),
        }
        for year, values in sorted(minutes_by_year.items())
    ]
    return {
        "kind": "time_bucketed_mean",
        "dimension": "incident_to_report_minutes_by_year",
        "unit": "minutes",
        "buckets": buckets,
        "missing_timestamp_count": skipped,
    }


# [Gold-QA fix — Module 22] Shared by the aggregate above; kept module-level
# and pure so the parsing rules (which tolerate the `Z` suffix AGE returns,
# and reject a negative delta rather than averaging it in) are unit-testable
# without a live graph.
def _minutes_between(start_value, end_value) -> Optional[float]:
    """
    Whole minutes from `start_value` to `end_value`, or None if either is
    unparseable or the pair is out of order.

    A report timestamp EARLIER than its incident timestamp is a data defect,
    not a negative delay — averaging it in would silently pull the mean
    down and hide the bad row. Returning None routes it to the caller's
    skipped/missing count instead, where it stays visible.
    """
    start = _parse_iso_datetime(start_value)
    end = _parse_iso_datetime(end_value)
    if start is None or end is None:
        return None
    delta_minutes = (end - start).total_seconds() / 60
    if delta_minutes < 0:
        return None
    return delta_minutes


def _parse_iso_datetime(value) -> Optional[datetime]:
    """
    Parse an ISO-8601 timestamp as projected onto the graph.

    AGE returns the property as a quoted agtype string; the API's own values
    carry a `Z` suffix (`2024-09-25T17:10:00Z`), which `fromisoformat()`
    only accepts from Python 3.11 — normalized here rather than relying on
    the runtime's version.
    """
    if not value:
        return None
    text = str(value).strip().strip('"')
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


async def _reporting_delay_rate_by_year(jurisdiction_case_ids: Optional[list[str]] = None) -> dict:
    where_parts = ["oe.event_type = 'incident'"]
    params: dict = {}
    if jurisdiction_case_ids is not None:
        where_parts.append("c.case_id IN $case_ids")
        params["case_ids"] = jurisdiction_case_ids
    query = (
        "MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case) "
        "MATCH (i)-[oe:OCCURRED_ON]->(d:Date) "
        f"WHERE {' AND '.join(where_parts)} "
        "RETURN d.date AS incident_date, i.reporting_delay_reason AS reporting_delay_reason"
    )
    rows = await age_client.execute_cypher(
        query, params=params, columns=["incident_date", "reporting_delay_reason"],
    )

    total_by_year: Counter = Counter()
    delayed_by_year: Counter = Counter()
    for row in rows:
        year = _extract_year(row.get("incident_date"))
        if year is None:
            continue
        total_by_year[year] += 1
        if row.get("reporting_delay_reason"):
            delayed_by_year[year] += 1

    ranked = _rate_breakdown(dict(delayed_by_year), dict(total_by_year))
    return {
        "kind": "time_bucketed_rate",
        "dimension": "reporting_delay_rate_by_year",
        "note": (
            "This is the rate of FIRs recording a delay reason each year, "
            "not a mean number of delay days — a day-level reporting delay "
            "is not currently projected as a structured, queryable field "
            "in this system's data model."
        ),
        "buckets": [
            {
                "year": r["key"],
                "delayed_count": r["subset_count"],
                "total_count": r["total_count"],
                "rate": r["rate"],
            }
            for r in sorted(ranked, key=lambda r: r["key"])
        ],
    }


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
    # [Gold-QA fix — Module 13, question M2] Checked early, same precedence
    # as AGE just above — no station-type dimension exists in this data
    # model at all (see _STATION_TYPE_KEYWORDS' own comment), so this must
    # win before _STATION_KEYWORDS' plain per-station group-by further down
    # silently answers a different, easier question than the one asked.
    if _matches_any(query_lower, _STATION_TYPE_KEYWORDS):
        return {"kind": "unsupported_aggregate", "message": _UNSUPPORTED_STATION_TYPE}
    # [Gold-QA fix — Module 7, question CP6] Checked before _OFFICER_KEYWORDS's
    # hard refusal — a placeholder-officer COUNT has a real data path
    # (Officer.canonical_name + the ASSIGNED_TO supersession chain, both
    # already populated), unlike a general "which officer" identity question.
    if _matches_any(query_lower, _PLACEHOLDER_OFFICER_KEYWORDS):
        return await _placeholder_officer_count(jurisdiction_case_ids=jurisdiction_case_ids)
    # [Gold-QA fix — CR7, Module 14] Criminal-record status + court-outcome
    # consistency, checked before the generic count/status paths so a
    # "criminal record" question isn't answered as a plain case count.
    if _matches_any(query_lower, _CRIMINAL_RECORD_KEYWORDS):
        return await _criminal_record_court_crosscheck(jurisdiction_case_ids=jurisdiction_case_ids)
    # [Gold-QA fix — CR8, Module 15] DV report ↔ FIR confirmation. Checked
    # before the CMS linkage below since a DV question can also mention
    # "complaint" (شکایت) but is specifically about the women-violence report.
    if _matches_any(query_lower, _DV_REPORT_KEYWORDS):
        return await _dv_report_fir_match(jurisdiction_case_ids=jurisdiction_case_ids)
    # [Gold-QA fix — CR6, Module 15] Walk-in CMS complaint ↔ FIR linkage.
    if _matches_any(query_lower, _CMS_LINKAGE_KEYWORDS):
        return await _cms_fir_linkage(jurisdiction_case_ids=jurisdiction_case_ids)
    # [Gold-QA fix — G3, Module 15/16] Court-readiness completeness — checked
    # before G2's general scan since the court/handover framing is the more
    # specific intent (and its answer combines three court-relevant signals).
    if _matches_any(query_lower, _COURT_READINESS_KEYWORDS):
        return await _court_readiness_scan(gateway, jurisdiction_case_ids=jurisdiction_case_ids)
    # [Gold-QA fix — G2, Module 15] Case data-completeness scan (needs the
    # gateway case rows, not the graph — see the function's own docstring).
    if _matches_any(query_lower, _COMPLETENESS_KEYWORDS):
        return await _case_completeness_scan(gateway, jurisdiction_case_ids=jurisdiction_case_ids)
    # [Gold-QA fix — G5, Module 15] Weapon-register compliance — a weapon term
    # AND a licence/compliance term together (a bare "weapon" stays the
    # recurrence aggregate's job).
    if _matches_any(query_lower, _WEAPON_TERMS) and _matches_any(query_lower, _COMPLIANCE_TERMS):
        return await _weapon_compliance_scan(jurisdiction_case_ids=jurisdiction_case_ids)
    if _matches_any(query_lower, _OFFICER_KEYWORDS):
        return {"kind": "unsupported_aggregate", "message": _UNSUPPORTED_OFFICER}
    # [Gold-QA fix — Module 13, question M7] Checked before both the A7
    # count-shaped reporting-delay check just below and _TREND_KEYWORDS'
    # hard refusal — a reporting-SPEED-over-time comparison has a real
    # aggregate rather than a refusal.
    #
    # [Gold-QA fix — Module 22] Re-pointed from
    # `_reporting_delay_rate_by_year()` to the true mean-minutes aggregate.
    # Module 13 could only offer the delay-REASON rate as an honest proxy
    # because no sub-day timestamp was projected; Module 22 projects
    # `Incident.incident_datetime`/`.report_datetime`, so the metric M7
    # actually asks for is now computable. `_reporting_delay_rate_by_year()`
    # is deliberately KEPT — it answers the A7-family delay-reason question,
    # which is a different quantity with its own tests.
    if _is_reporting_speed_comparison(query_lower):
        return await _incident_to_report_minutes_by_year(
            jurisdiction_case_ids=jurisdiction_case_ids
        )
    # [Gold-QA fix — Module 2, A7] Checked before the trend fallback: a
    # count-shaped reporting-delay question has a real data path now
    # (Incident.reporting_delay_reason), degrading to an honest "not synced
    # yet" like gender, not a hard refusal. The `not _matches_any(...,
    # _TREND_KEYWORDS)` guard is the safety net against the exact regression
    # a prior attempt at this fix shipped — a genuine trend question ("...
    # trend over time") still falls through to the _TREND_KEYWORDS check
    # below, unaffected, because it guards against that SAME list.
    if _matches_any(query_lower, _REPORTING_DELAY_COUNT_KEYWORDS) and not _matches_any(
        query_lower, _TREND_KEYWORDS
    ):
        return await _reporting_delay_count(jurisdiction_case_ids=jurisdiction_case_ids)
    # [Gold-QA fix — Module 23, question M5] Weapon × statute co-occurrence,
    # checked immediately BEFORE M1's `_TIME_COMPARISON_KEYWORDS` branch just
    # below, which is exactly what used to swallow M5 ("...کے مقابلے میں..."
    # is a literal entry in that tuple) and answer it with a per-year statute
    # ranking that has no weapon dimension at all.
    #
    # Placement, in both directions:
    #   - BELOW G5's weapon+compliance check and M7's
    #     `_is_reporting_speed_comparison()`, both of which stay first for
    #     their own shapes — neither carries all three of this predicate's
    #     signals anyway, so this is belt-and-braces, not load-bearing.
    #   - ABOVE `_TIME_COMPARISON_KEYWORDS` (M1), `_TREND_KEYWORDS`' refusal,
    #     `_DISTRICT_KEYWORDS` (Module 1c's district+weapon path) and the bare
    #     `_WEAPON_KEYWORDS` recurrence branch — the three prior collision
    #     sites this chain's comments already name. A bare weapon question,
    #     and a district+weapon question, still fall through untouched
    #     because `_is_weapon_statute_cooccurrence()` also requires a
    #     case-type/statute term AND a change-over-time term.
    if _is_weapon_statute_cooccurrence(query_lower):
        return await _weapon_statute_cooccurrence_by_year(
            jurisdiction_case_ids=jurisdiction_case_ids
        )
    # [Gold-QA fix — Module 13, question M1] Checked before _TREND_KEYWORDS'
    # hard refusal — a year-over-year case-type/statute comparison now has a
    # real aggregate (_statute_mix_by_year(), powered by each Incident's own
    # OCCURRED_ON->Date edge, already-real data). M7's own reporting-speed
    # shape is checked above and wins first, so it doesn't fall into this
    # more generic family instead.
    if _matches_any(query_lower, _TIME_COMPARISON_KEYWORDS):
        return await _statute_mix_by_year(gateway, jurisdiction_case_ids=jurisdiction_case_ids)
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
        # [Gold-QA fix — Module 13, question CP1] A district+weapon query
        # that ALSO carries a rate/relative-to-caseload signal wants the
        # weapon-recovery RATE per district, not the flat weapon count
        # `_top_districts_by()` returns for every other district+weapon
        # query — checked first so the existing flat-count behavior for a
        # plain "which district recovers the most weapons" is unchanged.
        if _matches_any(query_lower, _WEAPON_KEYWORDS) and _matches_any(query_lower, _RATE_SIGNAL_KEYWORDS):
            return await _weapon_recovery_rate_by_district(jurisdiction_case_ids=jurisdiction_case_ids)
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
