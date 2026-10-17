"""Explicit operator adjudication with persistent justification.

Never auto-merges a name match. Assertions and concept versions are retained.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
from uuid import UUID

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from src.evidence import database


async def main():
    load_dotenv(Path(__file__).resolve().parents[1]/".env")
    parser=argparse.ArgumentParser()
    parser.add_argument("command",choices=["list-candidates","confirm-distinct","confirm-same","reject-link","dispute-assertion"])
    parser.add_argument("--entity",type=UUID)
    parser.add_argument("--other",type=UUID)
    parser.add_argument("--assertion",type=UUID)
    parser.add_argument("--reviewer",type=UUID)
    parser.add_argument("--reason")
    parser.add_argument("--env",type=Path)
    args=parser.parse_args()
    if args.env: load_dotenv(args.env,override=True)
    if args.command!="list-candidates" and (not args.reviewer or not args.reason):
        parser.error("adjudication requires --reviewer and --reason")
    pool=await database.pool("ingest")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if args.command=="list-candidates":
                    import json
                    rows=await conn.fetch("SELECT entity_id,document_id,label FROM evidence.entities WHERE identity_status='unresolved' ORDER BY document_id,entity_id LIMIT 100")
                    print(json.dumps([dict(r) for r in rows],ensure_ascii=False,default=str));return
                if args.command=="dispute-assertion":
                    if not args.assertion: parser.error("--assertion required")
                    result=await conn.execute("UPDATE evidence.assertions SET status='disputed',validation=validation||$2::jsonb WHERE assertion_id=$1",
                        args.assertion,{"reviewer":str(args.reviewer),"reason":args.reason})
                elif args.command=="confirm-distinct":
                    if not args.entity: parser.error("--entity required")
                    result=await conn.execute("""UPDATE evidence.entities SET identity_status='confirmed',canonical_id=entity_id,
                       identifiers=identifiers||$2::jsonb WHERE entity_id=$1 AND canonical_id IS NULL""",args.entity,
                       {"reviewer":str(args.reviewer),"reason":args.reason})
                else:
                    if not args.entity or not args.other or args.entity==args.other:
                        parser.error("two different --entity and --other ids required")
                    left,right=sorted([args.entity,args.other])
                    entities=await conn.fetch("SELECT entity_id,concept_id,canonical_id FROM evidence.entities WHERE entity_id=ANY($1::uuid[]) FOR UPDATE",[left,right])
                    if len(entities)!=2 or len({r["concept_id"] for r in entities})!=1:
                        raise ValueError("Both entities must exist and have the same reviewed concept")
                    status="confirmed" if args.command=="confirm-same" else "rejected"
                    roots={r["canonical_id"] or r["entity_id"] for r in entities}
                    if status=="rejected" and len(roots)==1:
                        raise ValueError("Already merged identities require an explicit reviewed split; rejecting this link would not undo the merge")
                    await conn.execute("""INSERT INTO evidence.identity_links(left_id,right_id,status,method,justification,reviewed_by)
                      VALUES($1,$2,$3,'manual',$4,$5) ON CONFLICT(left_id,right_id) DO UPDATE
                      SET status=EXCLUDED.status,justification=EXCLUDED.justification,reviewed_by=EXCLUDED.reviewed_by,recorded_at=now()""",
                      left,right,status,{"reason":args.reason},args.reviewer)
                    if status=="confirmed":
                        canonical=min(roots)
                        await conn.execute("""UPDATE evidence.entities SET canonical_id=$2,identity_status='confirmed'
                          WHERE entity_id=ANY($1::uuid[]) OR canonical_id=ANY($1::uuid[])""",list(roots|{left,right}),canonical)
                    result="review recorded"
                print(result)
    finally: await database.close()


if __name__=="__main__": asyncio.run(main())
