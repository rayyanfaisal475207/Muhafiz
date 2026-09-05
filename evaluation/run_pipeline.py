"""
Run every gold-set query through the LIVE /api/chat pipeline and capture the
actual answer + retrieval context. Produces pipeline_outputs.json, which is
paired with gold_set.json's ground truth for DeepEval scoring.

Uses the correct role per query (investigator by default; supervisor for
cross-case items that would otherwise be denied). Real cookie+CSRF auth.

Run:  .venv/Scripts/python.exe evaluation/run_pipeline.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(HERE, "gold_set.json")
OUT = os.path.join(HERE, "pipeline_outputs.json")
BASE_URL = "http://127.0.0.1:8001"

# Test-account credentials come from the environment — NOT hardcoded, so no
# credential (even a throwaway one on a synthetic account) is committed to the
# repo. Set these before running:
#   EVAL_INVESTIGATOR_EMAIL / EVAL_INVESTIGATOR_PASSWORD
#   EVAL_SUPERVISOR_EMAIL    / EVAL_SUPERVISOR_PASSWORD
ACCOUNTS = {
    "investigator": (
        os.environ.get("EVAL_INVESTIGATOR_EMAIL", "browsercheck@example.com"),
        os.environ.get("EVAL_INVESTIGATOR_PASSWORD", ""),
    ),
    "supervisor": (
        os.environ.get("EVAL_SUPERVISOR_EMAIL", "audit_supervisor@example.com"),
        os.environ.get("EVAL_SUPERVISOR_PASSWORD", ""),
    ),
}


def _login(role: str) -> tuple[str, str]:
    email, pw = ACCOUNTS[role]
    body = json.dumps({"email": email, "password": pw}).encode()
    req = urllib.request.Request(f"{BASE_URL}/api/auth/login", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=25)
    cookies = resp.headers.get_all("Set-Cookie") or []
    access = next(re.search(r"access_token=([^;]+)", c).group(1) for c in cookies if "access_token=" in c)
    csrf = next(re.search(r"csrf_token=([^;]+)", c).group(1) for c in cookies if "csrf_token=" in c)
    return access, csrf


def _ask(query: str, case_id, access: str, csrf: str) -> str:
    payload = {"session_id": str(uuid.uuid4()), "message": query}
    if case_id:
        payload["case_id"] = case_id
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{BASE_URL}/api/chat", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Cookie", f"access_token={access}; csrf_token={csrf}")
    req.add_header("X-CSRF-Token", csrf)
    return urllib.request.urlopen(req, timeout=220).read().decode("utf-8")


def _parse_sse(sse: str) -> dict:
    """Extract the final answer, route, sub-agent, retrieval context, and
    verifier verdict from the SSE stream.

    [Gold-QA fix — GOLD_QA_MASTER_FIX_PLAN.md Module 19, RC-context-gap]
    The `step in ("retrieval", "retrieved_docs") and d.get("documents")`
    check this replaced never matched anything, on this codebase's current
    `HARNESS_CUTOVER_ROUTES=RAG,SQL,GRAPH,GRAPH_HYBRID,XGRAPH,XAGG,XNETWORK`
    (.env) OR the legacy orchestrator.py path it falls back to for DIRECT/
    WEB — confirmed by reading every SSE event both paths actually emit
    (src/pipeline/harness/cutover.py, src/pipeline/orchestrator.py): neither
    ever puts a "documents" field, or any chunk/aggregate TEXT at all, on
    the wire — `SubAgentResult`'s own design (types.py §3, "no raw evidence
    crosses the boundary") makes that a deliberate architectural choice for
    the harness path, not an oversight to route around. `retrieval_context`
    was therefore always `[]`, exactly EVALUATION_REPORT.md §4.1's finding.
    What genuinely WAS being computed but silently dropped: `sources`
    lists (web_search, file_generation — {"filename"/"url", "type", ...})
    and, as of this module, `result.citations` (bounded per-claim
    attribution — source_tool, case_id, source_file, confidence; still no
    chunk text, same boundary) via the new "citations" SSE step
    (cutover.py). Both are captured below as compact provenance strings —
    real signal for the judge (which tool/case/document backs each claim)
    that previously never reached `pipeline_outputs.json` at all, without
    claiming to solve context-relative scoring outright: DeepEval's
    text-context metrics still need narrative chunk TEXT the harness
    boundary will not expose, so `evaluation/gold_set.json`'s own
    hand-authored `retrieval_context` (see deepeval_score.py::build_cases())
    remains the primary text-context source for those metrics.
    """
    answer_parts, retrieved, route, subagent, status, verifier = [], [], None, None, None, None
    for line in sse.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            d = json.loads(line[5:])
        except Exception:
            continue
        detail = d.get("detail", "")
        if d.get("step") == "response":
            t = d.get("answer") or detail
            if t and len(t) > 15:
                answer_parts.append(t)
            status = d.get("status")
        if "route='" in str(detail):
            m = re.search(r"route='([^']*)'", detail)
            if m:
                route = m.group(1)
            m2 = re.search(r"sub-agent='([^']*)'", detail)
            if m2:
                subagent = m2.group(1)
        # Bounded per-claim attribution (cutover.py's "citations" step,
        # Module 19) — no chunk text, but real source/case/tool provenance.
        if d.get("step") == "citations" and d.get("citations"):
            for c in d["citations"]:
                parts = [f"tool={c.get('source_tool')}"]
                if c.get("source_file"):
                    parts.append(f"source={c['source_file']}")
                if c.get("case_id"):
                    parts.append(f"case={c['case_id']}")
                if c.get("confidence") is not None:
                    parts.append(f"confidence={c['confidence']}")
                retrieved.append("[Document {}] {}".format(c.get("document_index"), ", ".join(parts)))
        # Source references (web_search / file_generation `sources` lists —
        # filenames/URLs, never chunk text).
        if d.get("sources"):
            for s in d["sources"]:
                ref = s.get("filename") or s.get("url") or ""
                if ref:
                    retrieved.append(f"source: {ref}")
    answer = " ".join(answer_parts).strip()
    # strip the streaming UI chrome that isn't part of the answer content
    answer = re.sub(r"^Writing the answer…\s*", "", answer)
    answer = re.sub(r"\s*Response generated.*$", "", answer)
    return {
        "actual_answer": answer,
        "route": route,
        "subagent": subagent,
        "status": status,
        "retrieval_context": retrieved,
    }


def main() -> None:
    gold = json.load(open(GOLD, encoding="utf-8"))
    # Resume: keep already-completed outputs (a mid-run crash shouldn't cost
    # the expensive queries that already ran). Only re-run ids not yet present.
    outputs = []
    done_ids = set()
    if os.path.exists(OUT):
        outputs = json.load(open(OUT, encoding="utf-8"))
        done_ids = {o["id"] for o in outputs}
        print(f"resuming — {len(done_ids)} already done, skipping those")
    sessions = {}
    for i, item in enumerate(gold, 1):
        if item["id"] in done_ids:
            continue
        role = item.get("role", "investigator")
        if role not in sessions:
            sessions[role] = _login(role)
        access, csrf = sessions[role]
        t0 = time.time()
        try:
            sse = _ask(item["query"], item.get("case_id"), access, csrf)
            parsed = _parse_sse(sse)
            parsed["error"] = None
        except Exception as exc:  # noqa: BLE001
            parsed = {"actual_answer": "", "route": None, "subagent": None,
                      "status": "error", "retrieval_context": [], "error": str(exc)}
        parsed["elapsed_s"] = round(time.time() - t0, 1)
        rec = {"id": item["id"], "role": role, "query": item["query"],
               "expected_route": item["route"], "expected_subagent": item["subagent"], **parsed}
        outputs.append(rec)
        print(f"[{i}/{len(gold)}] {item['id']:18} route={str(parsed.get('route')):>12} "
              f"len={len(parsed['actual_answer']):5} {parsed['elapsed_s']}s "
              f"{'ERR:'+parsed['error'][:40] if parsed.get('error') else ''}")
        # save incrementally so a mid-run failure keeps completed results
        json.dump(outputs, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nwrote {len(outputs)} outputs to {OUT}")


if __name__ == "__main__":
    main()
