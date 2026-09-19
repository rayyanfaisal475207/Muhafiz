# FRONTEND_FEATURES.md

> Two independent SPAs. Sources: [frontend/src/](../frontend/src/) (chat) and
> [admin-frontend/src/](../admin-frontend/src/) (admin).

---

# PART A — Chat app (`frontend/`, dev port 5173)

## A1. Pages and routes

Defined in [frontend/src/App.tsx](../frontend/src/App.tsx).

| Route | Component | Protected | Notes |
|---|---|---|---|
| `/login` | `LoginPage` | ❌ public | Redirects to `/` via `useEffect` when `isAuthenticated` |
| `/register` | `RegisterPage` | ❌ public | Same redirect behaviour |
| `/` | `ChatPage` | ✅ | Restores the last session from `localStorage[LAST_SESSION_KEY]`, or starts a new one |
| `/chat/:id` | `ChatPage` | ✅ | Loads that session; persists the id to `localStorage` |
| `/settings` | `SettingsPage` | ✅ | Profile & preferences |
| `*` (inside the protected tree) | — | ✅ | `<Navigate to="/" replace />` |

Protected routes are nested `<Route element={<ProtectedRoute />}>` →
`<Route element={<AppLayout />}>`. There is **no `/ingest` route** — a code
comment records that knowledge-base ingestion moved to the admin app.

## A2. Protected-route mechanics

[frontend/src/components/auth/ProtectedRoute.tsx](../frontend/src/components/auth/ProtectedRoute.tsx):

1. `isLoading` → renders a centred spinner with *"Loading session..."*.
2. `!isAuthenticated` → `<Navigate to="/login" replace />`.
3. Otherwise → `<Outlet />`.

`authStore.isLoading` starts `true` and `checkAuth()` (`GET /api/auth/me`) runs
on app mount, so a refresh while logged in does **not** flash the login screen.

**Global 401 handling.** The axios response interceptor and `streamChat` both
dispatch a `window` event `auth:unauthorized` on a 401. `AppRoutes` listens for
it, calls `setUnauthenticated()`, and navigates to `/login` **unless** the
current path is already `/login` or `/register`.

## A3. Forms

### Login — [LoginPage.tsx](../frontend/src/pages/LoginPage.tsx)
| Field | Type | Client validation | Extras |
|---|---|---|---|
| `email` | `type="email"` | `required` | `autoComplete="email"` |
| `password` | `type="password"` | `required` | `autoComplete="current-password"`, Show/Hide toggle |

- Submit button label toggles `Sign in` ⇄ `Signing in…`, disabled while `isLoading`.
- Errors render in a `role="alert"` banner above the form.
- `clearError()` runs on mount.
- Link to `/register`.

### Register — [RegisterPage.tsx](../frontend/src/pages/RegisterPage.tsx)
| Field | Type | Client validation |
|---|---|---|
| `email` | `type="email"` | `required` |
| `password` | `type="password"` | `required`, **`minLength={8}`** |
| `company_name` | `text` | optional (labelled "Company Name (Optional)") |

- **⚠ Mismatch:** the backend requires **12** characters
  ([src/auth/routes.py](../src/auth/routes.py)). An 8–11 char password passes browser
  validation, fails server-side with 422, and surfaces as the generic
  *"Registration failed"* string. **QA should test this boundary.**
- On success `authStore.register()` chains login + `/auth/me` (auto-login).
- Button label toggles `Register` ⇄ `Registering...`.

### Settings / Profile — [SettingsPage.tsx](../frontend/src/pages/SettingsPage.tsx)
| Field | Control | Notes |
|---|---|---|
| `context_text` | `<textarea rows={4}>` | Free text; server caps at 1000 chars |
| `preferred_language` | `<select>` | Default `'auto'` |
| `llm_mode` | `<select>` | Default `'cloud'` |

- Redirects to `/login` if `user` is falsy on mount, then calls `loadProfile()`.
- On save: `PUT /api/profile`, then a success indicator shown for **3 seconds**
  (`setTimeout`), cleared on any subsequent field change.
- Displays *"Logged in as: **{email}**"*.

### Chat composer — [ChatInput.tsx](../frontend/src/components/chat/ChatInput.tsx)
- **The textarea is never disabled** (explicit code comment) — the user can
  compose the next question while a response streams.
