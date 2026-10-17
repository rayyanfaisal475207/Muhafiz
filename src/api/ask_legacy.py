"""The rebuilt retrieval path, exposed alongside the existing one.

WHY A SEPARATE ROUTE

/api/chat runs the deployed pipeline: one model call picks one of ten
routes. This is the rebuild — bind, plan, agent, guards, validator — and
the two answer differently. Replacing the live endpoint would make that
difference invisible exactly when it matters most.

So both run, and the same question can be sent to each. When the new path
has been measured against real traffic rather than my test set, cutting
over is a one-line change.

WHAT THIS RETURNS THAT THE OLD PATH DOES NOT

  citations     span ids resolving to exact characters in a document
  queries       every SQL statement that produced the answer
  health        which components were unavailable, in plain words
  refusal       what was missing, when no answer is possible

An answer without those is a claim. With them it is checkable.

RBAC

Case scope comes from the caller, never from the question. An investigator
restricted to one case stays restricted: the scope is passed into
retrieval, not suggested to the model, because a model can be talked out
of a restriction and a WHERE clause cannot.
"""

from __future__ import annotations

import logging
import os

import asyncpg
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.auth.routes import get_current_user, limiter
from src.database.models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ask"])

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def get_pool() -> asyncpg.Pool:
    """A small dedicated pool.

    The retrieval layer speaks raw SQL against introspection functions and
    runs agent-written queries in READ ONLY transactions. Borrowing the
    ORM's engine would entangle those with application sessions; a separate
    pool keeps the blast radius of an agent query to this pool.
    """
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=6)
    return _pool


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    case_id: str | None = Field(
        default=None,
        description="Restrict retrieval to one case. Enforced in SQL.")
    use_agent: bool = Field(
        default=True,
        description="False runs only the deterministic fast path.")


class Citation(BaseModel):
    span_id: int
    doc_id: str
    case_id: str | None
    excerpt: str


class AskResponse(BaseModel):
    question: str
    answer: str | None
    refusal: str | None = None
    system_message: str | None = None
    route: str
    escalated: list[str] = []
    sql: str | None = None
    queries: list[dict] = []
    citations: list[Citation] = []
    health: dict = {}
    interpretation: str | None = None


@router.post("/api/ask", response_model=AskResponse)
async def ask(payload: AskRequest,
              current_user: User = Depends(get_current_user)) -> AskResponse:
    from src.retrieval.engine import run

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            result = await run(conn, payload.question,
                               use_agent=payload.use_agent,
                               case_id=payload.case_id)
        except Exception as exc:                        # noqa: BLE001
            # A failure here is a system failure, and it is reported as one
            # rather than as a finding about the data. Returning an empty
            # answer would read as "nothing found", which is a different
            # and misleading statement.
            logger.exception("ask failed for user %s", current_user.id)
            raise HTTPException(
                status_code=503,
                detail=("I can't answer this right now — the system is not "
                        "fully available. This is a problem on our side, "
                        "not with your question. Please try again shortly."),
            ) from exc

        citations = await _citations(conn, result)

    plan = result.get("plan")
    reading = result.get("reading")
    answer = result.get("answer")

    return AskResponse(
        question=payload.question,
        answer=_render(answer),
        refusal=result.get("refusal"),
        system_message=result.get("system_message"),
        route=(plan.route.value if plan else "unknown"),
        escalated=result.get("escalated") or [],
        sql=result.get("sql"),
        queries=result.get("queries") or [],
        citations=citations,
        health=result.get("health") or {},
        interpretation=(reading.brief()
                        if reading is not None and reading.available
                        else None),
    )


def _render(answer) -> str | None:
    """Answers arrive as prose from the agent or a row from the fast path."""
    if answer is None:
        return None
    if isinstance(answer, str):
        return answer
    if isinstance(answer, dict):
        if "answer" in answer and len(answer) == 1:
            return str(answer["answer"])
        if "top" in answer:
            return str(answer["top"])
        return "; ".join(f"{k}: {v}" for k, v in answer.items())
    return str(answer)


async def _citations(conn: asyncpg.Connection, result: dict) -> list[Citation]:
    """Resolve retrieved spans to the exact text behind them.

    A span that no longer resolves is dropped rather than shown: a citation
    that does not check out is worse than none, because it looks like
    evidence.
    """
    passages = result.get("passages") or []
    span_ids = [p.span_id for p in passages if getattr(p, "span_id", None)]
    if not span_ids:
        return []

    rows = await conn.fetch(
        """
        SELECT s.span_id, s.doc_id, d.case_id, s.text
        FROM doc_spans s
        LEFT JOIN rebuild_documents d ON d.doc_id = s.doc_id
        WHERE s.span_id = ANY($1)
        """, span_ids[:8])
    return [
        Citation(span_id=r["span_id"], doc_id=r["doc_id"],
                 case_id=r["case_id"],
                 excerpt=" ".join((r["text"] or "").split())[:400])
        for r in rows
    ]


