# ============================================================
# Router — LLM Call 2: Does This Query Need Retrieval?
#
# PURPOSE:
# Not every question requires searching the document store.
# "Hello! How are you?" doesn't need retrieval.
# "What is the bleeding risk of aspirin?" definitely does.
#
# Routing correctly has two benefits:
# 1. Speed: skipping retrieval makes conversational responses instant
# 2. Quality: retrieving documents for a general question can inject
#    irrelevant context that confuses the final response
#
# THE PROMPT STRATEGY (FEW-SHOT):
# The router prompt includes 10 example Q→YES/NO pairs.
# This "few-shot prompting" dramatically improves accuracy compared to
# just describing the rules in words. The examples serve as calibration
# data embedded directly in the prompt.
#
# OUTPUT FORMAT:
# Strictly JSON conforming to the schema in the prompt.
# ============================================================

import logging
import re
from pathlib import Path

from src.extraction.structured_fields import _CNIC_RE, _PHONE_RE, _PLATE_RE
from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "router.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_VALID_ROUTES = ["DIRECT", "RAG", "WEB", "SQL", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "XNETWORK"]

# ── Deterministic pre-classification for unambiguous cross-case aggregate/
# recurrence queries ─────────────────────────────────────────────────────
#
# Confirmed live (2026-08-04 audit): the LLM classifier (local Qwen3-14B,
# the "reasoning" role's local-first model) reliably defaults these to RAG
# — including the router prompt's OWN literal few-shot example verbatim —
# even after adding more few-shot examples and an explicit "do not default
# to RAG" instruction to prompts/router.txt. This isn't a JSON-formatting
# failure call_llm_json's retry/correction logic can catch: the model
# produces syntactically valid JSON each time, it's just the wrong route.
# Cloud escalation was considered and explicitly ruled out (cost/dependency
# reasons) — so for this specific, well-defined failure class, a
# deterministic regex pre-check runs BEFORE the LLM call and short-circuits
# straight to XAGG/XGRAPH when it matches, skipping the unreliable local
# classification step entirely (a bonus: also saves the LLM round-trip
# for these queries). Anything that doesn't match still goes through the
# LLM classifier unchanged below — this is a narrow safety net for the
# specific patterns confirmed failing live, not a general-purpose router
# replacement, and it must stay narrow: a query naming an active case
# (CASE-xxx/FIR-xxx) is deliberately excluded even if it matches one of
# these patterns, since "how many X in CASE-009" is a within-case GRAPH
# query (see router.txt), not a cross-case XAGG one.
_ACTIVE_CASE_RE = re.compile(r"\b(CASE|FIR)[-\s]?\d", re.IGNORECASE)

