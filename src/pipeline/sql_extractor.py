import logging
from pathlib import Path
from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "sql_param_extractor.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

async def extract_sql_params(query: str) -> dict:
    # call_llm_json retries with an explicit schema-naming correction if
    # Qwen3 answers conversationally instead of with JSON, then makes one
    # guaranteed cloud attempt before giving up — same fix as
    # router.py/evaluator.py/verifier.py for the identical failure shape.
    # Previously this was a single attempt that returned None on any
    # failure at all, silently falling back to RAG downstream.
    result, response = await call_llm_json(
        system_prompt=_SYSTEM_PROMPT,
        user_message=query,
        temperature=0.0,
        # Qwen3-14B's thinking trace consumes max_tokens before its JSON
        # answer, and this server ignores enable_thinking=False — 150 was
        # truncating to an empty response every time; 800 later turned out
        # not to be enough either on live re-measurement. Raised to 2000
        # for the LOCAL budget; cloud_max_tokens pinned at the old 800 so
        # the cloud fallback is unaffected.
        max_tokens=2000,
        cloud_max_tokens=800,
        validate=lambda r: isinstance(r, dict) and "category" in r,
        schema_hint='"category" (string), "subject" (string), "section_ref" (string or null), "date" (string or null)',
        _call_llm=call_llm,
    )
    if result is None:
        logger.error("SQL Extractor failed to parse JSON after retries. Raw: %s", response[:200])
    return result