- Send is enabled only when `!disabled && text.trim().length > 0` (`canSend`).
- `disabled` is bound to `isStreaming`; the send button then shows a stop/wait
  affordance with tooltip *"Waiting for the current response"*.
- Placeholder: *"Ask about any section, procedure, or SOP…"*.
- **Web-search toggle:** an `aria-pressed` button setting `webSearchEnabled`.
  Tooltip switches between *"Search the web for this question"* and *"Web search
  on for your next message (click to turn off)"*. `sendMessage()` resets it to
  `false` after each send — it is strictly a **per-query opt-in**.
- File attachment via `react-dropzone`.

### Case creation modal — [CaseSettingsModal.tsx](../frontend/src/components/layout/CaseSettingsModal.tsx)
| Field | Required |
|---|---|
| `fir_number` | no |
| `crime_category` | no |
| `investigation_officer` | no |
| `police_station` | no |

- `investigation_status` is hard-coded to `'open'` on create.
- Empty strings are converted to `undefined` so they are omitted from the payload.
- Local `isLoading` / `error` state; error text is
  `err.response?.data?.detail || 'Failed to save case'`.
- Accessibility: `useModalA11y` hook (focus trap + Escape to close),
  `aria-labelledby="case-modal-title"`, backdrop click closes and resets.

### Project creation modal — [ProjectSettingsModal.tsx](../frontend/src/components/layout/ProjectSettingsModal.tsx)
Fields: `name`, `description`, `domain_context`. Same modal a11y pattern.

## A4. User actions and the endpoint each calls

| Action | Where | Endpoint |
|---|---|---|
| Register | RegisterPage | `POST /api/auth/register` → `POST /api/auth/login` → `GET /api/auth/me` |
| Log in | LoginPage | `POST /api/auth/login` → `GET /api/auth/me` |
| Log out | Sidebar footer | `POST /api/auth/logout` + resets 4 zustand stores + clears `LAST_SESSION_KEY` |
| Send a message | ChatInput | `POST /api/chat` (SSE) |
| Toggle web search | ChatInput | Sets `enable_web_search: true` on the **next** message only |
| Attach a file | ChatInput / AttachmentChips | `POST /api/attachments` |
| Remove an attachment | AttachmentChips | `DELETE /api/attachments/{id}` |
| New chat | Sidebar | Local `newSession()`; navigates with `state: { fresh: true }` |
| Open a session | Sidebar | `GET /api/sessions/{id}` + `GET /api/attachments?session_id=` |
| Rename a session | Sidebar (inline edit, commits `onBlur`) | `PATCH /api/sessions/{id}` |
| Delete a session | Sidebar (two-step confirm) | `DELETE /api/sessions/{id}` |
| Download/export a session | Sidebar | `GET /api/sessions/{id}/export` |
| List / create / update / delete projects | Sidebar + modal | `/api/projects/*` |
| List / create / update / delete cases | Sidebar + modal | `/api/cases/*` |
| Load / save profile | SettingsPage | `GET`/`PUT /api/profile` |
| Click a citation | MessageBubble → CitationPanel | Local state only (no fetch) |
| Toggle theme | ThemeToggle | `themeStore`, local only |

## A5. UI states

### Streaming / generation — [GenerationStatus.tsx](../frontend/src/components/chat/GenerationStatus.tsx)
Three explicit phases, per the module's own header comment:

1. **WORKING** — `isStreaming && !hasContent`: an animated live status line
   showing the most recent `active` phase (or the most recent `error` phase if
   one exists).
2. **STREAMING** — the moment answer text starts arriving, the live status line
   gives way to the text.
3. **DONE** — a collapsed summary of the phases.

Per-phase status values: `active`, `done`, `skipped`, `error`.
Phases with status `skipped` are filtered out of the visible list.
Events with `status === 'streaming'` are skipped when building phases.

### Pipeline panel — [PipelinePanel.tsx](../frontend/src/components/pipeline/PipelinePanel.tsx)
Renders the canonical seven `PIPELINE_STEPS` (`query_rewriter`, `router`,
`retrieval`, `reranker`, `evaluator`, `response`, `memory`), each as a
`PipelineStepCard` with status ∈
`waiting | active | done | skipped | error | retry`.
`retry` is derived client-side: an `evaluator` event with `status === 'done'`
and `retry_num > 0`. Cards can also display `ms`, `detail`, `hopCount` and
`graphConfidence`.

