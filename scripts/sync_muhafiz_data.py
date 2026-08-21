# ============================================================
# M9 of the Muhafiz Data API migration — the operational sync entry point
# (docs/decisions/0001-muhafiz-api-migration.md).
#
#   python scripts/sync_muhafiz_data.py --dry-run
#   python scripts/sync_muhafiz_data.py --full
#   python scripts/sync_muhafiz_data.py --full --endpoint fir
#   python scripts/sync_muhafiz_data.py --full --snapshot tests/fixtures/muhafiz_api_snapshot.json
#
# NARROWED SCOPE vs. the original plan (round-2 review, confirmed):
# updated_since watermark persistence/scheduling is CUT — the live API
# (muhafiz.onrender.com) is a same-schema STAND-IN, and the real
# integration arrives post-MVP with its own shape; building incremental-
# sync automation against a stand-in now is work likely to be redone.
# `--full` re-fetches and re-projects everything every run. What is NOT
# cut: idempotency. `--full` run twice must not duplicate a single edge.
#
# HOW IDEMPOTENCY WORKS:
# versioning.write_node() MERGEs (already idempotent — re-running a node
# write just refreshes its properties). versioning.write_edge() is a bare
# CREATE (constraint 7, decision record) — re-running a projection without
# a purge first would duplicate every edge it writes, every run. Before
# each record is (re-)projected, this script deletes every edge whose OWN
# source_doc_id starts with that record's synthetic doc-id prefix
# (purge_edges_by_source_prefix below), then re-runs the same projection
# call. Deliberately EDGE-ONLY, never touching nodes: a Person node that
# CNIC-auto-merged across two different FIRs must survive a single-FIR
# purge untouched — only edges THIS record's own projection call created
# are ever at risk of duplication (they always carry that record's own
# source_doc_id, never a shared node's).
#
# WHY THIS ONLY WORKS BECAUSE run_graph_extraction=False (M2/M9's
# ingest_documents() flag): the legacy LLM/NER graph-extraction path
# (src/ingestion/service.py's _run_graph_extraction) tags its writes with
# a HASHED, sanitized doc_id (Document._generate_id() — slashes become
# underscores, plus an md5 suffix), not the clean
# "psrms/fir/{fir_id}#..." strings src/graph/structured_projection.py and
# cross_silo_projection.py use directly. This script never lets that path
# run for Muhafiz Data API records (structured_projection.py/
# cross_silo_projection.py already extract people/weapons/timelines from
# ground truth — a second, LLM-guessed pass over the same text is pure
# waste), so EVERY graph write this script is responsible for uses the
# clean prefix the purge above can actually target. Chunking/embedding
# still runs normally — only the graph-extraction half is skipped.
#
# ORDER: cases (M4) -> FIRs (M6a) -> CMS/PKM/criminal records (M6b,
# cross-silo) -> citations (M6b, needs every FIR's Case node to exist —
# run last, not interleaved with FIR projection).
# ============================================================
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data_gateway.muhafiz_api.client import MuhafizApiClient
from src.data_gateway.muhafiz_api.models import CmsComplaint, CriminalRecord, FirRecord, PkmApplication, RoznamchaEntry
from src.data_gateway.muhafiz_api.snapshot import load_snapshot, records_for
from src.graph import age_client
from src.graph.cross_silo_projection import (
    project_cms_complaint,
    project_criminal_record,
    project_fir_citations,
    project_pkm_application,
)
from src.graph.structured_projection import project_fir
from src.ingestion import muhafiz_records as mr
from src.ingestion.muhafiz_cases import (
    build_display_code_index,
    build_e_tag_index,
    resolve_cms_case_id,
    resolve_pkm_case_id,
)
from src.ingestion.service import ingest_documents

# M4's case-provisioning entry point is reused as-is, not reimplemented —
# this script only adds the ORDER-dependency on top of it (cases must
# exist before Case-node MATCH-only writes elsewhere can find them).
from scripts.sync_muhafiz_cases import upsert_cases

ENDPOINTS = ("fir", "cms", "pkm", "criminal-records", "roznamcha")

# Every edge label the graph declares (migrations/005_age_graph.sql,
# 020_age_date_and_cites_labels.sql) — the purge sweeps all of them, same
# "every label, not just the ones a given record type happens to write"
# discipline as scripts/purge_eval_contamination.py.
EDGE_LABELS = (
    "BELONGS_TO_CASE", "APPEARS_IN", "ASSOCIATED_WITH", "SAME_AS", "OWNS",
    "REGISTERED_TO", "LOCATED_AT", "INVOLVED_IN", "PART_OF", "OCCURRED_ON",
    "CONFLICTS_WITH", "CITES",
    # Milestone B1 (structured_projection.py's `_write_jurisdiction()`) —
    # without this, re-running `--full` would duplicate every FILED_AT
    # edge on each sync, the exact bug this purge step exists to prevent.
    "FILED_AT",
    # Milestone B2 (structured_projection.py's `_write_officers()`) — same
    # reasoning: without this, a second `--full` run would duplicate (not
    # correctly re-chain) every officer's ASSIGNED_TO edge.
    "ASSIGNED_TO",
    # Milestone C1 (structured_projection.py's `_write_accused()`/
    # `_write_related_to()`, cross_silo_projection.py's
    # `_write_pkm_relationship()`) — same reasoning, RELATED_TO duplication
    # on a second `--full` run.
    "RELATED_TO",
)


