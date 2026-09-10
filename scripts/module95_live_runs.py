"""
[Gold-QA fix — Module 95] Live runner for G2 and G5 plus their paraphrases.

Adapted from `evaluation/gold32_run.py` (login / ask / SSE parse verbatim)
so the transport behaves exactly as the evaluation harness does, including
Module 42's `transport_ok` distinction between "abstained" and "never
answered".

What it adds is the only thing this module needs that gold32_run.py does
not record: a per-run FINDING-COVERAGE check. G2 and G5 are open-ended
briefings whose gold answer is a method line plus three numbered findings,
and Module 87's judge marked both down for naming DIFFERENT true findings.
So each answer is scored here against gold's findings by the evidence each
one rests on, not by wording — a briefing that makes gold's point in
different words counts.

Usage:
    GOLD32_BASE_URL=http://127.0.0.1:8095 \
    M95_OUT=.../runs.json M95_RUNS=3 M95_ARM=after \
    python scripts/module95_live_runs.py
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
sys.path.insert(0, HERE)

GOLD = os.path.join(HERE, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")
PARAPHRASES = os.path.join(
    HERE, "docs", "gold-qa-wave2-results", "module95_paraphrases.json"
)
BASE = os.environ.get("GOLD32_BASE_URL", "http://127.0.0.1:8095")
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "MuhafizAdmin2026!")
OUT = os.environ.get("M95_OUT", os.path.join(HERE, "module95_runs.json"))
RUNS = int(os.environ.get("M95_RUNS", "3"))
ARM = os.environ.get("M95_ARM", "after")
ONLY = [i for i in (os.environ.get("M95_IDS") or "").split(",") if i]
TIMEOUT_S = int(os.environ.get("GOLD32_TIMEOUT_S", "900"))


# ── finding coverage ─────────────────────────────────────────────────────
#
# One entry per gold finding, each a list of alternative markers; a finding
# counts as PRESENT when any marker matches. Markers are the finding's
# EVIDENCE (its figure, or the mechanism it names), never gold's phrasing,
# because that is the standard this programme judges by: matching the theme
# or content of the idea, not the wording.
_FINDINGS = {
    "G2": {
        "1_ordering": [
            r"\b9\b[^.\n]{0,60}(incident date|تاریخ)",
            r"(incident date|واقعے کی تاریخ)[^.\n]{0,60}\b9\b",
            r"\b188\b", r"\b259\b", r"entry type", r"قسم درج",
        ],
        "2_chronology": [
            r"\b13\b[^.\n]{0,80}(depart|روانگی)",
            r"(depart|روانگی)[^.\n]{0,80}\b13\b",
            r"depart\w*[^.\n]{0,80}(earlier|before|precede)",
            r"روانگی[^.\n]{0,80}(پہلے|قبل)",
        ],
        "3_two_systems": [
            r"(walk-in|complaint|شکایت|cms)[^.\n]{0,120}(separate|alag|الگ|optional|اختیاری|tag|ٹیگ)",
            r"(tag|ٹیگ)[^.\n]{0,120}(optional|اختیاری|not enforced|no.{0,12}key)",
            r"e_tag", r"case_tag",
        ],
    },
    "G5": {
        "1_unlicensed": [r"\b30\b[^.\n]{0,40}\b32\b", r"94\s*%", r"بغیر لائسنس", r"unlicens"],
        "2_custody": [
            r"chain of custody", r"custody", r"packag", r"photograph", r"seal",
            r"tasaweer", r"تحویل",
        ],
        "3_soft_join": [
            r"soft[^.\n]{0,30}(match|link|reference)",
            r"(no|without)[^.\n]{0,25}(enforced|foreign)\s*key",
            r"orphan", r"mistyp", r"narm[^.\n]{0,20}match",
        ],
    },
}


def coverage(qid: str, answer: str) -> dict:
    spec = _FINDINGS[qid]
    text = answer.lower()
    hit = {
        name: any(re.search(p, text, re.IGNORECASE) for p in patterns)
        for name, patterns in spec.items()
    }
    hit["_count"] = sum(1 for k, v in hit.items() if v)
    return hit


# ── transport (verbatim from evaluation/gold32_run.py) ───────────────────

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


def _questions() -> list[tuple[str, str, str]]:
    """(row_id, gold_id, question) — gold wording first, then paraphrases."""
    gold = {q["id"]: q for q in json.loads(open(GOLD, encoding="utf-8").read())}
    para = json.loads(open(PARAPHRASES, encoding="utf-8").read())
    rows: list[tuple[str, str, str]] = []
    for qid in ("G2", "G5"):
        for _ in range(RUNS):
            rows.append((qid, qid, gold[qid]["question"]))
        for p in para[qid]:
            rows.append((p["id"], qid, p["text"]))
    if ONLY:
        rows = [r for r in rows if r[1] in ONLY or r[0] in ONLY]
    return rows


def main():
    rows = _questions()
    out: list[dict] = []
    if os.path.exists(OUT):
        out = json.loads(open(OUT, encoding="utf-8").read())
    seen = {}
    ac, cs = login()
    for i, (row_id, qid, q) in enumerate(rows, 1):
        seen[row_id] = seen.get(row_id, 0) + 1
        run_no = seen[row_id]
        if any(r["row_id"] == row_id and r["run"] == run_no and r["arm"] == ARM for r in out):
            continue
        t0 = time.time()
        try:
            p = parse(ask(q, ac, cs))
            p["transport_ok"] = True
            p["error"] = None
        except Exception as e:  # noqa: BLE001
            p = {"actual_answer": "", "route": None, "status": "error",
                 "transport_ok": False, "error": f"{type(e).__name__}: {e}"}
        cov = coverage(qid, p["actual_answer"]) if p["actual_answer"] else {"_count": 0}
        rec = {"arm": ARM, "row_id": row_id, "gold_id": qid, "run": run_no,
               "question": q, "elapsed_s": round(time.time() - t0, 1),
               "coverage": cov, **p}
        out.append(rec)
        json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[{i}/{len(rows)}] {row_id:6} run{run_no} route={str(p.get('route')):>8} "
              f"findings={cov['_count']}/3 len={len(p['actual_answer']):5} {rec['elapsed_s']}s"
              + ("" if p.get("transport_ok") else f"  <-- NO ANSWER ({p.get('error')})"))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
