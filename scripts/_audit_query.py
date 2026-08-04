"""Audit test harness: log in as a given account, send one chat query, print
the routing trace + final response compactly. Not a permanent script."""
import io
import json
import sys
import uuid
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"

def run(email, password, message, case_id=None, session_id=None, timeout=90):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": password})
    if r.status_code != 200:
        print(f"LOGIN FAILED {r.status_code} {r.text[:200]}")
        return
    csrf = s.cookies.get("csrf_token")
    session_id = session_id or str(uuid.uuid4())
    body = {"session_id": session_id, "message": message}
    if case_id:
        body["case_id"] = case_id
    print(f"=== [{email}] case={case_id} q={message!r} ===")
    try:
        with s.post(
            f"{BASE}/api/chat", json=body,
            headers={"X-CSRF-Token": csrf}, stream=True, timeout=timeout,
        ) as resp:
            print("status:", resp.status_code)
            if resp.status_code != 200:
                print("BODY:", resp.text[:500])
                return
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                try:
                    evt = json.loads(payload)
                except Exception:
                    print("RAW:", payload[:300])
                    continue
                step, status = evt.get("step"), evt.get("status")
                if status == "streaming":
                    continue
                detail = str(evt.get("detail", ""))
                print(f"[{step}] {status} - {detail[:400]}")
    except requests.exceptions.ReadTimeout:
        print("TIMEOUT")

if __name__ == "__main__":
    # args: email password message [case_id]
    email, password, message = sys.argv[1], sys.argv[2], sys.argv[3]
    case_id = sys.argv[4] if len(sys.argv) > 4 else None
    run(email, password, message, case_id)
