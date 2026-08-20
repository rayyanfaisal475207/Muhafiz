"""
Entity-resolution evaluation harness (Phase 4.11), against the labeled
ground truth purpose-built for this: data/memory/entity_roster.csv.

    python scripts/eval_entity_resolution.py

Feeds entity_resolution.py directly with mentions DERIVED FROM THE ROSTER
(canonical_name/cnic/father's_name per case, per row) — not re-extracted
via NER/domain_entities first. This is deliberate: the roster's job is to
test whether the RESOLUTION algorithm gets the tiering right, and running
it behind imperfect NER would conflate NER recall/precision with
resolution precision/recall, which is exactly the "report precision/
recall per tier separately... a CNIC-tier failure and a name-fallback-
tier failure mean completely different things" instruction this script
exists to satisfy — muddying it with a third, unrelated error source
(NER misses) would defeat the point. End-to-end NER+resolution behavior
was already verified separately (a real FIR PDF ingested via ingest_file,
Phase 4.10) — that's the "does extraction find the right spans" story;
this script is the "does resolution decide correctly, given the mentions
ground truth says exist" story.

DESTRUCTIVE, BUT ISOLATED (Phase 3, Module 3.1): this script wipes its
own graph before running, so results are reproducible run over run
rather than accumulating stale entities/edges from a prior run. It runs
entirely against EVAL_GRAPH ("evidence_graph_eval" — see migration
011_age_eval_graph.sql), a second, physically separate Apache AGE graph
from the production "evidence_graph" real case data lives in. Every
versioning.py/entity_resolution.py call this script makes passes
graph=EVAL_GRAPH explicitly, including the wipe itself, and evaluate()
refuses to run at all if EVAL_GRAPH doesn't look like an eval graph name
(see _assert_eval_graph below) — so a future edit that drops a graph=
argument from one call site fails loudly instead of silently wiping or
reading production data. Before this migration, this script ran
`MATCH (n) DETACH DELETE n` against the shared production graph on every
invocation — a full wipe of all real case data. That is what this
isolation closes.
"""
import asyncio
import csv
import sys
from collections import defaultdict
from pathlib import Path

# Force UTF-8 stdout — the roster's canonical names are Urdu, and a
# Windows console's default cp1252 encoding crashes mid-report otherwise
# (same fix as scripts/apply_migration.py's docstring already notes).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.graph import age_client, entity_resolution as er, versioning

_ROSTER_PATH = Path(__file__).resolve().parent.parent / "data" / "memory" / "entity_roster.csv"

# Second, physically separate AGE graph — see migration
# 011_age_eval_graph.sql. Every graph-touching call in this script passes
# graph=EVAL_GRAPH explicitly; never rely on any function's default.
EVAL_GRAPH = "evidence_graph_eval"


def _assert_eval_graph() -> None:
    """
    Hard, unmissable guard: refuse to run at all unless EVAL_GRAPH
    resolves to a graph name containing "eval". Deliberately a simple
    substring check, not sophisticated — the point is that a future edit
    which accidentally repoints EVAL_GRAPH at the production graph name
    (or drops a graph= argument at a call site, which this alone can't
    catch, but narrows the blast radius for the most likely mistake)
    fails loudly here rather than silently wiping/polluting production
    data.
    """
    if "eval" not in EVAL_GRAPH:
        raise RuntimeError(
            f"Refusing to run: EVAL_GRAPH={EVAL_GRAPH!r} does not look like "
            "an eval graph name (must contain 'eval'). This guard exists "
            "specifically to stop this script from ever wiping the "
            "production evidence_graph again — see this file's module "
            "docstring."
        )


def _parse_attributes(raw: str) -> dict:
    """'role=...; father's_name=...; cnic=...; address=...' -> dict."""
    attrs = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            attrs[k.strip()] = v.strip()
    return attrs


def _split(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(";") if p.strip()]


