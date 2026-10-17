"""Ask the corpus in the language the corpus is written in.

WHY

Measured, the same question in two languages against the same records:

    "فراڈ کے طریقے"                 -> bank fraud, OTP theft, extortion.
                                        The right narratives.
    "what methods were used to
     commit fraud"                  -> FIR headers about kidnapping
                                        (365-A PPC). Wrong.

The narratives in this corpus are Urdu. An English question embeds near
English text, and there is very little English text to land on, so it
drifts to whichever headers happen to sit nearest in the space.

e5 is multilingual, and that is often taken to mean a question in one
language lands as close to a record in another as a question in its own
language would. It does not. Sharing a space is not the same as being
equally close within it.

So the question is ALSO asked in the other language, and both rankings
are fused. This is recall, not rewriting: the original question is still
searched exactly as asked, and a second ranking can only promote a record
the first ranking missed.

WHAT THIS IS NOT

It is not the bilingual expansion that broke binding (L50). That one
multiplied candidate TERMS and made every filter less certain. This adds
one more query to a vector search whose results are fused by rank, and it
never touches which column or value a filter uses.

It is also not a dictionary. Nothing here holds a list of police words.
The translation is produced at the moment it is needed and cached for the
session only; the vocabulary grows with the questions people ask, not
with the data.
"""

from __future__ import annotations

import re

from src.llm.client import call_llm

_ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
_LATIN = re.compile(r"[A-Za-z]")

# One process-lifetime cache. Questions repeat far more than they vary,
# and a translation of the same words is the same translation.
_CACHE: dict[str, str] = {}
_CACHE_LIMIT = 500


SYSTEM = """You translate a question about Pakistani police records so it
can be matched against records written in the other language.

Return ONLY the translated question. No explanation, no quotes, no
alternatives.

Rules:
1. Translate the MEANING as an investigator would say it, not word for
   word. "How many cases are registered" becomes the natural Urdu a
   police officer would use.
2. Keep numbers, FIR numbers, dates and proper names exactly as written.
3. If the text is already in the target language, return it unchanged."""


def script_of(text: str) -> str:
    """Which writing system a question is mostly in."""
    arabic = len(_ARABIC.findall(text or ""))
    latin = len(_LATIN.findall(text or ""))
    if arabic and arabic >= latin:
        return "ur"
    if latin:
        return "en"
    return "unknown"


async def alternate(question: str) -> str:
    """The same question in the other language, or "" if unavailable.

    Returning "" is a normal outcome, not an error: the caller still has
    the original question and searches with it. A translation that cannot
    be produced costs recall, never correctness.
    """
    text = (question or "").strip()
    if not text:
        return ""

    source = script_of(text)
    if source == "unknown":
        return ""
    target = "Urdu" if source == "en" else "English"

    key = f"{source}:{text}"
    if key in _CACHE:
        return _CACHE[key]

    try:
        # MEASURED: at 300 tokens qwen3 spent the whole budget on its
        # thinking trace, returned empty, and the retry produced "دھویں"
        # (smoke) and "غشی" (fainting) for "fraud" -- translations that
        # drag the search AWAY from the records. A reasoning model needs
        # room to think before it answers, and the budget must cover
        # both. Same failure that made the counting agent look random.
        raw = await call_llm(SYSTEM, f"Translate into {target}:\n{text}",
                             temperature=0.0, max_tokens=2500)
    except Exception:                                   # noqa: BLE001
        return ""

    out = " ".join((raw or "").split())
    if not out or len(out) > len(text) * 6:
        # A translation several times the length of the question is a
        # commentary, not a translation, and would pull the embedding
        # away from the question rather than toward the records.
        return ""

    # It must actually be in the target script, or it is not a
    # translation and adds nothing but a duplicate search.
    if script_of(out) == source:
        return ""

    if len(_CACHE) < _CACHE_LIMIT:
        _CACHE[key] = out
    return out