_XAGG_OVERRIDE_PATTERNS = [
    # Case status/category count aggregates:
    # "how many closed cases", "بند کیسز کی تعداد بتائیں", "band cases kitne hain"
    re.compile(r"\bhow many\b.{0,30}\bcases?\b", re.IGNORECASE),
    re.compile(r"\bcases?\b.{0,20}\bkitn[ei]\b", re.IGNORECASE),
    re.compile(r"\bkitn[ei]\b.{0,20}\bcases?\b", re.IGNORECASE),
    re.compile(r"کیسز?\s*کی\s*تعداد"),
    re.compile(r"کتن[ےی]\s*کیس"),
    # [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 2a] "How many police
    # stations are there" names neither "cases" nor any pattern above, so it
    # fell through to the LLM classifier, which (live-confirmed, Gold-QA
    # report §2.3) sent it to document search instead of an aggregate/count
    # route. A station-count question is exactly the same unambiguous count
    # shape the patterns above already fast-path for "cases" — extending
    # here, not inventing a new mechanism.
    re.compile(r"\bhow many\b.{0,20}\b(police )?stations?\b", re.IGNORECASE),
    re.compile(r"\bkitn[ei]\b.{0,20}\bthan[ea]y?\b", re.IGNORECASE),
    re.compile(r"تھانے?\s*کتن[ےی]"),
    re.compile(r"کتن[ےی]\s*تھانے?"),
    # Recurring-entity aggregates:
    # "recurring vehicles across cases", "top recurring vehicles",
    # "kitni gariyan bar bar cases mein aayi hain"
    #
    # [findings.md Module 4] "weapons?|firearms?|pistols?" added to all
    # three entity groups below alongside vehicles?/persons? — live gap:
    # "Which type of weapon appears most often across all cases?" named
    # no entity in this group, so this whole XAGG family never matched
    # and the query fell through to XGRAPH's own "across ... cases"
    # override (optional multiple/other group, fires on zero named
    # entities) instead, returning a nonsensical "no connections" answer.
    re.compile(r"\brecurring\b.{0,25}\b(vehicles?|persons?|people|weapons?|firearms?|pistols?)\b", re.IGNORECASE),
    re.compile(r"\btop recurring\b", re.IGNORECASE),
    re.compile(r"\b(vehicles?|persons?|people|weapons?|firearms?|pistols?)\b.{0,30}\bacross\b.{0,20}\bcases\b", re.IGNORECASE),
    re.compile(r"bar\s*bar\b.{0,20}\bcases\b", re.IGNORECASE),
    # "which persons/suspects/vehicles appeared in multiple/more than one/
    # several cases" — live-caught gap: no "recurring"/"across" keyword,
    # so the patterns above miss it even though it's the exact same
    # top-recurring-nodes question.
    re.compile(
        r"\b(persons?|people|suspects?|accused|offenders?|vehicles?|mulzim|shakhs|weapons?|firearms?|pistols?|ہتھیار)\b"
        r".{0,40}\b(multiple|more than one|several|many)\s+cases?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(multiple|more than one|several|many)\s+cases?\b"
        r".{0,40}\b(persons?|people|suspects?|accused|offenders?|vehicles?|mulzim|shakhs|weapons?|firearms?|pistols?|ہتھیار)\b",
        re.IGNORECASE,
    ),
    # Station/category "most" aggregate: "which stations have the most open theft cases"
    re.compile(r"\bwhich (stations?|police stations?)\b.{0,50}\bmost\b", re.IGNORECASE),
    # Bare case-record listing/count
    re.compile(r"\blist of all cases\b", re.IGNORECASE),
    re.compile(r"\bhow many cases (are there|in total|exist)\b", re.IGNORECASE),
    # [Gold-QA fix — Module 3, questions CR2/S3] Person-recurrence questions
    # phrased NARRATIVELY rather than with a "multiple/several cases" count
    # word (the shape the entity-group patterns above already cover). These
    # are the exact same top-recurring-Person aggregate ("who appears in
    # more than one case, by name?") — XAGG's `graph_recurrence` path
    # already answers them with each person named AND their specific case
    # set, which is precisely what the ground truth rewards. Without these
    # they fall through to the LLM classifier, which sends them to XGRAPH —
    # the correct route for a NAMED-seed traversal, but for a broad "is
    # anyone a repeat suspect" query with no named seed entity, XGRAPH (by
    # its own CCL-C2 safeguard in cross_case_linkage.py) deliberately
    # refuses to name a singular recurring entity and can only report a
    # flat case-ID union — never the names the ground truth actually asks
    # for. The recurrence belongs in XAGG; route it there deterministically.
    #
    # (a) English narrative recurrence: "resurfaced/reappears/shows up
    #     again as a suspect in a newer/separate/different case", "already
    #     on record ... in a(nother) case", "earlier case ... now newer/
    #     separate case". Requires a recurrence verb/phrase, not just any
    #     mention of "case", so it can't fire on an unrelated "separate
    #     case" mention.
    re.compile(
        r"\b(resurfac\w+|reappear\w+|reemerg\w+|re-emerg\w+|surfaced?\s+again|"
        r"shows?\s+up\s+again|again\b.{0,20}\b(?:suspect|accused)|"
        r"already\s+on\s+record|prior\s+(?:case|conviction|record)|"
        r"earlier\s+case\b.{0,40}\b(?:newer|separate|different|another))\b",
        re.IGNORECASE,
    ),
    # (b) "arrested/accused/charged more than once / again" — the person-
    #     centric recurrence count; the analogue of "more than one case"
    #     already handled by the entity-group patterns above, just phrased
    #     around the PERSON's recurrence rather than the case count.
    re.compile(
        r"\b(arrested|accused|charged|booked|caught|detained)\b.{0,30}"
        r"\b(more than once|again|multiple times|repeatedly|twice)\b",
        re.IGNORECASE,
    ),
    # (c) Urdu person-recurrence: "کیا کسی شخص کو ایک سے زیادہ بار گرفتار
    #     کیا گیا" (has any person been arrested more than once) and close
    #     variants — "ایک سے زیادہ بار" (more than once) plus a person/
    #     arrest token nearby. Also Roman-Urdu "aik se zyada bar ...
    #     giraftar".
    re.compile(r"ایک\s*سے\s*زیادہ\s*بار.{0,30}(گرفتار|ملزم|شخص|نامزد)"),
    re.compile(r"(گرفتار|ملزم|شخص|نامزد).{0,30}ایک\s*سے\s*زیادہ\s*بار"),
    re.compile(
        r"\baik\s*se\s*zyada\s*(?:bar|dafa|martaba)\b.{0,30}"
        r"\b(giraftar|mulzim|shakhs|namzad)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(giraftar|mulzim|shakhs|namzad)\b.{0,30}"
        r"\baik\s*se\s*zyada\s*(?:bar|dafa|martaba)\b",
        re.IGNORECASE,
    ),
]

