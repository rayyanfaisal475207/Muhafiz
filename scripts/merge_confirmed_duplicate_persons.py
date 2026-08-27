# ============================================================
# Physical Person node merge — redirect every active edge off a confirmed-
# SAME_AS component's "donor" nodes onto one canonical "survivor" node,
# using the SAME append-only supersede mechanism (src/graph/versioning.py)
# every other graph mutation in this codebase already uses. Donor nodes are
# NEVER deleted — they are tagged `merged_into` and kept for provenance.
#
# WHY A NODE-LEVEL SCRIPT AT ALL, GIVEN entity_resolution.py's OWN RULE
# ("name-fallback tiers never physically merge into the candidate node"):
# confirming a SAME_AS edge already makes every node in a confirmed
# component collapse to one logical identity at READ time
# (community_detection.build_canonical_map(), and — as of the XAGG/Local
# Search fix this script's own investigation prompted — XAGG and Local
# Search's community join too). What confirming does NOT do is reduce the
# PHYSICAL node/edge count: a real person can still be sitting on top of
# 139 separate graph nodes. This script is the deliberate, opt-in next
# step for a component someone has decided is worth physically
# consolidating — it is not run automatically by ingestion or by
# confirm_match(), and it changes nothing about what any canonicalizing
# consumer already returns (see the survivor-selection rule below).
#
# SURVIVOR SELECTION — MUST match build_canonical_map() exactly:
# the lexicographically smallest entity_id in the confirmed-SAME_AS
# connected component. This is not a style choice: every existing
# canonicalizing consumer (retrieve_graph, community_summarization.py,
# XAGG, Local Search) already computes this exact id as "the" identity for
# the component. Picking any other survivor would make this merge a
# regression — the min-id node would keep being treated as canonical by
# every reader, but hold zero active edges post-merge.
#
# WHAT GETS REDIRECTED — every edge label a Person can carry as either
# endpoint, enumerated by grepping every versioning.write_edge() call site
# in this codebase with "Person" as an endpoint (not guessed):
#   BELONGS_TO_CASE, APPEARS_IN, ASSOCIATED_WITH, INVOLVED_IN, RELATED_TO,
#   LOCATED_AT, OWNS, REGISTERED_TO
# SAME_AS edges themselves are never touched — they are the evidence of
# why the merge happened.
#
# WHAT NEVER HAPPENS:
#   - No node is ever deleted.
#   - No edge is ever deleted or mutated in place — every redirect is a
#     new versioning.write_edge() call with supersedes_edge_id set, so the
#     donor's original edge becomes superseded (historical), never gone.
#   - A donor-to-donor edge within the SAME component would become a
#     Person->itself self-loop on the survivor — skipped and reported,
#     never written.
#   - A component where find_cnic_conflicts() reports a conflict is
#     skipped entirely (defense in depth — confirm-time already vetoes
#     this, but an irreversible-ish bulk mutation re-checks its own
#     precondition rather than trusting it silently).
#
# AFTER REDIRECTING: donors that shared identical provenance for the same
# fact (e.g. two donors both citing the same chunk for the same case
# membership) now collide as TRUE duplicates on the survivor. This script
# does not deduplicate them itself — re-run
# cleanup_duplicate_belongs_to_case_edges.py and
# cleanup_duplicate_appears_in_edges.py afterward, same as the plan.
#
# Run: python scripts/merge_confirmed_duplicate_persons.py --dry-run
#      python scripts/merge_confirmed_duplicate_persons.py --dry-run --case-id fir-1001-26
#      python scripts/merge_confirmed_duplicate_persons.py --apply --admin-email you@example.com
#      python scripts/merge_confirmed_duplicate_persons.py --apply --admin-email you@example.com --case-id fir-1001-26
#
# --admin-email is REQUIRED for --apply — see scripts/_script_admin.py.
# --case-id (optional) scopes the run to components that include at least
# one Person node currently BELONGS_TO_CASE-linked to that case — intended
# for the first real run (fir-1001-26, the case this was audited against)
# before a corpus-wide sweep.
# ============================================================

import asyncio
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from scripts._script_admin import AdminIdentityError, resolve_admin
from src.graph import age_client, versioning
from src.graph.community_detection import build_canonical_map, fetch_confirmed_same_as
from src.graph.same_as_integrity import find_cnic_conflicts

