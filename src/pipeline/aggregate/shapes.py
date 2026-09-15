# -*- coding: utf-8 -*-
"""
Phase 5 — result-shape validation: catch the plausible-but-wrong result.

THE FAILURE THIS EXISTS FOR, reproduced live against this database:

    MATCH (c:Case)-[:FILED_AT]->(s:PoliceStation)
    RETURN s.canonical_name AS k, count(*) AS n
    -> [{'k': None, 'n': 73}]

No exception. One row. A count that matches the known case total, so it
reads as correct. The property is `name`, not `canonical_name`; the wrong
spelling silently collapsed 19 stations into a single NULL bucket. Nothing
upstream can catch this — the Cypher is valid, the schema check passed
(the spec named a logical field), and the number is arithmetically fine.
Only the SHAPE of the result reveals it.

So these are correctness gates, not telemetry. A result that fails one is
never served and never repaired: `shapes.validate_result()` returns a
verdict, and the caller's only legal responses are refuse or clarify. The
brief is explicit that silent repair is forbidden, and repair here would be
guessing what the user meant after the fact.

INVARIANTS ARE ATTACHED TO MEASURE TYPES, NOT QUESTIONS. `numerator <=
denominator` applies to every ratio ever compiled, so it is written once
here rather than per aggregate. That is what keeps this from becoming 43
hand-written checks — the same property the algebra has.

WHERE INVARIANTS MUST *NOT* FIRE. `sum(buckets) == population` is only
valid when the grouping dimension is a true partition: present on every
record and single-valued. Neither holds generally in this corpus —
`investigation_status` is present on 21 of 73 cases, and `crime_category`
can carry several acts in one value ("PPC, Arms Ordinance 1965"), which
xagg.py's own `split_crime_category` exists to handle. Firing the invariant
there would refuse correct results, so it is gated on an explicit
`is_partition` claim and records when it was skipped.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Literal, Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.spec import AggregateSpec

logger = logging.getLogger(__name__)


#: A grouped result whose key is missing on more than this share of rows is
#: refused. Set tight: a legitimate grouping key is normally present on
#: every row, and the measured failure (`canonical_name`) was absent on
#: 100% of them. The margin exists only for genuinely optional dimensions.
MAX_NULL_GROUP_KEY_SHARE = 0.20

ShapeVerdict = Literal["OK", "REFUSE"]


@dataclasses.dataclass(frozen=True)
class ShapeIssue:
    code: str
    message: str


@dataclasses.dataclass(frozen=True)
class ShapeResult:
    verdict: ShapeVerdict
    issues: tuple[ShapeIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return self.verdict == "OK"

    def codes(self) -> tuple[str, ...]:
        return tuple(i.code for i in self.issues)


def _issue(code: str, message: str) -> ShapeIssue:
    return ShapeIssue(code=code, message=message)


# ══════════════════════════════════════════════════════════════════════
# Grouped-result guards
# ══════════════════════════════════════════════════════════════════════
def validate_grouped_rows(
    snapshot: reg.RegistrySnapshot,
    spec: AggregateSpec,
    rows: list[dict],
    *,
    key_column: str = "gkey",
    value_column: str = "value",
) -> ShapeResult:
    """Guard a grouped result against the ways grouping silently fails."""
    issues: list[ShapeIssue] = []

    if not rows:
        # Not automatically an error: a filtered population can legitimately
        # be empty. Coverage decides whether that is reportable; the shape
        # itself is fine.
        return ShapeResult(verdict="OK")

    total = len(rows)
    null_keys = sum(1 for r in rows if r.get(key_column) is None)

    # (1) The measured failure. Every key NULL means the grouping property
    # does not exist under that name — a collapsed group, not a category.
    if null_keys == total:
        issues.append(
            _issue(
                "all_group_keys_null",
                f"Every one of the {total} grouping key(s) is NULL: the grouping "
                f"property is absent under the name used, so all records "
                f"collapsed into one bucket. This is a malformed query, not a "
                f"finding.",
            )
        )
    elif total and (null_keys / total) > MAX_NULL_GROUP_KEY_SHARE:
        issues.append(
            _issue(
                "group_key_mostly_null",
                f"{null_keys} of {total} grouping keys are NULL "
                f"({null_keys / total:.0%}); above "
                f"{MAX_NULL_GROUP_KEY_SHARE:.0%} the NULL bucket misrepresents "
                f"the distribution.",
            )
        )

    # (2) Collapse detection. One row where the registry knows the dimension
    # has many values is the same defect wearing a different mask: the group
    # key resolved, but to a constant.
    if spec.group_by and total == 1 and null_keys == 0:
        expected = _expected_dimension_cardinality(snapshot, spec)
        if expected is not None and expected > 1:
            issues.append(
                _issue(
                    "grouping_collapsed",
                    f"Grouped query returned a single row, but the registry "
                    f"measured {expected} distinct values for this dimension; "
                    f"the grouping did not take effect.",
                )
            )

    # (3) Duplicate keys in a grouped result mean the GROUP BY did not
    # group — each key must appear once by construction.
    keys = [r.get(key_column) for r in rows]
    if len(keys) != len(set(keys)):
        dupes = len(keys) - len(set(keys))
        issues.append(
            _issue(
                "duplicate_group_keys",
                f"{dupes} duplicate grouping key(s) in a grouped result; "
                f"aggregation did not collapse them.",
            )
        )

    # (4) Counts cannot be negative.
    for r in rows:
        v = r.get(value_column)
        if isinstance(v, (int, float)) and v < 0:
            issues.append(
                _issue("negative_count", f"Negative aggregate value {v!r} in a count.")
            )
            break

    # (5) top_n must not over-return.
    if spec.top_n is not None and total > spec.top_n:
        issues.append(
            _issue(
                "top_n_exceeded",
                f"top_n={spec.top_n} but {total} rows returned.",
            )
        )

    return ShapeResult(
        verdict="REFUSE" if issues else "OK", issues=tuple(issues)
    )


def _expected_dimension_cardinality(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> Optional[int]:
    """How many distinct values the grouping dimension should have.

    Uses the population count of the label that OWNS the dimension, which
    is an upper bound rather than an exact figure — enough to recognise
    "this should not have been one row", which is all guard (2) claims.
    """
    if not spec.group_by:
        return None
    gb = spec.group_by[0]
    label = gb.via[-1].target if gb.via else spec.population.entity
    info = snapshot.entity(label)
    if info is None:
        return None
    # A dimension owned by a small label (PoliceStation: 19, District: 9)
    # gives a meaningful expectation; a huge one does not constrain much.
    return info.total_n if info.total_n <= 1000 else None


# ══════════════════════════════════════════════════════════════════════
# Scalar and ratio invariants
# ══════════════════════════════════════════════════════════════════════
def validate_scalar(value: Any, *, measure: str) -> ShapeResult:
    issues: list[ShapeIssue] = []
    if value is None:
        issues.append(_issue("null_result", f"{measure} returned NULL."))
    elif isinstance(value, (int, float)):
        if measure in ("count", "count_distinct") and value < 0:
            issues.append(_issue("negative_count", f"Negative count {value!r}."))
    return ShapeResult(verdict="REFUSE" if issues else "OK", issues=tuple(issues))


def validate_ratio(
    numerator: Optional[float],
    denominator: Optional[float],
    *,
    as_percentage: bool = True,
) -> ShapeResult:
    """The ratio invariants, checked before any division is attempted.

    Order matters: a zero denominator is refused BEFORE dividing, so the
    failure is a stated refusal rather than a ZeroDivisionError or an
    inf/NaN leaking into a rendered answer.
    """
    issues: list[ShapeIssue] = []

    if numerator is None or denominator is None:
        return ShapeResult(
            verdict="REFUSE",
            issues=(_issue("ratio_missing_side", "Ratio numerator or denominator is NULL."),),
        )
    if denominator == 0:
        return ShapeResult(
            verdict="REFUSE",
            issues=(
                _issue(
                    "zero_denominator",
                    "Ratio denominator is zero; the percentage is undefined.",
                ),
            ),
        )
    if numerator < 0 or denominator < 0:
        issues.append(_issue("negative_count", "Ratio side is negative."))
    if numerator > denominator:
        # Almost always a grain mismatch: links counted in the numerator
        # against entities in the denominator.
        issues.append(
            _issue(
                "numerator_exceeds_denominator",
                f"Numerator {numerator} exceeds denominator {denominator}; the "
                f"two sides are not counting the same population (usually a "
                f"grain mismatch).",
            )
        )
    if as_percentage:
        pct = 100.0 * numerator / denominator
        if not (0.0 <= pct <= 100.0):
            issues.append(
                _issue("percentage_out_of_range", f"Percentage {pct:.2f} is outside 0-100.")
            )
    return ShapeResult(verdict="REFUSE" if issues else "OK", issues=tuple(issues))


def validate_distinct_vs_raw(distinct_n: int, raw_n: int) -> ShapeResult:
    """A distinct count can never exceed the raw count it derives from."""
    if distinct_n > raw_n:
        return ShapeResult(
            verdict="REFUSE",
            issues=(
                _issue(
                    "distinct_exceeds_raw",
                    f"Distinct count {distinct_n} exceeds raw count {raw_n}.",
                ),
            ),
        )
    return ShapeResult(verdict="OK")


def validate_subset(subset_n: int, parent_n: int, *, what: str = "filtered") -> ShapeResult:
    """Adding a filter cannot grow a population.

    The deterministic counterpart of the metamorphic property in the test
    suite: asserted here on real results, not only on generated ones.
    """
    if subset_n > parent_n:
        return ShapeResult(
            verdict="REFUSE",
            issues=(
                _issue(
                    "subset_exceeds_parent",
                    f"{what} population {subset_n} exceeds its parent {parent_n}; "
                    f"a filter cannot increase a count.",
                ),
            ),
        )
    return ShapeResult(verdict="OK")


def validate_partition(
    bucket_total: int, population_n: int, *, is_partition: bool
) -> ShapeResult:
    """Bucket totals equal the population ONLY for a declared true partition.

    Gated deliberately. In this corpus `crime_category` can hold several
    acts in one value and `investigation_status` is present on 21 of 73
    cases; firing this invariant on either would refuse a correct result.
    The caller must assert `is_partition` from registry facts, and the
    receipt records when the check was skipped so the omission is visible.
    """
    if not is_partition:
        return ShapeResult(verdict="OK")
    if bucket_total != population_n:
        return ShapeResult(
            verdict="REFUSE",
            issues=(
                _issue(
                    "partition_mismatch",
                    f"Bucket totals sum to {bucket_total} but the population is "
                    f"{population_n}; a declared partition must account for "
                    f"every record.",
                ),
            ),
        )
    return ShapeResult(verdict="OK")


def validate_row_count(
    rows_n: int, *, max_expected: Optional[int], context: str = ""
) -> ShapeResult:
    """Reject a row count the registry says is impossible.

    An unintended join is the usual cause, and it inflates rather than
    errors: Case joined to Address yields 2,100 rows from 73 cases.
    """
    if max_expected is not None and rows_n > max_expected:
        return ShapeResult(
            verdict="REFUSE",
            issues=(
                _issue(
                    "impossible_row_count",
                    f"{rows_n} rows returned but at most {max_expected} are "
                    f"possible{(' for ' + context) if context else ''}; the query "
                    f"multiplied rows through an unintended traversal.",
                ),
            ),
        )
    return ShapeResult(verdict="OK")


def combine(*results: ShapeResult) -> ShapeResult:
    """Merge guard results; any refusal refuses the whole."""
    issues: tuple[ShapeIssue, ...] = ()
    verdict: ShapeVerdict = "OK"
    for r in results:
        issues += r.issues
        if r.verdict == "REFUSE":
            verdict = "REFUSE"
    return ShapeResult(verdict=verdict, issues=issues)
