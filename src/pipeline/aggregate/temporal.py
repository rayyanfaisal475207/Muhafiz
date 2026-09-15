# -*- coding: utf-8 -*-
"""
Time-window resolution: a logical date bound -> an authoritative case-id set.

WHY A RESOLVER AND NOT A CYPHER PREDICATE. Measured, 2026-09-15:

    cases.incident_date (Postgres)     present 64/73   <- AUTHORITATIVE
    Case node (AGE)                    keys: as_of, case_id, confidence,
                                             source_doc_id  -- NO DATE
    Incident.incident_datetime (AGE)   present 64/73, optional property
    (i)-[:OCCURRED_ON]->(Date)         432 edges over 73 incidents (~6x fan)

The graph simply has no date on `Case`, and the two graph-side alternatives
are a heterogeneous optional property and a fanning edge. `xagg.py`'s own
`_case_completeness_scan()` already recorded why the case rows win: the
graph "backfills/defaults incident_date and so masks the very gaps this
question is about — live-confirmed the graph shows only 1 missing date
where the case rows show 9".

So a temporal restriction on `incident_date` cannot be compiled into the
AGE query at all. It is resolved HERE, against the authoritative source, to
a set of case ids, and that set is then intersected into the case-id
allow-list the compiler already honours. The restriction is therefore
executed, not approximated — which is the whole point of the exercise that
produced this module.

BOUNDARIES ARE INCLUSIVE, AND THAT IS A DECISION, NOT A DEFAULT.
`cases.incident_date` is a Postgres `date` (verified via
information_schema), so there is no time component and no
end-of-day ambiguity. `start` and `end` are therefore both inclusive:
`[start, end]`. The corpus proves it at the real extremes — the dataset's
own min (2024-09-14) and max (2026-08-01) are each matched by a window
whose bound equals that date.

NULLS ARE EXCLUDED AND REPORTED, never treated as non-matching. 9 of 73
cases carry no `incident_date`. A window silently dropping them would make
"cases in 2024" and "cases not in 2024" sum to 64 rather than 73, and the
missing 9 would be invisible. `resolve_window()` returns the null count so
`coverage.py` can state it.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from datetime import date as _date
from typing import Optional

from src.pipeline.aggregate import registry as reg

logger = logging.getLogger(__name__)

#: Logical fields this module can resolve, mapped to the physical column on
#: `cases` it reads. Deliberately a small, explicit map rather than a
#: string-split on `LogicalField.path`: a temporal restriction executes a
#: real query, so the set of columns it may read is a safety boundary, not
#: a convenience. A field absent here is refused by the validator rather
#: than guessed at.
#:
#: Keys are logical field names as the registry knows them; values are the
#: `cases` column. Both halves are checked against the live registry by
#: `supported_temporal_fields()` below, so a rename in the schema surfaces
#: as a refusal rather than as a wrong answer.
_TEMPORAL_COLUMNS: dict[str, str] = {
    "incident_date": "incident_date",
}

#: ISO date, the only accepted wire form. A bound is a date, not a
#: free-text phrase: parsing "last year" is the interpretation layer's job
#: and must not happen inside the executor.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclasses.dataclass(frozen=True)
class WindowResolution:
    """The outcome of resolving a time window against its authority.

    `case_ids` is the allow-list the compiler intersects. `None` would be
    indistinguishable from "no restriction", so a window that matches
    nothing yields an EMPTY tuple — and the compiler emits an allow-list
    that matches nothing, which is the honest compilation of an empty
    window rather than a silently dropped filter.
    """

    field: str
    source: str
    physical_path: str
    start: Optional[str]
    end: Optional[str]
    case_ids: tuple[str, ...]
    matched_n: int
    excluded_by_window_n: int
    null_field_n: int
    population_n: int
    authority_basis: str

    def describe(self) -> str:
        lo = self.start or "-inf"
        hi = self.end or "+inf"
        return f"{self.field} in [{lo} .. {hi}] (inclusive, via {self.physical_path})"


def supported_temporal_fields(snapshot: reg.RegistrySnapshot) -> frozenset[str]:
    """Logical fields for which a window can actually be executed.

    A field qualifies only when the registry knows it, names Postgres as
    its authority, and this module has a column mapping for it. All three
    are required: the first two make the authority decision the registry's
    rather than the caller's, and the third is what stops the validator
    from accepting a window the compiler cannot honour — the exact defect
    Phase 2 found.
    """
    out: set[str] = set()
    for name, column in _TEMPORAL_COLUMNS.items():
        lf = snapshot.logical_field(name)
        if lf is None:
            logger.warning("temporal: %r is mapped but absent from the registry", name)
            continue
        if lf.source != "postgres":
            logger.warning(
                "temporal: %r is mapped but the registry says its authority is %r",
                name, lf.source,
            )
            continue
        out.add(name)
    return frozenset(out)


def is_valid_bound(value: Optional[str]) -> bool:
    """Whether a bound is a well-formed ISO date (or absent)."""
    return value is None or bool(_ISO_DATE_RE.match(value))


def bounds_ordered(start: Optional[str], end: Optional[str]) -> bool:
    """`start <= end` when both are present. ISO dates compare as strings."""
    if start is None or end is None:
        return True
    return start <= end


async def resolve_window(
    snapshot: reg.RegistrySnapshot,
    field: str,
    start: Optional[str],
    end: Optional[str],
) -> WindowResolution:
    """Resolve a validated window to a case-id allow-list, against authority.

    Assumes the validator has already established that `field` is
    supported and the bounds are well-formed and ordered; this function is
    about EXECUTING the restriction, not deciding whether it is legal.

    The SQL is parameterised and the column name comes from
    `_TEMPORAL_COLUMNS`, never from caller input — the same rule the Cypher
    compiler follows, for the same reason.
    """
    from sqlalchemy import text

    from src.database.postgres import get_session

    lf = snapshot.logical_field(field)
    column = _TEMPORAL_COLUMNS[field]

    # Bind real `datetime.date` objects, not ISO strings.
    #
    # asyncpg type-checks parameters against the column's Postgres type
    # BEFORE the SQL is planned, so a `str` bound against a `date` column
    # fails with "'str' object has no attribute 'toordinal'" even when the
    # SQL wraps it in CAST(:x AS date) — the cast is part of the statement
    # the driver never gets to run. Parsing here keeps the value a bound
    # parameter (never interpolated) while giving the driver the type it
    # requires. `date.fromisoformat` is safe because the validator has
    # already established both bounds match `_ISO_DATE_RE`.
    conditions = [f"{column} IS NOT NULL"]
    params: dict[str, object] = {}
    if start is not None:
        conditions.append(f"{column} >= :start")
        params["start"] = _date.fromisoformat(start)
    if end is not None:
        conditions.append(f"{column} <= :end")
        params["end"] = _date.fromisoformat(end)
    where = " AND ".join(conditions)

    async with get_session() as db:
        matched = await db.execute(
            text(f"SELECT case_id FROM cases WHERE {where}"), params
        )
        case_ids = tuple(str(r[0]) for r in matched.fetchall())

        totals = await db.execute(
            text(f"SELECT count(*), count({column}) FROM cases")
        )
        population_n, non_null_n = totals.fetchone()

    population_n = int(population_n or 0)
    non_null_n = int(non_null_n or 0)
    null_n = max(0, population_n - non_null_n)

    resolution = WindowResolution(
        field=field,
        source=lf.source if lf else "postgres",
        physical_path=lf.path if lf else f"cases.{column}",
        start=start,
        end=end,
        case_ids=case_ids,
        matched_n=len(case_ids),
        # Records that HAVE a date but fall outside the window. Kept apart
        # from `null_field_n` because "outside the window" and "we do not
        # know when this happened" are different facts, and conflating them
        # is how a coverage report becomes misleading.
        excluded_by_window_n=max(0, non_null_n - len(case_ids)),
        null_field_n=null_n,
        population_n=population_n,
        authority_basis=lf.authority_basis if lf else "",
    )
    logger.info(
        "temporal: %s -> %d case(s) matched, %d excluded by window, "
        "%d with no recorded %s (population %d)",
        resolution.describe(), resolution.matched_n,
        resolution.excluded_by_window_n, resolution.null_field_n,
        field, resolution.population_n,
    )
    return resolution


def intersect_case_ids(
    existing: Optional[tuple[str, ...]], window: tuple[str, ...]
) -> tuple[str, ...]:
    """Combine a window's allow-list with any caller-supplied jurisdiction.

    Intersection, never union: a time window may only ever NARROW what the
    caller was already permitted to see. Widening here would turn a filter
    into a privilege escalation.
    """
    if existing is None:
        return window
    allowed = set(existing)
    return tuple(cid for cid in window if cid in allowed)
