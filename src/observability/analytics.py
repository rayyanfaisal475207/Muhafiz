"""
Analytics — turns raw rows into the series the dashboard charts.

Aggregation lives here (not in the gateway) for two reasons:

* Keeping the maths out of the gateway keeps `DirectGateway` a thin SQL layer —
  aggregation logic doesn't need to know about connection pooling or sessions.
* Percentiles over a few thousand rows are trivial in Python. Pushing them into
  SQL would mean writing — and keeping in sync — two dialects of the same query.

Everything here is pure: rows in, chart-ready series out.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        cleaned = str(ts).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def since_iso(days: int) -> str:
    """The cutoff timestamp for a `days`-long window, as the gateways want it."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _bucket_key(dt: datetime, granularity: str) -> str:
    if granularity == "hour":
        return dt.strftime("%Y-%m-%dT%H:00")
    return dt.strftime("%Y-%m-%d")


def _empty_buckets(days: int, granularity: str) -> list[str]:
    """
    Pre-seed every bucket in the window so a quiet day plots as zero rather than
    vanishing — a gap in a line chart reads as "no data", which is a different
    claim from "nothing happened".
    """
    now = datetime.now(timezone.utc)
    if granularity == "hour":
        start = now - timedelta(hours=days * 24)
        step = timedelta(hours=1)
        count = days * 24
    else:
        start = now - timedelta(days=days)
        step = timedelta(days=1)
        count = days
    return [_bucket_key(start + step * i, granularity) for i in range(count + 1)]


def percentiles(values: list[float]) -> dict:
    """avg / p50 / p95 / max over a list of durations (ms)."""
    clean = [v for v in values if isinstance(v, (int, float)) and v >= 0]
    if not clean:
        return {"count": 0, "avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "max_ms": 0}
    ordered = sorted(clean)
    # Nearest-rank p95 — with small samples this is honest, where an
    # interpolated percentile would invent a value nobody actually experienced.
    idx = max(0, min(len(ordered) - 1, int(round(0.95 * len(ordered))) - 1))
    return {
        "count": len(ordered),
        "avg_ms": int(statistics.fmean(ordered)),
        "p50_ms": int(statistics.median(ordered)),
        "p95_ms": int(ordered[idx]),
        "max_ms": int(ordered[-1]),
    }


def usage_timeseries(runs: list[dict], days: int, granularity: str = "day") -> list[dict]:
    """Requests over time, split by route, with empty buckets filled in."""
    buckets: dict[str, Counter] = defaultdict(Counter)
    for run in runs:
        dt = _parse(run.get("created_at"))
        if not dt:
            continue
        key = _bucket_key(dt, granularity)
        buckets[key]["total"] += 1
        buckets[key][(run.get("routed_to") or "UNKNOWN").upper()] += 1

    routes = sorted({r for counter in buckets.values() for r in counter if r != "total"})
    series = []
    for key in _empty_buckets(days, granularity):
        counter = buckets.get(key, Counter())
        point = {"bucket": key, "total": counter.get("total", 0)}
        for route in routes:
            point[route] = counter.get(route, 0)
        series.append(point)
    return series


def routing_breakdown(runs: list[dict]) -> list[dict]:
    """Counts and proportions per route, plus how often each one succeeded."""
    total = len(runs)
    by_route: dict[str, dict] = defaultdict(lambda: {"count": 0, "success": 0, "durations": []})

    for run in runs:
        route = (run.get("routed_to") or "UNKNOWN").upper()
        entry = by_route[route]
        entry["count"] += 1
        outcome = run.get("final_outcome")
        # "safe" is the fallback answer — a completed run, but not a win.
        if outcome and outcome != "safe":
            entry["success"] += 1
        if run.get("total_duration_ms"):
            entry["durations"].append(run["total_duration_ms"])

    out = []
    for route, entry in by_route.items():
        stats = percentiles(entry["durations"])
        out.append({
            "route": route,
            "count": entry["count"],
            "share_pct": round(entry["count"] / total * 100, 1) if total else 0,
            "success_pct": round(entry["success"] / entry["count"] * 100, 1) if entry["count"] else 0,
            "avg_ms": stats["avg_ms"],
            "p95_ms": stats["p95_ms"],
        })
    return sorted(out, key=lambda r: r["count"], reverse=True)


def latency_summary(runs: list[dict]) -> dict:
    return percentiles([r.get("total_duration_ms") for r in runs])


def latency_timeseries(runs: list[dict], days: int, granularity: str = "day") -> list[dict]:
    """Latency trend: avg and p95 per bucket."""
    buckets: dict[str, list] = defaultdict(list)
    for run in runs:
        dt = _parse(run.get("created_at"))
        if not dt or not run.get("total_duration_ms"):
            continue
        buckets[_bucket_key(dt, granularity)].append(run["total_duration_ms"])

    series = []
    for key in _empty_buckets(days, granularity):
        stats = percentiles(buckets.get(key, []))
        series.append({
            "bucket": key,
            "avg_ms": stats["avg_ms"],
            "p95_ms": stats["p95_ms"],
            "count": stats["count"],
        })
    return series


def latency_by_step(steps: list[dict]) -> list[dict]:
    """
    Which part of the pipeline is slow. This is the one that actually answers
    "why are we slow" — the total tells you *that* you are.
    """
    by_step: dict[str, list] = defaultdict(list)
    failures: Counter = Counter()
    for step in steps:
        name = step.get("step_name") or "unknown"
        if step.get("status") == "failed":
            failures[name] += 1
        if step.get("duration_ms"):
            by_step[name].append(step["duration_ms"])

    out = []
    for name, durations in by_step.items():
        stats = percentiles(durations)
        out.append({
            "step_name": name,
            "count": stats["count"],
            "avg_ms": stats["avg_ms"],
            "p50_ms": stats["p50_ms"],
            "p95_ms": stats["p95_ms"],
            "max_ms": stats["max_ms"],
            "failures": failures.get(name, 0),
        })
    return sorted(out, key=lambda s: s["avg_ms"], reverse=True)


def error_timeseries(errors: list[dict], days: int, granularity: str = "day") -> list[dict]:
    """Errors over time, split by severity."""
    buckets: dict[str, Counter] = defaultdict(Counter)
    for err in errors:
        dt = _parse(err.get("occurred_at"))
        if not dt:
            continue
        key = _bucket_key(dt, granularity)
        buckets[key]["total"] += 1
        buckets[key][err.get("severity") or "error"] += 1

    series = []
    for key in _empty_buckets(days, granularity):
        counter = buckets.get(key, Counter())
        series.append({
            "bucket": key,
            "total": counter.get("total", 0),
            "warning": counter.get("warning", 0),
            "error": counter.get("error", 0),
            "critical": counter.get("critical", 0),
        })
    return series


def verifier_stats(runs: list[dict]) -> dict:
    """Pass/fail stats for the Phase 6 grounding verifier."""
    total_gated = 0
    passed = 0
    regenerated = 0
    
    for run in runs:
        if run.get("verifier_passed") is not None:
            total_gated += 1
            if run["verifier_passed"]:
                passed += 1
        if run.get("verifier_regenerated"):
            regenerated += 1
            
    return {
        "total_gated": total_gated,
        "passed": passed,
        "regenerated": regenerated,
        "pass_rate_pct": round((passed / total_gated) * 100, 1) if total_gated else 0.0,
        "regeneration_rate_pct": round((regenerated / total_gated) * 100, 1) if total_gated else 0.0,
    }
