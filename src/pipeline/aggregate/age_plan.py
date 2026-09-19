# -*- coding: utf-8 -*-
"""
Phase 5D — `AgeQueryPlan`: what the model is allowed to say.

THE PIVOT. Phase 5C protected production by moving model-generated Cypher
away from it (an isolated evaluator database holding a production copy).
That worked — containment was proven destructively — but it required
keeping a synchronised copy of the graph purely so untrusted text had
somewhere safe to run. This module replaces that with security by
construction: the model no longer emits executable syntax at all.

    LLM chooses SEMANTICS.    Trusted code chooses SYNTAX.

WHY A TYPED PLAN IS A STRONGER BOUNDARY THAN A GUARD. `cypher_guard`
inspects text the model wrote and decides whether it looks dangerous. That
is a filtering exercise, and filtering is only ever as good as the next
evasion — Unicode homoglyphs, comment injection, dollar-quote escapes.
A typed plan inverts the problem: mutation is not something we detect and
reject, it is something the model *cannot express*, because no field in
this schema can carry a clause, a function name, or a fragment of query
text. There is no `cypher` field to smuggle `DETACH DELETE` through.

`extra="forbid"` IS LOad-BEARING, and deliberately diverges from the
house default. `src/pipeline/harness/types.py` uses `extra="allow"` for
telemetry shapes where unknown keys are harmless. Here an unknown key is
the whole attack: a model that returns `{"aggregate": ..., "raw_where":
"1=1 OR ..."}` must be REJECTED, not silently parsed with the extra field
dropped. Strict parsing is the first of the three gates (parse → validate
→ compile), and it is the cheapest.

WHAT THIS GRAMMAR COVERS, AND WHY NOT MORE. The fields below were derived
from the 42-case corpus (`corpus.py`'s own axis literals), not invented:
measures `count / count_distinct / sum / avg / min / max`, populations
reached `direct` or `through_relationship`, property filters, single
grouping dimension, and ratios. `median`, `comparison`, `threshold` and
`top_n` are deliberately ABSENT — the corpus marks them
`guard_axis="unimplemented"` and the structured route refuses them, so
emitting them here would create a capability with no counterpart to
reconcile against. A plan grammar is a liability surface; it should cover
what the system computes and stop.

INDEPENDENCE IS PRESERVED ON PURPOSE. This is NOT `AggregateSpec` renamed.
The structured route models a population with nested `PopulationNode` /
`Traversal` / `Predicate` objects and its own compiler; this models a flat
list of match patterns with its own aliasing. They can disagree — and the
recorded Phase 4 case where AGE chose `BELONGS_TO_CASE` while the
structured route used `ASSIGNED_TO` is exactly the disagreement this
architecture exists to surface. Removing AGE's ability to emit arbitrary
syntax must not remove its ability to reach a different semantic answer.
"""
from __future__ import annotations

import enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ══════════════════════════════════════════════════════════════════════
# Closed enumerations
#
# Every construct that affects emitted SYNTAX is an enum member, never a
# free string. A value absent from these enums cannot be compiled, so an
# unexpected string becomes a parse failure rather than something
# interpolated into a query.
# ══════════════════════════════════════════════════════════════════════

class Direction(str, enum.Enum):
    """Direction of travel along a relationship.

    Matches `spec.Traversal.direction`'s semantics ("out" follows the edge
    as stored) so registry fanout lookups mean the same thing in both
    routes. ANY is included because the model may legitimately not know
    orientation; the validator resolves it against the registry.
    """

    OUT = "OUT"
    IN = "IN"
    ANY = "ANY"


class Operator(str, enum.Enum):
    """Comparison operators. A closed map to Cypher text lives in the
    compiler — there is no path from a model string to an operator."""

    EQ = "EQ"
    NE = "NE"
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"
    IN = "IN"
    IS_NULL = "IS_NULL"
    IS_NOT_NULL = "IS_NOT_NULL"


class Aggregate(str, enum.Enum):
    """Aggregations the compiler has an emitter for.

    MEDIAN is absent: AGE has no percentile function and the corpus marks
    it unimplemented. Adding it here without an emitter would let a plan
    validate and then fail at compile time — a worse failure than refusing
    at the plan boundary.
    """

    COUNT = "COUNT"
    COUNT_DISTINCT = "COUNT_DISTINCT"
    SUM = "SUM"
    AVG = "AVG"
    MIN = "MIN"
    MAX = "MAX"


class Grain(str, enum.Enum):
    """What one counted unit IS. Carried so reconciliation can tell
    "92 distinct accused" from "94 accused involvements" — two correct
    numbers answering different questions."""

    ENTITY = "ENTITY"
    RELATIONSHIP = "RELATIONSHIP"
    ROLE_PAIR = "ROLE_PAIR"
    EVENT = "EVENT"
    RECORD = "RECORD"


#: Aliases are assigned by the compiler, never by the model — but the plan
#: still needs to REFER to a matched node. These are the only names a plan
#: may use, and they map to compiler-generated aliases. A closed set means
#: a model cannot supply `n) RETURN 1 //` as an "alias".
AliasName = Literal["a", "b", "c", "d"]


