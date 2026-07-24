# -*- coding: utf-8 -*-
"""Build the full entity_roster.csv: dry-run entities (CNIC format fixed to the
clearly-synthetic 00000-XXXXXXX-X block) + the full designed cross-case cast,
per SYNTHETIC_DATASET_PLAN.md §2.
"""
import csv
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "memory" / "entity_roster.csv"

FIELDS = ["entity_id", "type", "canonical_name", "canonical_attributes", "surface_variants",
          "designed_as", "pair_or_group_id", "case_ids", "appears_in", "cnic_shown_in"]


def cnic(seed: str) -> str:
    """Clearly-synthetic CNIC: reserved 00000 prefix (no real province/district
    uses this), keeps the real 5-7-1 grouping so regex-extraction still gets
    exercised against the real structural pattern."""
    r = random.Random(seed)
    return f"00000-{r.randint(1000000,9999999)}-{r.randint(0,9)}"


def phone(seed: str) -> str:
    r = random.Random(seed)
    return f"03{r.randint(0,9)}{r.randint(0,9)}-{r.randint(1000000,9999999)}"


def plate(seed: str) -> str:
    r = random.Random(seed)
    letters = "ABCDEFGHJKLMN"
    return f"ICT-{r.choice(letters)}{r.choice(letters)}-{r.randint(100,999)}"


rows = []


def add(entity_id, etype, name, attrs, variants, designed_as, group, case_ids, appears_in, cnic_shown_in):
    rows.append({
        "entity_id": entity_id, "type": etype, "canonical_name": name,
        "canonical_attributes": attrs, "surface_variants": variants, "designed_as": designed_as,
        "pair_or_group_id": group, "case_ids": ";".join(case_ids), "appears_in": ";".join(appears_in),
        "cnic_shown_in": ";".join(cnic_shown_in),
    })


# ── Dry-run entities, CNIC format fixed (were real-looking province codes) ──
add("P-DRY-001", "person", "عمران ستار",
    f"role=accused; father's_name=غلام ستار; cnic={cnic('p001-fix')}; address=مکان نمبر 12، گلی 4، جی-9/1، اسلام آباد",
    "عمران ستار", "confusable-pair", "CP-DRY-01", ["CASE-DRY-001"], ["FIR-2026-ARMS-001"],
    [])  # no accused_cnic field exists anywhere in the ground-truth schema — his CNIC
         # is never actually shown in any document text, so cnic_shown_in must stay empty
add("P-DRY-002", "person", "عمران ستار",
    f"role=unrelated shopkeeper (landmark reference only); father's_name=ناصر علی; cnic={cnic('p002-fix')}; address=دکان 3، آبپارہ مارکیٹ، اسلام آباد",
    "عمران ستار", "confusable-pair", "CP-DRY-01", ["CASE-DRY-001"], ["WITNESS-FIR-2026-ARMS-001-01"], [])
add("P-DRY-003", "person", "احمد رضا قریشی",
    f"role=complainant/witness; father's_name=محمد قریشی; cnic={cnic('p003-fix')}; address=مکان 7، سیکٹر جی-8/2، اسلام آباد; phone=0300-1234567",
    "احمد رضا قریشی|احمد رضا|احمد رضا قرشی", "name-variant", "NV-DRY-01", ["CASE-DRY-001"],
    ["FIR-2026-ARMS-001", "WITNESS-FIR-2026-ARMS-001-01"], ["FIR-2026-ARMS-001"])
add("P-DRY-004", "person", "ظفر اقبال",
    f"role=second witness (recovery mashir); father's_name=اقبال حسین; cnic={cnic('p004-fix')}; address=مکان 22، بارہ کہو، اسلام آباد",
    "ظفر اقبال", "single-mention", "", ["CASE-DRY-001"], ["WITNESS-FIR-2026-ARMS-001-02"], [])
add("WPN-DRY-001", "weapon", "لائسنس کے بغیر پستول، بور 30",
    "recovered_from=P-DRY-001; description=فرانسیسی ساخت کا خودکار پستول، بور 30، میگزین میں 6 گولیاں",
    "", "single-mention", "", ["CASE-DRY-001"], ["FIR-2026-ARMS-001", "WITNESS-FIR-2026-ARMS-001-02"], [])

# ── Repeat offenders (6), cross-case ────────────────────────────────────────
add("P-001", "person", "بلال شہزاد", f"role=cyber fraud ring leader; father's_name=شہزاد اکرم; cnic={cnic('p1')}; address=مکان 14، سیکٹر ای-11، اسلام آباد",
    "بلال شہزاد", "recurring", "ORG-001", ["CASE-004", "CASE-005", "CASE-006"], [], ["CASE-006"])
