"""Text retrieval over the rebuilt chunks: lexical, vector, and their fusion.

WHY THIS EXISTS SEPARATELY FROM SQL

SQL answers what is countable. It cannot answer "describe the incident in
FIR 201/26" or find a fact nobody extracted into a column. Those need the
text — and the text needs to come back with a span, so a claim made from it
can be checked.

EVERY RESULT CARRIES A SPAN

A passage without provenance is a claim. rebuild_chunks.span_id points at
exact character offsets in the document, verified to round-trip, so a
retrieved passage can always be traced to the source it came from.

THE EMBEDDINGS ARE NOT RECOMPUTED

Stage A preserved the original chunk ids, and the existing Chroma
collection already holds 7,716 vectors keyed by those same ids. So vector
search reuses what is there and maps results back through rebuild_chunks to
spans. Re-embedding 712 chunks to get vectors that already exist would be
work for its own sake, and at corpus scale it is the difference between
minutes and days.

FUSION, AND WHY RRF

Lexical and vector scores are not on the same scale and cannot be added.
Reciprocal rank fusion combines them by POSITION instead, which needs no
calibration between two incomparable numbers — and calibration is exactly
what was measured to be unavailable here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg


RRF_K = 60          # standard damping; rank 1 contributes 1/61, rank 2 1/62


@dataclass
class Passage:
    chunk_id: str
    case_id: str | None
    doc_id: str
    span_id: int | None
    text: str
    section_path: str | None
    lexical_rank: int | None = None
    vector_rank: int | None = None
    score: float = 0.0

    @property
    def cited(self) -> bool:
        """A passage with no span cannot support a claim."""
        return self.span_id is not None

    def brief(self, width: int = 110) -> str:
        body = " ".join((self.text or "").split())
        return body[:width] + ("…" if len(body) > width else "")


async def lexical_search(conn: asyncpg.Connection, terms: list[str],
                         case_id: str | None = None,
                         limit: int = 20) -> list[Passage]:
    """Full-text search, falling back to trigram when nothing matches.

    Two passes because they fail differently: the tsvector match is exact
    on tokens and misses morphological variants ('گرفتار' vs 'گرفتاری'),
    while trigram catches those but ranks noisily. Running the precise one
    first and only widening when it returns nothing keeps precision where
    precision is available.
    """
    if not terms:
        return []
    # websearch_to_tsquery parses untrusted text safely. Building a tsquery
    # by joining tokens with '|' fails on anything containing punctuation —
    # measured: "201/26" and "2026-03-28T00:00:00Z" both raise a syntax
    # error, and those are exactly the tokens an FIR question contains.
    # " OR " is websearch syntax for disjunction.
    cleaned = [t.strip() for t in terms if t and t.strip()]
    if not cleaned:
        return []
    query = " OR ".join(cleaned[:20])

    rows = await conn.fetch(
        """
        SELECT rc.chunk_id, rc.case_id, rc.doc_id, rc.span_id, rc.text,
               rc.section_path,
               ts_rank(rc.tsv, q) AS rank
        FROM rebuild_chunks rc, websearch_to_tsquery('simple', $1) q
        WHERE rc.tsv @@ q
          AND ($2::text IS NULL OR rc.case_id = $2)
        ORDER BY rank DESC
        LIMIT $3
        """, query, case_id, limit)

    if not rows:
        joined = " ".join(terms)[:200]
        rows = await conn.fetch(
            """
            SELECT rc.chunk_id, rc.case_id, rc.doc_id, rc.span_id, rc.text,
                   rc.section_path,
                   similarity(rc.text, $1) AS rank
            FROM rebuild_chunks rc
            -- asyncpg binds with $n, so % is the trigram operator here,
            -- not a format placeholder needing escape.
            WHERE rc.text % $1
              AND ($2::text IS NULL OR rc.case_id = $2)
            ORDER BY rank DESC
            LIMIT $3
            """, joined, case_id, limit)

    return [
        Passage(chunk_id=r["chunk_id"], case_id=r["case_id"],
                doc_id=r["doc_id"], span_id=r["span_id"], text=r["text"],
                section_path=r["section_path"], lexical_rank=i + 1)
        for i, r in enumerate(rows)
    ]


async def vector_search(query_text: str, limit: int = 20,
                        case_id: str | None = None,
                        collection: str = "muhafiz_kb",
                        ledger=None,
                        cases_only: bool = False) -> list[tuple[str, float]]:
    """Nearest chunks by embedding, from the collection that already exists.

    The query MUST be embedded by the same model that built the index.
    Measured: letting Chroma embed the query itself raised

        Collection expecting embedding with dimension of 1024, got 384

    because the collection holds e5 vectors from the project's model server
    while Chroma's built-in encoder produces 384 dimensions. The earlier
    version swallowed that exception and reported "no results", which reads
    as "nothing matched" — a silent failure, not a visible one.

    Returns (chunk_id, distance). A failure still degrades to empty rather
    than raising: vector search is one signal among several, and losing it
    must weaken the answer, not fail the request.
    """
    try:
        import chromadb
        from src.retrieval.embedder import embed_text
    except ImportError as exc:
        if ledger:
            ledger.vector_unavailable(f"library missing: {exc}")
        return []

    try:
        vector = await embed_text(query_text, task_type="RETRIEVAL_QUERY")
    except Exception as exc:                            # noqa: BLE001
        # Reported, not swallowed. An empty result here used to reach the
        # user as "nothing matched", which is a different statement about
        # the world than "we could not look".
        if ledger:
            ledger.embedder_unavailable(str(exc)[:200])
        return []
    if not vector:
        if ledger:
            ledger.embedder_unavailable("embedding service returned nothing")
        return []

    try:
        client = chromadb.PersistentClient(path="data/chroma_db")
        col = client.get_collection(collection)
        # ── Keep the statute library out of case questions ─────────
        #
        # The collection holds 712 case chunks and 7,004 chunks of
        # statutes and police rules. The statutes are in ENGLISH; the
        # case narratives are in URDU. So an English question lands on
        # the statutes almost every time.
        #
        # MEASURED, "what methods were used to commit fraud" returned
        # five results, all with case_id = None:
        #
        #   "A is accused of fraudulently delivering..."   Police Rules
        #   "477 | Fraudulently destroying or defacing..." PPC table
        #
        # Correct law, useless answer: the question was about these
        # cases. The reference library was drowning the evidence.
        #
        # `has_case` already distinguishes them in the metadata, so this
        # is a filter rather than a new index. Statutes stay reachable —
        # a question ABOUT the law still searches them — but a question
        # about cases is not answered from a rulebook.
        where: dict | None = None
        if case_id:
            where = {"case_id": case_id}
        elif cases_only:
            where = {"has_case": True}
        res = col.query(
            query_embeddings=[vector], n_results=limit, where=where)
    except Exception as exc:                            # noqa: BLE001
        if ledger:
            ledger.vector_unavailable(str(exc)[:200])
        return []

    ids = (res.get("ids") or [[]])[0]
    dists = (res.get("distances") or [[]])[0] or [0.0] * len(ids)
    return list(zip(ids, dists))


async def hydrate(conn: asyncpg.Connection,
                  chunk_ids: list[str]) -> dict[str, Passage]:
    """Map chunk ids back to rebuilt chunks, and therefore to spans.

    A vector hit that cannot be resolved to a span is dropped. It may be a
    legacy chunk that ingestion has not rebuilt, and an unciteable passage
    is not evidence.
    """
    if not chunk_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT chunk_id, case_id, doc_id, span_id, text, section_path
        FROM rebuild_chunks WHERE chunk_id = ANY($1)
        """, chunk_ids)
    return {
        r["chunk_id"]: Passage(
            chunk_id=r["chunk_id"], case_id=r["case_id"], doc_id=r["doc_id"],
            span_id=r["span_id"], text=r["text"],
            section_path=r["section_path"])
        for r in rows
    }


