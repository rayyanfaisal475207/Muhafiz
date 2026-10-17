"""Stage B — extract facts from document text into the rebuilt schema.

THE CONTRACT IS GENERATED, NOT WRITTEN

The prompt is built at runtime from describe_table(), so the set of fields
the model is asked for IS the schema. Add a column and the next run asks
for it; remove one and it stops. There is no field list in this file to
drift out of date, and no authored vocabulary of roles, weapon types or
event kinds — every value lands raw exactly as the document words it, and
resolution to a key is a separate, re-runnable pass.

NOTHING IS TRUSTED WITHOUT ITS SOURCE

Every fact must come back with `quote`: an EXACT substring of the document.
Before anything is written, the quote is located in the source text. If it
is not found verbatim, the fact is rejected and recorded in the review
queue rather than inserted.

That single rule is what separates this from the system it replaces. A
model that invents a plausible accused produces a row that looks identical
to a real one — unless the row must carry the characters that assert it.
Predicted failure F1 says extraction accuracy becomes the ceiling; this is
the mechanism that makes a miss visible instead of authoritative.

WRITES ARE BITEMPORAL

Each row records asserted_by_doc_id and valid_from, so a later document
that contradicts an earlier one supersedes it rather than overwriting it
(migration 046). A witness in the FIR who is an accused in the challan is
two assertions, both true of their moment.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# The Windows console defaults to cp1252, which cannot encode Urdu. Printing
# a rejected quote then kills the run mid-corpus with UnicodeEncodeError —
# the reporting crashed the work it was reporting on.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.llm.client import call_llm  # noqa: E402

# Tables Stage B populates. Not a vocabulary — a scope. Each name is
# expanded into a full field contract by asking the database, so this list
# says WHICH tables to fill, never what is valid inside them.
TARGET_TABLES = [
    "persons", "case_parties", "weapons", "case_events", "seized_items",
]

# Columns the model must never supply: surrogate keys, provenance and
# bookkeeping are set by this script from what it actually did.
# Which model does the extracting. The local model has no rate limit, so a
# 456-document corpus finishes without the 109 unasked documents the cloud
# run produced. max_tokens stays at or above 2000 because qwen3 spends
# hidden reasoning tokens from the same budget and returns an EMPTY string
# when it runs out (measured: empty at 300 and 500, safe at 1200+).
# provider_override names the CLOUD FALLBACK, not the local model: the
# client is already local-first whenever force_cloud is False. Passing
# "local" here made the fallback resolve to Gemini, which then returned
# 503 — so a local timeout was reported as a cloud outage.
PROVIDER = {"name": None, "force_cloud": False, "max_tokens": 2600}

# MEASURED: the local endpoint serializes. Per-request latency grows with
# concurrency (21.6s at 1, 47.3s at 2, 60.8s at 4) until requests hit the
# 60s timeout — 7 of 8 failed at concurrency 8. Throughput stays flat at
# ~2.8 docs/min however many are in flight, because one GPU is answering
# one request at a time.
#
# So concurrency against a single provider buys nothing. Real parallelism
# comes from using SEVERAL providers at once, each with its own limit:
#
#   local   unlimited calls, ~2.8/min, no quota
#   groq    fast, hard rate limit, 4 rotating keys
#   gemini  ~5 requests/minute per project
#
# Each lane runs at its own pace and pulls from one shared queue, so a
# throttled lane simply takes fewer documents instead of stalling the run.
# Slot counts are MEASURED against this deployment, not guessed.
#
# local: with OLLAMA_NUM_PARALLEL=1 the server serialized — concurrency 2
#   was SLOWER than 1 (2.5 vs 2.8 docs/min) and 8 produced 7 timeouts.
#   Raised to 2 server-side it batches properly:
#       1 -> 2.2/min   2 -> 4.8/min   4 -> 5.0/min   8 -> 7.8/min, 4 errors
#   Two slots is the clean ceiling: 4 is barely faster and its tail
#   (47s) crowds the 60s timeout. Re-measure if the server changes.
#
# gemini is deliberately absent. Measured over 6 documents it wrote ZERO
#   facts while dragging mixed-run quote verification from 100% to 55%.
#   A lane that produces nothing is not extra capacity.
LANES = {
    "local":  {"override": None,   "force_cloud": False, "slots": 2},
    "groq":   {"override": "groq", "force_cloud": True,  "slots": 2},
}

_MACHINE_COLUMNS = {
    "span_id", "asserted_by_doc_id", "asserted_at", "valid_from", "valid_to",
    "superseded_by_doc_id", "created_at", "updated_at", "confidence",
    "case_id", "person_id", "name_skeleton",
}


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        sys.exit("DATABASE_URL is not set")
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def build_contract(conn: asyncpg.Connection) -> str:
    """Turn the live schema into the field contract the model must fill."""
    lines: list[str] = []
    for table in TARGET_TABLES:
        desc = await conn.fetchval("SELECT describe_table($1)", table)
        if not desc:
            continue
        d = json.loads(desc) if isinstance(desc, str) else desc
        purpose = await conn.fetchval(
            "SELECT obj_description($1::regclass, 'pg_class')", table)
        cols = []
        for c in d.get("columns") or []:
            name = c.get("column")
            if name in _MACHINE_COLUMNS or name.endswith("_id") and c.get("primary_key"):
                continue
            if c.get("references"):
                continue
            t = c.get("type", "")
            cols.append(f"{name} ({t})")
        if cols:
            # The table's own COMMENT states what it is FOR. That is
            # documentation already in the database, not an authored
            # vocabulary — and it is what stops a crime-scene address
            # being filed as an event.
            head = f"{table}"
            if purpose:
                head += f" — {' '.join(purpose.split())}"
            lines.append(head + chr(10) + "    columns: " + ", ".join(cols))
    return "\n".join(lines)


SYSTEM = """You extract structured facts from Pakistani police case documents
written in Urdu, English, or both.

