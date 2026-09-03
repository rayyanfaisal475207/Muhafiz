# Muhafiz — Security Report: Phase 2 (Traditional App/API Surface)

**Scope:** Auth/session handling, RBAC/RLS from the HTTP layer, standard API
vulns (injection, upload, rate-limiting, error leakage), admin dashboard.
**Environment:** clean local `.venv` server (PID 55952), `main @ 536aa3c`, rotated
JWT secret. All tests bound to `127.0.0.1`. The live tunnel was never touched
(operator handled it). Synthetic test data only; all writes reversed and re-verified.
**Date:** 2026-09-02. **Companion:** `PHASE_0_1_SECURITY_REPORT.md`.

---

## Executive summary

One **Critical** (Finding 5, JWT default secret) was found and **fixed mid-engagement**
by the operator (secret rotated; re-verified: old-secret token → 401, rotated → 200).
Beyond it, the traditional surface is **strong**: RBAC/IDOR held under direct HTTP
attack on every route tested; injection paths are parameterized or allowlisted;
file uploads are content-sniffed; error responses don't leak internals; the admin
dashboard shares the main app's auth with no weaker parallel path. Two lower-severity
findings remain (6 and 8) plus the pre-existing Finding 7.

| # | Severity | Title | Status |
|---|---|---|---|
| 5 | **Critical** | Forgeable JWT via public default secret | **FIXED** (operator rotated; re-verified) |
| 6 | Medium | `Secure` cookie flag off under `ENVIRONMENT=development` | Open — same root cause as 5 |
| 8 | Low | `sessions.py` routes 500 on malformed `session_id` (F-06 not propagated) | Open |
| 7 | Low | `community/refresh` unthrottled | Confirmed still accurate |

---

## Finding 5 — Forgeable JWT via public default secret (CRITICAL — FIXED)