# Exhaustively grepped (not guessed) from every versioning.write_edge() call
# site with "Person" as an endpoint — see this module's own docstring.
# NOTE — OCCURRED_ON was NOT caught by the original literal-string grep
# (structured_projection.py writes it with a variable `from_label`, not a
# literal "Person"); the --dry-run's own "unmatched-endpoint-label" report
# is exactly what caught it — proof the defensive skip-and-report design
# works, not a bug it should have prevented. Left listed here, not removed,
# so the fix is visible in history.
_PERSON_EDGE_LABELS = (
    "BELONGS_TO_CASE", "APPEARS_IN", "ASSOCIATED_WITH", "INVOLVED_IN",
    "RELATED_TO", "LOCATED_AT", "OWNS", "REGISTERED_TO", "OCCURRED_ON",
)

# The identity/match property each non-Person label a Person edge can touch
# is matched on at its own write_edge() call site (grepped, not guessed).
# "Address" is matched on its literal text, "Date" on its literal date
# string — neither has a synthetic id.
_MATCH_KEY_BY_LABEL = {
    "Person": "entity_id", "Case": "case_id", "Document": "doc_id",
    "StructuredRecord": "record_id", "Incident": "entity_id",
    "PoliceStation": "station_id", "District": "district_id",
    "Address": "text", "Weapon": "entity_id", "Vehicle": "entity_id",
    "Date": "date",
}

