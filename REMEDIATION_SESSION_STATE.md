# Remediation — Session Handoff State

**Purpose:** operational state a new Claude Code session needs that is NOT captured in
`REMEDIATION_INVESTIGATION_REPORT.md` (findings), `PROJECT_OVERVIEW.md` (architecture),
or `README.md` (setup). Read all four.

**Last updated:** 2026-08-25, end of the Part A/Part B investigation checkpoint.

---

## 1. Where we are, in one paragraph

The dataset under this project was replaced (old synthetic `CASE-009`-style corpus → real
Muhafiz Data API FIR data, 73 cases). That silently invalidated assumptions across the
pipeline. **Phase 1 is complete and committed** (`1a81bff` — XAGG no longer reports
unevaluatable filters as zero). **Phase 2's sync/re-embed is complete**, but its final
**purge of 231 stale ChromaDB chunks is verified and awaiting explicit user approval —
NOT executed.** A full investigation of the four remaining harness sub-agents is complete
(Part B of the report). **Phase 3 implementation has not started and is not authorized.**

---

## 2. Two decisions pending from the user — DO NOT ACT WITHOUT THEM

1. **Approve/reject the 231-chunk ChromaDB purge.** Fully verified, recommendation is
   SAFE TO APPROVE. Target list and exclusion proof are in
   `REMEDIATION_INVESTIGATION_REPORT.md` §2.4–2.7. **Do not run it until the user says so
   in this new session** — approval given in the previous session does not carry over,
   and no approval was in fact given.
2. **Phase 3 scope and ordering.** Proposed in report §8–9. Not approved.

---

## 3. Working rules in force (user-established, still binding)

1. Investigate before implementing.
2. No code changes unless explicitly requested/approved.
3. No destructive operation without showing the exact target set + proving what is
   excluded, then getting explicit approval.
4. Work phase-by-phase. Complete a checkpoint, then STOP and wait.
5. Any change to a module shared by the live orchestrator AND the harness must explicitly
   state its impact on BOTH paths. One consistent fix, never divergent behavior.
6. Push back on incorrect assumptions when repository evidence contradicts them. Do not
   label something a security issue because it sounds serious — establish the actual
   authorization/data-flow impact first.
7. No new capabilities during remediation.
8. Specifically OUT OF SCOPE and must not be started: the parked **query-decomposition
   sub-agent**, and **`section_code` crime classification**.
9. Do not silently retry, self-heal, delete, reset, or mutate data.
10. If a command would modify data, stop before running it and report what would happen.

