# ============================================================
# Embedder — multilingual-e5-large-instruct (locally served)
#
# DEFAULT PROVIDER: "e5"  (1024 dimensions)
#   Calls EMBEDDINGS_URL — the local model-server endpoint for
#   multilingual-e5-large-instruct — with an asymmetric is_query flag:
#   True for a search query (embed_text, query time), False for a stored
#   chunk (embed_texts, ingestion time). Getting this backwards doesn't
#   error, it silently degrades retrieval quality.
#
# OTHER PROVIDERS (not the default, selected via EMBEDDING_PROVIDER):
#   - "gemini": Gemini gemini-embedding-001, 3072 dimensions. Cloud, kept
#     for environments without local model serving.
#   - "openai": OpenAI text-embedding-3-small. Cloud fallback.
#   - "local": chromadb's DefaultEmbeddingFunction (all-MiniLM-L6-v2,
#     384 dimensions) — a CPU-only last-resort fallback, NOT Gemini,
#     despite this file's previous header claiming otherwise. This is
#     what the existing corpus was actually embedded with before the e5
#     switch (Phase 0.4/0.8).
#
# IMPORTANT: All chunks in one Chroma collection must use the SAME
# embedding model + dimensions. If you switch providers, the collection
# must be dropped and the whole corpus re-ingested (see Phase 0.8 /
# scripts/reingest_kb.py) — Chroma raises a hard dimension-mismatch error
# rather than silently corrupting results, but only at query time.
# ============================================================

import asyncio
import logging

from src import config

logger = logging.getLogger(__name__)


async def embed_text(text: str, task_type: str = "RETRIEVAL_QUERY") -> list[float]:
    """
    Embed a single text (used at query time).

    Args:
        text:      The search query to embed.
        task_type: "RETRIEVAL_QUERY" for queries (default).

    Returns:
        A 3072-dimensional embedding vector.
    """
    vectors = await embed_texts([text], task_type=task_type)
    return vectors[0]


async def embed_texts(
    texts: list[str],
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> list[list[float]]:
    """
    Batch embed multiple texts (used at ingestion time).

    Args:
        texts:     List of text chunks to embed.
        task_type: "RETRIEVAL_DOCUMENT" for stored chunks (default).

    Returns:
        List of 3072-dimensional vectors in the same order as input.
    """
    if not texts:
        return []

    if config.EMBEDDING_PROVIDER == "e5":
        # multilingual-e5-large-instruct is asymmetric: is_query=True for a
        # search query, False for a stored chunk. Getting this backwards
        # doesn't error — it just silently degrades retrieval quality.
        is_query = task_type == "RETRIEVAL_QUERY"
        return await _embed_local_e5(texts, is_query)
    elif config.EMBEDDING_PROVIDER == "gemini":
        return await _embed_gemini(texts, task_type)
    elif config.EMBEDDING_PROVIDER == "openai":
        return await _embed_openai(texts)
    elif config.EMBEDDING_PROVIDER == "local":
        return await _embed_local(texts)
    else:
        # Auto-detect: prefer local to avoid rate limits if no keys
        if config.GEMINI_API_KEY and config.EMBEDDING_PROVIDER == "gemini":
            return await _embed_gemini(texts, task_type)
        else:
            return await _embed_local(texts)


from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ── Gemini Embeddings (google-genai SDK) ──────────────────────────────────────

async def _embed_gemini(texts: list[str], task_type: str) -> list[list[float]]:
    """
    Embed texts using Gemini gemini-embedding-001 via the new google-genai SDK.

    The new SDK's embed_content() accepts a list of texts in one call,
    returning one embedding per input. Much cleaner than the old SDK.

    Args:
        texts:     List of texts to embed.
        task_type: Gemini task hint ("RETRIEVAL_DOCUMENT" or "RETRIEVAL_QUERY").

    Returns:
        List of 3072-dim float vectors.
    """
    from google.genai import types
    from src.llm.client import _get_gemini_client

    # Reuse the shared cached client — constructing one per embed call added
    # connection setup to every retrieval (3+ times per query with expansion).
    client = _get_gemini_client()

    from google.genai.errors import ClientError

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        retry=retry_if_exception_type(ClientError),
        reraise=True
    )
    def _do_embed_batch(batch_texts: list[str]) -> list[list[float]]:
        result = client.models.embed_content(
            model=config.GEMINI_EMBEDDING_MODEL,
            contents=batch_texts,
            config=types.EmbedContentConfig(task_type=task_type),
        )
        return [emb.values for emb in result.embeddings]

    batch_size = 100
    all_embeddings = []
    
    # Process in batches sequentially. 
    # Batching to 100 uses only 1 API call instead of 100, avoiding limits.
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        # to_thread prevents blocking the async event loop during the API call
        import asyncio
        batch_embs = await asyncio.to_thread(_do_embed_batch, batch)
        all_embeddings.extend(batch_embs)

    logger.debug(
        "Gemini embedded %d text(s) via %s (task=%s, dims=%d)",
        len(texts), config.GEMINI_EMBEDDING_MODEL, task_type,
        len(all_embeddings[0]) if all_embeddings else 0,
    )
    return all_embeddings


