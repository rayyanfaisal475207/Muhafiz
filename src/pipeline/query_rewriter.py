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
from typing import Optional

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

    raw = await call_llm(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_input,
        temperature=0.0,   # Deterministic: same input should always produce same rewrite
        # Qwen3-14B's thinking trace consumes max_tokens before its one-line
        # answer, and this server ignores enable_thinking=False — 200 was
        # truncating to an empty response every time (esp. on Urdu/Roman-Urdu
        # follow-ups, silently masked by the empty-string fallback below).
        max_tokens=800,
    )

    rewritten = _sanitize_rewrite(raw) or user_message.strip()

    logger.info("Query rewritten: '%s' -> '%s'", user_message[:50], rewritten[:80])
    return rewritten


def _sanitize_rewrite(rewritten: str) -> Optional[str]:
    """
    Guard against common LLM failure modes: empty output, preambles,
    wrapping quotes, multi-line answers, or answering instead of rewriting.
    Returns None (rather than silently substituting a fallback) when the
    output is unusable, so callers can decide whether to retry or fall back.
    """
    text = (rewritten or "").strip()
    if not text:
        return None

    # Strip markdown emphasis markers first — confirmed live, the model
    # often wraps an echoed label in "**" ("**Improved search query:**"),
    # which would otherwise survive the prefix-strip below untouched.
    text = text.strip("*").strip()

    # Strip label-style preambles the model sometimes adds despite
    # instructions — including "improved search query:", confirmed live as
    # a genuine echo of rewrite_for_retry()'s own trailing prompt line
    # ("Write an improved search query:") rather than an actual rewrite.
    # Without this, the echoed label passed sanitization as a very short
    # "valid" single line and became the next retry's search query outright
    # — and, since rewrite_for_retry() feeds its own previous output back in
    # as `previous_query` on a second retry, an uncaught echo compounds:
    # confirmed live producing 'Improved search query: **"Which section of
    # the PPC' -> 'Improved search query: **"Which section of the PPC
    # Act...' across two retries.
    for prefix in (
        "output:", "rewritten query:", "query:", "rewritten:",
        "improved search query:", "improved query:", "search query:",
    ):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
    text = text.strip("*").strip()

    # Take the first non-empty line — a rewrite is always a single line
    text = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    text = text.strip('"').strip("'").strip()

    # A "rewrite" several times longer than a reasonable query is almost
    # certainly the model answering the question. A suspiciously short
    # leftover (the label stripped to nothing, or near enough) is the
    # compounding-echo failure above with no real query left behind either
    # way — both are unusable.
    if not text or len(text) < 3 or len(text) > 400:
        logger.warning("Query rewriter output unusable (len=%d).", len(text))
        return None

    # A third confirmed failure mode, distinct from both above: the model
    # discusses the query instead of rewriting it — short enough to survive
    # the length check, but recognizably commentary ("The question '...' is
    # unclear because...", "This appears to be asking about..."). A real
    # rewritten query never talks about "the question"/"the query" itself
    # in the third person, so that phrase alone is a reliable tell.
    lowered = text.lower()
    if any(marker in lowered for marker in (
        "the question ", "the query ", "this appears to be", "seems to be asking",
        "is unclear", "could refer to", "is not a standard",
    )):
        logger.warning("Query rewriter produced commentary, not a query: %r.", text[:100])
        return None

    # A fourth confirmed failure mode (2026-08-03, query_ids 887/889):
    # distinct from the commentary case above, the model treats the message
    # as a real question and answers it directly with a refusal — "I
    # currently do not have access to specific information regarding
    # which..." — rather than rewriting it. This is long enough (under 400
    # chars) and phrased in the first person, so it survives every check
    # above, and it's actively harmful once it survives: this text becomes
    # the literal search query handed to embedding + BM25, and it compounds
    # across turns — the very next message in the same session got
    # contaminated by this refusal sitting in conversation history and
    # produced a second one. A real rewritten query is never phrased in the
    # first person about the rewriter's own knowledge.
    if any(marker in lowered for marker in (
        "i currently do not have access", "i do not currently have access",
        "i currently don't have access", "i don't currently have access",
        "i do not have access to", "i don't have access to",
        "i do not have information", "i don't have information",
        "i cannot provide", "i can't provide", "i am not able to provide",
        "i'm not able to provide", "i recommend contacting",
        "i recommend referring to", "i suggest contacting",
    )):
        logger.warning("Query rewriter produced a refusal, not a query: %r.", text[:100])
        return None

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
        "You are NOT talking to the user and you are NOT answering the question — "
        "your only output is the raw query text itself, nothing else: no label, no "
        "heading, no leading phrase like \"Improved search query:\", no markdown "
        "formatting or bold text, no quotes, no explanation. Confirmed failure mode: "
        "a response that starts with a label like that (even just echoing this "
        "prompt's own wording) gets used verbatim as the next search query, which "
        "finds nothing — the label itself is not a query.\n\n"
        "Rules:\n"
        "- Target specifically what the feedback says is missing.\n"
        "- Use DIFFERENT keywords than the previous query: swap plain language for "
        "procedural/statutory terms (or vice versa), e.g. 'FIR copy' <-> 'certified copy of First Information Report', "
        "'PPC section' <-> 'penal code provision'.\n"
        "- Add the likely PPC/PECA section number if the feedback hints at one; "
        "drop section numbers that already failed.\n"
        "- Keep it focused: one topic, under 25 words.\n\n"
        "Another confirmed failure mode, distinct from the label-echo one above: "
        "treating this as a real question to discuss instead of a rewriting task, "
        "e.g. producing \"The question 'X' is unclear because...\" or \"This appears "
        "to be asking about...\". That is commentary about the query, not a query — "
        "never write about the question, only rewrite it.\n\n"
        "Example — WRONG (commentary, not a rewrite):\n"
        "Previous query: What PPC section covers mobile phone theft?\n"
        "Bad output: The question \"What PPC section covers mobile phone theft?\" is "
        "unclear because PPC could refer to different things.\n"
        "Example — RIGHT (an actual alternative query):\n"
        "Good output: Penal code provision for theft of mobile phone under PPC 379"
    )

    user_input = (
        f"Original user question: {original_message}\n\n"
        f"Previous search query (which failed): {previous_query}\n\n"
        f"Why it failed (evaluator feedback): {evaluator_feedback}"
    )

    # Up to 2 attempts: a plain retry with the same prompt tends to repeat
    # the same failure (confirmed live for both the label-echo and the
    # commentary failure modes above), so the second attempt appends an
    # explicit correction naming what went wrong in the first — same
    # pattern as call_llm_json's schema-repair retry for the JSON-output
    # call sites elsewhere in this pipeline, adapted for free-text output.
    message = user_input
    improved = None
    for attempt in range(2):
        raw = await call_llm(
            system_prompt=retry_prompt,
            user_message=message,
            temperature=0.2,  # Slight creativity to try different keywords
            # See rewrite_query() above — Qwen3-14B's thinking trace needs room
            # inside max_tokens on this server, which can't be told to skip it.
            max_tokens=800,
        )
        improved = _sanitize_rewrite(raw)
        if improved is not None:
            break
        logger.warning(
            "Retry rewrite attempt %d/2 unusable: %r", attempt + 1, raw[:120],
        )
        message = (
            user_input
            + "\n\n[SYSTEM CORRECTION] Your previous reply was not usable as a search "
            "query — it was either a label/heading instead of query text, or "
            "commentary discussing the question instead of rewriting it. Reply with "
            "ONLY the raw improved query text, nothing else."
        )

    if improved is None:
        logger.warning(
            "Retry rewrite failed after 2 attempts (feedback: %s). Using previous query unchanged.",
            evaluator_feedback[:60],
        )
        improved = previous_query.strip()

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
