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
# cross-silo) -> cross-versions (Milestone C4) -> citations (M6b) — both
# of the last two need every FIR's Case node to exist, so both run last,
# not interleaved with FIR projection.
# ============================================================
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logger = logging.getLogger(__name__)

from src.data_gateway.muhafiz_api.client import MuhafizApiClient
from src.data_gateway.muhafiz_api.models import CmsComplaint, CriminalRecord, FirRecord, PkmApplication, RoznamchaEntry
from src.data_gateway.muhafiz_api.snapshot import load_snapshot, records_for
from src.graph import age_client
from src.graph.cross_silo_projection import (
    project_cms_complaint,
    project_criminal_record,
    project_fir_citations,
    project_fir_cross_versions,
    project_pkm_application,
)
from src.graph.structured_projection import project_fir
from src.ingestion import muhafiz_records as mr
from src.ingestion.community_refresh_bg import refresh_if_stale
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
    # Milestone C4 (cross_silo_projection.py's `project_fir_cross_versions()`)
    # — same reasoning, CROSS_VERSION_OF duplication on a second `--full` run.
    "CROSS_VERSION_OF",
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


async def purge_orphaned_person_nodes_by_source_prefix(prefix: str, *, graph: str = age_client.GRAPH_NAME) -> int:
    """
    findings.md Module 1 follow-up, Priority 2b Option A
    (MODULE1_GAPS_FIX_PROMPT.md) — structured_projection.resolve_structured_person()'s
    no-CNIC/no-corroboration path (_write_new_person()) mints a fresh
    random entity_id on every call, with no dedup. Re-syncing the same
    no-CNIC person a second time therefore always resolves to a DIFFERENT
    node than last time — purge_edges_by_source_prefix() above correctly
    deletes the OLD node's edges (they carry this same record's own
    source_doc_id), but never the node itself (edge-only by design, see
    that function's own docstring). Left alone, that strands the old node
    as a permanent, zero-edge orphan on every single re-sync, growing
    without bound: one --full re-sync of the real 73-case corpus produced
    ~69 of these live.

    Scoped to THIS record's own source_doc_id prefix — the exact same one
    purge_edges_by_source_prefix() was just called with — so this can only
    ever delete a Person node that record's OWN purge, in this same call,
    could have just orphaned. It never reaches a node any other record is
    responsible for. Call this AFTER purge_edges_by_source_prefix() and
    BEFORE the record is re-projected: the orphan (from the PREVIOUS
    sync) already exists at that point, and cleaning it up before the new
    projection begins means a freshly-written node from THIS run is never
    at risk of being mistaken for one.

    Does not touch entity_resolution's id-generation semantics at all —
    the same real person still gets a new id on the next re-sync; this
    only stops the debris from that from accumulating. See
    MODULE1_GAPS_FIX_PROMPT.md Priority 2b's own Option B for the (bigger,
    riskier — real-name-collision risk) alternative of making the id
    itself deterministic instead.

    THREE QUERY SHAPES TRIED, in order, each ruled out live before landing
    on this one — recorded here so a future reader doesn't re-try the same
    dead ends:
      1. One combined query: `WHERE source_doc_id STARTS WITH $prefix`
         fused with `OPTIONAL MATCH ... WITH ... count(r) ... WHERE
         degree = 0`. Reliably dropped the connection against the real
         AGE instance at this graph's scale (reproduced 3 times) — same
         failure as a full-graph 0-edge scan tried earlier for Priority 2a.
      2. Split into a cheap id-only STARTS WITH lookup, then per-id
         `OPTIONAL MATCH (p)-[r]-()` (any label, any direction) with a
         `WHERE degree = 0` aggregate. Didn't error — HUNG: caught live
         via `pg_stat_activity`, one single-node lookup sat `active`
         (genuinely executing, not lock-waiting) for 25+ seconds. AGE's
         label-scoped edge storage means an unlabeled, undirected pattern
         apparently can't use a per-label index and ends up scanning
         across every label — the same cost class as shape 1, just paid
         inside one node's traversal instead of the WHERE clause.
      3. THIS ONE: loop over `EDGE_LABELS` (the same authoritative list
         purge_edges_by_source_prefix() above already sweeps) one label
         at a time, undirected, checking existence only — `MATCH
         ()-[r:{label}]-() WHERE ... LIMIT 1`. Every single-label query
         purge_edges_by_source_prefix() issues is already proven fast and
         reliable on every real sync run; this reuses that exact shape,
         just anchored to one candidate node instead of scanned globally,
         and stops at the first label that finds an edge (most non-orphan
         candidates resolve on the very first hit).

    Correctness note — this is NOT equivalent to only checking
    BELONGS_TO_CASE (a cheaper-looking shortcut that was considered and
    rejected): a Person's BELONGS_TO_CASE from THIS record's own purge can
    disappear while a genuinely separate edge from a DIFFERENT record
    survives untouched — e.g. a cross-silo ASSOCIATED_WITH written by
    cross_silo_projection.py's _pair_with_existing_incident_roster()
    (Priority 1) with its own, different source_doc_id. Checking only one
    label would misclassify that node as orphaned and DETACH DELETE would
    destroy a real, still-valid edge along with it. Checking every label
    in EDGE_LABELS is the only way to know a candidate is TRULY
    disconnected before deleting it — never trade that away for speed.

    Wrapped in its own try/except: a cleanup step failing must never take
    down the sync it's attached to (same discipline as every other
    resilience layer in this pipeline) — returns 0 and logs a warning
    rather than propagating, so `--full` keeps going through the rest of
    the corpus even if this one step has an off night.
    """
    try:
        candidates = await age_client.execute_cypher(
            "MATCH (p:Person) WHERE p.source_doc_id STARTS WITH $prefix "
            "RETURN p.entity_id AS entity_id",
            params={"prefix": prefix}, columns=["entity_id"], graph=graph,
        )
        deleted = 0
        for row in candidates:
            entity_id = row.get("entity_id")
            if not entity_id:
                continue
            if await _person_has_any_edge(entity_id, graph=graph):
                continue
            result = await age_client.execute_cypher(
                "MATCH (p:Person {entity_id: $eid}) DETACH DELETE p RETURN count(p) AS deleted",
                params={"eid": entity_id}, columns=["deleted"], graph=graph,
            )
            deleted += int(result[0]["deleted"]) if result else 0
        return deleted
    except Exception as exc:
        logger.warning(
            "purge_orphaned_person_nodes_by_source_prefix failed for prefix %r: %s", prefix, exc,
        )
        return 0