async def purge_edges_by_source_prefix(prefix: str, *, graph: str = age_client.GRAPH_NAME) -> int:
    """See module docstring — edge-only, node properties don't need this
    (write_node already MERGEs)."""
    deleted = 0
    for label in EDGE_LABELS:
        rows = await age_client.execute_cypher(
            f"""
            MATCH ()-[r:{label}]->()
            WHERE r.source_doc_id STARTS WITH $prefix
            DELETE r
            RETURN count(r)
            """,
            params={"prefix": prefix},
            columns=["c"],
            graph=graph,
        )
        deleted += int(rows[0]["c"]) if rows else 0
    return deleted


# ── fetch ────────────────────────────────────────────────────────────────

async def fetch_all(snapshot_path: str | None, endpoints: tuple[str, ...]) -> dict[str, list[dict]]:
    if snapshot_path:
        snapshot = load_snapshot(Path(snapshot_path))
        return {ep: records_for(snapshot, ep) for ep in endpoints}
    async with MuhafizApiClient() as client:
        return {ep: await client.fetch_all(ep) for ep in endpoints}


# ── per-record sync ──────────────────────────────────────────────────────

async def sync_fir(fir: FirRecord, *, dry_run: bool) -> dict:
    prefix = f"psrms/fir/{fir.fir_id}#"
    stats: dict = {"fir_id": fir.fir_id}

    docs = mr.render_fir(fir)
    if dry_run:
        stats.update(would_purge_prefix=prefix, would_render=len(docs))
        return stats

    stats["edges_purged"] = await purge_edges_by_source_prefix(prefix)
    if docs:
        ingest_stats = await ingest_documents(
            docs, source_name=fir.fir_id, case_id=fir.fir_id, is_global=False,
            doc_type="fir_narrative", run_graph_extraction=False,
        )
        stats["chunks_added"] = ingest_stats.get("chunks_added", 0)
        stats["ingest_error"] = ingest_stats.get("error")
    graph_stats = await project_fir(fir)
    stats["graph"] = graph_stats
    return stats


async def sync_cms(cms: CmsComplaint, case_id: str | None, *, dry_run: bool) -> dict:
    prefix = f"cms/complaint/{cms.complaint_id}#"
    stats: dict = {"complaint_id": cms.complaint_id, "case_id": case_id}

    docs = mr.render_cms(cms)
    if dry_run:
        stats.update(would_purge_prefix=prefix, would_render=len(docs))
        return stats

    stats["edges_purged"] = await purge_edges_by_source_prefix(prefix)
    if docs:
        ingest_stats = await ingest_documents(
            docs, source_name=cms.complaint_id, case_id=case_id, is_global=False,
            doc_type="cms_complaint", run_graph_extraction=False,
        )
        stats["chunks_added"] = ingest_stats.get("chunks_added", 0)
    stats["graph"] = await project_cms_complaint(cms, case_id)
    return stats


async def sync_pkm(pkm: PkmApplication, case_id: str | None, *, dry_run: bool) -> dict:
    prefix = f"pkm/application/{pkm.application_id}#"
    stats: dict = {"application_id": pkm.application_id, "case_id": case_id}

    docs = mr.render_pkm(pkm)
    if dry_run:
        stats.update(would_purge_prefix=prefix, would_render=len(docs))
        return stats

    stats["edges_purged"] = await purge_edges_by_source_prefix(prefix)
    if docs:
        ingest_stats = await ingest_documents(
            docs, source_name=pkm.application_id, case_id=case_id, is_global=False,
            doc_type="pkm_application", run_graph_extraction=False,
        )
        stats["chunks_added"] = ingest_stats.get("chunks_added", 0)
    stats["graph"] = await project_pkm_application(pkm, case_id)
    return stats


async def sync_criminal_record(record: CriminalRecord, *, dry_run: bool) -> dict:
    prefix = f"criminal_db/criminal_record/{record.record_id}#"
    stats: dict = {"record_id": record.record_id}
    if dry_run:
        stats["would_purge_prefix"] = prefix
        return stats
    stats["edges_purged"] = await purge_edges_by_source_prefix(prefix)
    stats["graph"] = await project_criminal_record(record)
    return stats


