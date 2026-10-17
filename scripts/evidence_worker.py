"""Durable ingestion and run recovery. Start separately from the web server.

Each attempt is bounded to ten minutes. A crash leaves a renewable lease;
completed source work is checkpointed, and cancellation is polled per document.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import UUID

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from src.evidence import database
from src.evidence.conversation import reauthorize
from src.evidence.ingestion import extract_document
from src.evidence.model import ModelUnavailable,ModelOutputError
from src.evidence.types import Scope,QueryOperation

_last_projection_cleanup=0


async def expire_unrecoverable(conn):
    """Terminalize exhausted leases so a session can accept a new request."""
    await conn.execute('''UPDATE evidence.runs SET status='unavailable',lease_until=NULL,updated_at=now(),
        response=jsonb_build_object('run_id',run_id,'status','unavailable','text','The request exhausted its recovery attempts. Please retry.','results','[]'::jsonb)
        WHERE attempts>=3 AND NOT cancel_requested AND
          (status='pending' OR status='running' AND lease_until<now())''')
    exhausted=await conn.fetch('''UPDATE evidence.discovery_jobs SET status='failed',lease_until=NULL,updated_at=now()
        WHERE failed_attempts>=3 AND (status='pending' OR status='running' AND lease_until<now()) RETURNING run_id''')
    for row in exhausted:
        await conn.execute("UPDATE evidence.runs SET status='pending',lease_until=NULL WHERE run_id=$1 AND status IN ('pending','running') AND attempts<3 AND NOT cancel_requested",row['run_id'])


async def work_once(gateway):
    global _last_projection_cleanup
    pool=await database.pool("ingest")
    async with pool.acquire() as conn:
        if time.monotonic()-_last_projection_cleanup>60:
            await conn.execute('DELETE FROM evidence.projections WHERE expires_at<=now()')
            _last_projection_cleanup=time.monotonic()
        await expire_unrecoverable(conn)
        async with conn.transaction():
            job=await conn.fetchrow("""UPDATE evidence.discovery_jobs SET failed_attempts=failed_attempts+CASE WHEN status='running' THEN 1 ELSE 0 END,
              status='running',attempts=attempts+1,
              lease_until=now()+interval '620 seconds',updated_at=now() WHERE job_id=(
                SELECT j.job_id FROM evidence.discovery_jobs j JOIN evidence.runs r USING(run_id)
                WHERE j.failed_attempts<3 AND NOT r.cancel_requested AND (j.status='pending' OR (j.status='running' AND j.lease_until<now()))
                ORDER BY j.updated_at FOR UPDATE OF j SKIP LOCKED LIMIT 1) RETURNING *""")
    if job:
        scope=Scope.model_validate(job["scope"])
        requested=(job['progress'] or {}).get('requested_outputs',[])
        completed=[]; failures=[]; docs=[]; deadline=time.monotonic()+600
        try:
            await reauthorize(gateway,scope)
            async with database.connection(scope) as conn:
                if requested:
                    from src.evidence.store import EvidenceStore
                    docs=await EvidenceStore(conn,scope).discovery_population([QueryOperation.model_validate(op) for op in requested])
                else:
                    docs=await conn.fetch("""SELECT d.document_id FROM evidence.documents d
                      WHERE NOT EXISTS(SELECT 1 FROM evidence.documents n WHERE n.supersedes=d.document_id)
                      ORDER BY d.document_id""")
            frozen = (job['progress'] or {}).get('population')
            if frozen:
                accessible = {str(d['document_id']) for d in docs}
                if not set(frozen) <= accessible:
                    raise ValueError('Frozen discovery population changed or is no longer accessible')
                docs = [{'document_id':UUID(key)} for key in frozen]
            population = [str(d['document_id']) for d in docs]
            previous=set((job["progress"] or {}).get("completed",[]))
            async with pool.acquire() as conn:
                changed = await conn.execute("""UPDATE evidence.discovery_jobs SET progress=$2,updated_at=now()
                    WHERE job_id=$1 AND attempts=$3 AND status='running'""",job['job_id'],
                    {'population':population,'completed':sorted(previous),'total':len(population),'requested_outputs':requested},job['attempts'])
                if changed == 'UPDATE 0': raise ValueError('Discovery attempt lost its lease')
            for item in docs:
                if str(item["document_id"]) in previous:
                    completed.append(str(item["document_id"]))
                    continue
                if time.monotonic()>=deadline:
                    break
                async with pool.acquire() as conn:
                    cancel=await conn.fetchval("SELECT cancel_requested FROM evidence.runs WHERE run_id=$1",job["run_id"])
                    if cancel:
                        break
                    await reauthorize(gateway,scope)
                    async with conn.transaction():
                        async with asyncio.timeout(max(.01,deadline-time.monotonic())):
                            result=await extract_document(conn,item["document_id"],requested=job["terms"],deadline=deadline)
                        await reauthorize(gateway,scope)
                        ownership = await conn.fetchrow('''SELECT j.attempts,j.status,r.cancel_requested
                            FROM evidence.discovery_jobs j JOIN evidence.runs r USING(run_id)
                            WHERE j.job_id=$1 FOR UPDATE OF j,r''',job['job_id'])
                        if (not ownership or ownership['attempts'] != job['attempts']
                                or ownership['status'] != 'running' or ownership['cancel_requested']):
                            raise ValueError('Discovery publication was cancelled or superseded')
                    completed.append(str(item["document_id"]))
                    changed = await conn.execute("UPDATE evidence.discovery_jobs SET progress=$2,updated_at=now() WHERE job_id=$1 AND attempts=$3 AND status='running'",job["job_id"],
                        {"completed":completed,"population":population,"total":len(docs),"last_result":result,'requested_outputs':requested},job['attempts'])
                    if changed == 'UPDATE 0': raise ValueError('Discovery attempt lost its lease')
        except (ModelUnavailable,ModelOutputError,TimeoutError) as exc:
            failures.append(type(exc).__name__)
        except Exception as exc:
            failures.append(type(exc).__name__)
        async with pool.acquire() as conn:
            cancel=await conn.fetchval("SELECT cancel_requested FROM evidence.runs WHERE run_id=$1",job["run_id"])
            status="cancelled" if cancel else "failed" if failures else "complete" if len(completed)==len(docs) else "partial"
            failures_count = job['failed_attempts'] + int(bool(failures))
            if status == 'partial' or status == 'failed' and failures_count < 3: status = 'pending'
            changed = await conn.execute("UPDATE evidence.discovery_jobs SET status=$2,progress=$3,failed_attempts=$5,lease_until=NULL,updated_at=now() WHERE job_id=$1 AND attempts=$4 AND status='running'",
                job["job_id"],status,{"completed":completed,"population":[str(d['document_id']) for d in docs],"failures":failures,'requested_outputs':requested},job['attempts'],failures_count)
            if changed == 'UPDATE 0' or status == 'pending': return True
            # The second answer attempt reports remaining gaps; it cannot queue
            # the same discovery forever. Terminal failure is visible to users.
            await conn.execute("""UPDATE evidence.runs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'pending' END,
              progress=$2,lease_until=NULL,updated_at=now() WHERE run_id=$1""",job["run_id"],{"discovery":status,"failures":failures})
        return True
    async with pool.acquire() as conn:
        row=await conn.fetchrow("""SELECT r.run_id,r.scope FROM evidence.runs r
          WHERE NOT r.cancel_requested AND r.attempts<3 AND (r.status='pending' OR r.status='running' AND r.lease_until<now())
          AND NOT EXISTS(SELECT 1 FROM evidence.discovery_jobs j WHERE j.run_id=r.run_id AND j.status IN ('pending','running'))
          ORDER BY r.created_at LIMIT 1""")
    if row:
        scope=Scope.model_validate(row["scope"])
        try:
            await reauthorize(gateway,scope)
        except Exception:
            async with pool.acquire() as conn:
                await conn.execute("UPDATE evidence.runs SET status='cancelled',cancel_requested=true WHERE run_id=$1",row["run_id"])
            return True
        from src.evidence.service import perform
        await perform(row["run_id"],scope,gateway)
        return True
    # Newly ingested sources use the same extractor as query-time discovery.
    async with pool.acquire() as conn:
        job=await conn.fetchrow("""UPDATE evidence.ingestion_jobs SET status='running',attempts=attempts+1,
          lease_until=now()+interval '620 seconds',updated_at=now() WHERE document_id=(
          SELECT document_id FROM evidence.ingestion_jobs WHERE attempts<3 AND
          (status IN ('pending','failed') OR status='running' AND lease_until<now()) ORDER BY updated_at
          FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""")
        if not job:return False
        try:
            async with conn.transaction():
                active=await conn.fetchval("SELECT NOT source_metadata @> '{\"attachment_revoked\":true}'::jsonb FROM evidence.documents WHERE document_id=$1",job['document_id'])
                if not active:raise ValueError('Source was revoked before ingestion')
                async with asyncio.timeout(600):
                    result=await extract_document(conn,job["document_id"],deadline=time.monotonic()+600)
                active=await conn.fetchval("SELECT NOT source_metadata @> '{\"attachment_revoked\":true}'::jsonb FROM evidence.documents WHERE document_id=$1",job['document_id'])
                if not active:raise ValueError('Source was revoked during ingestion')
                current = await conn.fetchrow('SELECT attempts,status FROM evidence.ingestion_jobs WHERE document_id=$1 FOR UPDATE',job['document_id'])
                if not current or current['attempts'] != job['attempts'] or current['status'] != 'running':
                    raise ValueError('Ingestion attempt no longer owns its work')
            status="complete"
        except Exception as exc:
            status="failed";result={"failure":type(exc).__name__}
        await conn.execute("UPDATE evidence.ingestion_jobs SET status=$2,progress=$3,lease_until=NULL,updated_at=now() WHERE document_id=$1 AND attempts=$4 AND status='running'",
            job["document_id"],status,result,job['attempts'])
    return True


async def main():
    load_dotenv(Path(__file__).resolve().parents[1]/".env")
    parser=argparse.ArgumentParser()
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--env",type=Path)
    parser.add_argument("--extract-document",type=UUID)
    parser.add_argument("--index-document",type=UUID)
    args=parser.parse_args()
    if args.env: load_dotenv(args.env,override=True)
    try:
        if args.extract_document or args.index_document:
            pool=await database.pool("ingest")
            async with pool.acquire() as conn:
                if args.extract_document:
                    async with conn.transaction():
                        print(json.dumps(await extract_document(conn,args.extract_document,deadline=time.monotonic()+600)))
                else:
                    from src.evidence.vectors import index_spans
                    print(json.dumps({"indexed":await index_spans(conn,args.index_document)}))
            return
        from src.data_gateway import get_gateway
        gateway=await get_gateway()
        while True:
            worked=await work_once(gateway)
            if args.once: break
            if not worked: await asyncio.sleep(3)
    finally:
        await database.close()


if __name__=="__main__": asyncio.run(main())
