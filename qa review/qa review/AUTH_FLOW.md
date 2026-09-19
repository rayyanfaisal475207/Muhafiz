# AUTH_FLOW.md

> Sources: [src/auth/jwt.py](../src/auth/jwt.py), [src/auth/routes.py](../src/auth/routes.py),
> [src/auth/rls_context.py](../src/auth/rls_context.py), [src/config.py](../src/config.py),
> [migrations/006_rbac.sql](../migrations/006_rbac.sql),
> [migrations/008_rls_policies.sql](../migrations/008_rls_policies.sql),
> [migrations/010_rls_null_case_fix.sql](../migrations/010_rls_null_case_fix.sql),
> [frontend/src/store/authStore.ts](../frontend/src/store/authStore.ts),
> [admin-frontend/src/AuthContext.tsx](../admin-frontend/src/AuthContext.tsx).

---

## 1. Signup (registration)

**Endpoint:** `POST /api/auth/register` — unauthenticated, rate-limited **5/minute**.

**Request:** `{ "email": EmailStr, "password": str, "company_name": str | null }`

**Server-side rules:**

| Rule | Enforcement | Failure |
|---|---|---|
| `email` is a syntactically valid address | Pydantic `EmailStr` | 422 |
| `email` is not already registered | `gateway.get_user_by_email()` lookup | 400 `"A user with this email already exists."` |
| `password` length ≥ **12** | `password_minimum_length` field validator | 422 |
| Password complexity classes | **Not enforced** — deliberate, documented as following current NIST guidance ("this platform has no MFA, so length is the one lever available") | — |
| Password hashing | `bcrypt.hashpw` with `bcrypt.gensalt()` | — |

**Assigned server-side, not client-controllable:** `role` (DB default
`investigator`), `plan` (DB default `free`), `id` (`gen_random_uuid()`),
`created_at`. `UserCreate` has no field for any of these.

**Response 200 (`UserResponse`):** `{ id, email, role, is_admin, company_name, plan }`.
`is_admin` is derived (`role == "platform-admin"`), not stored — migration 006
dropped the column.

**Registration does not create a session.** No cookies are set. The chat
frontend's `authStore.register()` chains an immediate `POST /api/auth/login`
followed by `GET /api/auth/me` to complete the sign-in.

### ⚠ QA finding — client/server password-rule mismatch
[frontend/src/pages/RegisterPage.tsx](../frontend/src/pages/RegisterPage.tsx) sets
`minLength={8}` on the password input, while the backend requires **12**.
A 8–11 character password passes browser validation and is rejected by the server
with a 422, which the store surfaces as a generic *"Registration failed"* message.

---

## 2. Login

**Endpoint:** `POST /api/auth/login` — unauthenticated, rate-limited **10/minute**.

**Request:** `{ "email": EmailStr, "password": str }`

**Flow:**
1. Look up the user by email.
2. `bcrypt.checkpw(plain, stored_hash)`.
3. On failure of **either** step → `401 "Incorrect email or password"`.
   The message is identical for both, so the endpoint does not leak whether an
   account exists.
4. On success, mint a JWT and a CSRF token and set two cookies.

**JWT:**
- Algorithm: `HS256` (`config.JWT_ALGORITHM`, hard-coded).
- Secret: `JWT_SECRET_KEY` env var (default `"your-secret-key-for-dev"`).
- Claims: `{ "sub": "<user uuid as string>", "exp": <utcnow + 7 days> }`.
  **No role, email, or any other claim is embedded** — the role is re-read from
  the database on every request.
- Lifetime: `ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7` = **7 days**.

**Cookies set:**

| Cookie | HttpOnly | Secure | SameSite | Max-Age |
|---|---|---|---|---|
| `access_token` | ✅ yes | `ENVIRONMENT != "development"` | `lax` | 604800 s (7 d) |
| `csrf_token` | ❌ no (JS must read it) | `ENVIRONMENT != "development"` | `lax` | 604800 s (7 d) |

`csrf_token` = `secrets.token_urlsafe(32)`.

**Response 200:** `{ "message": "Login successful" }` — the body carries no user
data; clients follow up with `GET /api/auth/me`.

---

## 3. Logout

**Endpoint:** `POST /api/auth/logout`

- **Requires authentication** (`Depends(get_current_user)`), and because it is a
  `POST`, it **also requires a valid CSRF header**. A client that has lost its
  CSRF cookie cannot log out through this endpoint.
