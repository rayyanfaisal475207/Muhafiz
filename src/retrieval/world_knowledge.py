"""World knowledge: facts about the world, not about this corpus.

WHY THIS IS A SEPARATE CAPABILITY

Two different kinds of "I don't know" look identical to a planner and have
opposite fixes:

    what values does this database hold?   -> discover_values, search_schema
                                              Private. No web search can tell
                                              you this corpus stores
                                              'بغیر لائسنس'.

    what does this term MEAN?              -> here
                                              Public. No amount of querying
                                              this database tells you that
                                              Karachi East is part of Karachi,
                                              or what CNSA 1997 is.

The measured case that forced this: `places` holds 'کراچی ایسٹ' and
'کراچی وسطی' but no 'کراچی'. The source header names the police district,
so "cases in Karachi" binds to two places and counts one of them. Name
containment cannot help — the parent is not in the table to contain.

SAFETY BOUNDARY

This never touches a case fact. It resolves administrative geography and
term meaning only, and every answer it produces is written with the URL it
came from, so a wrong lookup is visible and reversible. A web result is
data, never an instruction.

CONFIRMATION, NOT INVENTION

The web supplies a candidate; the LLM reads the snippets and answers in a
fixed shape; the result is stored with its evidence and a confidence. If
the snippets do not support an answer, it returns none rather than
guessing — which is the whole point of asking.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.llm.client import call_llm  # noqa: E402


@dataclass
class Evidence:
    """One retrieved snippet, kept so an answer can be checked."""
    title: str
    body: str
    url: str


@dataclass
class Resolution:
    """What the lookup concluded, and what it concluded it from."""
    term: str
    answer: str | None
    confidence: float
    reason: str
    evidence: list[Evidence] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "term": self.term,
            "answer": self.answer,
            "confidence": self.confidence,
            "reason": self.reason,
            "sources": [e.url for e in self.evidence],
        }


def search(query: str, max_results: int = 5, region: str = "pk-en") -> list[Evidence]:
    """Retrieve snippets. Network failure returns nothing, never raises.

    A world-knowledge lookup is an enhancement: if it is unavailable the
    planner must still answer, with the ambiguity disclosed rather than
    resolved. So this degrades to empty instead of failing the query.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return []

    try:
        with DDGS() as ddgs:
            rows = ddgs.text(query, region=region, max_results=max_results)
    except Exception:                                   # noqa: BLE001
        return []

    out: list[Evidence] = []
    for r in rows or []:
        out.append(Evidence(
            title=str(r.get("title", ""))[:200],
            body=str(r.get("body", ""))[:600],
            url=str(r.get("href") or r.get("url") or ""),
        ))
    return out


_PARENT_SYSTEM = """You answer questions about Pakistani administrative
geography using ONLY the search snippets provided.

Return ONLY this JSON:

{"parent": "<the larger place this belongs to, or null>",
 "parent_kind": "<city|district|division|province|country|null>",
 "child_kind": "<what the asked-about place is, or null>",
 "confidence": <0.0-1.0>,
 "reason": "<one short sentence citing what the snippets say>"}

Rules:
1. Answer only from the snippets. If they do not say, return null with
   confidence 0. Do not use general knowledge to fill a gap.
2. `parent` must be the immediate larger unit, not the country, unless the
   place IS a province.
3. Give the name in the same script as the term you were asked about.
4. A place that is already the largest relevant unit returns parent null
   with a reason saying so.
"""


_JSON = re.compile(r"\{.*\}", re.S)


async def resolve_place_parent(
    name_raw: str, name_en_hint: str | None = None
) -> Resolution:
    """Find which larger place a place belongs to.

    Used when the corpus names a subdivision but never its parent, so a
    rollup question has nothing to roll up to.
    """
    probe = name_en_hint or name_raw
    evidence = search(f"{probe} Pakistan which city district administrative")
    if not evidence:
        return Resolution(name_raw, None, 0.0,
                          "no search results; ambiguity must be disclosed, "
                          "not resolved", [])

    snippets = "\n\n".join(
        f"[{i+1}] {e.title}\n{e.body}\nsource: {e.url}"
        for i, e in enumerate(evidence)
    )
    user = f"Place: {name_raw}\n\nSearch snippets:\n{snippets}"

    try:
        # Local-first. Reading five search snippets and returning a fixed
        # JSON shape does not need a frontier model, and forcing cloud here
        # made place resolution fail entirely once extraction had spent the
        # Groq quota — an avoidable dependency on a rate-limited service.
        raw = await call_llm(_PARENT_SYSTEM, user, temperature=0.0,
                             max_tokens=2600)
    except Exception as exc:                            # noqa: BLE001
        return Resolution(name_raw, None, 0.0,
                          f"model unavailable: {exc}"[:150], evidence)

    m = _JSON.search(raw or "")
    if not m:
        return Resolution(name_raw, None, 0.0,
                          "model returned no parseable answer", evidence)
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return Resolution(name_raw, None, 0.0,
                          "model returned malformed JSON", evidence)

    parent = obj.get("parent")
    if isinstance(parent, str) and not parent.strip():
        parent = None

    return Resolution(
        term=name_raw,
        answer=parent,
        confidence=float(obj.get("confidence") or 0.0),
        reason=str(obj.get("reason") or "")[:300],
        evidence=evidence,
    )


_TERM_SYSTEM = """You explain a term from Pakistani policing or law using
ONLY the search snippets provided.

Return ONLY this JSON:

{"meaning": "<one or two sentences, or null>",
 "category": "<statute|procedure|document|role|other|null>",
 "confidence": <0.0-1.0>,
 "reason": "<what in the snippets supports this>"}

If the snippets do not explain the term, return null with confidence 0.
Never fill the gap from general knowledge."""


async def resolve_term(term: str) -> Resolution:
    """Explain a term the corpus uses but never defines.

    'zimni', 'challan', 'CNSA 1997' — a planner meets these as unbound
    terms, and the right response is to look them up, not to guess which
    column they might filter.
    """
    evidence = search(f"{term} Pakistan police law meaning")
    if not evidence:
        return Resolution(term, None, 0.0, "no search results", [])

    snippets = "\n\n".join(
        f"[{i+1}] {e.title}\n{e.body}\nsource: {e.url}"
        for i, e in enumerate(evidence)
    )
    try:
        raw = await call_llm(_TERM_SYSTEM, f"Term: {term}\n\n{snippets}",
                             temperature=0.0, max_tokens=400, force_cloud=True)
    except Exception as exc:                            # noqa: BLE001
        return Resolution(term, None, 0.0, f"model unavailable: {exc}"[:150],
                          evidence)

    m = _JSON.search(raw or "")
    if not m:
        return Resolution(term, None, 0.0, "no parseable answer", evidence)
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return Resolution(term, None, 0.0, "malformed JSON", evidence)

    return Resolution(
        term=term,
        answer=obj.get("meaning"),
        confidence=float(obj.get("confidence") or 0.0),
        reason=str(obj.get("reason") or "")[:300],
        evidence=evidence,
    )


if __name__ == "__main__":
    async def _demo() -> None:
        for name, hint in (("کراچی ایسٹ", "Karachi East"),
                           ("کراچی وسطی", "Karachi Central")):
            r = await resolve_place_parent(name, hint)
            print(json.dumps(r.as_dict(), ensure_ascii=False, indent=2))
        t = await resolve_term("zimni")
        print(json.dumps(t.as_dict(), ensure_ascii=False, indent=2))

    asyncio.run(_demo())