# ── Local e5 Embeddings (multilingual-e5-large-instruct, served locally) ──────

# Kept small so a bulk-ingestion request never risks timing out the
# free-tier ngrok tunnel in front of the model server.
_E5_BATCH_SIZE = 16
_E5_INTER_BATCH_DELAY = 0.3  # seconds between batches


async def _embed_local_e5(texts: list[str], is_query: bool) -> list[list[float]]:
    """
    Embed texts using multilingual-e5-large-instruct via the local model server.

    Args:
        texts:    List of texts to embed.
        is_query: True when embedding a search query (asymmetric-usage switch —
                  the server applies a different instruction prefix for
                  queries vs. documents). False for document chunks at
                  ingestion time.

    Returns:
        List of float vectors in the same order as input.
    """
    import httpx

    if not config.EMBEDDINGS_URL:
        raise ValueError("EMBEDDINGS_URL is not configured")

    if not texts:
        return []

    # Batched + sequential (not concurrent), with a small inter-batch pause
    # and retry/backoff on transient failures — the model server sits behind
    # a free-tier ngrok tunnel, which has no hard payload-size limit but is
    # one process on one machine: a single huge batch (a whole document's
    # worth of chunks in one request) risks a request timeout or overloading
    # the underlying model server. Small sequential batches keep each
    # request fast and let the free tunnel keep up during bulk ingestion.
    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        reraise=True,
    )
    async def _post_batch(batch: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                config.EMBEDDINGS_URL,
                json={"texts": batch, "is_query": is_query},
            )
            response.raise_for_status()
            return response.json()["embeddings"]

    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), _E5_BATCH_SIZE):
        batch = texts[i:i + _E5_BATCH_SIZE]
        all_embeddings.extend(await _post_batch(batch))
        if i + _E5_BATCH_SIZE < len(texts):
            await asyncio.sleep(_E5_INTER_BATCH_DELAY)

    logger.debug(
        "Local e5 embedded %d text(s) in %d batch(es) (is_query=%s, dims=%d)",
        len(texts), (len(texts) + _E5_BATCH_SIZE - 1) // _E5_BATCH_SIZE,
        is_query, len(all_embeddings[0]) if all_embeddings else 0,
    )
    return all_embeddings


# ── OpenAI Embeddings (fallback) ───────────────────────────────────────────────

async def _embed_openai(texts: list[str]) -> list[list[float]]:
    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    response = await asyncio.to_thread(
        lambda: client.embeddings.create(
            model=config.OPENAI_EMBEDDING_MODEL,
            input=texts,
        )
    )
    return [e.embedding for e in sorted(response.data, key=lambda e: e.index)]

# ── Local Embeddings (CPU fallback) ───────────────────────────────────────────

async def _embed_local(texts: list[str]) -> list[list[float]]:
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    import asyncio
    
    ef = DefaultEmbeddingFunction()

    # Chroma default embedder returns a list of vectors
    embeddings = await asyncio.to_thread(ef, texts)
    # Chroma returns numpy arrays — convert to plain lists so the vectors are
    # JSON-serializable (Postgres storage and ChromaVectorStore.upsert both
    # expect plain lists, not numpy arrays).
    embeddings = [e.tolist() if hasattr(e, "tolist") else list(e) for e in embeddings]

    logger.debug(
        "Local embedded %d text(s) (dims=%d)",
        len(texts),
        len(embeddings[0]) if embeddings else 0,
    )
    return embeddings
