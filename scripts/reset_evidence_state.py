"""
Wipe ALL derived evidence state ahead of the Muhafiz Data API migration
(M5, docs/decisions/0001-muhafiz-api-migration.md — "Wipe derived, keep
reference data", confirmed decision).

The synthetic corpus's embeddings, entity graph, and case rows are
RETIRED, not merged alongside real API data — a stale synthetic Person
node with a fabricated CNIC sitting next to a real one would actively
corrupt CNIC-based cross-case matching, not just add noise.

WHAT THIS WIPES, IN THIS ORDER:

  1. AGE `evidence_graph` — drop_graph(), then re-apply
     migrations/005_age_graph.sql to recreate the graph and pre-create
     every label (the concurrent-first-write race migration 005 exists to
     prevent — see that file's own comment).
  2. Chroma `muhafiz_kb` (ChromaVectorStore.drop_and_recreate()) AND
     `muhafiz_community_reports` (community_vector_store.clear_all_reports())
     — TWO independent collections living in the same persist directory;
     wiping only one leaves stale state in the other with nothing to
     notice it.
  3. Postgres derived rows: `documents`, `case_assignments`, `cases`,
     `community_runs` (cascades to `community_membership`/`community_reports`
     — see migrations/016_community_detection.sql), `ingestion_jobs`.
  4. Filesystem: `ingestion_state.json`, and the stale pre-e5 Chroma
     directories left over from an earlier embedding-dimension migration
     (`data/chroma/`, root `chroma_db/` — NOT `data/chroma_db/`, the live
     `CHROMA_PERSIST_DIR`, which step 2 already handled).

WHAT THIS KEEPS: `users`, `audit_logs`, `police_reference_data`,
`pipeline_runs`/`pipeline_steps`/`generated_files` — RBAC, audit trail,
and the penal-code reference table are not corpus-derived and survive
untouched. `data/memory/` (the synthetic source corpus on disk) is
likewise left alone — retired, not deleted.

ORDER MATTERS: graph before Chroma. `graph_retriever.py` silently drops
any hop whose `APPEARS_IN.source_chunk_id` no longer resolves in Chroma
(see that module's own comment) — if Chroma were wiped first and this
script died partway through, the graph would be left pointing at chunks
that no longer exist, degrading retrieval with no visible error instead
of cleanly having nothing.

Two explicit modes — dry-run is the default; the real wipe requires BOTH
--execute and --yes-i-am-sure, the same double-gate as
scripts/purge_eval_contamination.py, so this can never fire destructively
by accident or a copy-pasted command missing one flag:

    python scripts/reset_evidence_state.py                        # dry run (counts only)
    python scripts/reset_evidence_state.py --execute --yes-i-am-sure   # real wipe

Take a full pg_dump backup AND a copy of data/chroma_db/ immediately
before running with --execute — none of this is reversible once it runs.
"""
import argparse
import asyncio
import shutil
import sys
from pathlib import Path

# Force UTF-8 stdout — same fix as apply_migration.py / purge_eval_contamination.py;
# Urdu content elsewhere in this pipeline makes a Windows console's default
# cp1252 encoding crash mid-run otherwise.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config
from src.graph import age_client

GRAPH = age_client.GRAPH_NAME  # "evidence_graph"

# Every vertex/edge label the graph schema declares (migrations/005_age_graph.sql)
# plus the two ad-hoc labels observed live outside that static list (AGE
# creates a label's table lazily on first write — see purge_eval_contamination.py's
# own note on why "Date"/"DebugTest" exist despite not being in migration 005).
NODE_LABELS = [
    "Case", "Person", "Vehicle", "PhoneNumber", "Address", "Organization",
    "Weapon", "Incident", "Document", "StructuredRecord", "Date", "DebugTest",
]
EDGE_LABELS = [
    "BELONGS_TO_CASE", "APPEARS_IN", "ASSOCIATED_WITH", "SAME_AS", "OWNS",
    "REGISTERED_TO", "LOCATED_AT", "INVOLVED_IN", "PART_OF", "OCCURRED_ON",
    "CONFLICTS_WITH",
]

