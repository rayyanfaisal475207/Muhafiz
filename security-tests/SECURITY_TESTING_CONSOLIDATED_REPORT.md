# Muhafiz — Consolidated Security Testing Report

**Scope:** All manual security and AI red-team testing, Phases 0–2 plus Batch 5.
**Method:** Entirely manual — code audit plus hand-driven probes against a local
instance. No automated tooling (Promptfoo / DeepTeam / Garak / ZAP / Nuclei) was
used to produce any finding in this report.
**Environment:** `127.0.0.1` only, `main @ 536aa3c`. The shared tunnel was never
targeted. All synthetic artifacts torn down and removal re-verified.
**Date:** 2026-09-02 → 2026-09-03.

This supersedes `PHASE_0_1_SECURITY_REPORT.md` and `PHASE_2_SECURITY_REPORT.md`
where they differ — both are accurate as written but predate Batch 5 and the
Finding 5 reopening (see *Corrections to earlier reports*).

---

## 1. Findings register

| # | Severity | Title | Status |
|---|---|---|---|
| **5** | **Critical** | Forgeable JWT via public default secret | **Fixed** (rotated; reopened once; re-verified) |
| **4** | Medium | Verifier grounding = faithful-to-source, not faithful-to-truth | **Open** — fix proposed, not applied |
| **1** | Medium | Duplicated cross-case role gate (`xagg.py` vs canonical) | Open — latent drift, not exploitable |
| **6** | Medium | `Secure` cookie flag off under `ENVIRONMENT=development` | Open |
| **2** | Low–Med | `run_aggregate` `user_role` defaults to `"investigator"` | Open — fails safe, silently |
| **7** | Low | `community/refresh` unthrottled | Open |
| **8** | Low | `sessions.py` 500s on malformed `session_id` | Open |
| **3** | Info | Bearer-token fallback not dev-gated | Closed — no exploit path |

---

## 2. Finding 5 — Forgeable JWT (Critical, fixed)

`JWT_SECRET_KEY=your-secret-key-for-dev` — the public default from source — was
live in `.env` and the transferred `SHARE/.env`. The guard that would ref