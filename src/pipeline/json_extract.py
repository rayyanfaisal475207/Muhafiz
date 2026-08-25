import json
import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def extract_json(response: str) -> Any:
    """
    Extract JSON (object or array) from an LLM response, tolerating reasoning
    preambles, markdown fences, and trailing prose.

    The old greedy `\\{.*\\}` regex — duplicated across evaluator.py,
    verifier.py, and router.py — grabbed from the FIRST '{' to the LAST '}'
    in the response, so any brace in surrounding prose (or a reasoning-model
    <think> block) produced invalid JSON. This is the single implementation;
    every pipeline call site that parses a JSON LLM response should import it
    from here rather than re-rolling its own extraction.
    """
    if not response:
        raise ValueError("LLM returned empty response")

    # Models with visible reasoning wrap it in <think> tags — drop it.
    response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()

    # 1. The whole response is JSON
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # 2. A fenced ```json block
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", response, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    # 3. First balanced top-level object or array (bracket scanning, string-aware)
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = response.find(open_ch)
        while start != -1:
            depth = 0
            in_string = False
            escaped = False
            for i in range(start, len(response)):
                ch = response[i]
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == open_ch:
                        depth += 1
                    elif ch == close_ch:
                        depth -= 1
                        if depth == 0:
                            candidate = response[start:i + 1]
                            try:
                                return json.loads(candidate)
                            except json.JSONDecodeError:
                                break
            start = response.find(open_ch, start + 1)

    raise ValueError(f"Could not extract valid JSON from LLM response: {response[:200]!r}")


# ── JSON-output call with repair retry ──────────────────────────────────────
#
# Confirmed live: Qwen3 (the local reasoning model) sometimes ignores a
# "respond with ONLY JSON" system instruction entirely and answers
# conversationally instead — asking a clarifying question for a vague
# router query ("Sure! Could you please provide more context...") or
# directly answering the evaluator's classification prompt as if it were
# the user's actual question ("Here is a list of people mentioned in the
# cases: ..."). Plain retry-with-the-same-prompt doesn't fix this because
# nothing about the second attempt is different — it reliably repeats the
# same conversational failure. This helper appends an explicit correction
# on retry instead, telling the model what it did wrong and forbidding the
# specific failure mode observed (clarifying questions, direct answers).
def _json_only_correction(schema_hint: Optional[str]) -> str:
    schema_line = (
        f" Reuse the EXACT schema/field names already given above ({schema_hint}) —"
        " do not invent new field names or a different shape."
        if schema_hint else
        " Reuse the EXACT schema/field names already given above — do not"
        " invent new field names or a different shape."
    )
    return (
        "\n\n[SYSTEM CORRECTION] Your previous reply was not the required JSON — "
        "either it looked like a conversational answer/clarifying question, or "
        "it was JSON with the wrong shape (e.g. made-up field names instead of "
        "the ones specified)." + schema_line + " Do not ask the user anything and "
        "do not answer the question directly in prose. If the input is vague or "
        "ambiguous, make your best judgment call anyway and reflect that "
        "uncertainty using the fields already provided for it (e.g. a low "
        "confidence value) — never respond with a question or plain text. Reply "
        "with ONLY that JSON, nothing else."
    )