add("P-002", "person", "وقاص علی نیازی", f"role=burglary ring co-accused; father's_name=علی نیازی; cnic={cnic('p2')}; address=مکان 9، گلی 2، ترنول، اسلام آباد",
    "وقاص علی نیازی", "recurring", "ORG-002", ["CASE-007", "CASE-009"], [], ["CASE-009"])
add("P-003", "person", "کامران شیخ", f"role=burglary ring co-accused; father's_name=محمد شیخ; cnic={cnic('p3')}; address=مکان 21، گولڑہ موڑ، اسلام آباد",
    "کامران شیخ", "recurring", "ORG-002", ["CASE-008", "CASE-009"], [], ["CASE-009"])
add("P-004", "person", "نعمان اختر بھٹی", f"role=repeat mobile/vehicle theft offender; father's_name=اختر بھٹی; cnic={cnic('p4')}; address=ADDR-003",
    "نعمان اختر بھٹی", "recurring", "", ["CASE-010", "CASE-011", "CASE-012"], [], ["CASE-012"])
add("P-005", "person", "شفیق الرحمن ترین", f"role=repeat domestic dispute offender; father's_name=غلام رحمن ترین; cnic=; address=مکان 30، لوہی بھیر، اسلام آباد",
    "شفیق الرحمن ترین", "recurring", "", ["CASE-013", "CASE-014"], [], [])  # deliberately no CNIC anywhere (40% split)
add("P-006", "person", "عدنان قریشی وحید",
    f"role=repeat cheque/financial fraud offender (CASE-016 closed Untraced from the station's own perspective — the cross-case link to CASE-015 was never made by investigators; ground truth is he's the same person); father's_name=وحید قریشی; cnic={cnic('p6')}; address=ADDR-004",
    "عدنان قریشی وحید", "recurring", "", ["CASE-015", "CASE-016"], [], ["CASE-015"])

# ── Confusable pairs 2 & 3 (cross-case, new this revision) ─────────────────
add("P-CP2A", "person", "بلال احمد وحید", f"role=accused (weapon possession); father's_name=وحید بخش; cnic={cnic('cp2a')}; address=مکان 40، شالیمار، اسلام آباد",
    "بلال احمد وحید", "confusable-pair", "CP-02", ["CASE-002"], [], ["CASE-002"])
add("P-CP2B", "person", "بلال احمد رفیق", f"role=unrelated complainant (harassment, different case/station); father's_name=رفیق حسین; cnic={cnic('cp2b')}; address=مکان 6، کوہسار، اسلام آباد",
    "بلال احمد رفیق", "confusable-pair", "CP-02", ["CASE-017"], [], ["CASE-017"])
add("P-CP3A", "person", "ثنا ملک اکرم", f"role=RTA complainant, CNIC shown (formal FIR field); father's_name=اکرم ملک; cnic={cnic('cp3a')}; address=مکان 11، شاہ زاد ٹاؤن، اسلام آباد",
    "ثنا ملک اکرم", "confusable-pair", "CP-03", ["CASE-019"], [], ["CASE-019"])
add("P-CP3B", "person", "ثنا ملک یوسف",
    "role=unrelated witness (harassment case, informal mention, CNIC deliberately withheld — tests the harder no-CNIC-to-compare fallback path); father's_name=یوسف ملک; cnic=; address=مکان 17، سیکرٹریٹ، اسلام آباد",
    "ثنا ملک یوسف", "confusable-pair", "CP-03", ["CASE-018"], [], [])

# ── Name-variant people (2 more, cross-case; 2 already exist within-case: P-DRY-003 + NV-02 below) ──
add("NV-02", "person", "فریحہ ثاقب انصاری",
    f"role=complainant, name drifts across this ONE case's document chain (Complaint->FIR->CaseDiary->ChargeSheet); father's_name=ثاقب انصاری; cnic={cnic('nv2')}; address=مکان 3، نیلور، اسلام آباد",
    "فریحہ ثاقب انصاری|فریحہ ثاقب|فریحہ صاقب انصاری", "name-variant", "NV-02", ["CASE-006"], [], ["CASE-006"])
add("NV-03", "person", "طارق محمود جاوید",
    f"role=witness, appears across TWO unrelated cases (cross-case, harder variant); father's_name=محمود جاوید; cnic={cnic('nv3')}; address=مکان 25، سبزی منڈی، اسلام آباد",
    "طارق محمود جاوید|طارق محمود|طارق محمد جاوید", "name-variant", "NV-03", ["CASE-011", "CASE-015"], [], ["CASE-011"])
add("NV-04", "person", "زینب اکرم صدیقی",
    f"role=witness, appears across TWO unrelated cases (cross-case, harder variant); father's_name=اکرم صدیقی; cnic={cnic('nv4')}; address=مکان 8، لوہی بھیر، اسلام آباد",
    "زینب اکرم صدیقی|زینب اکرم|زینب اکبر صدیقی", "name-variant", "NV-04", ["CASE-013", "CASE-017"], [], [])

