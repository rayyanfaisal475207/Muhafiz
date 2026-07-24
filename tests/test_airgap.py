import pytest
import os
from unittest.mock import patch, AsyncMock
from src import config
from src.retrieval.web_search import perform_web_search
from src.llm import client as llm_client

@pytest.mark.asyncio
async def test_airgap_mode_blocks_outbound_requests():
    """
    Test that when AIR_GAP_MODE is enabled, the web_search module strictly
    refuses to make outbound HTTP requests. This proves the air-gap property
    functions correctly (requests are blocked), not just that the system survives.
    """
    original_api_key = os.getenv("TAVILY_API_KEY")
    os.environ["TAVILY_API_KEY"] = "fake-api-key"
    try:
        # Patch aiohttp so if a request is made, we can detect it
        with patch("src.retrieval.web_search.aiohttp.ClientSession.post") as mock_post:
            mock_post.return_value.__aenter__.return_value.status = 200
            mock_post.return_value.__aenter__.return_value.json = AsyncMock(return_value={"results": []})
            
            # 1. Test with Air-gap enabled (should NOT call mock_post)
            config.AIR_GAP_MODE = True
            results = await perform_web_search("test query", max_results=1)
            
            assert results == [], "In airgap mode, results should be strictly empty."
            mock_post.assert_not_called()  # Crucial: NO network call attempted

            # 2. Test with Air-gap disabled (should call mock_post)
            config.AIR_GAP_MODE = False
            await perform_web_search("test query", max_results=1)
            
            mock_post.assert_called_once()  # Network call was attempted when air-gap is off
    finally:
        # Restore state
        config.AIR_GAP_MODE = False
        if original_api_key is None:
            del os.environ["TAVILY_API_KEY"]
        else:
            os.environ["TAVILY_API_KEY"] = original_api_key


@pytest.mark.asyncio
async def test_airgap_mode_blocks_llm_cloud_fallback_on_local_failure():
    """
    Regression: AIR_GAP_MODE used to only gate the web-search route.
    call_llm()/stream_llm() — used by every pipeline stage, not just WEB —
    silently fell back to Groq/Gemini on ANY local-model failure regardless
    of AIR_GAP_MODE, which would leak case-query text to a cloud provider
    from an air-gapped deployment the moment the local model hiccuped.
    """
    original_local_url = config.LOCAL_LLM_URL
    config.LOCAL_LLM_URL = "http://fake-local-model:9/v1"
    try:
        with patch.object(llm_client, "_call_local", AsyncMock(side_effect=RuntimeError("local down"))), \
             patch.object(llm_client, "_call_groq", AsyncMock(return_value="cloud answer")) as mock_groq, \
             patch.object(llm_client, "_call_gemini", AsyncMock(return_value="cloud answer")) as mock_gemini:

            # Air-gapped: must fail closed, never reach a cloud provider.
            config.AIR_GAP_MODE = True
            with pytest.raises(RuntimeError):
                await llm_client.call_llm("system", "user message")
            mock_groq.assert_not_called()
            mock_gemini.assert_not_called()

            # Not air-gapped: the existing local-first-then-cloud-fallback
            # behavior (the ngrok-tunnel deployment this system normally runs
            # under) must be completely unaffected.
            config.AIR_GAP_MODE = False
            result = await llm_client.call_llm("system", "user message")
            assert result == "cloud answer"
            assert mock_groq.called or mock_gemini.called
    finally:
        config.AIR_GAP_MODE = False
        config.LOCAL_LLM_URL = original_local_url
