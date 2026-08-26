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
import re
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
        # follow-ups, silently masked by the empty-string fallback below);
        # 800 later turned out not to be enough either on live
        # re-measurement. Raised to 2000 for the LOCAL budget;
        # cloud_max_tokens pinned at the old 800 so the cloud fallback is
        # unaffected.
        max_tokens=2000,
        cloud_max_tokens=800,
    )

    sanitized = _sanitize_rewrite(raw)
    if sanitized is not None and _echoes_prior_assistant_turn(sanitized, conversation_history):
        logger.warning(
            "Query rewriter echoed a prior assistant message instead of "
            "rewriting: %r.", sanitized[:100],
        )
        sanitized = None
    rewritten = sanitized or user_message.strip()

    logger.info("Query rewritten: '%s' -> '%s'", user_message[:50], rewritten[:80])
    return rewritten


# Minimum overlap length before a rewrite is treated as "copied from
# history" rather than coincidentally sharing a short common phrase — a
# real rewrite legitimately reuses short fragments from history (a station
# name, an FIR number); only a long verbatim run is suspicious.
_ECHO_MIN_OVERLAP = 40


def _echoes_prior_assistant_turn(rewritten: str, conversation_history: list[dict]) -> bool:
    """
    Structural catch-all, complementary to _sanitize_rewrite()'s pattern
    list: confirmed live (2026-08-03), the rewriter sometimes verbatim-
    echoes a PRIOR ASSISTANT MESSAGE from history — most often the app's
    own canned abstention text truncated at the max_tokens boundary — and
    uses that as the search query for the current, unrelated question. A
    literal phrase list can only catch known wordings; this catches the
    shape of the failure (the "rewrite" is substantially a copy of
    something the assistant already said) regardless of what that text is,
    including future canned messages or genuine past answers alike.
    """
    lowered = rewritten.lower()
    for msg in conversation_history:
        if msg.get("role") != "assistant":
            continue
        content = (msg.get("content") or "").lower()
        if len(content) < _ECHO_MIN_OVERLAP:
            continue
        if lowered in content or content in lowered:
            return True
        # Prefix match: the rewrite is a truncated copy of a longer prior
        # assistant message (the max_tokens-truncation shape actually seen
        # live), not necessarily a full-string match either direction.
        prefix_len = min(len(lowered), len(content), 80)
        if prefix_len >= _ECHO_MIN_OVERLAP and lowered[:prefix_len] == content[:prefix_len]:
            return True
    return False


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
    #
    # A literal phrase list turned out to be exactly as incomplete here as
    # verifier.py's own docstring warns for refusal phrases in general —
    # confirmed live: re-running the SAME live scenario after the phrase
    # list above shipped, the model produced two more first-person refusals
    # ("I don't have specific information about items recovered...", "To
    # provide a more accurate and helpful response, it's important to
    # clarify that...") that matched none of those exact phrases. Patterns
    # below target the STRUCTURE of a refusal/meta-commentary sentence
    # instead of exact wording, so new phrasings of the same failure mode
    # don't each need their own literal entry:
    #   - first-person negated possession of information ("I don't have
    #     [any/specific/...] information/details/access/data")
    #   - meta-commentary about the response itself ("to provide a more
    #     accurate/helpful/better answer/response", "it's important to
    #     clarify/note/understand", "without specific/more details/context")
    _REFUSAL_PATTERNS = (
        r"\bi (?:currently )?(?:don'?t|do not|can'?t|cannot|am not able to|'?m not able to) "
        r"(?:currently )?have\b",
        r"\bi (?:can'?t|cannot|am not able to|'?m not able to) provide\b",
        r"\bi recommend (?:contacting|referring to)\b",
        r"\bi suggest (?:contacting|referring to)\b",
        r"\bto provide (?:a|an) (?:more|better) (?:accurate|helpful|complete)\b",
        r"\bit'?s important to (?:clarify|note|understand)\b",
        r"\bit is important to (?:clarify|note|understand)\b",
        r"\bwithout (?:specific|more|additional) (?:details|information|context)\b",
        # Confirmed live (2026-08-03, same re-test): a retry-rewrite of a
        # genuinely vague query addressed the USER instead of producing an
        # improved query — "To improve the search query and make it more
        # specific and effective, you can provide more context such as:".
        r"\bto (?:improve|refine) the (?:search )?query\b",
        r"\byou can provide more (?:context|details|information)\b",
        # Confirmed live (2026-08-03, second re-test): the rewriter didn't
        # invent a refusal this time — it verbatim-echoed the APP'S OWN
        # canned abstention text from the previous turn's assistant message
        # in conversation history ("I couldn't find sufficient information
        # in the knowledge base to accurately answer your question...",
        # `orchestrator.py`'s `_SAFE_RESPONSE`), truncated at the max_tokens
        # boundary, then used THAT as the search query for the current
        # question. Exact known string, not a wording the model invented —
        # matched permanently rather than left to the general history-echo
        # check below, since it will recur exactly like this every time.
        r"\bcouldn'?t find (?:sufficient|enough) information\b",
        r"\btry rephrasing your question\b",
        r"\b(?:ensure|make sure) (?:that )?(?:the )?relevant documents (?:have been|are) ingested\b",
    )
    if any(re.search(pattern, lowered) for pattern in _REFUSAL_PATTERNS):
        logger.warning("Query rewriter produced a refusal, not a query: %r.", text[:100])
        return None

    # A fifth confirmed failure mode (2026-08-03, live re-test), distinct
    # from refusing: given enough conversation history, the model instead
    # ANSWERS the follow-up directly using facts from the previous turn —
    # "Based on the information provided in **Document 1 (Recovery Memo)**
    # for **FIR-2026-THEFT-012**, the specific items that have been
    # recovered..." — rather than producing a new standalone search query.
    # This one has a clean, almost zero-false-positive tell: the rewriter
    # runs BEFORE retrieval, so it has never seen a "Document N" citation —
    # any [Document N] / **Document N** reference in its output can only be
    # the model echoing a citation from conversation history while
    # answering, never a legitimate rewrite. "Based on the information
    # provided/available" is the same failure's generic answering preamble,
    # flagged independently in case a citation isn't quoted verbatim.
    if re.search(r"[\[(]?\*{0,2}document\s+\d+\*{0,2}[\])]?", lowered) or re.search(
        r"\bbased on the information (?:provided|available)\b", lowered
    ):
        logger.warning("Query rewriter answered using history, not a query: %r.", text[:100])
        return None

    # A sixth confirmed failure mode (2026-08-04, live re-test, D-1): the
    # model narrates ABOUT the rewriting task instead of doing it — e.g.
    # 'The original question — "How many recurring vehicles have appeared
    # across multiple cases?" — is similar in intent to the previous search
    # query, but it may be interpreted differently depending on how
    # "recurring vehicles" are defined.' and, on a second, independent live
    # occurrence, 'To address your query effectively, we need to follow a
    # structured approach...'. Both survived every guard above (well under
    # 400 chars, no refusal/answering phrase, no "the question "/"the
    # query " exact substring the commentary check above looks for). A real
    # rewrite is always ONE standalone question/phrase, never multiple
    # sentences of prose — so multi-sentence output alone is disqualifying,
    # same structural-shape approach as every guard above it (this file's
    # own history: literal-phrase lists keep needing re-patching for new
    # wording of the same failure).
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(sentences) > 1:
        logger.warning("Query rewriter produced multi-sentence prose, not a query: %r.", text[:100])
        return None

    # Backstop for single-sentence narration that still evades the count
    # above (e.g. a truncated multi-clause sentence) — first-person-plural
    # planning language and self-reference to "the original question"/"the
    # previous query" never appear in an actual standalone search query.
    _META_PLANNING_PATTERNS = (
        r"\bwe need to\b",
        r"\blet'?s (?:follow|use|try|consider)\b",
        r"\bto address (?:your|this) query\b",
        r"\ba structured approach\b",
        r"\bthe original question\b",
        r"\bthe previous (?:search )?query\b",
        r"\bis similar in intent\b",
    )
    if any(re.search(pattern, lowered) for pattern in _META_PLANNING_PATTERNS):
        logger.warning("Query rewriter produced meta-planning commentary, not a query: %r.", text[:100])
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
            # Raised to 2000 for the LOCAL budget; cloud_max_tokens pinned
            # at the old 800 so the cloud fallback is unaffected.
            max_tokens=2000,
            cloud_max_tokens=800,
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