@dataclass
class FusedResult:
    passages: list[Passage] = field(default_factory=list)
    lexical_hits: int = 0
    vector_hits: int = 0
    vector_unresolved: int = 0
    note: str = ""


async def hybrid_search(conn: asyncpg.Connection, semantic_query: str,
                        lexical_terms: list[str],
                        case_id: str | None = None,
                        limit: int = 10, ledger=None,
                        alternate_query: str = "",
                        cases_only: bool = False) -> FusedResult:
    """Lexical and vector results fused by rank.

    Both inputs come from the enhancer, and they are deliberately
    different: `lexical_terms` are exact tokens in whatever script the data
    stores, `semantic_query` is fluent text carrying those stored forms so
    the embedding lands near the corpus rather than near the language the
    question happened to be asked in.
    """
    lex = await lexical_search(conn, lexical_terms, case_id, limit * 2)

    vec_pairs = await vector_search(semantic_query, limit * 2,
                                    case_id, ledger=ledger,
                                    cases_only=cases_only)

    # ── Search the language the RECORDS are written in ─────────────────
    #
    # MEASURED, same question in both languages:
    #
    #   "فراڈ کے طریقے"                  -> bank fraud, OTP theft,
    #                                        extortion narratives. Correct.
    #   "what methods were used to
    #    commit fraud"                   -> FIR headers about kidnapping
    #                                        (365-A PPC). Wrong.
    #
    # The narratives in this corpus are Urdu. An English question embeds
    # near English text, and there is little English text to land on, so
    # it drifts to whatever headers happen to be nearest.
    #
    # e5 is multilingual, but that only means both languages share a
    # space -- not that a question in one lands as close to a record in
    # the other as a question in its own language would. So the question
    # is also searched in the corpus language and the two rankings are
    # fused by RANK, the same RRF used for lexical and vector. A second
    # ranking can promote a record the first missed; it cannot demote one
    # the first found.
    if alternate_query and alternate_query.strip() != semantic_query.strip():
        alt_pairs = await vector_search(alternate_query, limit * 2,
                                        case_id, ledger=ledger,
                                        cases_only=cases_only)
        seen = {cid for cid, _ in vec_pairs}
        vec_pairs = vec_pairs + [(cid, sc) for cid, sc in alt_pairs
                                 if cid not in seen]
    vec_map = await hydrate(conn, [cid for cid, _ in vec_pairs])
    unresolved = sum(1 for cid, _ in vec_pairs if cid not in vec_map)

    scores: dict[str, float] = {}
    merged: dict[str, Passage] = {}

    for p in lex:
        merged[p.chunk_id] = p
        scores[p.chunk_id] = scores.get(p.chunk_id, 0.0) + \
            1.0 / (RRF_K + (p.lexical_rank or 999))

    for rank, (cid, _) in enumerate(vec_pairs, start=1):
        p = vec_map.get(cid)
        if p is None:
            continue
        existing = merged.get(cid)
        if existing:
            existing.vector_rank = rank
        else:
            p.vector_rank = rank
            merged[cid] = p
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)

    ranked = sorted(merged.values(), key=lambda p: -scores[p.chunk_id])
    for p in ranked:
        p.score = round(scores[p.chunk_id], 5)

    # Only citeable passages are returned; the count of what was dropped is
    # reported rather than hidden.
    citeable = [p for p in ranked if p.cited][:limit]

    note = ""
    if unresolved and ledger:
        ledger.data_incomplete(
            f"{unresolved} retrieved passages have no rebuilt chunk",
            "those passages could not be cited and were excluded, so the "
            "answer may be missing supporting text")
    if not vec_pairs:
        note = ("vector search returned nothing — the embedding store may "
                "be unavailable; results are lexical only")
    elif unresolved:
        note = (f"{unresolved} vector hits had no rebuilt chunk and were "
                f"dropped as uncitable")

    return FusedResult(passages=citeable, lexical_hits=len(lex),
                       vector_hits=len(vec_pairs),
                       vector_unresolved=unresolved, note=note)
