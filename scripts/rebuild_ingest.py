"""Ingestion for the rebuilt schema.

WHY THIS EXISTS

The rebuild tables are correct and empty. The 73 real cases live in the
legacy `cases` table and in `chunk_fulltext` (712 chunks, 456 documents),
which were chunked for vector search and never parsed into facts.

This fills the rebuilt schema from that source, front-loading the cost so
that retrieval — which happens on every question, forever — becomes a SQL
query rather than a re-reading of text.

WHAT IS AND IS NOT HARDCODED

Parsing the pipe-delimited chunk header IS format handling: every ingester
must know the serialisation of its input, the same way a CSV reader knows
about commas. That is not the thing the rebuild forbids.

What is forbidden is authored SEMANTICS — a list of valid statuses, crime
categories, weapon types, hierarchy levels, or join paths. None appear here.
Every value lands raw. Every resolved key is nullable and filled by a later,
re-runnable pass. An unseen value is never rejected.

STAGES

  A  structure   documents, elements, spans, chunks, cases, places
                 deterministic, no model, exactly reproducible
  B  entities    persons, roles, weapons, events, statutes
                 model-driven, schema-generated prompt, span-verified
  C  verify      catalog refresh and counts

Stage A must be exactly reproducible: running it twice produces the same
rows. Stage B is not deterministic, so every row it writes carries the span
it came from and a confidence, and nothing it writes is trusted without the
quoted text matching the source.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()


def _dsn() -> str:
    """asyncpg needs a plain postgresql:// URL, not SQLAlchemy's dialect form."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        sys.exit("DATABASE_URL is not set")
    return url.replace("postgresql+asyncpg://", "postgresql://")


# ── Document identity ───────────────────────────────────────────────────
# Source doc_ids carry their own structure:
#
#   psrms_fir_fir-117-26#narrative_f3a8f707
#   psrms_fir_fir-1001-26#zimni_FZ-C901-1_005a05dc
#   psrms_fir_fir-109-26#chalaan_outcome_CO-C15-1
#
# after '#': a role, an optional source reference, and an optional content
# hash. The role is read, never validated against a list — a document kind
# introduced next year lands with its own role and is not rejected.

_HASH = re.compile(r"^[0-9a-f]{8}$")
_REF = re.compile(r"^[A-Z]{1,3}-.*\d")          # FZ-C901-1, CO-C15-1, FP-64-2
_TRAILING_SEQ = re.compile(r"-(\d+)$")


@dataclass
class DocKey:
    role: str
    ref: str | None
    sequence: int | None


def parse_doc_id(doc_id: str) -> DocKey:
    frag = doc_id.split("#", 1)[1] if "#" in doc_id else doc_id
    parts = frag.split("_")
    if parts and _HASH.match(parts[-1]):
        parts = parts[:-1]

    role_parts: list[str] = []
    ref: str | None = None
    for p in parts:
        if ref is None and _REF.match(p):
            ref = p
        elif ref is None:
            role_parts.append(p)

    seq = None
    if ref:
        m = _TRAILING_SEQ.search(ref)
        if m:
            seq = int(m.group(1))
    return DocKey(role="_".join(role_parts) or "unknown", ref=ref, sequence=seq)


# ── Header parsing ──────────────────────────────────────────────────────
# Every chunk repeats a generated header:
#
#   FIR 117/26 | تھانہ مدینہ ٹاؤن، فیصل آباد | فیصل آباد | Sections: 328A PPC
#     | Incident: 2026-03-11T18:30:00Z
#
# Fields are identified by SHAPE, not by position, so a header that gains or
# loses a field still parses.

_FIR = re.compile(r"^FIR\s+(\d+)\s*/\s*(\d+)")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:]+Z?")


@dataclass
class Header:
    raw: str = ""
    fir_number: str | None = None
    fir_serial: int | None = None
    fir_year: int | None = None
    station_raw: str | None = None
    city_raw: str | None = None
    sections_raw: str | None = None
    incident_at: datetime | None = None
    length: int = 0


