# -*- coding: utf-8 -*-
"""
Phase 7 — which source OWNS each constraint.

THE PROBLEM. Some questions need facts whose authoritative representations
live in different places. "How many people aged 20-30 were involved in cases
during 2024?" needs `Person.age` (graph), a person-case relationship
(graph), and `incident_date` (Postgres — the AGE `Case` node carries no date
at all). Forcing one route to own every field is what produced Phase 6's
worst failure: the AGE planner, unable to express a date, silently dropped
the restriction and counted all 73 cases where the truth was 51.

THE MODEL DOES NOT CHOOSE A BACKEND. It may state a semantic requirement
("filter on incident_date"); trusted code resolves which source answers it.
That separation is the same one `LogicalField` was built for in Phase 0 —
this module is the lookup layer over it, not a new registry.

NOTHING HERE IS HARDCODED PER QUESTION. Authority comes from the live
registry's measurements. `incident_date` resolves to Postgres because the
registry measured `cases.incident_date` present on 64/73 while the graph
`Case` node has no date property — not because this file says so.

WHEN AUTHORITY IS UNKNOWN, REFUSE. A constraint whose authority cannot be
resolved is `UNRESOLVED`, and the caller must refuse rather than guess a
source. Guessing is how a filter silently becomes approximate.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import temporal


class Source(str, enum.Enum):
    """Where a constraint is executed. Not chosen by any model."""

    POSTGRES = "postgres"
    GRAPH = "graph"
    #: Authority could not be established from the registry. The caller
    #: refuses; it does not fall back to a guess.
    UNRESOLVED = "unresolved"


class ConstraintKind(str, enum.Enum):
    """What a constraint restricts. Drives which executor handles it."""

    #: A date/time bound resolved to a case-id allow-list.
    TEMPORAL = "temporal"
    #: A property comparison on a node (`Person.age >= 20`).
    PROPERTY = "property"
    #: A path that must exist (`Person -> Case -> Station -> District`).
    RELATIONSHIP = "relationship"


@dataclasses.dataclass(frozen=True)
class AuthorityDecision:
    """Where one constraint will be executed, and on what evidence.

    `basis` carries the registry's own `authority_basis` string so a reader
    of a receipt can see WHY Postgres owns `incident_date` without having to
    re-derive it. An unresolved decision carries the reason instead.
    """

    field: str
    kind: ConstraintKind
    source: Source
    physical_path: Optional[str] = None
    present_n: Optional[int] = None
    total_n: Optional[int] = None
    basis: str = ""
    reason: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return self.source is not Source.UNRESOLVED

    @property
    def presence_rate(self) -> Optional[float]:
        if self.present_n is None or not self.total_n:
            return None
        return self.present_n / self.total_n

    def describe(self) -> str:
        if not self.resolved:
            return f"{self.field}: UNRESOLVED ({self.reason})"
        rate = (
            f" present {self.present_n}/{self.total_n}"
            if self.present_n is not None else ""
        )
        return (
            f"{self.field} -> {self.source.value}:{self.physical_path}{rate}"
        )


def resolve_authority(
    snapshot: reg.RegistrySnapshot,
    field: str,
    kind: ConstraintKind,
) -> AuthorityDecision:
    """Resolve one constraint to its authoritative source.

    `field` is a logical name (`incident_date`) or a `Label.property` path
    (`Person.age`) — both forms appear in existing specs, so both are
    accepted rather than forcing callers to normalise first.
    """
    # ── Temporal: the registry AND the temporal module must agree ─────
    #
    # `supported_temporal_fields()` is a deliberate safety boundary — a
    # temporal restriction runs a real query, so the set of readable
    # columns is fixed in code rather than derived from a string. A field
    # the registry knows but temporal.py cannot execute is UNRESOLVED, not
    # "close enough".
    if kind is ConstraintKind.TEMPORAL:
        supported = temporal.supported_temporal_fields(snapshot)
        if field not in supported:
            return AuthorityDecision(
                field=field, kind=kind, source=Source.UNRESOLVED,
                reason=(
                    f"no executable temporal authority for {field!r}; "
                    f"supported: {sorted(supported) or 'none'}"
                ),
            )
        lf = snapshot.logical_field(field)
        return AuthorityDecision(
            field=field, kind=kind, source=Source.POSTGRES,
            physical_path=lf.path if lf else f"cases.{field}",
            present_n=lf.present_n if lf else None,
            total_n=lf.total_n if lf else None,
            basis=lf.authority_basis if lf else "",
        )

    # ── Relationship: always the graph; it is the only source with edges ──
    if kind is ConstraintKind.RELATIONSHIP:
        return AuthorityDecision(
            field=field, kind=kind, source=Source.GRAPH,
            physical_path=field,
            basis="relationships exist only in the evidence graph",
        )

    # ── Property: the registry's logical field decides ────────────────
    lf = snapshot.logical_field(field)
    if lf is not None:
        return AuthorityDecision(
            field=field, kind=kind,
            source=Source.POSTGRES if lf.source == "postgres" else Source.GRAPH,
            physical_path=lf.path,
            present_n=lf.present_n, total_n=lf.total_n,
            basis=lf.authority_basis,
        )

    # A `Label.property` the registry measured on a node label.
    if "." in field:
        label, prop = field.split(".", 1)
        info = snapshot.entity(label)
        if info is not None:
            presence = info.property_presence(prop)
            if presence is not None:
                return AuthorityDecision(
                    field=field, kind=kind, source=Source.GRAPH,
                    physical_path=f"{label}.{prop}",
                    present_n=presence.present_n, total_n=presence.total_n,
                    basis=f"measured on {label} by the live registry",
                )

    return AuthorityDecision(
        field=field, kind=kind, source=Source.UNRESOLVED,
        reason=(
            f"{field!r} is not a logical field and was not measured on any "
            f"node label; no source can be shown to own it"
        ),
    )
