# PROJECT_REQUIREMENTS.md

> Every requirement below is derived from implemented, runnable code — routers,
> stores, components, and their validation logic — not from planning documents.
> Cross-references point to [BACKEND_API.md](BACKEND_API.md),
> [AUTH_FLOW.md](AUTH_FLOW.md), [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md), and
> [FRONTEND_FEATURES.md](FRONTEND_FEATURES.md) for full detail; this file states
> the *requirement*, they state the *implementation*.

---

## 1. Account registration

**Feature:** a visitor creates an account via `POST /api/auth/register`.

- **Expected behaviour:** submitting a valid email + a password of at least 12
  characters creates a user with role `investigator` and plan `free`; the
  client then logs in automatically.
- **Business rules:** role and plan are never client-settable. Duplicate email
  is rejected.
- **Validation rules:** email must be syntactically valid (Pydantic
  `EmailStr`); password length ≥ 12, no complexity-class rule.
- **Permissions:** unauthenticated (anyone may register); rate-limited to
  5/minute per IP.
- **Known gap:** the chat app's client-side password field only enforces
  `minLength={8}`, weaker than the server's 12-character rule — see
  [AUTH_FLOW.md](AUTH_FLOW.md) §1.

## 2. Login

**Feature:** `POST /api/auth/login`.

- **Expected behaviour:** correct email + password sets an HttpOnly
  `access_token` cookie (7-day JWT) and a readable `csrf_token` cookie;
  `GET /api/auth/me` then returns the profile.
- **Business rules:** the same 401 message is returned for "no such user" and
  "wrong password" — no account enumeration.
- **Validation:** none beyond well-formed email/password fields.
- **Permissions:** unauthenticated; rate-limited to 10/minute per IP.

## 3. Logout

**Feature:** `POST /api/auth/logout`.

- **Expected behaviour:** clears both cookies; the chat app also resets its
  chat/case/project/session client state and clears `LAST_SESSION_KEY`.
- **Business rule:** the JWT itself is **not** server-side revoked — a captured
  token remains valid until its 7-day expiry regardless of logout.
- **Permissions:** requires authentication (and therefore a valid CSRF header).

## 4. Session verification / "who am I"

**Feature:** `GET /api/auth/me`.

