"""
Modules 74/75/76 — live runner.

A thin extension of `scripts/module39_kb_runs.py` (same login / SSE parse /
log-slice machinery, unchanged) with two additions this track needs:

  1. `XAGG <kind>: ...` log-line capture. XAGG's SSE stream exposes only
     `route='XAGG'` and never which aggregate inside it ran, so per Module
     55 that line in `backend.log` is the ONLY evidence of which family
     answered — the fact these three modules are verified against.
  2. Free-text probes alongside the gold ids, so the three canned data-half
     sub-queries can be sent through the real `/api/chat` and prove the new
     aggregates survive the harness wrapper. That wrapper is where the
     `XAggToolResult` Literal trap lives, and no unit test reaches it.

Usage:
    GOLD32_BASE_URL=http://127.0.0.1:8025 \
    M74_LOG=D:/Rapids AI/muhafiz-m74/backend.log \
    M74_OUT=.../runs.json M74_RUNS=3 M74_IDS=KB3,KB8,KB9 \
    python scripts/module74_live_runs.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")
BASE = os.environ.get("GOLD32_BASE_URL", "http://127.0.0.1:8025")
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "MuhafizAdmin2026!")
LOG = os.environ.get("M74_LOG", os.path.join(HERE, "backend.log"))
OUT = os.environ.get("M74_OUT", os.path.join(HERE, "scratchpad", "module74_runs.json"))
RUNS = int(os.environ.get("M74_RUNS", "3"))
ARM = os.environ.get("M74_ARM", "after")
IDS = [i for i in (os.environ.get("M74_IDS") or "").split(",") if i]
TIMEOUT_S = int(os.environ.get("GOLD32_TIMEOUT_S", "1200"))

# ── The three canned data-half sub-queries, verbatim from tests/test_xagg.py.
#
# These are the strings a future one-entry addition to
# `rag.py::_KB_DATA_HALF_PLANS` would dispatch for KB3, KB8 and KB9. That
# file belongs to a live parallel track and is out of bounds for this one,
# so they are sent DIRECTLY here — which is what actually proves each new
# aggregate runs end to end through the harness, renders, and returns a
# non-empty answer.
PROBES = {
    "SQ74": (
        "How many cases record the same person as both the recording officer "
        "and the investigating officer, and how many split those roles, "
        "across all cases?"
    ),
    "SQ75": (
        "How many challans have been sent to court, and how many cases do "
        "they cover, across all cases?"
    ),
    "SQ76": (
        "How many FIRs cite PPC section 302, across all cases?"
    ),
    # Two rewordings of SQ76, measured because the first one is classified
    # SQL by the LLM router. That does NOT affect the composition path — a
    # `_KB_DATA_HALF_PLANS` sub-query is handed straight to `xagg_tool()`
    # and never sees the router (Module 39 §2.1) — but a sub-query that also
    # routes to XAGG on its own is strictly better, and the difference is
    # worth measuring rather than assuming.
    "SQ76B": (
        "How many cases cite PPC section 302 in their FIR sections, and how "
        "many FIRs cite each section, across all cases?"
    ),
    "SQ76C": (
        "Aggregate the FIR sections across all cases: how many FIRs cite "
        "each section, and how many cite PPC 302?"
    ),
}


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
    return {"actual_answer": a, "route": route, "status": status}


_XAGG = re.compile(r"(XAGG [a-z0-9_]+: .*)$")
_DH_ANSWER = re.compile(
    r"KB data-half plan '([^']+)' answered by aggregate '([^']+)' \((\d+) chars\)"
)
_VERDICT = re.compile(r"Evaluator: relevant=(True|False) — (.*)$")
_QUOTA = re.compile(
    r"rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503", re.IGNORECASE
)


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
    xagg, dh, verdicts, quota = [], [], [], []
    for line in text.splitlines():
        m = _XAGG.search(line)
        if m:
            xagg.append(m.group(1)[:400])
        m = _DH_ANSWER.search(line)
        if m:
            dh.append({"plan": m.group(1), "kind": m.group(2), "chars": int(m.group(3))})
        m = _VERDICT.search(line)
        if m:
            verdicts.append({"relevant": m.group(1) == "True", "reason": m.group(2)[:200]})
        if _QUOTA.search(line):
            quota.append(line[:200])
    return {
        "xagg_log_lines": xagg,
        "data_half_answered": dh,
        "evaluator_verdicts": verdicts,
        "quota_lines": quota,
    }


def main():
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    for pid, text in PROBES.items():
        gold[pid] = {"id": pid, "question": text, "answer": "(probe — no gold)",
                     "language": "en"}
    ids = IDS or list(PROBES)
    out = []
    if os.path.exists(OUT):
        out = json.load(open(OUT, encoding="utf-8"))
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
                p = {"actual_answer": "", "route": None, "status": "error",
                     "transport_ok": False, "error": f"{type(e).__name__}: {e}"}
            p["elapsed_s"] = round(time.time() - t0, 1)
            time.sleep(2)  # let the last log lines flush
            p.update(dissect(log_slice(LOG, off)))
            rec = {"id": qid, "run": run, "arm": ARM,
                   "language": item.get("language"), "question": item["question"],
                   "expected_answer": item["answer"], **p}
            out.append(rec)
            json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            sys.stdout.write(
                f"[{ARM} run{run}] {qid:5} route={str(p.get('route')):>9} "
                f"status={str(p.get('status')):>7} {p['elapsed_s']:>7}s "
                f"xagg={[x.split(':')[0] for x in p['xagg_log_lines']]}\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
