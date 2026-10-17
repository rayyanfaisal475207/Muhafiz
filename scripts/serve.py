"""Bring the whole stack up, and prove the running code is the code on disk.

WHY THIS EXISTS

Three separate times this stack served STALE CODE while every surface
looked healthy, and each time it cost an hour to notice:

  * A uvicorn started before an edit kept serving the old module. The port
    answered 200, the frontend loaded, and the fix simply was not there.
  * Two leftover vite processes held port 5173, so a newly started frontend
    landed on 5174 and the tunnel kept pointing at the dead one. The UI
    loaded and every API call returned 404.
  * `--reload` runs a parent that RESPAWNS its child, so killing the child
    did nothing: each kill produced new PIDs, and four generations of
    uvicorn ended up running at once. Netstat compounded it by listing
    PIDs that no longer existed.

A healthy-looking port proves nothing. This script therefore does three
things in order, and refuses to continue when any of them fails:

  1. STOP everything, by killing the ROOT of each process tree rather than
     the leaves, and waiting until the ports genuinely stop answering.
  2. START one backend and one frontend, each verified to be listening on
     the port it was asked for -- not the next one it could find.
  3. PROVE the live code is current, by asking a question whose correct
     answer only today's code produces, through the public URL, as a real
     user. A timestamp can lie about what a process loaded; an answer
     cannot.

START THE SERVERS ONLY THROUGH THIS SCRIPT

Launching uvicorn from an agent's background-task runner, an IDE task or a
terminal that something supervises creates a SECOND generation that the
supervisor restarts whenever it dies. MEASURED: a duplicate backend
appeared about a minute after each run of this script, every time, and its
parent turned out to be the agent process that had launched an earlier
one. It could not bind the port, so it sat idle -- until the serving
generation was killed, at which point it took over and served whatever
code it had loaded.

This script starts each server detached, so nothing respawns it and
stopping it stops it.

Usage:
    python scripts/serve.py                 stop, start, tunnel
    python scripts/serve.py --verify        ...and prove the code is current
    python scripts/serve.py --stop          stop everything and exit

The verification account comes from MUHAFIZ_SMOKE_EMAIL and
MUHAFIZ_SMOKE_PASSWORD so no credential is written into this file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_PORT = int(os.getenv("MUHAFIZ_BACKEND_PORT", "8099"))
FRONTEND_PORT = int(os.getenv("MUHAFIZ_FRONTEND_PORT", "5173"))
CLOUDFLARED = os.getenv(
    "CLOUDFLARED", r"C:\Program Files (x86)\cloudflared\cloudflared.exe")
LOGS = ROOT / "data" / "serve"

# A question whose right answer only today's code produces. Criminal records
# are attached to no case, so a plan scoped to case documents counts zero of
# them; the scope correction is what makes 33 reachable at all.
PROOF_QUESTION = "How many records exist in the criminal records system?"
PROOF_EXPECTS = "33"


def say(message: str) -> None:
    print(message, flush=True)


def powershell(script: str) -> str:
    done = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, text=True)
    return (done.stdout or "").strip()


def server_processes() -> list[dict]:
    """Every process that could be serving this stack, orphans included.

    THE ORPHANED FORK IS THE ONE THAT CAUSED ALL THE TROUBLE.

    `uvicorn --reload` runs the real server as a multiprocessing fork of a
    watcher parent. Kill the parent and the CHILD SURVIVES, orphaned, still
    holding the socket and still serving the module it loaded. Its command
    line is

        python.exe -c "from multiprocessing.spawn import spawn_main;
                       spawn_main(parent_pid=31732, ...)" --multiprocessing-fork

    which contains neither "uvicorn" nor "vite", so every filter written
    against those names missed it completely. MEASURED: four such orphans
    were found running at once, from four different generations, and one of
    them was answering /health with `llm_provider: groq` hours after the
    configuration had been changed to openai -- a healthy-looking port
    serving stale code, which is exactly the failure this script exists to
    prevent.

    Netstat and Get-NetTCPConnection both attribute the socket to the DEAD
    parent, so the port owner cannot be used to find it either. Matching
    the fork's own command line is what works.
    """
    out = powershell(
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.CommandLine -like '*uvicorn*' -or $_.CommandLine -like '*vite*' "
        "-or $_.CommandLine -like '*multiprocessing-fork*' "
        "-or $_.CommandLine -like '*spawn_main*' } "
        "| Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress")
    if not out:
        return []
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return []
    rows = parsed if isinstance(parsed, list) else [parsed]
    return [{"pid": int(r["ProcessId"]), "parent": int(r["ParentProcessId"])}
            for r in rows]


def answers(port: int, path: str = "/", timeout: float = 3.0) -> int | None:
    """The status a port returns, or None when nothing is listening."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as refused:
        return refused.code          # an answer is an answer, 401 included
    except Exception:                # noqa: BLE001
        return None