### Error states
- **Chat error banner** — `ChatPanel` renders a dismissible banner
  (`Error: {message}`) driven by `chatStore.error`. The code comment notes this
  was added because the store recorded errors that nothing displayed.
- **Auth error banner** — Login (`role="alert"`) and Register pages.
- **Network vs. HTTP** — `authStore` distinguishes them:
  no `err.response` → *"Network error. The server may be offline."*;
  otherwise `err.response.data.detail` or a generic fallback.
- **Stream stall** — `StreamStallError` after **90 s** with no chunk:
  *"Connection seems stalled — no response received in a while."* The reader is
  cancelled explicitly rather than left half-open.
- **Malformed SSE frame** — logged via `console.warn` and dropped; the stream continues.
- **Attachment failure** — a pending chip shows a `failed` state with the
  server's `error_message`; `dismissPending(tempId)` removes it.
- **Attachment delete failure** — the optimistic removal is **rolled back**
  ("put it back if the delete failed").
- **Sidebar action errors** — a dedicated `actionError` state with a dismiss button.

### Loading states
- `ProtectedRoute` spinner: *"Loading session..."*.
- Auth buttons: `Signing in…` / `Registering...` while `isLoading`.
- Modals: local `isLoading` disables submit.
- Session/case/project list fetches pass an `AbortSignal` so a rapid navigation
  cancels the in-flight request.

### Empty / initial states
- No session in the URL and no `LAST_SESSION_KEY` → `newSession()` (empty conversation).
- Navigating with `state: { fresh: true }` explicitly **suppresses** last-session
  restore — a comment records that the bounce-back was why "New Chat" appeared to
  do nothing.
- `activeSource` is cleared on every navigation into `ChatPage`, so cross-case
  evidence never lingers on screen after the conversation changes.

### Degradation disclosure
`ChatMessage.degradationTrace` is `undefined` for legacy-orchestrator and
pre-harness messages — deliberately distinct from a trace showing a clean run,
and renders nothing. When present, the UI must render `trace.labels.*`
**verbatim**; the raw identifiers in `contributed_only` /
`degraded_and_contributed` / `degraded_only` are for keys and logic only. The
canonical label map lives in the backend to avoid client-side drift.

---

# PART B — Admin app (`admin-frontend/`, dev port 5174)

## B1. Pages, routes and required roles

From [admin-frontend/src/App.tsx](../admin-frontend/src/App.tsx) and
[components/Sidebar.tsx](../admin-frontend/src/components/Sidebar.tsx).

| Route | Page | Minimum role | Sidebar group |
|---|---|---|---|
| `/login` | `LoginPage` | public | — |
| `/` | `DashboardPage` | `platform-admin` | Overview |
| `/knowledge-base` | `KnowledgeBasePage` | `platform-admin` | Knowledge |
| `/cases` | `CaseManagementPage` | supervisor+ | Knowledge |
| `/review-queue` | `ReviewQueuePage` | supervisor+ | Knowledge |
| `/ingestion-quality` | `IngestionQualityPage` | supervisor+ | Knowledge |
| `/eval/entity-resolution` | `EntityEvalPage` | supervisor+ | Knowledge |
| `/audit-logs` | `AuditLogPage` | `platform-admin` | Monitoring |
| `/errors` | `ErrorsPage` | `platform-admin` | Monitoring |
| `/runs` | `RunHistoryPage` | `platform-admin` | Monitoring |
| `/files` | `GeneratedFilesPage` | `platform-admin` | Monitoring |
| `/mcp` | `McpCallLogPage` | `platform-admin` | Monitoring |
| `/users` | `UsersPage` | `platform-admin` | Monitoring |
| `/profile` | `ProfilePage` | any admin-app user | — |
| `*` | — | — | `<Navigate to="/" replace />` |

Sidebar `minRole` mirrors each page's backend `require_role(...)`, so an item is
**hidden entirely** for a role that would be redirected or 403'd.

## B2. Route protection

