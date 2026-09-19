# -*- coding: utf-8 -*-
"""
AggregateAnswer -> XAggToolResult: the boundary, and only the boundary.

WHAT THIS IS FOR. `xagg_tool` has one contract with the harness — an
`XAggToolResult` carrying evidence chunks, a deterministic
`raw_summary_text` the Verifier falls back to, and the case ids the answer
touched. The new aggregate engine returns an `AggregateAnswer`: a value, a
spec, a verification decision, a reconciliation and a receipt. Neither
shape is wrong; they were designed for different callers.

This module translates one into the other. It is deliberately the ONLY
place that knows both shapes.

WHAT IT MUST NOT DO, AND WHY.

  - It does not modify `AggregateAnswer`, `AggregateSpec` or anything else
    inside the aggregate package. The engine was specified and verified
    against its own contract; bending that contract to fit a caller would
    invalidate the verification and make the next caller's needs a reason
    to change it again.

  - It does not compute, re-derive, round, or reinterpret a value. The
    number it renders is the number reconciliation settled on, and a
    refusal stays a refusal.

  - It does not decide policy. Whether verification ran, whether routes
    agreed, whether an answer may be served at all — those are decided
    upstream and are only READ here.

THE HONEST-RENDERING RULE. The legacy engine answers a fixed family of
questions and its text says what it computed. The new engine can refuse,
can serve a value that no second route corroborated, and can report a
conflict. Those distinctions exist because they change what a reader
should believe, so they survive into the text rather than being flattened
into a bare number. A refusal renders as a refusal WITH ITS REASON; an
uncorroborated figure says so.

`aggregate_kind` is left None on purpose. It is a closed `Literal` naming
the legacy engine's canned families ("gender_breakdown",
"station_total_count", ...), and the new engine has no canned families —
it composes a spec per question. Inventing a member, or mapping a composed
spec onto the nearest canned name, would be a false claim about which code
produced the answer. The field is optional and the harness treats None as
"no canned presentation applies", which is exactly true here.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: How many grouped rows to render before summarising the remainder. The
#: text is evidence for a Verifier and a fallback for a reader, not a
#: report; an unbounded dump would bury the figure it exists to state.
_MAX_GROUP_ROWS = 25


def _render_value(answer: Any) -> str:
    """The deterministic text for an answered question."""
    lines: list[str] = []
    interpretation = getattr(answer, "interpretation", None)
    grain = getattr(answer, "grain", None)
    value = getattr(answer, "value", None)

    if isinstance(value, list):
        lines.append(f"Computed {interpretation or 'aggregate'}, by group:")
        for row in value[:_MAX_GROUP_ROWS]:
            if isinstance(row, dict):
                key = row.get("key")
                count = row.get("count")
                shown = "(no value recorded)" if key is None else key
                lines.append(f"  {shown}: {count}")
            else:
                lines.append(f"  {row}")
        if len(value) > _MAX_GROUP_ROWS:
            lines.append(f"  ... and {len(value) - _MAX_GROUP_ROWS} more group(s)")
        lines.append(f"Total groups: {len(value)}")
    else:
        label = interpretation or "aggregate"
        lines.append(f"{label}: {value}")

    if grain:
        lines.append(f"Counted unit: {grain}.")
    return "\n".join(lines)


def _render_assurance(answer: Any) -> list[str]:
    """What a reader needs in order to know how far to trust the figure.

    Rendered as text rather than dropped, because "two routes computed this
    and agreed" and "one route computed this and nothing checked it" are
    different claims, and a bare number cannot tell them apart.
    """
    out: list[str] = []
    note = getattr(answer, "verification_note", None)
    if note:
        out.append(note)
    for warning in getattr(answer, "warnings", ()) or ():
        out.append(f"Note: {warning}")
    return out


def render_answer_text(answer: Any) -> str:
    """The deterministic rendering handed to the Verifier and to the reader.

    Mirrors what `_render_aggregate_text` does for the legacy engine: one
    text, built without an LLM, that states what was computed and under
    what qualification.
    """
    status = getattr(answer, "status", None)

    if status == "REFUSED":
        reason = getattr(answer, "refusal_reason", None) or "no reason recorded"
        code = getattr(answer, "refusal_code", None)
        head = (
            f"This question was not answered ({code})."
            if code else "This question was not answered."
        )
        return f"{head}\n{reason}"

    if status == "CONFLICT":
        reason = (
            getattr(answer, "refusal_reason", None)
            or "Independent computations disagreed and no value can be served."
        )
        return f"No figure can be served for this question.\n{reason}"

    parts = [_render_value(answer)]
    parts.extend(_render_assurance(answer))
    return "\n".join(p for p in parts if p)


def case_ids_touched(answer: Any) -> list[str]:
    """Case ids the computation actually read, when the receipt records them.

    Returns an empty list rather than guessing. A cross-case aggregate over
    an entity population may legitimately touch no enumerated case id, and
    inventing one would misattribute the evidence.
    """
    structured = getattr(answer, "structured", None)
    provenance = getattr(structured, "provenance", None) or {}
    if not isinstance(provenance, dict):
        return []
    ids = provenance.get("case_ids") or provenance.get("case_ids_touched") or []
    if not isinstance(ids, (list, tuple)):
        return []
    return [str(i) for i in ids if i is not None]


def to_tool_result(answer: Any, *, result_cls: Any, status_ok: Any,
                   chunk_cls: Any, metadata_cls: Any) -> Any:
    """Build an `XAggToolResult` from an `AggregateAnswer`.

    The classes are injected rather than imported so this module stays
    free of a circular dependency on the tool that calls it, and so a test
    can exercise the translation without constructing the harness.

    A refusal is returned as a SUCCESSFUL tool call carrying refusal text,
    not as a tool FAILURE. The distinction matters: the tool did its job,
    and the answer is "this cannot be answered, here is why". Reporting it
    as `FAILED` would tell the harness the aggregate primitive broke, which
    would invite a retry or a fallback to a route that cannot answer the
    question either.
    """
    text = render_answer_text(answer)
    chunk = chunk_cls(
        id="xagg-aggregate-v1",
        text=text,
        metadata=metadata_cls(
            source_tool="XAGG",
            source_file="cross-case aggregate (aggregate_v1)",
        ),
    )
    return result_cls(
        status=status_ok,
        chunks=[chunk],
        case_ids_touched=case_ids_touched(answer),
        # Deliberately None — see this module's docstring.
        aggregate_kind=None,
        raw_summary_text=text,
    )