_XGRAPH_OVERRIDE_PATTERNS = [
    re.compile(r"\bacross\b.{0,15}\b(multiple |other )?cases\b", re.IGNORECASE),
    re.compile(r"\b(other|another)\s+cases?\b", re.IGNORECASE),
    re.compile(r"\belsewhere\b", re.IGNORECASE),
    re.compile(r"\brepeat offender\b", re.IGNORECASE),
    re.compile(r"کسی\s*اور\s*کیس"),
    re.compile(r"دوسرے\s*کیسز"),
    re.compile(r"\bkisi\s*aur\s*case\b", re.IGNORECASE),
]

# XNETWORK (Section 2, GraphRAG-inspired layer) — added after live testing
# found the exact same failure class G-1 already documented, one route
# later: an XNETWORK-shaped query ("What's the overall picture on this
# network of associates across cases?") got swallowed by
# _XGRAPH_OVERRIDE_PATTERNS's own "across ... cases" pattern before the
# LLM/few-shot layer ever ran, and the Urdu/Roman-Urdu XNETWORK phrasings
# fell through to RAG at the LLM layer — the same local-model
# unreliable-on-novel-classification pattern G-1's own comment already
# describes, just for a third route now. The original Section 2 design
# explicitly avoided a regex override for XNETWORK, reasoning that its
# open-ended phrasing has no small enumerable trigger set the way XAGG/
# XGRAPH's countable/named-entity queries do — that reasoning holds for
# XNETWORK's SHARED vocabulary with XGRAPH ("network... across cases" is
# genuinely ambiguous with XGRAPH's own "map ORG-002's network across all
# cases" few-shot example, and no regex can tell "no entity named" from
# "entity named" reliably). What it does NOT hold for is XNETWORK's
# genuinely distinctive open-ended-synthesis phrasing ("overall picture",
# "pattern emerges") — that vocabulary essentially never co-occurs with a
# genuine XAGG/XGRAPH query, so intercepting on it specifically is safe
# without reopening the ambiguous "network...across" case regex tried and
# rejected here.
_XNETWORK_OVERRIDE_PATTERNS = [
    re.compile(r"\boverall\s+picture\b", re.IGNORECASE),
    re.compile(r"\b(overall|general)\s+pattern\b", re.IGNORECASE),
    re.compile(r"\bpattern\b.{0,20}\bemerge", re.IGNORECASE),
    re.compile(r"\bgive me a sense\b", re.IGNORECASE),
    re.compile(r"مجموعی\s*طور\s*پر"),
    re.compile(r"نمونہ.{0,20}سامنے"),
    # Roman-Urdu — narrower than the English patterns above since "overall"
    # alone is too generic a token to intercept on by itself; requires a
    # co-occurring synthesis/network word within a short span.
    re.compile(r"\boverall\b.{0,40}\b(connection|dikhta|pattern|network)\b", re.IGNORECASE),
]


# Narrow by design, matching router.txt's own SQL definition exactly:
# "which PENAL CODE SECTION applies to an offense, or whether an offense
# is cognizable." Not meant to catch every SQL-shaped phrasing — only the
# unambiguous ones, the same scope discipline the other three override
# lists already follow. A query about document content/procedures (RAG's
# job) doesn't use this vocabulary.
_SQL_OVERRIDE_PATTERNS = [
    re.compile(r"\b(ppc|penal code)\s+section\b", re.IGNORECASE),
    re.compile(r"\bsection\b.{0,20}\b(ppc|penal code)\b", re.IGNORECASE),
    re.compile(r"\b(what|which) section\b.{0,30}\b(covers?|applies|applicable)\b", re.IGNORECASE),
    re.compile(r"\bcognizable\s+offen[cs]e\b", re.IGNORECASE),
    re.compile(r"\bis\b.{0,40}\b(a\s+)?cognizable\b", re.IGNORECASE),
]

