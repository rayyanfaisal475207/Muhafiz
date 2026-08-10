"""
Harness evaluation run — exercises the agent harness end-to-end against live
infrastructure and reports what each sub-agent actually did.

WHAT THIS RUNS, AND WHY IT GOES THROUGH THE SUPERVISOR
──────────────────────────────────────────────────────
Each scenario enters at `supervisor.invoke()` with a real `route_query()`
result, so a run exercises the whole path an investigator's question takes:

    route_query -> classifier -> supervisor dispatch -> sub-agent
                -> tools (Chroma / Postgres / Apache AGE / SQL)
                -> generation (local Qwen3 via the model server)
                -> verifier gate -> bounded SubAgentResult

Calling sub-agents directly would be easier and would test less: it would skip
routing entirely, which is where a question is matched to a capability, and it
would not catch a sub-agent that works in isolation but is never reachable.

WHAT IT DOES NOT DO
───────────────────
This measures BEHAVIOUR, not answer quality. There is no ground-truth answer
set for this corpus, so nothing here scores correctness of prose. What it does
verify is checkable without one:

  * which sub-agent handled the question, and on what basis
  * which tools contributed, and which were attempted and degraded
  * whether the answer carries citations, and whether every [Document N]
    marker in the prose resolves to a real returned citation
  * whether the verifier gate passed, and what it cost
  * whether role gating denies cross-case access to an investigator

`--judge` additionally asks an LLM to rate groundedness and relevance. That is
a signal, not a score: it is the same class of model that wrote the answer.

USAGE
    python scripts/evaluate_harness.py                 # all scenarios, table
    python scripts/evaluate_harness.py --json out.json # machine-readable
    python scripts/evaluate_harness.py --only timeline # one sub-agent
    python scripts/evaluate_harness.py --judge         # + LLM quality rating
    python scripts/evaluate_harness.py --repeat 3      # stability across runs
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# Load .env before importing anything that reads config at import time.
from dotenv import load_dotenv

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

from src.pipeline.harness import classifier, supervisor  # noqa: E402
from src.pipeline.harness.contracts import (  # noqa: E402
    CallerContext,
    Role,
    SubAgentInput,
    SubAgentStatus,
)
from src.pipeline.harness.tools import registry  # noqa: E402
from src.pipeline.router import route_query  # noqa: E402

# ── Fixtures ──────────────────────────────────────────────────────────────
# A real user and session, because Report Drafting records generated files
# against both (generated_files.session_id / user_id are NOT NULL).
USER_ID = "4939e74f-543b-4143-a997-49a86bc98da6"
SESSION_ID = "c260e4fb-3a2e-4698-bd30-2f35aa838bc3"

# CASE-009 is the richest case in the corpus (8 documents: FIR, case diary,
# three witness statements, recovery memo, darkhast), which makes it the
# fairest test of a case-scoped sub-agent.
CASE = "CASE-009"


@dataclass
class Scenario:
    """One question, and what it is meant to demonstrate."""
    id: str
    question: str
    role: Role
    case_id: Optional[str]
    expect_sub_agent: Optional[str]
    demonstrates: str
    output_format: str = "chat"
    # Scenarios that assert a REFUSAL rather than an answer. Their success
    # condition is inverted: an answer would be the failure.
    expect_denial: bool = False


SCENARIOS: list[Scenario] = [
    Scenario(
        id="semantic-search",
        question="What was reported stolen and who reported it?",
        role=Role.INVESTIGATOR, case_id=CASE,
        expect_sub_agent="semantic_search",
        demonstrates="Document retrieval with per-claim citations.",
    ),
    # Reaching Case Summarization takes a question the router calls GRAPH —
    # an entity-anchored one. "Summarize this case." alone routes to RAG ->
    # semantic_search, and asking about relationships in general routes to
    # GRAPH_HYBRID -> investigative_analysis. Both are the router's judgement,
    # which the harness follows rather than overrides.
    Scenario(
        id="case-summary",
        question="Who is connected to Waqas Ali Niazi in this case?",
        role=Role.INVESTIGATOR, case_id=CASE,
        expect_sub_agent="case_summary",
        demonstrates="Documents + entity graph combined into one summary.",
    ),
    Scenario(
        id="investigative-analysis",
        question="Analyze the evidence and identify who is implicated and how.",
        role=Role.INVESTIGATOR, case_id=CASE,
        expect_sub_agent="investigative_analysis",
        demonstrates="Three retrieval legs (RAG + GRAPH + SQL) in one answer.",
    ),
    Scenario(
        id="timeline",
        question="Build a timeline of events in this case.",
        role=Role.INVESTIGATOR, case_id=CASE,
        expect_sub_agent="timeline",
        demonstrates=(
            "Deterministic chronology, with an explicit UNKNOWN conflict state "
            "where contradiction-checking has not run."
        ),
    ),
    Scenario(
        id="report-draft",
        question="Draft a report on this case.",
        role=Role.INVESTIGATOR, case_id=CASE,
        expect_sub_agent="report_draft", output_format="file_pdf",
        demonstrates="A downloadable PDF, recorded against the session.",
    ),
    Scenario(
        id="cross-case-linkage",
        question="What other cases is Hina Malik involved in?",
        role=Role.SUPERVISOR, case_id=None,
        expect_sub_agent="cross_case_linkage",
        demonstrates=(
            "Cross-case identity search. Unconfirmed matches are surfaced as "
            "flagged leads, never asserted as fact."
        ),
    ),
    Scenario(
        id="aggregate",
        question="How many cases are there per crime category?",
        role=Role.SUPERVISOR, case_id=None,
        expect_sub_agent="aggregate_analysis",
        demonstrates="Deterministic aggregate over every accessible case.",
    ),
    # XNETWORK is Cross-Case Linkage's OTHER leg — thematic/MO patterns rather
    # than identity matching. It is expected to come back empty on this corpus:
    # `community_reports` is empty and no community-summary collection exists,
    # because the offline community pipeline has not been run. Included anyway,
    # so the report states that plainly instead of leaving the leg untested.
    Scenario(
        id="cross-case-patterns",
        question="What patterns or common methods appear across all the cyber fraud cases?",
        role=Role.SUPERVISOR, case_id=None,
        expect_sub_agent="cross_case_linkage",
        demonstrates="Thematic cross-case pattern search (XNETWORK leg).",
    ),
    # DIRECT is the one route with no sub-agent, and that is deliberate: it
    # answers from general knowledge with no retrieval, so the Verifier never
    # gates it and the harness hands it back to the caller. Worth showing that
    # the supervisor declines cleanly rather than forcing a retrieval path.
    Scenario(
        id="direct-no-subagent",
        question="Hello, what can you help me with?",
        role=Role.INVESTIGATOR, case_id=CASE,
        expect_sub_agent=classifier.NO_SUB_AGENT,
        demonstrates="A conversational turn correctly bypasses retrieval entirely.",
    ),

    # ── Security behaviour ────────────────────────────────────────────────
    # An investigator asking a cross-case question must be REFUSED. This is
    # the role gate from design §4.3, and a run that cannot demonstrate it is
    # not a complete evaluation of an evidence system.
    Scenario(
        id="role-gate-denial",
        question="What other cases is Hina Malik involved in?",
        role=Role.INVESTIGATOR, case_id=CASE,
        expect_sub_agent="cross_case_linkage",
        demonstrates="An investigator is DENIED cross-case access.",
        expect_denial=True,
    ),
]


@dataclass
class Outcome:
    """What one scenario run produced."""
    scenario_id: str
    question: str
    demonstrates: str
    role: str
    route: str = ""
    routing_basis: str = ""
    sub_agent: str = ""
    expected_sub_agent: Optional[str] = None
    routed_as_expected: bool = False
    status: str = ""
    tools_used: list[str] = field(default_factory=list)
    degraded_from: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    citation_count: int = 0
    citations: list[dict] = field(default_factory=list)
    answer_chars: int = 0
    answer_text: str = ""
    timeline_events: int = 0
    cross_case_links: int = 0
    unconfirmed_links: int = 0
    elapsed_s: float = 0.0
    event_count: int = 0
    error: Optional[str] = None
    exception: Optional[str] = None
    # Derived checks
    citation_markers_resolve: Optional[bool] = None
    dangling_markers: list[int] = field(default_factory=list)
    verdict: str = ""
    judge: Optional[dict] = None


# ── Derived checks ────────────────────────────────────────────────────────

_MARKER_RE = re.compile(r"\[Document (\d+)\]")


def check_citation_markers(answer: str, citation_count: int) -> tuple[Optional[bool], list[int]]:
    """
    Does every `[Document N]` in the prose resolve to a citation that came back?

    This is the one answer-quality property checkable WITHOUT ground truth, and
    it is the one that matters most in an evidence system: a marker pointing at
    nothing is a claim with no source behind it, which is exactly what an
    investigator would (reasonably) trust.

    Returns (None, []) when there is no prose or no citation to check, so
    "nothing to verify" is never reported as a pass.
    """
    if not answer or not citation_count:
        return None, []
    referenced = {int(n) for n in _MARKER_RE.findall(answer)}
    if not referenced:
        return None, []
    dangling = sorted(n for n in referenced if n < 1 or n > citation_count)
    return (not dangling), dangling


def verdict_for(outcome: Outcome, scenario: Scenario) -> str:
    """
    One word for what happened, judged against what the scenario intended.

    A denial scenario INVERTS the test: being refused is the pass, and being
    answered is the failure. Collapsing both into "did it return OK" would
    report a security regression as a success.
    """
    if outcome.exception:
        return "CRASH"

    if scenario.expect_denial:
        denied = (
            outcome.status == SubAgentStatus.DENIED.value
            or (outcome.error or "").lower().find("denied") >= 0
            or (outcome.error or "").lower().find("permission") >= 0
        )
        return "PASS" if denied else "SECURITY-FAIL"

    if not outcome.routed_as_expected:
        return "MISROUTED"

    # DIRECT deliberately produces no SubAgentResult: it answers from general
    # knowledge with no retrieval, so the harness declines it and the caller
    # answers on its existing path. Reaching that state IS the pass — treating
    # a missing result as a failure would penalise the route for working.
    if outcome.status == "NO_SUB_AGENT":
        return "PASS"

    if outcome.citation_markers_resolve is False:
        return "BAD-CITES"
    if outcome.status == SubAgentStatus.OK.value:
        return "PASS"
    if outcome.status == SubAgentStatus.PARTIAL.value:
        # Answered, but a source was missing or a check could not run — and the
        # sub-agent said so in a caveat. Distinguished from PASS because
        # "answered completely" and "answered with a stated gap" are different
        # facts, and an evaluation that renders them identically is hiding the
        # very thing the PARTIAL status exists to communicate.
        return "PARTIAL"
    if outcome.status == SubAgentStatus.EMPTY.value:
        # A truthful "nothing found" is a legitimate answer, not a failure.
        # But two very different things arrive here, and collapsing them would
        # hide the one that matters:
        #
        #   NO-MATCH  — the search ran and the corpus genuinely holds nothing,
        #               or holds only unconfirmed leads, which are reported as
        #               flagged possibilities rather than asserted as fact.
        #   NO-DATA   — the search could not run, because the data it reads has
        #               never been built (XNETWORK's community reports).
        #
        # The first is the system working. The second is a gap in the deployment
        # that no amount of harness work will close.
        if outcome.cross_case_links or outcome.citation_count:
            return "NO-MATCH"
        return "NO-DATA" if outcome.degraded_from else "NO-MATCH"
    if outcome.status == SubAgentStatus.ABSTAINED.value:
        return "ABSTAINED"
    return outcome.status or "UNKNOWN"


# ── Runner ────────────────────────────────────────────────────────────────

async def run_scenario(scenario: Scenario, gateway: Any) -> Outcome:
    outcome = Outcome(
        scenario_id=scenario.id,
        question=scenario.question,
        demonstrates=scenario.demonstrates,
        role=scenario.role.value,
        expected_sub_agent=scenario.expect_sub_agent,
    )

    caller = CallerContext(
        user_id=USER_ID, role=scenario.role, active_case_id=scenario.case_id,
    )
    agent_input = SubAgentInput(
        query_text=scenario.question,
        caller=caller,
        output_format=scenario.output_format,
        session_id=SESSION_ID,
    )

    t0 = time.monotonic()
    try:
        # Real routing, not a hand-written route dict: routing is part of what
        # is being evaluated.
        route_result = await route_query(scenario.question)
        outcome.route = str(route_result.get("route", ""))
        decision = classifier.describe(route_result, scenario.question)
        outcome.routing_basis = str(decision.get("basis", ""))

        state = await supervisor.invoke(
            agent_input, route_result, gateway=gateway,
        )
        outcome.elapsed_s = time.monotonic() - t0
        outcome.sub_agent = state.selected_agent or ""
        outcome.event_count = len(state.events or [])
        outcome.routed_as_expected = (
            scenario.expect_sub_agent is None
            or outcome.sub_agent == scenario.expect_sub_agent
        )

        result = state.result
        if result is None:
            # DIRECT returns no sub-agent result by design.
            outcome.status = "NO_SUB_AGENT"
        else:
            outcome.status = result.status.value
            outcome.tools_used = list(result.tools_used or [])
            outcome.degraded_from = list(result.degraded_from or [])
            outcome.caveats = list(result.caveats or [])
            outcome.answer_text = result.answer_text or ""
            outcome.answer_chars = len(outcome.answer_text)
            outcome.citation_count = len(result.citations or [])
            outcome.citations = [
                {
                    "index": c.document_index,
                    "source": c.display_label(),
                    "case_id": c.case_id,
                    "file": c.source_file,
                    "confidence": c.confidence,
                }
                for c in (result.citations or [])
            ]
            outcome.timeline_events = len(getattr(result, "timeline", None) or [])
            links = getattr(result, "cross_case_links", None) or []
            outcome.cross_case_links = len(links)
            outcome.unconfirmed_links = sum(1 for ln in links if ln.is_unconfirmed)
            if result.error:
                outcome.error = f"{result.error.kind}: {result.error.message}"

            resolves, dangling = check_citation_markers(
                outcome.answer_text, outcome.citation_count
            )
            outcome.citation_markers_resolve = resolves
            outcome.dangling_markers = dangling

    except Exception as exc:
        outcome.elapsed_s = time.monotonic() - t0
        outcome.exception = f"{type(exc).__name__}: {exc}"
        outcome.error = traceback.format_exc(limit=3)

    outcome.verdict = verdict_for(outcome, scenario)
    return outcome


# ── Optional LLM judge ────────────────────────────────────────────────────

_JUDGE_SYSTEM = """You are evaluating an answer produced by a police evidence-\
retrieval system. You are given the question, the answer, and the PROVENANCE of \
the sources that were retrieved (filenames and which retrieval tool found them).

