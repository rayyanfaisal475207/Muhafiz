import json
import re
import logging
from pathlib import Path
from src.llm.client import call_llm

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "file_structurer.txt"
_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")


def _extract_json(response: str) -> dict:
    """
    Extract a JSON object from an LLM response, tolerating reasoning
    preambles, markdown fences, and trailing prose.

    The old greedy `\\{.*\\}` regex grabbed from the FIRST '{' to the LAST '}'
    in the response — any brace in surrounding text produced invalid JSON.
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
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    # 3. First balanced top-level object (brace scanning, string-aware)
    start = response.find("{")
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
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = response[start:i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break
        start = response.find("{", start + 1)

    raise ValueError(f"Could not extract valid JSON from LLM response: {response[:200]!r}")


def _normalize_payload(payload: dict) -> dict:
    """
    Coerce the LLM payload into a shape the builders can never crash on:
    string title/description, and table rows padded/truncated to exactly
    match the header count (ragged rows crash reportlab and pandas).
    """
    if not isinstance(payload, dict):
        raise ValueError("Structured payload is not a JSON object")

    payload["title"] = str(payload.get("title") or "Muhafiz Export")
    payload["description"] = str(payload.get("description") or "")

    sections = payload.get("sections")
    if not isinstance(sections, list):
        sections = []
    normalized = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        stype = sec.get("type")
        if stype == "table":
            headers = [str(h) for h in sec.get("headers") or []]
            raw_rows = sec.get("rows") or []
            width = len(headers) or max((len(r) for r in raw_rows if isinstance(r, (list, tuple))), default=0)
            if width == 0:
                continue  # nothing usable in this table
            if not headers:
                headers = [f"Column {i + 1}" for i in range(width)]
            rows = []
            for r in raw_rows:
                if not isinstance(r, (list, tuple)):
                    r = [r]
                cells = ["" if c is None else str(c) for c in r]
                cells = (cells + [""] * width)[:width]
                rows.append(cells)
            normalized.append({"type": "table", "headers": headers, "rows": rows})
        elif stype in ("heading", "paragraph"):
            level = sec.get("level", 2)
            try:
                level = min(max(int(level), 1), 3)
            except (TypeError, ValueError):
                level = 2
            normalized.append({"type": stype, "level": level, "content": str(sec.get("content") or "")})
    payload["sections"] = normalized
    return payload


async def structure_for_file(content: str, requested_format: str) -> dict:
    """
    Takes raw text/data and a requested format (xlsx, docx, pdf)
    and uses the LLM to convert it into a structured JSON payload for generation.
    """
    user_message = f"Requested format: {requested_format}\n\nContent to structure:\n{content}"

    try:
        response = await call_llm(
            system_prompt=_PROMPT,
            user_message=user_message,
            temperature=0.0,
            # 4000 alone left no headroom under the local model's 4096-token
            # total context window (input + output), so every local attempt
            # hard-failed (400) and fell through to a rate-limited Groq.
            # 3000 leaves ~1000 tokens of input headroom while still fitting
            # realistic file-structuring payloads.
            max_tokens=3000,
        )
        return _normalize_payload(_extract_json(response))

    except Exception as e:
        logger.error("Failed to structure file payload: %s", e)
        raise
