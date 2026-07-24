import logging
import os
import aiohttp

from src import config

logger = logging.getLogger(__name__)

async def perform_web_search(query: str, max_results: int = 5) -> list[dict]:
    """
    Search the web using Tavily API, restricted to config.WEB_ALLOWED_DOMAINS
    (relevance/reliability control, not just safety — architecture doc's
    guarded-web-search requirement).

    Args:
        query: The search query string.
        max_results: Number of results to return.

    Returns:
        A list of result dictionaries: [{"title": str, "url": str, "content": str, "score": float}]
    """
    if config.AIR_GAP_MODE:
        # The one route in the architecture that needs outbound access —
        # first thing disabled in an air-gapped deployment. Never issue
        # the HTTP call at all, not just discard its result.
        logger.info("AIR_GAP_MODE enabled — web search skipped.")
        return []

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        logger.warning("TAVILY_API_KEY not set. Web search will return mock/empty results.")
        # Fallback for dev mode
        return [{"title": "Web Search Disabled", "url": "http://localhost", "content": "Tavily API key is missing. Please add it to your .env file to enable live web search.", "score": 1.0}]

    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.tavily.com/search"
            payload = {
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
                "include_answer": False,
                "time_range": "year",
                "include_domains": config.WEB_ALLOWED_DOMAINS,
            }
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    text = await response.text()
                    logger.error("Tavily search failed: %s", text)
                    return []
                
                data = await response.json()
                results = []
                for item in data.get("results", []):
                    results.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "content": item.get("content", ""),
                        "score": item.get("score", 0.0)
                    })
                return results
    except Exception as e:
        logger.error("Error performing web search: %s", e)
        return []
