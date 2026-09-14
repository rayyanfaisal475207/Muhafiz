"""
Module 79 — live runner for the chained-aggregate-plan fix.

Same login / SSE parse / `backend.log` slice machinery as
`scripts/module74_live_runs.py` (which Modules 75/76/89 also reused), with
one difference: the probe list is read from a JSON file (`M79_PROBES`),
so the paraphrase set written BEFORE the runs is the exact set measured.

The `XAGG <kind>: ...` log line is captured per run because XAGG's SSE
stream says only `route='XAGG'` — that line is the only evidence of which
aggregate answered and, after this module, of which filters it applied.
`Falling back to groq` lines are captured too, so the model that answered
each run is recorded rather than assumed.

Usage:
    GOLD32_BASE_URL=http://127.0.0.1:8079 \
    M79_LOG="D:/Rapids AI/muhafiz-m79/backend.log" \
    M79_PROBES=scratchpad/probes_before.json M79_OUT=scratchpad/before.json \
    M79_RUNS=3 M79_ARM=before python scripts/module79_live_runs.py
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
BASE = os.environ.get("GOLD32_BASE_URL", "http://127.0.0.1:8079")
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "MuhafizAdmin2026!")
LOG = os.environ.get("M79_LOG", os.path.join(HERE, "backend.log"))
OUT = os.environ.get("M79_OUT", os.path.join(HERE, "scratchpad", "module79_runs.json"))
PROBES_PATH = os.environ.get("M79_PROBES", "")
RUNS = int(os.environ.get("M79_RUNS", "1"))
ARM = os.environ.get("M79_ARM", "after")
IDS = [i for i in (os.environ.get("M79_IDS") or "").split(",") if i]
TIMEOUT_S = int(os.environ.get("GOLD32_TIMEOUT_S", "1200"))


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
_FALLBACK = re.compile(r"Falling back to (groq|gemini)[^\n]*", re.IGNORECASE)
_MODEL = re.compile(r"(local LLM|LOCAL_LLM|qwen3|qalb|groq|gemini)[^\n]{0,120}", re.IGNORECASE)
_QUOTA = re.compile(r"rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503", re.IGNORECASE)


def log_slice(path, start):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(start)
        return fh.read()


def log_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


_HTTP = re.compile(r"HTTP Request: POST (https?://[^ ]+)")
_VERIFIER = re.compile(r"(Structured-aggregate verifier: .*|verifier rejected paraphrase.*)$")
# Module 79: the plan / data-half / sub-query-deadline evidence lines.
_PLAN = re.compile(
    r"(Meta-Analysis: deterministic decomposition plan .*|Meta-Analysis: chained step .*|"
    r"Meta-Analysis: sub-query timed out .*|RAG tool: KB data-half .*|"
    r"RAG tool: compound legal-KB question .*)$"
)


def dissect(text):
    xagg, fallbacks, quota, hosts, verifier, plan = [], [], [], [], [], []
    for line in text.splitlines():
        m = _XAGG.search(line)
        if m:
            xagg.append(m.group(1)[:500])
        m = _PLAN.search(line)
        if m:
            plan.append(m.group(1)[:300])
        m = _FALLBACK.search(line)
        if m:
            fallbacks.append(m.group(0)[:200])
        m = _HTTP.search(line)
        if m:
            url = m.group(1)
            hosts.append("local:" + url.rsplit("/", 1)[-1] if "ngrok" in url else url.split("/")[2])
        m = _VERIFIER.search(line)
        if m:
            verifier.append(m.group(1)[:300])
        if _QUOTA.search(line):
            quota.append(line[:200])
    local = sorted({h for h in hosts if h.startswith("local:")})
    cloud = sorted({h for h in hosts if not h.startswith("local:")})
    if fallbacks or cloud:
        model = "cloud fallback (" + ", ".join(cloud or ["see fallback_lines"]) + ")" + (
            "; local calls: " + ", ".join(local) if local else "")
    else:
        model = "local model server (" + ", ".join(local) + ")" if local else "no LLM call logged"
    return {
        "xagg_log_lines": xagg,
        "fallback_lines": fallbacks,
        "llm_hosts": hosts,
        "model": model,
        "verifier_lines": verifier,
        "quota_lines": quota,
        "plan_lines": plan,
    }


def main():
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    probes = json.load(open(PROBES_PATH, encoding="utf-8")) if PROBES_PATH else {}
    for pid, spec in probes.items():
        if isinstance(spec, str):
            spec = {"question": spec}
        gold[pid] = {"id": pid, "question": spec["question"],
                     "answer": spec.get("expected", "(probe — no gold)"),
                     "language": spec.get("language", "en")}
    ids = IDS or list(probes)
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
            time.sleep(2)
            p.update(dissect(log_slice(LOG, off)))
            rec = {"id": qid, "run": run, "arm": ARM,
                   "language": item.get("language"), "question": item["question"],
                   "expected_answer": item["answer"], **p}
            out.append(rec)
            json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            sys.stdout.write(
                f"[{ARM} run{run}] {qid:8} route={str(p.get('route')):>9} "
                f"status={str(p.get('status')):>7} {p['elapsed_s']:>7}s "
                f"model={p['model']} xagg={[x[:60] for x in p['xagg_log_lines']]}\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
