# Muhafiz Knowledge Base — Tier 1 Legal and Procedural References

This explains why each Tier 1 reference was selected for the Muhafiz knowledge base, and why
two Tier 1 documents were left out for now. Tier 1 covers documents that directly govern how
evidence is collected, handled, and made admissible — the actual scope of what Muhafiz models.

Ingestion mechanics, verification, and rollout are tracked separately in
`MUHAFIZ_KNOWLEDGE_BASE_INTEGRATION_PLAN.md` at the repo root; this file is the durable record
of *what was selected and why*, kept with the source documents themselves.

## Documents selected

**1. Code of Criminal Procedure (CrPC), 1898**
The actual law behind FIR registration in our schema, including form type 154. Covers arrest
powers, search and seizure, and how the whole investigation process is legally structured —
the core reference for everything Muhafiz models.

**2. Qanun-e-Shahadat Order (QSO), 1984**
Pakistan's law of evidence. Explains why our schema is built the way it is — e.g. why witness
statement content is not stored by police and only lives with the courts, a rule already
followed when designing the witness table.

**3. Police Order, 2002**
Governs police organization and powers in Punjab, Islamabad, and Sindh — where all current
case data is set (Lahore, Faisalabad, Rawalpindi, Karachi, Hyderabad). Explains the officer
ranks and station structure the schema already uses (ASI, SI, SHO).

**4. Police Rules, 1934 (Volume III only)**
The actual rulebook behind day-to-day station procedures — the closest real-world source for
the malkhana register (property), the roznamcha (station diary), and general case-file
handling, all already core tables in the schema. The rules span three volumes: Volume I
(staffing, equipment, administration) and Volume II (appointments, discipline, training) are
untouched by our schema; Volume III (police station, investigation, case diaries, arrest and
custody) is exactly what our schema models, so only Volume III is included.

**5. Provincial and Federal Forensic SOPs** (evidence collection and chain of custody)
Covers how evidence is packaged, labeled, and tracked — maps directly onto the weapon register
and malkhana register tables, and explains the kind of ballistic comparison references already
appearing in generated cases.

**6. Pakistan Telecommunication (Re-organization) Act, 1996**
Governs call data records and telecom cooperation with investigations. Several cases rely on
phone tracing to connect crimes, including narcotics cases linked by a shared phone number, a
kidnapping case, and an extortion case.

**7. Anti-Rape (Investigation and Trial) Act, 2021, and Anti-Rape Investigation Rules,
2022/2023**
Covers evidence marking, packaging, and chain of custody specifically for assault-related
cases — directly relevant to the sensitive case categories in the dataset. Only the evidence
and investigation sections are in scope; trial procedure sections fall under court work, not
police work.

**Prevention of Electronic Crimes Act (PECA), 2016 — no PDF in this folder**
Rationale for selection is the same as the rest of this list (the two cyber fraud cases cite
PECA sections 14 and 21 specifically, so it's already reflected in real case data), but **no
source PDF for PECA was supplied alongside the other seven** — only 7 files exist in this
folder. Flagged here rather than silently mis-numbered against a file that doesn't exist.
Add the actual PECA text as an 8th document via the same ingestion path
(`scripts/ingest_knowledge_base_tier1.py`) once a source file is available.

## Documents not selected for now

**Khyber Pakhtunkhwa Police Act, 2017**
Only matters for cases set in KP province. No current seed or generated cases are set there —
not needed right now, can be added later if that changes.

**Balochistan Police Act, 2011**
Same reasoning as the KP act — no current cases set in Balochistan.