Return ONLY a JSON object of this exact shape:

{"facts": [{"table": "<table name>", "quote": "<exact substring of the text>",
            "fields": {"<column>": <value>, ...}}]}

Rules, in order of importance:

1. `quote` MUST be copied character-for-character from the document text.
   It is checked. A fact whose quote is not found verbatim is discarded.
   Quote the shortest span that supports the fact, in its original script.
2. Record values EXACTLY as written. Do not translate, transliterate,
   normalise, or map to a category. If the document says 'بغیر لائسنس',
   that is the value. If it says '30 بور پستول', that is the value.
3. Extract only what the text states. Never infer, complete, or assume.
   Missing is correct; invented is not.
4. Use only the columns listed in the contract. Omit any you cannot fill.
5. For a person, always include their name as written. Include a role only
   if the text states it.
6. A case_parties fact links a PERSON to the case. Always include
   "person_name" in its fields with the name exactly as written, plus
   "role_raw" when the text states the role (accused, witness, complainant,
   مدعی, ملزم, گواہ — whatever word the document uses, unchanged).
7. A case_events fact that happened TO SOMEONE must also include
   "person_name" with that person's name exactly as written. An arrest, a
   bail, a remand happens to a named individual — without the name the
   event cannot answer "who", "how many people", or "what happened to this
   person next". If the text does not name anyone, omit it.

