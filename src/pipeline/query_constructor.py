import logging
from pathlib import Path

from src.llm.client import call_llm

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "search_query_constructor.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

async def construct_search_queries(rewritten_query: str) -> list[str]:
    """
    Convert a natural language query into optimized search strings.
    
    Returns a list of search queries.
    """
    response = await call_llm(
        system_prompt=_SYSTEM_PROMPT,
        user_message=rewritten_query,
        temperature=0.2,
        max_tokens=100,
    )
    
    # Parse lines
    lines = [line.strip() for line in response.split("\n") if line.strip()]
    
    # Strip bullet points if LLM hallucinates them
    cleaned = []
    for line in lines:
        if line.startswith("-") or line.startswith("*"):
            line = line[1:].strip()
        # Remove numbering like "1. "
        if len(line) > 2 and line[0].isdigit() and line[1] in [".", ")"]:
            line = line[2:].strip()
        if line:
            cleaned.append(line)
            
    if not cleaned:
        logger.warning("Query constructor returned empty list, falling back to original query")
        return [rewritten_query]
        
    logger.info("Constructed %d search queries: %s", len(cleaned), cleaned)
    return cleaned
