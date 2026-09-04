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
BASE = "http://127.0.0.1:8001"
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "")


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
    return urllib.request.urlopen(req, timeout=300).read().decode("utf-8")


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
        except Exception as e:  # noqa: BLE001
            p = {"actual_answer": "", "route": None, "status": "error", "error": str(e)}
        p["elapsed_s"] = round(time.time() - t0, 1)
        rec = {"id": item["id"], "type": item["question_type"], "language": item["language"],
               "question": item["question"], "expected_answer": item["answer"], **p}
        outputs.append(rec)
        print(f"[{i}/32] {item['id']:5} {item['language']:8} route={str(p.get('route')):>10} "
              f"len={len(p['actual_answer']):5} {p['elapsed_s']}s")
        json.dump(outputs, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nwrote {len(outputs)} to {OUT}")


if __name__ == "__main__":
    main()
