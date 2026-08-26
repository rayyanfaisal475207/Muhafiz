# ============================================================
# Read-only reporting script: regenerate a human-review report for one
# case's Person-node duplicate cluster, live from the running
# Postgres/AGE graph + Chroma vector store — no file dependency, no
# mutation.
#
# Written to answer a follow-up on findings.md Module 11 (see that
# section for full root-cause history): after commit e040c64 confirmed
# the same-document/same-case pending SAME_AS edges for fir-1001-26,
# canonicalization at READ time collapsed to ~36, but the raw duplicate
# Person nodes are still physically in the graph (confirming a SAME_AS
# edge does not delete or merge a node — see that commit's own comments
# and entity_resolution.py's docstring). This script surfaces exactly
# what's left, grouped by distinct canonical_name string, with a sample
# source-chunk snippet per string so a human can judge "real person or
# extraction noise" the way findings.md's own Module 11 diagnosis did.
#
# Purely additive: only MATCH/RETURN Cypher, only Chroma .get_by_ids()
# reads. Does not call confirm_match(), does not touch SAME_AS edges,
# does not delete anything.
#
# Usage:
#   python scripts/review_case_person_duplicates.py [case_id]
#   (default case_id: fir-1001-26)
# ============================================================

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.graph import age_client

DEFAULT_CASE_ID = "fir-1001-26"
SNIPPET_RADIUS = 60  # chars either side of the matched name, for context

_PERSONS_QUERY = (
    "MATCH (p:Person)-[b:BELONGS_TO_CASE]->(c:Case {case_id: $case_id}) "
    "WHERE b.superseded_by IS NULL "
    "RETURN p.entity_id AS entity_id, p.canonical_name AS canonical_name, "
    "p.source_doc_id AS source_doc_id, b.source_chunk_id AS source_chunk_id"
)

_SAME_AS_QUERY = (
    "MATCH (a:Person)-[r:SAME_AS]->(b:Person) "
    "WHERE a.entity_id IN $entity_ids AND b.entity_id IN $entity_ids "
    "  AND r.superseded_by IS NULL "
    "RETURN r.status AS status"
)


async def _fetch_persons(case_id: str) -> list[dict]:
    return await age_client.execute_cypher(
        _PERSONS_QUERY,
        params={"case_id": case_id},
        columns=["entity_id", "canonical_name", "source_doc_id", "source_chunk_id"],
    )


async def _fetch_same_as_status_counts(entity_ids: list[str]) -> dict[str, int]:
    if not entity_ids:
        return {}
    rows = await age_client.execute_cypher(
        _SAME_AS_QUERY,
        params={"entity_ids": entity_ids},
        columns=["status"],
    )
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["status"]] += 1
    return dict(counts)


def _snippet(text: str, needle: str) -> str:
    idx = text.find(needle)
    if idx == -1:
        return text[: SNIPPET_RADIUS * 2].replace("\n", " ")
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(text), idx + len(needle) + SNIPPET_RADIUS)
    return text[start:end].replace("\n", " ")


async def main() -> None:
    case_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CASE_ID

    rows = await _fetch_persons(case_id)

    # Dedupe by entity_id at the node level (findings.md Module 11 notes a
    # pre-existing data-versioning detail: a person can have more than one
    # non-superseded BELONGS_TO_CASE edge to the same case, which would
    # otherwise double-count this row set).
    by_entity: dict[str, dict] = {}
    for row in rows:
        by_entity.setdefault(row["entity_id"], row)
    persons = list(by_entity.values())

    groups: dict[str, list[dict]] = defaultdict(list)
    for p in persons:
        groups[p["canonical_name"]].append(p)

    same_as_counts = await _fetch_same_as_status_counts(list(by_entity.keys()))

    # Pull one sample chunk per distinct name for context, via the vector
    # store (best-effort — a missing/never-ingested chunk id is skipped,
    # matching get_by_ids()'s own documented behavior).
    sample_chunk_ids = list({
        p["source_chunk_id"] for members in groups.values() for p in members[:1]
        if p.get("source_chunk_id")
    })
    chunk_texts: dict[str, str] = {}
    if sample_chunk_ids:
        try:
            from src.retrieval.vector_store import ChromaVectorStore
            store = ChromaVectorStore.get_instance()
            for c in store.get_by_ids(sample_chunk_ids):
                chunk_texts[c["id"]] = c["text"]
        except Exception as exc:
            print(f"(chunk text lookup unavailable: {exc})\n")

    lines: list[str] = []
    lines.append(f"# Person-record review — case {case_id}")
    lines.append("")
    lines.append(f"Raw Person nodes (non-superseded BELONGS_TO_CASE, deduped by entity_id): {len(persons)}")
    lines.append(f"Distinct canonical_name strings: {len(groups)}")
    lines.append(f"SAME_AS edges among these nodes, by status: {same_as_counts or '{}'}")
    lines.append("")
    lines.append("## Distinct strings, most frequent first")
    lines.append("")

    for name, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"### {name!r} — {len(members)} record(s)")
        sample = members[0]
        chunk_id = sample.get("source_chunk_id")
        text = chunk_texts.get(chunk_id) if chunk_id else None
        if text:
            lines.append(f"- Context: …{_snippet(text, name)}…")
        else:
            lines.append(f"- Context: (no chunk text available for {chunk_id!r})")
        lines.append(f"- Sample entity_id: {sample['entity_id']}")
        lines.append(f"- source_doc_id: {sample.get('source_doc_id')}")
        lines.append("")

    report = "\n".join(lines)
    print(report)

    out_path = Path(__file__).resolve().parent.parent / f"CASE_{case_id}_PERSON_REVIEW.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
