# -*- coding: utf-8 -*-
"""Build the structured-records evidence type: a small case-management-system
export CSV, per SYNTHETIC_DATASET_PLAN.md §1.6. Deterministic, no narrative
generation — this is genuinely row/field data, not prose.
"""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "memory" / "structured_records"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = OUT_DIR / "case_management_export.csv"

FIELDS = ["record_id", "case_id", "fir_number", "record_type", "field_1", "field_2",
          "field_3", "source_system", "export_date"]

rows = []

# Property-tag entries, for the recovery-adjacent cases
PROPERTY_TAGS = [
    ("REC-001", "CASE-003", "FIR-2026-ARMS-003", "property-tag", "پستول بور 30", "ٹیگ نمبر PT-2026-0031", "مال خانہ - تھانہ مارگلہ"),
    ("REC-002", "CASE-007", "FIR-2026-BUR-007", "property-tag", "چوری شدہ الیکٹرانکس", "ٹیگ نمبر PT-2026-0072", "مال خانہ - تھانہ ترنول"),
    ("REC-003", "CASE-008", "FIR-2026-BUR-008", "property-tag", "چوری شدہ زیورات", "ٹیگ نمبر PT-2026-0083", "مال خانہ - تھانہ گولڑہ"),
    ("REC-004", "CASE-009", "FIR-2026-BUR-009", "property-tag", "سوزوکی پک اپ (ضبط شدہ)", "ٹیگ نمبر PT-2026-0094", "مال خانہ - تھانہ مارگلہ"),
    ("REC-005", "CASE-012", "FIR-2026-THEFT-012", "property-tag", "ہونڈا سی ڈی-70 (برآمد شدہ)", "ٹیگ نمبر PT-2026-0125", "مال خانہ - تھانہ نیلور"),
    ("REC-006", "CASE-DRY-001", "FIR-2026-ARMS-001", "property-tag", "پستول بور 30", "ٹیگ نمبر PT-2026-0011", "مال خانہ - تھانہ رمنہ"),
]
for rid, cid, fir, rtype, f1, f2, f3 in PROPERTY_TAGS:
    rows.append({"record_id": rid, "case_id": cid, "fir_number": fir, "record_type": rtype,
                 "field_1": f1, "field_2": f2, "field_3": f3,
                 "source_system": "MalKhana-CMS-v2", "export_date": "2026-07-01"})

# Court-listing entries, for Pending Trial / post-charge-sheet cases
COURT_LISTINGS = [
    ("CRT-001", "CASE-009", "FIR-2026-BUR-009", "court-listing", "علاقہ مجسٹریٹ عدالت، مارگلہ", "2026-08-12", "زیر سماعت"),
    ("CRT-002", "CASE-005", "FIR-2026-CYBER-005", "court-listing", "علاقہ مجسٹریٹ عدالت، کوہسار", "2026-07-20", "چالان جمع"),
    ("CRT-003", "CASE-006", "FIR-2026-CYBER-006", "court-listing", "علاقہ مجسٹریٹ عدالت، آبپارہ", "2026-06-30", "فیصلہ - مجرم قرار"),
    ("CRT-004", "CASE-012", "FIR-2026-THEFT-012", "court-listing", "علاقہ مجسٹریٹ عدالت، نیلور", "2026-06-25", "فیصلہ - مجرم قرار"),
    ("CRT-005", "CASE-008", "FIR-2026-BUR-008", "court-listing", "علاقہ مجسٹریٹ عدالت، گولڑہ", "2026-08-05", "چالان جمع"),
]
for rid, cid, fir, rtype, f1, f2, f3 in COURT_LISTINGS:
    rows.append({"record_id": rid, "case_id": cid, "fir_number": fir, "record_type": rtype,
                 "field_1": f1, "field_2": f2, "field_3": f3,
                 "source_system": "CourtLiaison-CMS-v1", "export_date": "2026-07-01"})

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow(r)

print(f"wrote {len(rows)} structured records to {OUT}")
