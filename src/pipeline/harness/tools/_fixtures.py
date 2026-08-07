"""
Deliberately messy stub evidence.

The point of this module is to make the handoff-summarization boundary hard to
get accidentally right. Real retrieval returns long, redundant, near-duplicate
passages; a sub-agent that naively concatenates its chunks into `answer_text`,
or that leaks its working set upward, should FAIL a test now rather than pass
here and blow the supervisor's context budget in production.

So the RAG and GRAPH fixtures below are intentionally:
  * long — multi-hundred-character chunk bodies, not toy strings
  * redundant — several chunks restating the same fact in different words
  * near-duplicate — chunk pairs differing only in a clause, as a real
    hybrid-retrieval result set does before deduplication

Nothing here is real case data. All identifiers are synthetic.
"""
from __future__ import annotations

from src.pipeline.harness.contracts import ChunkMetadata, EvidenceChunk

_CASE = "CASE-A1B2C3D4"


def _chunk(
    idx: int, text: str, source_tool: str, source_file: str,
    score: float, case_id: str | None = _CASE, **extra,
) -> EvidenceChunk:
    return EvidenceChunk(
        id=f"chunk-{source_tool.lower()}-{idx:03d}",
        text=text,
        score=score,
        metadata=ChunkMetadata(
            source_tool=source_tool, case_id=case_id, source_file=source_file, **extra
        ),
    )


# ── RAG: verbose, redundant, near-duplicate ──────────────────────────────

_RAG_BODY_1 = (
    "First Information Report FIR-2026-THEFT-0143 records that on the night of 14 March 2026, "
    "between approximately 23:40 and 00:15 hours, an unknown individual gained entry to the "
    "commercial premises at Plot 22, Sector G-8/4, Islamabad, by forcing the rear service door. "
    "The complainant, the shop's night supervisor, stated that he had completed his round at "
    "23:30 and found the door secure at that time. On returning at approximately 00:20 he found "
    "the door ajar and the internal shutter partially raised. A silver-coloured pickup vehicle "
    "bearing a partially obscured registration plate was observed by a neighbouring watchman "
    "leaving the service lane at speed shortly after midnight. No injuries were reported."
)

_RAG_BODY_2 = (
    "Per FIR-2026-THEFT-0143, entry to the premises at Plot 22, Sector G-8/4 was effected through "
    "the rear service door, which was forced. The night supervisor confirmed the door was secure "
    "during his 23:30 round and found it ajar at roughly 00:20. A silver pickup with an unclear "
    "number plate was seen departing the service lane at speed just after midnight by a watchman "
    "employed at the adjacent property. The incident window is therefore approximately 23:40 to "
    "00:15 on the night of 14 March 2026. The complainant reported no injuries to any person."
)

_RAG_BODY_3 = (
    "Supplementary statement recorded 16 March 2026. The neighbouring watchman, on further "
    "questioning, stated that the vehicle he observed leaving the service lane was a pickup, "
    "silver or light grey in colour, and that he believed the registration plate had been "
    "partially covered with cloth or tape. He could not recall any digits with confidence. He "
    "estimated the time of departure as 'a little after twelve'. He did not see how many "
    "occupants were in the vehicle and did not observe the direction it took on reaching the "
    "main road. He confirmed he did not report the matter at the time."
)

_RAG_BODY_4 = (
    "Inventory annexure to FIR-2026-THEFT-0143, prepared by the complainant and countersigned by "
    "the investigating officer. Items recorded as missing: one steel cash box containing an "
    "unspecified sum; twelve cartons of assorted electrical fittings; one laptop computer, make "
    "and serial number not recorded by the complainant at the time of the report. The complainant "
    "undertook to provide purchase documentation for the laptop. The annexure notes that the "
    "stock register for the current month was not available at the time of inspection."
)

_RAG_BODY_5 = (
    "Procedural note appended to the case file. The premises at Plot 22, Sector G-8/4 falls within "
    "the jurisdiction of the local police station. Standard procedure for a reported burglary of "
    "commercial premises requires photography of the point of entry before any disturbance, "
    "collection of latent prints from the forced door and internal shutter, and a canvass of "
    "adjacent properties for witnesses and any private CCTV coverage. The note does not record "
    "whether latent print collection was in fact carried out at this scene."
)


def rag_chunks() -> list[EvidenceChunk]:
    """
    Five chunks. Note chunks 1 and 2 are near-duplicates (same facts, different
    phrasing) — exactly the shape RRF produces before deduplication, and the
    case a naive summarizer handles badly.
    """
    return [
        _chunk(1, _RAG_BODY_1, "RAG", "FIR-2026-THEFT-0143.pdf", 0.94),
        _chunk(2, _RAG_BODY_2, "RAG", "FIR-2026-THEFT-0143.pdf", 0.91),
        _chunk(3, _RAG_BODY_3, "RAG", "supplementary-statement-16mar.pdf", 0.78),
        _chunk(4, _RAG_BODY_4, "RAG", "inventory-annexure.pdf", 0.66),
        _chunk(5, _RAG_BODY_5, "RAG", "procedural-note.pdf", 0.51),
    ]


