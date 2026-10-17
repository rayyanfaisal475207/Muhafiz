"""Snapshot, migrate, or provision an isolated acceptance database.

Never enables the runtime or overwrites an existing database. Credentials are
written only to ignored .env files, never printed. Run with the project venv.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit,urlunsplit,quote
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asyncpg
from dotenv import dotenv_values,load_dotenv

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT/"migrations"/"078_evidence_runtime.sql"


def replace_dsn(url, *, database=None, username=None, password=None):
    parsed = urlsplit(url.replace("postgresql+asyncpg://","postgresql://"))
    netloc = parsed.netloc
    if username is not None:
        host = parsed.hostname or "localhost"
        if ":" in host:
            host = f"[{host}]"
        netloc = f"{quote(username,safe='')}:{quote(password or '',safe='')}@{host}:{parsed.port or 5432}"
    return urlunsplit((parsed.scheme,netloc,"/"+database if database else parsed.path,parsed.query,""))


async def snapshot():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://","postgresql://")
    destination = ROOT/"data"/"evidence_snapshots"/datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    destination.mkdir(parents=True,exist_ok=False)
    manifest = {"created_at":datetime.now(timezone.utc).isoformat(),"tables":{}}
    conn = await asyncpg.connect(url)
    try:
        async with conn.transaction(readonly=True,isolation="repeatable_read"):
            tables = await conn.fetch("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind='r'
                AND (c.relname IN (SELECT table_name FROM table_roles WHERE is_evidence)
                     OR c.relname IN ('doc_elements','rebuild_documents','rebuild_chunks','cases','documents','document_chunks','chunk_fulltext'))
                ORDER BY c.relname""")
            for row in tables:
                name = row["relname"]
                safe = '"'+name.replace('"','""')+'"'
                count = 0
                path = destination/(name+".jsonl.gz")
                with gzip.open(path,"wt",encoding="utf-8") as out:
                    async for record in conn.cursor("SELECT * FROM public."+safe):
                        out.write(json.dumps(dict(record),ensure_ascii=False,default=str)+"\n")
                        count += 1
                manifest["tables"][name] = {"rows":count,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
    finally:
        await conn.close()
    # Original files are copied alongside their hashes, not just named in a report.
    import shutil
    source_root = ROOT/"Muhafiz_Knowledge_Base"
    shutil.copytree(source_root,destination/"source_files")
    manifest["source_files"] = {str(p.relative_to(source_root)):hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in source_root.rglob("*") if p.is_file()}
    (destination/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    print(json.dumps({"snapshot":str(destination),"tables":len(manifest["tables"])}))


async def migrate(url):
    conn = await asyncpg.connect(url)
    try:
        if await conn.fetchval("SELECT to_regnamespace('evidence') IS NOT NULL"):
            raise RuntimeError("evidence schema already exists; refusing to overwrite it")
        await conn.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await conn.close()


async def test_database(admin_url,configuration=None):
    path = configuration or ROOT/".env.evidence-test"
    if path.exists():
        raise RuntimeError('Refusing to overwrite existing isolated database credentials; select a new --configuration path')
    if not path.parent.exists():
        raise RuntimeError('Configuration parent directory must exist')
    suffix = uuid4().hex[:12]
    name = "muhafiz_evidence_test_"+suffix
    reader,writer = "ev_test_reader_"+suffix,"ev_test_writer_"+suffix
    password = secrets.token_hex(24)
    admin = await asyncpg.connect(admin_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        await admin.execute(f"CREATE ROLE {reader} LOGIN PASSWORD '{password}'")
        await admin.execute(f"CREATE ROLE {writer} LOGIN PASSWORD '{password}'")
        await admin.execute(f"GRANT muhafiz_evidence_reader TO {reader}") if await admin.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='muhafiz_evidence_reader')") else None
    finally:
        await admin.close()
    url = replace_dsn(admin_url,database=name)
    await migrate(url)
    admin = await asyncpg.connect(admin_url)
    try:
        await admin.execute(f"GRANT muhafiz_evidence_reader TO {reader}")
        await admin.execute(f"GRANT muhafiz_evidence_ingestor TO {writer}")
    finally:
        await admin.close()
    values = {"EVIDENCE_TEST_ADMIN_URL":url,
              "EVIDENCE_QUERY_DATABASE_URL":replace_dsn(url,username=reader,password=password),
              "EVIDENCE_INGEST_DATABASE_URL":replace_dsn(url,username=writer,password=password)}
    with path.open('x',encoding='utf-8') as output:
        output.write("\n".join(f"{k}={v}" for k,v in values.items())+"\n")
    print(json.dumps({"database":name,"configuration":path.name,"migration":"passed"}))


async def main():
    load_dotenv(ROOT/".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("command",choices=["snapshot","migrate","create-test"])
    parser.add_argument("--admin-env",default="EVIDENCE_ADMIN_DATABASE_URL")
    parser.add_argument('--configuration',type=Path,help='New private credentials file for create-test; existing files are never overwritten')
    args = parser.parse_args()
    if args.command == "snapshot":
        await snapshot()
    else:
        url = os.getenv(args.admin_env)
        if not url:
            raise SystemExit(f"Set {args.admin_env}; admin credentials have no runtime fallback")
        if args.command == "migrate":
            await migrate(url)
            print("Evidence migration applied; runtime flag unchanged.")
        else:
            await test_database(url,args.configuration)


if __name__ == "__main__":
    asyncio.run(main())
