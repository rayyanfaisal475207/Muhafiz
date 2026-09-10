"""
Module 39 — live KB1..KB9 runner.

A copy of `scripts/module52_kb_runs.py` (same login / SSE parse / log-slice
machinery, unchanged) with three extra recorders for the thing THIS module
has to measure: whether the compound data-half plan was dispatched, which
aggregate answered it, and whether it was dropped.

Runs the eight legal-KB gold questions through the live backend N times each,
recording route, status, runtime, the answer, and — read straight out of
backend.log for exactly the window each request occupied — the number of
retrieve/rerank/evaluate attempts, the chunk ids that reached the evaluator on
each attempt, and every evaluator verdict.

Chunk IDS only, never citations: Module 30 recorded an evaluator citing a
section that was not in the chunks it judged, so the answer text cannot be
trusted to say what was actually read.

Usage:
    GOLD32_BASE_URL=http://127.0.0.1:8018 \
    M39_LOG=D:/Rapids AI/muhafiz-m52/backend.log \
    M39_OUT=.../runs.json M39_RUNS=3 M39_ARM=before \
    python scripts/module52_kb_runs.py
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
BASE = os.environ.get("GOLD32_BASE_URL", "http://127.0.0.1:8018")
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "MuhafizAdmin2026!")
LOG = os.environ.get("M39_LOG", os.path.join(HERE, "backend.log"))
OUT = os.environ.get("M39_OUT", os.path.join(HERE, "module39_runs.json"))
RUNS = int(os.environ.get("M39_RUNS", "3"))
ARM = os.environ.get("M39_ARM", "before")
IDS = [i for i in (os.environ.get("M39_IDS") or "").split(",") if i]
TIMEOUT_S = int(os.environ.get("GOLD32_TIMEOUT_S", "1200"))

KB_IDS = ["KB1", "KB2", "KB3", "KB4", "KB5", "KB6", "KB8", "KB9"]

# ── Module 39's own non-gold paraphrases ────────────────────────────────
#
# Written BEFORE the live sweeps and not adjusted afterwards. They test the
# thing this module could most easily have curve-fitted: whether the
# compound data-half plans are tied to the gold questions' wording or to
# their SUBJECT. Both deliberately move away from the gold text — KB4P is
# Roman-Urdu where gold KB4 is Urdu script and uses "maal khana" where gold
# says "پراپرٹی ریکارڈ"; KB6P is English where gold KB6 is Roman-Urdu, and
# says "firearm"/"packed" where gold says "aslaha"/"sambhala".
PARAPHRASE_ID = "KB4P"
PARAPHRASE = (
    "Kya koi qanooni zabta maujood hai ke police jo maal apni tehveel mein "
    "le, usay kaise register kiya jaye aur baad mein kaise tehleel kiya jaye "
    "— aur kya hamara maal khana record us par amal karta hai?"
)

PARAPHRASE2_ID = "KB6P"
PARAPHRASE2 = (
    "Do the forensics guidelines set any rule for how a recovered firearm "
    "must be packed before it is entered in the record, and does our own "
    "weapon register show whether that was done?"
)


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


_ATTEMPT = re.compile(r"chunk\(s\) to evaluator \(attempt (\d+)\): (.*)$")
_VERDICT = re.compile(r"Evaluator: relevant=(True|False) — (.*)$")
_ENGLISH = re.compile(r"English rendering of the question: (.*)$")
# [Module 39] The log lines rag.py emits on the compound path.
_DH_DISPATCH = re.compile(
    r"compound legal-KB question . dispatching data-half plan '([^']+)' \(([^)]*)\)"
)
_DH_ANSWER = re.compile(
    r"KB data-half plan '([^']+)' answered by aggregate '([^']+)' \((\d+) chars\)"
)
_DH_DROP = re.compile(
    r"KB data-half (?:plan|aggregate) '([^']+)' (expected|returned|failed|timed out)"
)
_STATUTE = re.compile(r"statute-hypothesis query: (.*)$")


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
    attempts, verdicts, english, statutes, errors = [], [], [], [], []
    dh_dispatched, dh_answered, dh_dropped = [], [], []
    for line in text.splitlines():
        m = _ATTEMPT.search(line)
        if m:
            ids = [c.split("[")[0] for c in m.group(2).split(", ") if c and c != "none"]
            attempts.append({"attempt": int(m.group(1)), "chunk_ids": ids})
        m = _VERDICT.search(line)
        if m:
            verdicts.append({"relevant": m.group(1) == "True", "reason": m.group(2)[:300]})
        m = _ENGLISH.search(line)
        if m:
            english.append(m.group(1)[:300])
        m = _STATUTE.search(line)
        if m:
            statutes.append(m.group(1)[:200])
        m = _DH_DISPATCH.search(line)
        if m:
            dh_dispatched.append({"plan": m.group(1), "expected_kind": m.group(2)})
        m = _DH_ANSWER.search(line)
        if m:
            dh_answered.append({"plan": m.group(1), "kind": m.group(2),
                                "chars": int(m.group(3))})
        if _DH_DROP.search(line):
            dh_dropped.append(line[-220:])
        if "RESOURCE_EXHAUSTED" in line or "rate limit" in line.lower() or " 429" in line:
            errors.append(line[:200])
    return {
        "evaluator_rounds": len(attempts),
        "attempts": attempts,
        "evaluator_verdicts": verdicts,
        "english_rendering": english,
        "statute_hypotheses": statutes,
        "quota_lines": errors,
        "data_half_dispatched": dh_dispatched,
        "data_half_answered": dh_answered,
        "data_half_dropped": dh_dropped,
    }


def main():
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    gold[PARAPHRASE_ID] = {**gold["KB4"], "id": PARAPHRASE_ID, "question": PARAPHRASE}
    gold[PARAPHRASE2_ID] = {**gold["KB6"], "id": PARAPHRASE2_ID, "question": PARAPHRASE2}
    ids = IDS or KB_IDS
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
            rec = {"id": qid, "run": run, "arm": ARM, "language": item["language"],
                   "question": item["question"], "expected_answer": item["answer"], **p}
            out.append(rec)
            json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            sys.stdout.write(
                f"[{ARM} run{run}] {qid:4} route={str(p.get('route')):>8} "
                f"rounds={p['evaluator_rounds']} len={len(p['actual_answer']):5} "
                f"dh={'+'.join(d['kind'] for d in (p.get('data_half_answered') or [])) or '-'} "
                f"{p['elapsed_s']}s\n")
            sys.stdout.flush()
    print(f"wrote {len(out)} rows to {OUT}")


if __name__ == "__main__":
    main()
