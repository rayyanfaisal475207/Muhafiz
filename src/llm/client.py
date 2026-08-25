import logging
import asyncio
from typing import AsyncGenerator, Optional

from google import genai
from google.genai import types

import httpx
from groq import AsyncGroq

from src import config
from src.llm.key_manager import key_manager

logger = logging.getLogger(__name__)

# ── Cached SDK clients ─────────────────────────────────────────────────────────
# Building a new client per call adds connection setup to every LLM call.
# Cache one client per (provider, api_key) — key rotation still works because
# the rotated key produces a new cache entry.

_groq_clients: dict[str, AsyncGroq] = {}
_gemini_clients: dict[str, genai.Client] = {}


def _get_groq_client() -> AsyncGroq:
    api_key = key_manager.get_current_key("groq")
    if api_key not in _groq_clients:
        _groq_clients[api_key] = AsyncGroq(api_key=api_key)
    return _groq_clients[api_key]


def _get_gemini_client() -> genai.Client:
    api_key = key_manager.get_current_key("gemini")
    if api_key not in _gemini_clients:
        _gemini_clients[api_key] = genai.Client(api_key=api_key)
    return _gemini_clients[api_key]


def _is_rate_limit(exc: Exception) -> bool:
    err_str = str(exc)
    return "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "rate limit" in err_str.lower()


# ── Public API ─────────────────────────────────────────────────────────────────

async def call_llm(
    system_prompt: str,
    user_message: str,
    temperature: float = 0.0,
    max_tokens: int = 1000,
    provider_override: str = None,
    llm_mode: str = None,
    role: str = "reasoning",
    cloud_max_tokens: int = None,
    force_cloud: bool = False,
    reasoning_effort: Optional[str] = None,
) -> str:
    provider = provider_override or config.LLM_PROVIDER
    # force_cloud skips the local branch entirely — for callers where a
    # "successful" local response that isn't usable (e.g. JSON-classification
    # call sites getting a conversational non-JSON reply back) doesn't count
    # as a call_llm-level failure, so the normal local-first-with-fallback
    # logic below never reaches the cloud branch on its own; AIR_GAP_MODE
    # still applies below regardless of this flag — a data-sovereignty
    # boundary a caller-side quality issue must never override.
    local_url = (
        None if force_cloud else
        (config.LOCAL_GEN_LLM_URL if role == "generation" else config.LOCAL_LLM_URL)
    )
    # The caller can't know in advance whether this call will resolve to the
    # local model or a cloud fallback — that's decided below, per attempt.
    # cloud_max_tokens lets a caller give the cloud branch a smaller budget
    # than the local one without touching the (confirmed, live-verified)
    # local value: Qwen3-14B's thinking trace needs real headroom that a
    # cloud model, with no equivalent hidden trace, never did.
    resolved_cloud_max_tokens = cloud_max_tokens if cloud_max_tokens is not None else max_tokens

    # ── First priority: local model for every call (router, evaluator, rewriter,
    # citation validator, memory summarizer included), falls back to Groq/Gemini
    # on any failure. Matches stream_llm()'s local-first policy — Groq's
    # free-tier quota was observed exhausted across all 3 rotated keys under
    # normal eval load, breaking router/evaluator calls specifically. ──
    if bool(local_url):
        try:
            return await _call_local(system_prompt, user_message, temperature, max_tokens, role)
        except Exception as e:
            if config.AIR_GAP_MODE:
                # Data-sovereignty boundary: an air-gapped deployment must never
                # let a local-model hiccup (timeout, restart, OOM) silently send
                # case-query text to a cloud provider. Fail closed instead of
                # falling through to Groq/Gemini below.
                logger.error(f"Local LLM failed under AIR_GAP_MODE: {e}. Refusing cloud fallback.")
                raise RuntimeError(
                    "Local model unavailable and AIR_GAP_MODE is active — refusing to fall back "
                    "to a cloud LLM provider."
                ) from e
            logger.warning(f"Local LLM failed: {e}. Falling back to {provider}...")

    if config.AIR_GAP_MODE:
        # No local endpoint configured at all — still must not phone home.
        raise RuntimeError(
            "No local LLM endpoint configured and AIR_GAP_MODE is active — refusing to call "
            "a cloud LLM provider."
        )

    max_retries = 3
    for attempt in range(max_retries):
        observed_index = key_manager.get_current_index(provider)
        try:
            if provider == "groq":
                return await _call_groq(
                    system_prompt, user_message, temperature, resolved_cloud_max_tokens,
                    reasoning_effort=reasoning_effort,
                )
            else:
                return await _call_gemini(system_prompt, user_message, temperature, resolved_cloud_max_tokens)
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning(f"Rate limit hit on {provider}. Rotating key... (Attempt {attempt+1}/{max_retries})")
                key_manager.rotate_key(provider, observed_index)
                await asyncio.sleep(2)
            else:
                raise e
    raise Exception(f"Failed to call {provider} after {max_retries} attempts due to rate limits.")


