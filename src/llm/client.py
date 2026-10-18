import logging
import asyncio
import os
from typing import AsyncGenerator, Optional

from google import genai
from google.genai import types

import httpx
from groq import AsyncGroq

from src import config
from src.llm import cache as response_cache
from src.llm.key_manager import key_manager

logger = logging.getLogger(__name__)

# ── Cached SDK clients ─────────────────────────────────────────────────────────
# Building a new client per call adds connection setup to every LLM call.
# Cache one client per (provider, api_key) — key rotation still works because
# the rotated key produces a new cache entry.
#
# NOTE: this caches CLIENTS, not responses — it saves connection setup and
# not one token. The response cache is `src/llm/cache.py`, applied in
# `call_llm` below so that all 36 call sites get it at once.

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


def _is_payload_too_large(exc: Exception) -> bool:
    """
    True for "this single request exceeds the provider's per-request token
    cap" — Groq returns this as HTTP 413 with code `rate_limit_exceeded`,
    which makes it look like a throttle to `_is_rate_limit()` above.

    It is NOT a throttle, and the distinction matters: a throttle clears by
    waiting or rotating to another key, but a per-request size cap is
    identical on every key of the same tier, so rotating just burns the
    retry budget and then fails the whole request. Observed live: the
    router prompt (~10.2k tokens) exceeded Groq's free-tier 8k cap, all
    three rotations failed the same way, and the chat turn died outright
    (scenario-verify Finding U).

    Callers use this to fail over to a provider without that cap instead.
    """
    err_str = str(exc)
    if "413" in err_str or "request_too_large" in err_str.lower():
        return True
    lowered = err_str.lower()
    return "too large" in lowered and "token" in lowered


def _fallback_provider_for(provider: str) -> Optional[str]:
    """
    A configured cloud provider to retry on when `provider` cannot serve a
    request at all (currently: the payload exceeds its per-request cap).
    Returns None when no distinct, configured alternative exists — the
    caller then surfaces the original error rather than pretending to retry.
    """
    # OpenAI-shaped providers first when configured: the condition that
    # lands here is a per-request size cap, and Groq's free tier has the
    # smallest one, so failing over to it is the least likely to help.
    if provider != "azure" and os.getenv("AZURE_OPENAI_API_KEY"):
        return "azure"
    if provider != "openai" and config.OPENAI_API_KEY:
        return "openai"
    if provider != "groq" and config.GROQ_API_KEY:
        return "groq"
    if provider != "gemini" and config.GEMINI_API_KEY:
        return "gemini"
    return None


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
    """Every LLM call outside the evidence runtime, with a response cache.

    AN IDENTICAL REQUEST HAS AN IDENTICAL ANSWER.

    The cache is applied here, around the whole call, because all 36 call
    sites across extraction, graph, pipeline and the harness agents arrive
    through this one function -- and because the body below has five
    separate success paths through local, Groq and Gemini. Wrapping it once
    cannot miss one of them; editing each return could.

    The key is every argument that can change the answer. A request that
    asked for sampling is not cached at all. See src/llm/cache.py.
    """
    cached, cache_key = response_cache.lookup(
        temperature,
        system_prompt=system_prompt, user_message=user_message,
        max_tokens=max_tokens,
        provider=provider_override or config.LLM_PROVIDER,
        llm_mode=llm_mode, role=role, cloud_max_tokens=cloud_max_tokens,
        force_cloud=force_cloud, reasoning_effort=reasoning_effort)
    if cached is not None:
        logger.debug("LLM response served from cache (role=%s)", role)
        return cached
    answer = await _call_llm_uncached(
        system_prompt, user_message, temperature, max_tokens,
        provider_override, llm_mode, role, cloud_max_tokens, force_cloud,
        reasoning_effort)
    # Only a success is remembered; a raised exception never reaches here.
    response_cache.remember(cache_key, answer)
    return answer


