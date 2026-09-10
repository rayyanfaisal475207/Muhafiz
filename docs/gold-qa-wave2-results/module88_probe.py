import asyncio, json
from src.graph import age_client

async def main():
    print("=== A. Person recurrence (raw, no canon fold) ===")
    rows = await age_client.execute_cypher(
        "MATCH (n:Person)-[:BELONGS_TO_CASE]->(c:Case) RETURN n, c",
        columns=["n", "c"])
    per = {}
    disp = {}
    for r in rows:
        np_ = (r.get("n") or {}).get("properties", {}) or {}
        cp = (r.get("c") or {}).get("properties", {}) or {}
        eid, cid = np_.get("entity_id"), cp.get("case_id")
        if not eid or not cid: continue
        per.setdefault(eid, set()).add(cid)
        disp[eid] = np_.get("canonical_name") or np_.get("name") or eid
    rec = {k: sorted(v) for k, v in per.items() if len(v) > 1}
    for k, v in sorted(rec.items(), key=lambda kv: -len(kv[1])):
        print(f"  {k} | {disp[k]} | {len(v)} | {v}")

    print("\n=== B. Incident dates for those cases (OCCURRED_ON) ===")
    drows = await age_client.execute_cypher(
        "MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case) "
        "MATCH (i)-[oe:OCCURRED_ON]->(d:Date) WHERE oe.event_type = 'incident' "
        "RETURN d.date AS incident_date, c.case_id AS case_id",
        columns=["incident_date", "case_id"])
    dmap = {r["case_id"]: r["incident_date"] for r in drows}
    cases_of_interest = sorted({c for v in rec.values() for c in v})
    for c in cases_of_interest:
        print(f"  {c} -> {dmap.get(c)}")

    print("\n=== C. INVOLVED_IN role/arrest_status for recurring persons ===")
    srows = await age_client.execute_cypher(
        "MATCH (p:Person)-[e:INVOLVED_IN]->(i:Incident)-[:BELONGS_TO_CASE]->(c:Case) "
        "RETURN p.entity_id AS pid, c.case_id AS case_id, e.role AS role, "
        "e.arrest_status AS arrest_status",
        columns=["pid", "case_id", "role", "arrest_status"])
    for r in srows:
        if r["pid"] in rec:
            print(f"  {r['pid']} | {disp.get(r['pid'])} | {r['case_id']} | role={r['role']} | arrest={r['arrest_status']}")

    print("\n=== D. criminal_record StructuredRecords ===")
    crows = await age_client.execute_cypher(
        "MATCH (r:StructuredRecord) WHERE r.record_type = 'criminal_record' "
        "RETURN r.subject_full_name AS subject, r.source_case_ref AS case_ref, "
        "r.conviction_status AS conviction_status",
        columns=["subject", "case_ref", "conviction_status"])
    print(f"  total criminal_record rows: {len(crows)}")
    for r in crows:
        ref = str(r.get("case_ref") or "")
        if "891" in ref or "214" in ref:
            print(f"  MATCH-OF-INTEREST: {r}")
    print("  --- all with a conviction-ish status ---")
    for r in crows:
        st = (r.get("conviction_status") or "")
        if any(t in st.lower() for t in ("convict", "acquit", "سزا", "بری", "نمٹ")):
            print(f"    {r}")

asyncio.run(main())
