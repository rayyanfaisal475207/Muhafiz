import logging
import asyncio
from typing import AsyncGenerator, Optional

from google import genai
from google.genai import types

from groq import AsyncGroq
from openai import AsyncOpenAI

from src import config
from src.llm.key_manager import key_manager

logger = logging.getLogger(__name__)

# ── Cached SDK clients ─────────────────────────────────────────────────────────
# Building a new client per call adds connection setup to every LLM call.
# Cache one client per (provider, api_key) — key rotation still works because
# the rotated key produces a new cache entry.

_groq_clients: dict[str, AsyncGroq] = {}
_gemini_clients: dict[str, genai.Client] = {}
_local_client: Optional[AsyncOpenAI] = None
_local_gen_client: Optional[AsyncOpenAI] = None


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


def _get_local_client() -> Optional[AsyncOpenAI]:
    """OpenAI-compatible local model endpoint (e.g. LM Studio / vLLM via ngrok).

    Only instantiated when LOCAL_LLM_URL is configured. Both call_llm()
    (routing, rewriting, evaluation, citation validation, memory
    summarization) and stream_llm() (the final, user-facing response) now
    try local FIRST whenever LOCAL_LLM_URL is set, for every call, falling
    back to Groq/Gemini on any failure — this is deliberate, not the earlier
    always-first regression it replaced. The difference from that regression
    is the fallback: that had none, so an unreachable local endpoint broke
    every call instead of costing one failed attempt before falling through.

    `_use_local()` below is now unused dead code (both call sites moved to
    the unconditional `bool(config.LOCAL_LLM_URL)` check) — left in place
    rather than removed unprompted; flag for cleanup if picked up later.
    """
    global _local_client
    if not config.LOCAL_LLM_URL:
        return None
    if _local_client is None:
        _local_client = AsyncOpenAI(
            base_url=f"{config.LOCAL_LLM_URL.rstrip('/')}/v1",
            api_key=config.LOCAL_LLM_API_KEY or "local-key",
            timeout=config.LOCAL_LLM_TIMEOUT,
        )
    return _local_client


def _get_local_gen_client() -> Optional[AsyncOpenAI]:
    """OpenAI-compatible local generation endpoint (Qalb, via ngrok).

    Mirrors _get_local_client() above but points at LOCAL_GEN_LLM_URL — the
    dedicated final-answer-writer slot, kept separate from the reasoning
    slot (Qwen3-14B) so the two roles can point at different models/servers.
    """
    global _local_gen_client
    if not config.LOCAL_GEN_LLM_URL:
        return None
    if _local_gen_client is None:
        _local_gen_client = AsyncOpenAI(
            base_url=f"{config.LOCAL_GEN_LLM_URL.rstrip('/')}/v1",
            api_key=config.LOCAL_GEN_LLM_API_KEY or "local-key",
            timeout=config.LOCAL_GEN_LLM_TIMEOUT,
        )
    return _local_gen_client


def _use_local(llm_mode: Optional[str]) -> bool:
    return bool(config.LOCAL_LLM_URL) and (llm_mode or "").lower() == "local"


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
) -> str:
    provider = provider_override or config.LLM_PROVIDER
    local_url = config.LOCAL_GEN_LLM_URL if role == "generation" else config.LOCAL_LLM_URL
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
        try:
            if provider == "groq":
                return await _call_groq(system_prompt, user_message, temperature, resolved_cloud_max_tokens)
            else:
                return await _call_gemini(system_prompt, user_message, temperature, resolved_cloud_max_tokens)
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning(f"Rate limit hit on {provider}. Rotating key... (Attempt {attempt+1}/{max_retries})")
                key_manager.rotate_key(provider)
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
                key_manager.rotate_key(provider)
                await asyncio.sleep(2)
            else:
                raise e
    raise Exception(f"Failed to stream {provider} after {max_retries} attempts due to rate limits.")


# ── Local (OpenAI-compatible) ─────────────────────────────────────────────────

def _local_client_and_model(role: str) -> tuple[AsyncOpenAI, str]:
    """Pick the local client + model for a role: "generation" → Qalb, else Qwen3 (reasoning)."""
    if role == "generation":
        client = _get_local_gen_client()
        if client is not None:
            return client, config.LOCAL_GEN_LLM_MODEL
    client = _get_local_client()
    return client, config.LOCAL_LLM_MODEL


async def _call_local(system_prompt: str, user_message: str, temperature: float, max_tokens: int, role: str = "reasoning") -> str:
    client, model = _local_client_and_model(role)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        # NOT clamped to 800 (as this used to silently do regardless of what
        # the caller passed) — confirmed live: verifier.py/evaluator.py both
        # already pass max_tokens=800 believing that IS the real ceiling
        # ("800 matches the ceiling used in evaluator.py"), so this second,
        # hidden cap at the same value was a no-op until a real, complex
        # verification prompt needed more than 800 tokens for Qwen3-14B's
        # thinking trace + a multi-claim JSON answer — it got silently
        # truncated mid-string ("Unterminated string...") instead, which
        # this cap masked as "the caller's own limit" rather than an extra
        # one nobody asked for. The caller decides its own budget now.
        max_tokens=max_tokens,
        temperature=temperature,
    )
    content = response.choices[0].message.content
    # A blank string is just as unusable as None — confirmed live: Qwen3-14B
    # (the local reasoning model) sometimes spends its ENTIRE max_tokens
    # budget on its own thinking trace (the server can't be told to skip
    # it — see every other max_tokens=800 comment in this codebase) and
    # returns a normal 200 response with empty `content`, not an error.
    # Treating only `None` as failure let that empty string silently pass
    # through as if it were a real answer — the caller (verifier.py in
    # this case) then failed to parse it and defaulted to a confusing
    # "fail-closed" rejection instead of reaching this function's existing
    # automatic Groq/Gemini fallback, which is what should have happened.
    if not content or not content.strip():
        raise ValueError("Local LLM returned empty content")
    return content


async def _stream_local(system_prompt: str, user_message: str, temperature: float, max_tokens: int, role: str = "reasoning"):
    client, model = _local_client_and_model(role)
    stream = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        temperature=temperature,
        max_tokens=max_tokens,  # see _call_local's comment on why this is no longer clamped
        stream=True
    )

    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


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
                key_manager.rotate_key("gemini")
                await asyncio.sleep(2)
            else:
                raise e

    raise Exception(f"Failed to use Gemini Search after {max_retries} attempts.")


# ── Groq ──────────────────────────────────────────────────────────────────────

async def _call_groq(system_prompt: str, user_message: str, temperature: float, max_tokens: int) -> str:
    client = _get_groq_client()
    response = await client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        model=config.GROQ_MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
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
