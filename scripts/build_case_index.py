# -*- coding: utf-8 -*-
"""Build case_index.csv: 14 retrofitted Batch-0 cases + 20 new Batch-1 cases
(1 already built in the Step-1 dry run: CASE-DRY-001), per
SYNTHETIC_DATASET_PLAN.md §1.1/§1.3/§1.4/§8 step 1.
"""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "memory" / "case_index.csv"

FIELDS = ["case_id", "fir_number", "crime_category", "investigation_officer",
          "police_station", "incident_date", "investigation_status", "location",
          "initial_description", "shape", "source", "linked_doc_ids", "structured_record_ids"]

# ── 14 retrofitted Batch-0 cases (data already known from manifest.json) ───
RETROFIT = [
    # case_id, fir_number, fir_id, category, station, date, status, docs
    ("CASE-B0-THEFT-001", "1/2026", "FIR-2026-THEFT-001", "Mobile/Vehicle Theft", "Bhara Kahu", "2026-01-03",
     "Closed – Untraced", "Inspector Fariha Saeed",
     "firs/FIR-2026-THEFT-001.pdf;complaint_applications/DARKHAST-FIR-2026-THEFT-001.pdf;case_diaries/CASEDIARY-FIR-2026-THEFT-001-01.pdf;charge_sheets/CHARGESHEET-FIR-2026-THEFT-001.pdf"),
    ("CASE-B0-THEFT-002", "—", "FIR-2026-THEFT-002", "Mobile/Vehicle Theft", "Shalimar", "2026-03-27",
     "Under Investigation", "SI (unassigned)", "firs/FIR-2026-THEFT-002.pdf"),
    ("CASE-B0-CYBER-001", "—", "FIR-2026-CYBER-001", "Cyber Fraud/Online Scam", "Shahzad Town", "2026-05-13",
     "Charge-Sheeted", "Inspector (Cyber Cell)",
     "firs/FIR-2026-CYBER-001.pdf;complaint_applications/DARKHAST-FIR-2026-CYBER-001.pdf;case_diaries/CASEDIARY-FIR-2026-CYBER-001-01.pdf;charge_sheets/CHARGESHEET-FIR-2026-CYBER-001.pdf"),
    ("CASE-B0-CYBER-002", "—", "FIR-2026-CYBER-002", "Cyber Fraud/Online Scam", "Shahzad Town", "2026-03-08",
     "Under Investigation", "SI (Cyber Cell)", "firs/FIR-2026-CYBER-002.pdf"),
    ("CASE-B0-HAR-001", "—", "FIR-2026-HAR-001", "Harassment/Cyber Harassment", "Kohsar", "2026-05-08",
     "Charge-Sheeted", "Inspector (Cyber Cell)",
     "firs/FIR-2026-HAR-001.pdf;complaint_applications/DARKHAST-FIR-2026-HAR-001.pdf;case_diaries/CASEDIARY-FIR-2026-HAR-001-01.pdf;charge_sheets/CHARGESHEET-FIR-2026-HAR-001.pdf"),
    ("CASE-B0-HAR-002", "—", "FIR-2026-HAR-002", "Harassment/Cyber Harassment", "Industrial Area", "2026-04-04",
     "Under Investigation", "SI (unassigned)", "firs/FIR-2026-HAR-002.pdf"),
    ("CASE-B0-RTA-001", "—", "FIR-2026-RTA-001", "Road Traffic Accident", "Tarnol", "2026-02-22",
     "Pending Trial", "Traffic SI",
     "firs/FIR-2026-RTA-001.pdf;complaint_applications/DARKHAST-FIR-2026-RTA-001.pdf;case_diaries/CASEDIARY-FIR-2026-RTA-001-01.pdf"),
    ("CASE-B0-RTA-002", "—", "FIR-2026-RTA-002", "Road Traffic Accident", "Shalimar", "2026-06-17",
     "Under Investigation", "Traffic SI (unassigned)", "firs/FIR-2026-RTA-002.pdf"),
    ("CASE-B0-DOM-001", "—", "FIR-2026-DOM-001", "Domestic Dispute", "Golra", "2026-03-13",
     "Under Investigation", "SI (unassigned)", "firs/FIR-2026-DOM-001.pdf"),
    ("CASE-B0-DOM-002", "—", "FIR-2026-DOM-002", "Domestic Dispute", "Bhara Kahu", "2026-06-07",
     "Under Investigation", "SI (unassigned)", "firs/FIR-2026-DOM-002.pdf"),
    ("CASE-B0-BUR-001", "—", "FIR-2026-BUR-001", "Burglary/House Theft", "Industrial Area", "2026-01-15",
     "Under Investigation", "SI (unassigned)", "firs/FIR-2026-BUR-001.pdf"),
    ("CASE-B0-BUR-002", "—", "FIR-2026-BUR-002", "Burglary/House Theft", "Industrial Area", "2026-04-17",
     "Under Investigation", "SI (unassigned)", "firs/FIR-2026-BUR-002.pdf"),
    ("CASE-B0-FRAUD-001", "—", "FIR-2026-FRAUD-001", "Cheating/Financial Fraud", "Secretariat", "2026-03-08",
     "Under Investigation", "SI (Fraud Cell)", "firs/FIR-2026-FRAUD-001.pdf"),
    ("CASE-B0-FRAUD-002", "—", "FIR-2026-FRAUD-002", "Cheating/Financial Fraud", "Lohi Bher", "2026-02-14",
     "Under Investigation", "SI (Fraud Cell)", "firs/FIR-2026-FRAUD-002.pdf"),
]

