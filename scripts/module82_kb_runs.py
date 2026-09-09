"""
Modules 82/85/86 — live runner for the "retrieve the right law, then lose it
downstream" class (KB2, KB4) plus the regression bucket.

Adapted from `scripts/module65_kb_runs.py` (Module 65), which is itself adapted
from `scripts/module52_kb_runs.py`. Two things are added, because this module's
subject is what happens AFTER retrieval:

  * the **verifier verdict** is read out of `backend.log`
    (`Semantic Search: verifier rejected answer: <reason>`), so a rejection is
    recorded with its reason rather than inferred from `status`;
  * the **data-half plan** line (`RAG tool: KB data-half plan ... answered by
    aggregate ...`) is captured, because Module 86 is about a figure that
    arrives on that chunk and does not reach the answer.

Chunk IDs only, never citations — Module 30 recorded an evaluator citing a
section that was not in the chunks it judged, so an answer's own "[Document 2]"
cannot establish what was actually read.

Usage:
    GOLD32_BASE_URL=http://127.0.0.1:8031 \
    M82_LOG=.../backend.log M82_OUT=.../runs.json M82_RUNS=3 M82_ARM=before \
    M82_IDS=KB2,KB4 python scripts/module82_kb_runs.py
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
BASE = os.environ.get("GOLD32_BASE_URL", "http://127.0.0.1:8031")
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "MuhafizAdmin2026!")
LOG = os.environ.get("M82_LOG", os.path.join(HERE, "backend.log"))
OUT = os.environ.get("M82_OUT", os.path.join(HERE, "module82_runs.json"))
RUNS = int(os.environ.get("M82_RUNS", "3"))
ARM = os.environ.get("M82_ARM", "before")
IDS = [i for i in (os.environ.get("M82_IDS") or "").split(",") if i]
TIMEOUT_S = int(os.environ.get("GOLD32_TIMEOUT_S", "1200"))

# Module 65's two KB2 paraphrases, verbatim, so this module's positive control
# is the same one that measured the capability rather than a fresh invention.
EXTRA = {
    "KB2P": ("KB2",
             "If an investigating officer writes down what a suspect admits during "
             "questioning at the police station, can that written admission be used "
             "against him in court under our law — and is that why our case files "
             "hold no interview transcripts?"),
    "KB2Q": ("KB2",
             "Kya qanoon ijazat deta hai ke mulzim ne police ke saamne jo iqbal-e-jurm "
             "kiya ho use adalat mein saboot ke tor par pesh kiya jaye — aur kya isi "
             "wajah se hamare record mein bayanat mehfooz nahin hote?"),
    # Module 38's paraphrase 2 — the KB4 phrasing measured to trip the KB-intent
    # gate. Non-gold, Roman-Urdu, and it exercises the register/disposal
    # capability from a different direction.
    "KB4P": ("KB4",
             "Kis qanoon ke tehat police ko maal-e-muqadma ka register rakhna parta "
             "hai, aur us property ko kitne arse baad tabah ya nilaam kiya ja sakta "
             "hai — kya hamare record mein iski paabandi nazar aati hai?"),
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


_ATTEMPT = re.compile(r"chunk\(s\) to evaluator \(attempt (\d+)\): (.*)$")
# rag.py renders the list as `id[source_file], id[source_file], ...` and BOTH
# halves can contain a comma — `7_Anti-Rape … Act_ 2021 & Rules, 2022 (…)_pdf_…`
# is one id. Splitting on ", " (module65_kb_runs.py's parser) shreds those into
# three fake ids, so the ids are pulled out by the bracket structure instead.
_CHUNK = re.compile(r"([^\[\]]+?)\[[^\[\]]*\](?:,\s*|$)")
_VERDICT = re.compile(r"Evaluator: relevant=(True|False) — (.*)$")
_STATUTE = re.compile(r"statute-hypothesis query: (.*)$")
_VERIF = re.compile(r"verifier rejected (?:synthesized )?answer: (.*)$")
_DATAHALF = re.compile(r"KB data-half plan '([^']+)' answered by aggregate '([^']+)'")
# Module 54's trap, and it fired live in BOTH arms of this module: when the
# cutover classifier's provider returns 429, `src.main` falls back to
# orchestrator.py, so **a different pipeline answers than the one being
# measured**. A row with this set is not a measurement of the harness and is
# discarded rather than averaged in.
_CUTOVER = re.compile(r"Cutover classification failed")
# Module 81's corrected quota pattern — numeric codes boundary-guarded so a
# timestamp's milliseconds or an ephemeral port cannot score as a 429/503.
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
    attempts, verdicts, statutes, errors = [], [], [], []
    verifier_rejections, data_half = [], []
    cutover_fallbacks = []
    for line in text.splitlines():
        m = _ATTEMPT.search(line)
        if m:
            raw = m.group(2)
            ids = [c.strip() for c in _CHUNK.findall(raw)] if raw != "none" else []
            attempts.append({"attempt": int(m.group(1)), "chunk_ids": ids})
        m = _VERDICT.search(line)
        if m:
            verdicts.append({"relevant": m.group(1) == "True", "reason": m.group(2)[:300]})
        m = _STATUTE.search(line)
        if m:
            statutes.append(m.group(1)[:200])
        m = _VERIF.search(line)
        if m:
            verifier_rejections.append(m.group(1)[:400])
        m = _DATAHALF.search(line)
        if m:
            data_half.append({"plan": m.group(1), "aggregate": m.group(2)})
        if _CUTOVER.search(line):
            cutover_fallbacks.append(line[:200])
        if _QUOTA.search(line):
            errors.append(line[:200])
    return {
        "evaluator_rounds": len(attempts),
        "attempts": attempts,
        "evaluator_verdicts": verdicts,
        "statute_hypotheses": statutes,
        "verifier_rejections": verifier_rejections,
        "data_half": data_half,
        "cutover_fallbacks": cutover_fallbacks,
        "quota_lines": errors,
    }


def main():
    gold = {q["id"]: q for q in json.load(open(GOLD, encoding="utf-8"))}
    for pid, (base_id, text) in EXTRA.items():
        gold[pid] = {**gold[base_id], "id": pid, "question": text}
    ids = IDS or ["KB2", "KB4"]
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
            rec = {"id": qid, "run": run, "arm": ARM, "language": item.get("language"),
                   "question": item["question"], "expected_answer": item["answer"], **p}
            out.append(rec)
            json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            sys.stdout.write(
                f"[{ARM} run{run}] {qid:5} route={str(p.get('route')):>8} "
                f"status={str(p.get('status')):>6} rounds={p['evaluator_rounds']} "
                f"len={len(p['actual_answer']):5} rej={len(p['verifier_rejections'])} "
                f"cutover={len(p['cutover_fallbacks'])} "
                f"{p['elapsed_s']}s\n")
            sys.stdout.flush()
    print(f"wrote {len(out)} rows to {OUT}")


if __name__ == "__main__":
    main()