@router.get("/api/ask/health", tags=["ask"])
async def ask_health(current_user: User = Depends(get_current_user)) -> dict:
    """Whether the rebuilt path can answer at all, and what it knows about.

    Deliberately reports the size of the evidence surface. An empty one
    answers every question with "nothing found", which is indistinguishable
    from a broken deployment unless the counts are visible.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM rebuild_cases)           AS cases,
                   (SELECT count(*) FROM rebuild_chunks)          AS chunks,
                   (SELECT count(*) FROM doc_spans)               AS spans,
                   (SELECT count(*) FROM case_parties)            AS parties,
                   (SELECT count(*) FROM weapons)                 AS weapons,
                   (SELECT count(*) FROM table_roles
                     WHERE is_evidence)                           AS evidence_tables
            """)
        stale = await conn.fetchrow("SELECT * FROM catalog_freshness")

    data = dict(row) if row else {}

    # Readiness must measure what it claims to. The first version compared
    # cached columns (560) against live base-table columns (550) — two
    # different populations, since the cache also covers materialised
    # views. It reported ready=false on a system that was answering
    # correctly, which is the same class of blind spot as a silent
    # failure: an indicator that lies is worse than no indicator.
    #
    # What actually matters is whether the catalog knows about every base
    # table, and whether there is evidence to answer from.
    cached_edges = (stale or {}).get("cached_edges") or 0
    live_edges = (stale or {}).get("live_edges") or 0
    data["catalog_edges_in_sync"] = (cached_edges == live_edges)
    data["has_evidence"] = bool(data.get("cases")) and bool(data.get("spans"))
    data["ready"] = data["has_evidence"] and data["catalog_edges_in_sync"]
    if not data["ready"]:
        data["why_not_ready"] = (
            "no case data loaded" if not data["has_evidence"]
            else "catalog is stale — run refresh_catalog()")
    return data


# ── The same answer, in the format the existing frontend speaks ─────────
#
# The chat UI consumes Server-Sent Events with a fixed shape:
#   {"step": ..., "status": "active|streaming|done|error", "detail": ...}
# and accumulates every `step == "response", status == "streaming"` chunk
# into the answer.
#
# /api/ask returns JSON, which the UI cannot render. Rather than change the
# frontend, this speaks the existing protocol — so pointing it at this URL
# is a one-line change, and pointing it back is too.


class StreamRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=2000)
    case_id: str | None = None
    project_id: str | None = None


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)


@router.post("/api/ask/stream")
async def ask_stream(payload: StreamRequest,
                     current_user: User = Depends(get_current_user)):
    """The rebuilt path, streamed in the chat UI's event format."""
    from src.retrieval.engine import run

    async def events():
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                yield _sse({"step": "routing", "status": "active",
                            "detail": "Understanding the question"})
                result = await run(conn, payload.message,
                                   use_agent=True, case_id=payload.case_id)

                plan = result.get("plan")
                reading = result.get("reading")
                if reading is not None and getattr(reading, "available", False):
                    yield _sse({"step": "routing", "status": "done",
                                "detail": reading.brief()})
                else:
                    yield _sse({"step": "routing", "status": "done",
                                "detail": (plan.route.value if plan
                                           else "unknown")})

                for esc in (result.get("escalated") or [])[:3]:
                    yield _sse({"step": "planning", "status": "done",
                                "detail": esc[:200]})

                for q in (result.get("queries") or [])[:6]:
                    yield _sse({"step": "retrieval", "status": "done",
                                "detail": f"{q['rows']} rows — {q['sql'][:160]}",
                                "ms": q.get("ms")})

                citations = await _citations(conn, result)
                if citations:
                    yield _sse({
                        "step": "retrieval", "status": "done",
                        "detail": f"{len(citations)} cited passage(s)",
                        "sources": [
                            {"doc_id": c.doc_id, "case_id": c.case_id,
                             "snippet": c.excerpt[:300],
                             "span_id": c.span_id}
                            for c in citations
                        ],
                    })

                answer = _render(result.get("answer"))
                refusal = result.get("refusal")
                system_message = result.get("system_message")

                if answer is None:
                    # A refusal is a real answer and must reach the user as
                    # text, not as an error the UI swallows.
                    text = refusal or system_message or (
                        "I could not answer that from the case data.")
                    yield _sse({"step": "response", "status": "streaming",
                                "detail": text})
                    yield _sse({"step": "response", "status": "done",
                                "detail": "Refused"})
                    return

                if system_message:
                    answer = answer + chr(10) + chr(10) + system_message

                yield _sse({"step": "response", "status": "streaming",
                            "detail": answer})
                yield _sse({"step": "response", "status": "done",
                            "detail": f"Response generated ({len(answer)} chars)"})

        except Exception as exc:                        # noqa: BLE001
            logger.exception("ask_stream failed for user %s", current_user.id)
            yield _sse({
                "step": "response", "status": "streaming",
                "detail": ("I can't answer this right now — the system is "
                           "not fully available. This is a problem on our "
                           "side, not with your question."),
            })
            yield _sse({"step": "response", "status": "error",
                        "detail": str(exc)[:200]})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