**A note on rule 6 that has already mattered twice:** in this remediation I twice reported
something as more severe than it was and had to correct it (jurisdiction resolution called
"P0 security-adjacent" when the role gate was intact — it is P1 precision; and
`data_quality`'s "masking" framing, which turned out to misdescribe the mechanism). Verify
severity claims against actual authorization/data flow before repeating them.

---

## 4. Current git state

| | |
|---|---|
| Branch | `agent-harness` |
| HEAD | `c96dead` (Merge main into agent-harness) |
| vs `origin/main` | 0 behind (verify with `git fetch` — teammate pushes often) |
| Ahead of `main` | 2 commits (`1a81bff` Phase 1 fix + merge) |

### Uncommitted work (deliberate — not abandoned)

| File | What it is | Status |
|---|---|---|
| `PROJECT_OVERVIEW.md` | New architecture/orientation doc | Untracked, complete |
| `REMEDIATION_INVESTIGATION_REPORT.md` | Full Part A + Part B findings | Untracked, complete |
| `REMEDIATION_SESSION_STATE.md` | This file | Untracked |
| `README.md` | Accuracy fixes (port, migrations, CI section, etc.) | Modified, complete |
| `.gitignore` | Added `/SHARE/` — it holds live API keys | Modified, **security-relevant, keep** |
| `frontend/*` (4 files) | react-markdown rendering fix | Modified, complete, typechecks |

None of this is half-done. It is uncommitted because the user has not asked to commit it.

**The teammate pushes to `main` frequently.** Fetch before assuming currency. Past merges
touched `xagg.py`, `orchestrator.py`, and `graph_retriever.py` — the same files this
remediation edits — so check for overlap before starting work.

---

## 5. Environment quirks that WILL waste time if unknown

| Quirk | Symptom | Workaround |
|---|---|---|
| **Docker Desktop stops on its own** | `failed to connect to the docker API at npipe:...` | Relaunch `C:\Program Files\Docker\Docker\Docker Desktop.exe`, wait ~5-30s, then `until docker exec muhafiz-postgres pg_isready -U postgres -d muhafiz; do sleep 2; done` |
| **Docling fails on every PDF** | `InvalidCxxCompiler: Compiler: cl is not found` | `TORCHDYNAMO_DISABLE=1` — torch 2.13's inductor wants MSVC, never installed here. Verified to fully fix it. Affects ANY ingestion, not just one script |
| **Windows console mangles Urdu** | `UnicodeEncodeError: 'charmap' codec...` | `PYTHONIOENCODING=utf-8`, or write output to a file and Read it |
| **Python buffers stdout on redirect** | Background job's log file shows 0 lines while clearly running | Don't panic-kill it. Check the process CPU/working set, or query the DB for progress |
| **`/tmp` is not a real path for Windows Python** | `FileNotFoundError: '/tmp/...'` | Use the session scratchpad dir |
| **`.ps1` scripts fail to parse** | `The string is missing the terminator` | `SHARE/database/restore_dump.ps1` is UTF-8-without-BOM; Windows PowerShell 5.1 mangles its smart quotes. Run the commands directly instead |
| **`muhafiz_app` role vanishes after a DB restore** | `password authentication failed for user "muhafiz_app"` | Dumps carry no role definitions. Re-apply `migrations/015_app_least_privilege_role.sql`, then `ALTER ROLE muhafiz_app WITH PASSWORD 'dev'` |
| **`scripts/_smoketest.py` does not exist** | Referenced by `RUN.md`, never committed | Do register/login/chat by hand. `/api/chat` needs `session_id` (client-generated UUID) and an `x-csrf-token` header echoing the login cookie |
| **Vite proxy port mismatch** | Browser UI can't reach backend | Both `vite.config.ts` files hardcode `8001`; backend defaults to `8000`. Known, documented in README, NOT fixed (user chose docs-only) |
| **ngrok model tunnel URL rotates** | Embeddings/LLM 502 or connection refused | URL lives in `.env` as `MODEL_SERVER_BASE_URL`. If it's down, embedding work is blocked — stop and tell the user, don't retry silently |

### Standard startup

```bash
# Docker first, then:
cd "c:/Users/PMLS/Desktop/Muhafiz"
./.venv/Scripts/python.exe -m uvicorn src.main:app --host 127.0.0.1 --port 8000   # API
cd frontend && npm run dev            # chat  :5173
cd admin-frontend && npm run dev      # admin :5174
curl -s http://localhost:8000/health  # expect status ok
```

Use `./.venv/Scripts/python.exe` — the global Python lacks this project's dependencies.

---

## 6. Verified live-data facts (re-verify before relying on them)

Anything here can change if the teammate re-syncs. **Re-measure rather than trusting these
numbers**; they are a baseline for spotting change, not a substitute for checking.

- 73 cases; `case_id` = `fir-117-26`; `fir_number` = `117/26`
- `crime_category` = **statute lists** (`PPC`, `CNSA 1997`, `PECA 2016, PPC`) — NOT crime
  types. Upstream has no offence-category field; this is intentional, not a sync bug
- `investigation_status` = free-text Urdu, **empty string in 52/73**
- `police_station` = Urdu free text, city embedded, nationwide
- Chroma `muhafiz_kb` = **1054** chunks (704 fir_narrative / 264 old pdf / 74 roznamcha /
  8 pkm / 4 cms); Chroma↔Postgres case overlap **73/73**
- Chroma `muhafiz_community_reports` = **0 embeddings** ← XNETWORK is dead because of this
- Postgres `community_reports` = 19 rows (only **2** span >1 case)
- Graph: SAME_AS **1211** (pending 1187 / confirmed 19 / rejected 5); ASSOCIATED_WITH 252;
  OCCURRED_ON 568; **CONFLICTS_WITH 0**; **CROSS_VERSION_OF 0**
- `Incident.description` NULL on **73/73**; **188** OCCURRED_ON edges have
  `detail = "entry None"`
- `conflicts_checked_at` set on **0/73**
- Node counts: Officer 832, StructuredRecord 713, Person 478, … Vehicle 2, PhoneNumber 1,
  **Organization 0**

Verification queries are in `REMEDIATION_INVESTIGATION_REPORT.md` Appendix B.

---

## 7. Test-suite reality — this is the trap

`pytest tests/ src/pipeline/harness/compliance/ --ignore=tests/test_pdf_loader.py`
→ **~1611 collected, exit 0, all green.**

**Green means "internal logic unchanged," NOT "works on real data."** Every fixture uses
old synthetic shapes (`CASE-001`, `P-1`/`V-1`, tiers `"high"/"medium"/"low"` that do not
exist), and `tests/README.md` states "No network, ever" — `FakeGateway`/`FakeLLM`, sockets
blocked. The suite is **structurally incapable** of catching dataset drift.

Do not cite a green suite as evidence a data-shape fix is unnecessary. Phase 4 (real-data
fixtures) exists precisely for this and is **mandatory, not optional cleanup**.

`tests/test_pdf_loader.py` (6 tests) always errors here — the Docling/MSVC issue. Exclude
it; it is not a regression.

---

## 8. What NOT to redo

Already investigated and settled — do not re-derive:

- The XAGG root cause (statutes-not-crime-types, empty status) and its Phase 1 fix
- Whether `crime_category` holding statutes is a sync bug — **it is not**, confirmed
  against `src/ingestion/muhafiz_cases.py:78-91` and the upstream DBML schema
- Whether the jurisdiction issue is a security hole — **it is not**; role gate intact,
  P1 precision
- Whether the 231 purge target is safe — verified three independent ways
- Whether Phase 2 needed PDF re-ingestion — **no**, the new corpus has no source PDFs;
  records are API-derived
- Whether `--full` sync is idempotent — **yes**, verified via docstring + edge-purge
  mechanism + two passing regression tests + Chroma upsert-by-deterministic-ID

---

## 9. Suggested first actions in the new session

1. Read `REMEDIATION_INVESTIGATION_REPORT.md` (findings), `PROJECT_OVERVIEW.md`
   (architecture), this file (state).
2. `git fetch origin && git status` — confirm branch, check whether the teammate pushed.
3. Re-verify the live-data facts in §6 that matter to whatever is being worked on.
4. Ask the user which of the two pending decisions (§2) to proceed with.
5. **Do not** start Phase 3 implementation, purge anything, or touch the two out-of-scope
   items (§3.8) without explicit approval.
