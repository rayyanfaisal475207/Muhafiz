"""
KeyManager.rotate_key()'s compare-and-swap (Module 6.4).

Concurrent rate-limit failures on the same key used to each call
rotate_key() unconditionally, over-rotating past keys nobody had tried yet.
rotate_key() now only advances the index if it still matches what the
caller observed before its own call failed — a caller that lost the race
(someone else already rotated) is a no-op instead of a second rotation.
"""
from src.llm.key_manager import KeyManager


def _manager(groq_keys):
    km = KeyManager.__new__(KeyManager)
    km.gemini_keys = []
    km.groq_keys = list(groq_keys)
    km.gemini_index = 0
    km.groq_index = 0
    return km


def test_rotate_key_without_observed_index_always_rotates():
    """Backward-compatible default: omitting observed_index behaves like
    the old unconditional increment."""
    km = _manager(["k0", "k1", "k2"])
    km.rotate_key("groq")
    assert km.groq_index == 1


def test_rotate_key_skips_when_index_already_moved():
    km = _manager(["k0", "k1", "k2"])
    km.rotate_key("groq", observed_index=0)
    assert km.groq_index == 1
    # A second caller that also observed index 0 (now stale) must not rotate again.
    km.rotate_key("groq", observed_index=0)
    assert km.groq_index == 1


def test_concurrent_failures_on_the_same_key_rotate_only_once():
    """
    N concurrent callers all capture the current index before making their
    own call; all of them then fail against that same key around the same
    time and each call rotate_key(). Only the first should actually advance
    the index.
    """
    km = _manager(["k0", "k1", "k2", "k3", "k4"])
    observed = km.get_current_index("groq")
    for _ in range(4):
        km.rotate_key("groq", observed)
    assert km.groq_index == 1


def test_a_caller_that_observes_the_new_index_can_still_rotate_again():
    """Once the observed index catches up to reality, rotation is not
    permanently stuck — a genuinely new failure still advances it."""
    km = _manager(["k0", "k1", "k2"])
    km.rotate_key("groq", observed_index=0)
    assert km.groq_index == 1
    km.rotate_key("groq", observed_index=1)
    assert km.groq_index == 2


def test_rotate_key_wraps_around_and_is_independent_per_provider():
    km = KeyManager.__new__(KeyManager)
    km.gemini_keys = ["g0", "g1"]
    km.groq_keys = ["q0", "q1"]
    km.gemini_index = 0
    km.groq_index = 0

    km.rotate_key("gemini", observed_index=0)
    km.rotate_key("gemini", observed_index=1)
    assert km.gemini_index == 0  # wrapped back around
    assert km.groq_index == 0  # untouched
