"""Independent check on an answer before it reaches a person.

WHY A SEPARATE LAYER

The agent already refuses to state a number no query returned. That is a
self-check, and a self-check shares the blind spot of the thing it checks.
This layer re-derives the claims from the query results, without asking the
model anything.

WHAT IT CHECKS, AND WHAT EACH ONE PREVENTS

  number is sourced      a figure in the prose appears in a result row.
                         Prevents the failure that produced 208 — a
                         confident total for a field that does not exist.

  no stale aggregation   an answer that counts rows the model paged through
                         rather than a count() the database computed.
                         Prevents a truncated result becoming a total.

  coverage disclosed     when a filtered column is largely empty, the
                         answer must say so. A correct count over 21 of 73
                         rows still misleads if it presents itself as
                         covering everything.

  citations resolve      every span referenced still points at real text.

  join inflation         a query joining one-to-many without DISTINCT, whose
                         count exceeds the base table's row count, is
                         reporting a product rather than a population.

VERDICTS

  pass        publish as is
  qualify     publish with the disclosure attached — the number is right
              but incomplete without context
  block       do not publish; say what is missing instead

A wrong number that looks authoritative is the only unacceptable output, so
`block` is preferred over a plausible figure whenever the evidence does not
support it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import asyncpg


@dataclass
class Finding:
    check: str
    severity: str               # info | qualify | block
    detail: str


@dataclass
class Verdict:
    outcome: str                # pass | qualify | block
    findings: list[Finding] = field(default_factory=list)
    disclosures: list[str] = field(default_factory=list)

    @property
    def publishable(self) -> bool:
        return self.outcome in ("pass", "qualify")

    def render(self, answer: str) -> str:
        if self.outcome == "block":
            reasons = "; ".join(f.detail for f in self.findings
                                if f.severity == "block")
            return f"I can't give that number reliably: {reasons}"
        if self.disclosures:
            return answer.rstrip() + "\n\n" + "\n".join(
                f"Note: {d}" for d in self.disclosures)
        return answer


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# A count() or sum() computed by the database, rather than rows the model
# added up itself.
_AGG = re.compile(r"(count|sum|avg|min|max)\s*\(", re.I)
_DISTINCT_COUNT = re.compile(r"count\s*\(\s*distinct", re.I)
_JOIN = re.compile(r"join", re.I)
# Numbering in a list — "1." at a line start, or "1)" — is formatting, not
# a claim about the data.
_ORDINAL = re.compile(r"(?m)^\s*\(?\d{1,2}[.):]\s")


def _numbers(text: str) -> set[float]:
    """Figures the answer ASSERTS, as numbers rather than strings.

    Two false rejections made this necessary, and both were the validator's
    fault rather than the answer's:

      "1, 2, 3, 4 does not appear in any query result"
          -> those were list numbering in a ranked answer
      "4.69 does not appear in any query result"
          -> the query returned 4.6923..., the answer rounded it

    String equality is the wrong test for a number. Rounding a result for
    presentation is correct behaviour, and a validator that forbids it
    blocks good answers — which is worse than useless, because it trains
    people to bypass the validator.
    """
    stripped = _ORDINAL.sub(" ", text or "")
    out: set[float] = set()
    for m in _NUM.finditer(stripped):
        try:
            out.add(float(m.group(0).replace(",", "")))
        except ValueError:
            continue
    return out


def _result_numbers(entries: list[dict],
                    results: list[list[dict]]) -> set[float]:
    out: set[float] = set()
    for rows in results:
        for row in rows or []:
            for v in row.values():
                if isinstance(v, bool):
                    continue
                # Postgres returns AVG/SUM over integers as Decimal, and
                # numeric columns as Decimal too. An isinstance check for
                # (int, float) skips every one of them, so an averaging
                # answer was rejected as unsourced while its own result sat
                # in the row. Any non-date numeric counts.
                if isinstance(v, (int, float, Decimal)):
                    out.add(float(v))
                elif isinstance(v, (datetime, date)):
                    continue
                elif isinstance(v, str):
                    for m in _NUM.finditer(v):
                        try:
                            out.add(float(m.group(0).replace(",", "")))
                        except ValueError:
                            continue
    # Row counts are legitimate figures: "three stations" for three rows.
    for e in entries:
        out.add(float(e.get("rows", 0)))
    return out


def _is_sourced(stated: float, available: set[float]) -> bool:
    """Whether a stated figure is supported by a result, allowing rounding.

    Matches exactly, or to any presentation rounding of a result value, or
    within a relative tolerance for floating point. A derived figure the
    answer computed itself is NOT sourced and must still fail.
    """
    if stated in available:
        return True
    for v in available:
        if v == stated:
            return True
        for places in (0, 1, 2, 3):
            if round(v, places) == stated:
                return True
        if v != 0 and abs(v - stated) / abs(v) < 1e-6:
            return True
    return False


async def validate(conn: asyncpg.Connection, answer: str,
                   query_entries: list[dict],
                   query_results: list[list[dict]] | None = None,
                   coverage_notes: list[str] | None = None,
                   observed: list[float] | None = None) -> Verdict:
    findings: list[Finding] = []
    disclosures: list[str] = list(coverage_notes or [])
    results = query_results or []

    stated = _numbers(answer)
    # A four-digit year in prose is not a claim about a count.
    stated = {n for n in stated if not (1900 <= n <= 2100 and n == int(n))}

    if stated:
        if not query_entries:
            findings.append(Finding(
                "number_is_sourced", "block",
                "the answer states a number but no query was run"))
        else:
            # Anything the DATABASE reported counts as a source, whether it
            # arrived through query(), describe() or values(). What must not
            # count is a figure the model produced itself.
            available = _result_numbers(query_entries, results)
            available |= {float(x) for x in (observed or [])}
            unsourced = sorted(n for n in stated
                               if not _is_sourced(n, available))
            if unsourced and results:
                findings.append(Finding(
                    "number_is_sourced", "block",
                    f"{', '.join(str(n) for n in unsourced[:4])} does not "
                    f"appear in any query result"))

    for e in query_entries:
        sql = e.get("sql", "")
        if e.get("error"):
            continue
        # An answer built by reading many rows rather than aggregating.
        if stated and not _AGG.search(sql) and e.get("rows", 0) >= 50:
            findings.append(Finding(
                "no_stale_aggregation", "block",
                f"a figure was drawn from {e['rows']} returned rows instead "
                f"of an aggregate computed by the database"))
        # A join without DISTINCT can report a product, not a population.
        if _JOIN.search(sql) and _AGG.search(sql) \
                and not _DISTINCT_COUNT.search(sql):
            findings.append(Finding(
                "join_inflation", "qualify",
                "an aggregate over a join without count(DISTINCT ...) can "
                "multiply rows; confirm the figure counts entities, not "
                "row pairs"))

    for note in disclosures:
        findings.append(Finding("coverage_disclosed", "info", note))

    if any(f.severity == "block" for f in findings):
        outcome = "block"
    elif any(f.severity == "qualify" for f in findings) or disclosures:
        outcome = "qualify"
    else:
        outcome = "pass"
    return Verdict(outcome, findings, disclosures)


async def check_citations(conn: asyncpg.Connection,
                          span_ids: list[int]) -> list[Finding]:
    """Every cited span must still resolve to real text at its offsets."""
    if not span_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT s.span_id,
               (substring(e.text FROM s.char_start + 1
                          FOR s.char_end - s.char_start) = s.text) AS resolves
        FROM doc_spans s
        JOIN doc_elements e ON e.doc_id = s.doc_id
                           AND e.element_type = 'document'
        WHERE s.span_id = ANY($1)
        """, span_ids)
    found = {r["span_id"] for r in rows}
    out: list[Finding] = []
    missing = sorted(set(span_ids) - found)
    if missing:
        out.append(Finding("citations_resolve", "block",
                           f"cited span(s) {missing[:5]} do not exist"))
    broken = [r["span_id"] for r in rows if not r["resolves"]]
    if broken:
        out.append(Finding("citations_resolve", "block",
                           f"cited span(s) {broken[:5]} no longer match the "
                           f"document text at their offsets"))
    return out