async def stream_llm(
    system_prompt: str,
    user_message: str,
    temperature: float = 0.7,
    max_tokens: int = 2000,
    provider_override: str = None,
    enable_tools: bool = False,
    llm_mode: str = None,
    role: str = "reasoning",
) -> AsyncGenerator[str, None]:
    provider = provider_override or config.LLM_PROVIDER
    local_url = config.LOCAL_GEN_LLM_URL if role == "generation" else config.LOCAL_LLM_URL

    # ── First priority: Local model for response generation (falls back to Groq) ──
    if bool(local_url):
        try:
            async for chunk in _stream_local(system_prompt, user_message, temperature, max_tokens, role):
                yield chunk
            return
        except Exception as e:
            if config.AIR_GAP_MODE:
                # See call_llm()'s matching guard — never fall through to a
                # cloud provider once air-gapped, even mid-stream.
                logger.error(f"Local LLM stream failed under AIR_GAP_MODE: {e}. Refusing cloud fallback.")
                raise RuntimeError(
                    "Local model unavailable and AIR_GAP_MODE is active — refusing to fall back "
                    "to a cloud LLM provider."
                ) from e
            logger.warning(f"Local LLM stream failed: {e}. Falling back to {provider}...")

    if config.AIR_GAP_MODE:
        raise RuntimeError(
            "No local LLM endpoint configured and AIR_GAP_MODE is active — refusing to call "
            "a cloud LLM provider."
        )

    max_retries = 3
    for attempt in range(max_retries):
        observed_index = key_manager.get_current_index(provider)
        try:
            if provider == "groq":
                async for chunk in _stream_groq(system_prompt, user_message, temperature, max_tokens, enable_tools):
                    yield chunk
            else:
                async for chunk in _stream_gemini(system_prompt, user_message, temperature, max_tokens, enable_tools):
                    yield chunk
            return
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning(f"Rate limit hit on {provider} (stream). Rotating key... (Attempt {attempt+1}/{max_retries})")
                key_manager.rotate_key(provider, observed_index)
                await asyncio.sleep(2)
            else:
                raise e
    raise Exception(f"Failed to stream {provider} after {max_retries} attempts due to rate limits.")


# ── Local (bespoke FastAPI wrapper, not OpenAI-compatible) ────────────────────
#
# Confirmed live against the actual model server: POST {LOCAL_LLM_URL} (no
# /v1 suffix — it is NOT an OpenAI-style /v1/chat/completions route) with
# {"system": ..., "prompt": ...} returns {"response": "...", "model": ...,
# "thinking": ..., "eval_count": ..., "total_duration_ms": ...}. It has no
# streaming mode (a /stream sibling route and a ?stream=true query param
# both 404/no-op) — _stream_local below fakes streaming by yielding the
# whole response as one chunk, which is enough for stream_llm()'s callers
# (they consume chunks incrementally but work fine with a single one).

