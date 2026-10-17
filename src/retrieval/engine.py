"""Run a question through bind -> gate -> plan -> compile -> execute.

Every decision here is made from something the database returned. The
model's only jobs are at the two ends: naming candidate terms (and even
that is done without it, by n-gram sweep) and phrasing the final answer.

The number itself is always computed by SQL and never re-derived by a
model, because a model reading rows and reporting a total is the counting-
by-reading problem this rebuild exists to remove.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import asyncpg  # noqa: E402

from src.retrieval.planner import (  # noqa: E402
    Binding, Capability, Escalation, Plan, Route,
    candidate_terms, compile_sql, derive_capabilities, gate, intent_words,
)
from src.retrieval.crosscheck import crosscheck  # noqa: E402
from src.retrieval.decompose import decompose  # noqa: E402
from src.retrieval.enhancer import enhance  # noqa: E402
from src.retrieval.health import Ledger, Severity  # noqa: E402
from src.retrieval.interpret import interpret  # noqa: E402
from src.retrieval.semantic_bind import (  # noqa: E402
    SchemaHit, fuse_tables, rank_schema,
)
from src.retrieval.text_search import hybrid_search  # noqa: E402


def dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def bind(conn: asyncpg.Connection, question: str,
               min_score: float = 0.45) -> list[Binding]:
    """Locate every candidate term in real schema and real values.

    One round trip for the whole question — measured at 70-112 ms — which
    is what makes this affordable before every query rather than a step
    reserved for hard ones.
    """
    terms = candidate_terms(question)
    if not terms:
        return []
    rows = await conn.fetch(
        "SELECT term, rank, match_kind, table_name, column_name, value_raw,"
        " score, table_role, ambiguous FROM bind_terms($1, $2, $3)",
        terms, min_score, 3,
    )
    return [
        Binding(
            term=r["term"], match_kind=r["match_kind"], table=r["table_name"],
            column=r["column_name"], value=r["value_raw"],
            score=float(r["score"] or 0), table_role=r["table_role"],
            ambiguous=bool(r["ambiguous"]),
        )
        for r in rows
    ]


def _best_per_term(bindings: list[Binding]) -> list[Binding]:
    """Keep the strongest binding per term, flagging only ambiguity that matters.

    A term with two near-equal readings is only ambiguous if choosing
    between them would change the ANSWER. Measured: "how many FIRs are
    registered" flagged "FIRs" ambiguous because it matched fir_number and
    fir_serial equally — two columns of the SAME table, and for a count of
    that table the column is irrelevant. The question was planned
    correctly and refused anyway.

    So ambiguity is judged on the TABLE, and on the value when one is
    being filtered. Same table and no competing value means one reading.
    """
    grouped: dict[str, list[Binding]] = {}
    for b in bindings:
        grouped.setdefault(b.term, []).append(b)

    # Evidence tiers. A stored VALUE is direct evidence about the data; a
    # column name is weaker; a table name weaker still. A value match is
    # not "rivalled" by a table whose NAME happens to resemble the term —
    # measured: "station" matched places.kind_raw='police_station' (0.533)
    # and was flagged ambiguous against the table case_station_history
    # (0.500), which is not a competing reading, just a similar word.
    tier = {"value": 0, "cross_script": 1, "column": 2, "table": 3}

    out: list[Binding] = []
    for term, group in grouped.items():
        group.sort(key=lambda b: (tier.get(b.match_kind, 9), -b.score))
        winner = group[0]
        # The SQL binder sets its own ambiguity flag on score proximity
        # alone. Recompute from scratch here, where table and value are
        # visible — leaving the old flag set made every fix a no-op.
        winner.ambiguous = False

        rivals = [
            b for b in group[1:]
            if tier.get(b.match_kind, 9) == tier.get(winner.match_kind, 9)
            and abs(b.score - winner.score) <= max(winner.score * 0.10, 1e-9)
        ]
        for r in rivals:
            same_table = (r.table == winner.table)
            competing_value = (winner.value is not None
                               and r.value is not None
                               and r.value != winner.value)
            if not same_table or competing_value:
                winner.ambiguous = True
                break
        out.append(winner)
    return sorted(out, key=lambda b: (tier.get(b.match_kind, 9), -b.score))


async def _learn_unbound(conn: asyncpg.Connection, question: str,
                         chosen: list[Binding]) -> list[str]:
    """Look up the question's words that bound to nothing, and keep them.

    Returns the terms that were successfully learned AND bound, which is
    the caller's signal to re-bind — a lookup that resolved to nothing
    changes no outcome and must not trigger a second round trip.

    Only content words are tried: `candidate_terms` sweeps every n-gram,
    and asking the web what "کتنے" means would burn a lookup on grammar.
    `intent_words` already knows the difference, and learn() refuses
    function words itself as a second line of defence.
    """
    from src.retrieval.learn_term import learn, remember

    bound = {b.term for b in chosen}
    intent = {w.lower() for w in intent_words(question)}
    unknown = [t for t in candidate_terms(question)
               if t not in bound and t.lower() not in intent
               and len(t.split()) == 1]

    got: list[str] = []
    for term in unknown[:3]:
        try:
            found = await learn(conn, term)
        except Exception:                               # noqa: BLE001
            continue
        if found.usable and found.bound_to:
            await remember(conn, found)
            got.append(f"{term} = {found.english}")
    return got


async def _non_empty(conn: asyncpg.Connection,
                     tables: list[str]) -> list[str]:
    """Those of `tables` that actually hold rows, in the same order.

    Counted live rather than read from statistics: reltuples is an
    estimate maintained by ANALYZE, and this decides whether a table may
    answer a question at all. The schema is small and these are indexed
    counts, so the cost is a few milliseconds.
    """
    if not tables:
        return []
    keep: list[str] = []
    for name in tables:
        if not name.replace("_", "").isalnum():
            continue                       # never interpolate a stray name
        try:
            n = await conn.fetchval(f'SELECT count(*) FROM "{name}"')
        except Exception:                                   # noqa: BLE001
            keep.append(name)              # unreadable is not the same as empty
            continue
        if n:
            keep.append(name)
    return keep


async def build_plan(conn: asyncpg.Connection, question: str,
                     learn_unknown: bool = True) -> Plan:
    raw = await bind(conn, question)
    chosen = _best_per_term(raw)
    route = gate(question, chosen)

    # ── A word we do not know is not a question we cannot answer ────────
    #
    # MEASURED, and the reason this block sits ABOVE the early return:
    #
    #   "کتنے تھانے ہیں؟"  ->  WORLD_KNOWLEDGE in 0.8s
    #
    # Nothing bound, so the gate wrote the question off as being about the
    # outside world. But the corpus has 19 police stations, and تھانہ is
    # simply the Urdu word for one — a fact any language model knows, and
    # a fact src/retrieval/learn_term.py exists to go and find.
    #
    # It was never called. `learn` ran AFTER this early return, so the one
    # layer built for "a word the system has never met" was unreachable
    # for every word the system had never met. The capability was present,
    # wired, tested, and dead.
    #
    # So: before concluding a question is not about this data, find out
    # what its words mean. Only if they still bind to nothing is the
    # question genuinely about something else.
    if route is not Route.DATA and learn_unknown:
        learned_terms = await _learn_unbound(conn, question, chosen)
        if learned_terms:
            raw = await bind(conn, question)
            chosen = _best_per_term(raw)
            route = gate(question, chosen)

    plan = Plan(question=question, route=route, bindings=chosen)
    if route is not Route.DATA:
        return plan

    plan.capabilities = derive_capabilities(question, chosen)

    # Tables, most strongly bound first, so the FROM clause is the table
    # the question is actually about.
    #
    # Only tables that MATERIALLY contribute are kept. Measured: "how many
    # FIRs are currently registered" bound rebuild_cases at 1.0 and also
    # case_events at 0.4 (because "registered" resembles recorded_at).
    # That second table added nothing to the question but did create a
    # join, whose two near-equal routes then escalated the plan into a
    # refusal. A weak column-name resemblance is not a reason to join.
    #
    # Material means: it carries an exact stored value the question filters
    # on, or it is the strongest binding, or it is close to the strongest.
    best_score = max((b.score for b in chosen if b.table), default=0.0)
    keep_floor = max(0.6, best_score * 0.8)
    tables: list[str] = []

    # An EXACT table-name match names the thing being asked about, so it is
    # always a candidate. Evidence tiers rank a stored value above a table
    # name, which is right when the name merely sounds similar — but
    # measured, "how many cases have a weapon" bound `weapons` (the table,
    # 1.0) below `seized_items.category = 'weapon'` (also 1.0), and the
    # agent spent ten turns querying the wrong tables while the right one
    # sat unconsidered.
    for b in chosen:
        if (b.table and b.match_kind == "table" and b.score >= 0.99
                and b.table not in tables):
            tables.append(b.table)

    for b in chosen:
        if not b.table or b.table in tables:
            continue
        if b.is_exact_value or b.score >= keep_floor:
            tables.append(b.table)
    if not tables:
        tables = [b.table for b in chosen if b.table][:1]

    # ── What the words found, fused with what the question MEANS ────────
    #
    # Everything above this point is spelling: trigram and exact matches
    # against identifiers and stored values. That is why English works and
    # Urdu does not — the identifiers are English, so an English question
    # collides with them by luck, while an Urdu question collides with
    # stored VALUES instead.
    #
    # Measured, "کتنے ہتھیار برآمد ہوئے؟":
    #   lexical    weapons 1.000  TIED WITH  case_events.event_type_raw 1.000
    #   semantic   weapons 0.8348, case_events not in the top five
    #
    # The agent took the event type and answered 7 against a truth of 96.
    # The embedding model that already serves passage retrieval had the
    # right answer the whole time and was never asked.
    #
    # Fused by RANK (RRF), never by score: a cosine and a trigram score
    # are not the same quantity. Ties break toward the lexical order, so
    # a question that already worked cannot be made worse — semantics can
    # only promote a table the words missed.
    semantic = await rank_schema(conn, question)
    plan.semantic = semantic
    if semantic:
        tables = fuse_tables(tables, semantic)

    # ── An empty table cannot answer anything ───────────────────────────
    #
    # `case_station_history` holds ZERO rows and wins lexical binding for
    # "cases" and "مقدمات" at 1.000, because its name contains "case". It
    # is a decoy, and it is the most dangerous kind: joining through it
    # returns nothing, and "nothing" renders as a confident 0.
    #
    #   "How many cases are registered?"  fast path -> 0   (truth 73)
    #
    # That question survives today only because its binding is ambiguous
    # enough to escalate to the agent, where check_empty_tables catches
    # it. The fast path runs NO guards, so anything that binds cleanly to
    # an empty table is answered without anything checking it.
    #
    # Rather than add a guard after the fact, the table is dropped here:
    # zero rows cannot contribute to any count, in any question, ever. If
    # every candidate is empty they are all kept, so the refusal can say
    # the data is missing instead of silently having no tables to use.
    populated = await _non_empty(conn, tables)
    all_empty = bool(tables) and not populated
    plan.tables = populated or tables
    plan.bindings = [b for b in chosen if b.table in tables or not b.table]

    esc = Escalation()

    # Every table the words found is empty. The fast path would compile a
    # perfectly valid query over zero rows and publish "0" — the exact
    # shape of a confident wrong number, and indistinguishable from a real
    # finding of none.
    #
    # MEASURED: "How many cases are registered?" binds `mقدمات`/"cases" to
    # case_station_history (0 rows) at 1.000, because the table's NAME
    # contains "case". Truth is 73, in rebuild_cases.
    #
    # It has been surviving on luck — its binding happened to be ambiguous
    # enough to escalate. Made deterministic here: nowhere to count means
    # hand it to the agent, which can look past the name.
    if all_empty:
        esc.sparse_filters.append(
            f"every table the wording matched is empty "
            f"({', '.join(tables[:3])}), so a count over them would be "
            f"zero for lack of data rather than as a finding")
    esc.ambiguous_terms = sorted({b.term for b in chosen if b.ambiguous})
    bound_terms = {b.term for b in chosen}
    # A term is only "unbound" if no window covering it bound either —
    # reporting every unbound n-gram would flag every question.
    # A term is unbound only if nothing bound it AND it is not part of a
    # question-shape phrase the planner already understood. Without the
    # second condition "how many cases are registered" refused because the
    # word "many" matched no column — refusing a question it had in fact
    # planned correctly.
    consumed = intent_words(question)
    esc.unbound_terms = [
        t for t in candidate_terms(question, max_n=1)
        if not any(t.casefold() in bt.casefold() for bt in bound_terms)
        and t.casefold() not in consumed
        and len(t) > 2
    ][:6]

    if len(tables) > 1:
        rows = await conn.fetch(
            "SELECT from_table, to_table, hops, path_cost, best_path,"
            " second_cost, close_call FROM plan_feasibility($1)", tables)
        for r in rows:
            if r["best_path"] is None:
                esc.unbound_terms.append(
                    f"{r['from_table']}~{r['to_table']} (no join path)")
                continue
            # A close call only matters when the two routes reach DIFFERENT
            # places. Measured: places->rebuild_cases came back 101 vs 102
            # and escalated, but both routes end at the same table, so the
            # answer is identical either way. Escalating there refuses a
            # question that was planned correctly.
            if r["close_call"] and _routes_differ(r["best_path"]):
                esc.close_join_paths.append(
                    f"{r['from_table']}->{r['to_table']}: "
                    f"{r['best_path']} (cost {r['path_cost']}) vs a route "
                    f"costing {r['second_cost']}")
        steps = await conn.fetchrow(
            "SELECT join_steps FROM find_join_paths($1, $2, 6, 1)",
            tables[0], tables[1])
        if steps and steps["join_steps"]:
            plan.join_steps = list(steps["join_steps"])

    # Coverage: a filter on a mostly-empty column returns a small plausible
    # number by discarding most of the population invisibly.
    for b in chosen:
        if b.table and b.column and b.is_exact_value:
            cov = await conn.fetchrow(
                "SELECT must_disclose, null_rate, populated, total, message"
                " FROM coverage_warning($1, $2)", b.table, b.column)
            if cov and cov["must_disclose"]:
                esc.sparse_filters.append(f"{b.table}.{b.column}")
                plan.coverage_notes.append(cov["message"])

    # Compound questions are SPLIT before they reach here (see
    # src/retrieval/decompose.py), so a question arriving at the planner
    # asks for one figure. This stays on the Escalation as the record of
    # why that layer exists; it is set by the caller, never guessed from
    # the words, because guessing from the words is what it replaced.

    # A term that binds to nothing may be a word the system has never met,
    # not a thing the corpus lacks. Those look identical and have opposite
    # right answers: refuse, or find out.
    #
    # So unbound terms are looked up on the open web and re-bound. Nothing
    # here holds a dictionary — the meaning is fetched when needed, and a
    # confirmed mapping is remembered so the next question is free. The
    # vocabulary grows with what people actually ask, never with the data.
    if esc.unbound_terms and learn_unknown:
        from src.retrieval.learn_term import learn, remember
        still_unbound: list[str] = []
        for term in esc.unbound_terms[:3]:
            try:
                learned = await learn(conn, term)
            except Exception:                           # noqa: BLE001
                still_unbound.append(term)
                continue
            if learned.usable and learned.bound_to:
                await remember(conn, learned)
                table = learned.bound_to[0][0]
                if table not in plan.tables:
                    plan.tables.append(table)
                plan.learned.append(
                    f"{term} = {learned.english}"
                    + (f" ({learned.meaning})" if learned.meaning else ""))
            else:
                still_unbound.append(term)
        esc.unbound_terms = still_unbound

    plan.escalation = esc
    return plan


def _routes_differ(best_path: str | None) -> bool:
    """Whether two near-equal routes would actually give different answers.

    A direct foreign key between two tables has one meaning. Ambiguity is
    real only when the route passes THROUGH something else — the city of a
    case's address versus the city of its station are different questions,
    while two ways of reaching the same table directly are not.
    """
    if not best_path:
        return False
    return len(best_path.split("->")) > 2


async def _primary_keys(conn: asyncpg.Connection,
                        tables: list[str]) -> dict[str, str]:
    """Single-column primary keys, read from the catalog.

    Needed so an aggregate can count DISTINCT the base entity instead of
    the joined product. Composite keys return nothing and fall back to
    count(*), which is honest: the compiler should not invent a key.
    """
    if not tables:
        return {}
    rows = await conn.fetch(
        "SELECT table_name, column_name FROM column_catalog"
        " WHERE table_name = ANY($1) AND is_primary_key", tables)
    seen: dict[str, str | None] = {}
    for r in rows:
        t = r["table_name"]
        seen[t] = None if t in seen else r["column_name"]
    return {t: c for t, c in seen.items() if c}


async def _hints(conn: asyncpg.Connection, plan: Plan,
                 passages: list | None = None, reading=None,
                 communities: str = "") -> str:
    """Give the agent what the deterministic pass already proved.

    Not instructions -- findings. The bindings came from the database, so
    the agent starts from real tables and real stored values instead of
    rediscovering them, and starts knowing why the fast path declined.
    """
    lines = []
    if getattr(plan, "learned", None):
        lines.append("Words looked up for this question: "
                     + "; ".join(plan.learned))
    if reading is not None and reading.available:
        lines.append("What the question means: " + reading.brief())
        if reading.descriptive:
            # A READING, never a directive. The interpreter correctly called
            # "registered" descriptive, then called "involve a weapon"
            # descriptive too — and that one genuinely restricts, 32 of 73
            # cases. Trusting it would have over-counted.
            #
            # So it proposes and the DATA decides: the selectivity guard
            # reports the population alongside every filtered count, which
            # settles it either way.
            lines.append("  Possibly descriptive rather than filters: "
                         + ", ".join(reading.descriptive)
                         + ". TEST this: count with and without the "
                           "condition. If the population is unchanged the "
                           "term describes the records; if it shrinks "
                           "materially it is a real filter. Do not assume.")
        if reading.reasoning:
            lines.append("  " + reading.reasoning)
    if plan.tables:
        lines.append("Terms in the question bound to these tables: "
                     + ", ".join(plan.tables))
    # How MUCH evidence each lead holds, not just how well it matched.
    #
    # MEASURED: "how many cases have a weapon" bound both `weapons` (the
    # table) and case_events.event_type_raw = 'arrest and weapon seizure'
    # at 1.0. The agent took the second and answered 1. The weapons table
    # holds 96 rows across 32 cases; that phrase matches exactly one row.
    #
    # Match score says how well a term fits. Row count says how much of the
    # answer is there. Without the second, equally-scored leads are
    # indistinguishable and the agent picks whichever came first.
    for b in plan.bindings[:6]:
        volume = ""
        if b.table:
            try:
                if b.value and b.column:
                    n = await conn.fetchval(
                        f'SELECT count(DISTINCT case_id) FROM "{b.table}"'
                        f' WHERE "{b.column}"::text = $1', b.value)
                else:
                    n = await conn.fetchval(
                        f'SELECT count(DISTINCT case_id) FROM "{b.table}"')
                volume = f" [{n} cases]" if n is not None else ""
            except Exception:                           # noqa: BLE001
                volume = ""
        if b.table and b.value:
            lines.append(f"  {b.term!r} occurs as {b.table}.{b.column} "
                         f"= {b.value!r} ({b.match_kind}){volume}")
        elif b.table:
            lines.append(f"  {b.term!r} -> {b.table}"
                         + (f".{b.column}" if b.column else "") + volume)
    if plan.bindings:
        lines.append("  Prefer the lead covering MORE cases: a phrase "
                     "matching one row is a coincidence, a table holding "
                     "the subject is the answer.")
    if plan.join_steps:
        lines.append("Discovered join: " + " AND ".join(plan.join_steps))
    for note in plan.coverage_notes[:2]:
        lines.append("Coverage: " + note)
    # Every evidence table, not just the bound ones.
    #
    # MEASURED: naming two tables as a "starting point" made them blinders.
    # Asked how many cases have a weapon, the agent wrote join after join
    # against rebuild_cases and case_events — the two tables it had been
    # handed — and did not reach the `weapons` table until turn 10, having
    # burned most of its budget. Telling it the list was partial did not
    # help; what it was shown is what it used.
    #
    # So it is shown everything, with the bound tables marked as a lead
    # rather than a boundary.
    rows = await conn.fetch(
        "SELECT table_name, role FROM table_roles"
        " WHERE is_evidence AND role IN ('fact','dimension','entity')"
        " ORDER BY role, table_name")
    if rows:
        lines.append("Every table that can answer a question about cases "
                     "(the bindings above are a lead, not a limit):")
        by_role: dict[str, list[str]] = {}
        for r in rows:
            by_role.setdefault(r["role"], []).append(r["table_name"])
        for role, names in by_role.items():
            lines.append(f"  {role}: " + ", ".join(names))

    if plan.escalation.reasons():
        lines.append("The single-query path declined because: "
                     + "; ".join(plan.escalation.reasons()))
    if passages:
        lines.append("")
        lines.append("Relevant passages already retrieved (each carries a "
                     "span you may cite; quote them rather than querying "
                     "description columns by hand):")
        for p in passages[:4]:
            lines.append(f"  [span {p.span_id}] {p.brief(200)}")

    # Cross-case pattern summaries, when there are any. Offered rather
    # than routed to: nothing here decides that a question is "thematic",
    # because that is a classification and classifications are what this
    # rebuild removed. The agent is shown what exists, told plainly that
    # it is derived text and that no figure may come from it, and decides.
    if communities:
        lines.append("")
        lines.append(communities)
    return chr(10).join(lines)


async def run(conn: asyncpg.Connection, question: str,
              use_agent: bool = True,
              case_id: str | None = None,
              ledger: Ledger | None = None,
              split: bool = True) -> dict:
    """Answer one question. A question asking several figures becomes several.

    `split` is what stops the recursion: the parts of a compound question
    are already one ask each, so they run with it off.

    `ledger` is threaded so every part records its degradations in the
    same place. A part that lost document search must weaken the whole
    answer, not just the line it produced.
    """
    if split:
        found = await decompose(question)
        if found.compound:
            return await _run_parts(conn, question, found.parts,
                                    use_agent=use_agent, case_id=case_id,
                                    ledger=ledger)
        if not found.available:
            # The splitter could not run. The question is still answered
            # as written, but if it WAS compound this answer covers part
            # of it, and that has to reach the person reading it.
            (ledger or Ledger())  # created below; see the note in _one
            question_unsplit_note = found.reason
        else:
            question_unsplit_note = None
    else:
        question_unsplit_note = None

    return await _one(conn, question, use_agent=use_agent, case_id=case_id,
                      ledger=ledger, unsplit_note=question_unsplit_note)


async def _carry_scope(conn: asyncpg.Connection, question: str,
                       parts: list[str]) -> list[tuple[str, str]]:
    """Put back the scope a split dropped, using bindings rather than words.

    MEASURED, and the reason this function exists:

        "لاہور میں کتنے مقدمات ہیں؟ اور ان میں سے کتنے میں ہتھیار برآمد ہوا؟"
          [1] لاہور میں کتنے مقدمات ہیں؟            <- in Lahore
          [2] ان میں سے کتنے میں ہتھیار برآمد ہوا؟  <- "of those"

    Part 2 is not standalone. Run as written it counts weapons across
    every case, and the answer is then displayed under a question about
    Lahore. Both figures pass every check inside their own part; the
    wrongness exists only in the relation between them.

    The same split in English carried "in Lahore" into part 2 correctly.
    So the prompt is not the fix — it already asks for this, and a model
    that obeys in one language and not the other cannot be relied on for
    either.

    What is reliable is the binder. A term that bound to a stored VALUE is
    a scope: لاہور is not a word here, it is a row in `places`.

    TWO RULES, BOTH MEASURED INTO EXISTENCE

    Scope moves FORWARD only. A value named in part 1 still applies in
    part 2; a value named in part 2 does not reach back into part 1. The
    first version had no direction and pushed برآمد — part 2's actual ask
    — backwards into part 1, turning a correct total into a filtered one.

    A weak match is not a scope. 'and' bound cross-script at 0.375 to
    'اندراج' and was carried into every part. Romanisation is lossy, so a
    low cross-script score is a coincidence, and only an exact stored
    value or a strong cross-script match may narrow another part.

    A part that names its OWN value on that column is left alone — "how
    many in Lahore and how many in Karachi" has each part setting its own
    scope, and overwriting either would be this same bug in reverse.

    Returns (what to show, what to run). They differ only where a part was
    repaired, so the person still reads the question they asked.
    """
    def is_scope(b: Binding) -> bool:
        """Whether this binding restricts WHICH ROWS, or WHAT HAPPENED.

        Only the first kind carries between parts, and the schema already
        separates them: a DIMENSION says which slice of the world is being
        talked about, a FACT says what occurred in it. لاہور is a row in
        `places`, a dimension — it still applies two clauses later. 'weapon'
        is a fact, and it is part 2's actual question, not a frame around
        part 3's.

        Measured, before this line existed: "total cases, cases with a
        weapon, and theft separately" carried 'weapon' into the theft part
        and reported thefts-with-a-weapon as thefts.
        """
        if not b.column or b.table_role != "dimension":
            return False
        if b.is_exact_value:
            return True
        # Romanisation is lossy, so a weak cross-script hit is coincidence,
        # not scope. 'and' bound to 'اندراج' at 0.375 and narrowed every part.
        return b.match_kind == "cross_script" and b.score >= 0.6

    bound: list[list[Binding]] = []
    for text in parts:
        try:
            bound.append(_best_per_term(await bind(conn, text)))
        except Exception:                               # noqa: BLE001
            bound.append([])

    out: list[tuple[str, str]] = []
    for i, text in enumerate(parts):
        constrained = {b.column for b in bound[i]
                       if b.match_kind in ("value", "cross_script")}
        carry: list[str] = []
        for earlier in bound[:i]:
            for b in earlier:
                if is_scope(b) and b.column not in constrained:
                    constrained.add(b.column)
                    carry.append(b.term)
        # Appended, not rewritten. The binder reads terms, not grammar, so
        # the term alone restores the filter — and rewriting the sentence
        # would hand the model back the decision this takes away from it.
        out.append((text, text + " " + " ".join(carry[:2]) if carry else text))
    return out


async def _run_parts(conn: asyncpg.Connection, question: str,
                     parts: list[str], use_agent: bool,
                     case_id: str | None,
                     ledger: Ledger | None) -> dict:
    """Give every ask its own full pipeline, then reconcile the results.

    Each part is a complete question to the layer below: its own binding,
    its own join discovery, its own guards, its own validator. Nothing
    about a part knows it is part of something larger. That is what makes
    this scale — a new kind of compound question needs no new code here,
    because the thing being composed is the whole engine.

    Parts run one after another, on the caller's connection. They are
    independent and could run concurrently, but the agent path holds a
    READ ONLY transaction for the length of a part, so concurrency means
    a connection each — a second failure surface, bought before this one
    is proven. Correct first. The loop below becomes a gather when it is.
    """
    ledger = ledger if ledger is not None else Ledger()

    scoped = await _carry_scope(conn, question, parts)

    results: list[dict] = []
    for text, effective in scoped:
        try:
            results.append(await _one(conn, effective, use_agent=use_agent,
                                      case_id=case_id, ledger=ledger))
        except Exception as exc:                        # noqa: BLE001
            # One part failing is one part failing. The others stand, and
            # this one says what happened instead of vanishing.
            results.append({"question": text, "answer": None,
                            "refusal": f"this part failed: {exc}"[:200]})

    # Cross-verify before composing. Parts of one question are related --
    # a subset cannot exceed its superset -- and checking that is the only
    # thing this layer knows that the parts cannot know alone.
    conflicts = crosscheck(list(zip(parts, results)))

    answered = [r for r in results if r.get("answer") is not None]

    lines: list[str] = []
    for text, res in zip(parts, results):
        got = res.get("answer")
        if got is None:
            why = res.get("refusal") or res.get("system_message") or (
                "could not be answered from the case data")
            lines.append(text + "\n  Not answered \u2014 " + str(why))
        else:
            lines.append(text + "\n  " + _text(got))

    out: dict = {
        "question": question,
        "parts": parts,
        "part_results": results,
        "plan": results[0].get("plan") if results else None,
        "reading": results[0].get("reading") if results else None,
        "sql": None,
        "ledger": ledger,
        "conflicts": conflicts,
        # Every query from every part, so the composed answer stays
        # checkable line by line rather than only in aggregate.
        "queries": [q for r in results for q in (r.get("queries") or [])],
        "passages": [p for r in results for p in (r.get("passages") or [])],
        "escalated": [e for r in results for e in (r.get("escalated") or [])],
    }

    if not answered:
        out["answer"] = None
        out["refusal"] = (
            "None of the " + str(len(parts)) + " things you asked could be "
            "answered from the case data.\n\n" + "\n\n".join(lines))
        return _finalise(out, ledger)

    if conflicts:
        # Figures that contradict each other are not publishable as a set.
        # Publishing them with a footnote would leave the reader to spot
        # the contradiction, which is the whole job this layer has.
        out["answer"] = None
        out["refusal"] = (
            "These figures contradict each other, so I am not reporting "
            "them as if they were consistent:\n\n" + "\n\n".join(lines)
            + "\n\n" + "\n".join("  \u2022 " + c for c in conflicts))
        return _finalise(out, ledger)

    out["answer"] = "\n\n".join(lines)
    if len(answered) < len(parts):
        # Partial success is stated, never implied by absence. An answer
        # listing two figures when three were asked for reads as complete.
        out["answer_caveat"] = (
            str(len(answered)) + " of " + str(len(parts)) + " parts "
            "answered; the rest are marked above with what was missing.")
    return _finalise(out, ledger)


def _text(answer) -> str:
    """One part's answer as a line, whatever shape the path returned."""
    if isinstance(answer, str):
        return answer
    if isinstance(answer, dict):
        if len(answer) == 1:
            return str(next(iter(answer.values())))
        return "; ".join(f"{k}: {v}" for k, v in answer.items())
    return str(answer)


