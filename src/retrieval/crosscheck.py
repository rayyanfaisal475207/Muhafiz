"""Check the parts of one question against each other.

WHAT THIS KNOWS THAT A PART CANNOT

Every part of a compound question is validated on its own — the guards
check its joins, the validator checks its figure came from SQL. All of
that happens inside a part, and inside a part it is complete.

But parts of one question are related, and no part can see the relation:

    "How many cases are there? How many had a weapon?"
        73 and 4       -> consistent, nothing to say
        73 and 118     -> impossible, and BOTH figures passed every check
                          that could see them

The second case is the one this exists for. 118 cases with a weapon out
of 73 cases is not a close call or a judgement — it is arithmetic, and it
means at least one of the two is wrong. Reporting them side by side with
no comment leaves the reader to notice, and the reader will not: two
figures that each carry a query and a citation look equally solid.

HOW THE RELATION IS DERIVED

From the SQL the parts actually ran, never from their wording. Two counts
over the same base table are comparable, and the one carrying strictly
more restrictions cannot return more rows than the one carrying fewer.
That is true of any schema, in any language, for questions nobody has
written yet — which is why it is a predicate comparison and not a list of
known pairs like "total vs weapon".

WHAT IT DELIBERATELY DOES NOT DO

It does not try to reconcile. Finding that two figures contradict each
other says one is wrong; it does not say which, and picking would be a
guess dressed as a correction. The contradiction is reported and the set
is withheld.

It also stays silent whenever it cannot establish the relation — a
different base table, a join, an unparseable query. A cross-check that
guesses at relationships invents contradictions, and a false contradiction
suppresses a correct answer, which is a worse failure than the silence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A count over one table with no join. Anything more complex is left
# alone: the moment a join appears, row counts stop being comparable by
# predicate inclusion, because the join itself changes what a row is.
_COUNT = re.compile(
    r"^\s*select\s+count\s*\(\s*(?:distinct\s+)?[^)]*\)\s*(?:as\s+\w+\s*)?"
    r"from\s+([a-z_][a-z0-9_]*)\s*(?:(?:as\s+)?[a-z][a-z0-9_]*\s*)?"
    r"(?:where\s+(.+?))?\s*(?:;|$)",
    re.I | re.S)

_JOIN = re.compile(r"\b(join|union|intersect|except|group\s+by)\b", re.I)
_NUM = re.compile(r"\d[\d,]*")


@dataclass
class Figure:
    """One part's count, and the query shape that produced it."""

    question: str
    value: int
    base: str
    predicates: frozenset[str]


def _predicates(where: str | None) -> frozenset[str] | None:
    """The conjuncts of a WHERE clause, as a comparable set.

    Returns None when the clause cannot be reduced to a plain conjunction.
    An OR makes the clause non-monotonic — adding it can widen the result
    rather than narrow it — so predicate inclusion stops implying anything
    and the comparison must be abandoned rather than approximated.
    """
    if where is None:
        return frozenset()
    clause = " ".join(where.split())
    if re.search(r"\bor\b", clause, re.I):
        return None
    parts = [p.strip().strip("()").strip()
             for p in re.split(r"\band\b", clause, flags=re.I)]
    kept = {" ".join(p.lower().split()) for p in parts if p}
    return frozenset(kept) if kept else frozenset()


def _figure(question: str, result: dict) -> Figure | None:
    """The countable figure a part established, if it established one."""
    answer = result.get("answer")
    if answer is None:
        return None

    text = answer if isinstance(answer, str) else str(answer)
    numbers = [int(m.group(0).replace(",", "")) for m in _NUM.finditer(text)]
    if len(numbers) != 1:
        # Zero numbers is prose; several is a breakdown, and which one the
        # part "is" would be a guess.
        return None

    # The query that produced it. Later queries are tried first: an agent
    # explores before it concludes, so the last successful count is the
    # one the answer rests on.
    for entry in reversed(result.get("queries") or []):
        sql = entry.get("sql") or ""
        if entry.get("error") or _JOIN.search(sql):
            continue
        m = _COUNT.match(" ".join(sql.split()))
        if not m:
            continue
        preds = _predicates(m.group(2))
        if preds is None:
            continue
        return Figure(question, numbers[0], m.group(1).lower(), preds)
    return None


def crosscheck(pairs: list[tuple[str, dict]]) -> list[str]:
    """Contradictions between the parts of one question, in plain words."""
    figures = [f for f in (_figure(q, r) for q, r in pairs) if f is not None]
    if len(figures) < 2:
        return []

    conflicts: list[str] = []
    for i, a in enumerate(figures):
        for b in figures[i + 1:]:
            if a.base != b.base:
                continue
            # Which one is narrower is read off the predicates, not the
            # wording. Strict inclusion only: an equal set means the same
            # question asked twice, and two different answers to that is a
            # separate problem this check is not looking for.
            if a.predicates < b.predicates:
                wide, narrow = a, b
            elif b.predicates < a.predicates:
                wide, narrow = b, a
            else:
                continue
            if narrow.value > wide.value:
                conflicts.append(
                    f"\"{narrow.question}\" gives {narrow.value}, but "
                    f"\"{wide.question}\" gives {wide.value} — the first "
                    f"counts a subset of the second, so it cannot be "
                    f"larger. One of these two figures is wrong.")
    return conflicts