class _Strict(BaseModel):
    """Base: unknown fields are an error, not noise.

    See the module docstring — this is the divergence from the harness's
    `extra="allow"` convention, and it is the point rather than an
    oversight.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# ══════════════════════════════════════════════════════════════════════
# Plan components
# ══════════════════════════════════════════════════════════════════════

class MatchPattern(_Strict):
    """One `(a:Label)-[:REL]->(b:Label)` hop.

    Labels and relationship types are plain strings here because they must
    be checked against the LIVE registry, not a hardcoded enum — the graph
    gains labels through ingestion, and a frozen enum would either go stale
    or force edits per deployment. The validator rejects anything the
    registry does not know, and the compiler refuses to interpolate an
    identifier the validator did not clear.
    """

    from_alias: AliasName
    from_label: str
    rel_type: str
    direction: Direction
    to_alias: AliasName
    to_label: str
    #: OPTIONAL MATCH. Needed for "cases with no officer" style questions,
    #: which AGE cannot express as `WHERE NOT (a)-[:R]->(:L)`.
    optional: bool = False


class Filter(_Strict):
    """One property predicate.

    `value` is DATA. It is bound as a query parameter by the compiler and
    can never become syntax — an Urdu station name, an integer, and a
    string containing `' OR 1=1 --` are all just `$pN`. IS_NULL and
    IS_NOT_NULL take no value.
    """

    alias: AliasName
    property: str
    op: Operator
    value: Optional[Any] = None


class GroupDimension(_Strict):
    """A single grouping dimension. One only — see the validator's cost
    limits; multi-dimensional grouping is not in the corpus and each extra
    dimension multiplies result rows."""

    alias: AliasName
    property: str


class AgeQueryPlan(_Strict):
    """The complete, closed description of one aggregate the model wants.

    Note what is NOT here, and cannot be added by a model: `cypher`,
    `raw_query`, `query_text`, `clause`, `custom_expression`, `raw_where`,
    `raw_return`, `sql`, `function_name`, `graph_name`, `database_name`,
    `database_url`. There is no escape hatch back into executable syntax,
    and `extra="forbid"` means inventing one is a parse error.
    """

    #: The population's own entity — what actually gets measured. Every
    #: aggregate is ultimately "how many/much of THIS".
    root_alias: AliasName
    root_label: str

    patterns: tuple[MatchPattern, ...] = ()
    filters: tuple[Filter, ...] = ()

    aggregate: Aggregate
    #: Alias the aggregate applies to. For COUNT_DISTINCT / SUM / AVG /
    #: MIN / MAX this is the node whose property is measured.
    aggregate_alias: AliasName
    #: Property for value measures (SUM/AVG/MIN/MAX) and for
    #: COUNT_DISTINCT's distinct key. None means count rows.
    aggregate_property: Optional[str] = None

    group_by: Optional[GroupDimension] = None
    limit: Optional[int] = Field(default=None, ge=1, le=1000)

    #: Semantics the model is asserting, carried into reconciliation so a
    #: disagreement can be classified as SEMANTICALLY_DIFFERENT rather than
    #: CONFLICT when the two routes measured different things.
    grain: Grain
    population_description: str = Field(default="", max_length=300)
    reasoning: str = Field(default="", max_length=500)

    #: Phase 7B: which APPROVED validation profile the model believes
    #: applies. A plain string, validated against the trusted catalogue in
    #: `gates.py` — not an enum here, so an unrecognised value produces a
    #: named refusal from the policy rather than an opaque parse error, and
    #: the selection logic stays in one place.
    #:
    #: SELECTING, NOT DEFINING. The model may name one catalogue entry. It
    #: cannot describe a gate, add a condition, weight a check or set a
    #: threshold, because no field here can carry one and `extra="forbid"`
    #: rejects an invented one. Trusted code independently derives the
    #: MINIMUM profile from the typed query, so a weak or absent selection
    #: cannot reduce what is enforced.
    gate_profile_id: Optional[str] = Field(default=None, max_length=64)


class PlanRefusal(_Strict):
    """The model declining, which is a legitimate outcome and not an error."""

    refuse: str = Field(min_length=1, max_length=500)


#: Field names that must never appear in the plan schema. Asserted by test
#: rather than merely intended: if someone later adds a convenience field
#: called `raw_where`, that test fails loudly instead of the boundary
#: quietly dissolving.
FORBIDDEN_PLAN_FIELDS = frozenset({
    "cypher", "raw_query", "query_text", "clause", "custom_expression",
    "raw_where", "raw_return", "sql", "function_name", "graph_name",
    "database_name", "database_url", "graph", "database", "dsn", "role",
    # Phase 7B — the model selects a profile id; it never defines policy.
    "custom_gates", "gate_expression", "gate_code", "custom_weights",
    "custom_thresholds", "gates", "gate_definition", "confidence_weight",
})
