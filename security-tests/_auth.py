"""Shared auth helper for local security tests. 127.0.0.1 ONLY."""
import json, uuid, urllib.request, urllib.error

BASE = "http://127.0.0.1:8001"

def login(email, password):
    """Returns (cookie_header, csrf_token). Mirrors the real double-submit flow."""
    req = urllib.request.Request(
        f"{BASE}/api/auth/login", method="POST",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.headers.get_all("Set-Cookie") or []
    jar, csrf = {}, None
    for c in raw:
        k, _