def _local_url_and_model(role: str) -> tuple[str, str]:
    """Pick the local endpoint + model name for a role: "generation" → Qalb, else Qwen3 (reasoning)."""
    if role == "generation" and config.LOCAL_GEN_LLM_URL:
        return config.LOCAL_GEN_LLM_URL, config.LOCAL_GEN_LLM_MODEL
    return config.LOCAL_LLM_URL, config.LOCAL_LLM_MODEL


async def _post_local(system_prompt: str, user_message: str, temperature: float, max_tokens: int, role: str) -> str:
    url, model = _local_url_and_model(role)
    timeout = config.LOCAL_GEN_LLM_TIMEOUT if role == "generation" else config.LOCAL_LLM_TIMEOUT
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            json={
                "system": system_prompt,
                "prompt": user_message,
                "model": model,
                "temperature": temperature,
                # NOT clamped to 800 (as this used to silently do regardless of
                # what the caller passed) — confirmed live: verifier.py/
                # evaluator.py both already pass max_tokens=800 believing
                # that IS the real ceiling, so a second, hidden cap at the
                # same value was a no-op until a real, complex verification
                # prompt needed more than 800 tokens for Qwen3-14B's thinking
                # trace + a multi-claim JSON answer — it got silently
                # truncated mid-string instead. The caller decides its own
                # budget now.
                "max_tokens": max_tokens,
            },
        )
        response.raise_for_status()
        return response.json()["response"]


async def _call_local(system_prompt: str, user_message: str, temperature: float, max_tokens: int, role: str = "reasoning") -> str:
    content = await _post_local(system_prompt, user_message, temperature, max_tokens, role)
    # A blank string is just as unusable as None — confirmed live: Qwen3-14B
    # (the local reasoning model) sometimes spends its ENTIRE max_tokens
    # budget on its own thinking trace (the server can't be told to skip
    # it) and returns a normal 200 response with empty `response`, not an
    # error. Treating only `None`/missing as failure let that empty string
    # silently pass through as if it were a real answer — the caller
    # (verifier.py in this case) then failed to parse it and defaulted to a
    # confusing "fail-closed" rejection instead of reaching this function's
    # existing automatic Groq/Gemini fallback, which is what should have
    # happened.
    if not content or not content.strip():
        raise ValueError("Local LLM returned empty content")
    return content


async def _stream_local(system_prompt: str, user_message: str, temperature: float, max_tokens: int, role: str = "reasoning"):
    # See the module-level note above — the server has no real streaming
    # mode, so this awaits the full (non-streamed) response and yields it
    # as a single chunk.
    content = await _post_local(system_prompt, user_message, temperature, max_tokens, role)
    # Same empty/whitespace-only failure _call_local() guards against (see
    # its comment) — raising here correctly triggers stream_llm()'s existing
    # Groq/Gemini fallback instead of silently "succeeding" with no output.
    if not content or not content.strip():
        raise ValueError("Local LLM stream returned empty content")
    yield content


# ── Gemini ────────────────────────────────────────────────────────────────────

async def _call_gemini(system_prompt: str, user_message: str, temperature: float, max_tokens: int) -> str:
    client = _get_gemini_client()
    response = await client.aio.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
        ),
    )
    return response.text


async def _stream_gemini(system_prompt: str, user_message: str, temperature: float, max_tokens: int, enable_tools: bool = False):
    # Fully async streaming — the previous implementation iterated a
    # synchronous generator inside the async function, blocking the event
    # loop (and every other request) between chunks.
    client = _get_gemini_client()
    stream = await client.aio.models.generate_content_stream(
        model=config.GEMINI_MODEL,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
        ),
    )
    async for chunk in stream:
        if chunk.text:
            yield chunk.text


