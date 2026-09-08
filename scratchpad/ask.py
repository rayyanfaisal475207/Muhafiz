"""Module 50 live asker. Captures the FULL SSE stream and EVERY route event.

Usage:  python scratchpad/ask.py <label> "<question>" [n_runs]
Writes scratchpad/live/<label>-run<k>.json
"""
from __future__ import annotations
import json, os, re, sys, time, urllib.request, uuid

BASE = os.environ.get("MUHAFIZ_BASE", "http://127.0.0.1:8013")
EMAIL = os.environ.get("EVAL_ADMIN_EMAIL", "admin@example.com")
PW = os.environ.get("EVAL_ADMIN_PASSWORD", "MuhafizAdmin2026!")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live")
os.makedirs(OUTDIR, exist_ok=True)


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
    return urllib.request.urlopen(req, timeout=600).read().decode("utf-8")


def parse(sse):
    """EVERY route event, not the last one — Meta-Analysis emits one per sub-query."""
    ans, routes, status, events = [], [], None, []
    for line in sse.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            d = json.loads(line[5:])
        except Exception:
            continue
        det = str(d.get("detail", ""))
        events.append({"step": d.get("step"), "detail": det[:400]})
        for m in re.finditer(r"route='([^']*)'[^\n]*?sub-agent='([^']*)'", det):
            routes.append(f"{m.group(1)} -> {m.group(2)}")
        if d.get("step") == "response":
            t = d.get("answer") or det
            if t and len(t) > 10:
                ans.append(t)
            status = d.get("status")
    a = " ".join(ans).strip()
    a = re.sub(r"^Writing the answer…\s*", "", a)
    a = re.sub(r"\s*Response generated.*$", "", a)
    return {"answer": a, "routes": routes, "status": status, "events": events}


def main():
    label, question = sys.argv[1], sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    ac, cs = login()
    for k in range(1, n + 1):
        t0 = time.time()
        try:
            sse = ask(question, ac, cs)
            p = parse(sse)
            p["error"] = None
        except Exception as e:  # noqa: BLE001
            sse = ""
            p = {"answer": "", "routes": [], "status": "error", "events": [], "error": str(e)}
        p["elapsed_s"] = round(time.time() - t0, 1)
        p["question"] = question
        p["label"] = label
        p["run"] = k
        p["raw_sse_len"] = len(sse)
        path = os.path.join(OUTDIR, f"{label}-run{k}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=1)
        with open(path.replace(".json", ".sse"), "w", encoding="utf-8") as f:
            f.write(sse)
        print(f"[{label} run{k}] {p['elapsed_s']}s status={p['status']} "
              f"routes={len(p['routes'])} err={p['error']}")
        for r in p["routes"]:
            print("    ", r)
        print("    ANSWER:", (p["answer"] or "")[:600].replace("\n", " "))


if __name__ == "__main__":
    main()