Two layers:
1. `ProtectedLayout` — `!isAuthenticated` → `/login`.
2. `RequireRole min={[...]}` — a role not in the list is redirected to its own
   `ROLE_HOME`: `platform-admin → "/"`, `station-admin → "/cases"`,
   `supervisor → "/review-queue"`. Notably `/` (Dashboard) is platform-admin-only,
   so a supervisor cannot simply land there by default.
3. `LoginRouteGuard` — an already-authenticated visit to `/login` redirects to `/`.

**State source:** `localStorage` keys `muhafiz_admin_auth`, `muhafiz_admin_role`,
`muhafiz_admin_email`. `AuthProvider` clears a stale `muhafiz_admin_auth=true`
with no valid role on init — that combination previously caused an infinite
`/login` ⇄ `/` redirect loop ("Maximum update depth exceeded").

> **QA note:** because the guard reads `localStorage`, editing
> `muhafiz_admin_role` in devtools renders a higher-tier page — but every page's
> data comes from a `require_role`-gated endpoint, so the content 403s. The
> client guard is UX, not a security boundary.

## B3. Admin login form

[LoginPage.tsx](../admin-frontend/src/pages/LoginPage.tsx) → `AuthContext.login(user, pass)`:

1. `POST /api/auth/login` (`credentials: 'include'`). Non-OK → `false`.
2. `GET /api/auth/me`. Non-OK → `false`.
3. If `me.role` is **not** in `['supervisor','station-admin','platform-admin']`
   → `POST /api/auth/logout` to drop the session just created, then `false`.
   (Rationale in the code: otherwise a regular user would see the whole shell
   with every request failing 403.)
4. Otherwise persist role/email to `localStorage` and set authenticated.

## B4. Per-page actions

| Page | Reads | Writes |
|---|---|---|
| Dashboard | `/usage`, `/latency`, `/kb/stats`, `/errors/trend`, `/verifier/stats`, `/instrumentation` (six parallel `Promise.all` calls) | — |
| Knowledge Base | `/kb/stats`, `/kb/jobs`, `/instrumentation` | `POST /kb/upload` (multipart), `DELETE /kb/documents/{filename}` (URL-encoded) |
| Case Management | `GET /cases/`, `GET /cases/{id}/assignments/` | `POST /cases/{id}/assignments/`, `DELETE /cases/{id}/assignments/{userId}` |
| Review Queue | `/graph-review/pending`, `/graph-review/stats` | `POST /graph-review/{edgeId}/confirm\|reject` |
| Ingestion Quality | `/ingestion-quality/runs` | `POST /ingestion-quality/{runId}/acknowledge` |
| Entity Eval | `/eval/entity-resolution` | — |
| Audit Logs | `/audit-logs` | — |
| Errors | `/errors`, `/errors/trend`, `/instrumentation` | — |
| Run History | `/runs`, `/runs/{run_id}/steps` (expand-on-click) | — |
| Generated Files | `/files?limit=100` | `DELETE /files/{fileId}` |
| MCP Call Log | `/mcp-calls?limit=100` | — |
| Users | `/users?limit=100` | — |
| Profile | — | **UNKNOWN** — no API call found in this page |

## B5. Admin UI states

- **Instrumentation banner.** Dashboard, Errors and Knowledge Base all fetch
  `/instrumentation` and use it to show an honest *"run migration 003"* banner
  instead of rendering empty charts that look like a healthy, silent system.
  The endpoint probes real table existence rather than inferring from an empty
  result set.
- **Case assignment form.** Assign-by-**email** (the admin user list is
  platform-admin-only, so a station-admin has no other way to find a `user_id`)
  plus a role value.
- **Review Queue.** Confirm/reject per pending match, showing tier, confidence and
  the human-readable `basis` — the module docstring frames this as what makes a
  match "verifiable rather than a rubber stamp".
- **Ingestion Quality.** A "needs attention" queue reading `flagged_for_review`
  runs, with a single acknowledge action.
- **Entity Eval.** Serves a static JSON file; when it is missing the endpoint
  returns HTTP 200 with `{"generated_at": null, "error": "..."}`, so the page
  must render that as an informational state, **not** as a request failure.
- **Loading / error.** Each page holds its own local `loading` / `error` state
  around its fetches; there is no shared query cache.
