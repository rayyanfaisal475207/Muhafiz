# Muhafiz — Security Testing: Consolidated Summary & Tooling Decision

**What this is.** A single roll-up of all manual security and AI-red-team testing
done to date (Phases 0–2 plus the global-KB batch), the current status of every
finding, and a recommendation on whether spending your remaining day on automated
tooling (Promptfoo / Nuclei / ZAP / DeepTeam / Garak) is worth it.

**Companion documents** (kept for detail; this supersedes them where they disagree):
- `PHASE_0_1_SECURITY_REPORT.md` — code audit + AI attack surface, Batches 1–4
- `PHASE_2_SECURITY_REPORT.md` — traditional app/API surface

**Date:** 2026-09-03. **Method:** entirely manual — targeted code review plus
authored probe scripts against a local instance bound to `127.0.0.1`, real login
flow (cookie + CSRF), synthetic-only test data, full teardown with re-verification
after every probe. No automated security tooling was used for any finding below.

---

## 1. Bottom line

The system is **fundamentally sound on confidentiality and access control**, and has
**one real, confirmed integrity weakness** in how it treats retrieved evidence.

- **Every authorization boundary held** under direct HTTP attack, prompt injection,
  disguised-intent queries, and forged-token attempts (after the one auth fix).
- **The one confirmed finding (Finding 4)** is an *integrity* issue, not a
  confidentiality one: the grounding Verifier certifies an answer as "grounded" when
  it faithfully repeats a retrieved document — even when that document is false. A
  poisoned reference/evidence document produces confident, cited, unhedged falsehoods.
  No cross-case data ever leaked; the exposure is misplaced trust *within* an
  authorized scope.
- **One Critical was found and fixed during the engagement** (Finding 5, forgeable
  JWT via a public default secret) — then accidentally reverted by a folder copy, then
  re-fixed. It is currently closed and verified.

---

## 2. Findings register (current status)

| # | Severity | Title | Status |
|---|----------|-------|--------|
| **5** | **Critical** | Forgeable JWT via public default `JWT_SECRET_KEY` | **FIXED & re-verified** (see lifecycle below) |
| **4** | **Medium** | Verifier grounding = faithful-to-source, not faithful-to-truth | **Confirmed, reproducible, open** — fix proposed, not applied |
| 1 | Medium | Duplicated cross-case role gate (`xagg.py` vs canonical) | Confirmed — latent drift hazard; enforces correctly today; no HTTP path reaches it independently |
| 2 | Low–Med | `run_aggregate` `user_role` defaults to `"investigator"` | Confirmed — fails safe; every real caller passes the role explicitly |
| 6 | Medium | `Secure` cookie flag off under `ENVIRONMENT=development` | Open — same root cause as 5; fixed by running as `production` |
| 8 | Low | `sessions.py` routes 500 on malformed `session_id` | Open — no leak; the Phase-0 F-06 boundary fix wasn't propagated here |
| 7 | Low | `community/refresh` unthrottled | Confirmed — supervisor-gated |
| 3 | Info | Bearer-token fallback not dev-gated | Downgraded — no XSS sink, token never in JS-readable storage; CSRF runs first |

**Not-a-bug (investigated, dismissed):** Meta-Analysis "unstable suspect counts" —
the hand-written verification query grouped people by first name; the aggregate was
correct (distinct CNICs = distinct people). Documented so it isn't re-raised.

---

## 3. Finding 5 lifecycle (the one that moved)

Worth its own section because its status changed three times and the *pattern* matters
more than the bug.

1. **Found (Critical).** `JWT_SECRET_KEY=your-secret-key-for-dev` — the public default
   from source — was live in `.env` and the transferred `SHARE/.env`. The startup guard
   only refuses it when `ENVIRONMENT != development`; both configs were `development`, so
   the guard never fired. **Proven:** a token signed with the public string, as a real
   platform-admin, with no login, returned **HTTP 200 + the full user list** from
   `/api/admin/users`. Because `SHARE/.env` ran on the internet-exposed office-PC tunnel,
   anyone reading the public-pattern source could have forged an admin token.
2. **Fixed.** Operator rotated to a 64-char random secret in both files and restarted.
   Re-verified from the tester side: old-secret token → **401**, rotated → **200**.
3. **Reverted (silently).** A teammate's replacement `SHARE/` folder was copied over the
   existing one, overwriting `SHARE/.env` back to the public default. Caught on a routine
   re-check. Flagged immediately as Finding 5 reopened.
