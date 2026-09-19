# BACKEND_API.md

> Every endpoint below was read from the router source in [src/](../src/).
> Anything the code does not establish is marked **UNKNOWN**.

---

## 0. Conventions that apply to every endpoint

### 0.1 Authentication

`get_current_user` ([src/auth/jwt.py](../src/auth/jwt.py)) is the single authentication
dependency. It performs three steps **in this order**:

1. **CSRF (mutating methods only).** For `POST`, `PUT`, `DELETE`, `PATCH` it
   compares the `csrf_token` cookie against the `X-CSRF-Token` header. Missing
   either, or a mismatch → **403 `"CSRF token validation failed."`**
   *This check runs before the JWT is even read, so an unauthenticated mutating
   request without CSRF headers returns 403, not 401.*
2. **Token extraction.** Reads the `access_token` cookie; falls back to an
   `Authorization: Bearer <token>` header if the cookie is absent.
   No token → **401 `"Not authenticated"`**.
3. **Decode + lookup.** `jwt.decode(...)` with `JWT_SECRET_KEY` / HS256.
   A `JWTError` or a missing `sub` claim → **401 `"Invalid token"`**.
   A `sub` that resolves to no user → **401 `"User not found"`**.

`require_role(minimum_role)` wraps `get_current_user` and adds an ordinal role
check against `['investigator', 'supervisor', 'station-admin', 'platform-admin']`.
Below the threshold → **403 `"Role '<minimum_role>' or higher required"`**.
An unrecognized role string → **403 `"Invalid role configuration"`**.

### 0.2 RLS arming

Routers arm Postgres Row-Level Security via
[src/auth/rls_context.py](../src/auth/rls_context.py):

- `case_rls_dependency` (case-scoped) — used by `case_assignments` router-wide,
  and called directly by `cases.py`.
- `cross_case_rls_dependency` (RLS active, case dimension bypassed) — used
  router-wide by `admin`, `graph_review`, `community_admin`,
  `ingestion_quality_admin`, `sessions`, `attachments`, `projects`, and by
  `GET /api/cases/`.

### 0.3 Rate limits (slowapi, keyed on remote address)

| Endpoint | Limit |
|---|---|
| `POST /api/auth/register` | 5/minute |
| `POST /api/auth/login` | 10/minute |
| `POST /api/chat` | 60/minute |
| `POST /api/admin/kb/upload` | 10/minute |
| `POST /api/attachments` | 20/minute |
| `POST /api/cases/` | 20/minute |
| `POST` / `DELETE` on `/api/cases/{case_id}/assignments` | 20/minute |

Exceeding a limit is handled by slowapi's `_rate_limit_exceeded_handler`
(registered in [src/main.py](../src/main.py)) → **429**.

### 0.4 Generic error shapes

FastAPI returns `{"detail": "<string>"}` for `HTTPException`, and
`{"detail": [<validation errors>]}` with status **422** for Pydantic request-body
or query-parameter validation failures.

---

## 1. System

### `GET /health`
- **Auth:** none.
- **Params:** none.
- **Response 200:**
  ```json
  {
    "status": "ok" | "degraded",
    "version": "0.1.0",
    "llm_provider": "<config.LLM_PROVIDER>",
    "vector_store_status": "ok" | "error: <msg>",
    "database_status": "ok" | "error: <msg>",
    "documents_in_store": <int>
  }
  ```
- **Behaviour:** probes the Chroma collection count and executes `SELECT 1`
  against Postgres with a hard **3-second** `asyncio.wait_for` timeout.
  `status` degrades to `"degraded"` if either probe fails. Never returns non-200
  for a degraded dependency.

---

## 2. Auth — `/api/auth`

Source: [src/auth/routes.py](../src/auth/routes.py)

### `POST /api/auth/register`
- **Auth:** none. **Rate limit:** 5/min.
- **Body:** `{ "email": EmailStr, "password": str, "company_name": str | null }`
- **Validation:** `email` must be a valid email (Pydantic `EmailStr`);
  `password` must be **≥ 12 characters** (`password_minimum_length` validator —
  no complexity-class rules, by explicit design comment citing NIST guidance).
