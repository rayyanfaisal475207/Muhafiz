"""
Chained aggregate step — src/pipeline/harness/tools/chained_aggregate.py
(Gold-QA Wave 2, Module 79).

THE DEFECT. *"How is the most frequently cited offence pattern distributed
across districts?"* — measured live on 2026-09-14 — ran ONE aggregate, the
per-district case count, and then said, honestly, that the data "does not
specify the most frequently cited offence pattern". The question needs two
steps: (1) find the section cited by the most FIRs, (2) break THAT section's
FIRs down by district. Both aggregates existed (`fir_section_case_count`
reports `top=PPC §34=40`; `district_breakdown` is G6's own first
sub-query), and every plan structure in the harness was single-aggregate by
construction: `rag.py::_KbDataHalfPlan` names one `sub_query`, and a
`meta_analysis.py::_DecompositionPlan` fans out N INDEPENDENT sub-queries
whose outputs never feed one another.

WHAT THIS IS. A plan step that is TWO aggregates run in SEQUENCE. `first`
runs; one named field of its structured result is read (`take`, a dotted
path such as `"sections.0.section_code"`); that value fills one
`AggregateFilters` field (`filter_field`, Module 144's mechanism); the
resulting case-id allow-list narrows `then` through the
`jurisdiction_case_ids` parameter EVERY aggregate already accepts (Milestone
E1). Nothing in `xagg.py` had to change: the second aggregate does not know
it is being chained, and no new aggregate kind enters the dispatch chain.

`take` is OPTIONAL. A step with no `take` is a plain two-aggregate plan —
`then` runs unnarrowed — which is exactly KB9's shape (a charging-side
figure AND a property one, with no data dependency between them; Modules
39/76/77 each recorded that gap as this module's).

WHY THE AGGREGATES ARE CALLED DIRECTLY AND NOT THROUGH THE SUPERVISOR.
Meta-Analysis dispatches each sub-query as a full Supervisor pass — route,
XAGG agent, an LLM paraphrase, a verifier verdict — under one shared
wall-clock deadline (`META_ANALYSIS_SUBQUERY_TIMEOUT`, Module 53's
staircase). Two of those in series would cost two model round trips for
a rendering the synthesis step re-paraphrases anyway. The chain instead
calls `run_aggregate()` twice (measured 0.1-0.4 s each against the live
graph), renders both halves with the same deterministic renderer the XAGG
tool uses, and hands that text on as the sub-answer — the same
correct-by-construction text Module 53's salvage path already serves when
a paraphrase is lost to the deadline, for the same reason. The role gate and
audit record inside `run_aggregate()` run for both calls, unchanged.

NOT GENERAL-PURPOSE. This is not a planner: a step is declared, not
inferred, and every dispatch string in it is checked against
`resolve_aggregate_kind()` by test, the same convention every
`_DecompositionPlan` and `_KbDataHalfPlan` string already follows.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from src.pipeline.aggregate_filters import DISTRICT_DISPLAY, AggregateFilters

logger = logging.getLogger(__name__)

__all__ = ["ThenStep", "ChainedAggregateOutcome", "run_aggregate_chain", "pluck"]


@dataclass(frozen=True)
class ThenStep:
    """The second aggregate of a chained step, and how it is fed.

    `sub_query` / `expected_kind` mirror the fields every single-step plan
    already carries. `take` is a dotted path into the FIRST aggregate's
    result dict (`"sections.0.section_code"`); its value fills the
    `AggregateFilters` field named by `filter_field` and narrows this
    aggregate to the cases that filter selects. With `take=None` this
    aggregate runs unnarrowed. `lead` is an optional line rendered between
    the two halves, formatted with the taken value (`{value}`), the record
    it was read from (that record's own keys) and the first result's
    top-level keys — so a plan can say what the chain did in words.
    """

    sub_query: str
    expected_kind: str
    take: Optional[str] = None
    filter_field: Optional[str] = None
    lead: Optional[str] = None

    def __post_init__(self) -> None:
        if (self.take is None) != (self.filter_field is None):
            raise ValueError("ThenStep: `take` and `filter_field` go together.")
        if self.filter_field is not None and self.filter_field not in _FILTER_FIELDS:
            raise ValueError(
                f"ThenStep: filter_field must be one of {sorted(_FILTER_FIELDS)}, "
                f"got {self.filter_field!r}."
            )


# The `AggregateFilters` fields a taken value can fill. `district` takes the
# STORED Urdu name (what a `district_breakdown` row carries) and expands it
# into the `districts` tuple `resolve_filter_case_ids()` reads.
_FILTER_FIELDS = frozenset({"section", "district", "date_from", "date_to", "age_min", "age_max"})


@dataclass(frozen=True)
class ChainedAggregateOutcome:
    """What a chain produced. `status` is one of `ok`, `denied`, `failed`,
    `mismatch` (an aggregate resolved to a family the step did not name —
    dropped rather than cited, exactly `rag.py`'s defence-in-depth rule)."""

    status: str
    text: Optional[str] = None
    first_kind: Optional[str] = None
    then_kind: Optional[str] = None
    value: Optional[Any] = None
    narrowed_to: Optional[int] = None  # size of the allow-list, None when unnarrowed
    seconds: float = 0.0
    error: Optional[str] = None


def pluck(result: dict, path: str) -> tuple[Any, Optional[dict]]:
    """Read `path` ("sections.0.section_code") out of a result dict.

    Returns `(value, parent)` where `parent` is the dict the leaf was read
    from (so a renderer can quote its siblings — the section's label and
    count next to its code). `(None, None)` when any segment is missing:
    an empty table, a shorter list, an absent key. Never raises.
    """
    node: Any = result
    parent: Optional[dict] = None
    for segment in path.split("."):
        if isinstance(node, dict):
            if segment not in node:
                return None, None
            parent, node = node, node[segment]
        elif isinstance(node, (list, tuple)):
            try:
                index = int(segment)
            except ValueError:
                return None, None
            if not (-len(node) <= index < len(node)):
                return None, None
            parent, node = None, node[index]
        else:
            return None, None
    if not isinstance(parent, dict):
        parent = None
    return node, parent


def filters_for(field: str, value: Any) -> AggregateFilters:
    """One `AggregateFilters` with exactly `field` set from a taken value."""
    if field == "district":
        stored = str(value)
        return AggregateFilters(
            district=DISTRICT_DISPLAY.get(stored, stored), districts=(stored,),
        )
    if field in ("age_min", "age_max"):
        return dataclasses.replace(AggregateFilters(), **{field: int(value)})
    return dataclasses.replace(AggregateFilters(), **{field: value})


def _lead_line(then: ThenStep, value: Any, parent: Optional[dict], first: dict) -> Optional[str]:
    if not then.lead:
        return None
    context: dict[str, Any] = {}
    context.update({k: v for k, v in first.items() if not isinstance(v, (list, dict))})
    if parent:
        context.update(parent)
    context["value"] = value
    # A district value is the STORED Urdu name; give the lead its English
    # display form too, so a plan can write "{district} ({value_display})".
    context["value_display"] = DISTRICT_DISPLAY.get(str(value), str(value))
    try:
        return then.lead.format(**context)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("Chained aggregate: lead template %r could not be rendered: %s", then.lead, exc)
        return None


async def run_aggregate_chain(
    first_query: str,
    first_kind: str,
    then: ThenStep,
    *,
    gateway,
    user_id: Optional[str],
    user_role: str,
    label: str = "",
) -> ChainedAggregateOutcome:
    """Run `first_query`, read `then.take` from its result, run
    `then.sub_query` narrowed to what that value selects, and render both.

    Never raises. Every failure — permission, upstream, a family mismatch,
    a `take` path that finds nothing — comes back as a non-`ok` outcome so
    a plan degrades exactly as its single-step siblings do.
    """
    # Imported here, not at module scope: `harness/tools/xagg.py` imports
    # `xagg.py` and the renderer, and `rag.py` imports the XAGG tool lazily
    # for the same reason (see `_run_kb_data_half()`).
    from src.pipeline.harness.tools.xagg import _render_aggregate_text
    from src.pipeline.xagg import resolve_filter_case_ids, run_aggregate

    started = time.perf_counter()

    def _done(status: str, **fields) -> ChainedAggregateOutcome:
        return ChainedAggregateOutcome(
            status=status, seconds=round(time.perf_counter() - started, 3), **fields,
        )

    try:
        first = await run_aggregate(first_query, None, gateway, user_id=user_id, user_role=user_role)
    except PermissionError as exc:
        return _done("denied", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — degradation is the contract
        logger.warning("Chained aggregate %r: first step failed: %s", label, exc)
        return _done("failed", error=str(exc))
    if first.get("kind") != first_kind:
        logger.warning(
            "Chained aggregate %r: first step expected %r but got %r — dropping the chain.",
            label, first_kind, first.get("kind"),
        )
        return _done("mismatch", first_kind=first.get("kind"))

    value: Any = None
    parent: Optional[dict] = None
    allow: Optional[set[str]] = None
    if then.take is not None:
        value, parent = pluck(first, then.take)
        if value is None:
            # An empty first result (no section on any FIR in scope) is a
            # real finding, not an error: serve the first half alone and
            # say the second could not be narrowed.
            text = _render_aggregate_text(first)
            text += (
                f"\n\nThe second step ({then.sub_query}) was not run: the first "
                f"result has nothing at {then.take!r} to narrow it by."
            )
            logger.info("Chained aggregate %r: nothing at %r in the first result.", label, then.take)
            return _done("ok", text=text, first_kind=first.get("kind"))
        try:
            allow, _meta = await resolve_filter_case_ids(
                filters_for(then.filter_field, value), include_age=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chained aggregate %r: filter resolution failed: %s", label, exc)
            return _done("failed", first_kind=first.get("kind"), value=value, error=str(exc))

    try:
        second = await run_aggregate(
            then.sub_query, None, gateway, user_id=user_id, user_role=user_role,
            jurisdiction_case_ids=sorted(allow) if allow is not None else None,
        )
    except PermissionError as exc:
        return _done("denied", first_kind=first.get("kind"), value=value, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chained aggregate %r: second step failed: %s", label, exc)
        return _done("failed", first_kind=first.get("kind"), value=value, error=str(exc))
    if second.get("kind") != then.expected_kind:
        logger.warning(
            "Chained aggregate %r: second step expected %r but got %r — dropping the chain.",
            label, then.expected_kind, second.get("kind"),
        )
        return _done("mismatch", first_kind=first.get("kind"), then_kind=second.get("kind"), value=value)

    parts = [_render_aggregate_text(first)]
    lead = _lead_line(then, value, parent, first)
    if then.take is not None:
        described = filters_for(then.filter_field, value).describe()
        parts.append(
            (lead + "\n" if lead else "")
            + f"Narrowed to the {len(allow or ())} case(s) {described} "
            f"(the value read from the first result), the second aggregate gives:"
        )
    elif lead:
        parts.append(lead)
    parts.append(_render_aggregate_text(second))
    text = "\n\n".join(parts)

    # Observability (Module 55): XAGG's SSE says only `route='XAGG'`, so the
    # log line is the only evidence of what a chain computed.
    logger.info(
        "XAGG chained %r: %s -> take %s=%r -> %s narrowed to %s case(s); %.2fs",
        label, first.get("kind"), then.take, value, second.get("kind"),
        "all" if allow is None else len(allow), time.perf_counter() - started,
    )
    return _done(
        "ok", text=text, first_kind=first.get("kind"), then_kind=second.get("kind"),
        value=value, narrowed_to=None if allow is None else len(allow),
    )
