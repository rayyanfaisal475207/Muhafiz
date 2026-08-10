"""
Read the shadow log — what the harness would have answered, next to what the
legacy pipeline actually said.

Shadow mode (config.HARNESS_SHADOW_MODE) records a sampled fraction of real
queries into `harness_shadow_runs` without ever showing the result to a user.
This is the other half: the tool that makes those rows readable, so a human can
decide whether the harness is ready to serve traffic.

WHAT TO LOOK FOR, IN ORDER OF WHAT WOULD BLOCK A CUTOVER
────────────────────────────────────────────────────────
  1. ERRORS — a shadow run that raised. This query shape would have FAILED for
     a real user had the harness been serving. Any error is a blocker.
  2. DISAGREEMENTS — one path answered and the other declined. Not necessarily
     a defect (the harness abstains deliberately where legacy would guess), but
     every one needs a human read before cutover.
  3. ABSTENTION RATE — how often the harness declines. A harness that abstains
     far more than legacy is safer but less useful; that trade is a product
     decision, not a technical one, and it needs to be made with a number.
  4. LATENCY — the harness runs more tools per query than most legacy routes.
     A large regression here is a user-visible cost.

This tool deliberately does NOT score answer quality. Nothing automatic can
judge whether an investigative answer is good; the `--verbose` output exists so
a person can read the two answers side by side and decide.

USAGE
    python scripts/compare_shadow_runs.py                  # summary
    python scripts/compare_shadow_runs.py --disagreements  # only the ones that matter
    python scripts/compare_shadow_runs.py --verbose        # full answers
    python scripts/compare_shadow_runs.py --limit 500
    python scripts/compare_shadow_runs.py --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from collections import Counter

from dotenv import load_dotenv

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

W = 88


def _pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):.0f}%" if total else "—"


def print_summary(rows: list[dict]) -> None:
    total = len(rows)
    print()
    print("=" * W)
    print("  AGENT HARNESS — SHADOW RUN COMPARISON")
    print("=" * W)
    if not total:
        print("  No shadow runs recorded.")
        print()
        print("  If that is unexpected, check in this order:")
        print("    * HARNESS_SHADOW_MODE=true in the environment the SERVER loaded")
        print("    * HARNESS_SHADOW_SAMPLE_RATE is above 0")
        print("    * traffic has actually hit an eligible route "
              "(HARNESS_SHADOW_ROUTES)")
        print("    * the server has served a query since the setting changed")
        print("=" * W)
        return

    errors = [r for r in rows if r.get("error")]
    disagree = [r for r in rows if r.get("routes_agree") is False]
    print(f"  {total} shadow run(s) recorded")
    print("=" * W)
    print()

    # ── The blocking numbers first ──
    print(f"  {'errors (would have failed a real user)':<48} "
          f"{len(errors):>4}   {_pct(len(errors), total):>5}")
    print(f"  {'outcome disagreements (one answered, one did not)':<48} "
          f"{len(disagree):>4}   {_pct(len(disagree), total):>5}")
    print()

    # ── Where the harness sent the work ──
    print("  Sub-agent handling")
    print("  " + "-" * (W - 4))
    for agent, n in Counter(
        r.get("harness_sub_agent") or "(none)" for r in rows
    ).most_common():
        print(f"    {agent:<32} {n:>5}   {_pct(n, total):>5}")
    print()

    # ── Routing: legacy's decision vs the harness's own ──
    print("  Legacy route -> harness sub-agent")
    print("  " + "-" * (W - 4))
    pairs = Counter(
        (r.get("legacy_route") or "?", r.get("harness_sub_agent") or "(none)")
        for r in rows
    )
    for (route, agent), n in pairs.most_common(12):
        print(f"    {route:<16} -> {agent:<28} {n:>5}")
    print()

    # ── Outcomes ──
    print("  Harness outcome")
    print("  " + "-" * (W - 4))
    statuses = Counter(r.get("harness_status") or "(none)" for r in rows)
    for status, n in statuses.most_common():
        note = ""
        if status == "abstained":
            note = "  <- declined rather than serve unverified prose"
        elif status == "partial":
            note = "  <- answered, with a stated gap"
        elif status == "denied":
            note = "  <- blocked by a role gate"
        print(f"    {status:<16} {n:>5}   {_pct(n, total):>5}{note}")
    print()

    # ── Evidence and cost ──
    cites = [r.get("citation_count") or 0 for r in rows]
    answered = [c for c in cites if c]
    durations = [r["duration_ms"] for r in rows if r.get("duration_ms")]
    print("  Evidence and cost")
    print("  " + "-" * (W - 4))
    print(f"    {'answers carrying citations':<32} "
          f"{len(answered):>5}   {_pct(len(answered), total):>5}")
    if answered:
        print(f"    {'median citations, when cited':<32} "
              f"{statistics.median(answered):>5.0f}")
    if durations:
        ordered = sorted(durations)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(f"    {'median shadow latency':<32} "
              f"{statistics.median(durations) / 1000:>5.1f}s")
        print(f"    {'p95 shadow latency':<32} {p95 / 1000:>5.1f}s")
    print()

    degraded = Counter(
        tool for r in rows for tool in (r.get("degraded_from") or [])
    )
    if degraded:
        print("  Tools attempted that did not contribute")
        print("  " + "-" * (W - 4))
        for tool, n in degraded.most_common():
            print(f"    {tool:<16} {n:>5}   {_pct(n, total):>5}")
        print()

    # ── The verdict, stated plainly ──
    print("=" * W)
    if errors:
        print(f"  BLOCKER: {len(errors)} shadow run(s) errored. These query shapes")
        print("  would have failed for a real user. Inspect with --disagreements.")
    elif disagree:
        print(f"  {len(disagree)} outcome disagreement(s) need a human read before")
        print("  cutover. Inspect with --disagreements --verbose.")
    else:
        print("  No errors and no outcome disagreements in this sample.")
        print("  Read a sample of answers with --verbose before drawing conclusions:")
        print("  agreeing on WHETHER to answer says nothing about answer quality.")
    print("=" * W)


def print_rows(rows: list[dict], verbose: bool) -> None:
    if not rows:
        print("\n  (no rows matched)\n")
        return
    print()
    for r in rows:
        print("-" * W)
        flag = "ERROR" if r.get("error") else (
            "DISAGREE" if r.get("routes_agree") is False else "ok"
        )
        print(f"  [{flag}]  {r.get('created_at') or ''}")
        print(f"  query        : {(r.get('original_query') or '')[:100]}")
        print(f"  case         : {r.get('case_id') or '(none)'}")
        print(f"  legacy       : route={r.get('legacy_route') or '?':<14} "
              f"outcome={r.get('legacy_outcome') or '?'}")
        print(f"  harness      : {r.get('harness_sub_agent') or '?':<20} "
              f"status={r.get('harness_status') or '?'}")
        if r.get("routing_basis"):
            print(f"  routing      : {r['routing_basis']}")
        print(f"  evidence     : {r.get('citation_count') or 0} citation(s), "
              f"tools={r.get('tools_used') or []}, "
              f"degraded={r.get('degraded_from') or []}")
        if r.get("caveats"):
            for c in r["caveats"]:
                print(f"  caveat       : {c[:100]}")
        if r.get("error"):
            print(f"  error        : {r['error'][:200]}")
        print(f"  took         : {(r.get('duration_ms') or 0) / 1000:.1f}s")
        if verbose and r.get("harness_answer"):
            print()
            print("  harness answer:")
            for line in r["harness_answer"].splitlines()[:20]:
                print(f"    {line[:100]}")
            extra = len(r["harness_answer"].splitlines()) - 20
            if extra > 0:
                print(f"    ... ({extra} more lines)")
        print()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--disagreements", action="store_true",
                        help="only rows that errored or disagreed")
    parser.add_argument("--verbose", action="store_true",
                        help="print each row, with the harness's full answer")
    parser.add_argument("--json", help="write the rows to this path")
    args = parser.parse_args()

    from src.data_gateway import get_gateway

    gateway = await get_gateway()
    rows = await gateway.get_harness_shadow_runs(
        limit=args.limit, only_disagreements=args.disagreements,
    )

    print_summary(rows)
    if args.disagreements or args.verbose:
        print_rows(rows, verbose=args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
        print(f"\n  wrote {len(rows)} row(s) to {args.json}")

    # Non-zero when something would block a cutover, so this can gate CI.
    return 1 if any(r.get("error") for r in rows) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
