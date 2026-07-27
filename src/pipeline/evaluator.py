# ============================================================
# Relevance Evaluator — LLM Call 3
#
# PURPOSE:
# After retrieving and re-ranking documents, we don't blindly pass them
# to the final response LLM. First, we check: do these documents actually
# contain what the user asked about?
#
# This prevents "hallucination by assumption" — the response LLM trying
# to answer from insufficient context and making things up.
# If the documents aren't relevant, we retry with a better query instead.
#
# OUTPUT FORMAT:
# The evaluator returns JSON:
#   {"relevant": true/false, "reason": "explanation"}
#
# The "reason" is the key to the retry mechanism. It's specific feedback
# about what information was missing — this gets passed to the retry
# rewriter to generate a more targeted query.
#
# ERROR HANDLING:
# LLMs occasionally return malformed JSON (especially cheaper models).
# We wrap json.loads() in try/except and retry the call once if parsing fails.
# After two failed parse attempts, we default to {"relevant": false} to
# trigger the retry loop rather than crashing.
# ============================================================

import json
import logging
import re
from pathlib import Path

from src.llm.client import call_llm

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "evaluator.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")


def _extract_json(response: str) -> str:
    """
    Pull the {...} substring out of a raw LLM response before parsing —
    see src/pipeline/verifier.py's _extract_json for why a bare
    json.loads(raw.strip()) is unsafe against Qwen3-14B's thinking-trace
    preamble (router.py already guards against this; this call site
    previously did not).
    """
    match = re.search(r"\{.*\}", response, re.DOTALL)
    return match.group(0) if match else response.strip()


async def evaluate_relevance(
    original_query: str,
    rewritten_query: str,
    retrieved_chunks: list[dict],
) -> dict:
    """
    Judge whether retrieved document chunks are sufficient to answer the query.

    LLM Call 3 in the pipeline.

    Args:
        original_query:   The user's original message (unmodified).
        rewritten_query:  The standalone query from the rewriter.
        retrieved_chunks: The top-k chunks from retrieval + re-ranking.
                          Each dict should have "text" and "metadata" keys.

    Returns:
        Dict with keys:
            "relevant" (bool): True if documents are sufficient.
            "reason"  (str):   Why the documents are/aren't sufficient.
    """
    if not retrieved_chunks:
        logger.warning("Evaluator received empty chunk list — returning not relevant.")
        return {
            "relevant": False,
            "reason": "No documents were retrieved. The knowledge base may not contain relevant information.",
        }

    # Format the retrieved chunks for the evaluator prompt
    documents_text = _format_chunks_for_prompt(retrieved_chunks)

    user_input = (
        f"User's original question: {original_query}\n\n"
        f"Search query used: {rewritten_query}\n\n"
        f"Retrieved documents:\n{documents_text}"
    )

    # Try up to 2 times to get valid JSON from the evaluator
    for attempt in range(2):
        raw_response = await call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_input,
            temperature=0.0,
            # Qwen3-14B's thinking trace consumes max_tokens before its JSON
            # answer, and this server can't be told to skip it — 200 was
            # truncating to an empty response every time. 800 itself was
            # later confirmed (in verifier.py, the identical call shape)
            # still too tight for a real multi-chunk prompt, cutting the
            # JSON off mid-string rather than just squeezing the thinking
            # trace — 2000 gives real headroom for both.
            max_tokens=2000,
        )

        try:
            result = json.loads(_extract_json(raw_response))
            # Validate expected keys exist
            if "relevant" in result and "reason" in result:
                logger.info(
                    "Evaluator: relevant=%s — %s",
                    result["relevant"], result["reason"][:80]
                )
                return result
            else:
                logger.warning(
                    "Evaluator JSON missing expected keys (attempt %d): %s",
                    attempt + 1, raw_response[:100]
                )
        except json.JSONDecodeError as exc:
            logger.warning(
                "Evaluator returned invalid JSON (attempt %d): %s — %s",
                attempt + 1, exc, raw_response[:100]
            )

    # All attempts failed — default to "not relevant" so the retry loop kicks in
    logger.error("Evaluator failed to return valid JSON after 2 attempts. Defaulting to not relevant.")
    return {
        "relevant": False,
        "reason": "Could not parse evaluator response. Retrying with a different query.",
    }


def _format_chunks_for_prompt(chunks: list[dict]) -> str:
    """
    Format retrieved chunks as readable text for the evaluator prompt.

    Example output:
        [1] Source: aspirin-guide.pdf (page 3)
        Text: Aspirin may increase bleeding risk in patients...

        [2] Source: drug-interactions.pdf (page 7)
        Text: When combined with warfarin, aspirin significantly...
    """
    lines: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        # Reranked chunks carry the source under metadata.source; older
        # formats used a top-level source_file key. Support both — the old
        # code showed "Source: unknown" for every chunk.
        source = chunk.get("source_file") or chunk.get("metadata", {}).get("source") or "unknown"
        text = chunk.get("chunk_text", chunk.get("text", ""))
        
        lines.append(f"[{i}] Source: {source}")
        lines.append(f"Text: {text}")
        lines.append("")  # blank line between chunks

    return "\n".join(lines)
