# -*- coding: utf-8 -*-
"""
Phase 6 — the coverage model: how much of the data the answer actually saw.

WHY THIS IS A FIRST-CLASS STAGE AND NOT A FOOTNOTE. A count over a sparse
field is arithmetically correct and practically a lie. Measured here:

    accused persons (distinct)            92
    accused persons carrying an age       17

An "average accused age" over the 17 is a true statement about 17 people
and a false impression about 92. `xagg._offender_age_profile()` already
understood this — it reports `with_age_count` alongside the mean, and
returns an honest refusal when NO accused carries an age. This module
generalises that instinct so every aggregate carries the same disclosure,
instead of it depending on which of 43 functions happened to be written
carefully.

THE DISTINCTION THAT MATTERS MOST: a record whose field is ABSENT is not a
record that FAILED the filter. Conflating the two is how "0 of 32 weapons
are unlicensed" became a finding rather than a bug (the value is Urdu
`بغیر لائسنس`; an English CONTAINS matched nothing). So absent records are
excluded from the numerator, counted separately, and reported — the fixed
`EXCLUDE_AND_REPORT` null policy in spec.py.

THRESHOLDS ARE POLICY, AND THEY ARE WRITTEN DOWN. Each constant below
states what it is protecting against and which measured value informed it.
They are the one part of this package that is a judgement call rather than
a derivation, and the brief requires them to be documented rather than
silently chosen. They belong in review.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Literal, Optional

from src.pipeline.aggregate import registry as reg

logger = logging.getLogger(__name__)


# ── Thresholds (policy — see module docstring) ────────────────────────

#: Below this share of missing data, the answer stands unqualified.
#: Chosen so that near-complete fields (police_station 73/73,
#: crime_category 73/73) do not attract noise caveats.
COVERAGE_CLEAN_MAX_ABSENT = 0.05

#: Between CLEAN and this, the answer is served WITH a stated caveat naming
#: the effective denominator. incident_date (64/73 = 12.3% absent) lands
#: here: usable, but the answer must say "of the 64 cases with a recorded
#: incident date".
COVERAGE_CAVEAT_MAX_ABSENT = 0.50

#: Above CAVEAT_MAX, a numeric answer is refused. Person.age (17 of 92
#: accused = 81.5% absent) lands here: an average over 18.5% of the
#: population is not an average of the population.
#: Anything above this is REFUSE.

#: A denominator below this makes a percentage arithmetic rather than
#: evidence. At 73 cases, one case is 1.4%; a denominator of 4 turns a
#: single record into 25%.
MIN_MEANINGFUL_DENOMINATOR = 5

CoverageVerdict = Literal["COMPLETE", "CAVEAT", "INSUFFICIENT"]


@dataclasses.dataclass(frozen=True)
class CoverageReport:
    """What the computation saw, and what it could not see.

    Every field here is meant to be reproducible from the receipt: a
    reviewer asking "why 17 and not 92?" should find the answer in
    `field_absent_n` without re-running anything.
    """

    expected_population_n: int
    observed_population_n: int

    field_present_n: Optional[int] = None
    field_absent_n: Optional[int] = None
    field_name: Optional[str] = None

    excluded_by_filter_n: int = 0
    excluded_null_n: int = 0
    excluded_superseded_n: int = 0
    excluded_merged_n: int = 0
    canonicalization_collapsed_n: int = 0

    source: Optional[str] = None
    source_exhaustive: bool = False

    effective_denominator_n: Optional[int] = None
    effective_denominator_definition: Optional[str] = None

    @property
    def absent_share(self) -> float:
        """Share of the expected population with no value for the field."""
        if self.field_absent_n is None or not self.expected_population_n:
            return 0.0
        return self.field_absent_n / self.expected_population_n

    @property
    def verdict(self) -> CoverageVerdict:
        """Whether a number may be served, and with what qualification."""
        denom = self.effective_denominator_n
        if denom is not None and denom < MIN_MEANINGFUL_DENOMINATOR:
            return "INSUFFICIENT"
        share = self.absent_share
        if share > COVERAGE_CAVEAT_MAX_ABSENT:
            return "INSUFFICIENT"
        if share > COVERAGE_CLEAN_MAX_ABSENT:
            return "CAVEAT"
        return "COMPLETE"

    def caveat_text(self) -> Optional[str]:
        """The disclosure that must accompany the number, if any.

        Phrased as the existing aggregates phrase it ("N of M carry ..."),
        so a caveat from the new engine reads like one from the old.
        """
        v = self.verdict
        if v == "COMPLETE":
            return None
        if v == "INSUFFICIENT":
            denom = self.effective_denominator_n
            if denom is not None and denom < MIN_MEANINGFUL_DENOMINATOR:
                return (
                    f"Only {denom} record(s) qualify — too few to report a "
                    f"meaningful figure."
                )
            return (
                f"{self.field_absent_n} of {self.expected_population_n} record(s) "
                f"carry no {self.field_name or 'value'} for this field "
                f"({self.absent_share:.0%}); a figure computed over the remainder "
                f"would not describe the population."
            )
        return (
            f"Computed over the {self.field_present_n} of "
            f"{self.expected_population_n} record(s) that carry a "
            f"{self.field_name or 'value'}"
            + (
                f" ({self.effective_denominator_definition})"
                if self.effective_denominator_definition
                else ""
            )
            + "."
        )


def build_coverage(
    snapshot: reg.RegistrySnapshot,
    *,
    entity_label: str,
    observed_n: int,
    field_name: Optional[str] = None,
    field_entity: Optional[str] = None,
    excluded_by_filter_n: int = 0,
    excluded_superseded_n: int = 0,
    excluded_merged_n: int = 0,
    canonicalization_collapsed_n: int = 0,
    effective_denominator_n: Optional[int] = None,
    effective_denominator_definition: Optional[str] = None,
    source: str = "graph",
    source_exhaustive: bool = True,
) -> CoverageReport:
    """Assemble a coverage report from registry facts plus execution counts.

    `expected_population_n` comes from the registry's ACTIVE count (total
    minus tombstones), not the raw node count — otherwise the 222 merged
    Person donors would inflate every denominator by exactly the amount the
    compiler just excluded, and coverage would claim to have missed records
    that were correctly never in scope.
    """
    info = snapshot.entity(entity_label)
    expected = info.active_n if info is not None else observed_n

    present_n: Optional[int] = None
    absent_n: Optional[int] = None
    if field_name:
        owner = field_entity or entity_label
        lf = snapshot.logical_field(field_name) or snapshot.logical_field(
            f"{owner}.{field_name}"
        )
        if lf is not None:
            present_n = lf.present_n
            absent_n = max(0, lf.total_n - lf.present_n)

    return CoverageReport(
        expected_population_n=expected,
        observed_population_n=observed_n,
        field_present_n=present_n,
        field_absent_n=absent_n,
        field_name=field_name,
        excluded_by_filter_n=excluded_by_filter_n,
        excluded_null_n=absent_n or 0,
        excluded_superseded_n=excluded_superseded_n,
        excluded_merged_n=excluded_merged_n,
        canonicalization_collapsed_n=canonicalization_collapsed_n,
        source=source,
        source_exhaustive=source_exhaustive,
        effective_denominator_n=(
            effective_denominator_n if effective_denominator_n is not None else observed_n
        ),
        effective_denominator_definition=effective_denominator_definition,
    )