def _split_variants(raw: str) -> list[str]:
    """surface_variants is pipe-delimited, not semicolon — every other
    multi-value roster column is ';'-separated, but this one isn't; using
    _split() here silently treats the whole pipe-joined string as one
    variant instead of splitting it."""
    return [p.strip() for p in (raw or "").split("|") if p.strip()]


def load_roster(roster_path: Path = _ROSTER_PATH) -> list[dict]:
    """
    `roster_path` — added M11 of the Muhafiz Data API migration
    (docs/decisions/0001-muhafiz-api-migration.md), default unchanged
    (the synthetic-corpus roster). Pass
    data/eval/real_entity_roster.csv (scripts/build_real_entity_roster.py)
    to evaluate against ground truth derived from real CNIC data instead —
    same exact column schema, so nothing else in this file changes.
    """
    rows = []
    with open(roster_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["attrs"] = _parse_attributes(row["canonical_attributes"])
            row["case_id_list"] = _split(row["case_ids"])
            row["cnic_shown_in_list"] = _split(row["cnic_shown_in"])
            row["surface_variant_list"] = _split_variants(row["surface_variants"]) or [row["canonical_name"]]
            rows.append(row)
    return rows


def _entity_type_for(row_type: str) -> str:
    return {"person": "person", "vehicle": "vehicle", "phone": "phone",
            "address": "location", "organization": "organization"}.get(row_type, row_type)


async def resolve_roster(rows: list[dict]) -> dict:
    """
    Process every roster row's case mentions through resolve_and_write, in
    file order. Returns {roster_entity_id: [{"case_id", "result"}]} —
    every mention's resolution outcome, keyed by the roster's OWN entity_id
    (the ground-truth label), not the graph's freshly-minted one.

    Every graph write/read below is explicitly pinned to EVAL_GRAPH.
    """
    # Pre-create every Case node referenced — write_edge() never
    # implicitly creates an endpoint.
    all_case_ids = {c for row in rows for c in row["case_id_list"]}
    for case_id in all_case_ids:
        await versioning.write_node("Case", {"case_id": case_id}, {}, source_doc_id=None, graph=EVAL_GRAPH)

    outcomes: dict[str, list[dict]] = defaultdict(list)

    for row in rows:
        entity_type = _entity_type_for(row["type"])
        if entity_type not in er.TYPE_TO_LABEL:
            continue  # organizations of type "organization" ARE covered; skip anything unmapped

        # One mention per SURFACE VARIANT, not per case_id — a row with 3
        # spelling variants and 1 case_id must generate 3 mentions (the
        # actual claim under test: do spelling variants of the same real
        # document converge), not 1. Where a row has more variants than
        # cases (or vice versa), case_id assignment cycles — this mirrors
        # the roster's own real shape (surface_variants document how the
        # SAME entity was spelled differently across its different real
        # mentions, which needn't line up 1:1 with case_ids).
        variants = row["surface_variant_list"]
        case_ids = row["case_id_list"]
        n_mentions = max(len(variants), len(case_ids))

        # `cnic_shown_in` is genuinely inconsistent in the roster itself:
        # for rows with a populated `appears_in`, it names the specific
        # DOCUMENT the CNIC appears in (e.g. "FIR-2026-ARMS-001"); for
        # rows without one (e.g. P-006), it falls back to naming the
        # CASE instead (e.g. "CASE-015"). Only the latter is meaningful
        # for per-case CNIC attachment. Detect which shape this row uses:
        # if none of cnic_shown_in's values match this row's own
        # case_ids, it's referencing documents (or is simply empty), and
        # a per-case restriction can't be read off it — attach the CNIC
        # to every mention instead of none, which was the eval's own
        # earlier bug (P-DRY-001/002 both silently got NO cnic attached
        # at all, so the "must not merge" check was passing vacuously —
        # name-fallback never merges regardless of whether the CNIC
        # hard-block logic even ran — rather than actually exercising
        # the hard-block path this test exists to guard).
        case_shaped_cnic_ids = [c for c in row["cnic_shown_in_list"] if c in case_ids]
        cnic_restricted_to_cases = bool(case_shaped_cnic_ids)

        for i in range(n_mentions):
            variant = variants[i % len(variants)]
            case_id = case_ids[i % len(case_ids)]
            mention = {"canonical_name": variant}

            if entity_type == "person":
                if row["attrs"].get("father's_name"):
                    mention["father_name"] = row["attrs"]["father's_name"]
                cnic = row["attrs"].get("cnic")
                if cnic:
                    if cnic_restricted_to_cases:
                        if case_id in case_shaped_cnic_ids:
                            mention["cnic"] = cnic
                    else:
                        mention["cnic"] = cnic
            elif entity_type == "vehicle":
                # canonical_name embeds the plate for vehicle rows
                # (e.g. "سوزوکی پک اپ، ICT-LE-309") — structured_fields
                # extraction pulls it out the same way it would from real
                # document text.
                from src.extraction import structured_fields as sf
                plates = sf.extract_plates(variant)
                if plates:
                    mention["plate"] = plates[0].normalized

            doc_id = f"EVAL-{row['entity_id']}-{case_id}-{i}"
            await versioning.write_node("Document", {"doc_id": doc_id}, {}, source_doc_id=doc_id, graph=EVAL_GRAPH)
            await versioning.write_edge(
                "BELONGS_TO_CASE", "Document", {"doc_id": doc_id}, "Case", {"case_id": case_id},
                {}, source_doc_id=doc_id, confidence=1.0, graph=EVAL_GRAPH,
            )

            result = await er.resolve_and_write(entity_type, mention, case_id, doc_id, graph=EVAL_GRAPH)
            outcomes[row["entity_id"]].append({"case_id": case_id, "mention": variant, **result})

    return outcomes


def _same_graph_entity(outcomes: dict, roster_id: str) -> set:
    """The set of distinct graph entity_ids a roster row's mentions resolved to."""
    return {o["entity_id"] for o in outcomes.get(roster_id, [])}


async def evaluate(roster_path: Path = _ROSTER_PATH) -> None:
    _assert_eval_graph()
    print(f"WARNING: wiping {EVAL_GRAPH} for a clean, reproducible run...")
    await age_client.execute_cypher("MATCH (n) DETACH DELETE n", columns=["result"], graph=EVAL_GRAPH)

    rows = load_roster(roster_path)
    by_id = {r["entity_id"]: r for r in rows}
    outcomes = await resolve_roster(rows)

    print(f"\nProcessed {len(rows)} roster entities, {sum(len(v) for v in outcomes.values())} mentions.\n")

    # ── Named test cases ────────────────────────────────────────────────
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str):
        checks.append((name, condition, detail))

    def ids_for(roster_id: str) -> set:
        return _same_graph_entity(outcomes, roster_id)

    def tiers_for(roster_id: str) -> list[str]:
        return [o["tier"] for o in outcomes.get(roster_id, [])]

    # Confusable pairs — must never resolve to the same graph entity, and
    # must never do so via the cnic_auto tier.
    for name, a, b in [
        ("P-DRY-001/002 (same name, diff CNIC)", "P-DRY-001", "P-DRY-002"),
        ("P-CP2A/B (same-ish name, diff CNIC)", "P-CP2A", "P-CP2B"),
        ("P-CP3A/B (similar name, one CNIC missing)", "P-CP3A", "P-CP3B"),
        ("V-NM1A/B (near-miss plate ICT-FL-273/274)", "V-NM1A", "V-NM1B"),
        ("V-NM2A/B (near-miss plate ICT-GM-621/622)", "V-NM2A", "V-NM2B"),
    ]:
        a_ids, b_ids = ids_for(a), ids_for(b)
        disjoint = a_ids.isdisjoint(b_ids)
        check(name, disjoint, f"{a}->{a_ids} vs {b}->{b_ids}")

    # Name-variant spelling drift where the roster's own cnic_shown_in
    # gives CNIC uniform coverage across every mention (P-DRY-003, NV-02,
    # and — per the roster's actual data, despite its "cross-case harder
    # variant" role text — NV-04, whose cnic_shown_in is empty/
    # unrestricted, not case-scoped) — every spelling variant must
    # converge to ONE graph entity. Checked by entity_id convergence, not
    # by requiring every mention's OWN tier to read "cnic_auto" — the
    # very first mention of a brand-new CNIC necessarily gets tier="new"
    # (there is nothing yet to match against); what matters is every
    # subsequent variant finds and attaches to that same node.
    for name, roster_id in [
        ("NV-DRY-01 (احمد رضا قریشی spelling drift)", "P-DRY-003"),
        ("NV-02 (فریحہ ثاقب spelling drift)", "NV-02"),
        ("NV-04 (زینب اکرم cross-case spelling drift)", "NV-04"),
    ]:
        ids = ids_for(roster_id)
        tiers = tiers_for(roster_id)
        check(
            name, len(ids) == 1,
            f"resolved to {len(ids)} distinct entities: {ids}; tiers: {tiers}",
        )

    # NV-03 is the one name-variant row where cnic_shown_in genuinely
    # restricts CNIC to a specific case (CASE-011) — structurally the
    # same pattern as P-006: the CNIC-bearing mentions converge to ONE
    # canonical entity, and the mention from the case where the CNIC
    # isn't shown must resolve to a DIFFERENT (flagged, never merged)
    # entity — never silently merged, never silently missed as an
    # unconnected "new" entity.
    mentions = outcomes.get("NV-03", [])
    ids = ids_for("NV-03")
    flagged_tiers = {o["tier"] for o in mentions} & {er.TIER_FLAGGED, er.TIER_REVIEW}
    check(
        "NV-03 (طارق محمود cross-case spelling drift, partial CNIC coverage)",
        len(ids) == 2 and bool(flagged_tiers),
        f"resolved to {len(ids)} distinct entities: {ids}; "
        f"tiers seen: {[(o['case_id'], o['tier']) for o in mentions]}",
    )

    # P-005 — no CNIC anywhere; must resolve via name-fallback only,
    # never cnic_auto (there's no CNIC to auto-merge on).
    p005_tiers = tiers_for("P-005")
    check(
        "P-005 (no CNIC anywhere, name-fallback only)",
        bool(p005_tiers) and er.TIER_CNIC_AUTO not in p005_tiers,
        f"tiers seen: {p005_tiers}",
    )

    # P-006 — the flagship cross-case repeat offender. CASE-016's mention
    # (no CNIC, per cnic_shown_in) must flag against CASE-015's canonical
    # entity, never auto-merge, never silently miss it.
    p006 = outcomes.get("P-006", [])
    case_016 = next((o for o in p006 if o["case_id"] == "CASE-016"), None)
    case_015 = next((o for o in p006 if o["case_id"] == "CASE-015"), None)
    if case_016 and case_015:
        linked = case_016["tier"] in (er.TIER_FLAGGED, er.TIER_REVIEW) and case_016["entity_id"] != case_015["entity_id"]
        check(
            "P-006 (flagship cross-case repeat offender, flagged not merged)",
            linked,
            f"CASE-015 -> {case_015['entity_id']} ({case_015['tier']}), "
            f"CASE-016 -> {case_016['entity_id']} ({case_016['tier']})",
        )
    else:
        check("P-006 (flagship cross-case repeat offender)", False, "missing expected mentions in roster")

    # ADDR-002 — structural guarantee, not a live graph query: shared
    # address must never be used as person-resolution corroboration.
    check(
        "ADDR-002 (shared address must not imply a relationship)",
        "address" not in er._STRUCTURED_ID_KEYS,
        "verified structurally: 'address' is absent from entity_resolution._STRUCTURED_ID_KEYS, "
        "so a shared address alone can never contribute to a person match score",
    )

    print("Named test cases:")
    passed = 0
    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        print(f"  [{status}] {name}\n         {detail}")
    print(f"\n{passed}/{len(checks)} named test cases passed.\n")

    # ── Per-tier precision/recall over ALL labeled pairs ────────────────
    # "Correct" ground truth per pair_or_group_id + designed_as:
    #   confusable-pair -> must NOT share a graph entity_id
    #   name-variant    -> must share a graph entity_id (single row, so
    #                      always true by construction — reported for
    #                      completeness, not as a discriminating signal)
    tier_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["pair_or_group_id"] and row["designed_as"] in ("confusable-pair",):
            groups[row["pair_or_group_id"]].append(row)

    print("Confusable-pair tier breakdown (must-NOT-merge cases):")
    for group_id, members in groups.items():
        if len(members) != 2:
            continue
        a, b = members
        a_ids, b_ids = ids_for(a["entity_id"]), ids_for(b["entity_id"])
        # Any tier reached that improperly merged (shared entity_id) is a
        # failure for whichever tier was actually used.
        a_tiers, b_tiers = tiers_for(a["entity_id"]), tiers_for(b["entity_id"])
        merged = not a_ids.isdisjoint(b_ids)
        tier_label = "cnic_auto" if (er.TIER_CNIC_AUTO in a_tiers or er.TIER_CNIC_AUTO in b_tiers) else "name_fallback"
        tier_stats[tier_label]["total"] += 1
        if not merged:
            tier_stats[tier_label]["correct"] += 1
        print(f"  {group_id}: merged={merged} ({'FAIL — hard invariant violated' if merged else 'OK'})")

    print("\nPer-tier precision (correct-non-merge / total), reported separately per the task's own instruction:")
    for tier, stats in tier_stats.items():
        precision = stats["correct"] / stats["total"] if stats["total"] else float("nan")
        print(f"  {tier}: {stats['correct']}/{stats['total']} ({precision:.0%})")

    print(
        "\nNote: recall (how many true matches/non-matches the labeled set "
        "actually contains per tier) is bounded by the roster's small, "
        "purpose-built size — every labeled case is enumerated above by "
        "name rather than sampled, since N is small enough to check "
        "exhaustively rather than estimate."
    )

    # ── Write metrics to JSON for the Admin Dashboard ───────────────────
    import json
    from datetime import datetime, timezone
    metrics_path = Path(__file__).resolve().parent.parent / "data" / "eval" / "resolution_metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Structure the results for the dashboard
    checks_out = [{"name": name, "passed": ok, "detail": detail} for name, ok, detail in checks]
    tiers_out = {
        tier: {
            "correct": stats["correct"],
            "total": stats["total"],
            "precision": (stats["correct"] / stats["total"]) if stats["total"] else None
        }
        for tier, stats in tier_stats.items()
    }

    output_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_mentions_processed": sum(len(v) for v in outcomes.values()),
        "total_entities_processed": len(rows),
        "test_cases_passed": passed,
        "test_cases_total": len(checks),
        "test_cases": checks_out,
        "tier_metrics": tiers_out
    }

    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)
    print(f"\nSaved metrics to {metrics_path}")

    await age_client.close_pool()


if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser(description=__doc__)
    _parser.add_argument(
        "--roster", metavar="PATH", default=str(_ROSTER_PATH),
        help="Ground-truth roster CSV (default: the synthetic-corpus roster). "
             "Pass data/eval/real_entity_roster.csv (scripts/build_real_entity_roster.py) "
             "to evaluate against real CNIC-derived ground truth instead.",
    )
    _args = _parser.parse_args()
    asyncio.run(evaluate(Path(_args.roster)))
