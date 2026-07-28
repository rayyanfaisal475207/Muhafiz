"""
Phase 7, Module 7.4 (item 3) — bounded vision-OCR cache.

_vision_cache used to be a plain dict with no cap, growing for the
process's entire lifetime. Now it's a bounded LRU (OrderedDict,
move-to-end on access, evict oldest on overflow) capped at
_VISION_CACHE_MAX_ENTRIES.
"""
from src.ingestion.loaders import image_loader


def _reset_cache(monkeypatch, max_entries=None):
    monkeypatch.setattr(image_loader, "_vision_cache", image_loader.OrderedDict())
    if max_entries is not None:
        monkeypatch.setattr(image_loader, "_VISION_CACHE_MAX_ENTRIES", max_entries)


def test_cache_get_put_round_trip(monkeypatch):
    _reset_cache(monkeypatch)

    assert image_loader._vision_cache_get("k1") is None
    image_loader._vision_cache_put("k1", "extracted text")
    assert image_loader._vision_cache_get("k1") == "extracted text"


def test_cache_evicts_oldest_entry_once_over_the_cap(monkeypatch):
    _reset_cache(monkeypatch, max_entries=3)

    image_loader._vision_cache_put("k1", "v1")
    image_loader._vision_cache_put("k2", "v2")
    image_loader._vision_cache_put("k3", "v3")
    assert len(image_loader._vision_cache) == 3

    image_loader._vision_cache_put("k4", "v4")

    assert len(image_loader._vision_cache) == 3
    assert image_loader._vision_cache_get("k1") is None, "the oldest entry must have been evicted"
    assert image_loader._vision_cache_get("k4") == "v4"


def test_cache_get_refreshes_recency_so_it_survives_eviction(monkeypatch):
    """LRU, not FIFO: accessing k1 must move it to the back, so the next
    eviction takes k2 (now the least-recently-used) instead."""
    _reset_cache(monkeypatch, max_entries=3)

    image_loader._vision_cache_put("k1", "v1")
    image_loader._vision_cache_put("k2", "v2")
    image_loader._vision_cache_put("k3", "v3")

    image_loader._vision_cache_get("k1")  # touch k1 — now most-recently-used

    image_loader._vision_cache_put("k4", "v4")  # should evict k2, not k1

    assert image_loader._vision_cache_get("k1") == "v1"
    assert image_loader._vision_cache_get("k2") is None
    assert image_loader._vision_cache_get("k4") == "v4"


def test_describe_image_bytes_uses_the_bounded_cache(monkeypatch):
    """End-to-end through _describe_image_bytes: a cache hit skips the
    vision call entirely; a miss calls it once and populates the cache."""
    _reset_cache(monkeypatch)

    calls = []

    def fake_call_gemini_vision(image_bytes, image_format):
        calls.append(1)
        return "some extracted text"

    monkeypatch.setattr(image_loader, "_call_gemini_vision", fake_call_gemini_vision)
    monkeypatch.setattr(image_loader, "_resize_if_large", lambda b, f: b)

    image_bytes = b"fake image bytes"

    first = image_loader._describe_image_bytes(image_bytes, "png")
    second = image_loader._describe_image_bytes(image_bytes, "png")

    assert first == "some extracted text"
    assert second == "some extracted text"
    assert len(calls) == 1, "the second call with identical bytes must hit the cache, not call vision again"


def test_describe_image_bytes_does_not_cache_empty_results(monkeypatch):
    """An empty/falsy extraction result is not cached — matches the
    pre-existing behavior (a transient failure shouldn't be memoized
    forever), unchanged by the bounding fix."""
    _reset_cache(monkeypatch)

    monkeypatch.setattr(image_loader, "_call_gemini_vision", lambda b, f: "")
    monkeypatch.setattr(image_loader, "_resize_if_large", lambda b, f: b)

    image_loader._describe_image_bytes(b"fake bytes", "png")

    assert len(image_loader._vision_cache) == 0