# [findings.md Module 7] A genuinely compound question can still match one
# of the patterns above purely because of its SQL HALF — "...and what PPC
# section covers illegal possession of an unlicensed firearm?" is this
# module's own live-tested example, and it hits the first pattern above
# just from that clause. The override's whole justification (see the
# comment above _SQL_OVERRIDE_PATTERNS) is that it's MORE reliable than
# the LLM for an unambiguous, SINGLE-intent SQL lookup — that argument
# doesn't hold once a second, different need is also in the sentence,
# because only the LLM call can populate secondary_methods; this
# deterministic fast-path never does (confirmed live: without this guard,
# the exact example above short-circuited straight to a SQL-only route,
# silently dropping the compound half every time, regardless of what
# route_query()'s new secondary_methods support could otherwise catch).
# Narrow and conservative on purpose: only skips the override when the
# query ALSO names a case-specific "this X" AND uses "and" somewhere (in
# either order — "what is THIS WEAPON's condition, AND what section..."
# names the case-specific half first; other real phrasings name it
# second) — an ordinary single-intent SQL lookup (the overwhelming
# majority of queries these patterns match) has no such language and is
# completely unaffected, still taking the fast, reliable override path.
_SQL_OVERRIDE_COMPOUND_THIS_X_RE = re.compile(
    r"\bthis (case|weapon|vehicle|accused|suspect|person|firearm)\b", re.IGNORECASE,
)


# [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 2b] A distinct shape from
# _SQL_OVERRIDE_PATTERNS above: "what does Section 154 CrPC say" (or
# "explain Section N", "text of Section N") asks for the CONTENT of a
# statute — a document/RAG question — not "which section applies to this
# offense" (SQL's job, answered from police_reference_data, not document
# text). These two shapes share the word "section" but need opposite
# routes; keeping them as separate pattern lists (rather than widening
# _SQL_OVERRIDE_PATTERNS) keeps that distinction explicit and auditable.
# This question will still come back "no relevant documents" until the
# legal knowledge base is loaded (see ROOT_CAUSE_AND_FIXES.md Module 5) —
# the defect this fixes is specifically the misroute to a route that
# structurally cannot ever answer it (SQL has no legal text at all),
# independent of whether the KB is loaded.
_RAG_LEGAL_TEXT_OVERRIDE_PATTERNS = [
    re.compile(r"\b(what does|explain|text of|meaning of)\b.{0,20}\bsection\b.{0,10}\d+", re.IGNORECASE),
    re.compile(r"\bsection\b.{0,10}\d+.{0,20}\b(say|says|state|states|mean|means)\b", re.IGNORECASE),
    re.compile(r"دفعہ\s*\d+.{0,20}(کیا کہتی ہے|کا متن|کیا بیان کرتی ہے)"),
]


def _sql_override_has_compound_signal(query: str) -> bool:
    return bool(re.search(r"\band\b", query, re.IGNORECASE)) and bool(
        _SQL_OVERRIDE_COMPOUND_THIS_X_RE.search(query)
    )


def _structured_identifier_in(query: str) -> str | None:
    """
    First CNIC/phone/plate match in `query`, or None — the same three
    regexes graph_retriever._seed_candidates() already trusts for exactly
    this purpose (imported, not re-copied). Used by the XGRAPH
    deterministic override below to populate target_entity when the
    query itself names an unambiguous instance, instead of always
    discarding it as None (bug fix — see this function's call site for
    the full story: without this, XGRAPH's fast deterministic path could
    never seed a graph traversal at all, guaranteeing a false "no
    connections found" for exactly the kind of query it exists to
    answer).
    """
    for regex in (_CNIC_RE, _PHONE_RE, _PLATE_RE):
        m = regex.search(query)
        if m:
            return m.group(0)
    return None


