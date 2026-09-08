"""
Run all 32 Gold-QA questions (with answers) through the LIVE fixed app and
capture the actual answers. Produces gold32_pipeline_outputs.json.

Runs as platform-admin with All Cases (no case_id) — matching the testing
team's conditions. Credentials from env (EVAL_ADMIN_EMAIL/PASSWORD).

Run: .venv/Scripts/python.exe evaluation/gold32_run.py
"""
from __future__ import annotations
import json, os, re, sys, time, urllib.request, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(HERE, "Gold_QA_Dataset_Final32_With_Answers.json")
OUT = os.path.join(HERE, "gold32_pipeline_outputs.json")
BASE = os.environ.get("GOLD32_BASE_URL", "http://127.0.0.1:8001")
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "")

# [Module 42] The read timeout on a single /api/chat call.
#
# This was a hard-coded 300, and that number — not any pipeline defect — is
# what produced KB6's uniquely bad row in the 2026-09-08 report:
# FactualCorrectness 0.0 AND AnswerRelevancy 0.0 with route=None, reported
# there as a "genuine error, did not recover". Measured live on this branch,
# KB6 takes 428-628s on the 4 runs in 5 that abstain (the 1 that answers takes
# 247.1s): it is a legal-KB question in All-Cases scope, so it pays
# a KB-only retrieval pass AND a mixed-pool fallback pass, three evaluator
# attempts each — six rounds of retrieve/rerank/evaluate. The request was
# therefore still in flight when urlopen() gave up; the exception handler in
# main() recorded route=None with an empty answer, and gold32_score.py scored
# that empty answer as a genuine 0.0.
#
# Nothing about that row described the pipeline. Live, with room to finish,
# KB6 returns route='RAG' every time (5/5).
#
# 900s is not a guess: it is comfortably above the slowest question measured
# on this machine (628s) while still bounding a genuinely hung request. Raise
# it via GOLD32_TIMEOUT_S on a slower box rather than editing this line — and
# see the `error` field, which is now always recorded, to tell a timeout from
# an answer.
TIMEOUT_S = int(os.environ.get("GOLD32_TIMEOUT_S", "900"))


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
            if m: route = m.group(1)
        if d.get("step") == "response":
            t = d.get("answer") or det
            if t and len(t) > 10: ans.append(t)
            status = d.get("status")
    a = " ".join(ans).strip()
    a = re.sub(r"^Writing the answer…\s*", "", a)
    a = re.sub(r"\s*Response generated.*$", "", a)
    return {"actual_answer": a, "route": route, "status": status}


def main():
    gold = json.load(open(GOLD, encoding="utf-8"))
    outputs, done = [], set()
    if os.path.exists(OUT):
        outputs = json.load(open(OUT, encoding="utf-8"))
        done = {o["id"] for o in outputs}
        print(f"resuming — {len(done)} done")
    ac, cs = login()
    for i, item in enumerate(gold, 1):
        if item["id"] in done:
            continue
        t0 = time.time()
        try:
            p = parse(ask(item["question"], ac, cs))
            p["error"] = None
            # [Module 42] Explicit, not merely implied by an empty string: the
            # request completed and whatever is in `actual_answer` is the
            # pipeline's own output, including a deliberate abstention.
            p["transport_ok"] = True
        except Exception as e:  # noqa: BLE001
            # [Module 42] The request never returned — a client-side timeout or
            # a dropped connection. The empty `actual_answer` here is the
            # ABSENCE of a measurement, not a bad answer, and route=None means
            # "the SSE stream was never read", not "classification failed".
            # gold32_score.py keys off `transport_ok` to leave the row
            # unscored instead of scoring the emptiness as a genuine 0.0 —
            # the same principle as Module 45's "a judge null is not a zero".
            p = {"actual_answer": "", "route": None, "status": "error",
                 "error": f"{type(e).__name__}: {e}", "transport_ok": False}
        p["elapsed_s"] = round(time.time() - t0, 1)
        rec = {"id": item["id"], "type": item["question_type"], "language": item["language"],
               "question": item["question"], "expected_answer": item["answer"], **p}
        outputs.append(rec)
        print(f"[{i}/32] {item['id']:5} {item['language']:8} route={str(p.get('route')):>10} "
              f"len={len(p['actual_answer']):5} {p['elapsed_s']}s"
              + ("" if p.get("transport_ok") else
                 f"   <-- NO ANSWER CAPTURED ({p.get('error')}) — will be left UNSCORED"))
        json.dump(outputs, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nwrote {len(outputs)} to {OUT}")


if __name__ == "__main__":
    main()