async def _person_has_any_edge(entity_id: str, *, graph: str = age_client.GRAPH_NAME) -> bool:
    """
    True the moment ANY of EDGE_LABELS is found touching this Person (in
    either direction) — stops checking further labels as soon as one
    hits, since that alone is enough to know the node isn't orphaned. See
    purge_orphaned_person_nodes_by_source_prefix()'s own docstring for why
    this checks every label individually rather than one unbounded
    any-label pattern (correctness AND performance both depend on it).
    """
    for label in EDGE_LABELS:
        rows = await age_client.execute_cypher(
            f"MATCH (p:Person {{entity_id: $eid}})-[r:{label}]-() RETURN r LIMIT 1",
            params={"eid": entity_id}, columns=["r"], graph=graph,
        )
        if rows:
            return True
    return False


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
    stats["orphaned_persons_purged"] = await purge_orphaned_person_nodes_by_source_prefix(prefix)
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
    stats["orphaned_persons_purged"] = await purge_orphaned_person_nodes_by_source_prefix(prefix)
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
    stats["orphaned_persons_purged"] = await purge_orphaned_person_nodes_by_source_prefix(prefix)
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


async def sync_cross_versions(firs: list[FirRecord], *, dry_run: bool) -> dict:
    """Milestone C4 — same ordering dependency as sync_citations() above
    (every FIR's Case node must already exist), same display_code_index."""
    display_code_index = build_display_code_index(firs)
    total_written = 0
    for fir in firs:
        if dry_run:
            continue
        result = await project_fir_cross_versions(fir, display_code_index)
        total_written += result["cross_version_written"]
    return {"cross_version_written": total_written}


# ── orchestration ─────────────────────────────────────────────────────────

async def run(endpoints: tuple[str, ...], *, dry_run: bool, snapshot_path: str | None) -> None:
    # [Ingestion Quality Control at Scale, Module G1] One tracked run per
    # `--full` sync pass — this is the realistic "millions of cases" bulk
    # path INGESTION_QUALITY_AT_SCALE_PLAN.md is actually about, not the
    # single-document ingest_file() path (also tracked, separately, in
    # src/ingestion/service.py). Skipped entirely for --dry-run: sync_fir()
    # itself returns before any write happens in that mode, so there would
    # be nothing to count — tracking it anyway would just leave an empty,
    # confusing row behind.
    from src.graph import ingestion_quality
    import contextlib
    from datetime import datetime, timezone

    run_id = f"sync-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    tracker = (
        ingestion_quality.track_run(run_id, "sync_muhafiz_data")
        if not dry_run
        else contextlib.nullcontext()
    )
    async with tracker:
        await _run_sync(endpoints, dry_run=dry_run, snapshot_path=snapshot_path)


async def _run_sync(endpoints: tuple[str, ...], *, dry_run: bool, snapshot_path: str | None) -> None:
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
        print("\n-- FIR->FIR cross-versions --")
        cross_version_stats = await sync_cross_versions(firs, dry_run=dry_run)
        print(f"  {cross_version_stats}")

        print("\n-- FIR->FIR citations --")
        citation_stats = await sync_citations(firs, dry_run=dry_run)
        print(f"  {citation_stats}")

    if not dry_run:
        # findings.md Module 6 — community detection never refreshed for
        # real sync data because nothing here ever called it. Awaited
        # directly (not asyncio.create_task, unlike community_refresh_bg's
        # HTTP-handler caller) since this is a one-shot CLI process about
        # to tear down its own connection pool right below.
        print("\n-- Community detection refresh --")
        refresh_result = await refresh_if_stale()
        staleness = refresh_result["staleness"]
        if refresh_result["ran"]:
            summary = refresh_result["summarize_result"] or {}
            print(f"  stale ({staleness['reason']}) — recomputed: "
                  f"{summary.get('attempted', 0)} attempted, "
                  f"{summary.get('written', 0)} written, "
                  f"{summary.get('skipped', 0)} skipped")
        else:
            print(f"  not stale ({staleness['reason']}) — skipped")

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