- Deletes both cookies with matching `secure`/`samesite`/`httponly` flags.
- **Response 200:** `{ "message": "Logged out successfully" }`.

**The JWT is not revoked.** There is no denylist, blocklist, or session table
consulted at verification time. A token captured before logout remains valid for
the remainder of its 7-day lifetime. This is a property of the implementation,
not a documented decision found in the code.

**Client-side logout side effects:**
- Chat app: `authStore.logout()` clears the auth store *and* resets the chat,
  case, project and session stores, and removes `LAST_SESSION_KEY` from
  localStorage — explicitly so a shared workstation does not carry one user's
  content into the next login.
- Admin app: `AuthContext.logout()` fires the backend logout (manually attaching
  `X-CSRF-Token` read from `document.cookie`) and clears
  `muhafiz_admin_auth` / `muhafiz_admin_role` / `muhafiz_admin_email`.

---

## 4. Email verification

**Not implemented.** There is no verification token column, no verification
endpoint, no mail transport, and no `is_verified`-style flag on the `users`
model. Accounts are usable immediately after registration.

---

## 5. Password reset

**Not implemented.** There is no forgot-password endpoint, no reset-token table,
no mailer, and no password-change endpoint. A password can only be changed by
direct database manipulation.

`scripts/create_admin.py` exists to create a platform-admin account with a
default password ([RUN.md](../RUN.md) §4 documents this and instructs changing the
password before any real deployment).

---

## 6. Sessions / JWT verification (per request)

`get_current_user` is the only authentication dependency. Order of operations:

```
1. If method in {POST, PUT, DELETE, PATCH}:
       cookie_csrf  = request.cookies["csrf_token"]
       header_csrf  = request.headers["x-csrf-token"]
       if either missing or unequal  ->  403 "CSRF token validation failed."
2. token = cookies["access_token"]
       or, if absent, the Bearer token from the Authorization header
   if no token  ->  401 "Not authenticated"
3. payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[HS256])
       JWTError                ->  401 "Invalid token"
       missing "sub"           ->  401 "Invalid token"
4. user = gateway.get_user_by_id(UUID(sub))
       user is None            ->  401 "User not found"
5. return AuthUser(**user)     # a dynamic attribute-bag, not the ORM User
```

Notable properties for QA:

- **CSRF is checked before authentication.** An unauthenticated `POST` without
  CSRF headers returns **403**, not 401. Test expectations must account for this.
- **The `Authorization: Bearer` fallback bypasses nothing.** CSRF is still
  enforced on mutating methods regardless of which transport carried the token,
  so a pure-Bearer API client must still send both a `csrf_token` cookie and a
  matching header to perform any write.
- **Expiry** is enforced by `jose` via the `exp` claim; an expired token raises
  `JWTError` → 401 `"Invalid token"` (the message does not distinguish expiry
  from tampering).
- **The role is fetched fresh from the database on every request**, so a role
  change takes effect immediately without re-issuing a token.
- `UUID(user_id_str)` is called **outside** the `except JWTError` block; a `sub`
  claim that is a valid string but not a UUID raises `ValueError`, which is not
  caught here and surfaces as a **500**, not a 401. **Flagged for QA.**

---

## 7. RBAC

### 7.1 Global roles

Postgres enum `user_role`, created in
[migrations/006_rbac.sql](../migrations/006_rbac.sql):

```
'investigator' < 'supervisor' < 'station-admin' < 'platform-admin'
```

`require_role(minimum_role)` compares list indices; a user at or above the
threshold passes.

The same four values are mirrored in
[src/pipeline/harness/types.py](../src/pipeline/harness/types.py) as the `Role` enum, with
`CROSS_CASE_ROLES = {SUPERVISOR, STATION_ADMIN, PLATFORM_ADMIN}`.

### 7.2 Per-case ABAC (`case_assignments`)

```sql
case_assignments(assignment_id UUID PK, case_id TEXT FK, user_id UUID FK,
                 role user_role NOT NULL DEFAULT 'investigator', created_at,
                 UNIQUE (case_id, user_id))
```

`check_case_access(case_id, user_id, user_role, min_role=None)`:

1. `user_role == "platform-admin"` → `True` (no assignment needed).
2. No `case_assignments` row for `(case_id, user_id)` → `False`.
3. `min_role is None` → `True` (any assignment suffices).
4. Otherwise compare **`assignment.role`** (the per-case role) against
   `min_role`. A global supervisor assigned to a case as `investigator` does
   **not** get supervisor-level destructive rights on that case.

### 7.3 Where each gate is applied