def parse_header(text: str) -> Header:
    line = text.split("\n", 1)[0]
    h = Header(raw=line, length=len(line))
    for part in (p.strip() for p in line.split("|")):
        if not part:
            continue
        m = _FIR.match(part)
        if m:
            h.fir_number = f"{m.group(1)}/{m.group(2)}"
            h.fir_serial, h.fir_year = int(m.group(1)), int(m.group(2))
        elif part.lower().startswith("sections:"):
            h.sections_raw = part.split(":", 1)[1].strip()
        elif part.lower().startswith("incident:"):
            m2 = _ISO.search(part)
            if m2:
                # asyncpg binds a real datetime, not an ISO string, even
                # with an explicit ::timestamptz cast in the statement.
                h.incident_at = datetime.fromisoformat(
                    m2.group(0).replace("Z", "+00:00"))
        elif h.station_raw is None:
            h.station_raw = part
        elif h.city_raw is None:
            h.city_raw = part
    return h


# ── Statutes ───────────────────────────────────────────────────────────
# The header carries 'Sections: 328A PPC, 34 PPC, 13 Arms Ordinance 1965'.
# That is exact, already structured, and was being discarded. Parsing it
# deterministically gives statute coverage at zero model cost and with no
# extraction risk.
#
# Acts are DISCOVERED here, never listed: whatever act name appears becomes
# an act row. 'CNSA 1997' appearing next year needs no code change.

_SECTION = re.compile(r"^\s*(\d+[A-Za-z]*)\s+(.+?)\s*$")