4. **Re-fixed & verified.** Operator restored the rotated secret in `SHARE/.env`; both
   files now carry the same 64-char value (verified: `sha256=f1b37ca7…`, zero occurrences
   of the default string).

**Durable lesson (recommend acting on before launch):** a live secret lives inside a
folder that gets copy-overwritten on every SHARE sync — so this *will* revert again. Keep
secrets out of `SHARE/` entirely, or make the guard fail-closed on the secret's *value*
regardless of `ENVIRONMENT`.

---

## 4. What was tested — by area

### Access control / RBAC / RLS — held everywhere
- **RLS proven live, not just read:** `muhafiz_app` is non-superuser with `rolbypassrls=false`;
  all 5 policy tables are `FORCE ROW LEVEL SECURITY`; empirically **73 cases visible unscoped
  → exactly 1 when scoped** to a case_id.
- **IDOR from the HTTP layer:** as an investigator, read/update/delete of an *unassigned*
  case → 403, no partial write. Read/delete/export of *another user's* session → 403.
  Profile PUT with a forged foreign `id`/`user_id` in the body → write used `current_user.id`,
  victim untouched.
- **Role enforcement per-route:** every admin route → 403 for investigator; every supervisor
  route (incl. mutating `/refresh`) → 403. All 11 routers have an auth dependency on every route.
