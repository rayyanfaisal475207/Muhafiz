"""Ask the live endpoint a set of questions and time each one."""
import json, sys, time, urllib.request
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv(".env")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from src.auth.jwt import create_access_token

BASE = "http://127.0.0.1:8099"
TOK = create_access_token({"sub": "4939e74f-543b-4143-a997-49a86bc98da6"})
C = "smoke"

def ask(q):
    t = time.time()
    body = json.dumps({"question": q}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/ask", data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer " + TOK,
                 "x-csrf-token": C, "Cookie": "csrf_token=" + C})
    try:
        with urllib.request.urlopen(req, timeout=500) as r:
            d = json.loads(r.read().decode("utf-8"))
        out = d.get("answer") or ("REFUSE: " + str(d.get("refusal"))[:80])
    except Exception as e:
        out = "FAILED " + str(e)[:60]
    print(f"{time.time()-t:6.1f}s  {q[:42]:<44} -> {str(out)[:70]}")

for q in sys.argv[1:] or ["How many weapons were recovered?",
                          "کتنے ہتھیار برآمد ہوئے؟"]:
    ask(q)