| Surface | Gate |
|---|---|
| `POST /api/chat` (with a `case_id`) | `check_case_access(...)` → 403 before the pipeline runs |
| `GET /api/cases/{id}` | `require_case_access()` — any assignment |
| `PUT`/`DELETE /api/cases/{id}` | `require_case_access(min_role="supervisor")` |
| `GET /api/cases/` | No per-case gate; `get_cases()` filters by assignment (or returns all for `platform-admin`) |
| `/api/cases/{id}/assignments/*` | `require_role("station-admin")` + `_require_station_match()` |
| `/api/admin/*` | `require_role("platform-admin")`, except `/eval/entity-resolution` → `supervisor` |
| `/api/admin/graph-review/*`, `/community/*`, `/ingestion-quality/*` | `require_role("supervisor")` |
| `/api/sessions/*`, `/api/projects/*`, `/api/attachments/*` | **Ownership only** (`row.user_id == current_user.id`), no role check |
| `GET /api/files/{id}/download` | Owner → platform-admin → case-scoped station-admin ladder (see [BACKEND_API.md](BACKEND_API.md) §4) |
| Cross-case graph traversal (XGRAPH / XAGG / XNETWORK) | `_enforce_cross_case_role_gate()` in [src/retrieval/graph_retriever.py](../src/retrieval/graph_retriever.py): `CROSS_CASE_ROLES = ("supervisor","station-admin","platform-admin")`; raises `PermissionError` and writes an `authorization_violation` audit record for anyone else |

### 7.4 Role provenance rule

[src/pipeline/harness/compliance/test_enforcement_4_role_provenance.py](../src/pipeline/harness/compliance/test_enforcement_4_role_provenance.py)
enforces, at CI time, that `user_role` always originates from
`current_user.role` and is **never** read out of `user_profile` (which contains
only `context_text`, `preferred_language`, `llm_mode`). This guards a documented
historical bug in which `user_profile.get("role", "investigator")` silently
downgraded every caller to `investigator`, denying real supervisors cross-case
access.

---

## 8. Row-Level Security (database-level backstop)

RLS is a **second** layer beneath the application checks, not a replacement.

### 8.1 Arming

`src/auth/rls_context.py` sets three `ContextVar`s that
`src/database/postgres.py::get_session()` translates into `SET LOCAL` /
`set_config(..., is_local=true)` statements on the connection:

| Function | `app.rls_active` | `app.case_id` | `app.cross_case` | Used by |
|---|---|---|---|---|
| `set_case_scope(case_id)` | `'true'` | `case_id` or `''` | `false` | `chat_endpoint`, `cases.py`, `case_assignments` router |
| `set_cross_case_scope()` | `'true'` | `''` | `true` | `admin`, `graph_review`, `community_admin`, `ingestion_quality_admin`, `sessions`, `attachments`, `projects`, `GET /api/cases/` |

`app.case_id` is **always** set to a real value — the empty string means "general,
no case" — never left unset. That is the fix for the NULL-vs-NULL defect
migration 010 documents.

### 8.2 Policies

`ENABLE` + `FORCE ROW LEVEL SECURITY` (so the table owner is also subject to
them) on: `documents`, `sessions`, `pipeline_runs`, `cases` (migration 008) and
`messages` (migration 010).

Policy shape after migration 010, e.g. for `cases`:

```sql
USING (
    current_setting('app.rls_active', true) IS DISTINCT FROM 'true'
    OR current_setting('app.cross_case', true) = 'true'
    OR (case_id IS NULL AND current_setting('app.case_id', true) = '')
    OR case_id = current_setting('app.case_id', true)
)
```

`documents` additionally passes on `is_global = true`. `pipeline_runs` and
`messages` join through `sessions` on `session_id`. All are `FOR ALL` policies
with no separate `WITH CHECK`, so `USING` also governs `INSERT` — which is why
`create_case` must arm RLS to the new `case_id` before inserting.

**Fail-open when unarmed:** if `app.rls_active` is not `'true'`, every policy
passes. RLS is therefore only protective on code paths that actually arm it.

### 8.3 ⚠ Deployment prerequisite that neutralises RLS

[RUN.md](../RUN.md) §4 states plainly: as long as `DATABASE_URL` points at the
`postgres` superuser, **every RLS policy is silently inert**, because a
superuser (or any `BYPASSRLS` role) unconditionally bypasses row-level security.
Migration 015 creates a least-privilege `muhafiz_app` role, but it ships with
`LOGIN` and no password and does **not** repoint `DATABASE_URL` — that is a
manual per-deployment step. Verify with `scripts/verify_app_role.py`.