retrofit_rows = []
for case_id, fir_no, fir_id, cat, station, date, status, io, *rest in RETROFIT:
    docs = rest[0] if rest else f"firs/{fir_id}.pdf"
    retrofit_rows.append({
        "case_id": case_id, "fir_number": fir_no, "crime_category": cat,
        "investigation_officer": io, "police_station": station, "incident_date": date,
        "investigation_status": status, "location": "", "initial_description": "",
        "shape": "complex" if docs.count(";") >= 3 else ("developing" if ";" in docs else "minimal"),
        "source": "retrofitted", "linked_doc_ids": docs, "structured_record_ids": "",
    })

# NOTE: the existing 3 Missing Person + 3 Traffic Accident Batch-0 reports are
# NOT retrofitted with Case records in this pass, to keep the total at the
# documented "14 retrofitted + 20 new = 34" rather than silently drifting
# upward. They remain reachable via the existing manifest.json entries as
# before; retrofitting them is a small, clearly-scoped follow-up if wanted,
# not folded in here without flagging it.

# ── 20 new Batch-1 cases (1 already built: CASE-DRY-001) ───────────────────
NEW_CASES = [
    dict(case_id="CASE-DRY-001", fir_number="FIR-2026-ARMS-001", crime_category="Illegal Weapon Possession",
         investigation_officer="Inspector (Ramna)", police_station="Ramna", incident_date="2026-05-02",
         investigation_status="Under Investigation", shape="developing",
         linked_doc_ids="firs/FIR-2026-ARMS-001.pdf;witness_statements/WITNESS-FIR-2026-ARMS-001-01.pdf;witness_statements/WITNESS-FIR-2026-ARMS-001-02.pdf"),
    dict(case_id="CASE-002", fir_number="FIR-2026-ARMS-002", crime_category="Illegal Weapon Possession",
         investigation_officer="Inspector (Aabpara)", police_station="Aabpara", incident_date="2026-02-09",
         investigation_status="Under Investigation", shape="minimal"),
    dict(case_id="CASE-003", fir_number="FIR-2026-ARMS-003", crime_category="Illegal Weapon Possession",
         investigation_officer="Inspector (Margalla)", police_station="Margalla", incident_date="2026-06-14",
         investigation_status="Under Investigation", shape="developing"),
    dict(case_id="CASE-004", fir_number="FIR-2026-CYBER-004", crime_category="Cyber Fraud/Online Scam",
         investigation_officer="Inspector (Cyber Cell)", police_station="Shah Zad Town", incident_date="2026-01-11",
         investigation_status="Under Investigation", shape="developing"),
    dict(case_id="CASE-005", fir_number="FIR-2026-CYBER-005", crime_category="Cyber Fraud/Online Scam",
         investigation_officer="Inspector (Cyber Cell)", police_station="Kohsar", incident_date="2026-02-19",
         investigation_status="Charge-Sheeted", shape="developing"),
    dict(case_id="CASE-006", fir_number="FIR-2026-CYBER-006", crime_category="Cyber Fraud/Online Scam",
         investigation_officer="Inspector (Cyber Cell)", police_station="Aabpara", incident_date="2026-03-25",
         investigation_status="Closed – Convicted", shape="complex"),
    dict(case_id="CASE-007", fir_number="FIR-2026-BUR-007", crime_category="Burglary/House Theft",
         investigation_officer="Inspector (Tarnol)", police_station="Tarnol", incident_date="2026-01-20",
         investigation_status="Under Investigation", shape="developing"),
    dict(case_id="CASE-008", fir_number="FIR-2026-BUR-008", crime_category="Burglary/House Theft",
         investigation_officer="Inspector (Golra)", police_station="Golra", incident_date="2026-02-27",
         investigation_status="Charge-Sheeted", shape="developing"),
    dict(case_id="CASE-009", fir_number="FIR-2026-BUR-009", crime_category="Burglary/House Theft",
         investigation_officer="Inspector (Margalla)", police_station="Margalla", incident_date="2026-04-02",
         investigation_status="Pending Trial", shape="complex"),
    dict(case_id="CASE-010", fir_number="FIR-2026-THEFT-010", crime_category="Mobile/Vehicle Theft",
         investigation_officer="SI (Sabzi Mandi)", police_station="Sabzi Mandi", incident_date="2026-01-30",
         investigation_status="Under Investigation", shape="minimal"),
    dict(case_id="CASE-011", fir_number="FIR-2026-THEFT-011", crime_category="Mobile/Vehicle Theft",
         investigation_officer="SI (Koral)", police_station="Koral", incident_date="2026-03-14",
         investigation_status="Under Investigation", shape="developing"),
    dict(case_id="CASE-012", fir_number="FIR-2026-THEFT-012", crime_category="Mobile/Vehicle Theft",
         investigation_officer="SI (Nilore)", police_station="Nilore", incident_date="2026-05-19",
         investigation_status="Closed – Convicted", shape="complex"),
    dict(case_id="CASE-013", fir_number="FIR-2026-DOM-013", crime_category="Domestic Dispute",
         investigation_officer="SI (Lohi Bher)", police_station="Lohi Bher", incident_date="2026-02-05",
         investigation_status="Under Investigation", shape="minimal"),
    dict(case_id="CASE-014", fir_number="FIR-2026-DOM-014", crime_category="Domestic Dispute",
         investigation_officer="SI (Sangjani)", police_station="Sangjani", incident_date="2026-04-22",
         investigation_status="Under Investigation", shape="minimal"),
    dict(case_id="CASE-015", fir_number="FIR-2026-FRAUD-015", crime_category="Cheating/Financial Fraud",
         investigation_officer="SI (Fraud Cell)", police_station="Secretariat", incident_date="2026-02-14",
         investigation_status="Under Investigation", shape="developing"),
    dict(case_id="CASE-016", fir_number="FIR-2026-FRAUD-016", crime_category="Cheating/Financial Fraud",
         investigation_officer="SI (Fraud Cell)", police_station="Bhara Kahu", incident_date="2026-04-30",
         investigation_status="Closed – Untraced", shape="complex"),
    dict(case_id="CASE-017", fir_number="FIR-2026-HAR-017", crime_category="Harassment/Cyber Harassment",
         investigation_officer="SI (Cyber Cell)", police_station="Shah Zad Town", incident_date="2026-03-02",
         investigation_status="Under Investigation", shape="minimal"),
    dict(case_id="CASE-018", fir_number="FIR-2026-HAR-018", crime_category="Harassment/Cyber Harassment",
         investigation_officer="SI (Cyber Cell)", police_station="Aabpara", incident_date="2026-05-11",
         investigation_status="Under Investigation", shape="minimal"),
    dict(case_id="CASE-019", fir_number="FIR-2026-RTA-019", crime_category="Road Traffic Accident",
         investigation_officer="Traffic SI (Tarnol)", police_station="Tarnol", incident_date="2026-01-27",
         investigation_status="Under Investigation", shape="minimal"),
    dict(case_id="CASE-020", fir_number="—", crime_category="Missing Person",
         investigation_officer="Inspector (Sangjani)", police_station="Sangjani", incident_date="2026-06-25",
         investigation_status="Under Investigation", shape="minimal"),
]