# ── Recurring vehicles (4) + near-miss pairs (2 pairs) ──────────────────────
add("V-001", "vehicle", f"سوزوکی پک اپ، {plate('v1')}", "role=burglary ring transport", "", "recurring", "ORG-002",
    ["CASE-007", "CASE-008", "CASE-009"], [], [])
add("V-002", "vehicle", f"ہونڈا سی ڈی-70، {plate('v2')}", "role=stolen in CASE-010, re-implicated in CASE-011", "",
    "recurring", "", ["CASE-010", "CASE-011"], [], [])
add("V-003", "vehicle", f"ہونڈا سی ڈی-70، {plate('v3')}", "role=P-004's own motorcycle", "", "recurring", "P-004",
    ["CASE-012"], [], [])
add("V-004", "vehicle", f"ٹویوٹا کرولا، {plate('v4')}", "role=P-006's vehicle", "", "recurring", "P-006",
    ["CASE-015", "CASE-016"], [], [])
_p1 = plate("nm1a"); _p1b = _p1[:-1] + str((int(_p1[-1]) + 1) % 10)
add("V-NM1A", "vehicle", f"سوزوکی مہران، {_p1}", "", "", "confusable-pair", "VNM-01", ["CASE-019"], [], [])
add("V-NM1B", "vehicle", f"سوزوکی مہران، {_p1b}", "", "", "confusable-pair", "VNM-01", ["CASE-011"], [], [])
_p2 = plate("nm2a"); _p2b = _p2[:-1] + str((int(_p2[-1]) + 1) % 10)
add("V-NM2A", "vehicle", f"ہونڈا سویفٹ، {_p2}", "", "", "confusable-pair", "VNM-02", ["CASE-003"], [], [])
add("V-NM2B", "vehicle", f"ہونڈا سویفٹ، {_p2b}", "", "", "confusable-pair", "VNM-02", ["CASE-020"], [], [])

# ── Recurring phones (5) ─────────────────────────────────────────────────
add("PH-001", "phone", phone("ph1"), "role=cyber fraud ring number", "", "recurring", "ORG-001",
    ["CASE-004", "CASE-005", "CASE-006"], [], [])
add("PH-002", "phone", phone("ph2"), "role=cyber fraud ring number (second)", "", "recurring", "ORG-001",
    ["CASE-005", "CASE-006"], [], [])
add("PH-003", "phone", phone("ph3"), "role=harassment number, reused against 2 victims", "", "recurring", "HARASS-01",
    ["CASE-017", "CASE-018"], [], [])
add("PH-004", "phone", phone("ph4"), "role=burglary ring coordination number", "", "recurring", "ORG-002",
    ["CASE-007", "CASE-008"], [], [])
add("PH-005", "phone", phone("ph5"), "role=burglary ring coordination number (second)", "", "recurring", "ORG-002",
    ["CASE-008", "CASE-009"], [], [])

# ── Recurring addresses (4) ─────────────────────────────────────────────
add("ADDR-001", "address", "گودام نمبر 3، صنعتی علاقہ، اسلام آباد", "role=burglary ring fence/storage location", "",
    "recurring", "ORG-002", ["CASE-007", "CASE-008", "CASE-009"], [], [])
add("ADDR-002", "address", "کوارٹر 12، مشترکہ رہائش گاہ، سبزی منڈی، اسلام آباد",
    "role=DIFFERENT unrelated people, same boarding-house address - should NOT imply a relationship", "",
    "recurring", "SHARED-ADDR", ["CASE-010", "CASE-013"], [], [])
add("ADDR-003", "address", "مکان 5، محلہ سبزی منڈی، اسلام آباد", "role=P-004's home, same person across his cases", "",
    "recurring", "P-004", ["CASE-010", "CASE-011", "CASE-012"], [], [])
add("ADDR-004", "address", "دکان 8، سیکرٹریٹ مارکیٹ، اسلام آباد", "role=P-006's shop", "", "recurring", "P-006",
    ["CASE-015", "CASE-016"], [], [])

# ── Organizations (2) ────────────────────────────────────────────────────
add("ORG-001", "organization", "سائبر فراڈ گروہ (نامعلوم نیٹ ورک)", "role=cyber fraud ring", "", "recurring", "",
    ["CASE-004", "CASE-005", "CASE-006"], [], [])
add("ORG-002", "organization", "نقب زنی گروہ (وقاص و کامران گروہ)", "role=burglary ring", "", "recurring", "",
    ["CASE-007", "CASE-008", "CASE-009"], [], [])

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS)
    w.writeheader()
    for row in rows:
        w.writerow(row)

n_person = sum(1 for r in rows if r["type"] == "person")
print(f"wrote {len(rows)} entity rows ({n_person} persons) to {OUT}")