Return the JSON object and nothing else."""


_JSON = re.compile(r"\{.*\}", re.S)


def parse_facts(raw: str) -> list[dict]:
    m = _JSON.search(raw or "")
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    facts = obj.get("facts")
    return facts if isinstance(facts, list) else []


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """Find the quote in the source, exactly, then forgiving whitespace only.

    Whitespace is the one difference allowed, because a model reflowing a
    long Urdu line is a formatting artefact and not a changed claim. Any
    other difference means the quote was not in the document.
    """
    if not quote:
        return None
    i = text.find(quote)
    if i >= 0:
        return i, i + len(quote)

    pattern = re.escape(quote.strip())
    pattern = re.sub(r"(\\\s)+", r"\\s+", pattern)
    m = re.search(pattern, text)
    return (m.start(), m.end()) if m else None


async def _extract_document(
    pool, sem, d, case_id: str, contract: str, dry_run: bool,
    lane: dict | None = None,
) -> dict:
    """One document: model call, quote verification, writes.

    Model latency dominates — roughly 456 calls for the corpus — so these
    run concurrently. Each task takes its OWN pooled connection, because a
    single asyncpg connection cannot be shared across concurrent coroutines.
    """
    lane = lane or {"override": PROVIDER["name"],
                    "force_cloud": PROVIDER["force_cloud"]}
    out = {"proposed": 0, "verified": 0, "rejected": 0, "written": 0,
           "unavailable": 0, "rejects": []}
    text = d["full_text"]
    user = (
        f"Document role: {d['doc_role_raw']}\n"
        f"Field contract:\n{contract}\n\n"
        f"Document text:\n{text}"
    )
    # A rate limit is NOT a rejection — it means the document was never
    # asked about. Measured on the first full run: 109 of 456 documents
    # failed this way, and 23 cases recorded zero facts as a result. Read
    # without care that says "these cases have nothing in them", which is
    # a confident wrong answer about the corpus.
    #
    # So rate limits are retried with backoff, and only a genuine failure
    # is recorded. Key rotation alone does not help: the keys share the
    # burst, so waiting is what actually recovers.
    raw = None
    last_error = None
    for attempt in range(5):
        async with sem:
            try:
                raw = await call_llm(SYSTEM, user, temperature=0.0,
                                     max_tokens=PROVIDER["max_tokens"],
                                     provider_override=lane["override"],
                                     force_cloud=lane["force_cloud"])
                break
            except Exception as exc:                  # noqa: BLE001
                last_error = exc
                msg = str(exc).lower()
                if "rate limit" not in msg and "429" not in msg:
                    break
        await asyncio.sleep(min(2 ** attempt * 5, 60))

    if raw is None:
        out["rejects"].append({"doc": d["doc_id"],
                               "error": f"unavailable: {last_error}"[:160]})
        out["unavailable"] = 1
        return out

    async with pool.acquire() as conn:
        for fact in parse_facts(raw):
            out["proposed"] += 1
            table = fact.get("table")
            quote = fact.get("quote") or ""
            fields = fact.get("fields") or {}
            if table not in TARGET_TABLES:
                out["rejected"] += 1
                out["rejects"].append({"doc": d["doc_id"],
                                       "reason": f"unknown table {table!r}"})
                continue
            loc = locate(quote, text)
            if not loc:
                out["rejected"] += 1
                out["rejects"].append({"doc": d["doc_id"], "table": table,
                                       "reason": "quote not in source",
                                       "quote": quote[:70]})
                continue
            out["verified"] += 1
            if dry_run:
                continue
            try:
                span_id = await conn.fetchval(
                    """
                    INSERT INTO doc_spans (doc_id, element_id, char_start,
                                           char_end, text)
                    SELECT $1, e.element_id, $2, $3, $4
                    FROM doc_elements e
                    WHERE e.doc_id = $1 AND e.element_type='document'
                    RETURNING span_id
                    """,
                    d["doc_id"], loc[0], loc[1], text[loc[0]:loc[1]],
                )
                if await write_fact(conn, case_id, d["doc_id"], table,
                                    fields, span_id):
                    out["written"] += 1
            except Exception as exc:                  # noqa: BLE001
                out["rejected"] += 1
                out["rejects"].append({"doc": d["doc_id"], "table": table,
                                       "reason": f"write failed: {exc}"[:150]})
    return out


async def extract_case(
    conn: asyncpg.Connection, case_id: str, contract: str, dry_run: bool,
    pool=None, sem=None, only_gaps: bool = False,
) -> dict:
    # Deduplication happens AFTER the model call, so a plain re-run pays
    # full price for every document already extracted. With 456 documents
    # at ~12s that is hours of work to fill a 24% gap.
    #
    # A document that has already asserted a fact is done. One that has
    # not is either genuinely empty or was never asked — indistinguishable
    # from here, and both are worth one attempt.
    gap_filter = """
        AND NOT EXISTS (
            SELECT 1 FROM case_events    f WHERE f.asserted_by_doc_id = e.doc_id
            UNION ALL
            SELECT 1 FROM case_parties   f WHERE f.asserted_by_doc_id = e.doc_id
            UNION ALL
            SELECT 1 FROM weapons        f WHERE f.asserted_by_doc_id = e.doc_id
            UNION ALL
            SELECT 1 FROM seized_items   f WHERE f.asserted_by_doc_id = e.doc_id
        )
    """ if only_gaps else ""

    docs = await conn.fetch(
        f"""
        SELECT e.doc_id, e.text AS full_text, cd.doc_role_raw, cd.sequence_no
        FROM doc_elements e
        JOIN case_documents cd ON cd.doc_id = e.doc_id
        WHERE cd.case_id = $1 AND e.element_type = 'document'
          AND e.text IS NOT NULL
          {gap_filter}
        ORDER BY cd.sequence_no NULLS FIRST, e.doc_id
        """,
        case_id,
    )
    stats = {"documents": len(docs), "proposed": 0, "verified": 0,
             "rejected": 0, "written": 0, "unavailable": 0, "rejects": []}

    results = await asyncio.gather(*[
        _extract_document(pool, sem, d, case_id, contract, dry_run)
        for d in docs
    ])
    for r in results:
        for k in ("proposed", "verified", "rejected", "written", "unavailable"):
            stats[k] += r[k]
        stats["rejects"].extend(r["rejects"])

    return stats


async def write_fact(
    conn: asyncpg.Connection, case_id: str, doc_id: str,
    table: str, fields: dict, span_id: int,
) -> bool:
    """Insert one verified fact, keeping only columns the table really has.

    Column filtering is done against the live catalog rather than a list
    here, so a model naming a plausible-but-absent column is dropped
    instead of aborting the run.
    """
    catalog = {r["column_name"]: r["data_type"] for r in await conn.fetch(
        "SELECT column_name, data_type FROM column_catalog WHERE table_name = $1",
        table)}
    real = set(catalog)
    usable = {}
    for k, v in fields.items():
        if k not in real or k in _MACHINE_COLUMNS or v in (None, ""):
            continue
        coerced = _coerce(v, catalog[k])
        if coerced is not _REJECT:
            usable[k] = coerced
    if table == "case_parties" and not usable and fields.get("person_name"):
        usable = {}          # a party with only a name is still a real link
    need = required_column(catalog)
    if need and not str(usable.get(need, "")).strip():
        # e.g. an event with no event_type_raw. Previously these were
        # written because description_ur alone satisfied the weaker gate,
        # producing rows that look real and answer nothing.
        return False
    if table == "case_parties":
        pass                      # a named party is substantive by itself
    elif not usable or not _is_substantive(usable, catalog):
        return False

    if table == "persons":
        await _upsert_person(conn, usable, span_id)
        return True

    if table in ("case_parties", "case_events"):
        # The model names the person; the link is made here. person_name is
        # not a column — it is the join key the model can actually express.
        #
        # case_events was missing from this list, and the cost was total:
        # every one of the 17 arrest events had person_id NULL, so the
        # per-person pathway model from migration 040 — arrested, then
        # bailed, then prosecuted — could not answer anything at all.
        # person_id is a machine column, so the model was never asked for
        # it and never volunteered it.
        name = (fields.get("person_name") or fields.get("canonical_name_ur")
                or fields.get("name") or "").strip()
        if not name:
            # A party with no person is meaningless; an event without one is
            # still a real event that happened to the case.
            if table == "case_events":
                pass
            else:
                return False
        # Measured failure: the model returned 'ملزم' (accused) and 'مدعیہ'
        # (complainant) as NAMES. Those are the roles, quoted from text that
        # names no one. A row like that makes the WORD "accused" a person
        # and inflates every count of people.
        #
        # Detected structurally, with no list of role words: a name that is
        # the same string as its own role is not a name. Comparison is on
        # the normalised skeleton so script variants do not slip through.
        role = str(usable.get("role_raw") or fields.get("role_raw") or "").strip()
        if name and role:
            same = await conn.fetchval(
                "SELECT normalize_name($1) = normalize_name($2)", name, role)
            if same:
                return False
            # Also reject a name that is merely contained in the role phrase
            # ('مدعیہ اور گواہ' naming 'مدعیہ'), which is the same error.
            contained = await conn.fetchval(
                "SELECT position(normalize_name($1) in normalize_name($2)) > 0"
                " AND length(normalize_name($1)) > 0", name, role)
            if contained:
                return False
        if name:
            person_id = await _upsert_person(
                conn, {"canonical_name_ur": name}, span_id)
            usable["person_id"] = person_id

    # Deduplicate on IDENTITY, not on every field.
    #
    # MEASURED: 145 weapon rows for 81 distinct (case, weapon type) pairs —
    # fir-430-26 held five identical 'پستول' entries. The old check required
    # every field to match, and a non-deterministic extractor produces the
    # same weapon with quantity present on one pass and absent on the next.
    # Neither row equalled the other, so both were written, and every
    # re-extraction inflated every count.
    #
    # The identity of a fact is its case plus the value that NAMES it —
    # the same `required_column` the quality gate already derives from the
    # schema's `*_type_raw` convention. Extra detail found later fills
    # blanks on the existing row instead of creating a rival.
    # Identity comes from the schema, not from this function guessing.
    # Passing the wrong column once merged 170 distinct people into 78
    # rows, because case_parties was keyed on role_raw and three accused
    # in one case all read 'ملزم'.
    ident_col = await conn.fetchval(
        "SELECT (fact_identity_columns($1))[1]", table)

    if ident_col and usable.get(ident_col) is not None:
        existing = await conn.fetchrow(
            f"SELECT * FROM {table} WHERE case_id = $1 AND {ident_col} = $2"
            f" LIMIT 1", case_id, usable[ident_col])
        if existing:
            pk = await conn.fetchval(
                "SELECT column_name FROM column_catalog"
                " WHERE table_name = $1 AND is_primary_key LIMIT 1", table)
            filled = 0
            if pk:
                for col, val in usable.items():
                    if col == ident_col or val in (None, ""):
                        continue
                    if existing.get(col) is None:
                        await conn.execute(
                            f"UPDATE {table} SET {col} = $1 WHERE {pk} = $2",
                            val, existing[pk])
                        filled += 1
            # The row already exists; this pass either enriched it or added
            # nothing. Either way it is not a new fact.
            return False

    cols = list(usable) + ["case_id", "span_id", "asserted_by_doc_id", "asserted_at"]
    vals = list(usable.values()) + [case_id, span_id, doc_id]
    ph = ", ".join(f"${i+1}" for i in range(len(vals))) + ", now()"
    await conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})", *vals)
    return True


_REJECT = object()


def required_column(catalog: dict) -> str | None:
    """The column that must be filled for a row in this table to mean anything.

    Derived from the schema's own naming convention rather than a per-table
    list: every table stores the document's wording in a `*_raw` column, and
    a `*_type_raw` column is what NAMES the thing. So:

        case_events  -> event_type_raw    (what happened)
        weapons      -> weapon_type_raw   (what kind of weapon)
        case_parties -> role_raw          (what role)

    A table with no `_raw` column falls back to needing any free text.
    Adding a table that follows the convention needs no edit here.
    """
    raws = [c for c in catalog if c.endswith("_raw")]
    typed = [c for c in raws if c.endswith("_type_raw")]
    if typed:
        return sorted(typed)[0]
    if len(raws) == 1:
        return raws[0]
    return None


def _is_substantive(usable: dict, catalog: dict) -> bool:
    """A fact must say WHAT it is, not only when or how many.

    Measured need: 13 case_events rows were written with event_type_raw
    empty, carrying only a date or a stray description. An untyped event
    cannot answer a pathway question — it is a row that looks real and
    says nothing.

    The rule is derived, not per-table: at least one filled column must be
    free text that is not a key. Dates, counts and booleans describe a
    fact; they cannot BE one.
    """
    for col, val in usable.items():
        t = (catalog.get(col) or "").lower()
        if t.startswith(("text", "character")) and not col.endswith("_id"):
            if str(val).strip():
                return True
    return False


def _coerce(value, pg_type: str):
    """Fit a model-supplied value to the column's declared type.

    Driven by the catalog rather than by a list of known columns, so a new
    timestamp or integer column is handled without an edit here. A value
    that cannot be fitted is REJECTED rather than coerced to something
    plausible — writing 0 for an unparseable age would be a quiet fiction.
    """
    t = (pg_type or "").lower()
    try:
        if t.startswith("timestamp"):
            if isinstance(value, datetime):
                return value
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if t == "date":
            if isinstance(value, date):
                return value
            return date.fromisoformat(str(value)[:10])
        if t in ("integer", "bigint", "smallint"):
            return int(str(value).strip())
        if t == "numeric":
            return float(str(value).strip())
        if t == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("true", "yes", "1")
        return str(value)
    except (ValueError, TypeError):
        return _REJECT


async def _upsert_person(
    conn: asyncpg.Connection, fields: dict, span_id: int
) -> str:
    """Create a person row keyed to the MENTION, not to the name.

    person_id is content-addressed on the span plus the name, so re-running
    the same document produces the same id and does not duplicate. Two
    people who happen to share a name stay separate rows on purpose:
    merging them is identity resolution, which needs confirmation and lives
    in person_identity_links. Predicted failure F3 is a wrong merge
    corrupting every count that touches that person — so nothing here
    merges on a name.
    """
    name = (fields.get("canonical_name_ur") or fields.get("canonical_name_en")
            or "").strip()
    cnic = str(fields.get("cnic") or "").strip()

    # CNIC is a national unique identifier, and the schema enforces that with
    # uq_persons_cnic. So two mentions carrying the same CNIC are the same
    # person, and reusing the row is exact resolution rather than a guess.
    # This is the ONE auto-merge that is safe; merging on a shared name is
    # predicted failure F3 and is never done here.
    if cnic:
        existing = await conn.fetchval(
            "SELECT person_id FROM persons WHERE cnic = $1", cnic)
        if existing:
            # Later mentions often carry detail the first lacked (a father
            # name, a date of birth). Fill only what is still missing; never
            # overwrite, because the earlier assertion is not wrong.
            for col in ("canonical_name_ur", "canonical_name_en",
                        "father_name_ur", "gender_id", "date_of_birth"):
                if fields.get(col):
                    await conn.execute(
                        f"UPDATE persons SET {col} = $1"
                        f" WHERE person_id = $2 AND {col} IS NULL",
                        fields[col], existing)
            return existing

    digest = hashlib.sha256(
        f"{span_id}|{name}|{cnic}".encode("utf-8")).hexdigest()[:16]
    person_id = f"p-{digest}"

    cols = ["person_id", "span_id"] + list(fields)
    vals = [person_id, span_id] + list(fields.values())
    ph = ", ".join(f"${i+1}" for i in range(len(vals)))
    await conn.execute(
        f"INSERT INTO persons ({', '.join(cols)}) VALUES ({ph})"
        " ON CONFLICT (person_id) DO NOTHING",
        *vals,
    )
    return person_id


async def run_corpus(pool, contract: str, cases: list[str], dry_run: bool,
                     only_gaps: bool, lanes: dict) -> dict:
    """One shared queue of documents, several provider lanes draining it.

    Not case-by-case: a per-case gather makes every lane wait for the
    slowest document in that case before any of them starts the next one.
    A single corpus-wide queue keeps every lane busy, and a throttled lane
    simply takes fewer documents rather than holding up the run.
    """
    gap_filter = """
        AND NOT EXISTS (
            SELECT 1 FROM case_events    f WHERE f.asserted_by_doc_id = e.doc_id
            UNION ALL
            SELECT 1 FROM case_parties   f WHERE f.asserted_by_doc_id = e.doc_id
            UNION ALL
            SELECT 1 FROM weapons        f WHERE f.asserted_by_doc_id = e.doc_id
            UNION ALL
            SELECT 1 FROM seized_items   f WHERE f.asserted_by_doc_id = e.doc_id
        )
    """ if only_gaps else ""

    async with pool.acquire() as conn:
        docs = await conn.fetch(
            f"""
            SELECT cd.case_id, e.doc_id, e.text AS full_text,
                   cd.doc_role_raw, cd.sequence_no
            FROM doc_elements e
            JOIN case_documents cd ON cd.doc_id = e.doc_id
            WHERE cd.case_id = ANY($1) AND e.element_type = 'document'
              AND e.text IS NOT NULL
              {gap_filter}
            ORDER BY cd.case_id, cd.sequence_no NULLS FIRST, e.doc_id
            """,
            cases,
        )

    queue: asyncio.Queue = asyncio.Queue()
    for d in docs:
        queue.put_nowait(d)

    total = {"documents": len(docs), "proposed": 0, "verified": 0,
             "rejected": 0, "written": 0, "unavailable": 0}
    per_lane = {name: {"done": 0, "written": 0} for name in lanes}
    done = 0
    started = asyncio.get_event_loop().time()

    async def worker(lane_name: str, lane: dict) -> None:
        nonlocal done
        sem = asyncio.Semaphore(1)          # one in flight per slot
        while True:
            try:
                d = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                r = await _extract_document(pool, sem, d, d["case_id"],
                                            contract, dry_run, lane=lane)
                for k in ("proposed", "verified", "rejected", "written",
                          "unavailable"):
                    total[k] += r[k]
                per_lane[lane_name]["done"] += 1
                per_lane[lane_name]["written"] += r["written"]
                done += 1
                if done % 10 == 0:
                    rate = done / max(
                        asyncio.get_event_loop().time() - started, 1e-9) * 60
                    left = (len(docs) - done) / max(rate, 1e-9)
                    print(f"  {done}/{len(docs)} docs  {rate:.1f}/min  "
                          f"~{left:.0f} min left  "
                          + "  ".join(f"{n}={per_lane[n]['done']}"
                                      for n in per_lane), flush=True)
            finally:
                queue.task_done()

    tasks = []
    for name, lane in lanes.items():
        for _ in range(lane["slots"]):
            tasks.append(asyncio.create_task(worker(name, lane)))
    await asyncio.gather(*tasks)

    total["per_lane"] = per_lane
    return total


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", required=True, help="case_id, or ALL")
    ap.add_argument("--dry-run", action="store_true",
                    help="verify quotes and report, write nothing")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-gaps", action="store_true",
                    help="skip documents that already asserted a fact")
    ap.add_argument("--lanes", default="local,groq",
                    help="providers to run in PARALLEL, comma separated. "
                         "One endpoint cannot be parallelised (measured: "
                         "it serializes), so throughput comes from using "
                         "several at once.")
    ap.add_argument("--provider", choices=["local", "groq", "gemini"],
                    default="local",
                    help="local has no rate limit; cloud is higher quality "
                         "but capped")
    ap.add_argument("--concurrency", type=int, default=2,
                    help="documents at once. The local model is a single "
                         "GPU behind one endpoint, so 2 is about its "
                         "useful ceiling; above that calls queue past the "
                         "60s timeout and fail over to the cloud.")
    args = ap.parse_args()

    # local: try the local model first, fall back to the configured cloud.
    # groq/gemini: skip local entirely and pin that provider.
    PROVIDER["force_cloud"] = args.provider != "local"
    PROVIDER["name"] = None if args.provider == "local" else args.provider

    pool = await asyncpg.create_pool(_dsn(), min_size=2,
                                     max_size=max(4, args.concurrency + 2))
    sem = asyncio.Semaphore(args.concurrency)
    conn = await pool.acquire()
    try:
        contract = await build_contract(conn)
        print("Field contract, generated from the live schema:")
        print("  " + contract.replace("\n", "\n  "))
        print()

        if args.case == "ALL":
            rows = await conn.fetch(
                "SELECT case_id FROM rebuild_cases ORDER BY case_id"
                + (f" LIMIT {args.limit}" if args.limit else ""))
            cases = [r["case_id"] for r in rows]
        else:
            cases = [args.case]

        lanes = {n: LANES[n] for n in
                 (x.strip() for x in args.lanes.split(","))
                 if n in LANES}
        if not lanes:
            sys.exit(f"no valid lanes in {args.lanes!r}")
        print("Lanes: " + ", ".join(
            f"{n} x{l['slots']}" for n, l in lanes.items()), flush=True)

        total = await run_corpus(pool, contract, cases, args.dry_run,
                                 args.only_gaps, lanes)
        print(chr(10) + "Per lane:", total.pop("per_lane"))
        print("\nTotals:", total)
        if total["proposed"]:
            rate = 100.0 * total["verified"] / total["proposed"]
            print(f"Quote verification rate: {rate:.1f}%")
    finally:
        await pool.release(conn)
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