for c in NEW_CASES:
    c.setdefault("location", "")
    c.setdefault("initial_description", "")
    c.setdefault("source", "new")

# ── Auto-derive linked_doc_ids / structured_record_ids from what actually
# exists on disk, instead of hand-listing them (hand-listing is exactly how
# the fir_number drift bug above happened: hardcoded values silently go
# stale once the real generated files use different numbers than planned) ──
_DOC_SUBDIRS = ["firs", "complaint_applications", "case_diaries", "charge_sheets",
                "witness_statements", "recovery_memos"]

def _discover_linked_docs(fir_number: str) -> str:
    if not fir_number or fir_number == "—":
        return ""
    found = []
    for sub in _DOC_SUBDIRS:
        subdir = ROOT / "data" / "memory" / sub
        if not subdir.exists():
            continue
        found.extend(f"{sub}/{f.name}" for f in sorted(subdir.glob(f"*{fir_number}*.pdf")))
    return ";".join(found)

_STRUCTURED_CSV = ROOT / "data" / "memory" / "structured_records" / "case_management_export.csv"

def _discover_structured_records() -> dict[str, list[str]]:
    by_case: dict[str, list[str]] = {}
    if _STRUCTURED_CSV.exists():
        with open(_STRUCTURED_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                by_case.setdefault(row["case_id"], []).append(row["record_id"])
    return by_case

_structured_by_case = _discover_structured_records()

for c in NEW_CASES:
    c["linked_doc_ids"] = _discover_linked_docs(c.get("fir_number", ""))
    c["structured_record_ids"] = ";".join(_structured_by_case.get(c["case_id"], []))

all_rows = retrofit_rows + NEW_CASES

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS)
    w.writeheader()
    for row in all_rows:
        w.writerow({k: row.get(k, "") for k in FIELDS})

print(f"wrote {len(all_rows)} case rows to {OUT}")
print(f"  retrofitted: {len(retrofit_rows)}, new: {len(NEW_CASES)}")
