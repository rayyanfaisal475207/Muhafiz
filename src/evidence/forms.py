"""Every term in a question, written every way these records might write it.

WHY THIS LAYER EXISTS

The reader produced a term and whatever variants the model happened to think
of, and that was the whole expansion. Two failures followed from it.

MEASURED, and the reason this module exists: asked "how many complainants
are recorded", the plan filtered on the predicate `complainant of` -- the
correct plan, the one that returns 71 -- and the run was rejected eight
times because nothing connected the plural "complainants" to the singular
"complainant". The model simply had not emitted the singular that run. It
emitted it on other runs, which is why the same question answered sometimes
and failed sometimes.

Leaving morphology to whatever a model feels like emitting is the bug. A
plural is not a judgement call.

GENERATE LIBERALLY, LET THE CORPUS DECIDE

Nothing here claims a form is real. Every form produced is a CANDIDATE,
handed to `ScopedStore.ground_forms`, and only forms that actually occur in
the records survive. So a wrong candidate costs nothing but a row in a
count, while a missing candidate costs an answer. That asymmetry is why this
generates eagerly.

THESE ARE RULES OF LANGUAGE, NOT A VOCABULARY

What is encoded below is how words inflect -- how a plural is formed, how it
is undone -- and it applies to any word, including words this corpus has
never contained and words no author here has seen. It is not a synonym list,
not a glossary, and not a table of police terms. No domain vocabulary is
stored, and nothing needs editing when the records change: that distinction
is the whole point, because a stored vocabulary rots and this does not.

BOUNDED ON PURPOSE

MEASURED: a question producing 31 forms cancelled the grounding query at the
statement timeout, and every form was then dropped as unconfirmed. So
expansion is capped per term and in total. More candidates stop being free
at the point where checking them fails.
"""
from __future__ import annotations

import os
import re

# Urdu/Arabic script. Used to decide which inflection rules can apply at all:
# an Urdu word does not take an English plural and vice versa.
URDU = re.compile(r"[؀-ۿ]")
SPLIT = re.compile(r"[^0-9A-Za-z؀-ۿ]+")

# Common Urdu plural and oblique endings. Removing one of these is a guess
# that the corpus then confirms or discards, exactly like every other
# candidate here.
URDU_ENDINGS = ("وں", "یں", "ات", "ان",
                "ہے", "ے")


def _is_urdu(word: str) -> bool:
    return bool(URDU.search(word or ""))


def _english_inflections(word: str) -> list[str]:
    """A Latin-script word's plural and singular, each derived from the other.

    Both directions, because the question may be plural and the record
    singular ("complainants" against `complainant of`) or the other way
    about ("station" against "police stations").
    """
    low = word.lower()
    out: list[str] = []
    if len(low) < 3:
        return out

    # THREE THINGS ARE NOT PLURALISED, BECAUSE THE RESULT IS ALWAYS JUNK.
    #
    # A junk candidate is not free: it occupies a row in the grounding query
    # that is already capable of timing out, and it displaces a real form
    # from the budget. MEASURED before this guard: "complainants" produced
    # "complainantses", "FIRs" produced "firses", "registered" produced
    # "registereds" and "June 2026" produced "june 2026s" -- four wasted
    # rows out of six generated.
    already_plural = low.endswith("s") and not low.endswith("ss")
    a_participle = low.endswith(("ed", "ing"))
    has_a_number = any(c.isdigit() for c in low)

    if not (already_plural or a_participle or has_a_number):
        # Towards the plural.
        if low.endswith("y") and len(low) > 3 and low[-2] not in "aeiou":
            out.append(low[:-1] + "ies")
        elif low.endswith(("ss", "x", "z", "ch", "sh")):
            # "witness" is not already plural -- it takes "es", not a third s.
            out.append(low + "es")
        elif low.endswith("fe"):
            out.append(low[:-2] + "ves")
        elif low.endswith("f"):
            out.append(low[:-1] + "ves")
        else:
            out.append(low + "s")

    # Towards the singular. Each is only a candidate; the corpus decides.
    if low.endswith("ies") and len(low) > 4:
        out.append(low[:-3] + "y")
    if low.endswith("ves") and len(low) > 4:
        out.extend([low[:-3] + "f", low[:-3] + "fe"])
    if low.endswith("es") and len(low) > 3:
        out.append(low[:-2])
    if low.endswith("s") and not low.endswith("ss") and len(low) > 3:
        out.append(low[:-1])
    return out


