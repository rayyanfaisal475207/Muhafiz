import logging
import json
from pathlib import Path
from src.llm.client import call_llm

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "sql_param_extractor.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

async def extract_sql_params(query: str) -> dict:
    response = await call_llm(
        system_prompt=_SYSTEM_PROMPT,
        user_message=query,
        temperature=0.0,
        # Qwen3-14B's thinking trace consumes max_tokens before its JSON
        # answer, and this server ignores enable_thinking=False — 150 was
        # truncating to an empty response every time.
        max_tokens=800,
    )
    
    try:
        cleaned = response.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        result = json.loads(cleaned.strip())
        return result
    except Exception as e:
        logger.error(f"SQL Extractor failed to parse JSON: {e}. Raw: {response}")
        return None
