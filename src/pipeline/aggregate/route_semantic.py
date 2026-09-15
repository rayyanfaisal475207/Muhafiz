# -*- coding: utf-8 -*-
"""
Phase 4D — the semantic evidence route.

WHAT THIS ROUTE IS AND IS NOT. It is a corroboration route. It retrieves
text that bears on the question and reports what that text says about the
INTERPRETATION — which entities and relationships the question seems to
concern, whether the corpus contains records consistent with the structured
reading, whether anything contradicts it.

It is not a third numeric oracle, and the code enforces that rather than
merely intending it. `EvidenceResult` has no `value` field. Numbers found
in retrieved text are captured in `numeric_mentions` — labelled with the
excerpt they came from — and `RouteResult.numeric` returns `None` for an
evidence result, so reconciliation structurally cannot promote a quoted
figure into a computed one.

WHY THAT MATTERS HERE SPECIFICALLY. This corpus is full of numerals in
prose: FIR numbers, section numbers, dates, quantities of recovered
property. A route that "counted what it retrieved" would produce confident
numbers with no relationship to the question — the exact failure the
three-way architecture exists to avoid, arriving through the one door left
open.

RETRIEVAL REUSES THE EXISTING STACK. `embedder.embed_text` +
`vector_store.query_similar` are the same primitives the production RAG
path uses. No second vector store, no parallel index. The heavier
`rag_tool` is deliberately NOT used: it carries statute hypotheses, a retry
loop and an LLM relevance evaluator, all of which would make this route's
behaviour depend on machinery whose failure modes belong to a different
problem.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import routes
from src.pipeline.aggregate.routes import (
    ROUTE_SEMANTIC,
    AggregateRouteRequest,
    EvidenceItem,
    EvidenceResult,
    RouteResult,
)

logger = logging.getLogger(__name__)

#: How many chunks to retrieve. Small on purpose: this route reports what
#: the corpus says about an interpretation, and a wider net would dilute
#: that into topic-level noise without changing the judgement.
_TOP_K = 8

_EXCERPT_CHARS = 220

#: Numerals in retrieved text. Captured for diagnosis, never aggregated.
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")

#: Wording that marks a retrieved passage as contradicting rather than
#: supporting. Deliberately narrow — a passage is only called a
#: contradiction when it says something is absent or unrecorded, which is
#: the contradiction that actually matters for an aggregate ("the system
#: does not record X" against a query that counts X).
_CONTRADICTION_MARKERS = (
    "no record", "not recorded", "no such", "does not exist", "not available",
    "unavailable", "missing", "not maintained", "no data",
    "کوئی ریکارڈ نہیں", "درج نہیں", "دستیاب نہیں",
)


def _entities_mentioned(text: str, snapshot: reg.RegistrySnapshot) -> set[str]:
    """Registry labels whose name appears in the question or a passage.

    Matching against the REGISTRY rather than a hand-written vocabulary
    keeps this route honest about what the data model contains: it can only
    report entities that actually exist.
    """
    lowered = text.lower()
    return {
        label for label in snapshot.known_labels()
        if label.lower() in lowered
    }


def _relationships_mentioned(text: str, snapshot: reg.RegistrySnapshot) -> set[str]:
    lowered = text.lower()
    out: set[str] = set()
    for rel in snapshot.known_rel_types():
        # Relationship types are SCREAMING_SNAKE; compare on their words so
        # "assigned to" in prose matches ASSIGNED_TO.
        words = rel.lower().replace("_", " ")
        if words in lowered or rel.lower() in lowered:
            out.add(rel)
    return out


async def run(
    snapshot: reg.RegistrySnapshot,
    request: AggregateRouteRequest,
    *,
    top_k: int = _TOP_K,
) -> RouteResult:
    """Retrieve evidence bearing on the question's interpretation."""
    started = time.perf_counter()

    try:
        from src.retrieval.embedder import embed_text
        from src.retrieval.vector_store import query_similar

        embedding = await embed_text(request.question)
        chunks = await query_similar(request.question, embedding, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 — retrieval down is not a refusal
        return routes.refusal(
            ROUTE_SEMANTIC, "retrieval_failed",
            f"Semantic retrieval was unavailable: {exc}",
            status=routes.EXECUTION_ERROR,
        )

    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if not chunks:
        # An empty corpus response is a real finding — it is evidence of
        # absence of evidence — so it is SUCCESS with zero items rather
        # than a refusal.
        return RouteResult(
            route=ROUTE_SEMANTIC,
            status=routes.SUCCESS,
            result_shape="evidence",
            result=EvidenceResult(
                evidence_count=0,
                interpretation="No passages in the corpus bear on this question.",
            ),
            provenance={"retrieval": "chroma vector search", "top_k": top_k},
            execution_metadata={"elapsed_ms": round(elapsed_ms, 1)},
        )

    supporting: list[EvidenceItem] = []
    contradicting: list[EvidenceItem] = []
    numeric_mentions: list[dict] = []
    entities: set[str] = _entities_mentioned(request.question, snapshot)
    relationships: set[str] = _relationships_mentioned(request.question, snapshot)

    for chunk in chunks:
        text = (chunk.get("text") or "").strip()
        excerpt = text[:_EXCERPT_CHARS]
        item = EvidenceItem(
            source_id=str(chunk.get("id") or ""),
            text_excerpt=excerpt,
            score=chunk.get("score") or chunk.get("distance"),
            metadata={
                k: v for k, v in (chunk.get("metadata") or {}).items()
                if k in ("case_id", "source", "source_file", "doc_id")
            },
        )
        lowered = text.lower()
        if any(marker in lowered for marker in _CONTRADICTION_MARKERS):
            contradicting.append(item)
        else:
            supporting.append(item)

        entities |= _entities_mentioned(text, snapshot)
        relationships |= _relationships_mentioned(text, snapshot)

        # Numerals are recorded WITH their excerpt so a reader can see the
        # context. They are never summed, counted as an answer, or exposed
        # as a NumericResult.
        for m in _NUMBER_RE.finditer(text[:600]):
            numeric_mentions.append({
                "number": m.group(0),
                "source_id": item.source_id,
                "context": text[max(0, m.start() - 60):m.end() + 60],
                "provenance": "semantic/evidence-derived — NOT a computed aggregate",
            })

    interpretation = (
        f"{len(chunks)} passage(s) retrieved. "
        f"Entities referenced: {', '.join(sorted(entities)) or 'none identified'}. "
        f"Relationships referenced: "
        f"{', '.join(sorted(relationships)) or 'none identified'}. "
        f"{len(contradicting)} passage(s) assert an absence of records."
    )

    return RouteResult(
        route=ROUTE_SEMANTIC,
        status=routes.SUCCESS,
        result_shape="evidence",
        result=EvidenceResult(
            evidence_count=len(chunks),
            supporting=tuple(supporting),
            contradicting=tuple(contradicting),
            relevant_entities=tuple(sorted(entities)),
            relevant_relationships=tuple(sorted(relationships)),
            interpretation=interpretation,
            # Capped: this is a diagnostic aid, not a dataset.
            numeric_mentions=tuple(numeric_mentions[:12]),
        ),
        provenance={
            "retrieval": "chroma vector search (production embedder + store)",
            "top_k": top_k,
            "computes_aggregates": False,
        },
        execution_metadata={"elapsed_ms": round(elapsed_ms, 1)},
    )
