import logging
import json
from typing import Optional

from src.graph import age_client, versioning
from src.llm.client import call_llm
from src.extraction.llm_json import parse_json_response

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a police investigator reviewing extracted Incident events for a single case.
You will be provided with a list of Incident events, each with an 'entity_id', 'description', 'date', and 'source_text'.

Your task is to identify if there are any direct, unambiguous contradictions between these events.
A contradiction occurs if:
1. Two events are clearly referring to the SAME real-world incident (same actors, similar action), but have contradictory details (e.g. different dates, different locations).
2. Two events state a timeline sequence that is physically impossible.

For each contradiction, return an object with:
- "incident_a_id": entity_id of the first incident
- "incident_b_id": entity_id of the second incident
- "basis": a brief explanation of the contradiction
- "quote_a": a verbatim exact quote from Incident A's 'source_text' that supports the contradiction
- "quote_b": a verbatim exact quote from Incident B's 'source_text' that supports the contradiction

Return ONLY a JSON array of these objects. If no contradictions exist, return [].
Do NOT flag different events (like an arrest vs a robbery on different days) as contradictions unless they are mutually exclusive.
"""

async def detect_conflicts(case_id: str) -> None:
    """
    Run case-scoped conflict detection.
    Queries the graph for Incident nodes in the case, uses the LLM to identify narrative or timeline conflicts,
    verifies the LLM's claims against the source text, and writes CONFLICTS_WITH edges between Incident nodes.
    """
    # 1. Fetch Incident nodes and their associated dates and source texts
    #
    # 2026-08-07: found live, always failing -- an OPTIONAL MATCH directly
    # followed by a mandatory MATCH is invalid openCypher/AGE ("MATCH
    # cannot follow OPTIONAL MATCH"), confirmed against the real AGE
    # instance. Every call to this function has been hitting the except
    # branch below and silently returning since this module was written --
    # conflict detection has never actually run for any case. Fixed by
    # moving OPTIONAL MATCH to the end, after both mandatory MATCH clauses
    # (Case and Document are required for an Incident row to be useful
    # here; Date is genuinely optional -- an Incident may not have a
    # resolved OCCURRED_ON edge) -- same semantics, valid clause order.
    query = """
    MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id})
    MATCH (i)-[app:APPEARS_IN]->(doc:Document)
    OPTIONAL MATCH (i)-[:OCCURRED_ON]->(d:Date)
    RETURN i.entity_id AS entity_id, i.description AS description,
           d.date AS date, app.surface_text AS source_text, doc.doc_id AS doc_id
    """
    try:
        # `columns` must be passed explicitly here -- execute_cypher()
        # defaults to a single ("result",) column, but this query RETURNs
        # 5 named columns. Without this, AGE/asyncpg raises
        # DatatypeMismatchError ("return row and column definition list do
        # not match") -- a second bug alongside the clause-order one above,
        # confirmed live: fixing only the clause order still failed on
        # this until columns was added too.
        rows = await age_client.execute_cypher(
            query, params={"case_id": case_id},
            columns=["entity_id", "description", "date", "source_text", "doc_id"],
        )
    except Exception as exc:
        logger.warning(f"Failed to fetch Incidents for conflict detection in case {case_id}: {exc}")
        return

    if len(rows) < 2:
        return  # Need at least 2 incident mentions to find a contradiction

    # 2. Deterministic Timeline Checks
    # Check if a single Incident node (merged via resolution) has multiple differing OCCURRED_ON dates.
    incident_dates = {}
    for row in rows:
        eid = row["entity_id"]
        date_val = row["date"]
        if date_val:
            if eid not in incident_dates:
                incident_dates[eid] = set()
            incident_dates[eid].add(date_val)

    for eid, dates in incident_dates.items():
        if len(dates) > 1:
            # Self-conflict on a single merged incident node
            logger.info(f"Deterministic conflict found: Incident {eid} has multiple dates: {dates}")
            try:
                # We can write a self-referential CONFLICTS_WITH edge or just log it.
                # Writing a self-referential edge:
                await versioning.write_edge(
                    edge_label="CONFLICTS_WITH",
                    from_label="Incident", from_match={"entity_id": eid},
                    to_label="Incident", to_match={"entity_id": eid},
                    properties={"basis": f"Deterministic: Multiple contradictory dates found for the same incident: {dates}"},
                    source_doc_id="system", confidence=1.0,
                )
            except Exception as e:
                logger.error(f"Failed to write deterministic CONFLICTS_WITH edge: {e}")

    # 3. Grounded LLM Narrative Comparison
    prompt = "Here are the extracted incidents:\n\n"
    for idx, row in enumerate(rows):
        prompt += f"--- Incident {row['entity_id']} ---\n"
        prompt += f"Description: {row['description']}\n"
        prompt += f"Date: {row['date']}\n"
        prompt += f"Source text: {row['source_text']}\n\n"

    try:
        response = await call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_message=prompt[:20000],
            temperature=0.0,
            max_tokens=1000,
        )
    except Exception as exc:
        logger.error(f"conflict_detection LLM call failed for case {case_id}: {exc}")
        raise  # Reraise so ingestion job fails

    parsed = parse_json_response(response, context="conflict_detection")
    if not isinstance(parsed, list):
        return

    # 4. Grounding Verification & Edge Attachment
    for item in parsed:
        inc_a = item.get("incident_a_id")
        inc_b = item.get("incident_b_id")
        basis = item.get("basis")
        quote_a = item.get("quote_a", "")
        quote_b = item.get("quote_b", "")

        if not inc_a or not inc_b or not basis:
            continue

        # Grounding check: the LLM must supply a verbatim quote for each side —
        # an empty/missing quote is a grounding failure, not a free pass. (A
        # bare `if quote_a and quote_a not in text_a` here would silently
        # accept an ungrounded claim whenever the LLM omitted the quote.)
        if not quote_a or not quote_b:
            logger.warning(
                f"Grounding failure: missing quote(s) for conflict {inc_a}/{inc_b} "
                f"(quote_a={'present' if quote_a else 'MISSING'}, "
                f"quote_b={'present' if quote_b else 'MISSING'})."
            )
            continue

        text_a = next((r["source_text"] for r in rows if r["entity_id"] == inc_a), "")
        text_b = next((r["source_text"] for r in rows if r["entity_id"] == inc_b), "")

        # A robust substring check (removing surrounding whitespace or punctuation might be needed,
        # but exact substring is the safest strict grounding).
        if quote_a not in text_a:
            logger.warning(f"Grounding failure: quote A '{quote_a}' not found in Incident {inc_a} source text.")
            continue
        if quote_b not in text_b:
            logger.warning(f"Grounding failure: quote B '{quote_b}' not found in Incident {inc_b} source text.")
            continue

        # Get source_doc_id for provenance
        doc_a = next((r["doc_id"] for r in rows if r["entity_id"] == inc_a), "unknown")

        try:
            await versioning.write_edge(
                edge_label="CONFLICTS_WITH",
                from_label="Incident", from_match={"entity_id": inc_a},
                to_label="Incident", to_match={"entity_id": inc_b},
                properties={"basis": basis},
                source_doc_id=doc_a, 
                confidence=1.0,
            )
            logger.info(f"LLM-grounded conflict detected in case {case_id} between {inc_a} and {inc_b}")
        except Exception as e:
            logger.error(f"Failed to write LLM CONFLICTS_WITH edge for case {case_id}: {e}")