def parse_sections(sections_raw: str | None) -> list[tuple[str, str]]:
    """-> [(section_no, act_name)], in the order the header lists them."""
    if not sections_raw:
        return []
    out: list[tuple[str, str]] = []
    for item in sections_raw.split(","):
        m = _SECTION.match(item)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def act_key(name: str) -> str:
    """Stable id from the act name as written; no authored mapping."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or hashlib.sha256(name.encode()).hexdigest()[:12]


# ── Body ────────────────────────────────────────────────────────────────
# After the header comes one labelled section: 'Narrative: …',
# 'Crime scene: …', 'Zimni entry None: …'. The label is kept raw as the
# element type.

_LABEL = re.compile(r"^([A-Za-z][A-Za-z ]{2,30}?)(?:\s+\w+)?:\s*", re.M)


@dataclass
class Body:
    label: str | None
    text: str
    offset: int


def parse_body(text: str) -> Body:
    nl = text.find("\n")
    if nl == -1:
        return Body(None, "", len(text))
    rest = text[nl + 1:]
    m = _LABEL.match(rest)
    if m:
        return Body(m.group(1).strip(), rest[m.end():], nl + 1 + m.end())
    return Body(None, rest, nl + 1)


# ── Reassembly ──────────────────────────────────────────────────────────
# chunk_fulltext splits documents mid-word ('inance 1965 لاگو۔'), so spans
# computed per chunk would anchor to fragments. Documents are rebuilt from
# their ordered chunks, and each original chunk keeps its offset range.

_CHUNK_IDX = re.compile(r"_c(\d+)$")


@dataclass
class SourceDoc:
    doc_id: str
    case_id: str
    chunks: list[tuple[str, str]] = field(default_factory=list)  # (chunk_id, text)

    def ordered(self) -> list[tuple[str, str]]:
        def key(c: tuple[str, str]) -> int:
            m = _CHUNK_IDX.search(c[0])
            return int(m.group(1)) if m else 0
        return sorted(self.chunks, key=key)

    def assemble(self) -> tuple[str, list[tuple[str, int, int]]]:
        """Full text plus (chunk_id, start, end) for every original chunk.

        Chunks repeat the header, so only the first contributes it; the rest
        contribute their body. Without this the header would appear once per
        chunk in the reassembled document and inflate every offset.
        """
        ordered = self.ordered()
        header_line = ordered[0][1].split(chr(10), 1)[0] if ordered else ""

        parts: list[str] = []
        ranges: list[tuple[str, int, int]] = []
        pos = 0
        for i, (chunk_id, text) in enumerate(ordered):
            if i == 0:
                piece = text
            elif header_line and text.startswith(header_line):
                # This continuation repeats the header; drop the repeat and
                # keep the body.
                piece = parse_body(text).text
            else:
                # A continuation with NO header — 'inance 1965 لاگو۔', the
                # tail of a word split across chunks. Earlier this was run
                # through parse_body, which found no newline, returned empty,
                # and silently DISCARDED the text: 712 source chunks became
                # 557. Keep it whole.
                piece = text
            if not piece:
                continue
            if parts:
                parts.append("\n")
                pos += 1
            start = pos
            parts.append(piece)
            pos += len(piece)
            ranges.append((chunk_id, start, pos))
        return "".join(parts), ranges


SCHEMA_A = """
-- Stage A writes only these; all are additive and re-runnable.
"""


async def stage_a(conn: asyncpg.Connection, limit_case: str | None = None) -> dict:
    """Structure: documents, elements, spans, chunks, cases, places.

    Deterministic. Re-running replaces the same rows with identical content,
    so a failed run can simply be repeated.
    """
    where = "WHERE case_id IS NOT NULL"
    args: list = []
    if limit_case:
        where += " AND case_id = $1"
        args.append(limit_case)

    rows = await conn.fetch(
        f"SELECT chunk_id, doc_id, case_id, text FROM chunk_fulltext {where}"
        " ORDER BY doc_id, chunk_id",
        *args,
    )
    if not rows:
        return {"error": "no source chunks matched"}

    docs: dict[str, SourceDoc] = {}
    for r in rows:
        d = docs.setdefault(r["doc_id"], SourceDoc(r["doc_id"], r["case_id"]))
        d.chunks.append((r["chunk_id"], r["text"]))

    stats = {
        "source_chunks": len(rows), "documents": 0, "cases": 0,
        "elements": 0, "spans": 0, "chunks": 0, "places": 0,
        "case_documents": 0, "statutes": 0,
    }

    # Cases first: a document must reference an existing case row.
    by_case: dict[str, list[SourceDoc]] = {}
    for d in docs.values():
        by_case.setdefault(d.case_id, []).append(d)

    place_cache: dict[tuple[str, str], int] = {}

    for case_id, case_docs in by_case.items():
        header = None
        for d in case_docs:
            full, _ = d.assemble()
            h = parse_header(full)
            if h.fir_number:
                header = h
                break
        header = header or Header()

        station_place_id = None
        if header.station_raw:
            station_place_id = await _upsert_place_chain(
                conn, header.station_raw, header.city_raw, place_cache
            )
            stats["places"] = len(place_cache)

        await conn.execute(
            """
            INSERT INTO rebuild_cases
                (case_id, fir_number, fir_serial, fir_year, incident_at,
                 station_place_id, location_text, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5::timestamptz,$6,$7, now(), now())
            ON CONFLICT (case_id) DO UPDATE SET
                fir_number = EXCLUDED.fir_number,
                fir_serial = EXCLUDED.fir_serial,
                fir_year   = EXCLUDED.fir_year,
                incident_at = EXCLUDED.incident_at,
                station_place_id = EXCLUDED.station_place_id,
                location_text = EXCLUDED.location_text,
                updated_at = now()
            """,
            case_id, header.fir_number, header.fir_serial, header.fir_year,
            header.incident_at, station_place_id, header.city_raw,
        )
        stats["cases"] += 1

        for section_no, act_name in parse_sections(header.sections_raw):
            aid = act_key(act_name)
            await conn.execute(
                "INSERT INTO acts (act_id, name_en) VALUES ($1,$2)"
                " ON CONFLICT (act_id) DO NOTHING", aid, act_name)
            sid = f"{aid}:{section_no}"
            await conn.execute(
                "INSERT INTO statute_sections (section_id, act_id, section_no)"
                " VALUES ($1,$2,$3) ON CONFLICT (section_id) DO NOTHING",
                sid, aid, section_no)
            await conn.execute(
                "INSERT INTO case_statutes (case_id, section_id) VALUES ($1,$2)"
                " ON CONFLICT DO NOTHING", case_id, sid)
            stats["statutes"] += 1

    # Documents, elements, spans, chunks.
    for doc in docs.values():
        full, ranges = doc.assemble()
        key = parse_doc_id(doc.doc_id)
        src_hash = hashlib.sha256(full.encode("utf-8")).hexdigest()

        await conn.execute(
            """
            INSERT INTO rebuild_documents
                (doc_id, case_id, doc_type_id, source_system, external_id,
                 language, is_global, revision, source_hash)
            VALUES ($1,$2,$3,'chunk_fulltext',$4,'ur',false,1,$5)
            ON CONFLICT (doc_id) DO UPDATE SET
                case_id = EXCLUDED.case_id,
                doc_type_id = EXCLUDED.doc_type_id,
                source_hash = EXCLUDED.source_hash
            """,
            doc.doc_id, doc.case_id, key.role, key.ref, src_hash,
        )
        stats["documents"] += 1

        await conn.execute(
            """
            INSERT INTO case_documents
                (case_id, doc_id, doc_role_raw, sequence_no, received_at)
            VALUES ($1,$2,$3,$4, now())
            ON CONFLICT (case_id, doc_id) DO UPDATE SET
                doc_role_raw = EXCLUDED.doc_role_raw,
                sequence_no  = EXCLUDED.sequence_no
            """,
            doc.case_id, doc.doc_id, key.role, key.sequence,
        )
        stats["case_documents"] += 1

        # Rebuilding elements/spans wholesale keeps the stage reproducible:
        # a re-run must not accumulate duplicates.
        # doc_spans is deliberately NOT cleared. Extraction writes spans
        # here too, and facts reference them; deleting a span nulls the
        # reference through ON DELETE SET NULL, which silently strips
        # provenance from rows that keep looking fine. Measured: one
        # accidental Stage A re-run during extraction stripped 5 facts.
        # Structural spans are now identified by their character range and
        # reused instead of recreated.
        await conn.execute("DELETE FROM doc_elements WHERE doc_id = $1", doc.doc_id)
        await conn.execute("DELETE FROM rebuild_chunks WHERE doc_id = $1", doc.doc_id)

        hdr = parse_header(full)
        body = parse_body(full)

        root = await conn.fetchval(
            """
            INSERT INTO doc_elements
                (doc_id, parent_id, element_type, section_path, ordinal,
                 text, char_start, char_end)
            VALUES ($1, NULL, 'document', $2, 0, $3, 0, $4)
            RETURNING element_id
            """,
            # The full reassembled text lives on the root element. Without
            # it, stored char offsets cannot be resolved back to anything
            # and provenance is unverifiable — a span that cannot be
            # checked is not provenance, it is a claim.
            doc.doc_id, key.role, full, len(full),
        )
        stats["elements"] += 1

        if hdr.length:
            await conn.execute(
                """
                INSERT INTO doc_elements
                    (doc_id, parent_id, element_type, section_path, ordinal,
                     text, char_start, char_end)
                VALUES ($1,$2,'header',$3,1,$4,0,$5)
                """,
                doc.doc_id, root, f"{key.role}/header", hdr.raw, hdr.length,
            )
            stats["elements"] += 1

        body_elem = None
        if body.text:
            body_elem = await conn.fetchval(
                """
                INSERT INTO doc_elements
                    (doc_id, parent_id, element_type, section_path, ordinal,
                     text, label, char_start, char_end)
                VALUES ($1,$2,$3,$4,2,$5,$6,$7,$8) RETURNING element_id
                """,
                doc.doc_id, root, (body.label or "body").lower().replace(" ", "_"),
                f"{key.role}/{(body.label or 'body').lower().replace(' ', '_')}",
                body.text, body.label, body.offset, body.offset + len(body.text),
            )
            stats["elements"] += 1

            # The span that every extracted fact from this document will cite.
            body_span = await _span(
                conn,
                doc.doc_id, body_elem, body.offset, body.offset + len(body.text),
                body.text,
            )
            stats["spans"] += 1

        for idx, (chunk_id, start, end) in enumerate(ranges):
            # Each chunk gets its own span, so a citation can point at the
            # chunk actually retrieved rather than the whole document.
            chunk_span = await _span(
                conn, doc.doc_id, body_elem or root, start, end, full[start:end])
            stats["spans"] += 1
            await conn.execute(
                """
                INSERT INTO rebuild_chunks
                    (chunk_id, doc_id, case_id, span_id, element_id, ordinal,
                     text, section_path, element_type, is_global,
                     is_roman_urdu, token_count)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,false,false,$10)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    span_id = EXCLUDED.span_id,
                    text    = EXCLUDED.text
                """,
                chunk_id, doc.doc_id, doc.case_id, chunk_span,
                body_elem or root, idx, full[start:end],
                f"{key.role}/{(body.label or 'body').lower().replace(' ', '_')}",
                (body.label or "body").lower().replace(" ", "_"),
                len(full[start:end].split()),
            )
            stats["chunks"] += 1

    return stats


async def _span(
    conn: asyncpg.Connection, doc_id: str, element_id: int | None,
    start: int, end: int, text: str,
) -> int:
    """Get or create the span for a character range.

    A span IS its range within a document, so the same range is the same
    span however it was produced. Reusing rather than recreating is what
    lets Stage A be re-run while extracted facts already point at spans —
    the earlier version deleted and recreated them, and the FK's
    ON DELETE SET NULL silently stripped provenance from facts that still
    looked complete.
    """
    existing = await conn.fetchval(
        "SELECT span_id FROM doc_spans"
        " WHERE doc_id = $1 AND char_start = $2 AND char_end = $3",
        doc_id, start, end)
    if existing is not None:
        return existing
    return await conn.fetchval(
        "INSERT INTO doc_spans (doc_id, element_id, char_start, char_end, text)"
        " VALUES ($1,$2,$3,$4,$5) RETURNING span_id",
        doc_id, element_id, start, end, text)


async def _upsert_place_chain(
    conn: asyncpg.Connection,
    station_raw: str,
    city_raw: str | None,
    cache: dict[tuple[str, str], int],
) -> int:
    """Create station and city places and the containment edge between them.

    Levels are values, not tables (migration 043), so this writes a station
    under a city without any schema knowing that stations sit under cities.
    A source that puts a station under a division instead needs no change here.
    """
    async def place(kind: str, name: str) -> int:
        ck = (kind, name)
        if ck in cache:
            return cache[ck]
        pid = await conn.fetchval(
            "SELECT place_id FROM places WHERE kind_raw=$1 AND name_raw=$2",
            kind, name,
        )
        if pid is None:
            pid = await conn.fetchval(
                "INSERT INTO places (kind_raw, name_raw) VALUES ($1,$2)"
                " RETURNING place_id",
                kind, name,
            )
        cache[ck] = pid
        return pid

    station_id = await place("police_station", station_raw.strip())
    if city_raw:
        city_id = await place("city", city_raw.strip())
        await conn.execute(
            """
            INSERT INTO place_hierarchy (child_id, parent_id, hierarchy_raw)
            VALUES ($1,$2,'civil') ON CONFLICT DO NOTHING
            """,
            station_id, city_id,
        )
    return station_id


async def stage_a2(conn: asyncpg.Connection) -> dict:
    """Carry structured metadata across from the legacy `cases` table.

    These fields were written by the source system, not read out of prose,
    so they need no model and carry no extraction risk:

        crime_category         73/73   ->  rebuild_cases.category_raw
        investigation_status   21/73   ->  rebuild_cases.status_raw
        location               73/73   ->  rebuild_cases.location_text
        incident_date          64/73   ->  rebuild_cases.incident_at
        investigation_officer  73/73   ->  officers + case_officers

    Values land RAW. crime_category is not a category (L2 — it holds
    statute references like 'PPC, Arms Ordinance 1965'), and
    investigation_status is a narrative sentence, not a status. Neither is
    interpreted here; both are stored as written and interpreted later by
    passes that can be re-run and corrected.

    Coverage is asymmetric on purpose: existing values are never
    overwritten, because Stage A read the document header, which is closer
    to the source than a system field copied from it.
    """
    stats = {"category": 0, "status": 0, "location": 0, "incident": 0,
             "officers": 0, "case_officers": 0}

    rows = await conn.fetch(
        """
        SELECT c.case_id, c.crime_category, c.investigation_status,
               c.location, c.incident_date, c.investigation_officer
        FROM cases c JOIN rebuild_cases r ON r.case_id = c.case_id
        """
    )
    for r in rows:
        if r["crime_category"]:
            await conn.execute(
                "UPDATE rebuild_cases SET category_raw = $2, updated_at = now()"
                " WHERE case_id = $1 AND category_raw IS NULL",
                r["case_id"], r["crime_category"])
            stats["category"] += 1
        if r["investigation_status"]:
            await conn.execute(
                "UPDATE rebuild_cases SET status_raw = $2, updated_at = now()"
                " WHERE case_id = $1 AND status_raw IS NULL",
                r["case_id"], r["investigation_status"])
            stats["status"] += 1
        if r["location"]:
            await conn.execute(
                "UPDATE rebuild_cases SET location_text = $2, updated_at = now()"
                " WHERE case_id = $1 AND location_text IS NULL",
                r["case_id"], r["location"])
            stats["location"] += 1
        if r["incident_date"]:
            await conn.execute(
                "UPDATE rebuild_cases SET incident_at = $2, updated_at = now()"
                " WHERE case_id = $1 AND incident_at IS NULL",
                r["case_id"], r["incident_date"])
            stats["incident"] += 1

        io_name = (r["investigation_officer"] or "").strip()
        if io_name:
            # Officer identity is by name here because that is all the
            # source field carries. Unlike a person mention, this is a
            # single system field with 41 distinct values across 73 cases,
            # so equal strings are the same officer — and if that turns out
            # to be wrong, it is visible as one officer holding two belts.
            officer_id = "o-" + hashlib.sha256(
                io_name.encode("utf-8")).hexdigest()[:16]
            await conn.execute(
                "INSERT INTO officers (officer_id, name_ur) VALUES ($1,$2)"
                " ON CONFLICT (officer_id) DO NOTHING", officer_id, io_name)
            stats["officers"] += 1
            await conn.execute(
                """
                INSERT INTO case_officers (case_id, officer_id, role)
                VALUES ($1,$2,'investigation_officer')
                ON CONFLICT DO NOTHING
                """,
                r["case_id"], officer_id)
            stats["case_officers"] += 1

    return stats


async def stage_c(conn: asyncpg.Connection) -> list:
    await conn.execute("SELECT refresh_catalog()")
    return await conn.fetch(
        """
        SELECT 'rebuild_cases' t, count(*) n FROM rebuild_cases
        UNION ALL SELECT 'rebuild_documents', count(*) FROM rebuild_documents
        UNION ALL SELECT 'case_documents', count(*) FROM case_documents
        UNION ALL SELECT 'doc_elements', count(*) FROM doc_elements
        UNION ALL SELECT 'doc_spans', count(*) FROM doc_spans
        UNION ALL SELECT 'rebuild_chunks', count(*) FROM rebuild_chunks
        UNION ALL SELECT 'places', count(*) FROM places
        UNION ALL SELECT 'place_hierarchy', count(*) FROM place_hierarchy
        ORDER BY 1
        """
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", default="a,a2,c",
                    help="comma-separated: a (structure), a2 (legacy "
                         "metadata), c (verify)")
    ap.add_argument("--case", help="restrict to one case_id, for inspection")
    args = ap.parse_args()

    stages = {x.strip() for x in args.stage.split(",") if x.strip()}

    conn = await asyncpg.connect(_dsn())
    try:
        if "a" in stages:
            print("Stage A — structure")
            stats = await stage_a(conn, args.case)
            for k, v in stats.items():
                print(f"  {k:>16}: {v}")
        if "a2" in stages:
            print(chr(10) + "Stage A2 - legacy structured metadata")
            for k, v in (await stage_a2(conn)).items():
                print(f"  {k:>16}: {v}")
        if "c" in stages:
            print("\nStage C — verify")
            for row in await stage_c(conn):
                print(f"  {row['t']:>20}: {row['n']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
