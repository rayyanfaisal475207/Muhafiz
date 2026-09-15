# -*- coding: utf-8 -*-
"""
Module 151 — live `/api/chat` runner (adapted from scripts/module37_kb_runs.py).

Records per run: wall time, dispatch route, status, the served answer, the
evaluator rounds and verdicts read from this request's own `backend.log`
window, whether the Module 151 retry gate fired ("zero-signal first pass"),
the gate's own verdict line, and — Module 101 §1.4 — which model answered
(`generation` = local | cloud, read from `Falling back to` lines).

Usage:
    M151_BASE=http://127.0.0.1:8151 M151_LOG=backend.log \
    M151_OUT=scratchpad/m151_live.json M151_RUNS=3 M151_ARM=after \
    M151_IDS=M143,P1 PYTHONPATH=. python -X utf8 scripts/module143_live_runs.py

M151_IDS defaults to all 32 gold questions (the regression control). Ids not
in the gold file are looked up in EXTRA (this module's own probes).
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
BASE = os.environ.get("M151_BASE", "http://127.0.0.1:8151")
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "MuhafizAdmin2026!")
LOG = os.environ.get("M151_LOG", os.path.join(HERE, "backend.log"))
OUT = os.environ.get("M151_OUT", os.path.join(HERE, "scratchpad", "m151_live.json"))
RUNS = int(os.environ.get("M151_RUNS", "1"))
ARM = os.environ.get("M151_ARM", "before")
IDS = [i for i in (os.environ.get("M151_IDS") or "").split(",") if i]
TIMEOUT_S = int(os.environ.get("M151_TIMEOUT_S", "1500"))

EXTRA = {
    # Module 151 §6 non-gold paraphrases — written BEFORE any live run.
    "KB2-P1": "Our witness records only hold names, CNICs and addresses — why is there nowhere to store what the witness actually told the police? Is that something we should be capturing?",
    "KB5-P1": "Aurat par tashaddud wale case mein qanoon jo khaas iqdamaat maangta hai, kya hamare women-violence report records mein woh sab darj hote hain, ya kuch cheezein record hi nahi hotin?",
    "KB9-P1": "When someone dies in suspicious circumstances the law requires a formal inquiry into the cause of death — does our system have any field or record for that inquiry, given how many of our cases involve a death?",
    # Retained Module 143 probes (unused unless named).
    "M143": "How often is the officer who registers an FIR also the officer who investigates the case?",
    # §6 paraphrases — written before any live run, see MODULE143_RESULT.md §6.
    "M143-P1": "In what proportion of our cases does the same officer both register the FIR and carry out the investigation?",
    "M143-P2": "Hamare cases mein kitni dafa aisa hota hai ke FIR likhne wala afsar hi tafteesh bhi karta hai?",
    # Phase 1 plain-path controls.
    "P1": "What weapon was recovered from the accused in FIR 1001/26, and who was the complainant?",
    "N1": "Tell me about the armed robbery reported from Iqbal Town where a pistol was seized.",
    "N2": "Kashif naam ke mulzim ke qabze se kya baramad hua tha?",
    "Z2": "What is the average number of days between an FIR's registration and its first zimni entry?",
    "Z3": "Which police station has the highest conviction rate this year?",
    "Z4": "Kitni baar FIR darj karne wala afsar hi us case ki tafteesh bhi karta hai?",
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
    ans, route, status, evaluator_details = [], None, None, []
    for line in sse.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            d = json.loads(line[5:])
        except Exception:  # noqa: BLE001
            continue
        det = d.get("detail", "")
        if "route='" in str(det):
            m = re.search(r"route='([^']*)'", det)
            if m:
                route = m.group(1)
        if d.get("step") == "evaluator":
            evaluator_details.append(f"{d.get('status')}:{det}")
        if d.get("step") == "response":
            t = d.get("answer") or det
            if t and len(t) > 10:
                ans.append(t)
            status = d.get("status")
    a = " ".join(ans).strip()
    a = re.sub(r"^Writing the answer…\s*", "", a)
    a = re.sub(r"\s*Response generated.*$", "", a)
    return {"actual_answer": a, "last_dispatch_route": route, "status": status,
            "evaluator_events": evaluator_details}


_ATTEMPT = re.compile(r"chunk\(s\) to evaluator \(attempt (\d+)\): (.*)$")
_VERDICT = re.compile(r"Evaluator: relevant=(True|False) — (.*)$")
_GATE = re.compile(r"Retry gate: retry_could_help=(True|False) — (.*)$")
_ZERO = re.compile(r"zero-signal first pass")
_VERIFIER = re.compile(r"Verifier: grounded=(True|False) off_topic=(True|False) leaked=(\S+) unsupported=(\d+) — (.*)$")
_REJECTED = re.compile(r"verifier rejected answer: (.*)$")
_M151 = re.compile(r"Verifier \[Module 151\]: (.*)$")
_SCHEMA_CLS = re.compile(r"Schema-absence classifier: (.*)$")
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
    attempts, verdicts, gate, errors = [], [], [], []
    verifier, rejected, m151, schema_cls = [], [], [], []
    local_fail = cutover = zero = 0
    for line in text.splitlines():
        m = _ATTEMPT.search(line)
        if m:
            attempts.append({"attempt": int(m.group(1)),
                             "chunk_ids": [c.split("[")[0] for c in m.group(2).split(", ") if c and c != "none"]})
        m = _VERDICT.search(line)
        if m:
            verdicts.append({"relevant": m.group(1) == "True", "reason": m.group(2)[:400]})
        m = _GATE.search(line)
        if m:
            gate.append({"retry_could_help": m.group(1) == "True", "reason": m.group(2)[:200]})
        if _ZERO.search(line):
            zero += 1
        m = _VERIFIER.search(line)
        if m:
            verifier.append({"grounded": m.group(1) == "True", "off_topic": m.group(2) == "True",
                             "unsupported": int(m.group(4)), "reason": m.group(5)[:400]})
        m = _REJECTED.search(line)
        if m:
            rejected.append(m.group(1)[:400])
        m = _M151.search(line)
        if m:
            m151.append(m.group(1)[:600])
        m = _SCHEMA_CLS.search(line)
        if m:
            schema_cls.append(m.group(1)[:600])
        if _LOCAL_FAIL.search(line):
            local_fail += 1
        if _CUTOVER.search(line):
            cutover += 1
        if "RESOURCE_EXHAUSTED" in line or " 429" in line or "rate limit" in line.lower():
            errors.append(line[:200])
    return {
        "evaluator_rounds": len(attempts),
        "attempts": attempts,
        "evaluator_verdicts": verdicts,
        "retry_gate": gate,
        "verifier": verifier,
        "verifier_rejections": rejected,
        "module151_lines": m151,
        "schema_classifier_lines": schema_cls,
        "zero_signal_abstention": zero > 0,
        "generation": ["cloud"] if local_fail else ["local"],
        "local_llm_fallbacks": local_fail,
        "cutover_fallbacks": cutover,
        "quota_lines": errors,
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
            if qid in gold:
                question, expected, lang = gold[qid]["question"], gold[qid].get("answer"), gold[qid].get("language")
            else:
                question, expected, lang = EXTRA[qid], None, None
            off = log_size(LOG)
            t0 = time.time()
            try:
                p = parse(ask(question, ac, cs))
                p["transport_ok"] = True
                p["error"] = None
            except Exception as e:  # noqa: BLE001
                p = {"actual_answer": "", "last_dispatch_route": None, "status": "error",
                     "evaluator_events": [], "transport_ok": False,
                     "error": f"{type(e).__name__}: {e}"}
            p["elapsed_s"] = round(time.time() - t0, 1)
            time.sleep(2)
            p.update(dissect(log_slice(LOG, off)))
            out.append({"id": qid, "run": run, "arm": ARM, "language": lang,
                        "question": question, "expected_answer": expected, **p})
            os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
            json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            v = "".join("T" if x["relevant"] else "F" for x in p["evaluator_verdicts"])
            print(f"[{ARM} run{run}] {qid:8} route={str(p.get('last_dispatch_route')):9} "
                  f"status={str(p.get('status')):6} verdicts={v:6} gate={p['zero_signal_abstention']!s:5} "
                  f"gen={p['generation'][0]:5} verif={''.join('G' if v['grounded'] else 'X' for v in p['verifier']):4} m151={len(p['module151_lines'])} "
                  f"len={len(p['actual_answer']):5} {p['elapsed_s']}s", flush=True)
    print(f"wrote {len(out)} rows to {OUT}")


if __name__ == "__main__":
    main()
