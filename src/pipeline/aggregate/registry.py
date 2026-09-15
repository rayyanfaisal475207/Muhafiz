# -*- coding: utf-8 -*-
"""
Phase 0 — the schema registry: a mirror of what the data ACTUALLY contains.

WHY THIS IS GENERATED AND NOT HAND-WRITTEN. A hand-written schema doc is an
assumption; this file's whole purpose is to replace assumptions with
measurements, because every wrong-number path found during the architecture
review came from a plausible assumption about the data being false:

  - "Person has an `age` property"          -> true for 19 of 430 nodes.
  - "PoliceStation has `canonical_name`"    -> it has `name`; the wrong
                                               spelling returns ONE row,
                                               key NULL, count 73, no error.
  - "Incident has `incident_date`"          -> 0 of 73; Postgres `cases`
                                               has it for 64 of 73.
  - "A Person node is a person"             -> 222 of 430 are merge
                                               tombstones (`merged_into`).
  - "Traversing to Person keeps 73 cases"   -> 73 -> 449 rows.

So the registry records, per label: which properties exist and HOW OFTEN;
per relationship: the observed cardinality and the worst-case fanout; per
logical field: which physical source is authoritative. The validator and
compiler consume these numbers to make wrong queries impossible to express
rather than merely unlikely.

REFRESH MODEL. `build_registry()` reads the live graph and Postgres. It is
a measurement, so it is only as current as the moment it ran — `as_of` is
stamped on the snapshot and belongs in every AggregateReceipt. A cached
snapshot is reused within a request; drift between the snapshot and the
database is a real hazard and is what `tests/test_aggregate_registry.py`'s
conformance test exists to catch (the same discipline
`test_module95_weapon_register_inventory.py` already applies to the weapon
register's column list).

NOT A CAPABILITY LIST. The registry says what the data HAS, not what
questions may be asked. Deciding what is askable is the validator's job,
and deciding how to compute it is the compiler's. Keeping those separate is
what stops this package from becoming 43 templates wearing a new hat.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Cardinality — the fanout classification the compiler is required to obey.
#
# This is the registry's single most safety-critical output. Measured live:
#
#   MATCH (c:Case)                                 ->    73 rows
#   MATCH (c:Case)<-[:BELONGS_TO_CASE]-(p:Person)  ->   449 rows
#   MATCH (c:Case)<-[:BELONGS_TO_CASE]-(a:Address) -> 2,100 rows  (28x)
#
# Every one of those is valid Cypher returning a plausible integer. A
# question like "how many cases involve a suspect?" answered with 449 is
# not a rounding error, it is a different question. The compiler therefore
# refuses to emit a non-DISTINCT count downstream of any traversal whose
# cardinality here is ONE_TO_MANY or MANY_TO_MANY (see compiler.py).
# ══════════════════════════════════════════════════════════════════════
ONE_TO_ONE = "ONE_TO_ONE"
ONE_TO_MANY = "ONE_TO_MANY"
MANY_TO_ONE = "MANY_TO_ONE"
MANY_TO_MANY = "MANY_TO_MANY"

#: Cardinalities for which a traversal can multiply rows in the direction
#: of travel, so an entity-grain count MUST be DISTINCT-ed.
FANNING_CARDINALITIES = frozenset({ONE_TO_MANY, MANY_TO_MANY})


# ══════════════════════════════════════════════════════════════════════
# Tombstone / version semantics — established by the repository, not
# invented here.
#
# `scripts/merge_confirmed_duplicate_persons.py` is the authority: it
# redirects every active edge off a confirmed-SAME_AS component's donor
# nodes onto one survivor, then tags each donor `merged_into=<survivor>`
# and `merged_at=<ts>`. Its own header states the rule this constant
# encodes: "Donor nodes are NEVER deleted — they are tagged `merged_into`
# and kept for provenance."
#
# `src/retrieval/graph_retriever.py` already treats that tag as "inactive",
# filtering `n.merged_into IS NULL` at 8 separate query sites alongside
# `b.superseded_by IS NULL`. This registry adopts the SAME idiom, for the
# same reason, rather than inventing a third convention.
#
# MEASURED IMPACT of not doing so (live, 2026-09-15):
#   MATCH (p:Person)-[:BELONGS_TO_CASE]->(c:Case)
#     RETURN count(DISTINCT p.entity_id)                     -> 429
#   ... WHERE p.merged_into IS NULL AND b.superseded_by IS NULL -> 208
# A 2x over-count on any cross-case person headcount. `src/pipeline/xagg.py`
# references `merged_into` ZERO times across its 72 Cypher sites.
# ══════════════════════════════════════════════════════════════════════

#: Node property whose presence marks an entity as a merge donor (inactive).
TOMBSTONE_PROPERTY = "merged_into"

#: Edge property whose presence marks a relationship version as historical.
SUPERSEDED_PROPERTY = "superseded_by"


@dataclasses.dataclass(frozen=True)
class PropertyInfo:
    """One property of one label, with how often it is actually present.

    `present_n` is the thing that matters. A property present on 19 of 430
    nodes is not "a field Person has" — a filter on it silently discards
    411 nodes, and a percentage computed over the survivors is meaningless
    unless the denominator is stated. coverage.py turns these numbers into
    caveat/refuse decisions; the validator uses them to reject a grouping
    dimension that is too sparse to group by.
    """

    name: str
    present_n: int
    total_n: int

    @property
    def presence_rate(self) -> float:
        return (self.present_n / self.total_n) if self.total_n else 0.0

    @property
    def is_universal(self) -> bool:
        return self.total_n > 0 and self.present_n == self.total_n


@dataclasses.dataclass(frozen=True)
class EntityInfo:
    """One node label as it actually exists in the graph.

    `distinct_key` is the property the compiler DISTINCTs on for ENTITY
    grain. It is recorded per label rather than assumed to be `entity_id`
    because it genuinely varies: Case is keyed by `case_id`, and
    PoliceStation carries `station_id`. Getting this wrong is the
    `{'k': None, 'n': 73}` failure.

    `has_tombstones` says whether this label participates in the
    merge-donor convention above. Only labels that actually carry the
    property get the predicate, so the compiler never emits a filter on a
    property that does not exist for that label.
    """

    label: str
    total_n: int
    distinct_key: Optional[str]
    properties: dict[str, PropertyInfo] = dataclasses.field(default_factory=dict)
    has_tombstones: bool = False
    tombstoned_n: int = 0

    @property
    def active_n(self) -> int:
        """Nodes not marked as merge donors — the real population."""
        return self.total_n - self.tombstoned_n

    def property_presence(self, prop: str) -> Optional[PropertyInfo]:
        return self.properties.get(prop)


@dataclasses.dataclass(frozen=True)
class RelationshipInfo:
    """One (source, relationship, target) triple, with measured fanout.

    `cardinality` is derived from the data, not declared: it compares the
    number of distinct sources and targets against the edge count. The
    compiler consults `is_fanning` to decide whether an entity-grain count
    downstream of this traversal must be DISTINCT-ed, which is the
    structural defence against the 73->449 and 73->2,100 inflations.

    `max_fanout` is retained for result-shape validation: a query whose row
    count exceeds what the registry says is possible for its population is
    evidence of an unintended join, and shapes.py refuses such a result
    rather than serving it.
    """

    source_label: str
    rel_type: str
    target_label: str
    edge_n: int
    distinct_sources_n: int
    distinct_targets_n: int
    max_fanout: int
    cardinality: str
    #: Worst-case rows per target node when traversed in reverse. Measured
    #: separately because it is NOT derivable from `max_fanout`: Address ->
    #: Case has a forward fanout of 1 and a reverse fanout of 2,100.
    max_reverse_fanout: int = 0
    is_versioned: bool = False
    superseded_n: int = 0

    @property
    def is_fanning(self) -> bool:
        """True when travelling source -> target can multiply rows."""
        return self.cardinality in FANNING_CARDINALITIES

    def fans_in_direction(self, direction: str) -> bool:
        """Whether travelling this edge in `direction` multiplies rows.

        CARDINALITY IS DIRECTIONAL, and conflating the two directions is a
        wrong-number path in its own right. Measured live on
        Address-[:BELONGS_TO_CASE]->Case: 2,100 edges, 2,100 distinct
        addresses, but only ONE distinct case. So:

          forward  (Address -> Case): each address reaches one case. No fan.
          backward (Case -> Address): that one case reaches 2,100 addresses.
                                      Max fanout 2,100.

        A single `cardinality` field describes the forward direction only,
        and reading it for a backward traversal would report "MANY_TO_ONE,
        safe" for the single worst inflation in this database. `direction`
        here is the direction of TRAVEL, matching `Traversal.direction`:
        "out" follows the edge as stored, "in" traverses it in reverse.
        """
        if direction == "out":
            # Travelling source -> target: fans when one source reaches
            # several targets.
            return self.edge_n > self.distinct_sources_n
        # Travelling target -> source: fans when one target is reached by
        # several sources.
        return self.edge_n > self.distinct_targets_n

    def max_fanout_in_direction(self, direction: str) -> int:
        """Worst-case rows produced per starting node, in `direction`."""
        if direction == "out":
            return self.max_fanout
        return self.max_reverse_fanout


@dataclasses.dataclass(frozen=True)
class LogicalField:
    """A question-level field name mapped to the source that OWNS it.

    This exists so the interpretation layer never chooses a data source.
    It names a concept ("incident_date"); the registry resolves which
    physical path answers it. Measured reason this is not cosmetic:

      cases.incident_date (Postgres)     -> present 64/73   <- authoritative
      Incident.incident_date (graph)     -> present  0/73   <- absent
      Incident.incident_datetime (graph) -> optional property
      (i)-[:OCCURRED_ON]->(Date)         -> 432 edges / 73 incidents (~6x)

    Three different answers to "the incident date", one of which does not
    exist and one of which fans out. `src/pipeline/xagg.py`'s own
    `_case_completeness_scan()` already recorded the authority decision in
    prose — "a data-quality scan over the real case rows (not the graph,
    which backfills/defaults incident_date and so masks the very gaps this
    question is about — live-confirmed the graph shows only 1 missing date
    where the case rows show 9)". This field makes that decision machine-
    readable instead of a comment, and `authority_basis` cites where it
    came from so it is never silently re-decided.
    """

    name: str
    source: str  # "postgres" | "graph"
    path: str  # "cases.incident_date" | "Person.age" | ...
    present_n: int
    total_n: int
    authority_basis: str
    secondary_paths: tuple[str, ...] = ()

    @property
    def presence_rate(self) -> float:
        return (self.present_n / self.total_n) if self.total_n else 0.0


@dataclasses.dataclass(frozen=True)
class RegistrySnapshot:
    """One measurement of the data model, at `as_of`.

    Immutable on purpose: a spec is validated and compiled against exactly
    one snapshot, and that snapshot's `as_of` goes into the receipt. Two
    aggregates computed minutes apart against different snapshots are not
    comparable, and the receipt is what makes that visible.
    """

    as_of: str
    entities: dict[str, EntityInfo]
    relationships: dict[tuple[str, str, str], RelationshipInfo]
    logical_fields: dict[str, LogicalField]

    # ── Lookups used by the validator and compiler ────────────────────
    def entity(self, label: str) -> Optional[EntityInfo]:
        return self.entities.get(label)

    def relationship(
        self, source_label: str, rel_type: str, target_label: str
    ) -> Optional[RelationshipInfo]:
        return self.relationships.get((source_label, rel_type, target_label))

    def relationships_from(self, source_label: str) -> list[RelationshipInfo]:
        return [r for r in self.relationships.values() if r.source_label == source_label]

    def logical_field(self, name: str) -> Optional[LogicalField]:
        return self.logical_fields.get(name)

    def known_labels(self) -> frozenset[str]:
        return frozenset(self.entities)

    def known_rel_types(self) -> frozenset[str]:
        return frozenset(r.rel_type for r in self.relationships.values())


# ══════════════════════════════════════════════════════════════════════
# Cardinality derivation
# ══════════════════════════════════════════════════════════════════════
def classify_cardinality(
    edge_n: int, distinct_sources_n: int, distinct_targets_n: int
) -> str:
    """Classify a relationship from observed counts alone.

    Deliberately conservative: when both sides fan, the answer is
    MANY_TO_MANY, and MANY_TO_MANY is treated as fanning. An over-strict
    classification costs a redundant DISTINCT (harmless); an under-strict
    one costs a wrong number (the whole point of this package).

    Measured examples this reproduces:
      Case -FILED_AT-> PoliceStation : 73 edges, 73 srcs, 19 tgts
        -> each source has one target, many sources share a target
        -> MANY_TO_ONE, non-fanning travelling source -> target.
      Case <-BELONGS_TO_CASE- Person : 449 edges over 73 targets
        -> travelling Case -> Person multiplies -> ONE_TO_MANY.
    """
    if edge_n <= 0 or distinct_sources_n <= 0 or distinct_targets_n <= 0:
        # No edges observed: nothing can fan, but nothing is known either.
        # ONE_TO_ONE is the least-permissive label that still compiles; the
        # validator separately refuses traversals with no observed edges.
        return ONE_TO_ONE
    source_fans = edge_n > distinct_sources_n
    target_fans = edge_n > distinct_targets_n
    if source_fans and target_fans:
        return MANY_TO_MANY
    if source_fans:
        # One source reaches several targets.
        return ONE_TO_MANY
    if target_fans:
        # Several sources reach one target.
        return MANY_TO_ONE
    return ONE_TO_ONE


# ══════════════════════════════════════════════════════════════════════
# Live measurement
# ══════════════════════════════════════════════════════════════════════

#: Labels whose distinct key is not `entity_id`. Derived by inspecting the
#: live graph (see tests); recorded rather than guessed because using the
#: wrong key is the `{'k': None, 'n': 73}` class of failure.
#:
#: Each entry below was verified against live data — present on 100% of the
#: label's nodes AND fully distinct (StructuredRecord.record_id 713/713
#: distinct=713; District.district_id 9/9 distinct=9). `entity_id` is
#: absent entirely on StructuredRecord and District, which is why the
#: default cannot simply be applied everywhere.
_KNOWN_DISTINCT_KEYS: dict[str, str] = {
    "Case": "case_id",
    "Document": "doc_id",
    "Date": "date",
    "PoliceStation": "station_id",
    "StructuredRecord": "record_id",
    "District": "district_id",
}
_DEFAULT_DISTINCT_KEY = "entity_id"

#: Fallback order when a label's mapped key is absent from the live data.
#: Tried in order; the first property present on EVERY node of the label
#: and fully distinct wins. This exists so a label added by a future
#: ingestion gets a usable key without editing this file — the registry
#: tracks the schema rather than declaring it. A label where none of these
#: qualifies keeps `distinct_key=None`, and the validator then refuses
#: ENTITY grain for it rather than DISTINCT-ing on something unsuitable.
_DISTINCT_KEY_CANDIDATES: tuple[str, ...] = (
    "entity_id", "record_id", "case_id", "doc_id", "station_id",
    "district_id", "date",
)


async def _resolve_distinct_key(
    label: str, counts: dict[str, int], total: int
) -> Optional[str]:
    """The property ENTITY-grain counts DISTINCT on, or None.

    A key is only accepted when it is present on EVERY node of the label
    AND fully distinct across them. Both halves matter and neither is
    assumable:

      - Presence: `entity_id` does not exist at all on StructuredRecord or
        District (0 of 713 / 0 of 9). DISTINCT-ing on it would collapse
        every node to a single NULL bucket — the `{'k': None, 'n': 73}`
        failure, in count form.
      - Distinctness: a property can be universal and still not identify a
        node (`record_type` is present 713/713 but has only 9 distinct
        values). Counting distinct record_types is not counting records.

    So the mapped key is checked first, then the candidate list, and a
    label where nothing qualifies keeps None. That is a deliberate refusal,
    not a gap: the validator rejects ENTITY grain for such a label, which
    is strictly safer than silently counting the wrong thing.

    LIVE EXAMPLE OF THE REFUSAL BEING CORRECT — Address, 2,171 nodes:
    `entity_id` is present on 2,100 of them and fully distinct across those
    2,100, but 71 nodes do not carry it at all (the label has heterogeneous
    shapes, the same way Person spans 22 distinct property sets). Accepting
    it on a 96.7%-present basis would silently fold those 71 into one NULL
    bucket, so "how many distinct addresses" would answer 2,101 — a number
    that looks right and is not. The strict rule refuses ENTITY grain for
    Address until either the ingestion backfills the key or a caller asks
    at a grain that does not need one.
    """
    ordered: list[str] = []
    mapped = _KNOWN_DISTINCT_KEYS.get(label)
    if mapped:
        ordered.append(mapped)
    for cand in (_DEFAULT_DISTINCT_KEY,) + _DISTINCT_KEY_CANDIDATES:
        if cand not in ordered:
            ordered.append(cand)

    from src.graph import age_client

    for cand in ordered:
        if counts.get(cand, 0) != total:
            continue  # not present on every node
        try:
            rows = await age_client.execute_cypher(
                f"MATCH (n:{label}) RETURN count(DISTINCT n.{cand}) AS d",
                columns=["d"],
            )
        except Exception as exc:  # noqa: BLE001 - a bad candidate is not fatal
            logger.warning(
                "registry: distinct-key probe failed for %s.%s: %s", label, cand, exc
            )
            continue
        if rows and int((rows[0] or {}).get("d") or 0) == total:
            return cand

    logger.warning(
        "registry: no distinct key for label %r (%d nodes) — ENTITY grain "
        "will be refused for it.",
        label, total,
    )
    return None


async def _measure_entity(label: str) -> Optional[EntityInfo]:
    """Measure one label: population, property presence, tombstone count.

    Property presence is computed from `keys(n)` rather than from a
    per-property `IS NOT NULL` query, because AGE stores absent properties
    as genuinely absent (not NULL) and the two are not distinguishable
    downstream — `keys()` is what tells us a Person node HAS no `age` key
    at all, which is the distinction coverage.py needs to report honestly.
    """
    from src.graph import age_client

    rows = await age_client.execute_cypher(
        f"MATCH (n:{label}) RETURN keys(n) AS k", columns=["k"]
    )
    total = len(rows)
    if total == 0:
        return None

    counts: dict[str, int] = {}
    for row in rows:
        for key in row.get("k") or []:
            counts[key] = counts.get(key, 0) + 1

    props = {
        name: PropertyInfo(name=name, present_n=n, total_n=total)
        for name, n in sorted(counts.items())
    }

    tombstoned = counts.get(TOMBSTONE_PROPERTY, 0)
    distinct_key = await _resolve_distinct_key(label, counts, total)

    return EntityInfo(
        label=label,
        total_n=total,
        distinct_key=distinct_key,
        properties=props,
        has_tombstones=tombstoned > 0,
        tombstoned_n=tombstoned,
    )


async def _measure_relationships() -> dict[tuple[str, str, str], RelationshipInfo]:
    """Measure every (source, rel, target) triple present in the graph.

    One pass enumerates the triples and their edge counts; a second pass
    measures, per triple, the distinct endpoint counts and the worst
    observed fanout. Fanout is measured rather than inferred because
    `max_fanout` is what shapes.py uses to recognise an impossible row
    count later.
    """
    from src.graph import age_client

    triples = await age_client.execute_cypher(
        "MATCH (a)-[r]->(b) "
        "RETURN label(a) AS src, type(r) AS rel, label(b) AS dst, count(*) AS n",
        columns=["src", "rel", "dst", "n"],
    )

    out: dict[tuple[str, str, str], RelationshipInfo] = {}
    for t in triples:
        src, rel, dst = t.get("src"), t.get("rel"), t.get("dst")
        edge_n = int(t.get("n") or 0)
        if not src or not rel or not dst:
            continue

        src_key = _KNOWN_DISTINCT_KEYS.get(src, _DEFAULT_DISTINCT_KEY)
        dst_key = _KNOWN_DISTINCT_KEYS.get(dst, _DEFAULT_DISTINCT_KEY)

        detail = await age_client.execute_cypher(
            f"MATCH (a:{src})-[r:{rel}]->(b:{dst}) "
            f"RETURN count(DISTINCT a.{src_key}) AS s, count(DISTINCT b.{dst_key}) AS t",
            columns=["s", "t"],
        )
        s_n = int((detail[0] or {}).get("s") or 0) if detail else 0
        t_n = int((detail[0] or {}).get("t") or 0) if detail else 0

        fan = await age_client.execute_cypher(
            f"MATCH (a:{src})-[r:{rel}]->(b:{dst}) "
            f"WITH a.{src_key} AS k, count(r) AS c RETURN max(c) AS mx",
            columns=["mx"],
        )
        max_fanout = int((fan[0] or {}).get("mx") or 0) if fan else 0

        # Reverse fanout, measured independently — see
        # `RelationshipInfo.fans_in_direction()` for why it cannot be
        # inferred from the forward figure.
        rfan = await age_client.execute_cypher(
            f"MATCH (a:{src})-[r:{rel}]->(b:{dst}) "
            f"WITH b.{dst_key} AS k, count(r) AS c RETURN max(c) AS mx",
            columns=["mx"],
        )
        max_reverse_fanout = int((rfan[0] or {}).get("mx") or 0) if rfan else 0

        sup = await age_client.execute_cypher(
            f"MATCH (a:{src})-[r:{rel}]->(b:{dst}) "
            f"WHERE r.{SUPERSEDED_PROPERTY} IS NOT NULL RETURN count(r) AS n",
            columns=["n"],
        )
        superseded_n = int((sup[0] or {}).get("n") or 0) if sup else 0

        out[(src, rel, dst)] = RelationshipInfo(
            source_label=src,
            rel_type=rel,
            target_label=dst,
            edge_n=edge_n,
            distinct_sources_n=s_n,
            distinct_targets_n=t_n,
            max_fanout=max_fanout,
            max_reverse_fanout=max_reverse_fanout,
            cardinality=classify_cardinality(edge_n, s_n, t_n),
            # Versioned iff the convention is actually observed on this
            # triple. Claiming otherwise would make the compiler emit a
            # predicate on a property that does not exist.
            is_versioned=superseded_n > 0,
            superseded_n=superseded_n,
        )
    return out


async def _measure_logical_fields(
    entities: dict[str, EntityInfo],
) -> dict[str, LogicalField]:
    """Build the logical-field authority table.

    ONLY authority decisions the repository has already established are
    recorded here. `incident_date` is the one such decision found in the
    code (`xagg._case_completeness_scan()`'s docstring, quoted in
    `LogicalField`), and it is transcribed with its basis. Every other
    entry is a plain single-source mapping with its measured presence — no
    authority is invented, per the Phase 0 brief.
    """
    from src.database.postgres import get_session
    from sqlalchemy import text

    fields: dict[str, LogicalField] = {}

    async with get_session() as db:
        res = await db.execute(
            text(
                "SELECT count(*) AS n, count(incident_date) AS d, "
                "count(police_station) AS ps, count(crime_category) AS cc, "
                "count(investigation_status) AS st, count(fir_number) AS fir, "
                "count(investigation_officer) AS io, count(location) AS loc "
                "FROM cases"
            )
        )
        row = res.fetchone()

    total = int(row[0] or 0)
    postgres_case_fields = {
        "incident_date": (int(row[1] or 0), "cases.incident_date"),
        "police_station": (int(row[2] or 0), "cases.police_station"),
        "crime_category": (int(row[3] or 0), "cases.crime_category"),
        "investigation_status": (int(row[4] or 0), "cases.investigation_status"),
        "fir_number": (int(row[5] or 0), "cases.fir_number"),
        "investigation_officer": (int(row[6] or 0), "cases.investigation_officer"),
        "location": (int(row[7] or 0), "cases.location"),
    }

    for name, (present, path) in postgres_case_fields.items():
        basis = "single structured source (cases table)"
        secondary: tuple[str, ...] = ()
        if name == "incident_date":
            # The one authority decision the repository already made.
            basis = (
                "xagg._case_completeness_scan(): case rows are authoritative "
                "because the graph backfills/defaults incident_date and masks "
                "the gaps (live-confirmed 1 missing in graph vs 9 in rows)"
            )
            secondary = ("Incident.incident_datetime", "Incident-[:OCCURRED_ON]->Date")
        fields[name] = LogicalField(
            name=name,
            source="postgres",
            path=path,
            present_n=present,
            total_n=total,
            authority_basis=basis,
            secondary_paths=secondary,
        )

    # Graph-only fields: every property of every measured label that has no
    # Postgres counterpart. Named `<Label>.<prop>` so a spec references a
    # concrete path and the compiler never has to guess which label a bare
    # property name belonged to.
    for label, info in entities.items():
        for prop, pinfo in info.properties.items():
            if prop in (TOMBSTONE_PROPERTY, "merged_at", "as_of", "source_doc_id"):
                continue
            key = f"{label}.{prop}"
            if key in fields:
                continue
            fields[key] = LogicalField(
                name=key,
                source="graph",
                path=key,
                present_n=pinfo.present_n,
                total_n=pinfo.total_n,
                authority_basis="single structured source (graph property)",
            )

    return fields


async def build_registry(labels: Optional[list[str]] = None) -> RegistrySnapshot:
    """Measure the live data model and return an immutable snapshot.

    `labels` defaults to every label observed on an edge endpoint plus the
    standalone ones, so a new label added by a future ingestion appears
    without editing this file — the registry tracks the schema rather than
    declaring it.
    """
    from src.graph import age_client

    if labels is None:
        rows = await age_client.execute_cypher(
            "MATCH (a)-[r]->(b) RETURN label(a) AS src, label(b) AS dst",
            columns=["src", "dst"],
        )
        found: set[str] = set()
        for row in rows:
            for key in ("src", "dst"):
                if row.get(key):
                    found.add(row[key])
        labels = sorted(found)

    entities: dict[str, EntityInfo] = {}
    for label in labels:
        try:
            info = await _measure_entity(label)
        except Exception as exc:  # noqa: BLE001 - one bad label must not kill the snapshot
            logger.warning("registry: could not measure label %r: %s", label, exc)
            continue
        if info is not None:
            entities[label] = info

    relationships = await _measure_relationships()
    logical_fields = await _measure_logical_fields(entities)

    snapshot = RegistrySnapshot(
        as_of=datetime.now(timezone.utc).isoformat(),
        entities=entities,
        relationships=relationships,
        logical_fields=logical_fields,
    )
    logger.info(
        "registry: measured %d label(s), %d relationship triple(s), %d logical field(s)",
        len(entities), len(relationships), len(logical_fields),
    )
    return snapshot


# ══════════════════════════════════════════════════════════════════════
# Per-request cache.
#
# `build_registry()` issues several queries per label and per relationship
# triple, so it must not run once per aggregate. This cache is deliberately
# process-local and explicit rather than an `@lru_cache`: a snapshot is a
# measurement with an `as_of`, and a caller that wants a fresh one must be
# able to say so.
# ══════════════════════════════════════════════════════════════════════
_CACHED: Optional[RegistrySnapshot] = None


async def get_registry(*, refresh: bool = False) -> RegistrySnapshot:
    global _CACHED
    if _CACHED is None or refresh:
        _CACHED = await build_registry()
    return _CACHED


def clear_registry_cache() -> None:
    """Drop the cached snapshot (tests, and after an ingestion run)."""
    global _CACHED
    _CACHED = None