async def _call_llm_uncached(
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
    # ── LOCAL-FIRST IS A POLICY, AND IT IS NOW A SETTING ──────────────────
    #
    # Every call here tried the local model first and only reached a cloud
    # provider when the local one FAILED. So pointing LLM_PROVIDER at a
    # cloud vendor changed nothing for a healthy local model: document
    # classification, NER, relationship extraction, entity resolution,
    # conflict detection, the evaluator, the file structurer and every
    # harness agent all kept running on Qwen3-14B. The setting looked
    # applied and was not.
    #
    # LLM_PREFER_CLOUD inverts that for deployments that chose a cloud model
    # for quality reasons. It is OFF by default, because turning it on means
    # DOCUMENT TEXT REACHES THE PROVIDER: these call sites read case files,
    # unlike the evidence runtime's Draft and Review, which are local by
    # design precisely so case text cannot leave.
    #
    # AIR_GAP_MODE still wins below, unconditionally. A data-sovereignty
    # boundary is not a preference and this flag must never cross it.
    prefer_cloud = os.getenv("LLM_PREFER_CLOUD", "false").strip().lower() == "true"
    if prefer_cloud and config.AIR_GAP_MODE:
        logger.warning(
            "LLM_PREFER_CLOUD is set but AIR_GAP_MODE is active; staying local.")
        prefer_cloud = False
    local_url = (
        None if (force_cloud or prefer_cloud) else
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
            # Dispatched by name. This used to read `if groq ... else gemini`,
            # which made "openai" -- a value config.py has advertised all
            # along -- silently call Gemini instead.
            if provider == "groq":
                return await _call_groq(
                    system_prompt, user_message, temperature, resolved_cloud_max_tokens,
                    reasoning_effort=reasoning_effort,
                )
            elif provider == "openai":
                return await _call_openai(
                    system_prompt, user_message, temperature, resolved_cloud_max_tokens,
                    reasoning_effort=reasoning_effort,
                )
            elif provider == "azure":
                return await _call_azure(
                    system_prompt, user_message, temperature, resolved_cloud_max_tokens,
                    reasoning_effort=reasoning_effort,
                )
            elif provider == "gemini":
                return await _call_gemini(system_prompt, user_message, temperature, resolved_cloud_max_tokens)
            else:
                # Named rather than quietly routed somewhere. A typo in
                # LLM_PROVIDER should not become a bill from a vendor the
                # deployment did not choose.
                raise RuntimeError(
                    f"LLM_PROVIDER={provider!r} is not a provider this client "
                    "implements (groq, openai, azure, gemini)."
                )
        except Exception as e:
            # Checked BEFORE _is_rate_limit: Groq reports an oversized request
            # as 413 with code `rate_limit_exceeded`, so it would otherwise be
            # mistaken for a throttle and "retried" by rotating keys — which
            # cannot help, because every key on the tier has the same
            # per-request cap. Fail over to a provider without that cap once.
            if _is_payload_too_large(e):
                fallback = _fallback_provider_for(provider)
                if fallback is None:
                    logger.error(
                        "Request too large for %s and no alternative provider is "
                        "configured: %s", provider, e,
                    )
                    raise
                logger.warning(
                    "Request too large for %s (per-request token cap). Failing over "
                    "to %s for this call.", provider, fallback,
                )
                if fallback == "groq":
                    return await _call_groq(
                        system_prompt, user_message, temperature, resolved_cloud_max_tokens,
                        reasoning_effort=reasoning_effort,
                    )
                if fallback == "openai":
                    return await _call_openai(
                        system_prompt, user_message, temperature, resolved_cloud_max_tokens,
                        reasoning_effort=reasoning_effort,
                    )
                if fallback == "azure":
                    return await _call_azure(
                        system_prompt, user_message, temperature, resolved_cloud_max_tokens,
                        reasoning_effort=reasoning_effort,
                    )
                return await _call_gemini(
                    system_prompt, user_message, temperature, resolved_cloud_max_tokens,
                )
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
    key = os.getenv('EVIDENCE_MODEL_API_KEY','')
    gateway_options = ({'think':False,'timeout_seconds':min(600,float(timeout))}
                       if os.getenv('EVIDENCE_MODEL_WRAPPER_V2','false').lower()=='true' else {})
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            headers={'Authorization':'Bearer '+key} if key else {},
            json={
                **gateway_options,
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


# Bounds for the empty-response retry in _call_local() below.
#
# The FLOOR matters more than the multiplier. Measured live against
# qwen3:14b, a doubled budget is not enough when the original was small:
# retrying 300 at 600 came back empty a second time, because the thinking
# trace alone wants ~500-1200 tokens before the answer starts. Budgets of
# 1,200+ had headroom in every run, and 2,000 is what evaluator.py and
# verifier.py already settled on for this same model. So retry at
# whichever is larger — double, or the floor.
#
# The CEILING keeps a pathological prompt from spiralling, and stays low
# enough that the retry lands inside LOCAL_LLM_TIMEOUT; a retry that times
# out just raises and hands off to the cloud fallback anyway.
_LOCAL_EMPTY_RETRY_FLOOR = 2000
_LOCAL_EMPTY_RETRY_CEILING = 4000


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
        # The trace length is not deterministic — measured live against
        # qwen3:14b, the SAME prompt produced traces from ~2,000 to ~4,750
        # characters run to run. So a budget that usually suffices will
        # intermittently be swallowed whole, and call_llm()'s default of
        # 1,000 sits right in that failure band (500 came back empty,
        # 800 only just answered, 1,200+ had headroom). 29 call sites use
        # that default, including the harness sub-agents.
        #
        # Retry once locally with a bigger budget (double, or the floor
        # above, whichever is larger) before giving up. This
        # keeps the work on the local GPU instead of spilling to Groq
        # (whose free tier rejects large prompts outright with a 413) or
        # Gemini, and it costs nothing on the overwhelming majority of
        # calls that never hit this branch. Capped so a pathological
        # prompt can't spiral; if the retry is still empty — or times
        # out — we raise exactly as before and the existing cloud
        # fallback takes over unchanged.
        retry_max_tokens = min(
            max(max_tokens * 2, _LOCAL_EMPTY_RETRY_FLOOR), _LOCAL_EMPTY_RETRY_CEILING
        )
        if retry_max_tokens > max_tokens:
            logger.warning(
                "Local LLM returned empty content (thinking trace consumed all %d tokens) — "
                "retrying once locally with %d.",
                max_tokens, retry_max_tokens,
            )
            content = await _post_local(
                system_prompt, user_message, temperature, retry_max_tokens, role
            )
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
                model=config.GEMINI_MODEL,
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


# ── OpenAI ────────────────────────────────────────────────────────────────────

# Parameter spellings a model has already refused, per model name. One
# refusal teaches the whole process rather than every call paying for the
# same 400.
_OPENAI_REFUSED: dict[str, set[str]] = {}

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


def _openai_adapt(body: dict, message: str) -> bool:
    """Rewrite one request to the dialect a refusal just named. True if changed.

    The server states exactly which parameter it will not take, so the
    refusal is a specification rather than a failure.

    MEASURED against this key: every gpt-5 model refuses `max_tokens` and
    names `max_completion_tokens` instead; gpt-5.5 refuses a `temperature`
    of anything but its default while gpt-5.4-mini accepts 0.0. A hardcoded
    table of which model tolerates what would be wrong the day a new one
    ships, so the refusal is read and applied.
    """
    low = (message or "").lower()
    seen = _OPENAI_REFUSED.setdefault(str(body.get("model")), set())
    if "max_tokens" in low and "max_completion_tokens" in low and "max_tokens" in body:
        body["max_completion_tokens"] = body.pop("max_tokens")
        seen.add("max_tokens")
        return True
    if "temperature" in low and "temperature" in body:
        body.pop("temperature")
        seen.add("temperature")
        return True
    return False


def _azure_endpoint() -> tuple[str, str]:
    """The Azure chat-completions URL and the deployment it names.

    Azure names the model by DEPLOYMENT in the path rather than by a `model`
    field, and requires an api-version. MEASURED against this deployment:
    every api-version from 2024-02-01 to 2025-01-01-preview answers, so the
    default is a recent stable one and it stays overridable.
    """
    base = os.getenv("AZURE_OPENAI_BASE_URL", "").rstrip("/")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
    version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
    if not base:
        raise RuntimeError("AZURE_OPENAI_BASE_URL is not set; cannot call Azure.")
    return ("%s/deployments/%s/chat/completions?api-version=%s"
            % (base, deployment, version)), deployment


async def _call_azure(
    system_prompt: str, user_message: str, temperature: float, max_tokens: int,
    reasoning_effort: Optional[str] = None,
) -> str:
    """One completion from an Azure OpenAI deployment.

    Shares `_call_openai`'s body and its dialect adaptation, because the
    request body is identical: MEASURED against this deployment, Azure
    refuses `max_tokens` in favour of `max_completion_tokens` and refuses
    any temperature but its default, exactly as the gpt-5 models on OpenAI
    do. Only the envelope differs -- the deployment in the path, and the key
    in an `api-key` header instead of a bearer token.
    """
    key = os.getenv("AZURE_OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("AZURE_OPENAI_API_KEY is not set; cannot call Azure.")
    url, deployment = _azure_endpoint()
    return await _openai_style_call(
        url=url, headers={"api-key": key}, model=deployment,
        system_prompt=system_prompt, user_message=user_message,
        temperature=temperature, max_tokens=max_tokens,
        reasoning_effort=reasoning_effort)


async def _call_openai(
    system_prompt: str, user_message: str, temperature: float, max_tokens: int,
    reasoning_effort: Optional[str] = None,
) -> str:
    """One completion from OpenAI.

    WHY THIS EXISTS AT ALL

    `config.LLM_PROVIDER` has advertised "openai" as a choice for a long
    time and there was no branch for it: the dispatch in
    `_call_llm_uncached` reads `if provider == "groq" ... else gemini`, so
    setting LLM_PROVIDER=openai silently called GEMINI. Every one of the 36
    call sites across extraction, graph, pipeline and the harness agents
    would have gone to the wrong vendor without a single error.

    Raw httpx rather than the SDK, because the dialect above needs to read
    the server's refusal text and retry, and because this file already
    speaks httpx to the local model.
    """
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set; cannot call OpenAI.")
    return await _openai_style_call(
        url=OPENAI_CHAT_URL,
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        model=config.OPENAI_MODEL,
        system_prompt=system_prompt, user_message=user_message,
        temperature=temperature, max_tokens=max_tokens,
        reasoning_effort=reasoning_effort)


async def _openai_style_call(
    *, url: str, headers: dict, model: str, system_prompt: str,
    user_message: str, temperature: float, max_tokens: int,
    reasoning_effort: Optional[str] = None,
) -> str:
    """One chat completion against any OpenAI-shaped endpoint.

    Shared by OpenAI and Azure because the BODY is identical and only the
    envelope differs -- Azure names the model by deployment in the path and
    carries its key in an `api-key` header. Keeping one implementation means
    the dialect adaptation, which is the part that took measuring, cannot
    drift between the two.
    """
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_message}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    # What this model has already refused, applied before the request rather
    # than after a 400 that teaches nothing new.
    for refused in _OPENAI_REFUSED.get(model, set()):
        if refused == "max_tokens" and "max_tokens" in body:
            body["max_completion_tokens"] = body.pop("max_tokens")
        elif refused == "temperature":
            body.pop("temperature", None)

    async with httpx.AsyncClient(timeout=180.0) as client:
        # Two attempts per refused parameter, plus one to succeed on.
        for _ in range(4):
            response = await client.post(url, json=body, headers=headers)
            if response.status_code == 400:
                try:
                    said = (response.json().get("error") or {}).get("message", "")
                except ValueError:
                    said = ""
                if _openai_adapt(body, said):
                    continue
            response.raise_for_status()
            data = response.json()
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError("OpenAI response was not in the expected shape") from exc
        response.raise_for_status()
        raise RuntimeError("OpenAI refused every dialect this client knows")


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
