"""Consistent evidence backup and guarded restoration into an empty isolated DB.

Backups contain private case/session content. Keep them in private local storage.
This command never drops tables, resets a database, or starts background work.
"""
import argparse
import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import asyncpg
from dotenv import load_dotenv

TABLES=('documents','spans','concepts','entities','concept_revisions','assertions','identity_links',
        'coverage','legacy_candidates','extraction_runs','source_profiles','source_bindings',
        'runs','conversations','artifacts','discovery_jobs','ingestion_jobs')


def quoted(name):
    if name not in TABLES: raise ValueError('Unknown infrastructure table')
    return 'evidence."'+name+'"'


async def snapshot(conn,directory):
    directory.mkdir(parents=True,exist_ok=False)
    manifest={'format_version':1,'tables':{},'contains_private_evidence':True}
    async with conn.transaction(readonly=True,isolation='repeatable_read'):
        for table in TABLES:
            if not await conn.fetchval('SELECT to_regclass($1)',quoted(table)): continue
            columns=await conn.fetch('''SELECT column_name FROM information_schema.columns
                WHERE table_schema='evidence' AND table_name=$1 ORDER BY ordinal_position''',table)
            path=directory/(table+'.jsonl.gz')
            order=' ORDER BY recorded_at,document_id' if table=='documents' else ''
            count=0
            with gzip.open(path,'wt',encoding='utf-8') as output:
                async for row in conn.cursor('SELECT to_jsonb(t)::text AS row FROM '+quoted(table)+' t'+order):
                    output.write(row['row']+'\n');count+=1
            manifest['tables'][table]={'rows':count,'columns':[r['column_name'] for r in columns],
                                     'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        manifest['captured_at']=str(await conn.fetchval('SELECT transaction_timestamp()'))
    (directory/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return manifest


async def restore(conn,directory):
    if not (await conn.fetchval('SELECT current_database()')).startswith('muhafiz_evidence_test_'):
        raise RuntimeError('Restoration is restricted to the isolated test database namespace')
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    if manifest['format_version']!=1: raise ValueError('Unknown backup format')
    async with conn.transaction():
        for table in TABLES:
            if await conn.fetchval('SELECT to_regclass($1)',quoted(table)):
                if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM '+quoted(table)+')'):
                    raise RuntimeError('Restore target must be empty; refusing to overwrite existing evidence')
        for table in TABLES:
            item=manifest['tables'].get(table)
            if item is None: continue
            path=directory/(table+'.jsonl.gz')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
                raise ValueError('Backup checksum failed: '+table)
            known={r['column_name'] for r in await conn.fetch('''SELECT column_name FROM information_schema.columns
                WHERE table_schema='evidence' AND table_name=$1''',table)}
            if not set(item['columns']) <= known: raise ValueError('Target schema is incompatible: '+table)
            columns=','.join('"'+name.replace('"','""')+'"' for name in item['columns'])
            sql='INSERT INTO '+quoted(table)+'('+columns+') SELECT '+columns+' FROM jsonb_populate_record(NULL::'+quoted(table)+',$1::jsonb)'
            count=0
            with gzip.open(path,'rt',encoding='utf-8') as source:
                for line in source:
                    await conn.execute(sql,line.strip());count+=1
            if count!=item['rows']: raise ValueError('Restored row count differs: '+table)
    return {'tables':len(manifest['tables']),'rows':sum(item['rows'] for item in manifest['tables'].values()),'restore':'verified'}


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['snapshot','restore'])
    parser.add_argument('directory',type=Path)
    parser.add_argument('--env',type=Path,default=ROOT/'.env')
    parser.add_argument('--dsn-env',default='EVIDENCE_INGEST_DATABASE_URL')
    args=parser.parse_args()
    load_dotenv(args.env,override=True)
    dsn=os.environ[args.dsn_env].replace('postgresql+asyncpg://','postgresql://')
    if args.command=='restore' and not urlsplit(dsn).path.startswith('/muhafiz_evidence_test_'):
        raise RuntimeError('Refusing a restore outside the isolated test namespace')
    conn=await asyncpg.connect(dsn,timeout=10)
    try:
        result=await snapshot(conn,args.directory) if args.command=='snapshot' else await restore(conn,args.directory)
        print(json.dumps({k:v for k,v in result.items() if k!='tables'}|{'tables':len(result['tables']) if isinstance(result['tables'],dict) else result['tables']}))
    finally: await conn.close()


if __name__=='__main__': asyncio.run(main())