MIGRATION_005 = ROOT / "migrations" / "005_age_graph.sql"
INGESTION_STATE_FILE = ROOT / "ingestion_state.json"
STALE_CHROMA_DIRS = [ROOT / "data" / "chroma", ROOT / "chroma_db"]


# ── counting (dry-run) ───────────────────────────────────────────────────

async def _count_graph() -> dict:
    node_counts, edge_counts = {}, {}
    for label in NODE_LABELS:
        try:
            rows = await age_client.execute_cypher(
                f"MATCH (n:{label}) RETURN count(n)", columns=["c"], graph=GRAPH,
            )
            node_counts[label] = int(rows[0]["c"]) if rows else 0
        except Exception:
            node_counts[label] = 0  # label doesn't exist yet in this graph
    for label in EDGE_LABELS:
        try:
            rows = await age_client.execute_cypher(
                f"MATCH ()-[r:{label}]->() RETURN count(r)", columns=["c"], graph=GRAPH,
            )
            edge_counts[label] = int(rows[0]["c"]) if rows else 0
        except Exception:
            edge_counts[label] = 0
    return {"nodes": node_counts, "edges": edge_counts}


def _count_chroma() -> dict:
    from src.retrieval.vector_store import ChromaVectorStore
    from src.retrieval import community_vector_store

    kb_count = 0
    try:
        kb_count = ChromaVectorStore.get_instance().count()
    except Exception as exc:
        print(f"  (could not count muhafiz_kb: {exc})")

    community_count = 0
    try:
        community_count = len(community_vector_store._get_collection().get(include=[])["ids"])
    except Exception as exc:
        print(f"  (could not count muhafiz_community_reports: {exc})")

    return {"muhafiz_kb": kb_count, "muhafiz_community_reports": community_count}


async def _count_postgres() -> dict:
    from sqlalchemy import func, select, text
    from src.database.models import Case, CaseAssignment, Document, IngestionJob
    from src.database.postgres import get_session

    counts = {}
    async with get_session() as session:
        for label, model in (
            ("documents", Document), ("cases", Case),
            ("case_assignments", CaseAssignment), ("ingestion_jobs", IngestionJob),
        ):
            result = await session.execute(select(func.count()).select_from(model))
            counts[label] = result.scalar_one()
        # community_runs has no ORM model — community_detection.py itself
        # writes to it via raw SQL only (see that module's _persist()).
        result = await session.execute(text("SELECT count(*) FROM community_runs"))
        counts["community_runs"] = result.scalar_one()
    return counts


def _display_path(p: Path) -> str:
    """Relative to ROOT when possible (the normal case); falls back to the
    absolute path so a test-overridden path (outside ROOT) doesn't crash
    reporting."""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def _count_filesystem() -> dict:
    return {
        "ingestion_state.json": INGESTION_STATE_FILE.exists(),
        **{_display_path(d): d.exists() for d in STALE_CHROMA_DIRS},
    }


async def dry_run() -> dict:
    print(f"=== DRY RUN — counting derived evidence state (no changes) ===\n")

    print(f"AGE graph '{GRAPH}':")
    graph_counts = await _count_graph()
    for label, c in graph_counts["nodes"].items():
        if c:
            print(f"  node {label}: {c}")
    for label, c in graph_counts["edges"].items():
        if c:
            print(f"  edge {label}: {c}")
    total_nodes = sum(graph_counts["nodes"].values())
    total_edges = sum(graph_counts["edges"].values())
    print(f"  TOTAL: {total_nodes} nodes, {total_edges} edges")

    print("\nChroma:")
    chroma_counts = _count_chroma()
    for name, c in chroma_counts.items():
        print(f"  {name}: {c} chunks/reports")

    print("\nPostgres (derived tables only — users/audit_logs/police_reference_data untouched):")
    pg_counts = await _count_postgres()
    for name, c in pg_counts.items():
        print(f"  {name}: {c} rows")

    print("\nFilesystem:")
    fs_state = _count_filesystem()
    for name, exists in fs_state.items():
        print(f"  {name}: {'present' if exists else 'absent'}")

    return {
        "graph": graph_counts, "chroma": chroma_counts,
        "postgres": pg_counts, "filesystem": fs_state,
    }


