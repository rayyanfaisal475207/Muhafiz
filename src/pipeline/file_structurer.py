import logging
from pathlib import Path
from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "file_structurer.txt"
_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")


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

    # call_llm_json retries with an explicit schema-naming correction if
    # Qwen3 answers conversationally instead of with JSON, then makes one
    # guaranteed cloud attempt before giving up — same fix as
    # router.py/evaluator.py/verifier.py/sql_extractor.py for the identical
    # failure shape. Previously this was a single attempt that raised
    # straight through to the caller on any failure at all.
    result, response = await call_llm_json(
        system_prompt=_PROMPT,
        user_message=user_message,
        temperature=0.0,
        # 4000 alone left no headroom under the local model's 4096-token
        # total context window (input + output), so every local attempt
        # hard-failed (400) and fell through to a rate-limited Groq.
        # 3000 leaves ~1000 tokens of input headroom while still fitting
        # realistic file-structuring payloads.
        max_tokens=3000,
        validate=lambda r: isinstance(r, dict) and "sections" in r,
        schema_hint='"title" (string), "description" (string), "sections" (array of {"type", "content"/"headers"/"rows", "level"})',
        _call_llm=call_llm,
    )
    if result is None:
        logger.error("Failed to structure file payload after retries. Raw: %s", response[:200])
        raise ValueError(f"Could not structure file payload — LLM never returned valid JSON: {response[:200]!r}")

    return _normalize_payload(result)
