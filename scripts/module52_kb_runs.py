"""
Module 52 — live KB1..KB9 runner.

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
    M52_LOG=D:/Rapids AI/muhafiz-m52/backend.log \
    M52_OUT=.../runs.json M52_RUNS=3 M52_ARM=before \
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
LOG = os.environ.get("M52_LOG", os.path.join(HERE, "backend.log"))
OUT = os.environ.get("M52_OUT", os.path.join(HERE, "module52_runs.json"))
RUNS = int(os.environ.get("M52_RUNS", "3"))
ARM = os.environ.get("M52_ARM", "before")
IDS = [i for i in (os.environ.get("M52_IDS") or "").split(",") if i]
TIMEOUT_S = int(os.environ.get("GOLD32_TIMEOUT_S", "1200"))

KB_IDS = ["KB1", "KB2", "KB3", "KB4", "KB5", "KB6", "KB8", "KB9"]

# A NON-GOLD Roman-Urdu paraphrase of KB6 (section 6 of the result-file
# shape). Deliberately keeps the hard vocabulary — "baramad shuda hathyar",
# a term with no English surface form — rather than Module 42's "zabt shuda
# pistol" loanword, which is the phrasing already known to pass. Its gold is
# KB6's gold: the same facts, asked in different words.
PARAPHRASE_ID = "KB6P"
PARAPHRASE = (
    "Kya forensics ke usoolon mein yeh wazeh kiya gaya hai ke baramad shuda "
    "hathyar ko record mein laane se pehle kis tarah sambhala aur band kiya "
    "jaye, aur kya hamare hathyar wale register se pata chalta hai ke aisa "
    "kiya gaya ya nahi?"
)


# A SECOND non-gold Roman-Urdu paraphrase. The first one turned out not to
# trip `_is_legal_kb_intent()` at all (measured: False) — "forensics ke
# usoolon" carries none of that gate's patterns, so it never reached the
# legal-KB path in either arm and cannot test Module 52. This one does trip
# it (measured: True) while keeping the same hard vocabulary.
PARAPHRASE2_ID = "KB6Q"
PARAPHRASE2 = (
    "Kya qanoon ya guidelines yeh kehti hain ke baramad shuda hathyar ko "
    "record mein laane se pehle kis tarah sambhala jaye, aur kya hamara "
    "register yeh darj karta hai ke aisa kiya gaya?"
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
        if "RESOURCE_EXHAUSTED" in line or "rate limit" in line.lower() or " 429" in line:
            errors.append(line[:200])
    return {
        "evaluator_rounds": len(attempts),
        "attempts": attempts,
        "evaluator_verdicts": verdicts,
        "english_rendering": english,
        "statute_hypotheses": statutes,
        "quota_lines": errors,
    }


def main():
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    gold[PARAPHRASE_ID] = {**gold["KB6"], "id": PARAPHRASE_ID, "question": PARAPHRASE}
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
                f"{p['elapsed_s']}s\n")
            sys.stdout.flush()
    print(f"wrote {len(out)} rows to {OUT}")


if __name__ == "__main__":
    main()
