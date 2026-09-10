# -*- coding: utf-8 -*-
"""
Module 37 — live runner for the orphaned-`chunk_fulltext` deletion.

Adapted from scripts/module64_kb_runs.py, with three additions this module
cannot do without:

1. **Orphan marking.** Every chunk id that reaches the evaluator is checked
   against the orphan id set (`--orphans <json>`, the list this module
   derived from both stores). The claim being tested is *which chunks are
   served*, so this is the primary measurement, not the score.
2. **Generation provenance.** Module 101 §1.4: `call_llm()` is local-first and
   falls back to Groq/Gemini on ANY local failure, and cloud models cite
   differently — that invalidated its first eight runs. Every row records
   `generation` = ["local"] or ["cloud"], read from the `Local LLM failed: …
   Falling back to` lines inside this request's own log window.
3. **Cutover fallback.** Module 54's trap, re-found by Module 82 §4a: when the
   cutover classifier's provider 429s, `src.main` falls back to
   `orchestrator.py` and a DIFFERENT pipeline answers than the one being
   measured. Counted per run so contaminated rows can be dropped rather than
   noticed.

Usage:
    GOLD32_BASE_URL=http://127.0.0.1:8037 \
    M37_LOG=.../backend.log M37_OUT=.../runs.json \
    M37_RUNS=3 M37_ARM=before M37_IDS=KB2 \
    M37_ORPHANS=.../orphan_ids.json \
    PYTHONPATH=. python scripts/module37_kb_runs.py

M37_IDS defaults to all 32 gold questions (the regression control); pass a
comma-separated list for a focused arm.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")
BASE = os.environ.get("GOLD32_BASE_URL", "http://127.0.0.1:8037")
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "MuhafizAdmin2026!")
LOG = os.environ.get("M37_LOG", os.path.join(HERE, "backend.log"))
OUT = os.environ.get("M37_OUT", os.path.join(HERE, "module37_runs.json"))
RUNS = int(os.environ.get("M37_RUNS", "1"))
ARM = os.environ.get("M37_ARM", "before")
IDS = [i for i in (os.environ.get("M37_IDS") or "").split(",") if i]
ORPHANS_PATH = os.environ.get("M37_ORPHANS", "")
TIMEOUT_S = int(os.environ.get("GOLD32_TIMEOUT_S", "1200"))

ORPHANS: set[str] = set()
if ORPHANS_PATH and os.path.exists(ORPHANS_PATH):
    ORPHANS = set(json.load(open(ORPHANS_PATH, encoding="utf-8")))


def login():
    body = json.dumps({"email": EMAIL, "password": PW}).encode()
    req = urllib.request.Request(f"{BASE}/api/auth/login", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    r = urllib.request.urlopen(req, timeout=25)
    ck = r.headers.get_all("Set-Cookie") or []
    ac = next(re.search(r"access_token=([^;]+)", c).group(1) for c in ck if "access_token=" in c)
    cs = next(re.search(r"csrf_token=([^;]+)", c).group(1) for c in ck if "csrf_token=" in c)
    return ac, cs


def ask(q, ac, cs):
    body = json.dumps({"session_id": str(uuid.uuid4()), "message": q}).encode("utf-8")
    req = urllib.request.Request(f"{BASE}/api/chat", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Cookie", f"access_token={ac}; csrf_token={cs}")
    req.add_header("X-CSRF-Token", cs)
    return urllib.request.urlopen(req, timeout=TIMEOUT_S).read().decode("utf-8")


def parse(sse):
    ans, route, status = [], None, None
    for line in sse.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            d = json.loads(line[5:])
        except Exception:
            continue
        det = d.get("detail", "")
        if "route='" in str(det):
            m = re.search(r"route='([^']*)'", det)
            if m:
                route = m.group(1)
        if d.get("step") == "response":
            t = d.get("answer") or det
            if t and len(t) > 10:
                ans.append(t)
            status = d.get("status")
    a = " ".join(ans).strip()
    a = re.sub(r"^Writing the answer…\s*", "", a)
    a = re.sub(r"\s*Response generated.*$", "", a)
    return {"actual_answer": a, "last_dispatch_route": route, "status": status}


_ATTEMPT = re.compile(r"chunk\(s\) to evaluator \(attempt (\d+)\): (.*)$")
_VERDICT = re.compile(r"Evaluator: relevant=(True|False) — (.*)$")
_LOCAL_FAIL = re.compile(r"Local LLM failed: .*Falling back to")
_CUTOVER = re.compile(r"[Cc]utover.*fall(ing|s|back)|falling back to orchestrator", re.I)


def log_slice(path, start):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(start)
        return fh.read()


def log_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def dissect(text):
    attempts, verdicts, errors = [], [], []
    local_fail = cutover = 0
    for line in text.splitlines():
        m = _ATTEMPT.search(line)
        if m:
            ids = [c.split("[")[0] for c in m.group(2).split(", ") if c and c != "none"]
            attempts.append({
                "attempt": int(m.group(1)),
                "chunk_ids": ids,
                "orphans": [i for i in ids if i in ORPHANS],
            })
        m = _VERDICT.search(line)
        if m:
            verdicts.append({"relevant": m.group(1) == "True", "reason": m.group(2)[:300]})
        if _LOCAL_FAIL.search(line):
            local_fail += 1
        if _CUTOVER.search(line):
            cutover += 1
        if "RESOURCE_EXHAUSTED" in line or " 429" in line or "rate limit" in line.lower():
            errors.append(line[:200])
    all_ids = [i for a in attempts for i in a["chunk_ids"]]
    return {
        "evaluator_rounds": len(attempts),
        "attempts": attempts,
        "evaluator_verdicts": verdicts,
        # Module 101 §1.4 — a run whose generator was a CLOUD model is not
        # comparable to one the local model wrote.
        "generation": ["cloud"] if local_fail else ["local"],
        "local_llm_fallbacks": local_fail,
        "cutover_fallbacks": cutover,
        "quota_lines": errors,
        "window_chunk_ids": all_ids,
        "window_orphan_ids": [i for i in all_ids if i in ORPHANS],
        "n_orphans_in_window": sum(1 for i in all_ids if i in ORPHANS),
    }


def main():
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    ids = IDS or list(gold.keys())
    out = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else []
    done = {(o["id"], o["run"], o["arm"]) for o in out}
    ac, cs = login()
    for run in range(1, RUNS + 1):
        for qid in ids:
            if (qid, run, ARM) in done:
                continue
            item = gold[qid]
            off = log_size(LOG)
            t0 = time.time()
            try:
                p = parse(ask(item["question"], ac, cs))
                p["transport_ok"] = True
                p["error"] = None
            except Exception as e:  # noqa: BLE001
                p = {"actual_answer": "", "last_dispatch_route": None, "status": "error",
                     "transport_ok": False, "error": f"{type(e).__name__}: {e}"}
            p["elapsed_s"] = round(time.time() - t0, 1)
            time.sleep(2)
            p.update(dissect(log_slice(LOG, off)))
            out.append({"id": qid, "run": run, "arm": ARM,
                        "language": item.get("language"), "question": item["question"],
                        "expected_answer": item.get("answer"), **p})
            json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"[{ARM} run{run}] {qid:4} status={str(p.get('status')):>6} "
                  f"orphans={p['n_orphans_in_window']:2} gen={p['generation'][0]:5} "
                  f"len={len(p['actual_answer']):5} {p['elapsed_s']}s")
            sys.stdout.flush()
    print(f"wrote {len(out)} rows to {OUT}")


if __name__ == "__main__":
    main()
