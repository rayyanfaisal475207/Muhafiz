# -*- coding: utf-8 -*-
"""
Phase 1 — shadow harness: run OLD and NEW side by side, explain every gap.

READ THIS FIRST. The old engine is NOT the oracle. Phase 0 measured, in the
production path:

  - `merged_into` filtered at 0 of 71 Cypher sites; 222 tombstoned Person
    nodes are therefore counted as people (429 vs 208 on the same query).
  - `superseded_by` filtered at 1 of 71 sites, while 41 traverse versioned
    relationships.
  - grain decisions are baked into each function rather than declared, so
    two defensible readings of one question (4 vs 70) are indistinguishable
    from outside.

So a disagreement is evidence that ONE of them is wrong, and which one is a
question to be answered from the data, not from seniority. Every divergence
below is classified with its reason, and `NEW_CORRECT_OLD_WRONG` is only
asserted where the repository itself defines the semantics (a tombstone is
a tombstone because `merge_confirmed_duplicate_persons.py` says so).

WHAT THIS MODULE MAY NOT DO. It does not import from `router.py`,
`supervisor.py`, or any orchestrator path, and it does not modify
`xagg.py`. It calls `xagg.run_aggregate()` as a plain function, exactly as
a caller would, and compares its return value. Production dispatch is
untouched; this is a separate harness, per the brief.

NO LLM ADJUDICATES ANYTHING HERE. Classification is a deterministic
function of the two receipts and the measured policy differences.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.executor import AggregateOutcome, execute
from src.pipeline.aggregate.receipt import AggregateReceipt
from src.pipeline.aggregate.spec import (
    AggregateSpec,
    FieldPredicate,
    GroupBy,
    PopulationNode,
    Ratio,
    RelationCountPredicate,
    Scope,
    Traversal,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Classification vocabulary. Fixed set — the brief's, verbatim.
# ══════════════════════════════════════════════════════════════════════
AGREE = "AGREE"
NEW_CORRECT_OLD_WRONG = "NEW_CORRECT_OLD_WRONG"
OLD_CORRECT_NEW_WRONG = "OLD_CORRECT_NEW_WRONG"
BOTH_WRONG = "BOTH_WRONG"
SEMANTICALLY_DIFFERENT = "SEMANTICALLY_DIFFERENT"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
UNRESOLVED = "UNRESOLVED"


@dataclasses.dataclass
class Comparison:
    """One old-vs-new comparison, with the semantic axes spelled out.

    The axes exist because equal numbers do not imply equal semantics and
    unequal numbers do not imply a bug. Two engines can agree by accident
    (the tombstones are currently invisible to `INVOLVED_IN` aggregates) and
    disagree correctly (a tombstone-excluding count of Persons SHOULD differ
    from one that includes them).
    """

    aggregate_kind: str
    question: str
    old_result: Any = None
    new_result: Any = None
    old_receipt: Optional[dict] = None
    new_receipt: Optional[AggregateReceipt] = None

    same_result: Optional[bool] = None
    same_grain: Optional[bool] = None
    same_population: Optional[bool] = None
    same_filters: Optional[bool] = None
    same_source_authority: Optional[bool] = None
    same_canonicalization: Optional[bool] = None
    same_superseded_policy: Optional[bool] = None
    same_tombstone_policy: Optional[bool] = None

    coverage_comparison: Optional[str] = None
    classification: str = UNRESOLVED
    explanation: str = ""

    old_ms: Optional[float] = None
    new_compile_ms: Optional[float] = None
    new_exec_ms: Optional[float] = None
    new_total_ms: Optional[float] = None

    def to_row(self) -> dict:
        d = dataclasses.asdict(self)
        if self.new_receipt is not None:
            d["new_receipt"] = self.new_receipt.to_dict()
        return d


# ══════════════════════════════════════════════════════════════════════
# The specs, one per GENERIC aggregate kind.
#
# These are DATA, not code paths: each is a set of bindings over the same
# operators, and adding one costs no compiler change. That is the property
# Phase 0's transcription claimed and this file is the evidence for it —
# if any entry below needed a bespoke emitter, the algebra would have
# failed its own test.
#
# `old_query` is the natural-language text that makes `xagg.run_aggregate()`
# dispatch to the corresponding legacy kind, taken from the phrase lists in
# `xagg.py` so the comparison is like-for-like.
# ══════════════════════════════════════════════════════════════════════
def _scope(role: str = "supervisor") -> Scope:
    return Scope(kind="cross_case", user_role=role, user_id="shadow-harness")


def build_generic_specs() -> dict[str, tuple[str, AggregateSpec]]:
    """`kind -> (old_query_text, new_spec)` for every GENERIC kind.

    Kinds whose legacy dispatch cannot be reached by a single unambiguous
    phrase are omitted here and handled as specialists; the harness reports
    them rather than guessing a phrasing that might dispatch elsewhere.
    """
    sc = _scope()
    out: dict[str, tuple[str, AggregateSpec]] = {}

    # ── total_count: the simplest possible aggregate ──────────────────
    out["total_count"] = (
        "How many cases are there in total?",
        AggregateSpec(
            question_text="How many cases are there in total?",
            measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sc,
        ),
    )

    # ── total_accused_count: THE tombstone/grain case ─────────────────
    # Reaches Person through INVOLVED_IN(role=accused), which is how the
    # legacy aggregate reaches it too.
    out["total_accused_count"] = (
        "How many accused persons are there in total across all cases?",
        AggregateSpec(
            question_text="How many accused persons are there in total across all cases?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Person",
                traversals=(
                    Traversal(
                        rel="INVOLVED_IN", target="Incident", direction="out",
                        role_field="role", role_value="accused",
                    ),
                ),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
    )

    # ── station_total_count ───────────────────────────────────────────
    out["station_total_count"] = (
        "How many police stations are there in total?",
        AggregateSpec(
            question_text="How many police stations are there in total?",
            measure="count_distinct",
            population=PopulationNode(entity="PoliceStation"),
            grain="ENTITY", distinct_key="station_id", scope=sc,
        ),
    )

    # ── graph_recurrence_person: relation-count predicate ─────────────
    out["graph_recurrence_person"] = (
        "Which people appear in more than one case?",
        AggregateSpec(
            question_text="Which people appear in more than one case?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Person",
                predicates=(
                    RelationCountPredicate(
                        rel="BELONGS_TO_CASE", target="Case", op="gt", value=1,
                        count_grain="ENTITY", direction="out",
                    ),
                ),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
    )

    # ── graph_recurrence_weapon: SAME TREE, different bindings ────────
    out["graph_recurrence_weapon"] = (
        "Which weapons appear in more than one case?",
        AggregateSpec(
            question_text="Which weapons appear in more than one case?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Weapon",
                predicates=(
                    RelationCountPredicate(
                        rel="BELONGS_TO_CASE", target="Case", op="gt", value=1,
                        count_grain="ENTITY", direction="out",
                    ),
                ),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
    )

    # ── placeholder_officer_count ─────────────────────────────────────
    out["placeholder_officer_count"] = (
        "How many cases have no investigating officer properly assigned?",
        AggregateSpec(
            question_text="How many officers are recorded?",
            measure="count_distinct",
            population=PopulationNode(entity="Officer"),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
    )

    return out


def build_adversarial_specs() -> dict[str, AggregateSpec]:
    """Specs for the Phase 0 adversarial cases.

    These have no legacy counterpart — they exist to demonstrate safety
    properties, so the harness runs them NEW-only and asserts the outcome
    (a number, or a refusal with a named code).
    """
    sc = _scope()
    out: dict[str, AggregateSpec] = {}

    # (1) OFFICER GRAIN — the mandated 4-vs-70 regression, both readings.
    for grain_name, count_grain in (("entity", "ENTITY"), ("relationship", "RELATIONSHIP")):
        out[f"officer_multi_{grain_name}"] = AggregateSpec(
            question_text="What percentage of incidents involved more than one officer?",
            measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sc,
            ratio=Ratio(
                numerator=PopulationNode(
                    entity="Case",
                    predicates=(
                        RelationCountPredicate(
                            rel="ASSIGNED_TO", target="Officer", op="gt", value=1,
                            count_grain=count_grain, direction="in",
                        ),
                    ),
                ),
                denominator=PopulationNode(entity="Case"),
            ),
        )

    # (2) TOMBSTONES — Person reached WITHOUT a role filter, which is the
    # traversal the 222 donors actually sit on (BELONGS_TO_CASE).
    out["person_via_belongs_to_case"] = AggregateSpec(
        question_text="How many distinct persons are linked to cases?",
        measure="count_distinct",
        population=PopulationNode(
            entity="Person",
            traversals=(Traversal(rel="BELONGS_TO_CASE", target="Case", direction="out"),),
        ),
        grain="ENTITY", distinct_key="entity_id", scope=sc,
    )

    # (3) FANOUT — must refuse without a distinct key.
    out["fanout_case_person_nokey"] = AggregateSpec(
        question_text="How many cases involve a person?",
        measure="count",
        population=PopulationNode(
            entity="Case",
            traversals=(Traversal(rel="BELONGS_TO_CASE", target="Person", direction="in"),),
        ),
        grain="ENTITY", distinct_key=None, scope=sc,
    )
    out["fanout_case_address_nokey"] = AggregateSpec(
        question_text="How many cases have an address?",
        measure="count",
        population=PopulationNode(
            entity="Case",
            traversals=(Traversal(rel="BELONGS_TO_CASE", target="Address", direction="in"),),
        ),
        grain="ENTITY", distinct_key=None, scope=sc,
    )
    # Same traversal WITH a key — must succeed and equal the case count.
    out["fanout_case_person_keyed"] = AggregateSpec(
        question_text="How many cases involve a person?",
        measure="count_distinct",
        population=PopulationNode(
            entity="Case",
            traversals=(Traversal(rel="BELONGS_TO_CASE", target="Person", direction="in"),),
        ),
        grain="ENTITY", distinct_key="case_id", scope=sc,
    )

    # Case + Person + Weapon: two hops, the compound fanout case. Counts
    # Cases, so DISTINCT on case_id must neutralise BOTH traversals.
    out["fanout_case_person_weapon"] = AggregateSpec(
        question_text="How many cases involve both a person and a weapon?",
        measure="count_distinct",
        population=PopulationNode(
            entity="Case",
            traversals=(
                Traversal(rel="BELONGS_TO_CASE", target="Person", direction="in"),
                Traversal(rel="BELONGS_TO_CASE", target="Weapon", direction="in"),
            ),
        ),
        grain="ENTITY", distinct_key="case_id", scope=sc,
    )

    # (4) NULL GROUPING — grouping on a field the label does not carry.
    out["null_grouping"] = AggregateSpec(
        question_text="Which police station handles the most cases?",
        measure="count",
        population=PopulationNode(
            entity="Case",
            traversals=(Traversal(rel="FILED_AT", target="PoliceStation", direction="out"),),
        ),
        grain="ENTITY", distinct_key="case_id", scope=sc,
        group_by=(GroupBy(field="PoliceStation.name",
                          via=(Traversal(rel="FILED_AT", target="PoliceStation"),)),),
    )

    # (5) SPARSE FIELD — Person.age at 19/430 must produce a coverage
    # refusal or caveat, never a bare average.
    out["sparse_age_avg"] = AggregateSpec(
        question_text="What is the average age of accused persons?",
        measure="avg", value_field="Person.age",
        population=PopulationNode(
            entity="Person",
            traversals=(
                Traversal(
                    rel="INVOLVED_IN", target="Incident", direction="out",
                    role_field="role", role_value="accused",
                ),
            ),
        ),
        grain="ENTITY", distinct_key="entity_id", scope=sc,
    )

    # (6) SECURITY — non-supervisor must be denied.
    out["scope_denied"] = AggregateSpec(
        question_text="How many cases are there?",
        measure="count",
        population=PopulationNode(entity="Case"),
        grain="ENTITY", distinct_key="case_id",
        scope=Scope(kind="cross_case", user_role="investigator", user_id="u"),
    )

    return out


# ══════════════════════════════════════════════════════════════════════
# Old-engine invocation
# ══════════════════════════════════════════════════════════════════════
async def run_old(query_text: str, *, user_role: str = "supervisor") -> tuple[Any, float]:
    """Call the legacy engine exactly as a production caller would.

    No monkeypatching, no private entry points: `run_aggregate()` with a
    real gateway, so whatever the production path would compute is what is
    compared.
    """
    from src.data_gateway import get_gateway
    from src.pipeline.xagg import run_aggregate

    gateway = await get_gateway()
    started = time.perf_counter()
    result = await run_aggregate(
        query_text, None, gateway,
        user_id="shadow-harness", user_role=user_role,
    )
    return result, (time.perf_counter() - started) * 1000.0


def _old_scalar(old: dict) -> Optional[int]:
    """Pull the comparable scalar out of a legacy result dict.

    The legacy dicts are heterogeneous by design — each of the 43 functions
    returns its own shape — so this reads the keys those functions actually
    emit, verified by calling them (not guessed):

      total_count          -> {"total_cases": int}
      total_accused_count  -> {"total_accused": int}
      station_total_count  -> {"total_stations": int}
      graph_recurrence     -> {"entity_type", "results": [ ... ]}
                              the RANKED LIST of entities appearing in more
                              than one case; its length is "how many
                              recur".
      relational_aggregate -> {"group_by", "counts": [{"key","count"}],
                               "total_cases_considered": int, ...}
                              a grouped breakdown; `total_cases_considered`
                              is the population it was computed over.

    Returns None when the kind genuinely has no comparable scalar, which
    the harness reports as SEMANTICALLY_DIFFERENT rather than as a numeric
    mismatch — an honest "these answer different questions" instead of a
    manufactured disagreement.
    """
    if not isinstance(old, dict):
        return None

    for key in (
        "total_cases", "total_accused", "total_stations", "count", "total",
    ):
        if key in old and isinstance(old[key], (int, float)):
            return int(old[key])

    # Recurrence families: the ranked list's length is the comparable
    # quantity. `results` is the key `_top_recurring_nodes()` callers emit;
    # the others are kept for older shapes.
    for key in ("results", "top", "entities"):
        if key in old and isinstance(old[key], list):
            return len(old[key])

    # Grouped relational aggregates expose the population they covered.
    if isinstance(old.get("total_cases_considered"), (int, float)):
        return int(old["total_cases_considered"])

    return None


def _old_kind_matches(old: dict, expected_kind: str) -> bool:
    """Whether the legacy engine dispatched where the comparison assumed.

    A mismatch is NOT a harness problem — it is Module 173's keyword
    collision observed live, and it must be reported rather than papered
    over. Measured example: "How many accused persons are there in total
    across all cases?" dispatches to `graph_recurrence` (4 people appearing
    in more than one case), not to a headcount, because the phrase chain
    matched a recurrence keyword first and a match ends dispatch.
    """
    actual = old.get("kind")
    if actual == expected_kind:
        return True
    # `station_or_category_counts` surfaces as `relational_aggregate`.
    if expected_kind == "station_or_category_counts" and actual == "relational_aggregate":
        return True
    if expected_kind.startswith("graph_recurrence") and actual == "graph_recurrence":
        return True
    return False


# ══════════════════════════════════════════════════════════════════════
# Classification — deterministic, no LLM.
# ══════════════════════════════════════════════════════════════════════
def classify(
    snapshot: reg.RegistrySnapshot,
    kind: str,
    old: Optional[dict],
    new: AggregateOutcome,
) -> tuple[str, str]:
    """Classify one comparison from the two results and the known policies."""
    if old is None:
        return INSUFFICIENT_DATA, "Legacy engine produced no comparable result."
    if not new.ok:
        return (
            INSUFFICIENT_DATA,
            f"New engine refused: {new.receipt.refusal_code} — "
            f"{new.receipt.refusal_reason}",
        )

    old_value = _old_scalar(old)
    new_value = new.value

    # DISPATCH CHECK FIRST. If the legacy phrase chain sent this question to
    # a different aggregate family than the one being compared, the two
    # engines are not answering the same question and a numeric comparison
    # would manufacture a disagreement (or, worse, an accidental agreement).
    # This is Module 173's keyword collision observed live, and it is a
    # finding in its own right — the legacy result is not wrong ARITHMETIC,
    # it is a correct answer to a question nobody asked.
    if not _old_kind_matches(old, kind):
        return (
            SEMANTICALLY_DIFFERENT,
            f"Legacy dispatch sent this question to {old.get('kind')!r}, not to "
            f"{kind!r}: the phrase chain is first-match-wins and a match ends "
            f"dispatch (Module 173). OLD computed {old_value} for a DIFFERENT "
            f"question; NEW computed {new_value} for the one asked. Not a "
            f"numeric disagreement — a routing one.",
        )

    if old_value is None:
        return (
            SEMANTICALLY_DIFFERENT,
            f"Legacy kind {old.get('kind')!r} returns a composite structure with "
            f"no single comparable scalar; the new spec computes one figure.",
        )

    if isinstance(new_value, (int, float)) and int(new_value) == int(old_value):
        return AGREE, f"Both engines computed {old_value}."

    # Disagreement. Decide from measured policy differences, not from
    # which engine is older.
    excluded = new.receipt.excluded_merged_n
    injected = new.receipt.injected_predicates

    if excluded and any("merged_into" in p for p in injected):
        return (
            NEW_CORRECT_OLD_WRONG,
            f"OLD={old_value}, NEW={new_value}. The new engine excluded "
            f"{excluded} tombstoned node(s) via `merged_into IS NULL`; the "
            f"legacy path applies that predicate at 0 of 71 Cypher sites. "
            f"`scripts/merge_confirmed_duplicate_persons.py` defines "
            f"`merged_into` as a merge-donor tombstone kept only for "
            f"provenance, and `graph_retriever.py` already excludes it at 8 "
            f"sites, so the exclusion is the repository's own semantics.",
        )

    if any("superseded_by" in p for p in injected):
        return (
            NEW_CORRECT_OLD_WRONG,
            f"OLD={old_value}, NEW={new_value}. The new engine excluded "
            f"superseded relationship versions; `versioning.py` supersedes "
            f"rather than mutating edges, so a superseded edge is a historical "
            f"version and counting it double-counts the current one.",
        )

    # GRAIN-OF-IDENTITY difference: the two engines group by different keys,
    # so they count different things. Documented per kind rather than
    # inferred, because "they disagree" and "they answer different
    # questions" demand opposite responses.
    identity_note = _IDENTITY_GRAIN_NOTES.get(kind)
    if identity_note:
        return SEMANTICALLY_DIFFERENT, (
            f"OLD={old_value}, NEW={new_value}. {identity_note}"
        )

    return (
        UNRESOLVED,
        f"OLD={old_value}, NEW={new_value}, and no measured policy difference "
        f"(tombstones, supersession) explains the gap. Requires manual "
        f"inspection of both populations before either is trusted.",
    )


#: Kinds where OLD and NEW legitimately group by different identity keys.
#: Each entry cites the repository's own statement of the semantics, so the
#: classification is a reading of the code rather than an opinion about it.
_IDENTITY_GRAIN_NOTES: dict[str, str] = {
    "graph_recurrence_weapon": (
        "The two count different things and both are right for their own "
        "question. `xagg._top_recurring_weapon_types()`'s docstring states "
        "the reason: weapon entity_ids are FIR-scoped by construction "
        "(`WEAPON-{id}-{fir_id}`), weapons never go through the cross-case "
        "merge tier Person/Vehicle use, so 'every real Weapon node belongs "
        "to exactly one case, permanently' and per-entity_id grouping "
        "'would always return an empty list — confirmed against the live "
        "graph'. Verified here: 32 nodes, 32 distinct entity_ids, 0 in more "
        "than one case. OLD therefore groups by normalised weapon TYPE "
        "(5 distinct canonical_names; 3 types span >1 case, 1 after the "
        "'بمعہ N گولیاں' suffix is normalised away). NEW's 0 is the correct "
        "answer to 'which weapon OBJECT recurs'; OLD's 1 is the correct "
        "answer to 'which weapon TYPE recurs'. A spec asking the latter "
        "needs a group_by on weapon type, not a recurrence predicate."
    ),
}


def _coverage_note(new: AggregateOutcome) -> str:
    c = new.receipt.coverage
    if c is None:
        return "no coverage report"
    return (
        f"{c.verdict}: expected={c.expected_population_n} "
        f"observed={c.observed_population_n}"
        + (f" field_present={c.field_present_n}" if c.field_present_n is not None else "")
    )


async def compare_kind(
    snapshot: reg.RegistrySnapshot,
    kind: str,
    old_query: str,
    spec: AggregateSpec,
) -> Comparison:
    """Run one kind through both engines and classify the outcome."""
    cmp = Comparison(aggregate_kind=kind, question=spec.question_text)

    try:
        old_result, old_ms = await run_old(old_query)
        cmp.old_receipt = old_result
        cmp.old_result = _old_scalar(old_result)
        cmp.old_ms = old_ms
    except Exception as exc:  # noqa: BLE001
        cmp.classification = INSUFFICIENT_DATA
        cmp.explanation = f"Legacy engine raised: {exc}"
        return cmp

    started = time.perf_counter()
    new = await execute(snapshot, spec)
    cmp.new_total_ms = (time.perf_counter() - started) * 1000.0
    cmp.new_receipt = new.receipt
    cmp.new_result = new.value
    if new.receipt.queries:
        cmp.new_exec_ms = sum(q.duration_ms or 0.0 for q in new.receipt.queries)
        cmp.new_compile_ms = max(0.0, (cmp.new_total_ms or 0.0) - cmp.new_exec_ms)

    cmp.same_result = (
        cmp.old_result is not None
        and isinstance(new.value, (int, float))
        and int(new.value) == int(cmp.old_result)
    )
    # Semantic axes. The legacy engine declares none of these, which is the
    # finding: absence of a policy is not agreement with one.
    cmp.same_grain = None
    cmp.same_population = None
    cmp.same_filters = None
    cmp.same_source_authority = True  # both read the graph for these kinds
    cmp.same_canonicalization = None
    cmp.same_superseded_policy = not any(
        "superseded_by" in p for p in new.receipt.injected_predicates
    )
    cmp.same_tombstone_policy = new.receipt.excluded_merged_n == 0
    cmp.coverage_comparison = _coverage_note(new)

    cmp.classification, cmp.explanation = classify(snapshot, kind, old_result, new)
    return cmp
