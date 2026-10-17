"""One response cache for every LLM call outside the evidence runtime.

WHY THIS EXISTS

`src/llm/client.py` already had something called a cache, and it cached SDK
*clients* -- connection reuse, not a single token saved. So every caller of
`call_llm` paid full price for every call, every time: document
classification, NER, relationship extraction, entity resolution, conflict
detection, the aggregate spec reader, the evaluator, the file structurer and
every harness agent. Thirty-six call sites across extraction, graph and
pipeline.

That is the expensive kind of waste, because these callers run over the
corpus. Re-extracting the same document, re-running an eval, re-processing a
batch after a crash -- all of it re-sends prompts the model has already
answered identically.

The evidence runtime has had a response cache for a while
(`src/evidence/model.py`). This is the same discipline for everything else.

WHAT IS SAFE TO CACHE, AND WHY ONLY THIS

The key is the WHOLE request -- every argument that can change the answer,
including the prompt, the model role, the token budget and the provider the
caller asked for. A different request is a different key, so a changed
prompt or a changed document can never be served an old answer.

ONLY DETERMINISTIC CALLS. A caller that passes a temperature above zero is
asking the model to vary; handing it the same text twice would silently
change what it does. MEASURED in this repo: 26 of 30 call sites pass
`temperature=0.0`, so declining to cache the sampled ones costs almost
nothing and keeps their behaviour honest.

ONLY SUCCESSES. An exception is never remembered -- the next attempt may
well work -- and neither is an empty string, which is a failure wearing a
success's clothes.

IT IS IN MEMORY ON PURPOSE

These prompts carry case text. Postgres holds that behind row-level
security; writing it to a cache file would put the same text somewhere with
none of those controls, for a saving that only matters inside one process
anyway. So it lives here and dies with the process.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import OrderedDict

logger = logging.getLogger(__name__)


def enabled() -> bool:
    """Whether responses may be served from this cache. On unless switched off."""
    return os.getenv("LLM_RESPONSE_CACHE", "true").strip().lower() == "true"


def _cacheable_temperature(temperature: float) -> bool:
    """Only a request that asked for one answer may be served a stored one.

    Above the threshold the caller wants sampling, and a cache would quietly
    take that away. The threshold is a setting rather than a constant so a
    deployment that wants every call cached can say so.
    """
    try:
        ceiling = float(os.getenv("LLM_RESPONSE_CACHE_MAX_TEMPERATURE", "0.0"))
    except ValueError:
        ceiling = 0.0
    return float(temperature or 0.0) <= ceiling


class ResponseCache:
    """A bounded, least-recently-used store of completed responses."""

    def __init__(self, limit: int = 512):
        self._items: "OrderedDict[str, str]" = OrderedDict()
        self.limit = max(1, limit)
        self.hits = 0
        self.misses = 0
        self.saved_chars = 0

    @staticmethod
    def key(**parts) -> str:
        """A digest of everything that can change the answer.

        Hashed rather than stored whole: these prompts contain case text, and
        a dictionary keyed on raw prompts would hold a second copy of it in a
        structure nothing audits. The digest is enough to recognise a repeat.
        """
        blob = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        if key in self._items:
            self._items.move_to_end(key)
            self.hits += 1
            return self._items[key]
        self.misses += 1
        return None

    def put(self, key: str, value: str) -> None:
        # A failure is never remembered. An empty response is a failure.
        if not isinstance(value, str) or not value.strip():
            return
        self._items[key] = value
        self._items.move_to_end(key)
        self.saved_chars += len(value)
        while len(self._items) > self.limit:
            self._items.popitem(last=False)

    def stats(self) -> dict:
        asked = self.hits + self.misses
        return {"entries": len(self._items), "hits": self.hits,
                "misses": self.misses,
                "hit_ratio": round(self.hits / asked, 3) if asked else 0.0,
                "response_chars_served": self.saved_chars}

    def clear(self) -> None:
        self._items.clear()
        self.hits = self.misses = self.saved_chars = 0


CACHE = ResponseCache(int(os.getenv("LLM_RESPONSE_CACHE_ENTRIES", "512")))


def lookup(temperature: float, **parts) -> tuple[str | None, str | None]:
    """A stored response for this request, and the key to store one under.

    Returns `(None, None)` when this request may not be cached at all, so a
    caller cannot accidentally store a sampled answer by holding on to a key
    it was not given.
    """
    if not enabled() or not _cacheable_temperature(temperature):
        return None, None
    key = ResponseCache.key(temperature=temperature, **parts)
    return CACHE.get(key), key


def remember(key: str | None, value: str) -> None:
    """Store a completed response, if this request was cacheable."""
    if key:
        CACHE.put(key, value)
