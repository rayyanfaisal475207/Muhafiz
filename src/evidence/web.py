"""Readings for a term the model could not resolve, from an open web search.

WHAT THIS IS FOR

The understanding step reads each term of a question into the forms the
records might use. When it cannot -- an unfamiliar place, a statute short
name, a word it has no reading for -- the term lands in
`QueryUnderstanding.unresolved` and retrieval simply goes without it. That
list was recorded in the trace and consumed by nothing.

This asks the open web for candidate readings of such a term, so a word the
model does not know can still reach records that use a different spelling or
a different script.

WHAT LEAVES THE MACHINE, AND WHAT CANNOT

Only a term that is ALREADY UNRESOLVED. That is the protection, and it
follows from the pipeline rather than from a rule bolted on top: a term the
records contain resolves against the corpus and never reaches this module,
so a name that is in the case files cannot be sent. No span, no document
title, no entity label, no case identifier and no part of the question
beyond the single unknown word is included in a request.

Every outbound query is recorded in the trace exactly as sent, so what was
disclosed is auditable after the fact rather than taken on trust.

WHY NOTHING IT RETURNS IS BELIEVED

The web is not evidence about these records. Anything returned here is a
CANDIDATE SPELLING and nothing more: the caller checks each one against the
corpus with `ground_forms` and keeps only forms that actually occur. A
reading the records do not use is discarded, so a wrong or mischievous
search result cannot introduce a term, change an answer or add a citation --
at worst it is dropped, and at best it bridges a spelling the model missed.

It is off unless switched on, because it sends data to a third party.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

logger = logging.getLogger("evidence.web")

ENDPOINT = "https://api.duckduckgo.com/"
# Urdu/Arabic script, and Latin words long enough to be a term rather than
# a fragment. Devanagari is excluded here for the same reason it is excluded
# from the model's own readings: these records are not written in it.
URDU = re.compile(r"[؀-ۿ][؀-ۿ\s]{1,40}")
LATIN = re.compile(r"\b[A-Z][A-Za-z'-]{2,30}(?:\s+[A-Z][A-Za-z'-]{2,30}){0,3}\b")


def enabled() -> bool:
    """Whether a term may be sent to the open web. Off unless switched on."""
    return os.getenv("EVIDENCE_WEB_LOOKUP", "false").strip().lower() == "true"


def _candidates(payload: dict) -> list[str]:
    """Readings suggested by one response, longest first."""
    texts: list[str] = []
    for key in ("AbstractText", "Definition", "Heading"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value)
    for topic in (payload.get("RelatedTopics") or [])[:8]:
        if isinstance(topic, dict) and isinstance(topic.get("Text"), str):
            texts.append(topic["Text"])
    found: list[str] = []
    for text in texts:
        found.extend(match.group(0).strip() for match in URDU.finditer(text))
        found.extend(match.group(0).strip() for match in LATIN.finditer(text))
    # Longest first: a fuller phrase is a better retrieval form than the
    # single word it contains, and the caller keeps only what occurs anyway.
    unique = sorted({f for f in found if 2 < len(f) <= 60}, key=len, reverse=True)
    return unique[:12]


async def readings_for(term: str, *, timeout: float = 6.0,
                       transport=None) -> tuple[list[str], list[str]]:
    """Candidate spellings for one unresolved term.

    Returns the candidates and the queries that were sent, so the caller can
    record exactly what was disclosed. Never raises: an unreachable network
    means a term stays unresolved, which is where it started.
    """
    term = (term or "").strip()
    if not term or len(term) > 60 or not enabled():
        return [], []
    # Two askings: the term itself, and the term as a translation request,
    # because a reading in the other script is what retrieval is missing.
    queries = [term, f"{term} meaning in Urdu"]
    out: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            for query in queries:
                try:
                    response = await client.get(
                        ENDPOINT,
                        params={"q": query, "format": "json", "no_html": "1",
                                "no_redirect": "1", "skip_disambig": "1"},
                        headers={"User-Agent": "Muhafiz/1.0 (+evidence term lookup)"})
                    if response.status_code != 200:
                        continue
                    out.extend(_candidates(response.json()))
                except (httpx.HTTPError, ValueError):
                    continue
    except Exception:                                    # noqa: BLE001
        logger.debug("web lookup failed", exc_info=True)
        return [], queries
    # The term itself is not a new reading.
    lowered = term.lower()
    unique = [c for c in dict.fromkeys(out) if c.lower() != lowered]
    return unique[:12], queries


async def readings_for_all(terms: list[str], *, limit: int = 4,
                           timeout: float = 6.0,
                           transport=None) -> tuple[dict[str, list[str]], list[str]]:
    """Candidate spellings for several unresolved terms, bounded.

    Bounded deliberately: a question with a dozen unknown words is a question
    the reader did not understand, and asking the web twelve times will not
    repair that -- it only widens what was disclosed.
    """
    wanted = [t for t in dict.fromkeys(terms or []) if t and t.strip()][:limit]
    if not wanted or not enabled():
        return {}, []
    results = await asyncio.gather(
        *(readings_for(t, timeout=timeout, transport=transport) for t in wanted),
        return_exceptions=True)
    readings: dict[str, list[str]] = {}
    sent: list[str] = []
    for term, result in zip(wanted, results):
        if isinstance(result, BaseException):
            continue
        candidates, queries = result
        sent.extend(queries)
        if candidates:
            readings[term] = candidates
    return readings, sent
