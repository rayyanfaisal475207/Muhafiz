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
from pathlib import Path

from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "router.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")


async def route_query(rewritten_query: str) -> dict:
    """
    Decide the route and output format for the query.

    Args:
        rewritten_query: The standalone query from the query rewriter.

    Returns:
        Dict: {"route": str, "output_format": str, "confidence": str, "reason": str}
    """
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
    )

    try:
        if result is None:
            raise ValueError(f"No valid JSON after retries. Raw response: {response[:200]!r}")

        # A syntactically valid classification the local model itself flags
        # as low-confidence is a different problem than the JSON-shape
        # failures call_llm_json already retries — the model DID produce a
        # real answer, it just isn't sure of it. Escalating on "medium" too
        # (not just "low") was confirmed more accurate live, but also the
        # single biggest consumer of Groq's free-tier quota across this
        # session — "medium" is common enough that it hit rate limits under
        # sustained load (confirmed live: 429 across all rotated keys),
        # which then blocks EVERY OTHER call_llm_json cloud fallback in the
        # pipeline too (evaluator, verifier, query rewriter), not just this
        # one. Scoped back to "low" only as the load/accuracy trade-off —
        # give Groq (dramatically more reliable at this exact classification
        # task in every comparison run live today) an independent second
        # opinion only when the local model is actually unsure, not merely
        # non-committal. Any failure escalating is non-fatal — the original
        # result is still usable and stays in place.
        if str(result.get("confidence", "")).strip().lower() == "low":
            try:
                escalated, _ = await call_llm_json(
                    system_prompt=_SYSTEM_PROMPT,
                    user_message=rewritten_query,
                    temperature=0.0,
                    max_tokens=800,
                    validate=lambda r: isinstance(r, dict) and "route" in r,
                    schema_hint='"route", "case_scope", "target_entity", "output_format", "target_year", "confidence", "reason"',
                    _call_llm=call_llm,
                    force_cloud=True,
                )
                if escalated is not None:
                    logger.info(
                        "Router: local classification was low-confidence (%s) — "
                        "using cloud second opinion instead: %s",
                        result.get("reason", "")[:60], escalated.get("route"),
                    )
                    result = escalated
            except Exception as exc:
                logger.warning("Router: low-confidence escalation to cloud failed: %s", exc)

        # str(...) every field before .upper()/.lower() — confirmed live:
        # a corrected retry can produce syntactically valid JSON with the
        # right field names but a wrong-typed value (e.g. "confidence": 0.8
        # as a float instead of "medium"/"low"), and .lower() on a float
        # crashed this whole function, discarding an otherwise-usable
        # "route" value along with it and falling all the way back to the
        # generic "failed to parse" RAG default instead of the real route.

        # Ensure default values if LLM misses them
        route = str(result.get("route") or "RAG").upper()
        if route not in ["DIRECT", "RAG", "WEB", "SQL", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG"]:
            route = "RAG"

        # Case-scoped is the default; only XGRAPH/XAGG are ever cross-case.
        # A GRAPH/RAG/etc. route can never carry case_scope="cross_case" —
        # cross-case must go through XGRAPH/XAGG's structurally separate
        # path, never silently blended into a case-scoped answer.
        case_scope = str(result.get("case_scope") or "within_case").lower()
        if case_scope not in ["within_case", "cross_case"]:
            case_scope = "within_case"
        if route not in ["XGRAPH", "XAGG"]:
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

