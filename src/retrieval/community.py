"""Thematic questions, answered from precomputed community summaries.

THE GAP THIS CLOSES

The old pipeline had XNETWORK: open-ended pattern and modus-operandi
questions — "what themes recur across these cases" — answered by
embedding search over a separate Chroma collection of GraphRAG-style
community summaries, each describing one cluster of related entities.

Nothing in the rebuilt path did this. Every other capability of
`cross_case_linkage.py` has an equivalent (entity recurrence is a GROUP BY
the agent writes unaided); this one did not, and it was the single reason
the old pipeline could not yet be deleted.

WHY A SUMMARY IS NOT EVIDENCE

Everything else in this system resolves to characters in a source
document. A community report does not: it is prose a model wrote about a
cluster, and re-deriving it would give slightly different prose. It cannot
be cited to an offset and it must never be treated as though it could.

So the rules here are narrower than anywhere else in retrieval:

  * A summary may DESCRIBE. It may never supply a FIGURE. Counting
    clusters by reading summaries is the counting-by-reading failure in
    its purest form, and any number in a thematic answer still comes from
    SQL over rows.
  * Every summary carries the case ids it was built from, so a claim can
    be taken back to real cases and checked there.
  * Coverage is stated on every single use (below), never assumed.

COVERAGE IS THE WHOLE PROBLEM

Measured today: 18 reports, citing 20 cases, in a corpus of 73.

    covered   20
    uncovered 53   (73%)

A thematic answer drawn from those reads as though it describes the
corpus. It describes a quarter of it. That is not a caveat to add if
there is room — a pattern claim over 27% of the data, presented whole, is
wrong in the way that matters, so coverage is computed on every call and
recorded at IMPORTANT severity. The investigator is told what fraction
the answer rests on, in plain words, before they act on it.

The reports also go stale silently: they are built once, and ingestion
runs again. Cases they cite that no longer exist are reported too, because
a summary about deleted cases is worse than no summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg

from src.retrieval.health import Ledger, Severity


@dataclass
class Coverage:
    """How much of the corpus the community summaries actually describe."""

    reports: int = 0
    cited_cases: int = 0
    live_cases: int = 0
    dangling: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        if not self.live_cases:
            return 0.0
        return self.cited_cases / self.live_cases

    @property
    def usable(self) -> bool:
        return self.reports > 0 and self.cited_cases > 0

    def sentence(self) -> str:
        """What the investigator is told, in words, not percentages alone."""
        if not self.usable:
            return ("I have no cross-case pattern summaries for this "
                    "corpus, so I cannot answer questions about themes "
                    "across cases — only about individual cases.")
        missing = self.live_cases - self.cited_cases
        out = (f"This pattern answer is based on {self.cited_cases} of "
               f"{self.live_cases} cases ({self.fraction:.0%}); "
               f"{missing} cases are not covered by any summary, so a "
               f"pattern that appears only in those would be missed.")
        if self.dangling:
            out += (f" {len(self.dangling)} summaries also refer to cases "
                    f"that are no longer in the database.")
        return out


@dataclass
class CommunityHit:
    """One cluster summary, with the cases it was built from."""

    community_id: str
    text: str
    case_ids: list[str]
    member_count: int
    distance: float

    def brief(self, limit: int = 240) -> str:
        body = " ".join((self.text or "").split())
        return body[:limit] + ("..." if len(body) > limit else "")


async def coverage(conn: asyncpg.Connection) -> Coverage:
    """What fraction of the live corpus the summaries describe."""
    out = Coverage()
    try:
        from src.retrieval.community_vector_store import _get_collection
        col = _get_collection()
        got = col.get(include=["metadatas"])
    except Exception:                                   # noqa: BLE001
        return out

    metadatas = got.get("metadatas") or []
    out.reports = len(metadatas)
    cited: set[str] = set()
    for md in metadatas:
        for cid in str((md or {}).get("case_ids", "")).split(","):
            cid = cid.strip()
            if cid:
                cited.add(cid)

    live = {r["case_id"] for r in
            await conn.fetch("SELECT case_id FROM rebuild_cases")}
    out.live_cases = len(live)
    out.cited_cases = len(cited & live)
    out.dangling = sorted(cited - live)[:10]
    return out


async def search(conn: asyncpg.Connection, question: str, limit: int = 4,
                 ledger: Ledger | None = None) -> tuple[list[CommunityHit],
                                                        Coverage]:
    """Cluster summaries relevant to a thematic question, with coverage.

    Coverage comes back with the hits rather than being available
    separately, so a caller cannot use the summaries without having been
    handed the fraction of the corpus they represent.
    """
    cov = await coverage(conn)
    if not cov.usable:
        if ledger:
            ledger.record(
                "cross-case patterns", Severity.IMPORTANT,
                f"{cov.reports} community summaries, "
                f"{cov.cited_cases} cases cited",
                cov.sentence())
        return [], cov

    try:
        from src.retrieval.community_vector_store import (
            query_similar_communities,
        )
        rows = await query_similar_communities(question, top_k=limit)
    except Exception as exc:                            # noqa: BLE001
        if ledger:
            ledger.record(
                "cross-case patterns", Severity.IMPORTANT, str(exc)[:200],
                "I could not search the cross-case pattern summaries, so "
                "this answer covers the cases you asked about but not "
                "patterns spanning others")
        return [], cov

    # query_similar_communities flattens the metadata itself and returns
    # {community_id, summary_text, case_ids, member_count, distance}.
    hits: list[CommunityHit] = []
    for r in rows:
        hits.append(CommunityHit(
            community_id=str(r.get("community_id") or ""),
            text=r.get("summary_text") or "",
            case_ids=[c.strip() for c in (r.get("case_ids") or [])
                      if str(c).strip()],
            member_count=int(r.get("member_count") or 0),
            distance=float(r.get("distance") or 0.0),
        ))

    # Recorded on EVERY successful use, not only on failure. A pattern
    # answer over 27% of the corpus is the failure mode here, and it looks
    # exactly like a pattern answer over all of it.
    if ledger and hits:
        ledger.record(
            "cross-case patterns", Severity.IMPORTANT,
            f"{cov.cited_cases}/{cov.live_cases} cases covered by "
            f"{cov.reports} summaries",
            cov.sentence())
    return hits, cov


def as_hint(hits: list[CommunityHit], cov: Coverage) -> str:
    """What the agent is shown, with the summaries' status made explicit.

    The agent is told in the same breath that these are derived text and
    that a number may not come from them. Stating it here rather than in
    the system prompt keeps the constraint next to the data it constrains,
    which is the only place it reliably survives.
    """
    if not hits:
        return ""
    lines = [
        "Cross-case pattern summaries (DERIVED TEXT, not evidence — these "
        "were written by a model about clusters of related records):",
        f"  {cov.sentence()}",
        "  You may quote or paraphrase these to describe a pattern. You "
        "may NOT take any number from them: every figure still comes from "
        "SQL over rows. Each summary lists the cases it was built from — "
        "query those to check anything it claims.",
        "",
    ]
    for h in hits:
        cases = ", ".join(h.case_ids[:4]) or "unknown"
        lines.append(f"  [{h.community_id}] cases: {cases} "
                     f"({h.member_count} members)")
        lines.append(f"      {h.brief()}")
    return chr(10).join(lines)
