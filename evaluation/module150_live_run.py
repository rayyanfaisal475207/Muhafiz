# -*- coding: utf-8 -*-
"""
Module 150 — G1 / G6 (and any other Meta-Analysis question) live, in process,
with the per-sub-query and per-LLM-call timeline this module has to report.

What is recorded per run that no existing runner records:

  1. **EVERY local-model call**: start/end offset from the moment the
     question was asked, duration, role, outcome (ok / exception type), and
     WHICH SUB-QUERY it belonged to. `src.llm.client._post_local` is wrapped
     (not edited) and a contextvar set by a wrapper around
     `meta_analysis._dispatch_one` names the owning sub-query — each
     `asyncio.gather` child runs in its own Task with a copied context, so
     the name follows the awaits down to the HTTP call.
  2. **EVERY sub-query's** own start/end/duration and outcome.
  3. The `Local LLM failed: <msg>. Falling back to ...` warnings, with the
     exact (possibly empty) message and their offsets — the metric this
     module exists for — plus the sub-query-timeout / salvage lines.

No backend is started: reuses `module92_inprocess_run.run_one()` verbatim, as
Modules 110, 116 and 143 did, for the same reason (a machine-wide cap of 2
live backends, one of which another track holds).

    PYTHONPATH=. python -X utf8 evaluation/module150_live_run.py --questions G6 --runs 3 --tag before
    PYTHONPATH=. python -X utf8 evaluation/module150_live_run.py --text "..." --runs 1 --tag para
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import io
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module150_live"
GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"

_WATCH = {
    "local_llm_failed": "Local LLM failed",
    "subquery_timeout": "sub-query timed out after",
    "salvaged": "serving it raw",
    "plan_matched": "deterministic decomposition plan",
    "synthesis_collapsed": "synthesis collapsed into a repetition loop",
    "regeneration_recovered": "regeneration recovered a usable synthesis",
    "verifier_rejected_synthesis": "verifier rejected synthesized answer",
}

_CURRENT_SUBQUERY: contextvars.ContextVar[str] = contextvars.ContextVar("m150_subquery", default="<top-level>")


class _Capture(logging.Handler):
    def __init__(self, t0: float) -> None:
        super().__init__(level=logging.INFO)
        self.t0 = t0
        self.lines: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if any(v in msg for v in _WATCH.values()):
            self.lines.append({"t_s": round(time.monotonic() - self.t0, 2),
                               "sub_query": _CURRENT_SUBQUERY.get(),
                               "line": f"{record.levelname} {record.name}: {msg[:300]}"})


class _Instrument:
    """Wraps `_post_local` and `_dispatch_one` for one run; restores on exit."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.llm_calls: list[dict] = []
        self.sub_queries: list[dict] = []

    def __enter__(self):
        from src.llm import client as llm_client
        from src.pipeline.harness.agents import meta_analysis as ma
        self._client, self._ma = llm_client, ma
        self._orig_post, self._orig_dispatch = llm_client._post_local, ma._dispatch_one
        inst = self

        async def timed_post(system_prompt, user_message, temperature, max_tokens, role):
            start = time.monotonic() - inst.t0
            rec = {"sub_query": _CURRENT_SUBQUERY.get(), "role": role, "max_tokens": max_tokens,
                   "start_s": round(start, 2)}
            try:
                out = await inst._orig_post(system_prompt, user_message, temperature, max_tokens, role)
                rec["ok"] = True
                rec["chars"] = len(out or "")
                return out
            except BaseException as exc:  # noqa: BLE001 — record cancellation too
                rec["ok"] = False
                rec["error"] = f"{type(exc).__name__}: {exc!r}"[:200]
                raise
            finally:
                end = time.monotonic() - inst.t0
                rec["end_s"] = round(end, 2)
                rec["duration_s"] = round(end - start, 2)
                inst.llm_calls.append(rec)

        async def timed_dispatch(sub_query, agent_input, on_event, gateway, *args, **kwargs):
            token = _CURRENT_SUBQUERY.set(sub_query[:90])
            start = time.monotonic() - inst.t0
            rec = {"sub_query": sub_query[:90], "start_s": round(start, 2)}
            try:
                outcome = await inst._orig_dispatch(sub_query, agent_input, on_event, gateway, *args, **kwargs)
                res = outcome.result
                rec["status"] = str(getattr(res, "status", None)) if res is not None else None
                rec["failure_reason"] = outcome.failure_reason
                rec["salvaged"] = bool(getattr(outcome, "salvaged", False))
                rec["tools_used"] = [str(t) for t in (getattr(res, "tools_used", None) or [])]
                return outcome
            finally:
                end = time.monotonic() - inst.t0
                rec["end_s"] = round(end, 2)
                rec["duration_s"] = round(end - start, 2)
                inst.sub_queries.append(rec)
                _CURRENT_SUBQUERY.reset(token)

        llm_client._post_local = timed_post
        ma._dispatch_one = timed_dispatch
        return self

    def __exit__(self, *exc):
        self._client._post_local = self._orig_post
        self._ma._dispatch_one = self._orig_dispatch
        return False


def _load(p: Path):
    return json.load(io.open(p, encoding="utf-8"))


