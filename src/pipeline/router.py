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

from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "router.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

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
    # Recurring-entity aggregates:
    # "recurring vehicles across cases", "top recurring vehicles",
    # "kitni gariyan bar bar cases mein aayi hain"
    re.compile(r"\brecurring\b.{0,25}\b(vehicles?|persons?|people)\b", re.IGNORECASE),
    re.compile(r"\btop recurring\b", re.IGNORECASE),
    re.compile(r"\b(vehicles?|persons?|people)\b.{0,30}\bacross\b.{0,20}\bcases\b", re.IGNORECASE),
    re.compile(r"bar\s*bar\b.{0,20}\bcases\b", re.IGNORECASE),
    # "which persons/suspects/vehicles appeared in multiple/more than one/
    # several cases" — live-caught gap: no "recurring"/"across" keyword,
    # so the patterns above miss it even though it's the exact same
    # top-recurring-nodes question.
    re.compile(
        r"\b(persons?|people|suspects?|accused|offenders?|vehicles?|mulzim|shakhs)\b"
        r".{0,40}\b(multiple|more than one|several|many)\s+cases?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(multiple|more than one|several|many)\s+cases?\b"
        r".{0,40}\b(persons?|people|suspects?|accused|offenders?|vehicles?|mulzim|shakhs)\b",
        re.IGNORECASE,
    ),
    # Station/category "most" aggregate: "which stations have the most open theft cases"
    re.compile(r"\bwhich (stations?|police stations?)\b.{0,50}\bmost\b", re.IGNORECASE),
    # Bare case-record listing/count
    re.compile(r"\blist of all cases\b", re.IGNORECASE),
    re.compile(r"\bhow many cases (are there|in total|exist)\b", re.IGNORECASE),
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


def _deterministic_route_override(query: str) -> dict | None:
    """Return a route dict for an unambiguous cross-case pattern, or None."""
    if _ACTIVE_CASE_RE.search(query):
        return None  # a named case anchors this within-case (GRAPH), not XAGG/XGRAPH/XNETWORK

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
            }
    for pat in _XGRAPH_OVERRIDE_PATTERNS:
        if pat.search(query):
            return {
                "route": "XGRAPH", "case_scope": "cross_case", "target_entity": None,
                "output_format": "chat", "target_year": None, "confidence": "high",
                "reason": "Deterministic override: unambiguous cross-case recurrence trigger language detected before the LLM call",
            }
    return None


async def route_query(rewritten_query: str) -> dict:
    """
    Decide the route and output format for the query.

    Args:
        rewritten_query: The standalone query from the query rewriter.

    Returns:
        Dict: {"route": str, "output_format": str, "confidence": str, "reason": str}
    """
    override = _deterministic_route_override(rewritten_query)
    if override is not None:
        return override

    # Also handles a distinct failure mode from truncated/malformed JSON:
    # Qwen3 sometimes ignores the "respond with ONLY JSON" instruction
    # entirely and answers conversationally instead — e.g. asking "Could
    # you clarify which cases you mean?" for a vague-but-answerable query
    # like "list of people mentioned in cases". That has no JSON to
    # extract at all, so a same-prompt retry just repeats it; call_llm_json
    # appends an explicit correction on retry that forbids exactly this.
    result, response = await call_llm_json(
        system_prompt=_SYSTEM_PROMPT,
        user_message=rewritten_query,
        temperature=0.0,
        # Qwen3-14B (the local reasoning model) emits a visible thinking
        # trace before its answer, and this server doesn't honor
        # enable_thinking=False/"/no_think" — the trace still consumes
        # max_tokens even though it never leaks into the JSON content.
        # 250 let the trace eat the whole budget and truncate the JSON
        # mid-"route"; 800 matches the client's own local-call ceiling
        # (src/llm/client.py) and leaves room for both.
        max_tokens=800,
        validate=lambda r: isinstance(r, dict) and "route" in r,
        schema_hint='"route", "case_scope", "target_entity", "output_format", "target_year", "confidence", "reason"',
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
        if route not in ["DIRECT", "RAG", "WEB", "SQL", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "XNETWORK"]:
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

        return {
            "route": route,
            "case_scope": case_scope,
            "target_entity": target_entity,
            "output_format": output_format,
            "target_year": target_year,
            "confidence": confidence,
            "reason": str(result.get("reason") or "No reason provided"),
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
            "reason": "Failed to parse router output, defaulting to RAG"
        }