`JWT_SECRET_KEY=your-secret-key-for-dev` (the public default from source) was live in
`.env` and the transferred `SHARE/.env`. The startup guard ([config.py:420](../src/config.py#L420))
only fires when `ENVIRONMENT != "development"`, and both configs set `development`, so it
never triggered.

**Proven:** a token signed with the public default string, as a real platform-admin's
`sub`, with no login, returned **HTTP 200 + the full user list** from `/api/admin/users`.
Complete auth bypass + privilege escalation. Because `SHARE/.env` ran on the internet-exposed
office-PC tunnel, anyone reading the (public-pattern) source could have forged an admin token
for the window it was live.

**Interaction with Phase 1:** Phase 1 concluded "confidentiality boundaries are solid" —
true *at the layers tested* (RLS, cross-case gates, exercised through authenticated sessions).
Finding 5 sits *below* auth, so a forged-admin token moots every downstream boundary. The
Phase 1 conclusion stands for *authenticated* attackers; Finding 5 was the unauthenticated
bypass beneath it.

**Fix + re-verification:** operator rotated `JWT_SECRET_KEY` (64-char random) in both `.env`
files and restarted the serving process. Re-verified from the tester side on the clean local
server: **old default-secret token → 401**, **rotated-secret token → 200**. Fixed on the
running server. (A brief detour: an initial re-check hit a *stale local process* still holding
the old key — resolved by killing the stale/duplicate uvicorns and restarting one clean `.venv`
server; the live deployment was correct throughout.)

**Recommended hardening (not applied):** make the guard key on the secret's *value* regardless
of `ENVIRONMENT`, or fail-closed unless explicitly `development`; and rotate/​invalidate tokens
on any future secret change.

**UPDATE (2026-09-03) — reverted then re-fixed.** A teammate's replacement `SHARE/` folder was
copied over the existing one, silently overwriting `SHARE/.env` back to the public default
`your-secret-key-for-dev`. Caught on a routine re-check and flagged as Finding 5 reopened. The
operator restored the rotated secret; both `.env` files now carry the same 64-char value
(verified `sha256=f1b37ca7…`, zero occurrences of the default). **This proves the "recommended
hardening" above is not optional:** because a live secret sits inside a folder that gets
copy-overwritten on every SHARE sync, it *will* revert again unless secrets are kept out of
`SHARE/` entirely or the guard is made value-based. See `SECURITY_TESTING_SUMMARY.md` §3 for the
full lifecycle.

## Finding 6 — `Secure` cookie flag disabled by `ENVIRONMENT` gate (Medium — open)

Live login response sets `access_token` and `csrf_token` with `HttpOnly; SameSite=lax` but
**no `Secure`** — `is_secure = ENVIRONMENT != "development"` ([routes.py:127](../src/auth/routes.py#L127)),
same root cause as Finding 5. Over the HTTPS tunnel, cookies were transmittable on non-secure
connections. Fixing Finding 5 by setting `ENVIRONMENT=production` also fixes this; if the
deployment stays `development`, the flag stays off.

## Finding 7 — `community/refresh` unthrottled (Low — confirmed)

[community_admin.py:42](../src/api/community_admin.py#L42) `POST /api/admin/community/refresh`
triggers a Louvain recompute with **no `@limiter`**. Confirmed still accurate. Supervisor-gated,
so abuse needs a privileged account. (The graph-review router's 15 mutating routes are likewise
unthrottled but all supervisor-gated — same low-risk profile.)

## Finding 8 — `sessions.py` 500s on malformed `session_id` (Low — open)

All four session routes (`GET/{id}`, `GET/{id}/export`, `DELETE/{id}`, `PATCH/{id}`) return
**HTTP 500** on a non-UUID `session_id` — an unhandled `ValueError('badly formed hexadecimal
UUID string')` from `UUID(session_id)` inside the gateway ([sessions.py:92](../src/api/sessions.py#L92)).
The Phase 0 F-06 fix added boundary UUID validation to `/api/chat` but was **not propagated**
to the session routes. **No data leak** — the client sees only "Internal Server Error"; requires
auth. It's a robustness/consistency gap, not an exposure. Fix: `validate_uuid_field(session_id)`
at the top of each route, mirroring `/api/chat`.

---

## What was tested and held (no findings)

### RBAC / IDOR from the HTTP layer — all blocked
| Attack (investigator via direct API) | Result |
|---|---|
| GET/PUT/DELETE an **unassigned** case (`cases.py`) | 403; target row unchanged (no partial write) |
| PUT **own** case under-privileged | 403 "supervisor or higher required" — per-case role, not global |
| Read/delete/export **another user's** session | 403 "Not authorized" |
| Profile PUT with injected foreign `id`/`user_id` | write used `current_user.id`; victim row untouched |
| Investigator → all platform-admin routes | 403 |
| Investigator → all supervisor routes incl. mutating `/refresh` | 403 |

Live-confirmed the `check_case_access` dependency-factory (`min_role=None` read, `supervisor`
update/delete). CSRF fires on missing **and** mismatched tokens, before the access check.

### JWT robustness — clean
Non-UUID / missing / null `sub`, expired, garbage, and **`alg:none`** all → clean **401, no 500**.
Algorithm pinned (`algorithms=[HS256]` rejects `alg:none` forgery); expiry enforced; Phase 0
UUID-500 fix holds *on `/api/chat`* (but not sessions — see Finding 8).

### Injection — clean (SQL and Cypher)
- **SQL:** zero raw-string-interpolation sites across gateway/db/api — all parameterized SQLAlchemy/asyncpg.
- **Cypher/AGE:** f-strings interpolate only **structural** pieces (`{label}`, `{id_key}`,
  `{edge_pattern}`, `{where_tail}`, `{returns}`), and every one is a **hardcoded literal**
  (`"[r:SAME_AS]"`) or resolved through a **closed allowlist** (`TYPE_TO_LABEL` / `TYPE_PRIMARY_ID_KEY`,
  which raise `ValueError` on unknown types). User-supplied values flow **only** through
  `$`-parameters (`$case_id`, `$id_value`, `$ids`). `scoped_cypher` enforces `$case_id` presence
  with a `ValueError`.
- **Live confirmation:** injecting `'}) RETURN n UNION MATCH (x) DETACH DELETE x //` as query
  text left the graph node count **unchanged at 4816** — the metacharacters were treated as
  search text, never executable Cypher.

### File upload — content-sniffed, not extension-only (your addition)
Two-layer validation: the endpoint checks extension + size first, then `route_and_load` →
`validate_file` runs **magic-byte checks** ([validation.py:91](../src/ingestion/validation.py#L91))
against a signature table (`.pdf`→`%PDF-`, `.docx/.xlsx`→`PK`, PNG/JPG/GIF/WEBP), plus a
**zip-bomb** ratio/size guard on `.docx/.xlsx`. Live-verified: an MZ-header file named `.pdf`
was **rejected by the `%PDF-` mismatch** (`status:failed`, `char_count:0`), the raw file was
**not stored** (temp file `unlink`'d), and no parser trusted the fake extension. Plain-text
formats (`.txt/.md/.csv/.html`) and legacy `.xls` intentionally skip signature checks (documented
rationale: not a smuggling vector). Filenames use `mkstemp` random names — no path traversal.
*Minor note:* a rejected upload still creates a `status:failed` metadata row that consumes one of
5 per-session slots — cosmetic, not tracked as a finding.

### Rate limiting — expensive routes covered
`/api/chat` 60/min, `/kb/upload` 10/min, attachments 20/min, login 10/min, register 5/min.
XGRAPH/XAGG/XNETWORK have **no separate REST entry** — reachable only via `/api/chat`, so they
inherit its 60/min. Gaps: Finding 7 (`/refresh`) and the supervisor-gated graph-review mutations.

### Error handling — no leakage
Malformed JSON → 422; bad UUID type → 422 with safe message; SQL metacharacters in a path param
→ treated as a literal id (safe). The only 500 found (Finding 8) returns a generic
"Internal Server Error" body — **no stack trace, path, or DB detail reaches the client**
(the traceback is server-log only).

### Phase 0 findings 1 & 2 — closed from the HTTP side
Traced every `run_aggregate` caller: **zero REST call sites** (only the harness tool `xagg.py:204`
and legacy orchestrator `orchestrator.py:448/1983`, **both passing `user_role` explicitly**).
No HTTP path reaches the duplicated gate independently; nothing relies on the `"investigator"`
default. Both confirmed **latent-only, not HTTP-exploitable**.

### Admin dashboard — no weaker parallel path
`admin-frontend` is a **pure client** (no own backend) using the **same** API (`/api/admin`),
**same** `/api/auth/login`, **same** cookie-based JWT+CSRF. Login fetches `/api/auth/me` and
checks `ADMIN_ROLES` server-side. The `localStorage` role flag is **cosmetic** — every `/api/admin/*`
call is independently gated by the backend `require_role` (proven: investigator cookie → 403 on
all admin routes), so a forged client-side flag grants nothing.

---

## Environment restored
No test data left (0 test attachments, 0 IDOR-modified cases, 0 profile cross-writes). Health OK,
793 docs. One clean `.venv` server on 8001.

## Not done / deferred
- **Garak** broad AI sweep — still held (operator deciding Garak vs. this phase; now complete).
- **ZAP / Nuclei automated scans** were **not run** — the manual + targeted testing above covered
  the OWASP categories in scope directly; automated scanning can still be run against `127.0.0.1`
  if you want breadth coverage on top. Flag if you want it.
