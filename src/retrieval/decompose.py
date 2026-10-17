"""Split a question that asks for several figures into one ask each.

THE QUESTION THIS LAYER ANSWERS

An investigator does not ask one thing at a time:

    "How many cases are there in total? I also need the number with a
     weapon, and theft separately."

That is three figures. Every layer below this one — binding, planning,
guards, the validator — is built to establish ONE figure and prove it.
Handed all three at once, the binder sees the union of their terms and
plans something that is none of them: a count filtered by weapon AND
theft, or a total that silently ignores the last two clauses.

The failure is quiet. A single number comes back, it is arithmetically
real, and nothing marks that two thirds of the question went unanswered.

So a compound question is split, and each part is run as its own complete
question — its own binding, its own join path, its own guards, its own
validation. Three figures, three proofs. A part that cannot be answered
refuses on its own without taking the other two down with it.

NOTHING NEW IS BUILT HERE

This adds no answering machinery. The engine already establishes one
figure and proves it; a compound question was failing because it was
handed three at once. This hands them over one at a time. Everything that
makes an answer trustworthy happens where it already happened.

WHY THE MODEL SPLITS AND CODE CHECKS

The version this replaces was a list of English phrases:

    " and how many" in q  or  ", and how many" in q  or  "count" in q

It could not see a compound Urdu question at all — the language most of
these questions arrive in — and it fired on English sentences that asked
one thing. A vocabulary list is not a parser; it is the old router's habit
of matching words where meaning was needed.

Dividing a sentence into its asks is what a language model is genuinely
for, and it is safe here because the model never touches a number: it
returns text, each piece goes through the full pipeline, and every figure
still comes from SQL. What the model must not be trusted with is checked
in code, below:

  * a part in a different script than the question was asked in — the
    model translating instead of splitting, which changes which language's
    stored values the part will reach
  * more parts than a sentence can hold, or parts far longer in total than
    the original — invention rather than division
  * anything at all going wrong — an unavailable model, unparseable
    output, an empty list. Every one of those falls back to treating the
    question as atomic, which is what the system did before this file
    existed. A decomposer that is down costs the compound case; it must
    never cost the simple one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from src.llm.client import call_llm

MAX_PARTS = 5


@dataclass
class Decomposition:
    """The asks found in one question. One part means it was atomic."""

    parts: list[str] = field(default_factory=list)
    available: bool = True
    reason: str = ""

    @property
    def compound(self) -> bool:
        return len(self.parts) > 1


SYSTEM = """You split a question into the separate figures it asks for.

Return ONLY this JSON:

{"parts": ["<question>", "<question>"]}

Rules:
1. One part per figure the asker wants back. If the question asks for one
   figure, return exactly one part: the question itself, unchanged.
2. Each part must stand alone. Carry shared context into every part --
   "how many cases in Lahore, and how many of those had a weapon" becomes
   "How many cases are in Lahore?" and "How many cases in Lahore had a
   weapon?". A part that depends on reading another part is not a part.
3. Write every part in the SAME language and script as the question. Do
   not translate. An Urdu question produces Urdu parts.
4. Split only what was asked. Never add a figure the asker did not ask
   for, however natural it would be to include.
5. A single figure that happens to carry several conditions is ONE part.
   "cases in Lahore with a weapon in 2024" is one question, not three."""


_JSON = re.compile(r"\{.*\}", re.S)
_ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
_LATIN = re.compile(r"[A-Za-z]")
_WORD = re.compile(r"\S+")

# Sentence and clause terminators across the scripts this corpus uses:
# ASCII, and Urdu's own question mark, full stop and comma.
_BREAKS = re.compile("[?؟.۔,،;؛&]")


def _script(text: str) -> str:
    """Which writing system a string is mostly in.

    Not which language — the distinction that matters here is whether a
    part is still in the script the question was asked in, because that
    decides which language's stored values it will reach.
    """
    arabic = len(_ARABIC.findall(text))
    latin = len(_LATIN.findall(text))
    if arabic and arabic >= latin:
        return "arabic"
    if latin:
        return "latin"
    return "neutral"


def looks_compound(question: str) -> bool:
    """A cheap, script-level reason to ask the model at all.

    This gate only decides whether to SPEND a model call; it never decides
    that a question is compound. So it is deliberately permissive, and it
    reads punctuation and length rather than vocabulary — a word list would
    be blind to whichever language it was not written in, which is the bug
    this module replaces.
    """
    q = (question or "").strip()
    if not q:
        return False
    words = len(_WORD.findall(q))
    trimmed = q.rstrip("?؟.۔ ")
    return words >= 8 or len(_BREAKS.findall(trimmed)) >= 1


async def decompose(question: str,
                    provider: str | None = None) -> Decomposition:
    """Return the asks in `question`. One part means answer it as it is."""
    whole = (question or "").strip()
    atomic = Decomposition(parts=[whole] if whole else [])

    if not whole or not looks_compound(whole):
        return atomic

    try:
        raw = await call_llm(SYSTEM, whole, temperature=0.0, max_tokens=1000,
                             provider_override=provider,
                             force_cloud=provider is not None)
    except Exception as exc:                            # noqa: BLE001
        # Unavailable is not "atomic" — it is unknown. The question is
        # still answered whole, but the ledger is told, so a compound
        # question answered in part is never reported as answered in full.
        atomic.available = False
        atomic.reason = f"model unavailable: {exc}"[:150]
        return atomic

    m = _JSON.search(raw or "")
    if not m:
        atomic.available = False
        atomic.reason = "no parseable split"
        return atomic
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        atomic.available = False
        atomic.reason = "malformed split"
        return atomic

    found = obj.get("parts")
    if not isinstance(found, list) or not found:
        return atomic

    parts = [str(p).strip() for p in found if str(p or "").strip()]
    if len(parts) <= 1:
        return atomic

    # ── What the model is not trusted with ──────────────────────────────
    if len(parts) > MAX_PARTS:
        atomic.reason = f"{len(parts)} parts is not a split, it is invention"
        return atomic

    want = _script(whole)
    if want != "neutral":
        for p in parts:
            if _script(p) not in (want, "neutral"):
                # Translating instead of splitting. An English part of an
                # Urdu question searches English values and reports a
                # figure drawn from half the corpus.
                atomic.reason = "split changed script; treated as one ask"
                return atomic

    # Parts legitimately restate shared context, so they run longer than
    # the original in total — but not several times over.
    if sum(len(p) for p in parts) > max(len(whole) * 4, 400):
        atomic.reason = "split expanded the question rather than dividing it"
        return atomic

    return Decomposition(parts=parts[:MAX_PARTS])