async def _one(conn: asyncpg.Connection, question: str,
               use_agent: bool = True,
               case_id: str | None = None,
               ledger: Ledger | None = None,
               unsplit_note: str | None = None) -> dict:
    # The enhancer grounds the question against real data FIRST. Its result
    # is what classifies the question — there is no separate classifier
    # guessing from the words alone, because grounding already answered
    # what the question is about.
    # One ledger per question. Every component that cannot do its job
    # records the fact here, and the user is told at the end. Nothing
    # degrades silently — a quietly worse answer is less useful than an
    # error message, because an error message is actionable.
    # Shared when this is one part of a compound question, so the health
    # of the whole answer is the health of all its parts.
    ledger = ledger if ledger is not None else Ledger()
    if unsplit_note:
        ledger.record(
            "question splitting", Severity.IMPORTANT, unsplit_note,
            "if you asked for more than one figure, I answered this as a "
            "single question — check that every part you asked about is "
            "covered below")

    enhanced = await enhance(conn, question)
    plan = await build_plan(conn, question)

    # Interpretation runs on every data question, and it is NOT overhead.
    #
    # I removed it as a latency optimisation and the benchmark broke: "how
    # many cases are registered" went from 73 in 98s to a refusal in 163s.
    # Interpretation is what tells the agent that "registered" DESCRIBES
    # cases rather than filtering them; without it the agent hunts a
    # registration marker, finds none, and spends every turn failing.
    #
    # Removing it did not even save time — the agent burned more turns
    # without the reading than the reading itself costs. A step that looks
    # like overhead may be the thing preventing a much larger cost.
    out_reading = None
    if plan.route is Route.DATA:
        out_reading = await interpret(conn, question)
    out: dict = {"question": question, "plan": plan, "sql": None,
                 "answer": None, "error": None, "enhanced": enhanced,
                 "ledger": ledger, "reading": out_reading}

    if plan.route is Route.SMALL_TALK:
        out["answer"] = "conversational — answered from the prompt, no retrieval"
        return _finalise(out, ledger)
    if plan.route is Route.WORLD_KNOWLEDGE:
        out["answer"] = "world knowledge — needs a web lookup, not this corpus"
        return _finalise(out, ledger)

    # Escalation must BLOCK, not annotate. Measured: "how many persons
    # have a criminal risk score above 80" correctly reported that
    # criminal/risk/score never bound — and then ran the SQL anyway and
    # answered 210, the count of every person in the database. The legacy
    # engine answered 208 to the same question. Detecting the problem and
    # answering regardless is not better than not detecting it.
    #
    # Until the planning loop exists, an escalation is a refusal that says
    # what failed.
    # Retrieve passages BEFORE deciding how to answer. They cost one
    # indexed query, and the agent needs them: a question like "describe
    # the narrative in FIR 201/26" is answered FROM text, so sending the
    # agent off without it made it write SQL against description columns
    # and fail. Text is context for every path, not a separate route.
    passages = []
    if Capability.TEXT_SEARCH in plan.capabilities or plan.escalation.required:
        # The corpus narratives are Urdu. An English question searched
        # only in English lands on headers rather than narratives, so the
        # question is also asked in the other language and the rankings
        # are fused. Costs one model call; buys the records back.
        from src.retrieval.bilingual import alternate
        try:
            other = await alternate(question)
        except Exception:                               # noqa: BLE001
            other = ""
        # ── Cases first, the rulebook only if the cases say nothing ────
        #
        # The index holds 712 case chunks and 7,004 chunks of statutes and
        # police rules. The statutes are ENGLISH; the narratives are URDU.
        # MEASURED, "what methods were used to commit fraud" returned five
        # statute passages and zero cases -- correct law, useless answer.
        #
        # Filtered to cases, the same query returns four real PECA 2016
        # bank-fraud FIRs.
        #
        # This is a RETRY, not a classification. Nothing here decides
        # whether a question is "about the law": the cases are searched
        # first because that is what an investigator is asking about, and
        # if they yield nothing the whole index is searched, so a genuine
        # question about a statute still finds it.
        found = await hybrid_search(conn, enhanced.semantic, enhanced.lexical,
                                    case_id=case_id, limit=8, ledger=ledger,
                                    alternate_query=other, cases_only=True)
        if not found.passages:
            found = await hybrid_search(
                conn, enhanced.semantic, enhanced.lexical, case_id=case_id,
                limit=8, ledger=ledger, alternate_query=other,
                cases_only=False)
        passages = found.passages
        out["passages"] = passages
        out["retrieval_note"] = found.note

    # ── A descriptive question is answered from its passages ───────────
    #
    # MEASURED, and the reason this branch exists. Asked "فراڈ کے مقدمات
    # میں ملزمان کیا طریقہ استعمال کرتے ہیں؟" the engine retrieved
    #
    #   [span 3753] ملزم نے مجھے دھوکہ دہی اور فراڈ کے ذریعے مالی...
    #
    # and then REFUSED, reporting that no table holds a list of fraud
    # methods. It found the answer and discarded it.
    #
    # Every guard here enforces "a number comes from SQL or we refuse".
    # That rule is why 208 cannot happen. But it was written for counting,
    # and a question that asks what the records SAY has different
    # evidence: the spans are the evidence. So the refusal test changes
    # with the question -- no span to support a claim, rather than no
    # table.
    #
    # The counting path below is untouched. Nothing about this branch
    # loosens what happens when a figure is requested.
    wants_figure = Capability.SQL_AGGREGATE in plan.capabilities
    if not wants_figure and passages:
        from src.retrieval.describe import describe
        described = await describe(conn, question, passages, ledger=ledger,
                                   counted=False)
        out["description"] = described
        out["answer"] = described.render() if described.answered else None
        out["refusal"] = described.refusal
        # This plan asks for a source description, not a SQL measure. An
        # empty/failed descriptive response is not evidence that sixteen
        # SQL-agent rounds can answer it. Preserve its refusal and sources;
        # a broader source review must be an explicit, bounded operation.
        return _finalise(out, ledger)

    if plan.escalation.required:
        # Escalation hands off; it does not refuse. Measured: "which police
        # station handles the most cases" was refused because the verb
        # "handles" matched no column -- a question the planner had
        # understood, killed by a compiler with no template for it.
        if use_agent:
            from src.retrieval.agent import run_agent
            # ── A layer runs only if it CAN contribute ──────────────
            #
            # Community summaries are derived prose. They are structurally
            # incapable of producing a figure -- that is stated in their
            # own module and enforced in the hint text. So for a question
            # whose plan asks for an AGGREGATE, they cannot contribute by
            # their own definition, and they are not fetched at all.
            #
            # This is not classifying the question. Nothing here reads the
            # words to guess a topic. It matches what a layer can do
            # against what the plan already established the answer needs.
            #
            # MEASURED, and the reason the check exists: "How many cases
            # are from Lahore?" answered 18 for weeks. The day pattern
            # summaries were added to every escalated question's context,
            # it answered 0 -- roughly a thousand characters of cluster
            # prose naming Lahore stations, handed to an agent that needed
            # a COUNT and had to ignore all of it. More context is not
            # free, and context a question cannot use is a distraction
            # with a cost.
            communities = ""
            wants_figure = Capability.SQL_AGGREGATE in plan.capabilities
            if not wants_figure:
                from src.retrieval.community import (
                    as_hint as community_hint, search as community_search,
                )
                try:
                    c_hits, c_cov = await community_search(
                        conn, enhanced.semantic, ledger=ledger)
                    communities = community_hint(c_hits, c_cov)
                except Exception:                       # noqa: BLE001
                    # A missing pattern index must not take down a
                    # question that never needed one.
                    communities = ""
            hints = await _hints(conn, plan, passages, out_reading,
                                 communities)
            res = await run_agent(conn, question, hints=hints, ledger=ledger)
            out["agent"] = res
            out["escalated"] = plan.escalation.reasons()
            out["refusal"] = res.refusal
            out["queries"] = res.evidence()

            if res.answered:
                from src.retrieval.validator import validate
                verdict = await validate(
                    conn, res.text, res.evidence(),
                    res.query_log.results, plan.coverage_notes,
                    observed=res.query_log.observed)
                out["verdict"] = verdict
                out["answer"] = (verdict.render(res.text)
                                 if verdict.publishable else None)
                if not verdict.publishable:
                    out["refusal"] = verdict.render(res.text)
            else:
                out["answer"] = None
            return _finalise(out, ledger)

        plan.refusal = "; ".join(plan.escalation.reasons())
        out["answer"] = None
        out["refusal"] = plan.refusal
        return out

    pk_map = await _primary_keys(conn, plan.tables)
    sql = compile_sql(plan, pk_map)
    out["sql"] = sql

    if sql is not None:
        try:
            row = await conn.fetchrow(sql)
            out["answer"] = dict(row) if row else None
        except Exception as exc:                        # noqa: BLE001
            # A plan that will not run is reported as such. Emitting a
            # number from a failed query, or silently falling back to text,
            # is how a wrong answer acquires confidence.
            out["error"] = str(exc)[:300]

    # Text retrieval runs alongside SQL rather than instead of it. They
    # answer different things: SQL gives the number, passages give the
    # narrative and the citation, and a question often needs both. The
    # deployed router picked ONE route and lost the other.
    if out["answer"] is None and passages:
        out["answer"] = {
            "passages": len(passages),
            "top": passages[0].brief(160),
            "span_id": passages[0].span_id,
        }
    return _finalise(out, ledger)


def _finalise(out: dict, ledger: Ledger) -> dict:
    """Attach the health verdict, and suppress the answer if it is unusable.

    An essential component being down does not produce a worse answer — it
    produces no answer. Returning one anyway, with a quiet caveat, is the
    blind spot this whole layer exists to remove.
    """
    out["health"] = ledger.as_dict()          # for logs and operators
    # A number answered from SQL is not weakened by a document-search
    # outage. Telling the user otherwise attaches a warning to a correct
    # answer, which is how warnings stop being read.
    # An answer is "number" if it came from SQL — whether the planner
    # compiled that SQL or the agent wrote it. The first version only
    # checked out["sql"], which the agent path never sets, so every agent
    # answer was classed as text and carried a document-search caveat it
    # had never relied on.
    agent = out.get("agent")
    ran_queries = bool(agent and agent.evidence())
    answer_kind = "number" if (out.get("sql") or ran_queries) else "text"
    message = ledger.user_message(answer_kind)
    out["system_message"] = message

    if not ledger.usable:
        out["answer"] = None
        out["refusal"] = message
    elif message and out.get("answer") is not None:
        out["answer_caveat"] = message
    return out