# ── GRAPH / GRAPH_HYBRID ─────────────────────────────────────────────────

_GRAPH_BODY_1 = (
    "Graph traversal result, hop 1. Vehicle node VEH-0091 (silver pickup, plate partially "
    "recorded as 'ICT-?4?-8812') is linked by an APPEARS_IN edge to the incident recorded under "
    "FIR-2026-THEFT-0143. The edge carries provenance to the supplementary witness statement of "
    "16 March 2026 and is marked as derived from an unverified visual description rather than a "
    "documented registration lookup."
)

_GRAPH_BODY_2 = (
    "Graph traversal result, hop 2. Person node PER-0338 is linked to Vehicle node VEH-0091 by an "
    "OWNS edge with a confidence below the auto-merge threshold. The link derives from a name "
    "match against a registration record and has NOT been confirmed against a CNIC. This "
    "association must be treated as a candidate, not as established fact."
)

_GRAPH_BODY_3 = (
    "Graph traversal result, hop 2. Address node ADR-0117 (Sector G-8/4 service lane) is linked to "
    "the incident by a LOCATED_AT edge derived directly from the FIR text. This is a structural "
    "link with documentary provenance and does not depend on any identity resolution."
)


def graph_chunks(hybrid: bool = False) -> list[EvidenceChunk]:
    """
    Graph traversal chunks with per-hop confidence.

    [RESOLVED-1a] When `hybrid` is True the chunks carry
    `source_tool="GRAPH_HYBRID"` and the fused RAG passages are included, so a
    hybrid result is structurally distinguishable from a plain GRAPH one — not
    merely differently scored.
    """
    tool = "GRAPH_HYBRID" if hybrid else "GRAPH"
    chunks = [
        _chunk(1, _GRAPH_BODY_1, tool, "evidence_graph", 0.88, confidence=0.62),
        _chunk(2, _GRAPH_BODY_2, tool, "evidence_graph", 0.71, confidence=0.34),
        _chunk(3, _GRAPH_BODY_3, tool, "evidence_graph", 0.69, confidence=0.95),
    ]
    if hybrid:
        # Fused document evidence, relabelled to the hybrid tool so provenance
        # reflects how it actually reached the answer.
        for i, body in enumerate((_RAG_BODY_1, _RAG_BODY_3), start=4):
            chunks.append(
                _chunk(i, body, tool, "FIR-2026-THEFT-0143.pdf", 0.85 - (i * 0.05), confidence=0.80)
            )
    return chunks


# ── Cross-case fixtures ──────────────────────────────────────────────────

_XCASE_IDS = ["CASE-A1B2C3D4", "CASE-9F8E7D6C", "CASE-5B4A3C2D"]


def xgraph_chunks() -> list[EvidenceChunk]:
    body = (
        "Cross-case traversal. Vehicle node VEH-0091 appears in three separate case graphs via "
        "BELONGS_TO_CASE edges. In two of the three the association derives from a witness "
        "description rather than a registration lookup, and one of those two carries an "
        "unconfirmed SAME_AS link to a differently-plated vehicle node."
    )
    return [
        _chunk(1, body, "XGRAPH", "evidence_graph", 0.87, case_id=cid, confidence=0.55)
        for cid in _XCASE_IDS
    ]


def xagg_rows() -> list[dict]:
    return [
        {"key": "Vehicle VEH-0091", "count": 3},
        {"key": "Vehicle VEH-0042", "count": 2},
        {"key": "Vehicle VEH-0203", "count": 2},
    ]


def xnetwork_chunks() -> list[EvidenceChunk]:
    body = (
        "Community summary C-014. A cluster of commercial burglaries across three cases shares a "
        "consistent pattern: forced rear service doors on light-industrial premises, entry between "
        "23:00 and 01:00, and a light-coloured pickup observed departing. Registration plates are "
        "obscured in every instance. The cluster is thematic and does not by itself establish that "
        "the same individuals are responsible."
    )
    return [
        _chunk(1, body, "XNETWORK", "community_summaries", 0.82, case_id=None, confidence=0.70),
    ]


def sql_rows() -> list[dict]:
    return [
        {
            "section_ref": "PPC 380",
            "category": "Offences against property",
            "subject": "Theft in dwelling house",
            "cognizable": True,
        },
        {
            "section_ref": "PPC 457",
            "category": "Offences against property",
            "subject": "House-breaking by night",
            "cognizable": True,
        },
    ]


def web_results() -> list[dict]:
    return [
        {
            "title": "Islamabad Police — Reporting a burglary",
            "url": "https://islamabadpolice.gov.pk/reporting",
            "content": "Procedural guidance on reporting burglary of commercial premises.",
            "score": 0.77,
        },
    ]
