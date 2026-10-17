"""Import original texts/spans and quarantine legacy fields as candidates.

This does not infer new facts, trust legacy quantities, merge people, enable
the runtime, or change the source database. It is safe to resume by source key.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import UUID

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asyncpg
from dotenv import load_dotenv
from src.evidence.database import _init
from src.evidence.ingestion import put_document,put_span,stable_id


async def import_sources(source,target):
    mapping={}
    documents=await source.fetch("""SELECT d.*,e.text AS original_text,
      ARRAY(SELECT cd.case_id FROM case_documents cd WHERE cd.doc_id=d.doc_id ORDER BY cd.case_id) AS linked_cases
      FROM rebuild_documents d JOIN doc_elements e ON e.doc_id=d.doc_id AND e.element_type='document'
      ORDER BY d.doc_id""")
    for row in documents:
        cases=sorted(set((row["linked_cases"] or [])+([row["case_id"]] if row["case_id"] else [])))
        async with target.transaction():
            doc_id=await put_document(target,source_key="rebuild:"+row["doc_id"],text=row["original_text"],
                title=row["title"],case_ids=cases,is_global=bool(row["is_global"]) and not cases,
                metadata={"source_system":row["source_system"],"original_id":row["doc_id"],"import":"source-text"})
            mapping[row["doc_id"]]=doc_id
            spans=await source.fetch("SELECT char_start,char_end,text FROM doc_spans WHERE doc_id=$1",row["doc_id"])
            for span in spans:
                if row["original_text"][span["char_start"]:span["char_end"]] != span["text"]:
                    raise ValueError("Original span offsets failed validation")
                await put_span(target,doc_id,row["original_text"],span["char_start"],span["char_end"])
    # Reference material outside the rebuild exists as source chunks. Keep
    # each chunk intact with its original identifier; do not invent offsets
    # into an unavailable original document or certify whole-file coverage.
    references=await source.fetch("""SELECT c.* FROM chunk_fulltext c
      WHERE NOT EXISTS(SELECT 1 FROM rebuild_documents d WHERE d.doc_id=c.doc_id)
      ORDER BY c.doc_id,c.chunk_id""")
    for chunk in references:
        metadata=chunk["metadata"] if isinstance(chunk["metadata"],dict) else json.loads(chunk["metadata"] or "{}")
        async with target.transaction():
            await put_document(target,source_key="legacy-chunk:"+chunk["chunk_id"],text=chunk["text"],
                title=chunk["source"] or chunk["doc_id"],case_ids=[chunk["case_id"]] if chunk["case_id"] else [],
                project_id=UUID(chunk["project_id"]) if chunk["project_id"] else None,
                is_global=bool(chunk["is_global"]) and not chunk["case_id"],
                metadata={"original_id":chunk["doc_id"],"original_chunk_id":chunk["chunk_id"],
                          "source_fragment":True,"original_metadata":metadata})
    # Discover importable tables through their provenance column. The contents
    # remain quarantined payloads, not asserted meanings or counting entities.
    tables=await source.fetch("""SELECT c.table_name FROM information_schema.columns c
        JOIN table_roles r ON r.table_name=c.table_name AND r.is_evidence
        WHERE c.table_schema='public' AND c.column_name='span_id'
        AND c.table_name NOT IN ('doc_spans','rebuild_chunks') ORDER BY c.table_name""")
    candidates=0
    for table in tables:
        name=table["table_name"]
        identifier='"'+name.replace('"','""')+'"'
        rows=await source.fetch(f"SELECT to_jsonb(t) AS payload,s.doc_id FROM public.{identifier} t JOIN doc_spans s ON s.span_id=t.span_id")
        async with target.transaction():
            for row in rows:
                if row["doc_id"] not in mapping:
                    continue
                payload=row["payload"] if isinstance(row["payload"],dict) else json.loads(row["payload"])
                key=json.dumps(payload,sort_keys=True,default=str)
                cid=stable_id("legacy",name,key)
                await target.execute("""INSERT INTO evidence.legacy_candidates(candidate_id,document_id,source_table,source_key,payload)
                    VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING""",cid,mapping[row["doc_id"]],name,str(cid),payload)
                candidates+=1
    return {"documents":len(mapping),"reference_chunks":len(references),"legacy_candidates":candidates,"legacy_facts_validated":0}


async def main():
    root=Path(__file__).resolve().parents[1]
    load_dotenv(root/".env")
    parser=argparse.ArgumentParser()
    parser.add_argument("--target-env",type=Path)
    args=parser.parse_args()
    if args.target_env:
        load_dotenv(args.target_env,override=True)
    source=await asyncpg.connect(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://","postgresql://"))
    target=await asyncpg.connect(os.environ["EVIDENCE_INGEST_DATABASE_URL"])
    await _init(target)
    try:
        async with source.transaction(readonly=True,isolation="repeatable_read"):
            print(json.dumps(await import_sources(source,target)))
    finally:
        await source.close();await target.close()


if __name__=="__main__": asyncio.run(main())