---

## 9. Protected routes (frontend)

### 9.1 Chat app — [frontend/src/App.tsx](../frontend/src/App.tsx)

| Route | Guard |
|---|---|
| `/login`, `/register` | Public. Redirect to `/` when already authenticated (via a `useEffect` on `isAuthenticated`) |
| `/`, `/chat/:id`, `/settings` | Wrapped in `<ProtectedRoute>` → `<AppLayout>` |
| `*` (inside the protected tree) | `<Navigate to="/" replace />` |

`ProtectedRoute` renders a *"Loading session…"* spinner while
`authStore.isLoading` is true, then redirects to `/login` if
`!isAuthenticated`, otherwise renders `<Outlet />`. `isLoading` starts as
`true` and `checkAuth()` (`GET /api/auth/me`) runs on mount, so an authenticated
refresh does not flash the login screen.

A global `auth:unauthorized` window event, dispatched by the axios response
interceptor on any 401 (and by `streamChat` on a 401 from `/api/chat`), calls
`setUnauthenticated()` and navigates to `/login` unless already on
`/login`/`/register`.

### 9.2 Admin app — [admin-frontend/src/App.tsx](../admin-frontend/src/App.tsx)

Two layers:

1. `ProtectedLayout` — redirects to `/login` when `!isAuthenticated`.
2. `RequireRole min={[...]}` per route — redirects to the role's own home
   (`ROLE_HOME`) when the role is not in the allowed list:
   `platform-admin → "/"`, `station-admin → "/cases"`, `supervisor → "/review-queue"`.

| Route | Minimum roles |
|---|---|
| `/` (Dashboard), `/knowledge-base`, `/errors`, `/runs`, `/files`, `/mcp`, `/users`, `/audit-logs` | `platform-admin` |
| `/review-queue`, `/ingestion-quality`, `/eval/entity-resolution`, `/cases` | `supervisor`, `station-admin`, `platform-admin` |
| `/profile` | Any authenticated admin-app user |
| `*` | `<Navigate to="/" replace />` |

**Admin auth state is stored in `localStorage`**, not derived from a server call
on every load: `muhafiz_admin_auth`, `muhafiz_admin_role`, `muhafiz_admin_email`.
`AuthProvider` includes a defensive migration: a stale `muhafiz_admin_auth=true`
with no valid stored role is cleared on init, because that combination previously
produced an infinite `/login` ↔ `/` redirect loop.

**Login eligibility:** `AuthContext.login()` performs `POST /api/auth/login`,
then `GET /api/auth/me`, and if the returned role is not in
`ADMIN_ROLES = ['supervisor','station-admin','platform-admin']` it calls
`POST /api/auth/logout` and returns `false` — an investigator cannot enter the
admin shell.

> **QA note:** because admin route guarding reads `localStorage`, a user who
> edits `muhafiz_admin_role` in devtools can render higher-tier admin pages. Every
> such page's data still comes from a `require_role`-gated endpoint, so the
> content fails with 403 — the client-side guard is a UX affordance, not a
> security boundary.

### 9.3 CSRF on the client

Both apps read the non-HttpOnly `csrf_token` cookie with
`document.cookie.match(/(^| )csrf_token=([^;]+)/)` and attach it as
`X-CSRF-Token` on `POST`/`PUT`/`DELETE`/`PATCH`:
[frontend/src/lib/api.ts](../frontend/src/lib/api.ts) (axios request interceptor and
`streamChat`), [admin-frontend/src/api.ts](../admin-frontend/src/api.ts),
[admin-frontend/src/casesApi.ts](../admin-frontend/src/casesApi.ts), and
`AuthContext.logout()`.

---

## 10. Configuration-level auth guards

`validate_config()` ([src/config.py](../src/config.py)) classifies two auth-relevant
problems as **critical** (as opposed to warnings):

1. `ENVIRONMENT` not in `{development, staging, production}` — cookie `Secure`
   flags and the JWT-secret check both key off it.
2. `JWT_SECRET_KEY` still equal to the public default
   `"your-secret-key-for-dev"` while `ENVIRONMENT != "development"` — the
   message states that anyone could forge a valid JWT for any user, including
   platform-admin.

[src/main.py](../src/main.py)'s lifespan **refuses to start** on any critical error when
`ENVIRONMENT == "production"`; in other environments it logs a `CRITICAL` and
starts anyway.