# ── the real wipe ─────────────────────────────────────────────────────────

async def _reset_graph() -> None:
    print(f"\nDropping AGE graph '{GRAPH}'...")
    pool = await age_client.get_pool()
    async with pool.acquire() as conn:
        # Reuses age_client's own LOAD/search_path setup rather than
        # reimplementing its superuser-vs-preloaded-AGE tolerance — see
        # that function's docstring for why a failed LOAD is swallowed but
        # a failed SET search_path is not.
        await age_client._load_age(conn)
        await conn.execute("SELECT drop_graph($1, true)", GRAPH)
    print(f"  '{GRAPH}' dropped.")

    print(f"Re-applying {MIGRATION_005.name} to recreate the graph + pre-create every label...")
    # Raw asyncpg .execute(), not SQLAlchemy — a multi-statement DDL script
    # can't run through asyncpg's prepared-statement path (same reasoning
    # as scripts/apply_migration.py's own docstring).
    sql = MIGRATION_005.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
    print(f"  '{GRAPH}' recreated with every label pre-created.")


def _reset_chroma() -> None:
    from src.retrieval.vector_store import ChromaVectorStore
    from src.retrieval import community_vector_store

    print("\nDropping and recreating Chroma 'muhafiz_kb'...")
    ChromaVectorStore.get_instance().drop_and_recreate()
    print("  done.")

    print("Clearing Chroma 'muhafiz_community_reports'...")
    community_vector_store.clear_all_reports()
    print("  done.")


async def _reset_postgres() -> None:
    from sqlalchemy import delete, text
    from src.database.models import Case, CaseAssignment, Document, IngestionJob
    from src.database.postgres import get_session

    print("\nDeleting derived Postgres rows...")
    async with get_session() as session:
        # Children before parents, even though the FKs would mostly handle
        # it via CASCADE/SET NULL — explicit is safer than relying on a
        # specific ON DELETE behavior staying what it is today.
        result = await session.execute(text("DELETE FROM community_runs"))
        # community_runs cascades to community_membership/community_reports
        # (both FK ON DELETE CASCADE — see migrations/016_community_detection.sql);
        # no ORM model exists for any of the three, so this is raw SQL,
        # matching community_detection.py's own _persist().
        print(f"  community_runs (cascades to membership/reports): {result.rowcount} deleted")

        for label, model in (
            ("ingestion_jobs", IngestionJob),
            ("case_assignments", CaseAssignment),
            ("documents", Document),
            ("cases", Case),
        ):
            result = await session.execute(delete(model))
            print(f"  {label}: {result.rowcount} deleted")
        await session.commit()


def _reset_filesystem() -> None:
    print("\nCleaning up filesystem artifacts...")
    if INGESTION_STATE_FILE.exists():
        INGESTION_STATE_FILE.unlink()
        print(f"  removed {_display_path(INGESTION_STATE_FILE)}")
    for d in STALE_CHROMA_DIRS:
        if d.exists():
            shutil.rmtree(d)
            print(f"  removed {_display_path(d)}/")


async def execute_reset() -> None:
    await _reset_graph()
    _reset_chroma()
    await _reset_postgres()
    _reset_filesystem()
    print("\n=== Reset complete. Run this script again (dry-run) to confirm everything is zero. ===")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true",
                         help="Perform the real wipe. Requires --yes-i-am-sure too.")
    parser.add_argument("--yes-i-am-sure", action="store_true",
                         help="Required alongside --execute to actually wipe anything.")
    args = parser.parse_args()

    await dry_run()

    if args.execute:
        if not args.yes_i_am_sure:
            print("\n--execute given without --yes-i-am-sure — refusing to wipe anything. "
                  "Re-run with both flags once the dry-run counts above have been reviewed, "
                  "after taking a pg_dump + data/chroma_db/ backup.")
            sys.exit(1)
        await execute_reset()
    else:
        print("\n(Dry run only — no changes made. Re-run with --execute --yes-i-am-sure "
              "to perform the real wipe, after taking a backup.)")

    await age_client.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
