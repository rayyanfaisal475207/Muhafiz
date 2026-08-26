# ============================================================
# Relationship extraction — person-to-person ASSOCIATED_WITH edges
#
# GAP THIS CLOSES (found in a retrieval-quality audit, not designed in
# from the start): docs/graph_schema.md describes ASSOCIATED_WITH as
# "Generic person-to-person relationship extracted from domain-entity
# extraction (4.6)" — but domain_entities.py (4.6) only ever extracted
# ENTITIES (vehicle/weapon/organization/person/incident), never
# relationships BETWEEN them. Grepping every write_edge() call site in
# this codebase for "ASSOCIATED_WITH" turns up exactly one place it is
# READ (src/retrieval/graph_retriever.py's `_HOP_EDGE_TYPE`) and zero
# places it is ever WRITTEN. Concretely: graph_retriever's multi-hop
# traversal can never advance past hop 0 (the seed entity's own
# APPEARS_IN chunks) for ANY case, because the one edge type it follows
# to reach a second entity has never been populated — this is the same
# failure mode GraphRAG's own published weak point (~42.8% relationship-
# pair coverage) describes, except total (0%) rather than partial, and a
# missing pipeline stage rather than a recall ceiling.
#
# SCOPE: person-to-person only, matching docs/graph_schema.md's own
# description of the edge type. Extraction runs per-chunk over the set of
# person mentions NER (4.5) already resolved in that chunk — it does not
# re-run NER, and it never invents a person who wasn't already extracted.
# ============================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.llm.client import call_llm
from src.pipeline.json_extract import call_llm_json

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "relationship_extraction.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_MAX_CHARS = 3000
_SCHEMA_HINT = '[{"person_a": <index>, "person_b": <index>, "basis": "...", "confidence": 0.0-1.0}, ...] (a JSON array, [] if none)'


@dataclass
class RelationshipMention:
    person_a: str          # canonical_name of the first person, as given in the input list
    person_b: str
    basis: str
    confidence: float

    def to_dict(self) -> dict:
        return {"person_a": self.person_a, "person_b": self.person_b,
                "basis": self.basis, "confidence": self.confidence}


async def extract_relationships(text: str, person_names: list[str]) -> list[dict]:
    """
    Given chunk text and the distinct person names already extracted from
    it (by ner.py/domain_entities.py), return the pairs the text itself
    states or clearly implies a relationship between.

    Returns [] immediately (no LLM call) when fewer than two people are
    named — there is no pair to consider — and on any LLM/parse failure
    (logged, never raised), matching every other extractor in this phase's
    per-document resilience requirement (src/ingestion/service.py's
    _run_graph_extraction already treats a step's own failure as
    "this document/chunk has no [X] data," never as ingestion failure).

    Uses call_llm_json (not a raw call_llm + parse_json_response, the
    original shape of this function) — root-caused via a Branch 5 audit
    into why ASSOCIATED_WITH edges were so sparse (~17 across ~1000 Person
    nodes): confirmed live, 4/4 reproducible, the local model answers this
    exact prompt with free-prose commentary instead of the required JSON
    array DESPITE correctly understanding the relationship being asked
    about (its prose response accurately described the stated
    association) — the same "local model ignores an explicit JSON-only
    instruction" failure class router.py's G-1 fix and
    community_summarization.py's own bugs already found elsewhere in this
    pipeline. This extractor was the one JSON-output call site in the
    pipeline that never got call_llm_json's retry-with-correction
    treatment, so every one of these failures silently returned [] with
    zero retry — very likely the dominant cause of the low edge count,
    not a genuine absence of stated relationships in the corpus.
    """
    distinct_names = list(dict.fromkeys(n for n in person_names if n and n.strip()))
    if len(distinct_names) < 2 or not text or not text.strip():
        return []

    numbered = "\n".join(f"{i}: {name}" for i, name in enumerate(distinct_names, start=1))
    snippet = text[:_MAX_CHARS]
    user_message = f"People:\n{numbered}\n\nPassage: {snippet}"

    parsed, raw = await call_llm_json(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_message,
        temperature=0.0,
        # 800 wasn't enough on live re-measurement — Qwen3-14B's thinking
        # trace can exhaust it before the JSON answer. Raised to 2000 for
        # the LOCAL budget; cloud_max_tokens pinned at the old 800 so the
        # cloud fallback is unaffected.
        max_tokens=2000,
        cloud_max_tokens=800,
        validate=lambda r: isinstance(r, list),
        schema_hint=_SCHEMA_HINT,
        _call_llm=call_llm,
    )
    if parsed is None:
        logger.warning("relationship_extraction: no valid JSON after retries — raw: %s", raw[:200])
        return []

    out: list[RelationshipMention] = []
    seen_pairs: set[frozenset] = set()
    for item in parsed:
        try:
            idx_a, idx_b = int(item.get("person_a")), int(item.get("person_b"))
        except (TypeError, ValueError):
            continue
        if idx_a == idx_b or not (1 <= idx_a <= len(distinct_names)) or not (1 <= idx_b <= len(distinct_names)):
            continue

        name_a, name_b = distinct_names[idx_a - 1], distinct_names[idx_b - 1]
        pair_key = frozenset({name_a, name_b})
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        basis = (item.get("basis") or "").strip()
        if not basis:
            continue
        confidence = float(item.get("confidence", 0.5) or 0.5)
        out.append(RelationshipMention(name_a, name_b, basis, confidence))

    return [m.to_dict() for m in out]