- **Expected behaviour:** returns the caller's `id, email, role, is_admin,
  company_name, plan`, re-read from the database on every call (the JWT itself
  carries no role claim), so a role change takes effect on the very next request.
- **Permissions:** requires a valid, non-expired JWT.

## 5. Password reset / email verification

**Not implemented.** No requirement exists in the code for either capability —
no reset-token table, no mailer, no verification flag. See
[AUTH_FLOW.md](AUTH_FLOW.md) §§4–5.

## 6. Ask a question (chat)

**Feature:** `POST /api/chat` — the platform's central capability.

- **Expected behaviour:** the user's message is classified into one of nine
  retrieval routes, evidence is retrieved and verified, and the answer streams
  back over Server-Sent Events with a live per-step trace. A completed answer
  is grounded — every claim traces to a cited chunk — or the system abstains
  rather than answer ungrounded.
- **Business rules:**
  - A message may optionally target a `project_id`/`case_id`. If the resolved
    `case_id` is not one the caller is assigned to (and they are not
    `platform-admin`), the request is rejected with 403 **before** any
    retrieval work happens.
  - `enable_web_search` is a strictly per-message opt-in; the system never
    silently reaches for the web as a fallback from a failed retrieval.
  - Cross-case answers (XGRAPH/XAGG/XNETWORK routes) are gated to
    `supervisor`/`station-admin`/`platform-admin` — an `investigator` cannot
    receive a cross-case answer regardless of how the question is phrased.
  - A low-confidence (<0.85) cross-case citation must be hedged with an
    explicit uncertainty phrase in the answer's own language (English or
    Urdu); an unhedged low-confidence citation causes the whole answer to be
    discarded and regenerated.
  - Retrieval retries at most once (`MAX_RETRIES=1`) before the system
    abstains rather than guessing.
- **Validation:** `session_id` and `message` are required string fields.
- **Permissions:** authenticated; rate-limited to 60/minute per IP.
- Full retrieval/generation/verification detail: [AI_RAG_ARCHITECTURE.md](AI_RAG_ARCHITECTURE.md).

## 7. Conversation history / sessions

**Feature:** list, open, rename, delete, and export a conversation.

- **Expected behaviour:** the sidebar lists the caller's own non-deleted
  sessions (optionally filtered by project/case); opening one loads its full
  message history; deleting is a soft delete (`deleted_at` set, not a hard
  row delete); exporting produces JSON, Markdown, or PDF.
- **Business rules:** ownership is absolute — every session route compares
  `session.user_id` to the caller's own id, with **no role-based override** (not
  even `platform-admin` can read another user's session through this router).
- **Validation:** the export `format` query param must be `json`, `md`, or
  `pdf`, else 400.
- **Permissions:** authenticated + owner.

## 8. Chat attachments (per-conversation)

**Feature:** attach a file to a single conversation for the model to read as context.

- **Expected behaviour:** an uploaded file is text-extracted once and injected
  into that conversation's prompt. It is **never embedded, indexed, or visible
  to any other conversation or user** — this is the defining distinction from
  knowledge-base ingestion.
- **Business rules:** at most 5 attachments per session; a session that does
  not yet exist (client-generated id, first message not yet sent) is a valid
  upload target; a session owned by someone else is rejected.
- **Validation:**
  - Extension allow-list: `pdf, txt, md, csv, xlsx, xls, html, htm, docx, png,
    jpg, jpeg, webp`.
  - Size ≤ 10 MB.
  - Extracted text is capped at 12 000 characters in the prompt; the full
    character count is still recorded.
- **Permissions:** authenticated + session ownership; 20/minute rate limit.
- **Failure behaviour:** a file that yields no extractable text is still saved
  (status `failed`, with `error_message`) rather than silently discarded — the
  UI must surface the failure, not hide it.

## 9. Case management

**Feature:** create, view, edit, and delete investigation cases.

- **Expected behaviour:** any authenticated user may create a case; the
  creator is automatically assigned to it as `investigator`. Listing shows
  every case for `platform-admin`, and only assigned cases for everyone else.
- **Business rules:**
  - Editing or deleting a case requires a **per-case** role of `supervisor` or
    higher (or global `platform-admin`) — a global supervisor who is only an
    `investigator` on this specific case cannot edit or delete it.
  - A supplied `case_id` must match `^[A-Za-z0-9._-]+$`; an omitted one is
    auto-generated as `CASE-<8 hex>`.
  - Duplicate `case_id` on create is rejected (409), not silently overwritten.
  - Every create/update/delete writes an `admin_action` audit-log entry.
- **Validation:** `incident_date` must parse as a date; `victim_info`/`suspect_info`
  are free-form JSON objects with no further schema enforcement.
- **Permissions:** create — any authenticated user; read — assigned or
  platform-admin; update/delete — per-case supervisor+ or platform-admin.
  Create is rate-limited to 20/minute.

## 10. Case assignment management

**Feature:** assign or unassign a user to/from a case, with a per-case role.

- **Expected behaviour:** a station-admin (or higher) assigns a colleague by
  **email** (not raw user id, since the admin user list is platform-admin-only).
- **Business rules:**
  - Below `platform-admin`, a station-admin may only manage assignments for
    cases at their **own police station** — unless their own `police_station`
    is unset, in which case the check is bypassed with a warning log
    (a documented, temporary bridge pending a backfill migration).
  - Assigning an already-assigned user **updates** their role rather than
    erroring or duplicating the row.
  - `role` on the request body is a free string with **no server-side
    allow-list validation** — an invalid value surfaces as a database error,
    not a clean 422. (QA: exercise this boundary.)
  - Every assign/unassign writes an audit-log entry.
- **Permissions:** `station-admin` or higher, plus the station-match rule above;
  rate-limited to 20/minute.

## 11. Knowledge-base ingestion

**Feature:** upload a document into the shared, cross-user knowledge base.

- **Expected behaviour:** the file is validated, stored under `DOCUMENTS_DIR`,
  and chunked/embedded in the background; the caller polls job status via
  `GET /api/admin/kb/jobs`.
- **Business rules:**
  - Never overwrites an existing file of the same name — a colliding filename
    is disambiguated (`name__2.ext`, `name__3.ext`, …), because the original
    bytes are considered part of the evidentiary record.
  - A background failure (extraction error, zero extractable chunks) is
    recorded on the job row, never surfaced synchronously to the uploader.
  - Deleting a document removes it from the vector store, the full-text index,
    and the `documents` table together — these three must never drift apart.
- **Validation:**
  - Extension allow-list identical to attachments, minus the 5-file cap.
  - Size ≤ `MAX_UPLOAD_SIZE_MB` (default 50 MB).
  - Magic-byte/extension match plus a zip-bomb guard for `.docx`/`.xlsx`,
    checked **synchronously** so a malformed upload 400s immediately instead
    of appearing to succeed and then failing silently in the background.
- **Permissions:** `platform-admin` only; 10/minute rate limit.
- **Explicit non-requirement:** normal (non-admin) users cannot ingest into the
  shared knowledge base — this was a deliberate product decision, documented
  in code, that replaced an earlier user-facing ingestion page.

## 12. Entity-resolution review (graph review queue)

**Feature:** a human reviewer confirms or rejects a system-proposed identity
match (`SAME_AS`) or case citation (`CITES`).

- **Expected behaviour:** every pending match shows the mention, the candidate,
  a confidence score, and a **human-readable basis** — not a bare percentage —
  so a reviewer can actually judge the evidence, not rubber-stamp a number.
  Confirming or rejecting writes a **new** edge that supersedes the pending
  one; the underlying graph nodes are never physically merged.
- **Business rules:**
  - CNIC-tier auto-merges never appear in this queue — they need no human decision.
  - An edge that has already been decided cannot be decided again (409).
  - This queue is **deliberately cross-case** by product design — finding the
    same real person across different cases is the entire point — so RLS is
    armed but the case dimension is intentionally bypassed here.
  - A batch action (confirm/reject a group) is internally one independent
    single-edge decision per member; one member already decided or missing
    does not abort the rest of the batch.
  - Reprioritizing the queue never confirms or rejects anything — it only
    re-scores and re-groups.
  - Consistency findings never touch the underlying `SAME_AS` edge — acknowledging
    one only records that a human looked at it.
- **Permissions:** `supervisor` or higher.

## 13. Admin analytics dashboard

**Feature:** system-wide usage, latency, error, and knowledge-base metrics.

- **Expected behaviour:** charts reflect real rows from `pipeline_runs`,
  `pipeline_steps`, `error_logs`, `ingestion_jobs`, plus the live Chroma
  collection count — nothing is stubbed. When the underlying observability
  tables have not been migrated in yet, the dashboard shows an explicit "run
  migration 003" banner rather than an empty chart that looks like a healthy,
  silent system.
- **Business rules:** the instrumentation check probes real table existence —
  it does not infer "table missing" from "zero rows returned", which would be
  indistinguishable from a genuinely quiet system.
- **Permissions:** `platform-admin` only, except the entity-resolution eval
  metrics view, which is `supervisor`+.

## 14. Audit log

**Feature:** a queryable history of privileged actions.

- **Expected behaviour:** every case create/update/delete, assignment
  change, KB upload/delete, graph-review decision, and ingestion-quality
  acknowledgment writes a row with an `event_type`, the acting `user_id`, an
  optional `case_id`, and a JSON `details` payload.
- **Business rules:** an audit row survives the deletion of its referenced
  user or case (`ON DELETE SET NULL`, not CASCADE) — the audit trail is never
  silently erased by an unrelated deletion.
- **Permissions:** `platform-admin` to view.

## 15. Ingestion quality monitoring

**Feature:** a rollup of entity-resolution outcomes per ingestion run, with a
flag-and-acknowledge workflow for runs whose ambiguous-match rate spikes.

- **Expected behaviour:** each run records counts across the four
  entity-resolution tiers, corroboration-gate rejections, and extraction
  errors; a flagged run stays flagged until a supervisor acknowledges it, and
  acknowledgment propagates so the *next* run from the same source does not
  inherit the flag automatically.
- **Business rule:** acknowledging never re-evaluates the run's own data and
  never writes to the graph — it only records that a human looked at it.
- **Permissions:** `supervisor` or higher.

## 16. Community detection (Global Search support)

**Feature:** cluster the person graph into communities and summarize each one,
feeding the Global Search retrieval mode.

- **Expected behaviour:** an incremental check runs automatically after every
  ingest (only recomputing if a drift heuristic fires); a supervisor can also
  trigger a manual full sweep on demand, since the platform has no scheduler.
- **Permissions:** `supervisor` or higher.

## 17. External data sync (Muhafiz Data API)

**Feature:** pull FIR/roznamcha/CMS/PKM/criminal-record data from the external
evidence source and project it into the case graph.

- **Expected behaviour:** a full sync (`scripts/sync_muhafiz_data.py --full`)
  re-fetches and re-projects everything but is **idempotent** — running it
  twice must never duplicate a single graph edge, because prior edges from
  that record are purged and re-written on each run.
- **Business rules:** this source is entirely opt-in — leaving
  `MUHAFIZ_API_BASE_URL` empty disables it. It is not run automatically; there
  is no scheduler.
- **Permissions:** operator/CLI only — not exposed through the web UI.

## 18. User profile / preferences

**Feature:** a user sets free-text context and answer-generation preferences.

- **Expected behaviour:** `context_text` (used to personalize answers),
  `preferred_language`, and `llm_mode` are saved and read back on the settings page.
- **Validation:** `context_text` capped at 1000 characters server-side.
  `preferred_language`/`llm_mode` have **no server-side allow-list** — only the
  frontend's `<select>` options constrain the practical value set.
- **Permissions:** authenticated; a user may only read/write their own profile.

## 19. Project management

**Feature:** group conversations under a named project with optional domain context.

- **Expected behaviour:** standard CRUD, scoped strictly to the creating user.
- **Business rule:** `user_id` on create is always the authenticated caller —
  never accepted from the request body.
- **Permissions:** ownership-only; no role escalation exists for this resource.

## 20. Generated file download

**Feature:** download a PDF/XLSX/DOCX the pipeline produced (a report, a
session export).

- **Expected behaviour:** the correct MIME type and filename (repairing a
  missing extension on legacy rows) are returned.
- **Business rules (permission ladder, evaluated in order):** file owner →
  `platform-admin` → (no `case_id` on the file) `station-admin`/`platform-admin`
  → (has a `case_id`) `station-admin` with real case access → deny.
  Pre-migration files with no `case_id` deliberately keep the older blanket
  access, documented as an accepted limitation rather than a silent gap.

## 21. Web search (guarded)

**Feature:** an explicit user opt-in to search the live web for a question.

- **Expected behaviour:** results are restricted to a fixed allow-list of
  government/legal/major-news domains — not the open web — framed as a
  relevance/reliability control as much as a safety one.
- **Business rule:** this is the **only** outbound-internet-dependent route in
  the whole architecture; `AIR_GAP_MODE` disables it outright for air-gapped deployments.
- **Permissions:** any authenticated user; opt-in per message.

## 22. Two-tier RBAC (cross-cutting requirement)

**Feature:** every privileged action in the system is gated by one or both of:
(a) a **global** role (`investigator < supervisor < station-admin <
platform-admin`), or (b) a **per-case** assignment role.

- **Expected behaviour, generally:**
  - Ordinary CRUD on a resource the user owns (sessions, projects, attachments,
    own profile) needs no role beyond authentication.
  - Case-destructive actions (edit/delete a case) need per-case `supervisor`+.
  - Admin-dashboard surfaces need global `platform-admin` (with a
    `supervisor`+ carve-out for entity-resolution eval, graph review,
    community admin, and ingestion quality).
  - Cross-case retrieval (seeing evidence from more than one case in one
    answer) needs global `supervisor`+ — enforced **independently, three
    times**, once inside each of the three cross-case retrieval functions, so
    no single code path is a single point of failure for this rule.
- **Defense in depth:** every REST route also has its access re-checked at the
  database level via Postgres Row-Level Security once `DATABASE_URL` is
  repointed off the superuser role (see [AUTH_FLOW.md](AUTH_FLOW.md) §8).

---

## 23. Explicitly out-of-scope / not implemented

Recorded here because a QA plan should not test for features that do not exist:

- Multi-factor authentication.
- Password reset / forgot-password flow.
- Email verification.
- Token refresh / rotation endpoint.
- Server-side JWT revocation / a session blocklist.
- User self-service account deletion or password change.
- Any scheduled/cron background job — every recurring maintenance task is
  either triggered on ingest or by a manual admin/CLI action.
- A user-facing knowledge-base ingestion page (deliberately removed; replaced
  by the admin-only KB upload and the user-facing per-conversation attachment).
