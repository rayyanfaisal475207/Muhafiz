# ============================================================
# Query Rewriter — LLM Call 1
#
# PURPOSE:
# Users often ask follow-up questions that reference previous messages.
# "What about the side effects?" is meaningless without knowing we've
# been talking about aspirin. The query rewriter resolves these references
# by using the conversation history to make the query self-contained.
#
# This is critical for retrieval quality: ChromaDB doesn't know about
# the conversation, it only sees the query text. If you pass in
# "What about the side effects?", ChromaDB will search for "side effects"
# of nothing in particular — and return irrelevant results.
#
# After rewriting: "What are the side effects of aspirin?" — precise search.
#
# RETRY MODE:
# The same rewriter runs again during the retry loop, but with different
# input: instead of just conversation history, it also gets the evaluator's
# feedback about why the previous retrieval failed.
# This produces a more targeted query for the second retrieval attempt.
# ============================================================

import logging
from pathlib import Path

from src.llm.client import call_llm

logger = logging.getLogger(__name__)

# Load the prompt template once at module import time.
# Prompts live in files so they can be tuned without touching Python code.
_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "query_rewriter.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")


async def rewrite_query(
    user_message: str,
    conversation_history: list[dict],
) -> str:
    """
    Rewrite the user's message as a standalone search query.

    LLM Call 1 in the pipeline.

    Args:
        user_message:          The user's latest message (possibly a follow-up).
        conversation_history:  Previous messages in this session.

    Returns:
        A self-contained query string suitable for vector search.
        If history is empty or the message is already standalone, returns
        the original message unchanged (the prompt instructs the LLM to do this).
    """
    # Edge case: no history — the query is already standalone
    if not conversation_history:
        logger.debug("No history — returning original query unchanged.")
        return user_message.strip()

    # Format history for the prompt
    history_text = _format_history(conversation_history)

    user_input = (
        f"Conversation history:\n{history_text}\n\n"
        f"Latest message: {user_message}"
    )

    rewritten = await call_llm(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_input,
        temperature=0.0,   # Deterministic: same input should always produce same rewrite
        # Qwen3-14B's thinking trace consumes max_tokens before its one-line
        # answer, and this server ignores enable_thinking=False — 200 was
        # truncating to an empty response every time (esp. on Urdu/Roman-Urdu
        # follow-ups, silently masked by the empty-string fallback below).
        max_tokens=800,
    )

    rewritten = _sanitize_rewrite(rewritten, user_message)

    logger.info("Query rewritten: '%s' -> '%s'", user_message[:50], rewritten[:80])
    return rewritten


def _sanitize_rewrite(rewritten: str, original: str) -> str:
    """
    Guard against common LLM failure modes: empty output, preambles,
    wrapping quotes, multi-line answers, or answering instead of rewriting.
    Falls back to the original message when the output is unusable.
    """
    text = (rewritten or "").strip()
    if not text:
        logger.warning("Query rewriter returned empty string. Using original message.")
        return original.strip()

    # Strip label-style preambles the model sometimes adds despite instructions
    for prefix in ("output:", "rewritten query:", "query:", "rewritten:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()

    # Take the first non-empty line — a rewrite is always a single line
    text = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    text = text.strip('"').strip("'").strip()

    # A "rewrite" several times longer than a reasonable query is almost
    # certainly the model answering the question. Fall back.
    if not text or len(text) > 400:
        logger.warning("Query rewriter output unusable (len=%d). Using original message.", len(text))
        return original.strip()

    return text


async def rewrite_for_retry(
    original_message: str,
    previous_query: str,
    evaluator_feedback: str,
) -> str:
    """
    Rewrite the query specifically to address evaluator feedback.

    This is called when the relevance evaluator returns {"relevant": false}.
    The evaluator provides a reason for failure (e.g., "documents discuss X
    but not the specific aspect Y the user asked about"). This function uses
    that feedback to craft a better retrieval query.

    Args:
        original_message:    The user's original message (unchanged).
        previous_query:      The query that failed to retrieve relevant docs.
        evaluator_feedback:  The evaluator's explanation of what was missing.

    Returns:
        An improved query string targeting what the evaluator said was missing.
    """
    retry_prompt = (
        "You are a search query optimizer for a police reference document database. "
        "A previous search query failed to retrieve relevant documents. Use the "
        "feedback below to write ONE better search query.\n\n"
        "Output ONLY the improved query on a single line. No explanation. No quotes. "
        "Never answer the question itself.\n\n"
        "Rules:\n"
        "- Target specifically what the feedback says is missing.\n"
        "- Use DIFFERENT keywords than the previous query: swap plain language for "
        "procedural/statutory terms (or vice versa), e.g. 'FIR copy' <-> 'certified copy of First Information Report', "
        "'PPC section' <-> 'penal code provision'.\n"
        "- Add the likely PPC/PECA section number if the feedback hints at one; "
        "drop section numbers that already failed.\n"
        "- Keep it focused: one topic, under 25 words."
    )

    user_input = (
        f"Original user question: {original_message}\n\n"
        f"Previous search query (which failed): {previous_query}\n\n"
        f"Why it failed (evaluator feedback): {evaluator_feedback}\n\n"
        f"Write an improved search query:"
    )

    improved = await call_llm(
        system_prompt=retry_prompt,
        user_message=user_input,
        temperature=0.2,  # Slight creativity to try different keywords
        # See rewrite_query() above — Qwen3-14B's thinking trace needs room
        # inside max_tokens on this server, which can't be told to skip it.
        max_tokens=800,
    )

    improved = _sanitize_rewrite(improved, previous_query)

    logger.info(
        "Retry rewrite: '%s' -> '%s' (feedback: %s)",
        previous_query[:50], improved[:80], evaluator_feedback[:60]
    )
    return improved


def _format_history(history: list[dict]) -> str:
    """Format conversation history for insertion into the rewriter prompt."""
    lines = []
    for msg in history:
        role = "User" if msg.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {msg.get('content', '')}")
    return "\n".join(lines)