def _deterministic_route_override(query: str, case_id: str | None = None) -> dict | None:
    """
    Return a route dict for an unambiguous cross-case pattern, or None.

    `case_id` — [bug fix, found via a live full-route sweep] an ACTIVE
    case on the request is a stronger, structural "this is within-case"
    signal than _ACTIVE_CASE_RE's own regex over the query TEXT below,
    which only catches a literal case number typed in the message
    ("CASE-009"). A user already inside a case-scoped chat naturally
    says "this case"/"the case" instead — that phrasing has no literal
    case number for _ACTIVE_CASE_RE to match, so it used to fall through
    to the XAGG/XGRAPH/XNETWORK override lists below and could misfire
    (confirmed live: "how many accused are involved in this case" —
    router.txt's own verbatim GRAPH few-shot example, just with "this
    case" instead of "CASE-009" — matched XAGG's `"how many...cases?"`
    pattern purely on the word "case" appearing nearby, answering with
    unrelated cross-case counts instead of the case's own accused).
    When `case_id` is given, skip straight past all three cross-case
    override blocks — same effective short-circuit _ACTIVE_CASE_RE
    already does for the text-based signal, just triggered by the
    request's own case context instead. SQL's override stays
    unconditional either way — see its own comment below for why it's
    orthogonal to case context by design.
    """
    # SQL checked absolute first, ahead of even the active-case exclusion
    # below — a penal-code/cognizability lookup is orthogonal to whether a
    # case happens to be named in the same sentence ("what section applies
    # to the offense in CASE-009?" is still SQL, not GRAPH). Added after
    # live-testing found this is the SAME failure class as G-1/XNETWORK's
    # own override, one route later still: "What PPC section covers
    # mobile phone theft?" and "Is cyber harassment a cognizable offense?"
    # — both this file's OWN few-shot examples, verbatim — misrouted to
    # RAG live, repeatedly, across independent test runs (not a one-off:
    # confirmed on a completely separate live pipeline run after the
    # G-1-style JSON-validation bug was already fixed, so this is a
    # genuine classification-reliability gap in the local model for this
    # prompt shape, not the malformed-JSON bug found earlier). The answers
    # happened to still be correct via RAG's fallback in THIS corpus only
    # because the ingested FIR documents happen to restate the same PPC
    # section numbers — a different corpus without that overlap would
    # simply fail. SQL's own trigger vocabulary ("PPC section", "cognizable
    # offense") never co-occurs with XAGG/XGRAPH/XNETWORK's cross-case
    # vocabulary, so there's no tie-breaking concern checking it first.
    for pat in _SQL_OVERRIDE_PATTERNS:
        if pat.search(query) and not _sql_override_has_compound_signal(query):
            return {
                "route": "SQL", "case_scope": "within_case", "target_entity": None,
                "output_format": "chat", "target_year": None, "confidence": "high",
                "reason": "Deterministic override: unambiguous structured penal-code/cognizability lookup trigger language detected before the LLM call",
                "station": None, "district": None,
            }

    # [Gold-QA fix — Module 2b] Checked immediately after SQL, ahead of the
    # active-case exclusion below, for the same reason SQL is: a "what does
    # Section N say" legal-text question is orthogonal to whether a case
    # happens to be named in the same sentence.
    for pat in _RAG_LEGAL_TEXT_OVERRIDE_PATTERNS:
        if pat.search(query):
            return {
                "route": "RAG", "case_scope": "within_case", "target_entity": None,
                "output_format": "chat", "target_year": None, "confidence": "high",
                "reason": "Deterministic override: unambiguous legal-text-content lookup trigger language detected before the LLM call",
                "station": None, "district": None,
            }

    if case_id or _ACTIVE_CASE_RE.search(query):
        return None  # an active case (by request context OR a literal case number in the text) anchors this within-case (GRAPH), not XAGG/XGRAPH/XNETWORK

    # XNETWORK checked first: its patterns are deliberately narrow/distinct
    # open-ended-synthesis phrasing (see comment above _XNETWORK_OVERRIDE_
    # PATTERNS) that doesn't overlap with XAGG's/XGRAPH's own trigger
    # vocabulary, so there's no real tie-breaking concern checking it
    # before them — unlike XAGG vs. XGRAPH below, which genuinely do share
    # vocabulary ("across cases") and need an explicit precedence rule.
    for pat in _XNETWORK_OVERRIDE_PATTERNS:
        if pat.search(query):
            return {
                "route": "XNETWORK", "case_scope": "cross_case", "target_entity": None,
                "output_format": "chat", "target_year": None, "confidence": "high",
                "reason": "Deterministic override: unambiguous open-ended cross-case network/pattern trigger language detected before the LLM call",
                "station": None, "district": None,
            }

    # XAGG checked next: "recurring vehicles across cases" matches both an
    # XAGG pattern (recurring-entity aggregate) and the XGRAPH "across ...
    # cases" pattern, and per router.txt's own examples ("top recurring
    # vehicles across all cases") that shape is XAGG's aggregate/count job,
    # not XGRAPH's single-entity network-mapping job — XAGG must win the tie.
    for pat in _XAGG_OVERRIDE_PATTERNS:
        if pat.search(query):
            return {
                "route": "XAGG", "case_scope": "cross_case", "target_entity": None,
                "output_format": "chat", "target_year": None, "confidence": "high",
                "reason": "Deterministic override: unambiguous cross-case aggregate/count trigger language detected before the LLM call",
                "station": None, "district": None,
            }
    for pat in _XGRAPH_OVERRIDE_PATTERNS:
        if pat.search(query):
            # Bug fix: per router.txt's own XGRAPH definition, target_entity
            # must be the named instance "extracted verbatim" when the query
            # names one (a CNIC/phone/plate here — the only kind this
            # override can safely recognize without risking a wrong guess;
            # see _structured_identifier_in()'s own docstring) — null is
            # only correct for the recurrence/enumeration-with-no-named-
            # instance case ("has any phone number recurred?"). This
            # override used to hardcode None unconditionally, silently
            # discarding an identifier sitting right in the text and
            # guaranteeing _seed_candidates() had nothing to seed a
            # traversal from — confirmed live: "no connections found" for
            # an entity that demonstrably does recur, every time.
            return {
                "route": "XGRAPH", "case_scope": "cross_case",
                "target_entity": _structured_identifier_in(query),
                "output_format": "chat", "target_year": None, "confidence": "high",
                "reason": "Deterministic override: unambiguous cross-case recurrence trigger language detected before the LLM call",
                "station": None, "district": None,
            }
    return None