- **Response 200 (`UserResponse`):**
  `{ "id", "email", "role", "is_admin", "company_name", "plan" }`
- **Errors:** 400 `"A user with this email already exists."`; 422 on schema
  violation (including the password-length rule).
- **Notes:** role and plan are assigned by the DB defaults (`investigator`,
  `free`) — the request cannot set them. Registration does **not** log the user in;
  the frontend issues a follow-up login.

### `POST /api/auth/login`
- **Auth:** none. **Rate limit:** 10/min.
- **Body:** `{ "email": EmailStr, "password": str }`
- **Response 200:** `{ "message": "Login successful" }` plus two `Set-Cookie` headers:
  - `access_token` — HttpOnly, `samesite=lax`, `secure = (ENVIRONMENT != "development")`, `max_age = 604800` (7 days).
  - `csrf_token` — **not** HttpOnly (readable by JS), same flags/max-age.
- **Errors:** 401 `"Incorrect email or password"` (identical for unknown email
  and wrong password — no user enumeration).

### `POST /api/auth/logout`
- **Auth:** required (`get_current_user`) — therefore **CSRF header required**.
- **Body:** none.
- **Response 200:** `{ "message": "Logged out successfully" }`, deleting both cookies.
- **Errors:** 401 / 403 per §0.1.
- **Note:** the JWT itself is **not** revoked server-side; there is no
  denylist/blocklist table in the schema. Logout only clears cookies.

### `GET /api/auth/me`
- **Auth:** required.
- **Response 200 (`UserResponse`):** `{ "id", "email", "role", "is_admin", "company_name", "plan" }`
- **Errors:** 401.

---

## 3. Chat — `/api/chat`

Source: [src/main.py](../src/main.py)

### `POST /api/chat`
- **Auth:** required. **Rate limit:** 60/min.
- **Body:**
  ```json
  {
    "session_id": "<str, required>",
    "message": "<str, required>",
    "project_id": "<str|null>",
    "case_id": "<str|null>",
    "enable_web_search": false
  }
  ```
- **Permissions:** if a `case_id` is resolved (from the body, else from the
  session row), `gateway.check_case_access(case_id, user_id, role)` must pass →
  otherwise **403 `"Not assigned to this case"`**.
- **Response 200:** `text/event-stream`. Each frame is `data: <json>\n\n`.
  Event objects carry at minimum `step` and `status`; observed additional keys
  include `detail`, `ms`, `retry_num`, `sources`, `hop_count`,
  `graph_confidence`, `thinking`, `trace`, `delegate_to_legacy`.
  Step names emitted by the legacy orchestrator: `query_rewriter`, `router`,
  `attachments`, `retrieval`, `reranker`, `cross_reranker`, `evaluator`,
  `web_search`, `citation_validator`, `cross_case_finding`, `response`,
  `file_generation`, `title_generation`, `memory`.
  Additional step names emitted by the harness cutover path: `supervisor`,
  `timeline_building`, `data_quality`, `system`.
- **Error handling inside the stream:** any exception raised while generating is
  caught and emitted as a **normal 200 SSE frame**
  `{"step": "system", "status": "error", "detail": "<str>"}` — the HTTP status is
  already committed by then. QA must assert on the SSE payload, not the status code.
- **Side effects:** arms RLS (`set_case_scope`), creates the `sessions` row with
  its owner if missing, creates a `pipeline_runs` row, and writes
  `messages` / `pipeline_steps` rows.

---

## 4. Files — `/api/files`

Source: [src/main.py](../src/main.py)

