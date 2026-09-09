"""
XAGG tool — src/pipeline/harness/tools/xagg.py (Phase 0, foundation layer).

Thin adapter over `run_aggregate(query_text, target_entity, gateway,
user_id, user_role)` (AGENT_HARNESS_DESIGN.md §2.4) plus the same
deterministic-text-rendering `orchestrator.py`'s XAGG route already builds
from `run_aggregate()`'s result dict, packaged as one synthetic, citable
evidence chunk (mirrors `orchestrator.py`'s `agg_chunks` — the shape the
Verifier is fed today). No new aggregate logic: XAGG stays a two-canned-
family, keyword-dispatched primitive (`run_aggregate()`'s own job), never
a general text-to-SQL/Cypher system.

[PRESERVE — design §2.4] NEVER falls back to RAG (`XAggToolResult`
inherits `fallback_to_rag: Literal[False]` from `CrossCaseToolResult`).

[PRESERVE — design §2.4] `raw_summary_text` is the deterministic rendering
of the computed aggregate, independent of any LLM paraphrase — this is
what a sub-agent serves on a Verifier rejection instead of a generic
abstention, since the aggregate evidence is machine-computed and correct
by construction (a rejection there means the *paraphrase* failed, not that
the evidence is thin). This tool always populates it; generating and
verifying the paraphrase is the sub-agent's job, not this primitive's.

[PRESERVE — design §2.3/§4.3, same pattern as XGRAPH] All authorization
logic (role gate -> audit log -> arm cross-case/RLS scope) stays inside
`run_aggregate()` itself; this wrapper only translates its `PermissionError`
into `status=DENIED`.

DEVIATION FROM THE LITERAL DOC TEXT — `AggregateKind`. SUBAGENT_INTERFACES.md
§1.5 lists `AggregateKind = Literal["graph_recurrence", "relational_aggregate",
"case_listing"]` — three values. `run_aggregate()` (xagg.py) legitimately
returns a FOURTH kind, `"total_count"`, for a bare "how many cases in
total" query (`_total_count()` — added to fix a live-confirmed gap, see
xagg.py's own comment on `_TOTAL_KEYWORDS`). Rejecting that kind here
would crash the tool on a normal, already-fixed query — the exact kind of
regression "preserve existing behavior" (design §2, non-optional) forbids.
This wrapper's `AggregateKind` therefore includes `"total_count"`,
additively, alongside the doc's three. Flagged here for anyone reconciling
this file against the doc later.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import Field

from src.data_gateway import get_gateway
from src.pipeline.harness.types import (
    ChunkMetadata,
    CrossCaseToolInput,
    CrossCaseToolResult,
    EvidenceChunk,
    ToolError,
    ToolStatus,
)
from src.pipeline.xagg import (
    run_aggregate,
    render_criminal_record_crosscheck,
    render_criminal_record_local_match_gap,
    render_cms_fir_linkage,
    render_dv_report_fir_match,
    render_case_completeness_scan,
    render_weapon_compliance_scan,
    render_weapon_evidence_chain,
    render_court_readiness_scan,
    render_station_caseload_by_specialisation,
    render_time_bucketed_mean,
    render_weapon_statute_cooccurrence,
    render_statute_court_stage_join,
    render_officer_role_pair_overlap,
    render_offender_age_profile,
    render_accused_relationship_breakdown,
    render_seized_property_disposition,
    render_incident_time_of_day,
    render_arrest_rate,
    render_filtered_fir_listing,
    _UNSUPPORTED_JURISDICTION,
)
from src.retrieval.graph_retriever import jurisdiction_unresolved

logger = logging.getLogger(__name__)

# See module docstring's "DEVIATION FROM THE LITERAL DOC TEXT" note:
# "total_count" is additive to SUBAGENT_INTERFACES.md §1.5's three-value
# Literal, not a replacement of it.
#
# [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 1] Five more kinds added
# the same additive way: run_aggregate() (xagg.py) gained
# total_accused_count/gender_breakdown/district_breakdown/
# station_total_count (real new aggregate families) and
# unsupported_aggregate (an honest refusal for topics with no data path)
# — see xagg.py's own module comments for each. Live-confirmed bug this
# closes: this wrapper's _render_aggregate_text() and this Literal are a
# SEPARATE, hand-maintained copy of orchestrator.py's rendering (per this
# module's own docstring), not derived from it — so orchestrator.py
# already handling these five kinds did NOT mean this file did. Before
# this fix, any of these five (e.g. "how many accused in total") crashed
# XAggToolResult construction (Literal validation) or _render_aggregate_text
# (a bare KeyError on agg_result["counts"], confirmed live) the moment
# XAGG reached this wrapper — i.e. every time in a deployment with XAGG in
# HARNESS_CUTOVER_ROUTES.
AggregateKind = Literal[
    "graph_recurrence", "relational_aggregate", "case_listing", "total_count",
    "total_accused_count", "gender_breakdown", "district_breakdown",
    "station_total_count", "unsupported_aggregate",
    # [Gold-QA fix — Module 2, A7] added the same additive way as the five
    # kinds above (see this Literal's own comment block).
    "reporting_delay_count",
    # [Gold-QA fix — Module 7, CP6] same additive convention.
    "placeholder_officer_count",
    # [Gold-QA fix — Module 13, RC-2] Three more kinds, same additive
    # convention — the derived-aggregate primitives' own result shapes
    # (questions CP1/M1/M7). Live-confirmed the exact same crash class this
    # Literal's own docstring describes for the five kinds above: before
    # this fix, any of CP1/M1/M7's new aggregates raised a Pydantic
    # `literal_error` on `XAggToolResult` construction the instant XAGG
    # reached this wrapper (confirmed live via the actual traceback, not
    # just inferred from reading the code — CP1's and M7's live answers
    # came back empty with no user-visible error at all, since main.py's
    # top-level exception handler swallows this into a bare "Chat pipeline
    # error" log line).
    "rate_breakdown",
    "time_bucketed_breakdown",
    "time_bucketed_rate",
    # [Gold-QA fix — Module 22, M7] same additive convention — the true
    # mean-incident-to-report-minutes shape that replaces the delay-REASON
    # rate proxy for M7. Omitting this entry reproduces exactly the silent
    # `literal_error` crash the comment block above documents.
    "time_bucketed_mean",
    # [Gold-QA fix — Module 14, CR7] same additive convention.
    "criminal_record_court_crosscheck",
    # [Gold-QA fix — Module 15, CR6] same additive convention.
    "cms_fir_linkage",
    # [Gold-QA fix — Module 15, CR8] same additive convention.
    "dv_report_fir_match",
    # [Gold-QA fix — Module 15, G2] same additive convention.
    "case_completeness_scan",
    # [Gold-QA fix — Module 15, G5] same additive convention.
    "weapon_compliance_scan",
    # [Gold-QA fix — Module 28, CR4] same additive convention.
    "weapon_evidence_chain",
    # [Gold-QA fix — Module 15/16, G3] same additive convention.
    "court_readiness_scan",
    # [Gold-QA fix — Module 23, M5] same additive convention — the
    # weapon × statute co-occurrence shape. Omitting this entry reproduces
    # exactly the silent `literal_error` crash the comment block above
    # documents.
    "weapon_statute_cooccurrence",
    # [Gold-QA fix - Module 24, M4] same additive convention - the
    # statute x court-stage join shape.
    "statute_court_stage_join",
    # [Gold-QA fix — Modules 31-34, G1] same additive convention — the four
    # caseload-profile shapes. Caught the hard way, and worth recording as
    # the fourth repeat of this exact defect: Module 31's aggregate was
    # unit-green and correct against the live graph, and still returned an
    # EMPTY answer through `/api/chat` with `status=None` and no
    # user-visible error, because `XAggToolResult` construction raised
    # `pydantic_core.ValidationError: literal_error` on
    # `aggregate_kind='offender_age_profile'`. That is precisely the silent
    # crash this Literal's own comment block above warns about, and no unit
    # test in `tests/test_xagg.py` can catch it — only a live run through
    # the harness wrapper does.
    "offender_age_profile",
    "accused_relationship_breakdown",
    "seized_property_disposition",
    "incident_time_of_day",
    # [Gold-QA fix — Module 35, G6] same additive convention — the
    # arrest-rate shape. FIFTH module family to depend on this
    # hand-maintained Literal; `tests/test_xagg.py::
    # test_every_new_aggregate_kind_is_accepted_by_the_harness_tool_result`
    # is what now forces it to be updated.
    "arrest_rate",
    # [Gold-QA fix — Module 36, CR3] same additive convention — the
    # subject-filtered FIR listing shape.
    "filtered_fir_listing",
    # [Gold-QA fix — Module 44, questions M2 and CS4] SEVENTH and EIGHTH
    # families to depend on this hand-maintained Literal. Added in the same
    # commit as the aggregates themselves, before any live run, precisely
    # because Module 31 proved that a missing entry here is invisible to
    # every unit test and surfaces only as an EMPTY answer with status=None
    # through /api/chat.
    "station_caseload_by_specialisation",
    "criminal_record_local_match_gap",
    # [Gold-QA fix — Module 74, KB3] NINTH family to depend on this
    # hand-maintained Literal. Added in the same commit as the
    # aggregate itself, before any live run — the trap this comment
    # block describes has now bitten four times, and every one of them
    # surfaced only as an EMPTY answer with status=None through
    # /api/chat, invisible to every unit test in tests/test_xagg.py.
    "officer_role_pair_overlap",
]


class XAggToolInput(CrossCaseToolInput):
    """
    Cross-case aggregates: recurrence counts, and grouped counts over case
    metadata (station / status / category), plus plain case enumeration.

    [PRESERVE — design §2.4] TWO CANNED AGGREGATE FAMILIES, keyword-
    dispatched inside `run_aggregate()`. Deliberately NOT a general
    text-to-SQL/text-to-Cypher system.
    """

    target_entity: Optional[str] = None


class XAggToolResult(CrossCaseToolResult):
    """
    [PRESERVE — design §2.4] Never falls back to RAG (inherited, pinned
    False).

    [PRESERVE — design §2.4] VERIFIER-REJECTION HANDLING IS DIFFERENT HERE
    (a sub-agent-level concern, not this tool's): the aggregate is
    machine-computed and correct by construction, so a Verifier rejection
    means the natural-language PARAPHRASE failed — not that the evidence
    is unsound. `raw_summary_text` is what makes that fallback possible.
    """

    aggregate_kind: Optional[AggregateKind] = Field(
        default=None, description="Which canned family answered. Shapes presentation."
    )
    raw_summary_text: Optional[str] = Field(
        default=None,
        description=(
            "[PRESERVE] Deterministic rendering of the computed aggregate, "
            "independent of any LLM paraphrase. This is what gets served on "
            "Verifier rejection."
        ),
    )


def _render_aggregate_text(agg_result: dict) -> str:
    """
    Verbatim port of orchestrator.py's XAGG-route text rendering (the same
    four branches, same formatting) — not new logic, just relocated so the
    tool wrapper can hand the Verifier the identical deterministic text
    orchestrator.py already builds today.
    """
    kind = agg_result["kind"]
    # Every enumerating branch leads with its own total. This text is what the
    # user actually reads whenever the natural-language paraphrase fails
    # verification (see large_scale_aggregate.py's raw-aggregate fallback), and
    # without a total a "how many cases involve PPC?" question was answered
    # with 60+ bullet rows and no number anywhere — the reader had to count the
    # list themselves (verify-log Finding W). The count is derived from the
    # same rows being rendered, so it cannot disagree with them.
    if kind == "graph_recurrence":
        rows = agg_result["results"]
        lines = [
            f"**{len(rows)} matching {agg_result['entity_type']}(s) found.**",
            "",
        ] if rows else []
        lines += [
            f"- {r['name']} ({agg_result['entity_type']}): appears in {r['case_count']} cases — {', '.join(r['case_ids'])}"
            for r in rows
        ]
    elif kind == "case_listing":
        cases = agg_result["cases"]
        lines = [f"**{len(cases)} matching case(s) found.**", ""] if cases else []
        lines += [
            f"- {c['case_id']} (FIR {c['fir_number'] or 'N/A'}): {c['crime_category'] or 'uncategorized'} "
            f"— {c['investigation_status'] or 'unknown status'}, {c['police_station'] or 'unknown station'}"
            for c in cases
        ]
    elif kind == "total_count":
        lines = [f"Total cases: {agg_result['total_cases']}"]
    # [Gold-QA fix — Module 1] Kept in sync with orchestrator.py's own
    # identical branches for these five kinds — see this function's own
    # docstring and AggregateKind's comment above for why this file needs
    # its own copy rather than reusing orchestrator.py's.
    elif kind == "unsupported_aggregate":
        lines = [agg_result["message"]]
    elif kind == "total_accused_count":
        lines = [f"Total distinct accused persons: {agg_result['total_accused']}"]
    elif kind == "gender_breakdown":
        if agg_result["unsupported"]:
            lines = [agg_result["message"]]
        else:
            lines = [f"- {c['key']}: {c['count']}" for c in agg_result["counts"]]
            lines.append(f"Total accused: {agg_result['total_accused']}")
    # [Gold-QA fix — Module 2, A7] Reporting-delay count. Kept in sync with
    # orchestrator.py's two identical branches (same convention as the other
    # kinds above). Degrades to the stated limitation when not yet populated.
    elif kind == "reporting_delay_count":
        if agg_result["unsupported"]:
            lines = [agg_result["message"]]
        else:
            wd, tot = agg_result["with_delay_reason"], agg_result["total_firs"]
            pct = f" (~{round(100 * wd / tot)}%)" if tot else ""
            lines = [
                f"{wd} of {tot} FIRs recorded a reporting-delay reason{pct}; "
                f"the remaining {tot - wd} were reported with no stated delay."
            ]
    # [Gold-QA fix — Module 7, CP6] Placeholder-officer count. Kept in sync
    # with orchestrator.py's two identical branches. Reports the CURRENT
    # count (the honest, live-defensible answer) and, when it differs, the
    # historical ever-had-a-placeholder count — see
    # xagg.py::_placeholder_officer_count()'s own comment for why the two
    # can diverge.
    elif kind == "placeholder_officer_count":
        cur, ever = agg_result["current_count"], agg_result["ever_count"]
        asi, si = agg_result["asi_count"], agg_result["si_count"]
        caveat = (
            f" {ever - cur} additional case(s) originally had a placeholder "
            f"officer too but have since been assigned a real one."
            if ever > cur else ""
        )
        lines = [
            f"{cur} FIRs currently carry only a placeholder investigating "
            f"officer — {asi} marked \"(نامزد ASI)\", {si} marked "
            f"\"(نامزد SI)\".{caveat}"
        ]
    elif kind == "district_breakdown":
        label = agg_result.get("entity_label")
        lines = [
            f"- {c['district']}: {c['count']} {label + ' record(s)' if label else 'case(s)'}"
            for c in agg_result["counts"]
        ]
    # [Gold-QA fix — Module 14, CR7] Criminal-record status + court-outcome
    # consistency. Kept in sync with orchestrator.py's two identical branches.
    elif kind == "criminal_record_court_crosscheck":
        lines = render_criminal_record_crosscheck(agg_result)
    elif kind == "cms_fir_linkage":
        lines = render_cms_fir_linkage(agg_result)
    elif kind == "dv_report_fir_match":
        lines = render_dv_report_fir_match(agg_result)
    elif kind == "case_completeness_scan":
        lines = render_case_completeness_scan(agg_result)
    elif kind == "weapon_compliance_scan":
        lines = render_weapon_compliance_scan(agg_result)
    # [Gold-QA fix — CR4, Module 28] Kept in sync with orchestrator.py's two
    # identical XAGG-route rendering sites, per this function's own docstring.
    elif kind == "weapon_evidence_chain":
        lines = render_weapon_evidence_chain(agg_result)
    elif kind == "court_readiness_scan":
        lines = render_court_readiness_scan(agg_result)
    # [Gold-QA fix — Module 74, KB3] Kept in sync with orchestrator.py's
    # two identical XAGG-route rendering sites, per this function's own
    # docstring.
    elif kind == "officer_role_pair_overlap":
        lines = render_officer_role_pair_overlap(agg_result)
    elif kind == "station_total_count":
        lines = [f"Total police stations: {agg_result['total_stations']}"]
    # [Gold-QA fix — Module 13, RC-2] Three new kinds from the rate/ratio
    # and time-bucket primitives. Kept in sync with orchestrator.py's two
    # identical branches (same convention as every other kind above) — see
    # xagg.py's own docstrings on _weapon_recovery_rate_by_district()/
    # _statute_mix_by_year()/_reporting_delay_rate_by_year() for the exact
    # result shape each produces.
    elif kind == "rate_breakdown":
        lines = [
            f"- {c['district']}: {c['cases_with_weapon']} of {c['total_cases']} cases recovered a "
            f"weapon (~{round(100 * c['rate'])}%)"
            for c in agg_result["counts"]
        ]
    elif kind == "time_bucketed_breakdown":
        lines = []
        for b in agg_result["buckets"]:
            lines.append(f"**{b['year']}:**")
            lines.extend(f"  - {c['key']}: {c['count']}" for c in b["counts"])
    elif kind == "time_bucketed_rate":
        lines = [agg_result["note"], ""]
        lines.extend(
            f"- {b['year']}: {b['delayed_count']} of {b['total_count']} FIRs recorded a delay "
            f"reason (~{round(100 * b['rate'])}%)"
            for b in agg_result["buckets"]
        )
    # [Gold-QA fix — Module 22, M7] Kept in sync with orchestrator.py's two
    # identical XAGG-route rendering sites, per this function's own docstring.
    elif kind == "time_bucketed_mean":
        lines = render_time_bucketed_mean(agg_result)
    # [Gold-QA fix — Module 44, M2] Kept in sync with orchestrator.py's two
    # identical XAGG-route rendering sites, per this function's own docstring.
    elif kind == "station_caseload_by_specialisation":
        lines = render_station_caseload_by_specialisation(agg_result)
    # [Gold-QA fix — Module 44, CS4] same.
    elif kind == "criminal_record_local_match_gap":
        lines = render_criminal_record_local_match_gap(agg_result)
    # [Gold-QA fix — Module 23, M5] Kept in sync with orchestrator.py's two
    # identical XAGG-route rendering sites, per this function's own docstring.
    elif kind == "weapon_statute_cooccurrence":
        lines = render_weapon_statute_cooccurrence(agg_result)
    # [Gold-QA fix - Module 24, M4] same, for the statute x court-stage join.
    elif kind == "statute_court_stage_join":
        lines = render_statute_court_stage_join(agg_result)
    # [Gold-QA fix — Module 31, G1] same, for the offender age profile.
    elif kind == "offender_age_profile":
        lines = render_offender_age_profile(agg_result)
    # [Gold-QA fix — Module 32, G1] same, for the relationship breakdown.
    elif kind == "accused_relationship_breakdown":
        lines = render_accused_relationship_breakdown(agg_result)
    # [Gold-QA fix — Module 33, G1] same, for the seized-property breakdown.
    elif kind == "seized_property_disposition":
        lines = render_seized_property_disposition(agg_result)
    # [Gold-QA fix — Module 34, G1] same, for the time-of-day distribution.
    elif kind == "incident_time_of_day":
        lines = render_incident_time_of_day(agg_result)
    # [Gold-QA fix — Module 35, G6] same, for the arrest rate.
    elif kind == "arrest_rate":
        lines = render_arrest_rate(agg_result)
    # [Gold-QA fix — Module 36, CR3] same, for the filtered FIR listing.
    elif kind == "filtered_fir_listing":
        lines = render_filtered_fir_listing(agg_result)
    else:
        lines = [f"- {c['key']}: {c['count']} cases" for c in agg_result["counts"]]
        # [Legal-code semantic layer] Kept in sync with orchestrator.py's
        # own identical XAGG-route rendering — see this function's own
        # docstring ("verbatim port... not new logic"). crime_category can
        # combine several legal acts per case (e.g. "PPC, Arms Ordinance
        # 1965") — counts_by_act, when present
        # (src/pipeline/xagg.py::_station_or_category_counts()), re-derives
        # a per-ACT total so e.g. two differently-combined Arms-Ordinance
        # buckets above collapse into one real number here instead of
        # staying invisible.
        if agg_result.get("counts_by_act"):
            lines.append("")
            lines.append("Breakdown by individual legal code (a case can involve more than one):")
            lines.extend(f"- {c['key']}: {c['count']} cases" for c in agg_result["counts_by_act"])
    text = "\n".join(lines) or "(no matching cases found)"

    # Mirrors orchestrator.py's XAGG rendering exactly (same prepend, same
    # "NOTE: " prefix): a filter the corpus cannot evaluate, or an
    # unresolved jurisdiction, means these figures answer a BROADER question
    # than the one asked — and that is invisible from the counts alone. This
    # text is also what the Verifier sees on rejection (raw_summary_text),
    # so the caveat must live in the text itself, not only in metadata.
    caveats = list(agg_result.get("unsupported_filters") or [])
    if jurisdiction_unresolved():
        caveats.append(_UNSUPPORTED_JURISDICTION)
    if caveats:
        text = "\n".join(f"NOTE: {c}" for c in caveats) + "\n\n" + text
    return text


def _case_ids_touched(agg_result: dict) -> list[str]:
    """
    Best-effort per-case provenance for observability — NOT load-bearing
    for the Verifier's leakage check, since (matching orchestrator.py's own
    XAGG verifier call, which passes no cross_case_ids) the synthetic
    aggregate chunk below carries no metadata.case_id at all: aggregate
    evidence belongs to no single case, so it is inert to that check by
    the same reasoning already applied to SQL/WEB chunks.
    """
    kind = agg_result["kind"]
    if kind == "graph_recurrence":
        ids: set[str] = set()
        for r in agg_result["results"]:
            ids.update(r.get("case_ids") or [])
        return sorted(ids)
    if kind == "case_listing":
        return sorted({c["case_id"] for c in agg_result["cases"] if c.get("case_id")})
    return []


async def xagg_tool(tool_input: XAggToolInput) -> XAggToolResult:
    """The XAGG primitive: cross-case aggregate, never falls back to RAG."""
    caller = tool_input.execution.caller
    gateway = await get_gateway()

    try:
        agg_result = await run_aggregate(
            tool_input.query_text,
            tool_input.target_entity,
            gateway,
            user_id=caller.user_id,
            user_role=caller.role.value,
        )
    except PermissionError as exc:
        # run_aggregate() has already written the authorization_violation
        # audit record (see xagg.py) — this wrapper only translates the
        # outcome, it does not re-derive or duplicate it.
        return XAggToolResult(
            status=ToolStatus.DENIED,
            error=ToolError(kind="permission_denied", message=str(exc)),
        )
    except Exception as exc:
        logger.error("XAGG tool: cross-case aggregate failed: %s", exc)
        return XAggToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
        )

    raw_summary_text = _render_aggregate_text(agg_result)
    chunk = EvidenceChunk(
        id="xagg-aggregate",
        text=raw_summary_text,
        metadata=ChunkMetadata(source_tool="XAGG", source_file="cross-case aggregate"),
    )

    return XAggToolResult(
        status=ToolStatus.OK,
        chunks=[chunk],
        case_ids_touched=_case_ids_touched(agg_result),
        aggregate_kind=agg_result["kind"],
        raw_summary_text=raw_summary_text,
    )


xagg_tool.name = "XAGG"