async def call_gemini_with_search(user_message: str, max_tokens: int = 1500) -> tuple[str, list[dict]]:
    """
    Calls Gemini using the Google Search tool.
    Returns a tuple of (generated_text, list_of_sources).
    """
    max_retries = 3
    for attempt in range(max_retries):
        observed_index = key_manager.get_current_index("gemini")
        try:
            client = _get_gemini_client()
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_message,
                config=types.GenerateContentConfig(
                    tools=[{'google_search': {}}],
                    temperature=0.3,
                    max_output_tokens=max_tokens
                )
            )

            text = response.text or ""
            sources = []

            # Extract URLs from grounding metadata
            if response.candidates and response.candidates[0].grounding_metadata:
                metadata = response.candidates[0].grounding_metadata
                if hasattr(metadata, "grounding_chunks") and metadata.grounding_chunks:
                    for chunk in metadata.grounding_chunks:
                        if hasattr(chunk, "web") and chunk.web:
                            sources.append({
                                "title": getattr(chunk.web, "title", "Google Search Result"),
                                "url": getattr(chunk.web, "uri", getattr(chunk.web, "url", ""))
                            })

            return text, sources

        except Exception as e:
            if _is_rate_limit(e):
                logger.warning(f"Rate limit hit on Gemini Search. Rotating key... (Attempt {attempt+1}/{max_retries})")
                key_manager.rotate_key("gemini", observed_index)
                await asyncio.sleep(2)
            else:
                raise e

    raise Exception(f"Failed to use Gemini Search after {max_retries} attempts.")


# ── Groq ──────────────────────────────────────────────────────────────────────

async def _call_groq(
    system_prompt: str, user_message: str, temperature: float, max_tokens: int,
    reasoning_effort: Optional[str] = None,
) -> str:
    # [Module 2 follow-up, findings.md] reasoning_effort: config.GROQ_MODEL
    # (openai/gpt-oss-120b) is itself a reasoning model — confirmed live it
    # spends max_tokens on an internal reasoning trace before any visible
    # content, the exact same failure mode already documented for the local
    # Qwen3-14B model (see router.py's own comment on why its max_tokens is
    # 800, not 250), just previously unaddressed on the cloud side because
    # nothing had ever needed a small cloud_max_tokens budget before. A
    # short JSON-classification reply (router.py's call site) doesn't need
    # deep reasoning; "low" cut a real router call's reasoning_tokens from
    # consuming its entire budget down to 82, leaving room for the actual
    # JSON content within a 300-token completion budget. `None` (the
    # default) omits the field entirely, unchanged behavior for every other
    # caller and for a non-reasoning GROQ_MODEL that wouldn't recognize it.
    kwargs = {}
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    client = _get_groq_client()
    response = await client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        model=config.GROQ_MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
        **kwargs,
    )
    return response.choices[0].message.content


async def _stream_groq(system_prompt: str, user_message: str, temperature: float, max_tokens: int, enable_tools: bool = False):
    from src.llm.tools import TOOL_DEFINITIONS, execute_tool

    client = _get_groq_client()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]
    tools = TOOL_DEFINITIONS if enable_tools else None

    stream = await client.chat.completions.create(
        messages=messages,
        model=config.GROQ_MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        tools=tools,
        tool_choice="auto" if enable_tools else "none"
    )

    tool_calls = {}
    async for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls:
                    tool_calls[idx] = {"id": tc.id, "name": tc.function.name, "arguments": ""}
                if tc.function.arguments:
                    tool_calls[idx]["arguments"] += tc.function.arguments
        elif delta.content:
            yield delta.content

    if tool_calls:
        # Append the assistant's tool calls to messages
        messages.append({
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    }
                } for tc in tool_calls.values()
            ]
        })

        # Execute tools and append tool results
        for tc in tool_calls.values():
            result = await execute_tool(tc["name"], tc["arguments"])
            messages.append({
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": tc["name"],
                "content": result,
            })
            # Yield event dict to caller (orchestrator)
            yield {"tool_call": tc["name"], "args": tc["arguments"], "result": result}

        # Second stream call with tool results
        stream2 = await client.chat.completions.create(
            messages=messages,
            model=config.GROQ_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True
        )
        async for chunk in stream2:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
