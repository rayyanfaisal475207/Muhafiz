"""
Module 71 — live runner.

`scripts/module74_live_runs.py` with two additions this track needs:

  1. Verifier / Meta-Analysis rejection-reason capture. The defect this
     module exists to fix is a `status=error` produced by
     `meta_analysis.py`'s "verifier rejected synthesized answer" branch, so
     the reason string in `backend.log` is the primary evidence.
  2. Gold-coverage probes for G1's four gold findings, computed per run so
     the n-of-N table in the result file is derived from the captured answer
     rather than eyeballed.

Usage:
    GOLD32_BASE_URL=http://127.0.0.1:8026 M71_LOG=.../backend.log \
    M71_OUT=.../runs.json M71_RUNS=6 M71_IDS=G1 M71_ARM=before \
    python scripts/module71_live_runs.py
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
BASE = os.environ.get("GOLD32_BASE_URL", "http://127.0.0.1:8026")
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "MuhafizAdmin2026!")
LOG = os.environ.get("M71_LOG", os.path.join(HERE, "backend.log"))
OUT = os.environ.get("M71_OUT", os.path.join(HERE, "scratchpad", "module71_runs.json"))
RUNS = int(os.environ.get("M71_RUNS", "6"))
ARM = os.environ.get("M71_ARM", "after")
IDS = [i for i in (os.environ.get("M71_IDS") or "G1").split(",") if i]
TIMEOUT_S = int(os.environ.get("GOLD32_TIMEOUT_S", "1200"))

# Non-gold paraphrases, sent through the same path as a gold id.
PROBES = {
    "G1P1": (
        "Take a look across everything we currently have on the books and tell "
        "me what stands out about the offenders, the property we are holding "
        "and when these incidents actually happen."
    ),
    "G1P2": (
        "Mujhe poore caseload ka jaiza chahiye - ghair mamooli kya hai? "
        "Mulzimon ki umar, unka complainant se taluq, zabt shuda saman ka kya "
        "bana, aur waqia kis waqt hota hai?"
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
_REJECT = re.compile(
    r"((?:Meta-Analysis|Large-Scale Aggregate|Global Search|Local Search|"
    r"Investigative Analysis|Cross-Case Linkage|Case Summarization): "
    r"verifier rejected[^\n]*)"
)
_MOD71 = re.compile(r"(Meta-Analysis \[Module 71\][^\n]*)")
# Module 81's corrected quota pattern (evaluation/gold32_score.py
# LOG_GREP_PATTERN) - the old one matched a log line's own milliseconds.
_QUOTA = re.compile(
    r"rate limit|RESOURCE_EXHAUSTED|quota|UNAVAILABLE|(^|[^0-9.,:])(429|503)([^0-9]|$)",
    re.IGNORECASE,
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
    xagg, rej, mod71, quota = [], [], [], []
    for line in text.splitlines():
        m = _XAGG.search(line)
        if m:
            xagg.append(m.group(1)[:400])
        m = _REJECT.search(line)
        if m:
            rej.append(m.group(1)[:400])
        m = _MOD71.search(line)
        if m:
            mod71.append(m.group(1)[:400])
        if _QUOTA.search(line):
            quota.append(line[:200])
    return {
        "xagg_log_lines": xagg,
        "verifier_rejections": rej,
        "module71_lines": mod71,
        "quota_lines": quota,
    }


# -- G1 gold coverage ----------------------------------------------------
# Gold's four findings. Facts-and-ideas coverage, not wording: each probe is
# a disjunction of the ways the live pipeline has actually rendered that
# finding, and every numeric probe allows the +/-5-10 % band the judging
# standard sets.
_COVER = {
    # (1) offender age range 24-49, mean ~31.5
    "age": re.compile(
        r"(2[34]\s*(?:to|-|–|—|and)\s*(?:49|50)|mean age|average age"
        r"|\b3[12][.,]\d)",
        re.IGNORECASE,
    ),
    # (2) 'stranger' (اجنبی) dominates, 15 of 24
    "relationship": re.compile(
        r"(اجنبی|stranger|unknown\s+to\s+the\s+complainant)",
        re.IGNORECASE,
    ),
    # (3) 13 forensic-lab / 7 heirs
    "property": re.compile(
        r"(forensic|فورنزک|heirs?|ورثا|وارث)",
        re.IGNORECASE,
    ),
    # (4) time-of-day distribution
    "time": re.compile(
        r"(time of day|morning|afternoon|evening|night|صبح"
        r"|دوپہر|شام|رات)",
        re.IGNORECASE,
    ),
}
_NUM = {
    "age_mean_31_5": re.compile(r"\b3[12](?:[.,]\d+)?\b"),
    "relationship_15_of_24": re.compile(r"\b15\b.{0,60}\b24\b|\b24\b.{0,60}\b15\b", re.DOTALL),
    "property_13_7": re.compile(r"\b13\b.{0,120}\b7\b", re.DOTALL),
}


def coverage(answer):
    keys = list(_COVER) + list(_NUM)
    if not answer:
        return {k: False for k in keys}
    out = {k: bool(p.search(answer)) for k, p in _COVER.items()}
    out.update({k: bool(p.search(answer)) for k, p in _NUM.items()})
    return out


def main():
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    for pid, text in PROBES.items():
        gold[pid] = {"id": pid, "question": text,
                     "answer": "(paraphrase - no gold)", "language": "en"}
    out = []
    if os.path.exists(OUT):
        out = json.load(open(OUT, encoding="utf-8"))
    done = {(o["id"], o["run"], o["arm"]) for o in out}
    ac, cs = login()
    for run in range(1, RUNS + 1):
        for qid in IDS:
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
                     "transport_ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            p["elapsed_s"] = round(time.time() - t0, 1)
            time.sleep(2)
            p.update(dissect(log_slice(LOG, off)))
            p["coverage"] = coverage(p.get("actual_answer") or "")
            rec = {"id": qid, "run": run, "arm": ARM,
                   "language": item.get("language"), "question": item["question"],
                   "expected_answer": item["answer"]}
            rec.update(p)
            out.append(rec)
            json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            cov = "".join(k[0].upper() if v else "." for k, v in p["coverage"].items())
            sys.stdout.write(
                "[%s run%d] %-5s route=%-9s status=%-7s %7ss cov=%s rej=%d\n"
                % (ARM, run, qid, str(p.get("route")), str(p.get("status")),
                   p["elapsed_s"], cov, len(p["verifier_rejections"]))
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
