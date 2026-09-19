# -*- coding: utf-8 -*-
"""
Verification policy V2 — deciding, BEFORE execution, whether a question
must be answered by more than the structured route.

WHY THIS IS DECIDED BEFORE EXECUTION. Running the AGE route is the
expensive act being gated (median 41 s, max 92 s measured across the
42-case corpus). A policy keyed on `CoverageVerdict` or `GateEvaluation`
would be deciding after the cost was already paid, so neither is an input
here. Registry presence and measured fanout ARE available beforehand, and
they are what the triggers read.

WHY THESE FOUR TRIGGERS AND NOT THE EIGHT THAT PRECEDED THEM. V1 proposed
eight. Replayed against the Phase 7D corpus, V1 verified 32 of 42 cases
(76%) and caught 3 of the 5 real disagreements. It forced verification on
traversal and sparsity — which between them caught nothing that fanning
did not already cover — and served structured-only on the two cases that
actually disagreed:

  weapons_unlicensed_property_filter   count_distinct/ENTITY/Weapon
      Weapon.license_status contains <literal>, presence 30/32
  malkhana_records                     count_distinct/RECORD/StructuredRecord
      StructuredRecord.record_type eq <literal>, presence 713/713

Both are single-source, zero-traversal, dense-property counts — precisely
V1's "structured-only is safe" profile. The signal V1 missed was not
sparsity or traversal. It was (a) a property-value filter, where the model
must choose a literal and `'unlicensed'` instead of `'بغیر لائسنس'`
returns 0 where the truth is 30, and (b) a non-ENTITY grain declaration,
where the two routes counted different KINDS of thing.

So M3 and M4 exist because a measured failure class demanded them, and A1
(non-fanning traversal), A2 (sparse field) and A3 (grouping) are advisory
because in the measured run they fired on 19 cases between them and caught
nothing. That is the whole basis of the classification: MANDATORY means a
failure class was observed, ADVISORY means it was not.

WHAT IS DELIBERATELY NOT HERE. No TRUSTED-shape lifecycle, no learned
thresholds, no confidence weighting. Those were designed and then held
back deliberately: each needs persistent state that does not exist yet,
and a first production version that verifies slightly too often is safe in
a way that one tuning itself on unvalidated history is not.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Literal, Optional

# ══════════════════════════════════════════════════════════════════════
# Levels
# ══════════════════════════════════════════════════════════════════════
#: NONE    — structured route only. The answer ships marked unverified.
#: PARTIAL — a second route runs, but AGE cannot produce a COMPARABLE
#:           number for this shape (ratio, median, time-window). Semantic
#:           corroboration only. Must never be reported as independently
#:           verified; see `VerificationDecision.claims_independent`.
#: FULL    — structured + AGE + semantic, then reconciliation.
VerificationLevel = Literal["NONE", "PARTIAL", "FULL"]

NONE: VerificationLevel = "NONE"
PARTIAL: VerificationLevel = "PARTIAL"
FULL: VerificationLevel = "FULL"

#: Ordering for `max(deterministic, llm_recommendation)`. The LLM may only
#: ever RAISE the level, which is the same escalation shape the approved
#: gate-profile policy uses — reused rather than re-invented.
_ORDER: dict[str, int] = {NONE: 0, PARTIAL: 1, FULL: 2}


def level_max(a: VerificationLevel, b: VerificationLevel) -> VerificationLevel:
    """The stricter of two levels."""
    return a if _ORDER[a] >= _ORDER[b] else b


# ══════════════════════════════════════════════════════════════════════
# Trigger identity
# ══════════════════════════════════════════════════════════════════════
#: Operators that force the model to choose a LITERAL VALUE, which is the
#: act M3 guards. `gt`/`lt` against a number are not in this set: a wrong
#: number is a visibly different answer, whereas a wrong string silently
#: matches nothing.
_LITERAL_OPS: frozenset[str] = frozenset({"eq", "contains", "in"})

#: Measures AGE cannot compute in a form comparable to the structured
#: route. Verification for these is PARTIAL, never FULL.
_AGE_INCAPABLE_MEASURES: frozenset[str] = frozenset({"median"})


@dataclasses.dataclass(frozen=True)
class Trigger:
    """One fired rule, with the evidence that fired it.

    `mandatory` distinguishes a rule that forces verification from one that
    only annotates the decision. Both are recorded: an advisory that fires
    repeatedly without ever coinciding with a real disagreement is the
    evidence that would justify deleting it, and an advisory that DOES
    coincide with one is the evidence that would promote it. Discarding
    advisories would throw away the measurement that decides their fate.
    """

    code: str
    mandatory: bool
    reason: str
    detail: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class VerificationDecision:
    """What was decided, and everything needed to audit why."""

    level: VerificationLevel
    triggers: tuple[Trigger, ...] = ()
    #: Level the deterministic rules alone required, before the LLM was
    #: consulted. Kept separate so "the model raised this" is visible and
    #: so a model that tried to LOWER it is detectable after the fact.
    deterministic_level: VerificationLevel = NONE
    llm_recommendation: Optional[VerificationLevel] = None
    llm_rationale: str = ""
    #: Why AGE cannot give a comparable number, when level is PARTIAL.
    partial_reason: str = ""

    @property
    def verify(self) -> bool:
        """Whether any route beyond structured should run."""
        return self.level != NONE

    @property
    def run_age(self) -> bool:
        """AGE runs only at FULL. At PARTIAL it cannot produce a number
        comparable to the structured route, so running it would burn 41-92 s
        to produce something reconciliation must then discard."""
        return self.level == FULL

    @property
    def run_semantic(self) -> bool:
        return self.level in (PARTIAL, FULL)

    @property
    def claims_independent(self) -> bool:
        """Whether the result may be described as independently verified.

        FULL only. This is the flag that stops a PARTIAL result — where a
        second COMPUTATION never ran — from being presented with the same
        assurance as one where two routes independently agreed. Presenting
        PARTIAL as verified would be a stronger claim than the system can
        support, which is worse than not verifying at all.
        """
        return self.level == FULL

    @property
    def mandatory_codes(self) -> tuple[str, ...]:
        return tuple(t.code for t in self.triggers if t.mandatory)

    @property
    def advisory_codes(self) -> tuple[str, ...]:
        return tuple(t.code for t in self.triggers if not t.mandatory)

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "deterministic_level": self.deterministic_level,
            "llm_recommendation": self.llm_recommendation,
            "llm_rationale": self.llm_rationale,
            "claims_independent": self.claims_independent,
            "partial_reason": self.partial_reason,
            "mandatory_triggers": list(self.mandatory_codes),
            "advisory_triggers": list(self.advisory_codes),
            "triggers": [t.to_dict() for t in self.triggers],
        }


# ══════════════════════════════════════════════════════════════════════
# Capability: can AGE produce a number comparable to the structured one?
# ══════════════════════════════════════════════════════════════════════
def age_capability(spec: Any) -> tuple[bool, str]:
    """(comparable, why_not). Decides FULL vs PARTIAL.

    These are not AGE bugs. A ratio needs two populations and a division
    the planner does not emit; a time restriction on `incident_date`
    resolves to Postgres, which the graph cannot see (measured: the graph's
    Incident.incident_date is present on 0 of 73). Declaring the gap is the
    honest alternative to running AGE and quietly ignoring what it said.
    """
    if getattr(spec, "ratio", None) is not None:
        return False, "AGE does not compute ratios; no comparable number exists."
    if getattr(spec, "measure", None) in _AGE_INCAPABLE_MEASURES:
        return False, (
            f"AGE does not compute {spec.measure}; no comparable number exists."
        )
    if getattr(spec, "time_window", None) is not None:
        return False, (
            "The time restriction is Postgres-authoritative; the graph cannot "
            "evaluate it, so an AGE figure would answer a different question."
        )
    if getattr(spec, "compare", None) is not None:
        return False, "AGE does not compute bucketed comparisons."
    return True, ""


# ══════════════════════════════════════════════════════════════════════
# The triggers
# ══════════════════════════════════════════════════════════════════════
def _predicates_of(spec: Any) -> tuple:
    population = getattr(spec, "population", None)
    return tuple(getattr(population, "predicates", ()) or ())


def _is_string_valued(value: Any) -> bool:
    """Whether a predicate's literal is a string (or a list of them).

    `PropertyInfo` records presence, not dtype, so there is no declared
    type to consult. The VALUE the model chose is the honest signal — and
    it is also the exact thing M3 guards, since the risk is the model
    picking a wrong literal, not the column having a particular type.
    """
    if isinstance(value, str):
        return True
    if isinstance(value, (list, tuple)):
        return any(isinstance(v, str) for v in value)
    return False


def _m1_multi_source(reqs: Any) -> Optional[Trigger]:
    """M1 — the answer is assembled from two authorities."""
    if not getattr(reqs, "requires_multiple_sources", False):
        return None
    sources = tuple(getattr(reqs, "distinct_sources", ()) or ())
    return Trigger(
        code="M1_multi_source",
        mandatory=True,
        reason=(
            "The answer is assembled from more than one authority, so no "
            "single route saw the whole computation."
        ),
        detail=f"sources={sorted(sources)}",
    )


def _m2_fanning_traversal(spec: Any, snapshot: Any) -> Optional[Trigger]:
    """M2 — a traversal that can multiply rows.

    Narrower than V1's T2 ("any traversal") on purpose: the measured
    disagreement (208 vs 92) fans, and the two AGREEMENT cases that paid
    for verification without benefit did not. Cardinality is read from the
    registry in the DIRECTION OF TRAVEL, because Address->Case has a
    forward fanout of 1 and a reverse fanout of 2,100.
    """
    population = getattr(spec, "population", None)
    traversals = tuple(getattr(population, "traversals", ()) or ())
    if not traversals or snapshot is None:
        return None

    entity = getattr(population, "entity", None)
    fanning: list[str] = []
    current = entity
    for t in traversals:
        info = _lookup_relationship(snapshot, current, t)
        if info is not None and info.fans_in_direction(t.direction):
            width = info.max_fanout_in_direction(t.direction)
            fanning.append(f"{t.rel}->{t.target} (up to {width} per row)")
        current = t.target

    if not fanning:
        return None
    return Trigger(
        code="M2_fanning_traversal",
        mandatory=True,
        reason=(
            "A traversal in this query can multiply rows, which is the "
            "measured cause of inflated counts (73->449, 73->2100)."
        ),
        detail="; ".join(fanning),
    )


def _lookup_relationship(snapshot: Any, source: Optional[str], traversal: Any) -> Any:
    """Find the registry entry for one hop, in either stored direction.

    A traversal with direction "in" travels target->source, so the stored
    triple has the endpoints the other way round. Looking only for the
    forward spelling would silently find nothing and report "no fanout" for
    exactly the reverse traversals that fan worst.
    """
    rels = getattr(snapshot, "relationships", {}) or {}
    rel_type = getattr(traversal, "rel", None)
    target = getattr(traversal, "target", None)
    direction = getattr(traversal, "direction", "out")

    if direction == "out":
        want = (source, rel_type, target)
    else:
        want = (target, rel_type, source)

    for info in rels.values():
        triple = (info.source_label, info.rel_type, info.target_label)
        if triple == want:
            return info
    # Endpoint unknown (a spec naming an entity the hop does not start
    # from). Fall back to rel_type alone and take the worst case: an
    # unrecognised hop must not be assumed safe.
    candidates = [i for i in rels.values() if i.rel_type == rel_type]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _m3_value_literal(spec: Any) -> Optional[Trigger]:
    """M3 — a filter whose literal the model had to choose.

    The failure this guards is total and silent: a plausible English guess
    against data holding `'بغیر لائسنس'` returns 0 where the truth is 30,
    and nothing in the result looks wrong.
    """
    hits: list[str] = []
    for p in _predicates_of(spec):
        op = getattr(p, "op", None)
        if op not in _LITERAL_OPS:
            continue
        if not _is_string_valued(getattr(p, "value", None)):
            continue
        hits.append(f"{getattr(p, 'field', '?')} {op} {getattr(p, 'value', None)!r}")

    threshold = getattr(spec, "threshold", None)
    if threshold is not None and getattr(threshold, "op", None) in _LITERAL_OPS:
        if _is_string_valued(getattr(threshold, "value", None)):
            hits.append(
                f"threshold {threshold.field} {threshold.op} {threshold.value!r}"
            )

    if not hits:
        return None
    return Trigger(
        code="M3_value_literal",
        mandatory=True,
        reason=(
            "A filter matches a chosen string literal. A wrong literal "
            "returns a clean, plausible, entirely wrong number."
        ),
        detail="; ".join(hits),
    )


def _m4_non_entity_grain(spec: Any) -> Optional[Trigger]:
    """M4 — the spec counts something other than entities."""
    grain = getattr(spec, "grain", None)
    if grain is None or grain == "ENTITY":
        return None
    return Trigger(
        code="M4_non_entity_grain",
        mandatory=True,
        reason=(
            f"Grain is {grain}, not ENTITY. A grain disagreement means the "
            f"routes counted different kinds of thing, not different totals."
        ),
        detail=f"grain={grain}",
    )


# ── Advisory ──────────────────────────────────────────────────────────
def _a1_traversal(spec: Any) -> Optional[Trigger]:
    population = getattr(spec, "population", None)
    traversals = tuple(getattr(population, "traversals", ()) or ())
    if not traversals:
        return None
    return Trigger(
        code="A1_traversal",
        mandatory=False,
        reason="Traversal present but not measured as fanning.",
        detail=f"traversal_count={len(traversals)}",
    )


def _a2_sparse_field(spec: Any, snapshot: Any) -> Optional[Trigger]:
    """A2 — a referenced property is thinly populated.

    Advisory, not mandatory: it fired on 6 corpus cases and coincided with
    zero disagreements, and the risk it names (a figure describing a
    fraction of the population) is already reported at serve time as
    INSUFFICIENT_DATA_COVERAGE by a mechanism that costs nothing.
    """
    if snapshot is None:
        return None
    population = getattr(spec, "population", None)
    entity = getattr(population, "entity", None)
    info = (getattr(snapshot, "entities", {}) or {}).get(entity)
    if info is None:
        return None

    fields = [getattr(p, "field", None) for p in _predicates_of(spec)]
    value_field = getattr(spec, "value_field", None)
    if value_field:
        fields.append(value_field)

    sparse: list[str] = []
    for field in fields:
        if not field:
            continue
        prop = info.property_presence(field.split(".")[-1])
        if prop is not None and prop.presence_rate < 0.5:
            sparse.append(
                f"{field} present on {prop.present_n}/{prop.total_n} "
                f"({prop.presence_rate:.0%})"
            )
    if not sparse:
        return None
    return Trigger(
        code="A2_sparse_field",
        mandatory=False,
        reason="A referenced property is present on under half the population.",
        detail="; ".join(sparse),
    )


def _a3_grouping(reqs: Any) -> Optional[Trigger]:
    if not getattr(reqs, "has_grouping", False):
        return None
    return Trigger(
        code="A3_grouping",
        mandatory=False,
        reason=(
            "Grouped breakdown. Group sums are checkable against the "
            "population arithmetically, without a second route."
        ),
    )


def _a4_profile_adjusted(selection: Any) -> Optional[Trigger]:
    if selection is None:
        return None
    escalated = bool(getattr(selection, "escalated", False))
    corrected = bool(getattr(selection, "corrected", False))
    if not (escalated or corrected):
        return None
    what = "escalated" if escalated else "corrected"
    return Trigger(
        code="A4_profile_adjusted",
        mandatory=False,
        reason=(
            f"The model's gate profile was {what} by trusted code — its "
            f"reading of the query differed from the derived requirement."
        ),
    )


# ══════════════════════════════════════════════════════════════════════
# Decision
# ══════════════════════════════════════════════════════════════════════
def decide(
    spec: Any,
    *,
    snapshot: Any = None,
    requirements: Any = None,
    selection: Any = None,
    llm_recommendation: Optional[str] = None,
    llm_rationale: str = "",
) -> VerificationDecision:
    """Decide the verification level for one spec, before any route runs.

    Deterministic rules are evaluated FIRST and in full, before the LLM
    recommendation is read, so the recommendation cannot influence them —
    it can only be combined afterwards, and only upward.
    """
    from src.pipeline.aggregate import gates as gatelib

    reqs = requirements
    if reqs is None:
        reqs = gatelib.requirements_from_spec(spec, snapshot)

    triggers: list[Trigger] = []
    for t in (
        _m1_multi_source(reqs),
        _m2_fanning_traversal(spec, snapshot),
        _m3_value_literal(spec),
        _m4_non_entity_grain(spec),
    ):
        if t is not None:
            triggers.append(t)

    mandatory_fired = bool(triggers)

    for t in (
        _a1_traversal(spec),
        _a2_sparse_field(spec, snapshot),
        _a3_grouping(reqs),
        _a4_profile_adjusted(selection),
    ):
        if t is not None:
            triggers.append(t)

    # ── Level from the deterministic rules alone.
    comparable, why_not = age_capability(spec)
    if mandatory_fired:
        deterministic = FULL if comparable else PARTIAL
    else:
        deterministic = NONE

    # ── The model may raise, never lower. A recommendation BELOW the
    # deterministic level is not an error and not obeyed: it is recorded
    # and discarded by `level_max`, which is what makes "deterministic
    # rules are authoritative" a property of the code rather than a
    # convention the prompt asks the model to respect.
    recommendation: Optional[VerificationLevel] = None
    if llm_recommendation in (NONE, PARTIAL, FULL):
        recommendation = llm_recommendation  # type: ignore[assignment]

    level = deterministic
    if recommendation is not None:
        level = level_max(deterministic, recommendation)

    # A raise into FULL is still bounded by capability: the model asking
    # for independent verification does not give AGE the ability to
    # compute a median.
    partial_reason = ""
    if level == FULL and not comparable:
        level = PARTIAL
        partial_reason = why_not
    elif level == PARTIAL:
        partial_reason = why_not or (
            "Corroboration only; no second computation of this measure."
        )

    return VerificationDecision(
        level=level,
        triggers=tuple(triggers),
        deterministic_level=deterministic,
        llm_recommendation=recommendation,
        llm_rationale=llm_rationale,
        partial_reason=partial_reason,
    )