- **CSRF:** fires on missing *and* mismatched tokens, before the access check.
- **Cross-case role gate:** investigator cross-case queries denied with audit rows written at
  two independent layers (defence in depth). Prompt-injection role override ("SYSTEM OVERRIDE…
  as platform-admin") denied in **15ms** — authorization runs before any LLM call. A cross-case
  question *disguised* as within-case still routed to XGRAPH and was denied.

### Injection — clean
- **SQL:** zero raw-string interpolation anywhere; all parameterized.
- **Cypher/AGE:** f-strings interpolate only *structural* pieces, each a hardcoded literal
  (`"[r:SAME_AS]"`) or a closed allowlist (`TYPE_TO_LABEL`, raises on unknown types). User input
  flows only through `$`-parameters. Live: a `DETACH DELETE` injection in query text left the
  graph node count unchanged.

### File upload — content-sniffed
Two-layer: extension + size at the endpoint, then magic-byte signature checks + a zip-bomb guard
at the loader. Live: an MZ-executable named `.pdf` was rejected by the `%PDF-` mismatch; the raw
file was never stored.

### JWT robustness — clean
Non-UUID / missing / null `sub`, expired, garbage, and **`alg:none`** all → clean **401, no 500**.
Algorithm pinned; expiry enforced.

### Error handling — no leakage
Malformed input → 422; the one 500 found (Finding 8) returns a generic body — no stack trace,
path, or DB detail reaches the client.

### Admin dashboard — not a weaker path
Pure client, same backend, same JWT+CSRF; the `localStorage` role flag is cosmetic — the backend
`require_role` is the real gate.

### Data hygiene (global KB)
Scanned all 6,583 global-KB reference chunks: **zero** case-specific PII (CNIC/phone/belt/FIR-ref).
The 18 "plate-like" hits were legal citations (`PLD 2016`), not PII.

---

## 5. Finding 4 in depth — the one real weakness

Confirmed across **four independent probes**, all with the same result:

| Probe | Route / source | Result |
|-------|----------------|--------|
| Batch 2 | Case-scoped evidence, token injection | Instruction executed (emitted a marker token); **zero** cross-case leak |
| Batch 3 | Case-scoped evidence, false facts as narrative | Fabrication propagated, cited, unhedged; **Verifier `grounded=True`** |
| Batch 4 | *Control* — claim NOT in any chunk | Correctly **abstained** / rejected — the gate works for its actual job |
| Batch 5 | **Global KB** (`is_global=True`), false legal procedure | Fabrication propagated, cited, unhedged; **Verifier `grounded=True`** — same result, wider blast radius (shared across all users) |

**What this means precisely.** The Verifier validates *answer-to-source faithfulness* — and it
does that job correctly (Batch 4 proves it rejects claims with no supporting chunk). It has no
mechanism, and structurally cannot have one, to validate whether the *source itself* is truthful.
So a poisoned FIR narrative, roznamcha entry, or reference document propagates confident, cited,
unhedged falsehoods to an authorized user. In Urdu, Roman Urdu, and English identically. The
scopes don't blend (global and case retrieval are mutually exclusive by construction), so there
is **no cross-scope leakage** — the harm is integrity, not confidentiality.

**Proposed fix (documented, not applied):** a *single-source / low-corroboration flag*. The
Verifier already receives the full cited-chunk list and already traverses citations per-chunk, so
the signal is computable with ~4 additive lines + one pipeline event + one UI tag, no pipeline
restructuring. **Documented tradeoff:** the cheap version is answer-level ("does this whole answer
rest on one document"), not per-claim — a mixed answer that hides a single-source fabrication among
corroborated claims wouldn't be flagged. It's a deliberate first increment, with a per-claim
version as the named follow-on. This does not *solve* Finding 4 (you can't detect a lie from text
alone) but it surfaces the risk to the human reader.

---

## 6. Should you spend your last day on automated tooling?

**Short answer: no — not for finding new issues. Yes — only if you specifically need
repeatable, shareable regression evidence and have the fix in hand.**

### What the tools would actually add
| Tool | What it targets | Would it find something manual testing didn't? |
|------|-----------------|-----------------------------------------------|
| **Promptfoo** | Variant sweeps of the fabrication technique | **No new bug** — it measures how *consistent* Finding 4 is across topics/phrasings/languages. Manual runs already show 75–100% propagation every time. It produces a nice scored table, not a new finding. |
| **Nuclei** | Known-CVE / misconfig templates against the API | Low odds. The surface is a custom FastAPI app, not off-the-shelf software with public CVEs. Might catch a generic header/TLS nit. |
| **ZAP / Burp** | Automated OWASP crawl + active scan | Low odds of new findings — IDOR/CSRF/injection/auth were all tested by hand and held. Value is breadth-of-coverage assurance, not discovery. |
| **DeepTeam / Garak** | LLM jailbreak / safety probes | Possible marginal value, but aimed at model-safety (toxicity, jailbreaks) rather than *your* architecture. Garak in particular can pull large model/dependency downloads — a real cost on a machine that just hit a disk-space wall. |

### The honest cost/benefit for a one-day deadline
- **The one finding that matters (Finding 4) is already confirmed and reproducible by hand.**
  Tooling would re-confirm it, not extend it.
- We already sank meaningful time into a Promptfoo harness that fights the environment (it kills
  and respawns your backend per variant, which broke repeatedly and left artifacts to clean up).
  That's time not spent on the fix.
- Every tool run hits the **shared model server** and the **Groq free-tier token budget** — real
  contention if teammates are testing.

### Recommendation for the remaining day, in priority order
1. **Apply and verify the Finding 4 single-source flag** (the ~4-line Verifier change + UI tag).
   This is the only finding with real user impact, and the fix is scoped and understood. Shipping
   the *mitigation* beats producing more evidence of the *problem*.
2. **Lock down Finding 5 permanently** — get the JWT secret out of `SHARE/`, and set
   `ENVIRONMENT=production` on any real deployment (which also fixes Finding 6's cookie flag).
3. **Cheap hardening:** propagate the F-06 UUID-validation to `sessions.py` (Finding 8), add a
   rate limit to `community/refresh` (Finding 7). Both are a few lines each.
4. **Only if time remains and you want shareable regression artifacts:** run *one* small Promptfoo
   sweep — but drop the backend-restart step from the harness (talk to Chroma directly), since that
   step is what made it fragile. This is a "nice to have for the report," not a discovery activity.

**If you do only one thing:** #1. The tools measure a problem you've already found; the fix removes it.

---

## 7. Environment state at time of writing
Clean baseline: backend healthy on `127.0.0.1:8001`, 7,373 vectors, **zero** leftover test
artifacts (verified), JWT secret rotated and consistent across both `.env` files, RLS forced.

The exploratory Promptfoo harness was **removed** after the tooling analysis in §6 — it ran a
single variant correctly by hand but had an unresolved failure when driven through Promptfoo's
subprocess wrapper (the restarted backend wasn't the one the query hit), and the decision was
not to invest further in tooling. Nothing under `security-tests/promptfoo/` remains. The manual
probe scripts that produced the findings above are retained:
`indirect_injection_probe.py` (Batch 2), `fabricated_claim_probe.py` (Batch 3),
`invalid_citation_probe.py` (Batch 4), and `global_kb_fabrication_probe.py` (Batch 5).