def stop_everything() -> None:
    """Kill the ROOT of every server process tree, then wait for the ports.

    Killing a leaf is useless under `--reload`: the parent respawns it. So
    the tree root is killed with /T, and the loop repeats because a root
    that dies can leave a child that is briefly re-parented.
    """
    for attempt in range(6):
        running = server_processes()
        if not running:
            break
        mine = {row["pid"] for row in running}
        # A root is a process whose parent is not itself one of ours. Roots
        # die first so a watcher cannot respawn what has just been killed --
        # but EVERY process is then killed anyway, because an orphaned fork
        # has no live parent and would otherwise be left holding the port.
        roots = sorted({row["pid"] for row in running if row["parent"] not in mine})
        say(f"  stopping {len(running)} process(es), {len(roots)} tree root(s)")
        for pid in roots:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True)
        time.sleep(1.0)
        for pid in sorted(mine - set(roots)):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True)
        time.sleep(1.0)
    subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe"],
                   capture_output=True, text=True)

    # THE PORT, NOT THE PROCESS LIST, IS THE TEST.
    #
    # Netstat has listed PIDs here that no longer existed, so "no processes"
    # is not proof that nothing serves. Waiting until the port stops
    # answering is.
    for port in (BACKEND_PORT, FRONTEND_PORT):
        for attempt in range(20):
            if answers(port) is None:
                break
            time.sleep(0.5)
        else:
            raise SystemExit(
                f"port {port} is still answering after every server process "
                f"was killed; something else is bound to it")
    say("  stopped: both ports silent")