# Per-mention/per-extraction metadata, not a fact ABOUT the person — every
# donor legitimately has its own value here (a different spelling variant,
# a different per-mention extraction score) and disagreement is expected,
# not a conflict worth surfacing for human review. True identity fields
# (cnic, phone, dob, ...) are NOT in this set and are still conflict-checked.
_NON_IDENTITY_PROPERTY_KEYS = {
    "entity_id", "as_of", "confidence", "source_doc_id",
    "extraction_confidence", "canonical_name",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _components(case_id: str | None) -> dict[str, list[str]]:
    """canonical_survivor_id -> [donor_id, ...] (donors only, survivor excluded),
    for every confirmed-SAME_AS component with >1 member. When `case_id` is
    given, keep only components with at least one member currently
    BELONGS_TO_CASE-linked (active) to that case."""
    pairs = await fetch_confirmed_same_as()
    canonical_map = build_canonical_map(pairs)

    members: dict[str, set[str]] = defaultdict(set)
    for entity_id, survivor_id in canonical_map.items():
        members[survivor_id].add(entity_id)
    members = {s: m for s, m in members.items() if len(m) > 1}

    if case_id is not None:
        rows = await age_client.execute_cypher(
            "MATCH (n:Person)-[e:BELONGS_TO_CASE]->(c:Case) WHERE e.superseded_by IS NULL "
            "AND c.case_id = $case_id RETURN n.entity_id AS entity_id",
            params={"case_id": case_id}, columns=["entity_id"],
        )
        in_case = {r["entity_id"] for r in rows if r.get("entity_id")}
        members = {s: m for s, m in members.items() if m & in_case}

    return {survivor: sorted(m - {survivor}) for survivor, m in members.items()}


async def _fetch_active_edges(entity_id: str) -> list[dict]:
    """Every active (superseded_by IS NULL), non-SAME_AS edge touching this
    Person node as either endpoint. Directed queries (outgoing/incoming
    separately), same performance reasoning as
    graph_retriever._both_directions()'s own comment: an undirected AGE
    pattern compiles to an un-indexable Cartesian OR."""
    outgoing = await age_client.execute_cypher(
        "MATCH (a:Person {entity_id: $id})-[e]->(b) "
        "WHERE e.superseded_by IS NULL AND type(e) <> 'SAME_AS' "
        "RETURN id(e) AS edge_id, type(e) AS label, properties(e) AS props, "
        "labels(b) AS to_labels, properties(b) AS to_props, "
        "e.source_doc_id AS source_doc_id, e.source_chunk_id AS source_chunk_id, "
        "e.confidence AS confidence",
        params={"id": entity_id},
        columns=["edge_id", "label", "props", "to_labels", "to_props",
                  "source_doc_id", "source_chunk_id", "confidence"],
    )
    for r in outgoing:
        r["direction"] = "outgoing"

    incoming = await age_client.execute_cypher(
        "MATCH (a)-[e]->(b:Person {entity_id: $id}) "
        "WHERE e.superseded_by IS NULL AND type(e) <> 'SAME_AS' "
        "RETURN id(e) AS edge_id, type(e) AS label, properties(e) AS props, "
        "labels(a) AS to_labels, properties(a) AS to_props, "
        "e.source_doc_id AS source_doc_id, e.source_chunk_id AS source_chunk_id, "
        "e.confidence AS confidence",
        params={"id": entity_id},
        columns=["edge_id", "label", "props", "to_labels", "to_props",
                  "source_doc_id", "source_chunk_id", "confidence"],
    )
    for r in incoming:
        r["direction"] = "incoming"

    return outgoing + incoming


def _other_match(row: dict) -> tuple[str, dict] | None:
    """(label, {match_key: value}) for the non-Person endpoint of an edge
    row, or None if that label's match key is unknown (skip defensively —
    never guess a match dict for an edge type not verified against a real
    write_edge() call site)."""
    label = (row["to_labels"] or [None])[0]
    if label is None:
        return None
    key = _MATCH_KEY_BY_LABEL.get(label)
    if key is None:
        return None
    value = (row["to_props"] or {}).get(key)
    if value is None:
        return None
    return label, {key: value}


async def _plan_component(survivor_id: str, donor_ids: list[str], all_component_ids: set[str]) -> dict:
    """Read-only: compute the full set of actions for one component without
    writing anything. Used identically by --dry-run and as the first phase
    of --apply, so the report printed always matches what --apply does."""
    report = {
        "survivor_id": survivor_id,
        "donor_ids": donor_ids,
        "property_additions": {},   # donor_id -> {key: value} the survivor is missing
        "property_conflicts": [],   # (donor_id, key, survivor_value, donor_value)
        "edge_redirects": [],       # per-edge plan dicts
        "self_loops_skipped": [],
        "unmatched_endpoints_skipped": [],
        "locked_skipped": [],
        "cnic_conflict": False,
    }

    conflicts = await find_cnic_conflicts()
    conflicting_ids = {c["a_id"] for c in conflicts} | {c["b_id"] for c in conflicts}
    if conflicting_ids & (set(donor_ids) | {survivor_id}):
        report["cnic_conflict"] = True
        return report

    survivor_rows = await age_client.execute_cypher(
        "MATCH (n:Person {entity_id: $id}) RETURN properties(n) AS props",
        params={"id": survivor_id}, columns=["props"],
    )
    survivor_props = (survivor_rows[0]["props"] if survivor_rows else {}) or {}

    for donor_id in donor_ids:
        donor_rows = await age_client.execute_cypher(
            "MATCH (n:Person {entity_id: $id}) RETURN properties(n) AS props",
            params={"id": donor_id}, columns=["props"],
        )
        donor_props = (donor_rows[0]["props"] if donor_rows else {}) or {}
        additions = {}
        for k, v in donor_props.items():
            if k in _NON_IDENTITY_PROPERTY_KEYS:
                continue
            if v is None or v == "":
                continue
            existing = survivor_props.get(k)
            if existing is None or existing == "":
                additions[k] = v
                survivor_props[k] = v  # so a later donor's conflicting value is caught, not silently applied too
            elif existing != v:
                report["property_conflicts"].append((donor_id, k, existing, v))
        if additions:
            report["property_additions"][donor_id] = additions

        for row in await _fetch_active_edges(donor_id):
            if (row["props"] or {}).get("locked"):
                # versioning.write_edge() itself refuses to supersede a
                # locked OCCURRED_ON edge (investigator-verified — see its
                # own docstring). Skip it here too rather than attempting
                # the write and reporting a confusing "failed" — a locked
                # edge staying on the donor, un-redirected, is the CORRECT
                # outcome, not an error.
                report["locked_skipped"].append({
                    "donor_id": donor_id, "edge_id": row["edge_id"], "label": row["label"],
                })
                continue
            other = _other_match(row)
            if other is None:
                report["unmatched_endpoints_skipped"].append({
                    "donor_id": donor_id, "edge_id": row["edge_id"], "label": row["label"],
                })
                continue
            other_label, other_match = other
            if other_label == "Person" and other_match.get("entity_id") in all_component_ids:
                report["self_loops_skipped"].append({
                    "donor_id": donor_id, "edge_id": row["edge_id"], "label": row["label"],
                    "other_id": other_match.get("entity_id"),
                })
                continue
            report["edge_redirects"].append({
                "donor_id": donor_id, "edge_id": row["edge_id"], "label": row["label"],
                "direction": row["direction"], "other_label": other_label, "other_match": other_match,
                "props": row["props"] or {}, "source_doc_id": row["source_doc_id"],
                "source_chunk_id": row["source_chunk_id"], "confidence": row["confidence"],
            })

    return report


async def _apply_component(report: dict, admin) -> dict:
    survivor_id = report["survivor_id"]
    result = {"properties_written": 0, "edges_redirected": 0, "edges_failed": 0, "donors_tagged": 0}

    if report["property_additions"]:
        merged = {}
        for additions in report["property_additions"].values():
            merged.update(additions)
        if merged:
            await versioning.write_node("Person", {"entity_id": survivor_id}, merged)
            result["properties_written"] = len(merged)

    for plan in report["edge_redirects"]:
        clean_props = {k: v for k, v in plan["props"].items()
                        if k not in ("as_of", "confidence", "source_doc_id", "source_chunk_id", "superseded_by")}
        if plan["direction"] == "outgoing":
            new_edge = await versioning.write_edge(
                plan["label"], "Person", {"entity_id": survivor_id},
                plan["other_label"], plan["other_match"], clean_props,
                source_doc_id=plan["source_doc_id"], source_chunk_id=plan["source_chunk_id"],
                confidence=plan["confidence"] if plan["confidence"] is not None else 1.0,
                supersedes_edge_id=plan["edge_id"],
            )
        else:
            new_edge = await versioning.write_edge(
                plan["label"], plan["other_label"], plan["other_match"],
                "Person", {"entity_id": survivor_id}, clean_props,
                source_doc_id=plan["source_doc_id"], source_chunk_id=plan["source_chunk_id"],
                confidence=plan["confidence"] if plan["confidence"] is not None else 1.0,
                supersedes_edge_id=plan["edge_id"],
            )
        if new_edge is not None:
            result["edges_redirected"] += 1
        else:
            result["edges_failed"] += 1

    for donor_id in report["donor_ids"]:
        await versioning.write_node(
            "Person", {"entity_id": donor_id},
            {"merged_into": survivor_id, "merged_at": _now_iso()},
        )
        result["donors_tagged"] += 1

    from src.retrieval.entity_vector_store import delete_entities
    try:
        await delete_entities(report["donor_ids"])
    except Exception as exc:
        print(f"  WARNING: vector store cleanup failed for {survivor_id}: {exc}")

    return result


def _admin_email_arg() -> str:
    for i, arg in enumerate(sys.argv):
        if arg == "--admin-email" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--admin-email="):
            return arg.split("=", 1)[1]
    return ""


def _case_id_arg() -> str | None:
    for i, arg in enumerate(sys.argv):
        if arg == "--case-id" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--case-id="):
            return arg.split("=", 1)[1]
    return None


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply
    case_id = _case_id_arg()

    admin = None
    if apply:
        try:
            admin = await resolve_admin(_admin_email_arg())
        except AdminIdentityError as exc:
            print(f"REFUSING TO APPLY — {exc}")
            return
        print(f"Acting as: {admin.email} ({admin.role}, id={admin.id})\n")

    components = await _components(case_id)
    print(f"Confirmed-SAME_AS components with >1 member"
          f"{f' touching case {case_id}' if case_id else ''}: {len(components)}")
    if not components:
        print("Nothing to merge.")
        return

    for survivor_id, donor_ids in sorted(components.items(), key=lambda kv: -len(kv[1])):
        all_ids = set(donor_ids) | {survivor_id}
        report = await _plan_component(survivor_id, donor_ids, all_ids)

        print(f"\n=== survivor {survivor_id} — {len(donor_ids)} donor(s) ===")
        if report["cnic_conflict"]:
            print("  SKIPPED — a CNIC conflict was found in this component "
                  "(find_cnic_conflicts()). Needs human review, not merged.")
            continue

        label_counts = Counter(r["label"] for r in report["edge_redirects"])
        print(f"  Property additions: {sum(len(v) for v in report['property_additions'].values())} "
              f"across {len(report['property_additions'])} donor(s)")
        if report["property_conflicts"]:
            print(f"  Property CONFLICTS (not applied, needs review): {len(report['property_conflicts'])}")
            for donor_id, key, existing, donor_val in report["property_conflicts"][:10]:
                print(f"    {donor_id}.{key}={donor_val!r} vs survivor's {existing!r}")
        print(f"  Edge redirects: {sum(label_counts.values())} — {dict(label_counts)}")
        if report["self_loops_skipped"]:
            print(f"  Self-loops skipped (donor<->donor within component): {len(report['self_loops_skipped'])}")
        if report["locked_skipped"]:
            print(f"  Locked edges skipped (investigator-verified, stays on donor): {len(report['locked_skipped'])}")
        if report["unmatched_endpoints_skipped"]:
            print(f"  Unmatched-endpoint-label edges skipped (needs script update): "
                  f"{len(report['unmatched_endpoints_skipped'])}")
            for u in report["unmatched_endpoints_skipped"][:5]:
                print(f"    donor={u['donor_id']} edge_id={u['edge_id']} label={u['label']}")

        if apply:
            result = await _apply_component(report, admin)
            print(f"  APPLIED — properties_written={result['properties_written']} "
                  f"edges_redirected={result['edges_redirected']} "
                  f"edges_failed={result['edges_failed']} donors_tagged={result['donors_tagged']}")

    if dry_run:
        print("\nDRY RUN — no changes made. Re-run with --apply --admin-email <email> to merge.")


if __name__ == "__main__":
    asyncio.run(main())