### `GET /api/files/{file_id}/download`
- **Auth:** required.
- **Path param:** `file_id` — must parse as a UUID.
- **Response 200:** `FileResponse` with `Content-Disposition` filename and one of:
  `application/pdf`, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
  `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, else
  `application/octet-stream`. A stored `file_name` missing its extension is repaired on the way out.
- **Permissions (evaluated in this order):**
  1. Owner (`file_record.user_id == current_user.id`) → allowed.
  2. `platform-admin` → allowed.
  3. `file_record.case_id IS NULL` → allowed for `station-admin` and `platform-admin` only.
  4. `station-admin` with a non-null `case_id` → allowed only if `check_case_access()` passes.
  5. Everything else → denied.
- **Errors:** 400 `"Invalid file ID format"`; 404 `"File not found"`;
  404 `"File content no longer exists on server"`; 403 `"Unauthorized to access this file"`.

---

## 5. Sessions — `/api/sessions`

Source: [src/api/sessions.py](../src/api/sessions.py). Router-level dependency:
`cross_case_rls_dependency`. **All routes are owner-gated** (`session.user_id ==
current_user.id`); there is no role escalation on this router.

### `GET /api/sessions`
- **Auth:** required. **Query:** `project_id?`, `case_id?` (ANDed).
- **200:** `List[Dict[str, Any]]` — the caller's non-deleted sessions.

### `GET /api/sessions/{session_id}`
- **200:** `{ "history": [ ...messages... ] }`
- **Errors:** 404 `"Session not found."`; 403 `"Not authorized to access this session."`

### `DELETE /api/sessions/{session_id}`
- **200:** `{ "message": "Session deleted successfully" }` (soft delete — sets `deleted_at`).
- **Errors:** 404 / 403 as above.

### `PATCH /api/sessions/{session_id}`
- **Body:** `{ "title": str }` (required).
- **200:** `{ "message": "Session renamed successfully", "title": "<title>" }`
- **Errors:** 404 / 403 as above; 422 if `title` is missing.

### `GET /api/sessions/{session_id}/export`
- **Query:** `format` — `"json"` (default) | `"md"` | `"pdf"`.
- **200:** JSON body `{ "session": {...}, "history": [...] }` for `json`;
  a `FileResponse` (`text/markdown` or `application/pdf`) otherwise.
- **Errors:** 404 / 403 as above; **400 `"Invalid format. Use json, md, or pdf."`**

---

## 6. Profile — `/api/profile`

Source: [src/api/profile.py](../src/api/profile.py). **No RLS dependency is wired on this router.**

### `GET /api/profile`
- **Auth:** required. **200:** the user's context-profile record.

### `PUT /api/profile`
- **Body (`ProfileUpdate`, all three fields required):**
  `{ "context_text": str (max_length 1000), "preferred_language": str, "llm_mode": str }`
- **200:** the updated profile.
- **Validation:** only `context_text`'s max length is enforced. `preferred_language`
  and `llm_mode` are **free-form strings with no server-side allow-list** — the
  constrained value set exists only in the frontend `<select>` options.

---

## 7. Projects — `/api/projects`

Source: [src/api/projects.py](../src/api/projects.py). Router dependency:
`cross_case_rls_dependency` (a documented no-op — `projects` carries no RLS policy).
**Ownership-gated**, not role-gated.

| Method | Path | Body | 200 | Errors |
|---|---|---|---|---|
| GET | `/api/projects/` | — | `List[ProjectResponse]` (caller's own) | 401 |
| GET | `/api/projects/{project_id}` | — | `ProjectResponse` | 404 `"Project not found"`, 403 `"Not authorized to access this project"` |
| POST | `/api/projects/` | `{name, description?, domain_context?}` | `ProjectResponse` | 500 `"Failed to create project"`, 422 if `name` missing |
| PUT | `/api/projects/{project_id}` | `{name?, description?, domain_context?}` | `ProjectResponse` | 404 / 403 (ownership re-checked first), 404 `"Project not found"` |
| DELETE | `/api/projects/{project_id}` | — | `{"status": "deleted"}` | 404 / 403 |

`user_id` is taken from the authenticated user on create — a client-supplied
`user_id` is ignored (`ProjectCreate` has no such field).

---

## 8. Cases — `/api/cases`

Source: [src/api/cases.py](../src/api/cases.py)

Access is enforced by the `require_case_access(min_role=None)` dependency
factory, which arms `set_case_scope(case_id)` and then calls
`check_case_access(...)`.

### `GET /api/cases/`
- **Auth:** required. RLS: cross-case bypass.
- **200:** `List[CaseResponse]`. `platform-admin` receives **all** cases;
  every other role receives only cases they are assigned to (join on
  `case_assignments`).

### `GET /api/cases/{case_id}`
- **Permissions:** must be assigned to the case (any per-case role), or be `platform-admin`.
- **200:** `CaseResponse`.
- **Errors:** 403 `"Not assigned to this case"`; 404 `"Case not found"`.

### `POST /api/cases/`
- **Auth:** required. **Rate limit:** 20/min.
- **Body (`CaseCreate`, all optional):** `case_id`, `fir_number`, `crime_category`,
  `investigation_officer`, `police_station`, `incident_date` (date),
  `investigation_status`, `location`, `description`, `victim_info` (object),
  `suspect_info` (object).
- **Business rules:**
  - A missing `case_id` is generated as `CASE-<8 uppercase hex>`.
  - A supplied `case_id` must match `^[A-Za-z0-9._-]+$` → else **400
    `"case_id may only contain letters, numbers, '.', '_' and '-'"`**.
  - RLS is armed to the new `case_id` **before** the insert, so the `FOR ALL`
    policy's `WITH CHECK` passes.
  - Duplicate `case_id` → **409 `"Case '<id>' already exists"`**.
  - On success the creator is auto-assigned to the case with per-case role
    `investigator`, and an `admin_action` audit event is written.
- **200:** `CaseResponse`. **Errors:** 400, 409, 500 `"Failed to create case"`.
- **Note:** **any authenticated user can create a case.** There is no role gate here.

### `PUT /api/cases/{case_id}`
- **Permissions:** per-case role **`supervisor` or higher**, or `platform-admin`.
- **Body (`CaseUpdate`):** same fields as create minus `case_id`, all optional.
- **200:** `CaseResponse`. An empty payload short-circuits to a plain read.
- **Errors:** 403 `"Case role 'supervisor' or higher required"`; 404 `"Case not found"`.
- **Side effect:** `admin_action` audit event.

### `DELETE /api/cases/{case_id}`
- **Permissions:** per-case role `supervisor` or higher, or `platform-admin`.
- **200:** `{"status": "deleted"}`.
- **Side effect:** the audit event is written **before** the delete.
- **Note:** the handler does not verify the case exists — deleting an
  already-absent case still returns 200 (after the 403 gate passes).

---

## 9. Case assignments — `/api/cases/{case_id}/assignments`

Source: [src/api/case_assignments.py](../src/api/case_assignments.py).
Router dependencies: `case_rls_dependency`. **Every route requires
`require_role("station-admin")`.**

Additional station gate, `_require_station_match()`:
- `platform-admin` → skipped entirely.
- Caller with `police_station IS NULL` → **skipped, with a warning log**
  (documented bridge until migration 012 is backfilled).
- Otherwise the case must exist (**404 `"Case not found"`**) and
  `case.police_station` must equal `current_user.police_station`, else
  **403 `"Case belongs to a different police station"`**.

| Method | Path | Body | 200 | Errors |
|---|---|---|---|---|
| GET | `/` | — | `List[{user_id, email, role}]` | 403, 404 |
| POST | `/` | `{ "email": str, "role": str }` | `{"status": "assigned"}` | 404 `"No user found with email '<email>'"`, 403, 404 |
| DELETE | `/{user_id}` | — | `{"status": "unassigned"}` | 403, 404 |

- The target user is resolved **server-side by email**, because the admin user
  list is platform-admin-only.
- `role` is a free string in `CaseAssignmentCreate` — **no server-side allow-list**;
  the Postgres `user_role` enum is the only enforcement, so an invalid value
  surfaces as a database error rather than a clean 422. **Flagged for QA.**
- Assigning an already-assigned user updates the existing row's role rather than
  erroring ([direct_backend.py](../src/data_gateway/direct_backend.py) `assign_user_to_case`).
- Both mutations write an `admin_action` audit event.

---

## 10. Attachments — `/api/attachments`

Source: [src/api/attachments.py](../src/api/attachments.py). Router dependency:
`cross_case_rls_dependency`. **Ownership-gated.**

### `POST /api/attachments`
- **Auth:** required. **Rate limit:** 20/min. **Content type:** `multipart/form-data`.
- **Form fields:** `session_id` (str, required), `file` (upload, required).
- **Validation, in order:**
  1. Extension must be in `{.pdf .txt .md .csv .xlsx .xls .html .htm .docx .png .jpg .jpeg .webp}`
     → else **400 `"Unsupported file type '<ext>'. Supported: ..."`**
  2. Size ≤ **10 MB** (`MAX_UPLOAD_MB`) → else **413 `"Attachments are limited to 10MB."`**
  3. If the session exists and is owned by someone else → **403 `"Not authorized to attach files to this session."`**
     *A session that does not exist yet is allowed through by design — the client
     generates `session_id` before the first message.*
  4. At most **5** attachments per session (`MAX_ATTACHMENTS_PER_SESSION`) →
     else **400 `"A conversation can hold at most 5 attachments."`**
- **200:** the created attachment record **with `extracted_text` removed**.
  `status` is `"ready"` or `"failed"`; a failed extraction still creates the row
  and returns 200 with `error_message` populated.
- **Errors:** 400, 413, 403; **503 `"Attachments are not enabled yet — run migration 003 ..."`**
  when the `session_attachments` table does not exist.
- **Business rules:** extracted text is truncated to **12 000 characters**
  (`MAX_CHARS_PER_ATTACHMENT`); `char_count` records the untruncated length. The
  uploaded bytes are written to a temp file, parsed, and deleted — attachments
  are never persisted to disk.

### `GET /api/attachments?session_id=<id>`
- **200:** attachment metadata list (never `extracted_text`).
- **Errors:** 403 `"Not authorized to access this session."`

### `DELETE /api/attachments/{attachment_id}`
- **200:** `{"status": "deleted", "attachment_id": "<id>"}`
- **Errors:** 404 `"Attachment not found"`; 403 `"Not authorized to delete this attachment"`.

---

## 11. Admin — `/api/admin`

Source: [src/api/admin.py](../src/api/admin.py). Router dependency:
`cross_case_rls_dependency`. **Every route requires `require_role("platform-admin")`
except `GET /eval/entity-resolution`, which requires `supervisor`.**

| Method | Path | Query / body | 200 response | Notes / errors |
|---|---|---|---|---|
| GET | `/metrics` | — | `gateway.get_system_metrics()` | |
| GET | `/instrumentation` | — | `{ "tables": {error_logs, ingestion_jobs, session_attachments: bool}, "ready": bool, "error_queue": {...} }` | Probes real table existence |
| GET | `/usage` | `days=30`, `granularity=day\|hour` | `{days, granularity, total_requests, timeseries, routing}` | **400** `"granularity must be 'day' or 'hour'"` |
| GET | `/verifier/stats` | `days=30` | `analytics.verifier_stats(runs)` | |
| GET | `/latency` | `days=30`, `granularity=day\|hour` | `{days, granularity, summary, timeseries, by_route, by_step}` | **400** on bad granularity |
| GET | `/errors` | `limit=100`, `offset=0`, `days=30`, `severity?`, `module?`, `error_type?` | `{ "errors": [...], "facets": {...} }` | No bounds enforced on `limit` |
| GET | `/errors/trend` | `days=30`, `granularity=day` | `{days, granularity, total, timeseries}` | `granularity` is **not** validated here |
| GET | `/kb/stats` | — | total chunks + per-document counts | |
| GET | `/eval/entity-resolution` | — | contents of `data/eval/resolution_metrics.json` | **Requires only `supervisor`.** Returns `{"generated_at": null, "error": "..."}` (still HTTP 200) when the file is missing or unreadable |
| GET | `/kb/jobs` | `limit=50`, `offset=0` | ingestion-job rows | |
| GET | `/audit-logs` | `limit=100`, `offset=0`, `days=30`, `event_type?`, `case_id?`, `user_id?` | audit-log rows | |
| POST | `/kb/upload` | multipart `file` | `{job_id, filename, status:"processing", file_size_bytes}` | See below |
| DELETE | `/kb/documents/{source_file}` | — | `{deleted_chunks, source_file}` | Deletes from Chroma **and** `chunk_fulltext` **and** the `documents` rows; writes audit event |
| GET | `/runs` | `limit=50`, `offset=0`, `route_filter?` | pipeline-run rows | |
| GET | `/runs/{run_id}/steps` | — | step rows | |
| GET | `/files` | `limit=50`, `offset=0` | generated-file rows | |
| DELETE | `/files/{file_id}` | — | `{"status":"success","deleted":"<id>"}` | **404** `"File not found"`; deletes the row then best-effort deletes the bytes |
| GET | `/mcp-calls` | `limit=50`, `offset=0` | MCP tool-call rows | |
| POST | `/mcp-demo` | `{ "query": str }` | `{query, extracted_params, sql, results, run_id}` | **502** `"MCP query failed: <exc>"`; always logs an `mcp_tool_calls` row in a `finally` block |
| GET | `/users` | `limit=50`, `offset=0` | user rows | |

**`POST /api/admin/kb/upload` validation order:**
1. Extension in the allow-list → else **400**.
2. Size ≤ `MAX_UPLOAD_SIZE_MB` (default **50 MB**) → else **413
   `"File is <N>MB; the limit is 50MB."`**
3. Filename is basename-ed; on collision the file is saved as
   `name__2.ext`, `name__3.ext`, … (originals are never overwritten).
4. `validate_file(dest)` — magic-byte/extension match plus a zip-bomb guard for
   `.docx`/`.xlsx`. On failure the file is unlinked and **400** is returned with the
   validation message.
5. An `ingestion_jobs` row is created, an audit event written, and chunk+embed
   runs as a `BackgroundTasks` job. A background failure is recorded on the job
   row (`status: "failed"`), **not** returned to the caller.
   Zero extracted chunks is also recorded as `failed`
   (`"No text could be extracted from this file."`).

---

## 12. Graph review — `/api/admin/graph-review`

Source: [src/api/graph_review.py](../src/api/graph_review.py). Router dependency:
`cross_case_rls_dependency`. **Every route requires `require_role("supervisor")`.**
This queue is deliberately cross-case by design.

### Identity (`SAME_AS`) queue

| Method | Path | Params / body | 200 | Errors |
|---|---|---|---|---|
| GET | `/pending` | `case_id?`, `tier?` | `{"pending": [...], "count": n}` — each entry has `edge_id, tier, confidence, basis, as_of, source_doc_id, source_chunk_id, mention{}, candidate{}`, newest first | |
| GET | `/stats` | — | `{ "<tier>": { "<status>": count } }` | |
| GET | `/queue/history` | `case_id?`, `days=30` (clamped 1–365) | `{case_id, days, snapshots: [...]}` | Omitting `case_id` returns the **global** rollup, not per-case |
| POST | `/{edge_id}/confirm` | body `{}` (`ReviewAction`, no fields) | `{"status":"confirmed","new_edge_id": <int>}` | **404** `"Review edge not found"`, **409** `"This match has already been reviewed"`, **500** `"Failed to write confirmation"` |
| POST | `/{edge_id}/reject` | body `{}` | `{"status":"rejected","new_edge_id": <int>}` | **404**, **409**, **500** `"Failed to write rejection"` |

`edge_id` is an **integer** AGE edge id. "Already reviewed" is determined by
`superseded_by IS NOT NULL`, never by re-reading `status` (writes are append-only).
Both mutations write `graph_review_confirm` / `graph_review_reject` audit events.

### Citation (`CITES`) queue — parallel, not merged

| Method | Path | 200 | Errors |
|---|---|---|---|
| GET | `/citations/pending` | `{"pending": [{edge_id, confidence, basis, as_of, source_doc_id, citing_case{}, cited_case{}}], "count": n}` | |
| POST | `/citations/{edge_id}/confirm` | `{"status":"confirmed","new_edge_id": n}` | **404** `"Citation edge not found"`, **409** `"This citation has already been reviewed"`, **500** |
| POST | `/citations/{edge_id}/reject` | `{"status":"rejected","new_edge_id": n}` | same |

Audit event types: `graph_review_citation_confirmed` / `..._rejected`.

### Prioritized / batch queue

| Method | Path | Params | 200 | Errors |
|---|---|---|---|---|
| GET | `/queue` | `include_deprioritized=true` | `{"queue": [{edge_id, tier, priority_score, why, group_id, deprioritized, original_basis, mention{}, candidate{}}], "count": n}` | |
| GET | `/queue/groups` | — | `{"groups": [{group_id, member_count, top_priority_score, why, edge_ids, deprioritized}], "count": n}` | Ungrouped candidates become `UNGROUPED-<edge_id>` singletons |
| POST | `/queue/reprioritize` | — | `{"rescored": <int>}` | Re-scores and re-groups; never confirms/rejects |
| POST | `/queue/batches/{group_id}/confirm` | body `{}` | `{"group_id", "decision":"confirm", "results":[...]}` | **404** `"No pending candidates found in this batch"` |
| POST | `/queue/batches/{group_id}/reject` | body `{}` | `{"group_id", "decision":"reject", "results":[...]}` | **404** as above |

A batch action is internally N single-edge `confirm_match()`/`reject_match()`
calls. A per-member 404/409 does **not** abort the batch — it is reported inline
as `{"edge_id", "error", "status_code"}` and the overall response is still 200.

### Consistency findings

| Method | Path | 200 | Errors |
|---|---|---|---|
| GET | `/consistency-findings` | `{"findings": [...], "count": n}` — unacknowledged only, newest first | |
| POST | `/consistency-findings/{finding_id}/acknowledge` | `{"finding_id", "acknowledged": true}` | **404** `"Finding not found or already acknowledged"` |

Acknowledging records that a human looked at the finding; it never touches the
`SAME_AS` edge. Audit event: `graph_review_consistency_finding_acknowledge`.

---

## 13. Community admin — `/api/admin/community`

Source: [src/api/community_admin.py](../src/api/community_admin.py).
Both routes require `require_role("supervisor")`; router dependency is
`cross_case_rls_dependency`.

| Method | Path | 200 |
|---|---|---|
| GET | `/staleness` | `community_detection.get_staleness()` — read-only drift heuristic, no side effect |
| POST | `/refresh` | `{run_id, node_count, edge_count, community_count, attempted, written, skipped}` |

`POST /refresh` always runs `detect_communities()` **and**
`summarize_communities()` regardless of the staleness heuristic — it is the
manual full sweep (there is no cron in this codebase).

---

## 14. Ingestion quality — `/api/admin/ingestion-quality`

Source: [src/api/ingestion_quality_admin.py](../src/api/ingestion_quality_admin.py).
All routes require `require_role("supervisor")`.

| Method | Path | Params | 200 | Errors |
|---|---|---|---|---|
| GET | `/runs` | `source?` (`ingest_file` \| `sync_muhafiz_data`), `limit=50` **clamped to 1–200** | `{"runs": [...], "count": n}`, newest first | |
| GET | `/flagged` | — | `{"flagged": [...], "count": n}` — `flagged_for_review = true` only | |
| POST | `/{run_id}/acknowledge` | — | `{"run_id", "acknowledged": true}` | **404** `"Ingestion run not found"`; **409** `"This run is not currently flagged"` |

Acknowledging clears `flagged_for_review`/`flagged_reason` on that run only so
the next same-source run stops inheriting the flag. It never re-evaluates rates
and never writes to the graph. Audit event:
`ingestion_quality_acknowledge`.

---

## 15. Endpoints that do **not** exist

Confirmed absent from the codebase (searched across `src/`):

- Password reset / forgot-password — **no endpoint, no token table, no mailer.**
- Email verification / account activation — **none.**
- Refresh-token endpoint or token rotation — **none.**
- User self-service role change, account deletion, or password change — **none.**
- Any user-facing document-ingestion endpoint. Removed by design; the comment in
  [src/main.py](../src/main.py) states ingestion moved to `/api/admin/kb/*` and normal
  users use `/api/attachments` instead.
