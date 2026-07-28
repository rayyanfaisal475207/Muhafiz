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
from src.pipeline.json_extract import extract_json

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
    response = await call_llm(
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
    )

    try:
        result = extract_json(response)
        if not isinstance(result, dict):
            raise ValueError(f"Router JSON was not an object: {response[:200]!r}")

        # Ensure default values if LLM misses them
        route = result.get("route", "RAG").upper()
        if route not in ["DIRECT", "RAG", "WEB", "SQL", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG"]:
            route = "RAG"

        # Case-scoped is the default; only XGRAPH/XAGG are ever cross-case.
        # A GRAPH/RAG/etc. route can never carry case_scope="cross_case" —
        # cross-case must go through XGRAPH/XAGG's structurally separate
        # path, never silently blended into a case-scoped answer.
        case_scope = str(result.get("case_scope", "within_case")).lower()
        if case_scope not in ["within_case", "cross_case"]:
            case_scope = "within_case"
        if route not in ["XGRAPH", "XAGG"]:
            case_scope = "within_case"

        output_format = result.get("output_format", "chat").lower()
        if output_format not in ["chat", "file_xlsx", "file_docx", "file_pdf"]:
            output_format = "chat"

        return {
            "route": route,
            "case_scope": case_scope,
            "target_entity": result.get("target_entity") or None,
            "output_format": output_format,
            "target_year": result.get("target_year", None),
            "confidence": result.get("confidence", "high").lower(),
            "reason": result.get("reason", "No reason provided")
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