def _summarise(inst: _Instrument, cap: _Capture) -> dict:
    fallbacks = [ln for ln in cap.lines if _WATCH["local_llm_failed"] in ln["line"]]
    local_ok = [c for c in inst.llm_calls if c["ok"]]
    local_failed = [c for c in inst.llm_calls if not c["ok"]]
    per_call = [c["duration_s"] for c in local_ok]
    return {
        "llm_calls_total": len(inst.llm_calls),
        "local_ok": len(local_ok),
        "local_failed": len(local_failed),
        "local_failed_errors": sorted({c["error"].split(":")[0] for c in local_failed}),
        "groq_fallbacks": len(fallbacks),
        "fallback_messages": [ln["line"].split("Local LLM failed: ", 1)[-1][:80] for ln in fallbacks],
        "subquery_timeouts": sum(1 for ln in cap.lines if _WATCH["subquery_timeout"] in ln["line"]),
        "salvaged": sum(1 for ln in cap.lines if _WATCH["salvaged"] in ln["line"]),
        "per_call_s_min": min(per_call) if per_call else None,
        "per_call_s_mean": round(sum(per_call) / len(per_call), 1) if per_call else None,
        "per_call_s_max": max(per_call) if per_call else None,
        "sub_query_count": len(inst.sub_queries),
        "fanout_wall_s": (round(max(s["end_s"] for s in inst.sub_queries) - min(s["start_s"] for s in inst.sub_queries), 1)
                          if inst.sub_queries else None),
        "model": "groq-fallback" if fallbacks else "local",
        "synthesis_collapsed": sum(1 for ln in cap.lines if _WATCH["synthesis_collapsed"] in ln["line"]),
        "verifier_rejected_synthesis": sum(1 for ln in cap.lines if _WATCH["verifier_rejected_synthesis"] in ln["line"]),
    }


def _print_timeline(inst: _Instrument) -> None:
    for s in sorted(inst.sub_queries, key=lambda x: x["start_s"]):
        calls = [c for c in inst.llm_calls if c["sub_query"] == s["sub_query"]]
        cs = " ".join(f"[{c['start_s']:.0f}->{c['end_s']:.0f} {'ok' if c['ok'] else c['error'].split(':')[0]}]" for c in calls)
        print(f"      {s['start_s']:6.1f}->{s['end_s']:6.1f} ({s['duration_s']:5.1f}s) {s['status'] or s['failure_reason']:>22} "
              f"{'SALVAGED ' if s.get('salvaged') else ''}{s['sub_query'][:48]!r} calls={cs}", flush=True)


async def main_async(a) -> None:
    from evaluation.module92_inprocess_run import run_one  # noqa: WPS433
    from src import config

    if a.text:
        items = [{"id": a.id or "CUSTOM", "text": a.text, "language": "en"}]
    else:
        gold = {g["id"]: g for g in _load(GOLD_PATH)}
        qids = [q.strip() for q in a.questions.split(",") if q.strip()]
        items = [{"id": q, "text": gold[q]["question"], "language": gold[q]["language"]} for q in qids]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"module150_{a.tag}.json"
    results = _load(path) if path.exists() else {}
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    print(f"LOCAL_LLM_TIMEOUT={config.LOCAL_LLM_TIMEOUT} META_ANALYSIS_SUBQUERY_TIMEOUT={config.META_ANALYSIS_SUBQUERY_TIMEOUT} "
          f"max_concurrent={getattr(config, 'META_ANALYSIS_MAX_CONCURRENT_SUBQUERIES', 'n/a')}", flush=True)

    for it in items:
        for run in range(1, a.runs + 1):
            key = f"{it['id']}::run{run}"
            if key in results and not a.force:
                continue
            print(f"{key} ...", flush=True)
            with _Instrument() as inst:
                cap = _Capture(inst.t0)
                root.addHandler(cap)
                try:
                    r = await run_one(it["text"])
                except Exception as exc:  # noqa: BLE001
                    r = {"route": None, "route_error": f"{type(exc).__name__}: {exc}",
                         "answer": "", "steps": [], "status": "error", "elapsed_s": 0.0}
                finally:
                    root.removeHandler(cap)
                r["wall_s"] = round(time.monotonic() - inst.t0, 1)
            r["question"] = it["text"]
            r["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            r["signals"] = _summarise(inst, cap)
            r["sub_queries"] = sorted(inst.sub_queries, key=lambda x: x["start_s"])
            r["llm_calls"] = sorted(inst.llm_calls, key=lambda x: x["start_s"])
            r["log_lines"] = cap.lines
            r["config"] = {"LOCAL_LLM_TIMEOUT": config.LOCAL_LLM_TIMEOUT,
                           "META_ANALYSIS_SUBQUERY_TIMEOUT": config.META_ANALYSIS_SUBQUERY_TIMEOUT,
                           "META_ANALYSIS_MAX_CONCURRENT_SUBQUERIES": getattr(config, "META_ANALYSIS_MAX_CONCURRENT_SUBQUERIES", None)}
            results[key] = r
            s = r["signals"]
            print(f"    route={r['route']} status={r['status']} wall={r['wall_s']}s fanout={s['fanout_wall_s']}s "
                  f"model={s['model']} fallbacks={s['groq_fallbacks']} local_ok={s['local_ok']} "
                  f"per_call={s['per_call_s_min']}/{s['per_call_s_mean']}/{s['per_call_s_max']}s "
                  f"sq_timeouts={s['subquery_timeouts']} answer={len(r['answer'])} chars", flush=True)
            _print_timeline(inst)
            with io.open(path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"\nwrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default="G6")
    ap.add_argument("--text", default=None, help="a free-text question instead of gold ids")
    ap.add_argument("--id", default=None)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--force", action="store_true")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    main()
