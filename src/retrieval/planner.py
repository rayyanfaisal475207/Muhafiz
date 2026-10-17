"""Turn a question into a typed plan, and a typed plan into SQL.

THE SHAPE, AND WHY

    QUESTION
      -> bind terms against real schema and real values   (the database answers)
      -> gate: is this even about the data?
      -> plan: capabilities, not a route
      -> compile: SQL for what SQL can answer
      -> execute + validate

Nothing here classifies a question into a category. There are millions of
categories, which is exactly why the deployed router — one LLM call picking
one of ten routes — fails on anything that needs two of them at once.

Instead the BINDINGS decide. "how many unlicensed weapons in Karachi" binds
three terms to real columns and values; those bindings imply a count, two
filters and a join, and the join path is discovered rather than written.

ESCALATION IS MEASURED, NOT GUESSED

One model call is enough when every term binds cleanly. The loop is for
the hard ones, and what makes a question hard is a number the database
returns, not an opinion:

    a term did not bind
    a term bound two ways with near-equal scores
    two join paths cost within 10% of each other
    a filter column is mostly null
    the question has more than one atomic ask

Each is deterministic. If none fire, the plan executes on one call.

WHAT THE MODEL DOES AND DOES NOT DO

It names terms at the front and phrases the answer at the back. It never
chooses a stored value, a join path, or a number. Those come from the
database, because a model that guesses what a column contains is the
failure this whole rebuild exists to remove — it filtered "unlicensed"
against 'بغیر لائسنس' and returned 0 where the truth was 30.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Route(str, Enum):
    """What kind of answering this question needs — derived, not classified."""

    SMALL_TALK = "small_talk"          # binds nothing, names nothing
    WORLD_KNOWLEDGE = "world"          # binds nothing, names an unknown term
    DATA = "data"                      # binds to real schema


class Capability(str, Enum):
    """How a bound term must be answered. Several compose in one plan."""

    SQL_FILTER = "sql_filter"          # exact stored value -> WHERE
    SQL_AGGREGATE = "sql_aggregate"    # count, sum, min/max over rows
    SQL_JOIN = "sql_join"              # reach another table
    TEXT_SEARCH = "text_search"        # free text -> vector + BM25
    GRAPH_TRAVERSAL = "graph"          # variable-depth, identity chains
    WORLD_LOOKUP = "world_lookup"      # term meaning, cross-script confirm


@dataclass
class Binding:
    """One term, located in the schema, with the evidence for it."""

    term: str
    match_kind: str                    # table | column | value | cross_script
    table: str | None
    column: str | None
    value: str | None
    score: float
    table_role: str | None
    ambiguous: bool = False

    @property
    def is_exact_value(self) -> bool:
        return self.match_kind == "value" and self.score >= 0.999

    @property
    def needs_confirmation(self) -> bool:
        """Cross-script matches are candidates: romanisation is lossy."""
        return self.match_kind == "cross_script" or self.ambiguous


@dataclass
class Escalation:
    """Why one model call is not enough. Empty means it is."""

    unbound_terms: list[str] = field(default_factory=list)
    ambiguous_terms: list[str] = field(default_factory=list)
    close_join_paths: list[str] = field(default_factory=list)
    sparse_filters: list[str] = field(default_factory=list)
    multi_part: bool = False

    @property
    def required(self) -> bool:
        return bool(self.unbound_terms or self.ambiguous_terms
                    or self.close_join_paths or self.sparse_filters
                    or self.multi_part)

    def reasons(self) -> list[str]:
        out = []
        if self.unbound_terms:
            out.append(f"{len(self.unbound_terms)} term(s) did not bind: "
                       + ", ".join(self.unbound_terms[:4]))
        if self.ambiguous_terms:
            out.append("ambiguous binding for: "
                       + ", ".join(self.ambiguous_terms[:4]))
        if self.close_join_paths:
            out.append("two join paths of near-equal cost: "
                       + "; ".join(self.close_join_paths[:2]))
        if self.sparse_filters:
            out.append("filter on a mostly-empty column: "
                       + ", ".join(self.sparse_filters[:3]))
        if self.multi_part:
            out.append("more than one question was asked")
        return out


@dataclass
class Plan:
    question: str
    route: Route
    bindings: list[Binding] = field(default_factory=list)
    capabilities: set[Capability] = field(default_factory=set)
    tables: list[str] = field(default_factory=list)
    join_steps: list[str] = field(default_factory=list)
    escalation: Escalation = field(default_factory=Escalation)
    # What the embedding model says the question is about. Kept on the
    # plan so the agent can be shown it and so a wrong ranking can be
    # explained after the fact rather than guessed at.
    semantic: list = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    # Words this question needed that the system had to learn, with what
    # they turned out to mean. Surfaced so an answer built on a looked-up
    # word says so.
    learned: list[str] = field(default_factory=list)
    refusal: str | None = None

    def summary(self) -> str:
        if self.refusal:
            return f"REFUSE: {self.refusal}"
        caps = ", ".join(sorted(c.value for c in self.capabilities)) or "none"
        return (f"{self.route.value} | tables={self.tables or '-'} | "
                f"capabilities={caps} | "
                f"escalate={'yes' if self.escalation.required else 'no'}")


# ── Candidate terms ─────────────────────────────────────────────────────
# Every 1-, 2- and 3-word window is offered to the binder. Words that mean
# nothing to this database simply fail to bind and cost nothing, which is
# why no stopword list exists here — one would be an authored vocabulary,
# and it would be wrong in Urdu, Roman Urdu, and the mixed script this
# corpus actually uses.

_WORD = re.compile(r"[^\s،,.؟?!;:()\[\]\"']+")


def candidate_terms(question: str, max_n: int = 3) -> list[str]:
    words = _WORD.findall(question or "")
    terms: list[str] = []
    seen: set[str] = set()
    for n in range(1, max_n + 1):
        for i in range(len(words) - n + 1):
            t = " ".join(words[i:i + n]).strip()
            if len(t) < 2:
                continue
            key = t.casefold()
            if key not in seen:
                seen.add(key)
                terms.append(t)
    return terms


# ── Gate ────────────────────────────────────────────────────────────────
# Three outcomes from one primitive, no classifier:
#
#   binds nothing, no domain-looking term  -> answer from the prompt
#   binds nothing, a domain-looking term   -> world knowledge
#   binds something                        -> plan against the data
#
# A "domain-looking" term is judged structurally: capitalised mid-sentence,
# contains digits with letters (CNSA 1997, 328A), or is non-Latin script.
# That is orthography, not a vocabulary of known terms.

_DOMAINISH = re.compile(r"[A-Z][a-z]{2,}|[A-Za-z]+\s*\d{2,}|\d+[A-Z]|[؀-ۿ]")


def gate(question: str, bindings: list[Binding]) -> Route:
    if bindings:
        return Route.DATA
    return (Route.WORLD_KNOWLEDGE if _DOMAINISH.search(question or "")
            else Route.SMALL_TALK)


# ── Capabilities fall out of the bindings ───────────────────────────────

_COUNTING = re.compile(
    r"\bhow many\b|\bcount\b|\bnumber of\b|\btotal\b|\bkitne\b|کتنے|کتنی|تعداد",
    re.I)
_COMPARING = re.compile(
    r"\bcompare\b|\bversus\b|\bvs\b|\btrend\b|\bmost\b|\bhighest\b|\bratio\b"
    r"|تناسب|سب سے", re.I)


# Words that express the SHAPE of a question rather than name anything in
# the data: "how many", "above", "list", "which".
#
# This is NOT the authored-vocabulary mistake. These describe the QUESTION
# LANGUAGE, not the corpus: no status, category, weapon type or place name
# appears here, and adding a new kind of case never requires touching it.
# Same distinction as normalize_name (writing system) versus a list of
# stored values (data).
#
# Needed because without it "how many cases are registered" REFUSED: the
# word "many" matched no column, so a question the planner had in fact
# planned correctly was reported as unanswerable.
BOUNDARY = chr(92) + "b"

# Phrases that express the SHAPE of a question rather than name anything
# in the data. Built from a list so the pattern carries no hand-written
# escapes -- an earlier version wrote literal backspace bytes instead of
# word boundaries, and every match silently returned nothing.
#
# This is NOT the authored-vocabulary mistake. These describe the QUESTION
# LANGUAGE, not the corpus: no status, category, weapon type or place name
# appears here, and adding a new kind of case never requires touching it.
#
# Needed because without it "how many cases are registered" REFUSED --
# the word "many" matched no column, so a question the planner had in
# fact planned correctly was reported as unanswerable.
_INTENT_PHRASES = (
    "how many", "how much", "number of", "at least", "more than",
    "less than", "compared to", "count", "total", "list", "show",
    "which", "what", "who", "where", "when", "above", "below", "over",
    "under", "between", "are", "is", "have", "has", "does", "do",
    "the", "and", "in", "of", "a", "an",
    "کتنے", "کتنی",
    "تعداد", "کون",
    "کیا", "کہاں", "کب",
)

_INTENT = re.compile(
    "|".join(
        "" + BOUNDARY + re.escape(ph).replace("\\ ", "\\s+") + BOUNDARY
        for ph in sorted(_INTENT_PHRASES, key=len, reverse=True)
    ),
    re.I,
)


def intent_words(question: str) -> set[str]:
    """Words consumed by question-shape phrases, casefolded."""
    out: set[str] = set()
    for m in _INTENT.finditer(question or ""):
        for w in m.group(0).split():
            out.add(w.casefold())
    return out



def derive_capabilities(question: str,
                        bindings: list[Binding]) -> set[Capability]:
    """What must run, implied by what the terms turned out to be.

    Deliberately additive: a question needing a filter AND a count AND a
    join gets all three. The deployed router picks one route and loses the
    rest, which is why "how many weapons in Karachi" degrades there.
    """
    caps: set[Capability] = set()

    for b in bindings:
        if b.is_exact_value:
            caps.add(Capability.SQL_FILTER)
        elif b.match_kind == "cross_script":
            caps.add(Capability.WORLD_LOOKUP)
            caps.add(Capability.SQL_FILTER)
        elif b.match_kind == "column" and b.table_role == "text":
            caps.add(Capability.TEXT_SEARCH)

    if _COUNTING.search(question or "") or _COMPARING.search(question or ""):
        caps.add(Capability.SQL_AGGREGATE)

    if len({b.table for b in bindings if b.table}) > 1:
        caps.add(Capability.SQL_JOIN)

    # Nothing bound to a value or a countable column: the question is about
    # what documents SAY, not what rows hold.
    if not caps:
        caps.add(Capability.TEXT_SEARCH)

    return caps


# ── SQL compilation ─────────────────────────────────────────────────────


def primary_key_of(table: str, pk_map: dict[str, str]) -> str | None:
    return pk_map.get(table)


def compile_sql(plan: Plan, pk_map: dict[str, str] | None = None) -> str | None:
    """A FAST PATH, not the answering engine.

    This compiles the narrow case where every term bound cleanly and no
    escalation fired: a count or a simple filter over one or two tables. It
    exists so "how many cases are registered" costs ~100ms and no model
    call, not because SQL generation is how questions get answered.

    Everything else goes to the agent, which inspects the schema and
    composes its own queries. That is deliberate. A compiler that tries to
    cover more question shapes has to enumerate them, and question shapes
    are combinatorial — the same reason there is no route taxonomy and no
    vocabulary table.

    A companion module once derived measures, time windows and group-bys
    here to widen this path. It was deleted: the agent had made it obsolete
    before it was finished, and widening a compiler is the wrong direction.

    Returns None whenever the plan is not this narrow case, which is the
    signal to use the agent rather than to force something out.
    """
    """Build SQL from a plan whose joins were discovered, not authored.

    Returns None when the plan is not a SQL question — text search and
    world lookups compile elsewhere. A plan that cannot compile must say
    so rather than emit something that runs and means nothing.
    """
    if Capability.SQL_AGGREGATE not in plan.capabilities and \
       Capability.SQL_FILTER not in plan.capabilities:
        return None
    if not plan.tables:
        return None

    base = plan.tables[0]
    pk_map = pk_map or {}
    pk = pk_map.get(base)

    # MEASURED: "how many weapons are recorded" joined weapons to
    # case_events and answered 227 where the truth is ~80. A join to a
    # one-to-many table multiplies the base rows, and count(*) counts the
    # product. That is exactly L4 — the graph's 73 PART_OF edges for 19
    # stations — reappearing in generated SQL.
    #
    # Counting DISTINCT on the base table's primary key makes the fan-out
    # structurally harmless: however many event rows a weapon joins to, it
    # is still one weapon.
    if Capability.SQL_AGGREGATE in plan.capabilities:
        if pk:
            select = f"count(DISTINCT {base}.{pk}) AS answer"
        else:
            select = "count(*) AS answer"
    else:
        select = "*"

    parts = [f"SELECT {select}", f"FROM {base}"]

    # Join steps come from find_join_paths: discovered by cost, with
    # provenance hubs de-prioritised, never written by hand.
    seen = {base}
    for step in plan.join_steps:
        m = re.match(r"(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)", step)
        if not m:
            continue
        left_t, _, right_t, _ = m.groups()
        target = right_t if right_t not in seen else left_t
        if target in seen:
            continue
        parts.append(f"JOIN {target} ON {step}")
        seen.add(target)

    wheres = []
    for b in plan.bindings:
        if b.is_exact_value and b.table and b.column and b.value is not None:
            if b.table not in seen:
                continue
            # The stored value verbatim. Never a translation, never a guess
            # at what the column "probably" holds.
            wheres.append(f"{b.table}.{b.column} = {_lit(b.value)}")

    # Bitemporal default: a count means what is true NOW. A superseded
    # assertion stays queryable but must not be counted twice.
    for t in sorted(seen):
        if t in _TEMPORAL_TABLES:
            wheres.append(f"{t}.valid_to IS NULL")

    if wheres:
        parts.append("WHERE " + "\n  AND ".join(wheres))

    return "\n".join(parts)


_TEMPORAL_TABLES = {
    "case_events", "case_officers", "case_parties", "case_station_history",
    "case_statutes", "phone_numbers", "seized_items", "vehicles", "weapons",
}


def _lit(value: str) -> str:
    """Single-quote a literal. Values arrive from the database itself, but
    quoting is still explicit rather than assumed safe."""
    return "'" + str(value).replace("'", "''") + "'"