async def route_query(rewritten_query: str, case_id: str | None = None) -> dict:
    """
    Decide the route and output format for the query.

    Args:
        rewritten_query: The standalone query from the query rewriter.
        case_id: The case active on this request, if any — see
            _deterministic_route_override()'s own docstring for why this
            (not just a literal case number in the text) must also
            suppress the cross-case deterministic overrides.

    Returns:
        Dict: {"route": str, "output_format": str, "confidence": str, "reason": str}
    """
    override = _deterministic_route_override(rewritten_query, case_id=case_id)
    if override is not None:
        return override

    # [Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Root Cause 1, the actual
    # upstream fix] The LLM classifier previously had NO signal at all for
    # whether a case is actually selected — it classified from the query
    # TEXT alone, so a within-case-SHAPED question ("this weapon," "the
    # accused") asked with no case active (e.g. "All Cases" mode) could
    # still land on GRAPH/GRAPH_HYBRID, which then finds nothing to
    # summarize (live-confirmed misroute, Gold-QA report CR3/CR4/G2/G3/G5/
    # G6 — see router.txt's own "ACTIVE_CASE context" section for the full
    # rule and worked examples). Only the "no case" shape changes the
    # prompt at all — when `case_id` is set, `user_message` stays exactly
    # `rewritten_query`, unchanged, so the (heavily tuned, already-tested)
    # active-case behavior is untouched.
    llm_user_message = (
        rewritten_query if case_id else
        "ACTIVE_CASE: none (no case is currently selected — the query below "
        f"is being asked without any specific case active)\n\nQUESTION: {rewritten_query}"
    )

    # Also handles a distinct failure mode from truncated/malformed JSON:
    # Qwen3 sometimes ignores the "respond with ONLY JSON" instruction
    # entirely and answers conversationally instead — e.g. asking "Could
    # you clarify which cases you mean?" for a vague-but-answerable query
    # like "list of people mentioned in cases". That has no JSON to
    # extract at all, so a same-prompt retry just repeats it; call_llm_json
    # appends an explicit correction on retry that forbids exactly this.
    result, response = await call_llm_json(
        system_prompt=_SYSTEM_PROMPT,
        user_message=llm_user_message,
        temperature=0.0,
        # Qwen3-14B (the local reasoning model) emits a visible thinking
        # trace before its answer, and this server doesn't honor
        # enable_thinking=False/"/no_think" — the trace still consumes
        # max_tokens even though it never leaks into the JSON content.
        # 250 let the trace eat the whole budget and truncate the JSON
        # mid-"route"; 800 still weren't enough on live re-measurement
        # (confirmed: a 50-token probe burned its entire budget on the
        # thinking trace alone without reaching the answer) — raised to
        # 2000, LOCAL-ONLY. Does not affect the cloud budget below, which
        # stays independently tuned at cloud_max_tokens=300 for a
        # different, already-solved reason (see that comment).
        max_tokens=2000,
        # [Module 2 follow-up, findings.md] cloud_max_tokens + reasoning_effort:
        # without cloud_max_tokens, the cloud escalation branch (call_llm's
        # force_cloud=True attempt) inherited the same 800-token ceiling
        # max_tokens sets for LOCAL, needed there only to out-wait
        # Qwen3-14B's local thinking trace (see above) — but with max_tokens
        # counted as part of Groq's own TPM accounting, that unnecessary
        # local-sized 800-token reservation on top of this ~7650-token
        # system prompt pushed a real request to 8451 tokens against this
        # account's 8000 TPM on_demand cap: a 413 on EVERY case-scoped
        # query that ever needed the cloud fallback, before route_query()
        # could return anything at all. Confirmed live: cloud_max_tokens=300
        # alone cleared the TPM cap but came back EMPTY — config.GROQ_MODEL
        # (openai/gpt-oss-120b) is itself a reasoning model and was spending
        # the entire 300-token budget on its own hidden reasoning trace
        # before ever emitting the JSON, the exact same failure mode as
        # Qwen3-14B's, just never triggered on the cloud side before because
        # nothing had ever given it a small enough budget to expose it.
        # reasoning_effort="low" (see _call_groq()'s own docstring) fixed
        # that: a real router call's reasoning_tokens dropped to 82,
        # completion_tokens 151 total — comfortably inside 300 — and total
        # request tokens landed at 7802, under the 8000 cap, still returning
        # the correct classification.
        cloud_max_tokens=300,
        reasoning_effort="low",
        # isinstance(r.get("route"), str), not just "route" in r — live-
        # confirmed (found while auditing for more issues after Section 2):
        # a corrected retry can produce syntactically valid JSON with a
        # "route" KEY present but a malformed VALUE, e.g.
        # {"route": {"confidence": 0.1, "path": []}} — a nested object
        # instead of a route string. The old check only verified the key
        # existed, so this passed validation, then silently defaulted to
        # RAG deep in this function's own str(result.get("route") or
        # "RAG").upper() fallback below — no warning logged, no retry
        # triggered, completely invisible. This is the exact same class of
        # bug already found and fixed in community_summarization.py's
        # "summary" field (a nested dict instead of a string) — router.py
        # never got the equivalent fix. Confirmed this was live-active and
        # not a one-off: 100% reproducible across DIRECT/SQL/WEB/GRAPH/
        # GRAPH_HYBRID-shaped queries this session, none of which have a
        # deterministic regex override (only XAGG/XGRAPH/XNETWORK do) — so
        # this silently degraded a large fraction of the router's total
        # surface to a blind RAG default.
        #
        # Also checks membership in _VALID_ROUTES, not just "is a non-
        # empty string" — live-confirmed the type-only check above still
        # wasn't enough: a corrected retry reliably produces
        # {"route": "unknown", ...} for straightforward queries (including
        # "Hello", the router prompt's own first few-shot example
        # verbatim) — a syntactically fine, non-empty string that still
        # isn't a real route. That passed the type check, so no further
        # retry ever fired, and it silently defaulted to RAG via the same
        # downstream str(...).upper() not in _VALID_ROUTES fallback below.
        # Rejecting it here means the local model gets its full 3
        # attempts before falling through to the cloud escalation already
        # configured below, instead of quietly giving up after 2.
        validate=lambda r: (
            isinstance(r, dict)
            and isinstance(r.get("route"), str)
            and r["route"].strip().upper() in _VALID_ROUTES
        ),
        schema_hint='"route", "case_scope", "target_entity", "output_format", "target_year", "confidence", "reason", "secondary_methods"',
        _call_llm=call_llm,
        # Router-specific opt-in (unlike evaluator/query_rewriter, which do
        # NOT set this — see call_llm_json's own docstring for why blanket
        # escalation was reverted elsewhere: it was the single biggest
        # consumer of Groq's free-tier quota under sustained testing).
        # Router differs in call shape: it fires once per turn, not once per
        # retry-loop iteration, so the quota exposure here is bounded very
        # differently. This only engages after 3 local attempts have all
        # failed to produce ANY valid JSON (confirmed live: exactly the
        # failure mode that currently causes route_query() to silently fall
        # back to a low-confidence RAG default below) — it does not engage
        # when local returns syntactically valid JSON with a questionable
        # classification, since that's a calibration problem the few-shot
        # examples in router.txt address, not a cloud-escalation problem.
        escalate_to_cloud_on_failure=True,
    )

    try:
        if result is None:
            raise ValueError(f"No valid JSON after retries. Raw response: {response[:200]!r}")

        # No cloud escalation on low confidence, by design: cloud is reserved
        # for genuine local unavailability (handled inside call_llm() itself
        # via its own local-first/cloud-on-exception logic, independent of
        # anything here), not for local content-quality/confidence issues.
        # Confirmed live: proactively escalating on confidence alone was the
        # single biggest consumer of Groq's free-tier quota under sustained
        # testing, exhausting it across all rotated keys — and once
        # exhausted, that ALSO blocks the genuinely important escalations
        # elsewhere (e.g. RAG's refusal-regeneration). call_llm_json's own
        # increased local-attempt budget (3, up from 2) is the retry lever
        # now instead.

        # str(...) every field before .upper()/.lower() — confirmed live:
        # a corrected retry can produce syntactically valid JSON with the
        # right field names but a wrong-typed value (e.g. "confidence": 0.8
        # as a float instead of "medium"/"low"), and .lower() on a float
        # crashed this whole function, discarding an otherwise-usable
        # "route" value along with it and falling all the way back to the
        # generic "failed to parse" RAG default instead of the real route.

        # Ensure default values if LLM misses them
        route = str(result.get("route") or "RAG").upper()
        if route not in _VALID_ROUTES:
            route = "RAG"

        # Case-scoped is the default; only XGRAPH/XAGG/XNETWORK are ever
        # cross-case. A GRAPH/RAG/etc. route can never carry
        # case_scope="cross_case" — cross-case must go through one of
        # these three structurally separate paths, never silently blended
        # into a case-scoped answer.
        case_scope = str(result.get("case_scope") or "within_case").lower()
        if case_scope not in ["within_case", "cross_case"]:
            case_scope = "within_case"
        if route not in ["XGRAPH", "XAGG", "XNETWORK"]:
            case_scope = "within_case"

        output_format = str(result.get("output_format") or "chat").lower()
        if output_format not in ["chat", "file_xlsx", "file_docx", "file_pdf"]:
            output_format = "chat"

        target_year = result.get("target_year")
        if not isinstance(target_year, int):
            target_year = None

        confidence = str(result.get("confidence") or "high").lower()
        if confidence not in ["high", "medium", "low"]:
            confidence = "medium"

        target_entity = result.get("target_entity") or None
        if target_entity is not None and not isinstance(target_entity, str):
            target_entity = str(target_entity)

        # [findings.md Module 7] Adaptive multi-method retrieval: an
        # OPTIONAL, additive signal alongside the single `route` this
        # function has always returned — the router may also name other
        # retrieval methods a genuinely compound question needs alongside
        # its primary route (e.g. "what is this weapon's condition, AND
        # what PPC section covers unlicensed possession?" → route GRAPH,
        # secondary_methods ["SQL"]). Deliberately conservative: only ever
        # honored downstream (orchestrator.py) for a within-case primary
        # route (SQL/GRAPH/GRAPH_HYBRID) — XGRAPH/XAGG/XNETWORK's own
        # structurally-separate, never-blended cross-case contract is
        # untouched by this field regardless of what's returned here, so
        # a router misclassification of this new field can't leak into
        # those three routes. Missing/malformed input (not a list, not
        # strings, self-referential, or an unrecognized route name) all
        # collapse to the same safe default: [] — identical to this
        # field never having existed, which is what every pre-existing
        # query (no compound "and" clause for the LLM to detect) will
        # still produce.
        secondary_methods_raw = result.get("secondary_methods")
        secondary_methods: list[str] = []
        if isinstance(secondary_methods_raw, list):
            for m in secondary_methods_raw:
                if not isinstance(m, str):
                    continue
                m_upper = m.strip().upper()
                if m_upper in ("SQL", "GRAPH", "XGRAPH", "XAGG") and m_upper != route:
                    secondary_methods.append(m_upper)
        # Cap at 2 — bounds the extra retrieval cost of any one query and
        # matches every real compound-need shape found in this module's
        # own live mini-sweep (findings.md), all of which needed exactly
        # one method beyond the primary route.
        secondary_methods = secondary_methods[:2]

        # [Milestone E1 — GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md] station/
        # district — extends this SAME single classification call, per E1's
        # own resolved open point (does not add a second classification
        # gate: this is the identical route_query() JSON, not a separate
        # LLM call or a separate parsing pass). Free text as extracted by
        # the router (a station/district name in the query), resolved to a
        # real graph node id downstream by
        # graph_retriever.resolve_jurisdiction_case_ids() — never trusted
        # as an id here. Only meaningful for the three cross-case routes
        # (jurisdiction-scoped narrowing only applies before cross-case
        # work runs — a within-case query's jurisdiction is already fixed
        # by its one active case), same "case_scope only ever cross-case
        # for XGRAPH/XAGG/XNETWORK" discipline already applied above —
        # forced to None for every other route so a stray value can never
        # leak into a within-case path.
        station = result.get("station") or None
        if station is not None and not isinstance(station, str):
            station = str(station)
        district = result.get("district") or None
        if district is not None and not isinstance(district, str):
            district = str(district)
        if route not in ["XGRAPH", "XAGG", "XNETWORK"]:
            station = None
            district = None

        return {
            "route": route,
            "case_scope": case_scope,
            "target_entity": target_entity,
            "output_format": output_format,
            "target_year": target_year,
            "confidence": confidence,
            "reason": str(result.get("reason") or "No reason provided"),
            "station": station,
            "district": district,
            "secondary_methods": secondary_methods,
        }
    except Exception as e:
        logger.error("Router failed to parse JSON: %s. Raw response: %s", e, response)
        return {
            "route": "RAG",
            "case_scope": "within_case",
            "target_entity": None,
            "output_format": "chat",
            "target_year": None,
            "confidence": "low",
            "reason": "Failed to parse router output, defaulting to RAG",
            "station": None,
            "district": None,
            "secondary_methods": [],
        }

