# Manual QA — Recording Template

One block per question. The runner prints every field you need; this file
is where your **judgement** goes, because the runner deliberately does not
grade itself (see `QA_READINESS_REPORT.md` §2).

**How to run one question**

```bash
PYTHONPATH=. python scripts/manual_qa_runner.py -q "your question here"
```

**How to run the smoke set**

```bash
PYTHONPATH=. python scripts/manual_qa_runner.py
PYTHONPATH=. python scripts/manual_qa_runner.py --category temporal
PYTHONPATH=. python scripts/manual_qa_runner.py --limit 5
```

The JSON report lands in `docs/aggregate-shadow/manual_qa_report.json`.

---

## The four things to judge separately

A single "is it right?" hides where a defect lives. Judge these
independently, because the fix is in a different place for each:

| Judgement | The question you are answering | If wrong, the defect is in |
|---|---|---|
| **Spec correctness** | Did it understand what was asked? | the NL prompt (`nl_spec.py`) |
| **Route correctness** | Did each route compute its own answer correctly? | the route or compiler |
| **Final answer correctness** | Is the number right, and is it the number the question asked for? | composition / reconciliation |
| **Verification honesty** | Does the claimed confidence match what actually ran? | the policy (`verification_policy.py`) |

The fourth is the one most easily missed. **A correct number presented as
"independently verified" when no second route ran is a defect**, even
though the number is right — because the claim is stronger than the
evidence. Record it.

---

## Record block — copy one per question

```markdown
### QA-<nnn> · <short name>

**Question**
> <exactly what you typed>

**Category**
simple count | filters | relationships | multi-source | temporal | value literal | unsupported | other

**Expected behaviour**
<What should happen. Say which routes you expect and whether the answer may
be called verified. If you know the true value independently — from SQL, a
count you did by hand — write it here and say how you got it. If you do
not know it, write "unknown" rather than guessing.>

**Actual behaviour**
<Paste the ANSWER / VERIFIED / ROUTES lines from the runner.>

| Check | Verdict | Note |
|---|---|---|
| Spec correctness         | OK / WRONG / UNSURE | |
| Route correctness        | OK / WRONG / UNSURE / N/A | |
| Final answer correctness | OK / WRONG / UNKNOWN | |
| Verification honesty     | OK / OVERSTATED / UNDERSTATED | |

**Issue found**
<None, or: what is wrong, which stage produced it, and how to reproduce.
If the answer is wrong, say which stage FIRST went wrong — a bad spec makes
every later stage wrong for a reason that is not their fault.>

**Severity**
BLOCKER — a wrong number served as if correct
HIGH    — wrong answer, or a verification claim stronger than the evidence
MEDIUM  — a refusal that should have been an answer, or vice versa
LOW     — wording, latency, presentation

**Reproducible?**
Yes / No / Intermittent — <ran it N times, got X>
```

---

## Worked example

This is a real finding from the first smoke run, recorded in the form
above so the shape is unambiguous.

### QA-001 · Cases registered in 2026

**Question**
> How many cases were registered in 2026?

**Category**
temporal

**Expected behaviour**
M1 fires (Postgres-authoritative date over a graph population). PARTIAL —
AGE cannot evaluate the window, so it must not run, and the answer must be
marked not independently verified. Phase 6 established the true value as
**51** by direct SQL over `cases.incident_date`.

**Actual behaviour**
```
SPEC     : count(Case) grain=ENTITY key=case_id
           (no time_window)
DECISION : PARTIAL (deterministic=PARTIAL, llm=NONE)
ANSWER   : ANSWERED  value=73
VERIFIED : False — Not independently verified.
```

| Check | Verdict | Note |
|---|---|---|
| Spec correctness         | **WRONG** | `time_window` omitted entirely |
| Route correctness        | OK | 73 is the correct count of *all* cases |
| Final answer correctness | **WRONG** | 73 is every case; the answer to the question asked is 51 |
| Verification honesty     | OK | correctly declined to claim verification |

**Issue found**
The model dropped the year restriction, producing a spec that asks "how
many cases" rather than "how many cases in 2026". The structured route then
answered that spec correctly. Every stage after generation behaved
properly — the defect is entirely in NL→spec.

Rephrasing to *"How many cases were registered in the year 2026?"* produced
the correct spec with `incident_date 2026-01-01..2026-12-31` and returned
**51**.

**Severity**
HIGH — a wrong number, served without any indication it was wrong. The
verification note says "not independently verified", which is true but does
not tell the reader the *question* was altered.

**Reproducible?**
Intermittent — ran 3 times: got 73 (window dropped), None (CONFLICT), and
51 (window correct). Same question, three outcomes.

---

## Session log

Keep one row per question so a run can be reviewed at a glance.

| ID | Question | Status | Value | Verified | Spec | Route | Answer | Honesty | Severity |
|---|---|---|---|---|---|---|---|---|---|
| QA-001 | cases in 2026 | ANSWERED | 73 | no | WRONG | OK | WRONG | OK | HIGH |
| | | | | | | | | | |

---

## Before filing an issue

1. **Run it again.** Spec generation is not deterministic (QA-001). An
   intermittent defect and a consistent one need different fixes, and
   "ran it once" cannot tell them apart.
2. **Say which stage failed first.** The runner prints them in order.
3. **Separate "wrong number" from "wrong question".** 73 was a correct
   count of the wrong population — that is a generation defect, not an
   arithmetic one.
4. **Check the warnings.** A figure carrying `INSUFFICIENT_DATA_COVERAGE`
   is not the same claim as a bare figure, and judging it as one produces
   a false report.
