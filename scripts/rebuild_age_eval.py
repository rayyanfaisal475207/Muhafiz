"""
Rebuild the disposable AGE evaluator graph from production.

    python scripts/rebuild_age_eval.py                      # dry run
    python scripts/rebuild_age_eval.py --execute            # real rebuild

TEST AND SECURITY INFRASTRUCTURE — NOT A RUNTIME DEPENDENCY (Phase 5D).
Phase 5C used this evaluator to contain model-written Cypher: production was
protected by running untrusted text somewhere else. Phase 5D removed the
untrusted text entirely — the model now emits a typed `AgeQueryPlan` that
trusted code compiles into read-only Cypher, executed directly against
production. **No production runtime path calls this script or needs the
evaluator database.** Answering an aggregate question does not require
running this first.

What the evaluator is still FOR, and why it was not deleted: it is the only
place in this system where genuinely destructive queries can be run on
realistic data. Adversarial tests, compiler-output verification, injection
attempts and security regressions all belong there rather than against
production. Refresh it with this script when those tests need current data.

WHAT THIS DOES. Copies `muhafiz.evidence_graph` into
`muhafiz_age_eval.evidence_graph_eval`, then records a snapshot row so the
evaluator copy can be identified and its staleness reasoned about. The
evaluator graph is DISPOSABLE: model-generated Cypher is allowed to damage
it, which is the entire point of it existing.

DIRECTION IS THE WHOLE RISK. Source is production and is opened READ-ONLY;
target is the evaluator and is the only thing ever written. Reversing them
would destroy production, so the direction is asserted three times — by
name check, by a read-only source transaction, and by refusing to issue any
write statement whose target database is not the evaluator. This script
never drops or recreates `muhafiz` or `evidence_graph`, and contains no
statement that could.

WHO CONNECTS. Both connections are made by an OPERATOR role (whatever
DATABASE_URL/AGE_EVAL_DATABASE_URL name), not by the evaluator application
role. `muhafiz_age_eval_app` deliberately has no CONNECT privilege on
production — granting it any would dissolve the containment boundary this
script exists to serve. The copy is an operator action; the evaluator
cannot initiate it.

HOW THE COPY WORKS. AGE stores each label as an ordinary table of
(id graphid, properties agtype) — plus (start_id, end_id) for edges. A
graphid encodes `label_id << 48 | sequence`, so a copied vertex id only
resolves if the target graph assigns the SAME label id. Labels are
therefore created in production's exact label-id order, including labels
with zero rows (Organization, CONFLICTS_WITH, CROSS_VERSION_OF) whose
absence would shift every later id. Rows move as COPY text, which
round-trips agtype faithfully; ids are preserved verbatim and each
sequence is then advanced past production's, so later writes to the
evaluator cannot collide with copied ids.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PRODUCTION_DB = "muhafiz"
PRODUCTION_GRAPH = "evidence_graph"
EVAL_DB = "muhafiz_age_eval"
EVAL_GRAPH = "evidence_graph_eval"

#: Labels AGE creates itself with create_graph(). Never created by hand and
#: never copied: every row lives in a child table (measured: `SELECT count(*)
#: FROM ONLY _ag_label_vertex` = 0), so copying the parent would double.
_AGE_INTERNAL_LABELS = ("_ag_label_vertex", "_ag_label_edge")

#: Counts that must match production for the snapshot to be marked verified.
#: Deliberately small and high-value rather than a full per-label assertion:
#: these three are the figures every Phase 4/5 report is stated in terms of.
_VERIFY_LABELS = (("Case", "case_count"), ("Person", "person_count"),
                  ("SAME_AS", "same_as_count"))


def _plain(url: str) -> str:
    return (url or "").replace("postgresql+asyncpg://", "postgresql://")


def _db_name(dsn: str) -> str:
    if not dsn:
        return ""
    return dsn.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _strip_ssl_require(dsn: str) -> str:
    """Local container Postgres has no TLS; `?ssl=require` would fail."""
    return dsn.split("?", 1)[0]


class DirectionError(RuntimeError):
    """Raised when source/target are not exactly production -> evaluator."""


def assert_direction(source_dsn: str, target_dsn: str) -> tuple[str, str]:
    """Refuse anything but production -> evaluator. Checked before connecting."""
    src, tgt = _db_name(source_dsn), _db_name(target_dsn)

    if not src:
        raise DirectionError("DATABASE_URL is not set — no source database.")
    if not tgt:
        raise DirectionError(
            "AGE_EVAL_DATABASE_URL is not set — no evaluator target. Refusing "
            "to guess one."
        )
    if src == tgt:
        raise DirectionError(
            f"Source and target are the same database ({src!r}). Refusing."
        )
    if src != PRODUCTION_DB:
        raise DirectionError(
            f"Source database is {src!r}, expected {PRODUCTION_DB!r}. Refusing "
            f"to copy from an unrecognised source."
        )
    if tgt != EVAL_DB:
        raise DirectionError(
            f"Target database is {tgt!r}, expected {EVAL_DB!r}. Refusing to "
            f"write to anything but the disposable evaluator."
        )
    if tgt == PRODUCTION_DB:  # unreachable given the above; stated anyway
        raise DirectionError("Target is production. Refusing.")
    return src, tgt


async def _label_manifest(src_conn) -> list[dict]:
    """Production's labels in creation order, with sequence state.

    Ordered by `ag_label.id` because that id is what a graphid encodes.
    """
    rows = await src_conn.fetch(
        """
        SELECT l.id, l.kind::text AS kind, l.name::text AS name,
               l.seq_name::text AS seq_name
        FROM ag_catalog.ag_label l
        JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
        WHERE g.name = $1
        ORDER BY l.id
        """,
        PRODUCTION_GRAPH,
    )
    return [dict(r) for r in rows]


async def _copy_label(src_conn, tgt_conn, label: str, kind: str) -> int:
    """Move one label's rows. Returns the row count written.

    `copy_from_query` streams to a sink rather than returning the data (it
    returns the COPY status string), so the sink here is a coroutine that
    accumulates the bytes. Fine at this scale — the largest label is ~4.7k
    rows — and it keeps the whole copy in one transaction-free pass without
    a temp file.
    """
    cols = ("id", "start_id", "end_id", "properties") if kind == "e" else ("id", "properties")
    col_sql = ", ".join(f'"{c}"' for c in cols)

    buf = io.BytesIO()

    async def _sink(chunk: bytes) -> None:
        buf.write(chunk)

    # COPY ... TO STDOUT serialises agtype as text and graphid as its
    # integer form; agtype_in/graphid_in read exactly that back. Verified
    # round-trip before this script was written. ONLY excludes the
    # inheritance parent, whose rows all live in these child tables.
    await src_conn.copy_from_query(
        f'SELECT {col_sql} FROM ONLY {PRODUCTION_GRAPH}."{label}"',
        output=_sink,
    )
    data = buf.getvalue()
    if not data:
        return 0

    await tgt_conn.copy_to_table(
        label, schema_name=EVAL_GRAPH, columns=list(cols),
        source=io.BytesIO(data),
    )
    row = await tgt_conn.fetchrow(f'SELECT count(*) AS n FROM ONLY {EVAL_GRAPH}."{label}"')
    return int(row["n"])


async def _count(conn, schema: str, label: str) -> int:
    row = await conn.fetchrow(f'SELECT count(*) AS n FROM {schema}."{label}"')
    return int(row["n"])


async def rebuild(execute: bool) -> int:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    source_dsn = _strip_ssl_require(_plain(os.getenv("DATABASE_URL", "")))

    # The TARGET connection needs operator rights: drop_graph()/create_graph()
    # mutate ag_catalog objects owned by whoever installed the AGE extension,
    # and the evaluator application role deliberately does not own them (see
    # config.AGE_EVAL_ADMIN_DATABASE_URL). Fall back to the application URL
    # only so a deployment that made the app role the extension owner still
    # works — the direction guard still applies either way, so this cannot
    # widen WHERE we write, only WHO writes.
    target_dsn = _strip_ssl_require(
        _plain(
            os.getenv("AGE_EVAL_ADMIN_DATABASE_URL", "")
            or os.getenv("AGE_EVAL_DATABASE_URL", "")
        )
    )

    try:
        src_name, tgt_name = assert_direction(source_dsn, target_dsn)
    except DirectionError as exc:
        print(f"REFUSED: {exc}")
        return 2

    print(f"  source (read-only) : {src_name}.{PRODUCTION_GRAPH}")
    print(f"  target (disposable): {tgt_name}.{EVAL_GRAPH}")
    if not execute:
        print("\n(Dry run — nothing written. Re-run with --execute.)")

    import asyncpg

    src_conn = await asyncpg.connect(source_dsn, statement_cache_size=0)
    tgt_conn = await asyncpg.connect(target_dsn, statement_cache_size=0)
    try:
        # Belt and braces: the source session cannot write even if a
        # statement below were wrong.
        await src_conn.execute("SET default_transaction_read_only = on")
        await src_conn.execute("SET search_path = ag_catalog, public")
        await tgt_conn.execute("SET search_path = ag_catalog, public")

        # Identity confirmed from the server, not from the DSN string.
        actual_src = await src_conn.fetchval("SELECT current_database()")
        actual_tgt = await tgt_conn.fetchval("SELECT current_database()")
        if actual_src != PRODUCTION_DB or actual_tgt != EVAL_DB:
            print(
                f"REFUSED: connections resolved to {actual_src!r} -> "
                f"{actual_tgt!r}, expected {PRODUCTION_DB!r} -> {EVAL_DB!r}."
            )
            return 2
        print(f"  verified at server : {actual_src} -> {actual_tgt}")

        manifest = await _label_manifest(src_conn)
        copyable = [m for m in manifest if m["name"] not in _AGE_INTERNAL_LABELS]
        print(f"  labels to recreate : {len(copyable)} "
              f"({sum(1 for m in copyable if m['kind'] == 'v')} vertex, "
              f"{sum(1 for m in copyable if m['kind'] == 'e')} edge)")

        src_totals = {
            m["name"]: await _count(src_conn, PRODUCTION_GRAPH, m["name"])
            for m in copyable
        }
        print(f"  source vertices    : "
              f"{await _count(src_conn, PRODUCTION_GRAPH, '_ag_label_vertex')}")
        print(f"  source edges       : "
              f"{await _count(src_conn, PRODUCTION_GRAPH, '_ag_label_edge')}")

        if not execute:
            for m in copyable:
                if src_totals[m["name"]]:
                    print(f"      would copy {m['name']:<20} {src_totals[m['name']]:>6}")
            return 0

        # ── Drop and recreate ONLY the evaluator graph ────────────────
        # Guarded by name so this statement cannot name production even if
        # the constant were edited carelessly.
        assert EVAL_GRAPH != PRODUCTION_GRAPH, "evaluator graph must not be production"
        exists = await tgt_conn.fetchval(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1", EVAL_GRAPH
        )
        if exists:
            print(f"  dropping {EVAL_GRAPH} in {actual_tgt} …")
            await tgt_conn.execute(
                f"SELECT ag_catalog.drop_graph('{EVAL_GRAPH}', true)"
            )
        await tgt_conn.execute(f"SELECT ag_catalog.create_graph('{EVAL_GRAPH}')")

        # Labels in production's id order, so graphids stay valid.
        for m in copyable:
            fn = "create_vlabel" if m["kind"] == "v" else "create_elabel"
            await tgt_conn.execute(
                f"SELECT ag_catalog.{fn}('{EVAL_GRAPH}', $1)", m["name"]
            )

        # Verify the id alignment actually held before moving any data.
        tgt_ids = {
            r["name"]: r["id"]
            for r in await tgt_conn.fetch(
                """
                SELECT l.name::text AS name, l.id
                FROM ag_catalog.ag_label l
                JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
                WHERE g.name = $1
                """,
                EVAL_GRAPH,
            )
        }
        misaligned = [
            (m["name"], m["id"], tgt_ids.get(m["name"]))
            for m in manifest
            if tgt_ids.get(m["name"]) != m["id"]
        ]
        if misaligned:
            print("REFUSED: label ids did not align; copied graphids would "
                  "not resolve. Mismatches (label, production, evaluator):")
            for row in misaligned[:10]:
                print(f"    {row}")
            return 3
        print(f"  label ids aligned  : {len(tgt_ids)} labels")

        # ── Data ──────────────────────────────────────────────────────
        copied: dict[str, int] = {}
        for m in copyable:
            n = await _copy_label(src_conn, tgt_conn, m["name"], m["kind"])
            copied[m["name"]] = n
            if n:
                print(f"      copied {m['name']:<20} {n:>6}")

        # Sequences: advance past production so evaluator writes (which are
        # expected and allowed) cannot collide with copied ids.
        for m in copyable:
            last = await src_conn.fetchval(
                "SELECT last_value FROM pg_sequences "
                "WHERE schemaname = $1 AND sequencename = $2",
                PRODUCTION_GRAPH, m["seq_name"],
            )
            if last:
                await tgt_conn.execute(
                    f'SELECT setval(\'{EVAL_GRAPH}."{m["seq_name"]}"\', $1, true)',
                    int(last),
                )

        # ── Verification ──────────────────────────────────────────────
        mismatches = [
            (name, src_totals[name], copied.get(name, 0))
            for name in src_totals
            if src_totals[name] != copied.get(name, 0)
        ]
        tgt_v = await _count(tgt_conn, EVAL_GRAPH, "_ag_label_vertex")
        tgt_e = await _count(tgt_conn, EVAL_GRAPH, "_ag_label_edge")
        src_v = await _count(src_conn, PRODUCTION_GRAPH, "_ag_label_vertex")
        src_e = await _count(src_conn, PRODUCTION_GRAPH, "_ag_label_edge")
        verified = not mismatches and tgt_v == src_v and tgt_e == src_e

        if mismatches:
            print("  PER-LABEL MISMATCH:")
            for row in mismatches:
                print(f"    {row}")

        snapshot_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        await tgt_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS public.age_eval_snapshot (
                snapshot_id     text PRIMARY KEY,
                created_at      timestamptz NOT NULL DEFAULT now(),
                source_database text NOT NULL,
                source_graph    text NOT NULL,
                case_count      bigint,
                person_count    bigint,
                same_as_count   bigint,
                total_vertices  bigint,
                total_edges     bigint,
                verified        boolean NOT NULL DEFAULT false
            )
            """
        )
        await tgt_conn.execute(
            """
            INSERT INTO public.age_eval_snapshot
                (snapshot_id, source_database, source_graph, case_count,
                 person_count, same_as_count, total_vertices, total_edges,
                 verified)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            snapshot_id, actual_src, PRODUCTION_GRAPH,
            copied.get("Case", 0), copied.get("Person", 0),
            copied.get("SAME_AS", 0), tgt_v, tgt_e, verified,
        )
        # The evaluator application role must be able to READ its own
        # snapshot record (age_eval_client.require_verified_snapshot).
        #
        # KNOWN LIMITATION, stated rather than hidden: ideally this role
        # would hold SELECT only, so that a compromised or runaway
        # evaluation could not forge `verified = true` and present a damaged
        # graph as a valid production copy. That split requires genuinely
        # separate operator credentials for the rebuild. On this deployment
        # the rebuild runs as the evaluator role itself (it owns the graph
        # objects it must drop and recreate), so revoking write here would
        # only break the rebuild, not protect anything. Snapshot metadata is
        # therefore integrity-checked, NOT tamper-proof against this role.
        # Point AGE_EVAL_ADMIN_DATABASE_URL at a separate operator role and
        # restore the REVOKE to close it.
        await tgt_conn.execute(
            "GRANT SELECT ON public.age_eval_snapshot TO muhafiz_age_eval_app"
        )

        print(f"\n  snapshot_id   : {snapshot_id}")
        print(f"  Case          : {copied.get('Case', 0)} (source {src_totals.get('Case')})")
        print(f"  Person        : {copied.get('Person', 0)} (source {src_totals.get('Person')})")
        print(f"  SAME_AS       : {copied.get('SAME_AS', 0)} (source {src_totals.get('SAME_AS')})")
        print(f"  total vertices: {tgt_v} (source {src_v})")
        print(f"  total edges   : {tgt_e} (source {src_e})")
        print(f"  verified      : {verified}")

        if not verified:
            print("\n  Snapshot recorded as UNVERIFIED — the AGE route will "
                  "refuse to use it.")
            return 1

        # Production is read-only in this session, but say so from the data.
        print(f"\n  production after: Case="
              f"{await _count(src_conn, PRODUCTION_GRAPH, 'Case')} "
              f"Person={await _count(src_conn, PRODUCTION_GRAPH, 'Person')} "
              f"SAME_AS={await _count(src_conn, PRODUCTION_GRAPH, 'SAME_AS')}")
        return 0
    finally:
        await src_conn.close()
        await tgt_conn.close()


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Perform the real rebuild. Without it, this is a dry run.",
    )
    args = parser.parse_args()
    sys.exit(await rebuild(execute=args.execute))


if __name__ == "__main__":
    asyncio.run(main())
