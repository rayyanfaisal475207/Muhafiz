# ============================================================
# Identity index — Postgres-backed CNIC/plate/(phone) -> entity_id lookup
# (Graph Scale & Schema Expansion, Milestone A1).
#
# See migrations/021_identity_index.sql for the table's own design
# rationale. This module is the only thing that reads/writes it.
#
# IDENTITY_KEYS names every (label, id_key) pair this index tracks — the
# same primary-identifier mapping entity_resolution.TYPE_PRIMARY_ID_KEY
# already uses for the CNIC-auto-merge tier, kept here as its own constant
# (not imported from entity_resolution.py) to avoid a circular import:
# entity_resolution.py imports THIS module, not the other way around.
# "Officer"/"belt_no" added Milestone B2 (officer identity resolution) —
# added here, in the same module that makes Officer a real graph label,
# rather than deferred to a follow-up, so Officer resolution gets A1's
# O(1) identity-index lookup from day one, not a temporary full-label-scan
# gap the way it would if this addition were left for later.
#
# Maintenance is a single choke point: src.graph.versioning.write_node()
# calls maintain() for every write whose label appears here, mirroring the
# module's own stated purpose ("the one place... actually enforced, not a
# convention every call site has to remember"). Resolution
# (entity_resolution.py) only ever READS this module.
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

from src.database.postgres import get_session

logger = logging.getLogger(__name__)

IDENTITY_KEYS: dict[str, str] = {
    "Person": "cnic",
    "Vehicle": "plate",
    "PhoneNumber": "phone",
    "Officer": "belt_no",
}


async def maintain(label: str, entity_id: str, properties: dict) -> None:
    """
    Upsert this label's identity-index row if `properties` carries a
    non-empty value for its tracked id_key. A no-op for labels not in
    IDENTITY_KEYS, or when the property is absent/empty on this particular
    write (e.g. a later write_node() call that refreshes other properties
    without repeating the CNIC) — that write simply leaves whatever row
    already exists untouched, it never clears it.

    Failure here must never fail the graph write it's riding alongside —
    same resilience contract versioning.py already gives log_audit_event()
    (src/data_gateway/direct_backend.py's log_audit_event wraps its own
    Postgres write in a broad try/except for exactly this reason). Any
    exception is logged and swallowed; the index simply stays stale until
    the next successful write for that identity, and entity_resolution.py's
    fallback-to-graph-scan-on-miss is exactly the safety net that gap
    exists for.
    """
    id_key = IDENTITY_KEYS.get(label)
    if id_key is None:
        return
    id_value = properties.get(id_key)
    if not id_value:
        return

    try:
        async with get_session() as db:
            await db.execute(
                text(
                    """
                    INSERT INTO identity_index (label, id_key, id_value, entity_id, updated_at)
                    VALUES (:label, :id_key, :id_value, :entity_id, now())
                    ON CONFLICT (label, id_key, id_value)
                    DO UPDATE SET entity_id = EXCLUDED.entity_id, updated_at = now()
                    """
                ),
                {
                    "label": label, "id_key": id_key,
                    "id_value": str(id_value).strip(), "entity_id": entity_id,
                },
            )
    except Exception as exc:
        logger.warning(
            "identity_index.maintain: failed to index %s.%s=%r -> %s (%s)",
            label, id_key, id_value, entity_id, exc,
        )


async def lookup(label: str, id_key: str, id_value: str) -> Optional[str]:
    """
    O(1) (Postgres primary-key) lookup of the entity_id currently indexed
    for this exact (label, id_key, id_value). None on a miss — the caller
    (entity_resolution._find_by_primary_id) falls back to the AGE scan,
    it does not treat a miss as "no such entity exists".
    """
    try:
        async with get_session() as db:
            res = await db.execute(
                text(
                    "SELECT entity_id FROM identity_index "
                    "WHERE label = :label AND id_key = :id_key AND id_value = :id_value"
                ),
                {"label": label, "id_key": id_key, "id_value": str(id_value).strip()},
            )
            row = res.first()
            return row[0] if row else None
    except Exception as exc:
        logger.warning(
            "identity_index.lookup: failed for %s.%s=%r (%s) — "
            "falling back to graph scan", label, id_key, id_value, exc,
        )
        return None


async def entity_ids_excluding(label: str, id_key: str, exclude_id_value: str) -> list[str]:
    """
    Every entity_id this (label, id_key) has an indexed value for, OTHER
    than `exclude_id_value` — i.e. every entity known (via this index) to
    carry a DIFFERENT non-empty id_key value than the mention currently
    being resolved.

    entity_resolution._generate_candidates() uses this to shrink the
    name-fallback candidate pool: those entity_ids are already excluded
    from consideration by the hard-block rule (a mention with an id_value
    is never compared by name against a candidate with a different
    non-empty id_value of the same key — see entity_resolution.py's module
    docstring), so there is no need to fetch their full node properties
    from AGE at all. Everything NOT in this list (never indexed for this
    id_key, e.g. an older record with no CNIC on file) is an index "miss"
    and still goes through the graph scan — this is what keeps the
    optimization safe: it only ever removes entities the hard block would
    have discarded anyway, never one still eligible for name comparison.

    Returns [] (rather than raising) on a lookup failure — callers treat
    an empty exclude set as "fall back to the full graph scan", the same
    safe default as an index that has never been populated yet.
    """
    try:
        async with get_session() as db:
            res = await db.execute(
                text(
                    "SELECT entity_id FROM identity_index "
                    "WHERE label = :label AND id_key = :id_key AND id_value != :exclude_id_value"
                ),
                {"label": label, "id_key": id_key, "exclude_id_value": str(exclude_id_value).strip()},
            )
            return [row[0] for row in res.fetchall()]
    except Exception as exc:
        logger.warning(
            "identity_index.entity_ids_excluding: failed for %s.%s (%s) — "
            "falling back to the full graph scan", label, id_key, exc,
        )
        return []
