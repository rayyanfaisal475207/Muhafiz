"""Propose, independently review/publish, and import learned source bindings."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
from src.evidence import database
from src.evidence.catalog import SchemaProposal, import_bound_record, propose_schema, publish_bindings
from src.evidence.ingestion import put_document
from src.evidence.model import LocalModel


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',type=Path,default=ROOT/'.env',help='Explicit configuration file; use .env.evidence-test for isolated imports')
    sub = parser.add_subparsers(dest='command',required=True)
    propose = sub.add_parser('propose')
    propose.add_argument('--records',type=Path,required=True)
    propose.add_argument('--documentation',type=Path,required=True)
    propose.add_argument('--output',type=Path,required=True)
    for command in ['publish','import']:
        cmd = sub.add_parser(command)
        cmd.add_argument('--proposal',type=Path,required=True)
        cmd.add_argument('--namespace',required=True)
        cmd.add_argument('--case',action='append',required=True)
        if command == 'publish': cmd.add_argument('--review',type=Path,required=True)
        else:
            cmd.add_argument('--records',type=Path,required=True)
            cmd.add_argument('--id-pointer',required=True)
    args = parser.parse_args()
    load_dotenv(args.env,override=True)
    if args.command == 'propose':
        records = json.loads(args.records.read_text(encoding='utf-8'))
        if not isinstance(records,list): raise ValueError('Records must be a JSON array')
        docs = args.documentation.read_text(encoding='utf-8')
        profile,proposal = await propose_schema(LocalModel(priority='background'),records,docs,deadline=time.monotonic()+90)
        # Exclusive creation preserves previous proposals and evidence.
        with args.output.open('x',encoding='utf-8') as output:
            json.dump({'profile':profile,'proposal':proposal.model_dump(),'documentation':docs},output,ensure_ascii=False,indent=2)
        print('Candidate proposal saved; independent review is required before publication.')
        return
    bundle = json.loads(args.proposal.read_text(encoding='utf-8'))
    proposal = SchemaProposal.model_validate(bundle['proposal'])
    pool = await database.pool('ingest')
    try:
        async with pool.acquire() as conn:
            if args.command == 'publish':
                review = json.loads(args.review.read_text(encoding='utf-8'))
                async with conn.transaction():
                    doc = await put_document(conn,source_key=f"catalog-documentation:{args.namespace}",
                        text=bundle['documentation'],title=f'{args.namespace} source definitions',case_ids=args.case)
                    result = await publish_bindings(conn,document_id=doc,namespace=args.namespace,
                        profile=bundle['profile'],proposal=proposal,documentation=bundle['documentation'],
                        reviewer=review['reviewer'],decisions=review['decisions'])
                print(json.dumps(result,default=str))
            else:
                from src.evidence.catalog import values_at
                bindings = await conn.fetch('''SELECT b.* FROM evidence.source_bindings b
                    JOIN evidence.source_profiles p USING(profile_id) WHERE b.source_namespace=$1
                    AND b.schema_fingerprint=$2 AND b.status='validated' AND p.status='validated'
                    AND NOT EXISTS(SELECT 1 FROM evidence.documents d WHERE d.supersedes=b.document_id)''',
                    args.namespace,bundle['profile']['schema_fingerprint'])
                if not bindings: raise ValueError('No current reviewed bindings; profile/review the schema first')
                records = json.loads(args.records.read_text(encoding='utf-8'))
                totals = {'records':0,'assertions':0,'missing':0,'failed':0}
                seen = set()
                for record in records:
                    ids = values_at(record,args.id_pointer)
                    if len(ids) != 1 or ids[0][0] is None: raise ValueError('A unique source record identifier is required')
                    key = str(ids[0][0])
                    if key in seen: raise ValueError('Duplicate source record identifier in import batch')
                    seen.add(key)
                    async with conn.transaction():
                        result = await import_bound_record(conn,namespace=args.namespace,record_key=key,record=record,
                            schema_fingerprint=bundle['profile']['schema_fingerprint'],bindings=bindings,case_ids=args.case)
                    totals['records'] += 1
                    for name in ['assertions','missing','failed']: totals[name] += result[name]
                print(json.dumps(totals))
    finally: await database.close()


if __name__ == '__main__': asyncio.run(main())
