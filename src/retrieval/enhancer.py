"""Turn one question into the terms each retriever can actually match.

THE PROBLEM IT SOLVES

A person asks in English. The corpus is Urdu. Every retriever fails
differently on that mismatch, and all of them fail silently:

    SQL       license_state_raw = 'unlicensed'   -> 0 rows, looks like an
                                                    answer. The truth is 30.
    BM25      tokenises 'unlicensed'             -> no such token exists
    vector    embeds an English sentence         -> drifts toward English
                                                    statute text, of which
                                                    this corpus has ten
                                                    times more than case text
    Cypher    MATCH (:Weapon {status:'unlicensed'}) -> no match
    web       would answer, but about the world, not this corpus

One rewrite cannot fix all four, because they need different things. SQL
needs the EXACT stored string. BM25 needs tokens in the script actually
indexed. Vector needs fluent text in the corpus language. Cypher needs
label and property names. So the enhancer produces a grounded bundle, not
a sentence.

WHERE THE GROUNDING COMES FROM

The database, not the model. Every term is looked up with search_schema,
which returns real stored values in whatever script they are stored in.
The model is never asked what a column probably contains — that guess is
the failure this whole rebuild exists to remove.

A term that grounds nowhere is reported as needing world knowledge rather
than forced into a filter. Refusing to invent a value is the point.

TWO VARIANTS, NOT TEN

A precise one for structured retrieval and a loose one for semantic
search. They are different queries, not two phrasings of the same one, and
that is the whole reason to have two.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg

from src.retrieval.planner import candidate_terms, intent_words


@dataclass
class GroundedTerm:
    """A word from the question, located in real data."""

    term: str                    # as the person typed it
    table: str
    column: str | None
    value: str | None            # the stored form, in its own script
    match_kind: str              # value | contains | column | table | cross_script
    score: float

    @property
    def confirmed(self) -> bool:
        """Cross-script matches are candidates until something confirms them."""
        return self.match_kind != "cross_script"


@dataclass
class EnhancedQuery:
    question: str
    equality: list[GroundedTerm] = field(default_factory=list)
    contains: list[GroundedTerm] = field(default_factory=list)
    columns: list[GroundedTerm] = field(default_factory=list)
    candidates: list[GroundedTerm] = field(default_factory=list)
    unground: list[str] = field(default_factory=list)

    precise: str = ""            # for SQL / Cypher planning
    semantic: str = ""           # for vector search
    lexical: list[str] = field(default_factory=list)   # for BM25

    def tables(self) -> list[str]:
        seen: list[str] = []
        for g in self.equality + self.contains + self.columns:
            if g.table not in seen:
                seen.append(g.table)
        return seen

    def summary(self) -> str:
        return (f"equality={len(self.equality)} contains={len(self.contains)} "
                f"columns={len(self.columns)} candidates={len(self.candidates)} "
                f"unground={self.unground}")


async def enhance(conn: asyncpg.Connection, question: str,
                  per_term: int = 3) -> EnhancedQuery:
    out = EnhancedQuery(question=question)
    terms = candidate_terms(question)
    if not terms:
        return out

    # Bind every FORM of every term, not just the word as typed.
    #
    # The corpus is mixed: some values are English ('weapon'), some Urdu
    # (ہتھیار), and which one a row uses depends on who typed it. A question
    # in either language therefore reaches only part of its own data, and
    # neither half-answer says so.
    #
    # term_forms() returns the word plus every equivalent the system knows —
    # translations it has learned, and other names the schema records for
    # the same object. Searching all of them is what makes a bilingual
    # corpus answerable from either language.
    expanded: list[str] = []
    form_origin: dict[str, str] = {}
    for t in terms:
        expanded.append(t)
        form_origin[t] = t
        try:
            for r in await conn.fetch(
                    "SELECT form FROM term_forms($1) WHERE form <> $1", t):
                f = r["form"]
                if f and f not in form_origin:
                    expanded.append(f)
                    form_origin[f] = t          # remember what it came from
        except Exception:                       # noqa: BLE001
            continue

    rows = await conn.fetch(
        "SELECT term, rank, match_kind, table_name, column_name, value_raw,"
        " score, table_role FROM bind_terms($1, $2, $3)",
        expanded[:120], 0.30, per_term)

    # Keep the strongest grounding per (term, kind) so one word does not
    # contribute the same evidence several times.
    # Translation must CONFIRM, not multiply.
    #
    # MEASURED: expanding every term into all its language forms and
    # treating each as its own candidate flooded the agent with leads. "How
    # many weapons" improved (refusal -> 96 in 20s) while "cases from
    # Lahore" and "cases registered" regressed from correct answers to
    # refusals. More reach, less convergence — translation had become a
    # source of ambiguity, which is the opposite of its job.
    #
    # So forms of the SAME question word that land on the SAME target are
    # one finding, reinforced. Agreement across languages is evidence: if
    # both 'weapons' and 'ہتھیار' point at the weapons table, that target is
    # more certain, not more contested. Only genuine disagreement — the
    # same word pointing somewhere else — stays as a rival.
    agreed: dict[tuple[str, str, str, str], GroundedTerm] = {}
    support: dict[tuple[str, str, str, str], set[str]] = {}

    for r in rows:
        origin = form_origin.get(r["term"], r["term"])
        g = GroundedTerm(
            # Report the word the PERSON used, so a binding is traceable to
            # their question rather than to a translation they never saw.
            term=origin,
            table=r["table_name"], column=r["column_name"],
            value=r["value_raw"], match_kind=r["match_kind"],
            score=float(r["score"] or 0))
        key = (origin, g.match_kind, r["table_name"],
               (r["column_name"] or "") + "|" + (r["value_raw"] or ""))
        support.setdefault(key, set()).add(r["term"])
        if key not in agreed or g.score > agreed[key].score:
            agreed[key] = g

    # Corroboration across forms raises confidence, capped at 1.0 so a
    # reinforced match cannot outrank an exact one.
    best: dict[tuple[str, str], GroundedTerm] = {}
    for key, g in agreed.items():
        forms = support.get(key, set())
        if len(forms) > 1:
            g.score = min(1.0, g.score * (1.0 + 0.1 * (len(forms) - 1)))
        slot = (g.term, g.match_kind)
        if slot not in best or g.score > best[slot].score:
            best[slot] = g

    for g in best.values():
        if g.match_kind == "value":
            out.equality.append(g)
        elif g.match_kind == "contains":
            out.contains.append(g)
        elif g.match_kind == "cross_script":
            out.candidates.append(g)
        else:
            out.columns.append(g)

    grounded_words = {w.casefold()
                      for g in best.values()
                      for w in g.term.split()}
    consumed = intent_words(question)
    out.unground = [
        t for t in candidate_terms(question, max_n=1)
        if t.casefold() not in grounded_words
        and t.casefold() not in consumed
        and len(t) > 2
    ]

    _build_variants(out)
    return out


def _build_variants(q: EnhancedQuery) -> None:
    """Two queries for two jobs.

    PRECISE restates the question in terms of real columns and real stored
    values, so a planner or an agent starts from what exists rather than
    from what the words suggest.

    SEMANTIC is the opposite: it carries the STORED forms into fluent text
    so an embedding lands near the corpus rather than near the language the
    question happened to be asked in. That is the step that lets an English
    question retrieve Urdu passages — the embedding sees 'بغیر لائسنس',
    not 'unlicensed'.
    """
    parts: list[str] = []
    for g in q.equality:
        parts.append(f"{g.table}.{g.column} = '{g.value}'")
    for g in q.contains:
        parts.append(f"{g.table}.{g.column} contains '{g.value}'")
    for g in q.columns:
        parts.append(f"{g.table}.{g.column}" if g.column else g.table)
    for g in q.candidates:
        parts.append(f"{g.table}.{g.column} ~ '{g.value}' (unconfirmed)")
    q.precise = (q.question + ("\nGrounded in: " + "; ".join(parts)
                               if parts else "\nNothing grounded."))

    # Only forms that ARE the term get injected.
    #
    # MEASURED FAILURE: injecting `contains` values made retrieval worse.
    # For a contains match the stored value is the CONTAINING FIELD, not
    # the matched term — so "FIR 418/26", an unrelated case, was appended
    # to a question about FIR 201/26. Vector search then returned 213/26,
    # 403/26 and 421/26, while the RAW question returned 201/26 correctly.
    # The enhancer was degrading the thing it exists to improve.
    #
    # equality     the field equals the term -> the stored form IS the term
    # cross_script the romanised match of the term -> its form in the
    #              corpus script, which is the whole point (Karachi -> کراچی)
    # contains     the term is already in the question; the container is
    #              somebody else's text and must not be added
    stored: list[str] = []
    for g in q.equality + q.candidates:
        if not g.value:
            continue
        # A long value is a passage, not a term, whatever matched it.
        if len(str(g.value)) > 60:
            continue
        if g.value not in stored:
            stored.append(g.value)
    q.semantic = " ".join([q.question] + stored[:8])

    # BM25 wants exact tokens, in every script they were observed in, plus
    # the user's own words — an index may hold either.
    lexical: list[str] = []
    for v in stored:
        for tok in str(v).split():
            if len(tok) > 1 and tok not in lexical:
                lexical.append(tok)
    # A contains match contributes the column it was found in as a place to
    # look, not its container text as query terms.
    for g in q.contains:
        if g.value and len(str(g.value)) <= 40:
            for tok in str(g.value).split():
                if len(tok) > 1 and tok not in lexical:
                    lexical.append(tok)
    for t in candidate_terms(q.question, max_n=1):
        if len(t) > 2 and t.casefold() not in intent_words(q.question):
            if t not in lexical:
                lexical.append(t)
    q.lexical = lexical[:24]


# ── Cypher grounding ────────────────────────────────────────────────────
# The graph needs the same treatment for the same reason: a Cypher pattern
# written from the question's words matches nothing when the graph stores
# another script, and returns an empty result that reads as "none".


@dataclass
class GraphGrounding:
    labels: list[str] = field(default_factory=list)
    properties: list[tuple[str, str]] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    note: str = ""


async def ground_for_graph(conn: asyncpg.Connection,
                           q: EnhancedQuery) -> GraphGrounding:
    """Map grounded SQL terms onto graph labels and properties.

    The graph is a projection of SQL (design axiom 5), so a table maps to a
    label and a column to a property. Discovered from the graph's own
    catalog rather than assumed, because a projection that has drifted
    should be visible, not papered over.
    """
    g = GraphGrounding()
    try:
        rows = await conn.fetch(
            """
            SELECT c.relname::text AS label
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'evidence_graph' AND c.relkind = 'r'
              AND c.relname NOT LIKE '\\_%'
            """)
    except Exception:                                   # noqa: BLE001
        g.note = "graph schema unavailable"
        return g

    labels = {r["label"].lower(): r["label"] for r in rows}
    for table in q.tables():
        stem = table.replace("rebuild_", "").rstrip("s")
        for low, original in labels.items():
            if stem and (stem in low or low in stem):
                if original not in g.labels:
                    g.labels.append(original)

    for t in q.equality + q.contains:
        if t.column:
            pair = (t.table, t.column)
            if pair not in g.properties:
                g.properties.append(pair)
        if t.value and t.value not in g.values:
            g.values.append(t.value)

    if not g.labels:
        g.note = ("no graph label matches the grounded tables; the graph "
                  "projection may be stale relative to SQL")
    return g
