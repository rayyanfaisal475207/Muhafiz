"""Ask the embedding model what a question is ABOUT.

THE HOLE THIS FILLS

`bind()` locates a question's words in the schema with trigram and exact
comparison. That is spelling. It works for English by coincidence —
English questions use English words, and the identifiers are English.

Urdu gets no such coincidence, because the Urdu in this database is in the
VALUES, not the identifiers. Measured, on "کتنے ہتھیار برآمد ہوئے؟":

    'ہتھیار' -> weapons                     table  1.000
    'برآمد'  -> case_events.event_type_raw  value  1.000

A dead tie between the right answer and a wrong one. The agent took the
event type and returned 7. The truth is 96.

Meanwhile the multilingual embedding model — already running, already
serving passage retrieval — ranks the same question:

    0.8348  weapons        <- first, and scoring HIGHER than the English
    0.8213  seized_items      form of the same question manages (0.7940)
    0.8066  vehicles          case_events does not reach the top five

It was never asked. Every Urdu failure in the benchmark sits on top of
that omission, and so does one English failure.

WHAT THIS DOES AND DOES NOT DECIDE

It ranks schema objects by meaning, and that ranking says WHAT TO COUNT.
It never says how many. The figure is still computed by SQL over rows,
because a model reading rows and reporting a total is the counting-by-
reading failure this rebuild exists to remove.

So: retrieval chooses the table, SQL produces the number. Both halves are
necessary, and until now only one of them was wired.

FUSION, NOT REPLACEMENT

Lexical binding is not deleted — it is better than embeddings at exactly
what embeddings are bad at. 'لاہور' is a literal row in `places`, and an
exact value match is the strongest possible evidence for it; a cosine of
0.83 is not. So the two rankings are fused with RRF, the same way
text_search.py fuses lexical and vector hits for passages: by RANK, since
a cosine and a trigram score are not on comparable scales and averaging
them would be arithmetic with no meaning.

COST

The schema is small and does not grow with the corpus: 77 objects here,
and still 77 at a million cases. The vectors load once into the process
and are re-read only when the index is rebuilt, so a question pays for one
query embedding — the same call passage retrieval already makes.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
import numpy as np

from src.retrieval.health import Ledger, Severity

# Reciprocal Rank Fusion. 60 is the value from the original paper and the
# same constant text_search.py already fuses passages with; keeping them
# identical means one behaviour to reason about, not two.
RRF_K = 60


@dataclass(frozen=True)
class SchemaHit:
    """One schema object, ranked by what the question means."""

    table: str
    column: str | None
    score: float                 # cosine similarity, for explanation only
    doc: str

    @property
    def label(self) -> str:
        return f"{self.table}.{self.column}" if self.column else self.table


class _Index:
    """The schema's vectors, held in the process between questions.

    Reloaded when the stored index is rebuilt, detected by its build
    timestamp and row count rather than by a timer — a cache that expires
    on a clock is either stale or wasteful, and this one can know.
    """

    def __init__(self) -> None:
        self.vectors: np.ndarray | None = None
        self.rows: list[dict] = []
        self.stamp: tuple | None = None

    async def load(self, conn: asyncpg.Connection) -> bool:
        head = await conn.fetchrow(
            "SELECT count(*) AS n, max(built_at) AS at,"
            "       count(DISTINCT model) AS models,"
            "       count(DISTINCT dims) AS dims"
            " FROM schema_embeddings")
        if not head or not head["n"]:
            return False
        # Vectors from two models share no space, so cosine between them
        # is a number with no meaning. Refuse the index rather than rank
        # by it -- a silently mixed index would look like it was working.
        if head["models"] > 1 or head["dims"] > 1:
            return False

        stamp = (head["n"], head["at"])
        if stamp == self.stamp and self.vectors is not None:
            return True

        rows = await conn.fetch(
            "SELECT table_name, column_name, doc, embedding"
            " FROM schema_embeddings ORDER BY table_name, column_name")
        matrix = np.array([r["embedding"] for r in rows], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        self.vectors = matrix / np.maximum(norms, 1e-12)
        self.rows = [{"table": r["table_name"], "column": r["column_name"],
                      "doc": r["doc"]} for r in rows]
        self.stamp = stamp
        return True


_INDEX = _Index()


async def rank_schema(conn: asyncpg.Connection, question: str,
                      limit: int = 8,
                      ledger: Ledger | None = None) -> list[SchemaHit]:
    """The schema objects this question is about, best first.

    Returns [] whenever it cannot do the job, and says so in the ledger.
    An empty ranking costs the fusion nothing — lexical binding still
    stands — but a wrong ranking would quietly steer every question, so
    unavailable must never be approximated.
    """
    question = (question or "").strip()
    if not question:
        return []

    try:
        ready = await _INDEX.load(conn)
    except Exception as exc:                            # noqa: BLE001
        if ledger:
            ledger.record("semantic binding", Severity.IMPORTANT,
                          str(exc)[:200],
                          "I matched your question to the data by wording "
                          "alone, so a question phrased differently from "
                          "the records may have missed them")
        return []

    if not ready:
        if ledger:
            ledger.record(
                "semantic binding", Severity.IMPORTANT,
                "schema embedding index missing, empty or mixed-model",
                "I matched your question to the data by wording alone, "
                "which works less well for Urdu than for English")
        return []

    try:
        from src.retrieval.embedder import embed_text
        vec = await embed_text(question)
    except Exception as exc:                            # noqa: BLE001
        if ledger:
            ledger.embedder_unavailable(str(exc)[:200])
        return []
    if not vec:
        if ledger:
            ledger.embedder_unavailable("embedding service returned nothing")
        return []

    q = np.array(vec, dtype=np.float32)
    q /= max(float(np.linalg.norm(q)), 1e-12)
    sims = _INDEX.vectors @ q                           # type: ignore[operator]

    order = np.argsort(-sims)[:limit]
    return [
        SchemaHit(table=_INDEX.rows[i]["table"],
                  column=_INDEX.rows[i]["column"],
                  score=float(sims[i]),
                  doc=_INDEX.rows[i]["doc"])
        for i in order
    ]


def fuse_tables(lexical: list[str], semantic: list[SchemaHit],
                limit: int = 6) -> list[str]:
    """Let meaning answer only where the words found nothing.

    TWO MEASURED FAILURES SHAPED THIS, AND BOTH WERE MINE.

    First version ranked both lists with RRF and took the result. "How
    many cases are registered?" had answered 73 for weeks; the fusion
    promoted `case_station_history` over `rebuild_cases` and it began
    refusing. So I made it additive and claimed that additive "can never
    make an already-correct question worse".

    That claim was false, and the second failure proved it:

        "How many items were seized?"   139 -> 103

        lexical tables : ['seized_items']            <- correct
        semantic top 4 : seized_items.quantity, .category, ...  <- correct
        after fusion   : ['seized_items', 'weapons', 'rebuild_cases']

    Both rankings were right. Appending swept up `weapons` from the weak
    tail of the list, and `compile_sql` JOINs every table in plan.tables:

        FROM seized_items JOIN rebuild_cases ... JOIN weapons ...

    An INNER JOIN restricted the count to items in cases that also had a
    weapon. Adding a table is not additive — downstream it is a filter.

    THE RULE

    Semantic ranking is a FALLBACK for when the words fail, which is
    exactly the case it was built for: an Urdu question whose terms
    collide with stored values instead of identifiers. Where lexical
    binding already produced a table, it had real evidence — an exact
    identifier or an exact stored value — and meaning adds only risk.

    So: if the words found a table, the words decide. If they found
    nothing, meaning gets to answer. The ranking is still carried on the
    plan either way, so the agent can see it without the compiler
    joining it.
    """
    if lexical:
        return lexical[:limit]

    out: list[str] = []
    for hit in semantic:
        if hit.table not in out:
            out.append(hit.table)
    return out[:limit]