async def call_llm_json(
    system_prompt: str,
    user_message: str,
    *,
    max_tokens: int,
    temperature: float = 0.0,
    cloud_max_tokens: Optional[int] = None,
    role: str = "reasoning",
    max_attempts: int = 3,
    validate: Optional[Callable[[Any], bool]] = None,
    schema_hint: Optional[str] = None,
    _call_llm: Optional[Callable[..., Any]] = None,
    force_cloud: bool = False,
    escalate_to_cloud_on_failure: bool = False,
    reasoning_effort: Optional[str] = None,
) -> tuple[Optional[Any], str]:
    """
    Call an LLM expecting a JSON response, retrying with an explicit
    correction message (see _json_only_correction above) if the model
    responds conversationally instead of with valid JSON.

    Local-only by explicit choice: cloud is reserved for genuine local
    unavailability (a connection/HTTP error — call_llm's own existing
    local-first/cloud-on-exception logic already handles that case,
    unaffected by anything here), not for local content-quality issues
    like a conversational non-JSON reply. Confirmed live under sustained
    testing: proactively escalating every JSON-shape failure to Groq made
    a single user query fire 6+ cloud calls, exhausted the free-tier quota
    across all rotated keys, and then still failed anyway (rate-limited) —
    while the local model's own raw output was often already substantively
    correct. max_attempts defaults to 3 (up from 2) to give local more
    chances before giving up, since that's now the only lever available
    for a content-quality retry.

    Args:
        validate:    Optional predicate the parsed JSON must satisfy (e.g. "is
                     a dict with the expected keys") — a syntactically valid
                     but semantically empty/wrong-shaped JSON reply also
                     triggers a corrective retry, not just a parse failure.
        schema_hint: Short description of the required top-level field names
                     (e.g. '"relevant", "reason"'), quoted back in the retry
                     correction — confirmed live: without naming the real
                     fields, a corrected retry can still fail by inventing
                     its own plausible-looking but wrong schema instead of
                     reusing the one already in system_prompt.
        _call_llm:   Injectable override for src.llm.client.call_llm. Callers
                     (router.py, evaluator.py) pass their own module-level
                     `call_llm` import through here rather than this function
                     hard-importing it itself — several existing tests
                     monkeypatch e.g. `router.call_llm` to give router,
                     evaluator, and other LLM-calling modules independent
                     canned responses within the same test; a hard import
                     here would make every caller of this shared helper
                     collapse onto one single global patch point and break
                     that. Defaults to the real client.call_llm.
        force_cloud: Skip the local-first attempts entirely and go straight
                     to a cloud attempt. Only meaningful when a caller
                     explicitly wants a fresh cloud opinion on demand — not
                     used by anything in this pipeline by default anymore.
        escalate_to_cloud_on_failure: After exhausting local attempts with
                     no usable result, make one last-resort cloud attempt
                     before giving up. Defaults to False — opt-in only, for
                     a caller that has decided the specific failure mode
                     genuinely warrants spending cloud quota on it.
        reasoning_effort: [Module 2 follow-up, findings.md] Passed straight
                     through to call_llm/_call_groq — see that function's own
                     docstring. Only meaningful when the cloud branch is a
                     reasoning model (GROQ_MODEL); ignored by local and by
                     Gemini. `None` (default) omits it, unchanged behavior.

    Returns:
        (parsed_result, last_raw_response) — parsed_result is None if every
        attempt failed; callers pick their own fallback default (a route,
        "not relevant", an empty list, ...) since that differs per call site.
    """
    if _call_llm is None:
        from src.llm.client import call_llm as _call_llm  # deferred: avoids import cycle at module load

    def _attempt(n: int, force_cloud: bool):
        return _call_llm(
            system_prompt=system_prompt,
            user_message=message,
            temperature=temperature,
            max_tokens=max_tokens,
            cloud_max_tokens=cloud_max_tokens,
            role=role,
            force_cloud=force_cloud,
            reasoning_effort=reasoning_effort,
        )

    message = user_message
    last_raw = ""
    if not force_cloud:
        for attempt in range(max_attempts):
            last_raw = await _attempt(attempt, force_cloud=False)
            try:
                result = extract_json(last_raw)
            except ValueError as exc:
                logger.warning(
                    "call_llm_json: invalid JSON on attempt %d/%d: %s — raw: %s",
                    attempt + 1, max_attempts, exc, last_raw[:150],
                )
                message = user_message + _json_only_correction(schema_hint)
                continue

            if validate is not None and not validate(result):
                logger.warning(
                    "call_llm_json: JSON failed validation on attempt %d/%d: %s",
                    attempt + 1, max_attempts, last_raw[:150],
                )
                message = user_message + _json_only_correction(schema_hint)
                continue

            return result, last_raw

    if not (force_cloud or escalate_to_cloud_on_failure):
        return None, last_raw

    # Reached either because every local-first attempt above produced
    # something call_llm itself considers "successful" (non-empty text) but
    # unusable here — a conversational non-JSON reply isn't an exception, so
    # the normal local-first/cloud-fallback logic inside call_llm never
    # actually reached Groq/Gemini on its own — or because the caller passed
    # force_cloud=True and skipped the local loop entirely. Either way, one
    # real cloud attempt, since Groq/Gemini were confirmed reliable at this
    # exact kind of structured-JSON task even when the local model isn't
    # (AIR_GAP_MODE still blocks this inside call_llm — force_cloud never
    # overrides that boundary).
    try:
        last_raw = await _attempt(max_attempts, force_cloud=True)
        result = extract_json(last_raw)
        if validate is None or validate(result):
            return result, last_raw
        logger.warning("call_llm_json: cloud fallback JSON failed validation: %s", last_raw[:150])
    except Exception as exc:
        logger.warning("call_llm_json: cloud fallback attempt failed: %s", exc)

    return None, last_raw
