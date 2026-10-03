"""A readable record of what the runtime actually did, step by step.

WHY THIS EXISTS

Until now a failed question produced one sentence -- "an answer has not yet
been established" -- and everything that led to it lived in a diagnostic
blob nobody reads. Two failures that look identical on screen ("the
planner chose narrative search" and "the planner emitted an invalid plan")
need opposite fixes, and there was no way to tell them apart without
re-running the question under a debugger.

So every stage records what it received, what it decided and what came
back: the concepts that matched, the names that resolved and how, the plan
in CONCEPT LABELS rather than UUIDs, the SQL each operation compiled to,
the rows and citations it returned, the findings composition proposed and
exactly which ones were dropped and why.

WHAT IT IS NOT

Not a log level and not sampling -- a trace is per-run and complete, or it
is useless for the run that failed. Not a metrics feed either: it is meant
to be READ, by a person, while they watch a question go wrong.

UUIDs are resolved to labels wherever a label exists, because "predicate
54bfd20a-1ea0-520f-a18a-85b095b68eb6" tells a reader nothing and "accused
in" tells them everything.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import UUID

logger = logging.getLogger("evidence.trace")

# A span's text is case material. It is shown to the operator who is
# already authorized to read the case, but never in full: enough to see
# WHICH passage was retrieved, not a bulk export of the record.
SNIPPET = 220
SQL_LIMIT = 4000


def shorten(value: Any, limit: int = SNIPPET) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + " …"


class Trace:
    """Collects the steps of one run and pushes them to a listener."""

    def __init__(self, emit=None, *, labels: dict[str, str] | None = None):
        self.steps: list[dict] = []
        self.started = time.monotonic()
        self._emit = emit
        # concept_id -> human label, filled in as the catalog is read.
        self.labels: dict[str, str] = dict(labels or {})

    def name(self, value: Any) -> Any:
        """A concept id rendered as its label, when one is known."""
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return [self.name(item) for item in value]
        key = str(value)
        return self.labels.get(key, key)

    def learn(self, concepts) -> None:
        """Remember labels so later steps can read in words, not ids."""
        try:
            for key, concept in (concepts or {}).items():
                label = getattr(concept, "label", None)
                if label:
                    self.labels[str(key)] = label
        except Exception:                                # noqa: BLE001
            pass

    async def step(self, stage: str, title: str, **detail) -> None:
        """Record one step. Never raises: tracing must not break a run."""
        try:
            entry = {
                "seq": len(self.steps) + 1,
                "stage": stage,
                "title": title,
                "at_ms": int((time.monotonic() - self.started) * 1000),
                "detail": _plain(detail),
            }
            self.steps.append(entry)
            logger.info("[trace %02d] %-18s %s%s", entry["seq"], stage, title,
                        _terminal(entry["detail"]))
            if self._emit:
                await self._emit({"steps": self.steps})
        except Exception:                                # noqa: BLE001
            logger.debug("trace step failed", exc_info=True)


def _plain(value: Any) -> Any:
    """JSON-safe, with UUIDs as strings and nothing unserializable."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _terminal(detail: dict) -> str:
    """A compact one-line tail for the server log."""
    if not isinstance(detail, dict) or not detail:
        return ""
    parts = []
    for key, value in detail.items():
        if isinstance(value, (list, dict)):
            parts.append(f"{key}={len(value)}")
        elif value is not None and value != "":
            parts.append(f"{key}={shorten(value, 60)}")
    return "  " + " ".join(parts) if parts else ""


def describe_operation(op, trace: Trace) -> dict:
    """One query operation, in words a reader can check against the question."""
    out: dict[str, Any] = {"output_id": op.output_id, "operation": op.operation}
    if op.subject_type:
        out["subject_type"] = trace.name(op.subject_type)
    if op.predicate_id:
        out["predicate"] = trace.name(op.predicate_id)
    if op.relationship_path:
        out["relationship_path"] = trace.name(list(op.relationship_path))
    if op.text:
        out["text"] = shorten(op.text, 200)
    if op.entity_ids:
        out["entity_ids"] = f"{len(op.entity_ids)} resolved id(s)"
    if op.entities_from:
        out["entities_from"] = op.entities_from
    if op.depends_on:
        out["depends_on"] = list(op.depends_on)
    if op.group_by:
        out["group_by"] = trace.name(op.group_by)
    if op.occurred_after or op.occurred_before:
        out["period"] = {"from_inclusive": op.occurred_after,
                         "to_exclusive": op.occurred_before}
    if op.filters:
        out["filters"] = [
            {"predicate": trace.name(f.predicate_id), "operator": f.operator,
             **({"value": f.value} if f.value is not None else {}),
             **({"entity_id": str(f.object_entity_id)} if f.object_entity_id else {}),
             **({"entity_name": f.object_name} if getattr(f, "object_name", None) else {})}
            for f in op.filters]
    out["source_scope"] = op.source_scope
    out["limit"] = op.limit
    out["requires_complete"] = op.requires_complete
    return out


def describe_plan(plan, trace: Trace) -> dict:
    """The whole plan, readable end to end."""
    return {
        "language": plan.language,
        "standalone_question": shorten(plan.standalone_question, 300),
        "continuation": plan.continuation,
        "clarification": shorten(plan.clarification, 300) if plan.clarification else None,
        "entity_queries": list(plan.entity_queries),
        "catalog_queries": list(plan.catalog_queries),
        "discovery_terms": list(plan.discovery_terms),
        "outputs": [describe_operation(op, trace) for op in plan.outputs],
    }


def describe_sql(sql: str, params) -> dict:
    """The exact statement and the values bound to it."""
    compact = " ".join(str(sql).split())
    return {"sql": compact[:SQL_LIMIT] + (" …" if len(compact) > SQL_LIMIT else ""),
            "params": [shorten(p, 120) for p in (params or [])]}