def launch(name: str, command: list[str], cwd: Path) -> subprocess.Popen:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = (LOGS / f"{name}.log").open("w", encoding="utf-8", errors="replace")
    return subprocess.Popen(command, cwd=str(cwd), stdout=log, stderr=log,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def wait_for(port: int, path: str, wanted: set[int], label: str,
             seconds: float = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = answers(port, path)
        if status in wanted:
            say(f"  {label}: port {port} -> {status}")
            return
        time.sleep(1)
    raise SystemExit(f"{label} did not come up on port {port} within {seconds:.0f}s "
                     f"(last status {answers(port, path)})")


def start_backend() -> None:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    launch("backend", [str(python), "-m", "uvicorn", "src.main:app",
                       "--host", "127.0.0.1", "--port", str(BACKEND_PORT),
                       "--reload", "--reload-dir", "src",
                       "--log-level", "warning"], ROOT)
    wait_for(BACKEND_PORT, "/health", {200}, "backend")


def start_frontend() -> None:
    launch("frontend", ["npm.cmd", "run", "dev"], ROOT / "frontend")
    # 401 is the right answer here: it means the proxy reached the backend
    # and auth refused an anonymous caller. A 404 would mean the proxy is
    # forwarding to the wrong path, and 200 would mean the SPA fallback
    # swallowed the API route.
    wait_for(FRONTEND_PORT, "/api/evidence/health", {401}, "frontend proxy")
    listening = powershell(
        f"(Get-NetTCPConnection -State Listen -LocalPort {FRONTEND_PORT} "
        "-ErrorAction SilentlyContinue | Measure-Object).Count")
    if listening and listening.strip() not in {"1"}:
        raise SystemExit(
            f"{listening.strip()} processes are listening on {FRONTEND_PORT}; "
            "a second frontend would send the tunnel to the wrong one")


def start_tunnel() -> str:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / "tunnel.log"
    log.unlink(missing_ok=True)
    launch("tunnel-stdout", [CLOUDFLARED, "tunnel",
                             "--url", f"http://127.0.0.1:{FRONTEND_PORT}",
                             "--logfile", str(log), "--no-autoupdate"], ROOT)
    found = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    deadline = time.monotonic() + 90
    url = ""
    while time.monotonic() < deadline:
        if log.exists():
            match = found.search(log.read_text(encoding="utf-8", errors="replace"))
            if match:
                url = match.group(0)
                break
        time.sleep(1)
    if not url:
        raise SystemExit("the tunnel did not report a public URL")
    for attempt in range(20):           # registration lags the URL appearing
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                if response.status == 200:
                    say(f"  tunnel: {url} -> 200")
                    return url
        except Exception:               # noqa: BLE001
            pass
        time.sleep(3)
    raise SystemExit(f"{url} never answered; the tunnel did not register")


def prove_code_is_current(url: str) -> None:
    """Ask a question whose right answer only today's code produces.

    This is the check the other three could not make. A process can start
    after a file was edited and still serve something else; only an answer
    settles it.
    """
    email = os.getenv("MUHAFIZ_SMOKE_EMAIL")
    password = os.getenv("MUHAFIZ_SMOKE_PASSWORD")
    if not (email and password):
        say("  skipped verification: set MUHAFIZ_SMOKE_EMAIL and "
            "MUHAFIZ_SMOKE_PASSWORD to enable it")
        return
    try:
        import httpx
    except ImportError:
        say("  skipped verification: httpx is not installed")
        return
    with httpx.Client(base_url=url, timeout=600.0, follow_redirects=True) as client:
        signed_in = client.post("/api/auth/login",
                                json={"email": email, "password": password})
        if signed_in.status_code != 200:
            raise SystemExit(f"verification login failed ({signed_in.status_code})")
        headers = {"X-CSRF-Token": client.cookies.get("csrf_token") or ""}
        said: list[str] = []
        with client.stream("POST", "/api/chat", headers=headers,
                           json={"session_id": str(uuid.uuid4()),
                                 "message": PROOF_QUESTION}) as streamed:
            for line in streamed.iter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except ValueError:
                    continue
                if event.get("step") == "response" and event.get("detail"):
                    said.append(event["detail"])
        answer = max(said, key=len) if said else ""
        if PROOF_EXPECTS not in answer:
            raise SystemExit(
                "THE LIVE CODE IS NOT CURRENT.\n"
                f"  asked:    {PROOF_QUESTION}\n"
                f"  expected: an answer containing {PROOF_EXPECTS!r}\n"
                f"  got:      {' '.join(answer.split())[:200]!r}\n"
                "A process is serving older code than what is on disk.")
        say(f"  verified: live code answers {PROOF_EXPECTS} "
            f"({' '.join(answer.split())[:60]}...)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true",
                        help="stop everything and exit")
    parser.add_argument("--verify", action="store_true",
                        help="prove the live code is current after starting")
    parser.add_argument("--no-tunnel", action="store_true")
    args = parser.parse_args()

    say("stopping any running servers")
    stop_everything()
    if args.stop:
        return

    say("starting backend")
    start_backend()
    say("starting frontend")
    start_frontend()
    url = "" if args.no_tunnel else start_tunnel()
    if args.verify and url:
        say("proving the live code is current")
        prove_code_is_current(url)
    say("")
    say(f"local:  http://127.0.0.1:{FRONTEND_PORT}")
    if url:
        say(f"public: {url}")


if __name__ == "__main__":
    main()