async def sync_roznamcha(entry: RoznamchaEntry, *, dry_run: bool) -> dict:
    """Case-less by design (see decision record) — chunk/embed only, no graph write."""
    stats: dict = {"entry_id": entry.entry_id}
    docs = mr.render_roznamcha(entry)
    if dry_run:
        stats["would_render"] = len(docs)
        return stats
    if docs:
        ingest_stats = await ingest_documents(
            docs, source_name=entry.entry_id, is_global=False,
            doc_type="roznamcha_entry", run_graph_extraction=False,
        )
        stats["chunks_added"] = ingest_stats.get("chunks_added", 0)
    return stats


async def sync_citations(firs: list[FirRecord], *, dry_run: bool) -> dict:
    display_code_index = build_display_code_index(firs)
    known_codes = set(display_code_index.keys())
    total_written = 0
    for fir in firs:
        if dry_run:
            continue
        result = await project_fir_citations(fir, display_code_index, known_codes)
        total_written += result["cites_written"]
    return {"cites_written": total_written}


# ── orchestration ─────────────────────────────────────────────────────────

async def run(endpoints: tuple[str, ...], *, dry_run: bool, snapshot_path: str | None) -> None:
    raw = await fetch_all(snapshot_path, endpoints)
    firs = [FirRecord(r) for r in raw.get("fir", [])]
    cms_complaints = [CmsComplaint(r) for r in raw.get("cms", [])]
    pkm_applications = [PkmApplication(r) for r in raw.get("pkm", [])]
    criminal_records = [CriminalRecord(r) for r in raw.get("criminal-records", [])]
    roznamcha_entries = [RoznamchaEntry(r) for r in raw.get("roznamcha", [])]

    print(f"Fetched: {len(firs)} FIR, {len(cms_complaints)} CMS, {len(pkm_applications)} PKM, "
          f"{len(criminal_records)} criminal records, {len(roznamcha_entries)} roznamcha")
    if dry_run:
        print("=== DRY RUN — no writes, showing what would happen ===\n")

    if "fir" in endpoints and firs and not dry_run:
        inserted, updated = await upsert_cases(firs)
        print(f"\n-- Cases: {inserted} inserted, {updated} updated --")

    if "fir" in endpoints:
        print(f"\n-- FIRs ({len(firs)}) --")
        for fir in firs:
            stats = await sync_fir(fir, dry_run=dry_run)
            errors = (stats.get("graph") or {}).get("errors") or []
            print(f"  {stats['fir_id']}: {stats}" if errors else f"  {stats['fir_id']}: ok")
            if errors:
                print(f"    errors: {errors}")

    e_tag_index = build_e_tag_index(firs)
    display_code_index = build_display_code_index(firs)

    if "cms" in endpoints:
        print(f"\n-- CMS complaints ({len(cms_complaints)}) --")
        for cms in cms_complaints:
            case_id = resolve_cms_case_id(cms, e_tag_index)
            stats = await sync_cms(cms, case_id, dry_run=dry_run)
            print(f"  {stats['complaint_id']} -> case={case_id}")

    if "pkm" in endpoints:
        print(f"\n-- PKM applications ({len(pkm_applications)}) --")
        for pkm in pkm_applications:
            case_id = resolve_pkm_case_id(pkm, display_code_index)
            stats = await sync_pkm(pkm, case_id, dry_run=dry_run)
            print(f"  {stats['application_id']} -> case={case_id}")

    if "criminal-records" in endpoints:
        print(f"\n-- Criminal records ({len(criminal_records)}) --")
        for record in criminal_records:
            stats = await sync_criminal_record(record, dry_run=dry_run)
            print(f"  {stats['record_id']}")

    if "roznamcha" in endpoints:
        print(f"\n-- Roznamcha entries ({len(roznamcha_entries)}) --")
        for entry in roznamcha_entries:
            stats = await sync_roznamcha(entry, dry_run=dry_run)
            print(f"  {stats['entry_id']}")

    if "fir" in endpoints and firs:
        print("\n-- FIR->FIR citations --")
        citation_stats = await sync_citations(firs, dry_run=dry_run)
        print(f"  {citation_stats}")

    if not dry_run:
        await age_client.close_pool()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="Perform the real sync (writes).")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; no writes. Default if neither flag given.")
    parser.add_argument("--endpoint", choices=ENDPOINTS, default=None,
                         help="Sync only this record type (default: all).")
    parser.add_argument("--snapshot", metavar="PATH", default=None,
                         help="Sync from a saved snapshot instead of the live API.")
    args = parser.parse_args()

    if args.full and args.dry_run:
        print("--full and --dry-run are mutually exclusive.")
        sys.exit(1)

    endpoints = (args.endpoint,) if args.endpoint else ENDPOINTS
    dry_run = not args.full  # dry-run is the default, matching every other destructive script in this repo

    await run(endpoints, dry_run=dry_run, snapshot_path=args.snapshot)


if __name__ == "__main__":
    asyncio.run(main())
