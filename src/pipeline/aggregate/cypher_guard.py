# -*- coding: utf-8 -*-
"""
Phase 4C — deterministic guards for LLM-generated AGE Cypher.

WHY THIS MODULE HAS TO EXIST, AND WHY IT IS STRICT.

`src/graph/age_client.execute_cypher()` states its own contract plainly:
the `cypher_query` argument is "a literal Cypher template — written in this
codebase, never built from request/user input", because AGE's `cypher()`
declares `query_string` as `cstring`, which libpq cannot bind as a wire
parameter. The text is therefore interpolated into SQL. That is safe for
hand-written templates and categorically unsafe for model output.

Route 2 needs model output to reach that function. This module is what
earns the exception: a generated query executes ONLY after passing every
check below, and anything unproven is refused rather than repaired. The
brief is explicit — "Do not silently rewrite incorrect generated Cypher" —
and silent repair would also destroy the evaluation, because a repaired
query no longer tells us what the model actually produced.

TWO CLASSES OF CHECK, KEPT APART ON PURPOSE.

  SAFETY   — could this query damage or exfiltrate? Read-only, single
             statement, no writes, no procedure calls, no escape from the
             `$cypher$` delimiter. These are absolute: failing one is a
             refusal with no appeal.

  SEMANTIC — could this query return a plausible but wrong NUMBER? Grain,
             fanout/DISTINCT, tombstones, supersession. These use the
             registry's measured facts, and they are heuristics over a
             query language, not proofs. The brief anticipates this: build
             deterministic guards for "high-value, detectable violations",
             not a theorem prover.

WHAT THE SEMANTIC GUARDS DO NOT DO. They do not attempt to decide whether
the query answers the user's question — that is reconciliation's job, using
independent routes. They flag constructions that Phase 0-3 MEASURED as
producing wrong numbers on this specific corpus:

  * `count(r)` / `count(*)` after a fanning traversal, where a distinct
    count was almost certainly meant (73 cases -> 449 rows via Person,
    -> 2,100 via Address);
  * counting Person without excluding `merged_into` (429 vs 208);
  * counting versioned relationships without excluding `superseded_by`;
  * grouping on a property the registry never measured on that label
    (the `{'gkey': None, 'n': 73}` failure).

A flagged query is REFUSED, not corrected. The model is told nothing and
gets no second attempt inside this module; retry policy belongs to the
route, and repair belongs to nobody.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Literal, Optional

from src.pipeline.aggregate import registry as reg

Severity = Literal["SAFETY", "SEMANTIC"]


@dataclasses.dataclass(frozen=True)
class GuardViolation:
    code: str
    severity: Severity
    message: str


@dataclasses.dataclass(frozen=True)
class GuardReport:
    violations: tuple[GuardViolation, ...] = ()

    @property
    def safe(self) -> bool:
        """True when nothing at all was flagged.

        Note there is no "safe but semantically doubtful" tier that still
        executes. A semantic violation is a refusal too, because the whole
        point of the evaluation is to compare CORRECT computations; letting
        a known-wrong construction run would put a number into
        reconciliation that we already know not to trust.
        """
        return not self.violations

    def codes(self) -> tuple[str, ...]:
        return tuple(v.code for v in self.violations)

    def worst_severity(self) -> Optional[Severity]:
        if any(v.severity == "SAFETY" for v in self.violations):
            return "SAFETY"
        if self.violations:
            return "SEMANTIC"
        return None

    def summary(self) -> str:
        return "; ".join(f"[{v.severity}] {v.code}: {v.message}" for v in self.violations)


# ══════════════════════════════════════════════════════════════════════
# SAFETY
# ══════════════════════════════════════════════════════════════════════

#: Clauses that write, delete or otherwise mutate. Matched as whole words so
#: a legitimate identifier containing them (`created_at`, `merged_into`) is
#: not a false positive.
_WRITE_CLAUSES = (
    "create", "merge", "delete", "detach", "set", "remove", "drop",
    "load", "foreach", "call", "using", "grant", "revoke", "alter",
    "insert", "update", "truncate", "copy",
)

#: A query must begin with one of these. Anything else is either a write or
#: a construction this route has no reason to emit.
_ALLOWED_OPENERS = ("match", "with", "unwind", "return", "optional")

#: The `$cypher$` dollar-quote `execute_cypher()` wraps the query in. A
#: generated query containing it could terminate the quote early and append
#: arbitrary SQL — the single most dangerous string for this route, and the
#: reason a plain keyword scan is not sufficient on its own.
_DOLLAR_QUOTE_RE = re.compile(r"\$cypher\$")

#: Semicolons outside string literals: multi-statement execution.
_SEMICOLON_RE = re.compile(r";")

#: String literals, so keyword scanning ignores their contents.
_STRING_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

#: A bare literal in a comparison, where a parameter belongs. Detects
#: `= 'value'` / `CONTAINS "x"` — the model inlining user text instead of
#: binding it.
_INLINE_STRING_COMPARISON_RE = re.compile(
    r"(=|<>|!=|=~|\bCONTAINS\b|\bSTARTS\s+WITH\b|\bENDS\s+WITH\b|\bIN\b)\s*"
    r"('[^']*'|\"[^\"]*\")",
    re.IGNORECASE,
)


def _strip_strings(query: str) -> str:
    """Blank out string literals so keyword scans see only structure."""
    return _STRING_LITERAL_RE.sub("''", query)


def check_safety(query: str) -> list[GuardViolation]:
    """Absolute checks. Any hit refuses execution."""
    out: list[GuardViolation] = []
    if not query or not query.strip():
        return [GuardViolation("empty_query", "SAFETY", "The model produced no query.")]

    stripped = _strip_strings(query)
    lowered = stripped.lower()

    if _DOLLAR_QUOTE_RE.search(query):
        out.append(GuardViolation(
            "dollar_quote_injection", "SAFETY",
            "Query contains the $cypher$ delimiter that execute_cypher() wraps "
            "it in; it could terminate the quote and append arbitrary SQL.",
        ))

    if _SEMICOLON_RE.search(stripped):
        out.append(GuardViolation(
            "multi_statement", "SAFETY",
            "Query contains a semicolon — multi-statement execution is refused.",
        ))

    for clause in _WRITE_CLAUSES:
        if re.search(rf"\b{clause}\b", lowered):
            out.append(GuardViolation(
                "write_operation", "SAFETY",
                f"Query contains the '{clause.upper()}' clause; this route is "
                f"strictly read-only.",
            ))
            break

    opener = lowered.strip().split(None, 1)
    if not opener or opener[0] not in _ALLOWED_OPENERS:
        out.append(GuardViolation(
            "bad_opener", "SAFETY",
            f"Query must begin with one of "
            f"{', '.join(o.upper() for o in _ALLOWED_OPENERS)}; it begins with "
            f"{(opener[0] if opener else '(nothing)')!r}.",
        ))

    if "return" not in lowered:
        out.append(GuardViolation(
            "no_return", "SAFETY",
            "Query has no RETURN clause, so it produces no readable result.",
        ))

    if _INLINE_STRING_COMPARISON_RE.search(query):
        out.append(GuardViolation(
            "inline_literal", "SAFETY",
            "Query compares against an inline string literal; caller-derived "
            "values must be bound parameters ($name), never interpolated.",
        ))

    return out


# ══════════════════════════════════════════════════════════════════════
# AGE dialect
# ══════════════════════════════════════════════════════════════════════

#: Constructions AGE rejects, each measured against this database during
#: Phase 0/1. A model trained on Neo4j emits all three routinely, and each
#: fails at execution — so catching them here turns a confusing runtime
#: error into a named refusal.
_DIALECT_CHECKS: tuple[tuple[str, str, str], ...] = (
    (r"\bEXISTS\s*\{", "age_exists_subquery",
     "AGE rejects EXISTS { ... } subqueries (PostgresSyntaxError at '{')."),
    (r"\bWHERE\s+NOT\s*\([a-zA-Z_][a-zA-Z0-9_]*\)?\s*-\[", "age_negated_pattern",
     "AGE rejects negated relationship patterns (WHERE NOT (a)-[:R]->(:L)); "
     "count the complement instead."),
    (r"\bORDER\s+BY\s+(?!count\s*\()[a-zA-Z_][a-zA-Z0-9_]*\s*(?:DESC|ASC)?\s*(?:LIMIT|$)",
     "age_order_by_alias",
     "AGE rejects ORDER BY <alias> (UndefinedColumnError); use "
     "WITH ... RETURN ... ORDER BY instead."),
)


def check_dialect(query: str) -> list[GuardViolation]:
    out: list[GuardViolation] = []
    for pattern, code, message in _DIALECT_CHECKS:
        if re.search(pattern, query, re.IGNORECASE):
            out.append(GuardViolation(code, "SAFETY", message))
    return out


# ══════════════════════════════════════════════════════════════════════
# SEMANTIC
# ══════════════════════════════════════════════════════════════════════
_MATCH_PATTERN_RE = re.compile(
    r"\(\s*(?P<a>[a-zA-Z_][a-zA-Z0-9_]*)?\s*:\s*(?P<alabel>[A-Za-z_][A-Za-z0-9_]*)\s*\)"
    r"\s*(?P<ldash><-|-)\s*\[\s*(?P<r>[a-zA-Z_][a-zA-Z0-9_]*)?\s*:\s*"
    r"(?P<rel>[A-Za-z_][A-Za-z0-9_]*)\s*\]\s*(?P<rdash>->|-)\s*"
    r"\(\s*(?P<b>[a-zA-Z_][a-zA-Z0-9_]*)?\s*:\s*(?P<blabel>[A-Za-z_][A-Za-z0-9_]*)\s*\)"
)

_COUNT_STAR_RE = re.compile(r"\bcount\s*\(\s*(\*|[a-zA-Z_][a-zA-Z0-9_]*)\s*\)", re.IGNORECASE)
_COUNT_DISTINCT_RE = re.compile(r"\bcount\s*\(\s*DISTINCT\b", re.IGNORECASE)
_LABELS_IN_QUERY_RE = re.compile(r":\s*([A-Z][A-Za-z0-9_]*)")


def check_semantics(
    query: str, snapshot: reg.RegistrySnapshot
) -> list[GuardViolation]:
    """Registry-informed checks for constructions measured to be wrong.

    Heuristic by nature and deliberately conservative: each rule fires only
    on a pattern Phase 0-3 observed producing a wrong number on this
    corpus, and each refuses rather than edits.
    """
    out: list[GuardViolation] = []
    lowered = query.lower()

    # (1) FANOUT. A traversal the registry measured as fanning, combined
    # with a non-distinct count, is the 73 -> 449 / 73 -> 2,100 defect.
    fanning_found: list[str] = []
    for m in _MATCH_PATTERN_RE.finditer(query):
        a_label, rel, b_label = m.group("alabel"), m.group("rel"), m.group("blabel")
        reversed_dir = m.group("ldash") == "<-"
        src, dst = (b_label, a_label) if reversed_dir else (a_label, b_label)
        info = snapshot.relationship(src, rel, dst)
        if info is None:
            continue
        direction = "in" if reversed_dir else "out"
        if info.fans_in_direction(direction):
            fanning_found.append(
                f"{src}-[{rel}]->{dst} (up to "
                f"{info.max_fanout_in_direction(direction)} rows per {src})"
            )

    if fanning_found and _COUNT_STAR_RE.search(query) and not _COUNT_DISTINCT_RE.search(query):
        out.append(GuardViolation(
            "fanout_without_distinct", "SEMANTIC",
            f"Query counts without DISTINCT across a fanning traversal "
            f"[{'; '.join(fanning_found)}], so it counts relationship rows "
            f"rather than entities.",
        ))

    # (2) TOMBSTONES. 222 of 430 Person nodes are merge donors; counting
    # them inflates a headcount by ~2x (429 vs 208).
    labels = set(_LABELS_IN_QUERY_RE.findall(query))
    for label in labels:
        info = snapshot.entity(label)
        if info is not None and info.has_tombstones:
            if reg.TOMBSTONE_PROPERTY not in lowered:
                out.append(GuardViolation(
                    "tombstones_not_excluded", "SEMANTIC",
                    f"Query reads {label}, of which {info.tombstoned_n} of "
                    f"{info.total_n} are merge tombstones, without excluding "
                    f"`{reg.TOMBSTONE_PROPERTY} IS NULL`.",
                ))
                break

    # (3) SUPERSESSION. Versioned relationships keep historical versions;
    # counting them double-counts the current one.
    rels_in_query = {m.group("rel") for m in _MATCH_PATTERN_RE.finditer(query)}
    for rel_name in rels_in_query:
        versioned = [
            r for r in snapshot.relationships.values()
            if r.rel_type == rel_name and r.is_versioned
        ]
        if versioned and reg.SUPERSEDED_PROPERTY not in lowered:
            total_superseded = sum(r.superseded_n for r in versioned)
            out.append(GuardViolation(
                "superseded_not_excluded", "SEMANTIC",
                f"Query traverses {rel_name}, which carries {total_superseded} "
                f"superseded edge version(s), without excluding "
                f"`{reg.SUPERSEDED_PROPERTY} IS NULL`.",
            ))
            break

    # (4) GROUPING ON AN UNMEASURED PROPERTY. The `{'gkey': None, 'n': 73}`
    # failure: a property the label does not carry collapses every row into
    # one NULL bucket and returns a plausible total.
    for m in re.finditer(
        r"\b([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)", query
    ):
        alias, prop = m.group(1), m.group(2)
        label = _label_for_alias(query, alias)
        if label is None:
            continue
        info = snapshot.entity(label)
        if info is None:
            continue
        if prop in (reg.TOMBSTONE_PROPERTY, reg.SUPERSEDED_PROPERTY):
            continue
        if info.property_presence(prop) is None:
            out.append(GuardViolation(
                "unknown_property", "SEMANTIC",
                f"Query reads {alias}.{prop}, but the registry measured no "
                f"`{prop}` property on {label}; it would read as NULL on every "
                f"node.",
            ))
            break

    return out


def _label_for_alias(query: str, alias: str) -> Optional[str]:
    """The label bound to `alias` in a MATCH pattern, if any."""
    m = re.search(
        rf"\(\s*{re.escape(alias)}\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", query
    )
    return m.group(1) if m else None


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════
def guard(query: str, snapshot: reg.RegistrySnapshot) -> GuardReport:
    """Run every check. A query executes only if this returns `safe`."""
    violations: list[GuardViolation] = []
    violations.extend(check_safety(query))
    violations.extend(check_dialect(query))
    # Semantic checks assume a structurally sane query; running them on one
    # that failed safety would produce noise on top of a refusal.
    if not violations:
        violations.extend(check_semantics(query, snapshot))
    return GuardReport(violations=tuple(violations))


def expected_columns(query: str) -> tuple[str, ...]:
    """Column names the RETURN clause declares, for `execute_cypher`.

    AGE cannot infer output shape at runtime — the SQL wrapper must declare
    `AS (col agtype, ...)` statically — so the aliases are parsed out here.
    A RETURN whose terms are not all explicitly aliased yields an empty
    tuple, and the route refuses: guessing a column name would mean
    executing a query whose shape we could not state.
    """
    m = re.search(r"\bRETURN\b(.*)$", query, re.IGNORECASE | re.DOTALL)
    if not m:
        return ()
    tail = m.group(1)
    tail = re.split(r"\bORDER\s+BY\b|\bLIMIT\b|\bSKIP\b", tail, flags=re.IGNORECASE)[0]

    cols: list[str] = []
    depth = 0
    current = ""
    for ch in tail:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            cols.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        cols.append(current)

    out: list[str] = []
    for col in cols:
        alias = re.search(r"\bAS\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*$", col.strip(), re.IGNORECASE)
        if not alias:
            return ()
        out.append(alias.group(1))
    return tuple(out)