def _urdu_inflections(word: str) -> list[str]:
    """An Urdu word with a plural or oblique ending removed, and with one added.

    Urdu marks plurals and oblique cases with endings that English rules
    cannot touch, and the records are written in both scripts, so a question
    in one script must still be able to reach a record in the other.
    """
    out: list[str] = []
    for ending in URDU_ENDINGS:
        if word.endswith(ending) and len(word) > len(ending) + 1:
            out.append(word[: -len(ending)])
    if len(word) > 2:
        out.extend(word + ending for ending in URDU_ENDINGS[:2])
    return out


def inflections(word: str) -> list[str]:
    """Every inflection of one word that this corpus might be written with."""
    word = (word or "").strip()
    if not word:
        return []
    found = _urdu_inflections(word) if _is_urdu(word) else _english_inflections(word)
    return [f for f in dict.fromkeys(found) if f and f != word]


def pieces(phrase: str) -> list[str]:
    """A phrase's own words and its contiguous runs of words.

    "police station" must be able to reach a record that writes only
    "station", or one that writes "police station house". The runs are kept
    because a two-word phrase is a better retrieval form than either word
    alone when the records do use it -- and `ground_forms` ranks by
    occurrence, so the one the corpus actually uses rises.
    """
    words = [w for w in SPLIT.split(phrase or "") if w]
    if len(words) < 2:
        return []
    out: list[str] = list(words)
    for size in range(2, min(len(words), 4) + 1):
        for start in range(len(words) - size + 1):
            run = " ".join(words[start:start + size])
            if run != phrase:
                out.append(run)
    return [p for p in dict.fromkeys(out) if p and p != phrase]


def expand(term: str, *, per_term: int | None = None) -> list[str]:
    """One term, written every way worth trying, the term itself first.

    The order matters: the term as the reader gave it stays at the front, so
    a caller that has to truncate keeps the reading the model actually meant
    and loses only the speculative tail.
    """
    term = (term or "").strip()
    if not term:
        return []
    cap = per_term if per_term is not None else int(
        os.getenv("EVIDENCE_FORMS_PER_TERM", "10"))
    out: list[str] = [term]
    out.extend(inflections(term))
    for piece in pieces(term):
        out.append(piece)
        out.extend(inflections(piece))
    unique = [f for f in dict.fromkeys(out) if f and len(f) > 1]
    return unique[:max(1, cap)]


def expand_all(terms: list[str], *, total: int | None = None,
               per_term: int | None = None) -> list[str]:
    """Several terms expanded together, bounded overall.

    Round-robin rather than term by term, so a question with one wordy term
    and three plain ones does not spend the whole budget on the first. Every
    original term survives truncation; only the derived forms compete.
    """
    originals = [t.strip() for t in (terms or []) if t and t.strip()]
    if not originals:
        return []
    cap = total if total is not None else int(
        os.getenv("EVIDENCE_FORMS_TOTAL", "48"))
    expansions = [expand(t, per_term=per_term) for t in originals]
    # The terms as given come first and are never dropped.
    out: list[str] = list(dict.fromkeys(originals))
    depth = 1
    while len(out) < cap:
        added = False
        for forms in expansions:
            if depth < len(forms):
                added = True
                if forms[depth] not in out:
                    out.append(forms[depth])
                    if len(out) >= cap:
                        break
        if not added:
            break
        depth += 1
    return out[:max(len(dict.fromkeys(originals)), cap)]