IMPORTANT: you are NOT given the source text itself, so you cannot verify whether \
a specific claim appears in a document. Do not penalise the answer for that. Judge \
only what is visible to you.

Rate two things from 1 to 5:
  grounded  — is every claim ATTRIBUTED to a source, via a [Document N] marker
              that matches a listed source, and are the sources of a type that
              could plausibly support the claim (e.g. an FIR for a complaint, a
              recovery memo for recovered property)? 5 = every claim carries a
              plausible citation, 1 = claims float with no attribution.
  relevant  — does the answer address the question actually asked? 5 = directly
              answers it, 1 = does not address it.

Reply with ONLY a JSON object: {"grounded": N, "relevant": N, "note": "one short sentence"}"""


async def judge_outcome(outcome: Outcome) -> Optional[dict]:
    """
    Ask an LLM to rate groundedness and relevance.

    Deliberately reported as a separate, clearly-labelled signal: the judge is
    the same class of model that wrote the answer, so this is a smoke test for
    obvious failure, NOT an accuracy metric. Never merged into `verdict`.
    """
    if not outcome.answer_text:
        return None

    from src.llm.client import call_llm

    # The judge sees only citation PROVENANCE — filenames and source tools —
    # never the chunk text, because `SubAgentResult` deliberately does not carry
    # it across the handoff boundary (design §3). That is a real limit on what
    # this rating can mean: the judge cannot confirm a claim appears in a
    # source it was never shown, so a low `grounded` score here may reflect the
    # judge's missing context rather than an ungrounded answer. Said explicitly
    # in the prompt so the model rates what it can actually see.
    sources = "\n".join(
        f"[Document {c['index']}] {c['file'] or 'unknown'} "
        f"(via {c['source']}, case {c['case_id'] or 'n/a'})"
        for c in outcome.citations
    ) or "(no sources returned)"

    user = (
        f"QUESTION\n{outcome.question}\n\n"
        f"RETRIEVED SOURCES\n{sources}\n\n"
        f"ANSWER\n{outcome.answer_text}"
    )
    try:
        raw = await call_llm(_JUDGE_SYSTEM, user, temperature=0.0, max_tokens=300)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(match.group(0)) if match else {"raw": raw[:200]}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ── Reporting ─────────────────────────────────────────────────────────────

_VERDICT_MARK = {
    "PASS": "PASS", "PARTIAL": "PARTIAL", "NO-MATCH": "NO-MATCH",
    "NO-DATA": "NO-DATA", "ABSTAINED": "ABSTAIN", "MISROUTED": "MISROUTE",
    "BAD-CITES": "BAD-CITE", "SECURITY-FAIL": "SEC-FAIL", "CRASH": "CRASH",
}

# Only CRASH, SEC-FAIL, MISROUTE and BAD-CITE are failures. The rest are the
# system correctly reporting the limits of what it could establish — which, in
# an evidence platform, is the behaviour worth having.
_VERDICT_MEANING = {
    "PASS": "answered, correctly routed, citations resolve",
    "PARTIAL": "answered with a stated gap (a source was missing; caveat given)",
    "NO-MATCH": "searched; nothing confirmed found (a truthful answer)",
    "NO-DATA": "could not search — the underlying data has not been built",
    "ABSTAIN": "declined to answer rather than serve unverified prose",
    "MISROUTE": "reached a different sub-agent than the question needed",
    "BAD-CITE": "prose cites a [Document N] that was never returned",
    "SEC-FAIL": "a role gate that should have denied did not",
    "CRASH": "raised an unhandled exception",
}


def print_report(outcomes: list[Outcome], elapsed: float) -> None:
    w = 92
    print()
    print("=" * w)
    print("  MUHAFIZ AGENT HARNESS — EVALUATION RUN")
    print("=" * w)
    print(f"  {len(outcomes)} scenarios, {elapsed:.1f}s total, live infrastructure")
    print("=" * w)
    print()

    for o in outcomes:
        print("-" * w)
        print(f"  [{_VERDICT_MARK.get(o.verdict, o.verdict)}]  {o.scenario_id}")
        print("-" * w)
        print(f"  question    : {o.question}")
        print(f"  as role     : {o.role}")
        print(f"  demonstrates: {o.demonstrates}")
        print()
        routed = "as expected" if o.routed_as_expected else (
            f"EXPECTED {o.expected_sub_agent}"
        )
        print(f"  routed      : {o.route or '?'} -> {o.sub_agent or '?'}  ({routed})")
        if o.routing_basis:
            print(f"  basis       : {o.routing_basis}")
        print(f"  status      : {o.status}    ({o.elapsed_s:.1f}s, {o.event_count} events)")
        print(f"  tools used  : {o.tools_used or '(none)'}")
        if o.degraded_from:
            print(f"  degraded    : {o.degraded_from}")
        print(f"  citations   : {o.citation_count}")
        for c in o.citations[:6]:
            conf = f" conf={c['confidence']:.2f}" if c["confidence"] is not None else ""
            print(f"                [{c['index']}] {c['source']:<20} "
                  f"{c['file'] or '-'}{conf}")
        if o.timeline_events:
            print(f"  timeline    : {o.timeline_events} event(s)")
        if o.cross_case_links:
            print(f"  links       : {o.cross_case_links} "
                  f"({o.unconfirmed_links} unconfirmed)")
        if o.citation_markers_resolve is not None:
            mark = "all resolve" if o.citation_markers_resolve else (
                f"DANGLING {o.dangling_markers}"
            )
            print(f"  cite check  : {mark}")
        for c in o.caveats:
            print(f"  caveat      : {c}")
        if o.error:
            first = o.error.strip().splitlines()[-1] if o.error else ""
            print(f"  error       : {first[:110]}")
        if o.judge:
            print(f"  llm judge   : {o.judge}   (signal only, not a score)")
        if o.answer_text:
            print()
            print("  answer:")
            for line in o.answer_text.splitlines()[:14]:
                print(f"    {line[:100]}")
            extra = len(o.answer_text.splitlines()) - 14
            if extra > 0:
                print(f"    ... ({extra} more lines, {o.answer_chars} chars total)")
        print()

    # ── Summary ──
    print("=" * w)
    print("  SUMMARY")
    print("=" * w)
    print(f"  {'scenario':<22} {'verdict':<9} {'sub-agent':<23} "
          f"{'evidence':<28} time")
    print("  " + "-" * (w - 2))
    for o in outcomes:
        # What the run actually produced, rather than only which tools ran —
        # "XGRAPH degraded" and "3 unconfirmed leads" are different outcomes
        # and a reader must be able to tell them apart at a glance.
        if o.tools_used:
            evidence = f"{','.join(o.tools_used)}"
            if o.citation_count:
                evidence += f" ({o.citation_count} cites)"
            if o.degraded_from:
                evidence += f" -{','.join(o.degraded_from)}"
        elif o.cross_case_links:
            evidence = f"{o.cross_case_links} unconfirmed lead(s)"
        elif o.degraded_from:
            evidence = f"none - {','.join(o.degraded_from)} unavailable"
        else:
            evidence = "-"
        print(f"  {o.scenario_id:<22} {_VERDICT_MARK.get(o.verdict, o.verdict):<9} "
              f"{o.sub_agent or '-':<23} {evidence[:28]:<28} {o.elapsed_s:.1f}s")
    print()

    print("  verdicts seen:")
    for verdict in sorted({o.verdict for o in outcomes}):
        mark = _VERDICT_MARK.get(verdict, verdict)
        print(f"    {mark:<10} {_VERDICT_MEANING.get(mark, '')}")
    print()

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.verdict] = counts.get(o.verdict, 0) + 1
    print("  " + "   ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    # `__direct__` is the no-sub-agent sentinel, not a sub-agent, so it must not
    # inflate the coverage count.
    reached = {
        o.sub_agent for o in outcomes
        if o.sub_agent and o.sub_agent != classifier.NO_SUB_AGENT
    }
    all_names = (
        set(classifier._ROUTE_TO_SUB_AGENT.values()) | {"report_draft", "timeline"}
    ) - {classifier.NO_SUB_AGENT}
    missing = sorted(all_names - reached)
    print(f"  sub-agents exercised: {len(reached)}/{len(all_names)}")
    if missing:
        print(f"  not exercised: {', '.join(missing)}")

    blocking = [o for o in outcomes if o.verdict in
                ("CRASH", "SECURITY-FAIL", "MISROUTED", "BAD-CITES")]
    print()
    if blocking:
        print(f"  {len(blocking)} SCENARIO(S) NEED ATTENTION:")
        for o in blocking:
            print(f"    - {o.scenario_id}: {o.verdict}")
    else:
        print("  No crashes, misroutes, dangling citations, or security failures.")
    print("=" * w)


def print_stability(runs: list[list[Outcome]]) -> None:
    """Same questions, several runs — does the system answer consistently?"""
    w = 92
    print()
    print("=" * w)
    print(f"  STABILITY ACROSS {len(runs)} RUNS")
    print("=" * w)
    print(f"  {'scenario':<24} {'verdicts':<28} {'evidence count':<16} time (s)")
    print("  " + "-" * (w - 2))
    # Iterate the scenarios that actually ran, not the module-level list, so a
    # filtered run (--only) does not index into the wrong outcomes.
    for i in range(len(runs[0])):
        verdicts = [r[i].verdict for r in runs]
        # Citations for sub-agents that answer from evidence; links for
        # Cross-Case Linkage, whose finding IS the link list — reporting only
        # citations showed "0,0" for a run that returned 3 flagged leads.
        counts = [
            r[i].citation_count or r[i].cross_case_links or r[i].timeline_events
            for r in runs
        ]
        times = [r[i].elapsed_s for r in runs]
        varies = len(set(verdicts)) > 1 or len(set(counts)) > 1
        flag = "  <-- VARIES" if varies else ""
        median = statistics.median(times)
        print(f"  {runs[0][i].scenario_id:<24} {','.join(verdicts):<28} "
              f"{','.join(map(str, counts)):<16} {median:.1f}{flag}")
    print("=" * w)


# ── Entry point ───────────────────────────────────────────────────────────

async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="run scenarios whose id contains this")
    parser.add_argument("--json", help="write full results to this path")
    parser.add_argument("--judge", action="store_true",
                        help="add an LLM groundedness/relevance rating")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run N times and report stability")
    args = parser.parse_args()

    scenarios = [s for s in SCENARIOS if not args.only or args.only in s.id]
    if not scenarios:
        print(f"No scenario matches {args.only!r}. "
              f"Available: {', '.join(s.id for s in SCENARIOS)}")
        return 2

    # The real tools, explicitly. The harness test suite pins these to stubs,
    # so being explicit here is what makes this an integration run rather than
    # an accidental replay of fixtures.
    registry.use_real()
    if not registry.is_real():
        print("ERROR: tool registry is not using real implementations.")
        return 2

    from src.data_gateway import get_gateway

    gateway = await get_gateway()

    runs: list[list[Outcome]] = []
    for run_index in range(args.repeat):
        if args.repeat > 1:
            print(f"\n>>> run {run_index + 1} of {args.repeat}")
        started = time.monotonic()
        outcomes = []
        for scenario in scenarios:
            print(f"    running {scenario.id} ...", flush=True)
            outcome = await run_scenario(scenario, gateway)
            if args.judge:
                outcome.judge = await judge_outcome(outcome)
            outcomes.append(outcome)
        elapsed = time.monotonic() - started
        runs.append(outcomes)
        if run_index == args.repeat - 1:
            print_report(outcomes, elapsed)

    if args.repeat > 1:
        print_stability(runs)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {"runs": [[asdict(o) for o in run] for run in runs]},
                fh, indent=2, ensure_ascii=False,
            )
        print(f"\n  full results written to {args.json}")

    # Exit non-zero on anything that would be wrong to demo.
    final = runs[-1]
    blocking = [o for o in final if o.verdict in
                ("CRASH", "SECURITY-FAIL", "MISROUTED", "BAD-CITES")]
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
