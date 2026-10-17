"""Guardrails enforced in code, because a prompt rule is only a request.

WHY

The agent runs on a Q4-quantized local model. It will ignore instructions,
and it will do so confidently. Every rule that matters was written into the
system prompt and then measured being broken:

  "look before refusing"          -> refused on turn 1 with zero tool calls
  "check a value exists first"    -> filtered on an English guess against
                                     Urdu data, which is the failure that
                                     started this rebuild
  "count(DISTINCT pk) when joining" -> counted a join product: 227 where
                                     the truth was 80

A rule stated in a prompt is a request. A rule checked in the loop is a
boundary. These are the boundaries.

THE SHAPE

Each guard inspects an agent action BEFORE it takes effect and either
allows it or returns a correction naming what to do instead. A correction
is far more useful than a rejection: the model gets a specific, actionable
next step, which a quantized model can follow even when it cannot follow
an abstract instruction.

Guards never silently rewrite the agent's SQL. Rewriting would hide the
error and teach it nothing; the next question would fail the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import asyncpg


@dataclass
class GuardResult:
    allowed: bool
    correction: str | None = None
    warning: str | None = None


_LITERAL_EQ = re.compile(
    r"(\w+)\s*\.\s*(\w+)\s*(?:=|ILIKE|LIKE)\s*'([^']{2,80})'", re.I)
_BARE_EQ = re.compile(r"(?<![\w.])(\w+)\s*(?:=|ILIKE|LIKE)\s*'([^']{2,80})'", re.I)
_JOIN = re.compile(r"\bjoin\b", re.I)
_AGG = re.compile(r"\b(count|sum|avg)\s*\(", re.I)
_COUNT_DISTINCT = re.compile(r"\bcount\s*\(\s*distinct\b", re.I)
_COUNT_STAR = re.compile(r"\bcount\s*\(\s*\*\s*\)", re.I)


async def check_literals(conn: asyncpg.Connection,
                         sql: str) -> GuardResult:
    """Every literal a query filters on must exist in the column it filters.

    This is the structural cure for the failure that began the rebuild: a
    model filtered license_status = 'unlicensed' against data holding
    'بغیر لائسنس' and returned 0, which reads as an answer. No prompt rule
    prevents that reliably. Checking does.

    A filter on a value that does not exist is not run. The agent is told
    which values DO exist, which is a correction it can act on.
    """
    known = {r["table_name"] for r in
             await conn.fetch("SELECT DISTINCT table_name FROM column_catalog")}
    aliases = _alias_map(sql)

    pairs: list[tuple[str, str, str]] = []
    for m in _LITERAL_EQ.finditer(sql):
        pairs.append((m.group(1), m.group(2), m.group(3)))

    # Bare column references, with no table prefix. These slipped past
    # entirely: `WHERE role_id = 'accused'` was never checked, and role_id
    # is a resolved-key column that is always null — the real values live
    # in role_raw, in Urdu. The query returned nothing for a reason that
    # had nothing to do with the question.
    #
    # Resolvable only when the query reads ONE table; with a join the
    # column is ambiguous and guessing which table owns it would be worse
    # than not checking.
    tables_in_query = sorted(set(aliases.values()))
    if len(tables_in_query) == 1:
        owner = tables_in_query[0]
        already = {(t, c) for t, c, _ in pairs}
        for m in _BARE_EQ.finditer(sql):
            col, literal = m.group(1), m.group(2)
            if (owner, col) in already:
                continue
            if await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM column_catalog"
                    " WHERE table_name = $1 AND column_name = $2)",
                    owner, col):
                pairs.append((owner, col, literal))

    if not pairs:
        return GuardResult(True)

    for tbl, col, literal in pairs:
        table = aliases.get(tbl.lower(), tbl)
        if table not in known:
            continue
        # Wildcards mean containment, which is a different question.
        if "%" in literal or "_" in literal and "ILIKE" in sql.upper():
            continue
        try:
            exists = await conn.fetchval(
                f'SELECT EXISTS (SELECT 1 FROM "{table}" '
                f' WHERE "{col}"::text = $1)', literal)
        except Exception:                               # noqa: BLE001
            continue
        if exists:
            continue

        try:
            samples = await conn.fetch(
                f'SELECT DISTINCT "{col}"::text AS v FROM "{table}" '
                f' WHERE "{col}" IS NOT NULL LIMIT 12')
        except Exception:                               # noqa: BLE001
            samples = []
        have = ", ".join(repr(r["v"]) for r in samples) or "(no values)"

        # A `_id` column that is empty usually has a `_raw` sibling holding
        # what the document actually said. Naming it turns a rejection into
        # a usable next step.
        sibling = None
        if col.endswith("_id"):
            cand = col[:-3] + "_raw"
            if await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM column_catalog"
                    " WHERE table_name = $1 AND column_name = $2)",
                    table, cand):
                sibling = cand

        hint = ""
        if sibling:
            try:
                vals = await conn.fetch(
                    f'SELECT DISTINCT "{sibling}"::text AS v FROM "{table}"'
                    f' WHERE "{sibling}" IS NOT NULL LIMIT 8')
                hint = (f" {table}.{sibling} holds what the documents "
                        f"actually say: "
                        + ", ".join(repr(r["v"]) for r in vals)
                        + ". Filter on that instead.")
            except Exception:                           # noqa: BLE001
                hint = f" Try {table}.{sibling} instead."

        return GuardResult(
            False,
            correction=(
                f"{table}.{col} contains no value equal to {literal!r}, so "
                f"that filter would return zero rows for the wrong reason. "
                f"Values actually present: {have}.{hint} You can also match "
                f"with ILIKE '%...%' for a term inside a field, or call "
                f"find_term to locate the word."))
    return GuardResult(True)


def _alias_map(sql: str) -> dict[str, str]:
    """Resolve `FROM weapons w` / `JOIN places p` so alias.col checks work."""
    out: dict[str, str] = {}
    for m in re.finditer(
            r"\b(?:from|join)\s+(\w+)(?:\s+(?:as\s+)?(\w+))?", sql, re.I):
        table, alias = m.group(1), m.group(2)
        out[table.lower()] = table
        if alias and alias.lower() not in ("on", "where", "join", "left",
                                           "inner", "group", "order"):
            out[alias.lower()] = table
    return out


def check_join_inflation(sql: str) -> GuardResult:
    """An aggregate over a join without DISTINCT counts row pairs.

    Measured: a weapons/case_events join answered 227 where the truth was
    about 80, because a one-to-many join multiplies the base rows and
    count(*) counts the product. This is L4 — the graph's 73 edges for 19
    stations — reappearing in generated SQL.
    """
    if not (_JOIN.search(sql) and _AGG.search(sql)):
        return GuardResult(True)
    if _COUNT_DISTINCT.search(sql):
        return GuardResult(True)
    if _COUNT_STAR.search(sql):
        return GuardResult(
            False,
            correction=(
                "This query joins and then counts rows with count(*). A "
                "one-to-many join multiplies the base rows, so that counts "
                "row pairs rather than things. Use "
                "count(DISTINCT <base_table>.<primary_key>)."))
    return GuardResult(True, warning=(
        "aggregate over a join without DISTINCT — confirm it counts "
        "entities, not row pairs"))


async def check_temporal(conn: asyncpg.Connection, sql: str) -> GuardResult:
    """A bitemporal fact table counted without valid_to IS NULL.

    Superseded assertions remain in the table by design: a witness who
    later becomes an accused is two rows, both true of their moment.
    Counting both is counting the same person twice.

    A warning rather than a block — an as-of question legitimately reads
    superseded rows, and the guard cannot tell which was meant.
    """
    tables = set(_alias_map(sql).values())
    if not tables:
        return GuardResult(True)
    rows = await conn.fetch(
        "SELECT DISTINCT table_name FROM column_catalog "
        " WHERE column_name = 'valid_to' AND table_name = ANY($1)",
        list(tables))
    temporal = {r["table_name"] for r in rows}
    if not temporal:
        return GuardResult(True)
    if re.search(r"valid_to\s+IS\s+NULL", sql, re.I):
        return GuardResult(True)
    return GuardResult(True, warning=(
        f"{', '.join(sorted(temporal))} keeps superseded assertions. Without "
        f"`valid_to IS NULL` this counts historical versions too. Add it "
        f"unless you deliberately want the history."))


_COL_REF = re.compile(r"(\w+)\s*\.\s*(\w+)")
_PREDICATE_COL = re.compile(
    r"(?:where|and|or)\s+(?:(\w+)\s*\.\s*)?(\w+)\s*"
    r"(?:IS\s+(?:NOT\s+)?NULL|=|<>|!=|>|<|>=|<=|ILIKE|LIKE|IN)", re.I)


async def check_coverage(conn: asyncpg.Connection, sql: str) -> GuardResult:
    """Block a filter on a column that is empty or nearly empty.

    MEASURED: asked how many FIRs are currently registered, the agent ran

        SELECT COUNT(*) FROM rebuild_cases WHERE registered_at IS NOT NULL

    and answered 0. The query is valid, the number is what Postgres
    returned, and it is completely wrong — registered_at is null for every
    one of the 73 cases. The true answer is 73.

    This is predicted failure F7: a filter on a sparse column returns a
    plausible number by discarding the population invisibly. coverage_warning
    already measured this, but it only ran inside the planner and never on
    the agent's own SQL. A check that exists and is not applied is not a
    check.
    """
    aliases = _alias_map(sql)
    seen: set[tuple[str, str]] = set()

    for m in _PREDICATE_COL.finditer(sql):
        alias, col = m.group(1), m.group(2)
        if col.lower() in ("null", "not", "and", "or", "select", "true",
                           "false"):
            continue
        tables = ([aliases[alias.lower()]] if alias
                  and alias.lower() in aliases else list(set(aliases.values())))
        for table in tables:
            if (table, col) in seen:
                continue
            seen.add((table, col))
            try:
                row = await conn.fetchrow(
                    "SELECT must_disclose, null_rate, populated, total,"
                    " message FROM coverage_warning($1, $2)", table, col)
            except Exception:                           # noqa: BLE001
                continue
            if row is None or not row["must_disclose"]:
                continue
            if row["total"] and row["populated"] == 0:
                return GuardResult(
                    False,
                    correction=(
                        f"{table}.{col} is empty for all {row['total']} rows, "
                        f"so filtering on it answers a question about "
                        f"missing data, not about cases. Drop that condition, "
                        f"or use a column that is actually populated — call "
                        f"describe on {table} to see which."))
            if row["null_rate"] and float(row["null_rate"]) > 0.5:
                # Saying a column is sparse is half the help. Saying where
                # the data actually IS turns a caveat into a fix.
                #
                # Measured: filtering case_parties.role_id = 'accused' gave
                # 10, honestly disclosed as covering 17 of 228 rows. The
                # real answer is 125 — role_raw holds what the documents
                # said, in Urdu. The resolved-key column is filled later by
                # a separate pass and is mostly empty by design.
                alt = None
                if col.endswith("_id"):
                    cand = col[:-3] + "_raw"
                    try:
                        better = await conn.fetchrow(
                            "SELECT populated, total FROM"
                            " coverage_warning($1, $2)", table, cand)
                    except Exception:                   # noqa: BLE001
                        better = None
                    if better and (better["populated"] or 0) > (
                            row["populated"] or 0):
                        alt = (f" {table}.{cand} is populated for "
                               f"{better['populated']} of {better['total']} "
                               f"rows — it holds what the documents actually "
                               f"say, and is almost certainly the column you "
                               f"want. Check its values with `values` before "
                               f"filtering.")
                if alt:
                    # BLOCK, not warn. Warned about exactly this, the agent
                    # kept the sparse filter and answered 10 where the truth
                    # is 125 — and dropped the coverage note it had given a
                    # moment earlier. A rule the model can ignore is not a
                    # rule; a query it cannot run is.
                    return GuardResult(
                        False,
                        correction=(
                            f"{table}.{col} is filled for only "
                            f"{row['populated']} of {row['total']} rows, so "
                            f"this filter answers about a small fraction of "
                            f"the data.{alt}"))
                return GuardResult(
                    True,
                    warning=(
                        f"{table}.{col} is recorded for only "
                        f"{row['populated']} of {row['total']} rows. "
                        f"Filtering on it silently excludes the rest. State "
                        f"the coverage in your answer."))
    return GuardResult(True)


_HAS_WHERE = re.compile(r"where", re.I)
_COUNT_ANY = re.compile(r"count\s*\(", re.I)


async def check_selectivity(conn: asyncpg.Connection,
                            sql: str) -> GuardResult:
    """Report what a filter removed, not just what it kept.

    MEASURED: asked how many cases are registered, the agent filtered on a
    registration marker, got 0, and refused — concluding the data could not
    say. The truth is 73: in a police database every FIR is registered, so
    the word describes the population rather than selecting a subset.

    It never learned the baseline. Seeing "73 total, 0 after this filter"
    makes a misread term obvious; seeing "0" alone makes it look like a
    finding.

    So every filtered count is accompanied by its unfiltered count. The
    rule carries no vocabulary — it does not know what "registered" means,
    only that a predicate removing everything deserves a second look. It
    holds for any corpus in any domain.
    """
    if not (_HAS_WHERE.search(sql) and _COUNT_ANY.search(sql)):
        return GuardResult(True)

    tables = sorted(set(_alias_map(sql).values()))
    if len(tables) != 1:
        # With a join, "the population" is ambiguous; the join guard covers
        # that case and guessing here would mislead.
        return GuardResult(True)

    table = tables[0]
    try:
        baseline = await conn.fetchval(f'SELECT count(*) FROM "{table}"')
        filtered = await conn.fetchval(f"SELECT count(*) FROM ({sql}) _s")
    except Exception:                                   # noqa: BLE001
        return GuardResult(True)

    if not baseline:
        # An EMPTY table is exactly when this matters most, and the first
        # version returned "allowed" here without a word.
        #
        # MEASURED: "how many cases are from Lahore" counted from
        # case_station_history, which holds zero rows, and answered 0. The
        # truth is 18. The query was valid, the count was real, and the
        # number was a confident lie — the single output we agreed is
        # unacceptable.
        #
        # This is the empty_table distinction from L15 rebuilt and then
        # discarded by a shortcut: a count over no rows is not zero
        # evidence, it is NO evidence.
        return GuardResult(
            False,
            correction=(
                f"{table} contains no rows at all, so any count from it is "
                f"0 for reasons unrelated to the question. That is not an "
                f"answer. Call schema_overview or describe to find a table "
                f"that actually holds this data, and query that instead."))

    # The count is the query's own result, not the row count of the wrapper.
    try:
        row = await conn.fetchrow(sql)
        kept = next((v for v in (row or {}).values()
                     if isinstance(v, (int, float))), None)
    except Exception:                                   # noqa: BLE001
        kept = None
    if kept is None:
        return GuardResult(True)

    if kept == 0:
        return GuardResult(True, warning=(
            f"This filter matches NONE of the {baseline} rows in {table}. "
            f"Either nothing qualifies, or the term you filtered on "
            f"describes the whole population rather than selecting part of "
            f"it — in which case the answer is {baseline}, not 0. Check "
            f"which before answering."))
    if kept < baseline * 0.05:
        return GuardResult(True, warning=(
            f"This filter keeps {kept} of {baseline} rows in {table} "
            f"({100.0 * kept / baseline:.1f}%). If that seems too few, the "
            f"term may describe the population rather than select a subset. "
            f"Say the coverage in your answer either way."))
    return GuardResult(True, warning=(
        f"population {baseline} in {table}, {kept} after your filter"))


async def check_equality_narrowness(conn: asyncpg.Connection,
                                    sql: str) -> GuardResult:
    """Catch an equality filter that a containment filter would widen.

    MEASURED: `role_raw = 'ملزم'` returned 120 accused. The corpus also
    writes 'نامزد ملزم' and 'ملزمان' — the same role, spelled as the
    document happened to word it. Equality matches one spelling and
    silently drops the rest.

    This is not a fact about Urdu or about roles. Free-text columns hold
    however the source phrased a thing, in any language, and equality on
    them is almost always too narrow. The check is arithmetic: count the
    equality, count the containment, and if containment finds materially
    more then the equality is answering a smaller question than was asked.

    No vocabulary: it does not know which spellings exist, only that more
    of them do.
    """
    tables = sorted(set(_alias_map(sql).values()))
    if len(tables) != 1:
        return GuardResult(True)
    table = tables[0]

    pairs: list[tuple[str, str]] = []
    for m in _LITERAL_EQ.finditer(sql):
        if "=" in m.group(0) and "LIKE" not in m.group(0).upper():
            pairs.append((m.group(2), m.group(3)))
    for m in _BARE_EQ.finditer(sql):
        if "LIKE" not in m.group(0).upper():
            pairs.append((m.group(1), m.group(2)))
    if not pairs:
        return GuardResult(True)

    for col, literal in pairs:
        if not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM column_catalog"
                " WHERE table_name = $1 AND column_name = $2)", table, col):
            continue
        try:
            exact = await conn.fetchval(
                f'SELECT count(*) FROM "{table}" WHERE "{col}"::text = $1',
                literal)
            loose = await conn.fetchval(
                f'SELECT count(*) FROM "{table}"'
                f' WHERE "{col}"::text ILIKE $1', f"%{literal}%")
        except Exception:                               # noqa: BLE001
            continue
        if not exact or not loose or loose <= exact:
            continue
        # Any difference at all. The earlier version required containment to
        # find 10% more, and the real case was 120 vs 125 — a 4% gap that
        # silently dropped five accused. That 1.1 was a number I chose, not
        # one the data supported: if containment finds MORE, equality is
        # provably dropping rows, whatever the ratio.
        if loose > exact:
            try:
                variants = await conn.fetch(
                    f'SELECT DISTINCT "{col}"::text AS v, count(*) AS n'
                    f'  FROM "{table}" WHERE "{col}"::text ILIKE $1'
                    f'   AND "{col}"::text <> $2'
                    f' GROUP BY 1 ORDER BY 2 DESC LIMIT 6',
                    f"%{literal}%", literal)
            except Exception:                           # noqa: BLE001
                variants = []
            others = ", ".join(f"{r['v']!r} ({r['n']})" for r in variants)
            # Containment can over-match too: 'ملزم' appears inside a
            # phrase that might mean the opposite. So the correction offers
            # BOTH ways out — widen with ILIKE, or name the spellings
            # judged equivalent — and shows the variants so the agent can
            # tell which it is. It is not forced into either.
            in_list = ", ".join(f"'{r['v']}'" for r in variants[:5])
            return GuardResult(
                False,
                correction=(
                    f"{table}.{col} = {literal!r} matches {exact} rows, but "
                    f"{loose} rows CONTAIN that term. The source writes it "
                    f"more than one way: {others or 'other phrasings exist'}. "
                    f"Equality silently drops those. Either widen it — "
                    f"{col} ILIKE '%{literal}%' — or, if some of those "
                    f"phrasings mean something different, list the ones you "
                    f"want: {col} IN ('{literal}'"
                    + (f", {in_list}" if in_list else "") + ")."))
    return GuardResult(True)


async def check_empty_tables(conn: asyncpg.Connection,
                             sql: str) -> GuardResult:
    """No query may count from a table that holds no rows.

    This lived inside check_selectivity, which bails out when a query has
    more than one table:

        if len(tables) != 1:
            return GuardResult(True)

    MEASURED: "how many cases are from Lahore" joined case_station_history
    (zero rows) to places, and answered 0. The truth is 18. The join meant
    the empty-table test was never reached — a shortcut written for a
    different purpose silently disabled the check that mattered.

    An empty table poisons a join completely: any join through it yields
    nothing, whatever the other tables hold. So every referenced table is
    checked, joins included, and this runs FIRST because no other finding
    matters if the population is empty.
    """
    tables = sorted(set(_alias_map(sql).values()))
    if not tables:
        return GuardResult(True)

    rows = await conn.fetch(
        "SELECT table_name FROM column_catalog WHERE table_name = ANY($1)"
        " GROUP BY table_name", tables)
    known = [r["table_name"] for r in rows]

    for table in known:
        try:
            n = await conn.fetchval(f'SELECT count(*) FROM "{table}"')
        except Exception:                               # noqa: BLE001
            continue
        if n == 0:
            return GuardResult(
                False,
                correction=(
                    f"{table} contains no rows at all. Any count involving "
                    f"it is 0 for reasons unrelated to the question, and a "
                    f"join through it returns nothing whatever the other "
                    f"tables hold. That is not an answer. Call "
                    f"schema_overview or describe to find a table that "
                    f"actually holds this data."))
    return GuardResult(True)


async def check_all(conn: asyncpg.Connection, sql: str) -> GuardResult:
    """Run every guard. The first block wins; warnings accumulate."""
    warnings: list[str] = []

    # First: nothing else matters if a table in this query is empty.
    r = await check_empty_tables(conn, sql)
    if not r.allowed:
        return r

    r = check_join_inflation(sql)
    if not r.allowed:
        return r
    if r.warning:
        warnings.append(r.warning)

    r = await check_literals(conn, sql)
    if not r.allowed:
        return r

    r = await check_coverage(conn, sql)
    if not r.allowed:
        return r

    r = await check_equality_narrowness(conn, sql)
    if not r.allowed:
        return r
    if r.warning:
        warnings.append(r.warning)

    r = await check_temporal(conn, sql)
    if r.warning:
        warnings.append(r.warning)

    r = await check_selectivity(conn, sql)
    if r.warning:
        warnings.append(r.warning)

    return GuardResult(True, warning="; ".join(warnings) or None)
