"""Durable request lifecycle shared by chat, JSON and streaming adapters."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import logging
import time
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from . import database
from .attachments import ingest_session_attachments
from .conversation import prepare, reauthorize
from .reports import create_report
from .runtime_registry import build_runtime as Runtime
from .store import ScopedStore
from .types import Answer, ConversationState, Scope

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()


async def shutdown():
    tasks=list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks,return_exceptions=True)
    await database.close()


def _until(deadline):
    """Bound this step by `deadline`, or not at all when there is none.

    `asyncio.timeout(max(.01, deadline - time.monotonic()))` cannot be
    written where the deadline may be None, and after "unset means
    unbounded" became the rule, None is the DEFAULT. MEASURED: the very
    first question asked through the web UI returned HTTP 500 --
    `unsupported operand type(s) for -: 'NoneType' and 'float'` -- while
    every command-line run kept passing, because those call the runtime
    directly and never cross this path.

    So the None case gets a context manager of its own rather than
    arithmetic that assumes a number.
    """
    if deadline is None:
        return contextlib.nullcontext()
    return asyncio.timeout(max(.01, deadline - time.monotonic()))


def _run_deadline():
    """The configured bound on foreground work, or NO BOUND when unset.

    This read the setting and then threw it away: `min(seconds, 90)` capped
    every value at ninety seconds and `if seconds > 0 else 90` turned the
    "no limit" value into the limit. The setting could not be raised and
    could not be switched off, whatever it said.

    MEASURED: a planning request refused 413 waits for the per-minute token
    allowance to refill, and the clamp expired mid-wait -- so a run that was
    about to succeed reported "the request reached its deadline" instead.
    Two of the seven verified questions failed that way while the same
    questions answered correctly when run one at a time.

    So: unset or zero means unbounded, and a positive value is honoured as
    given. A run is bounded by the work it has left, not by a stopwatch.
    """
    seconds = float(os.getenv("EVIDENCE_RUN_DEADLINE_SECONDS", "0") or 0)
    return time.monotonic() + seconds if seconds > 0 else None


def sse(event):
    return "data: " + json.dumps(event,ensure_ascii=False,default=str) + "\n\n"


async def get_run(scope,run_id):
    async with database.connection(scope) as conn:
        row = await conn.fetchrow("SELECT * FROM evidence.runs WHERE run_id=$1",run_id)
    if row is None:
        raise HTTPException(404,"Run not found")
    original = Scope.model_validate(row["scope"])
    if original.user_id != scope.user_id or original.session_id != scope.session_id:
        raise HTTPException(403,"Not authorized to access this run")
    if (not set(original.case_ids) <= set(scope.case_ids) or original.project_id != scope.project_id
            or original.include_global and not scope.include_global):
        raise HTTPException(403,"Access to the run's evidence has changed")
    return dict(row)


async def start(payload,user,gateway,*,deadline=None):
    deadline=deadline if deadline is not None else _run_deadline()
    prepared = await prepare(gateway,user,payload)
    scope = prepared.scope
    run_id = uuid4()
    try:
        await ingest_session_attachments(scope,prepared.attachments)
        async with database.connection(scope,write=True) as conn:
            stored = await conn.fetchval("SELECT state FROM evidence.conversations WHERE session_id=$1",scope.session_id)
            state = ConversationState.model_validate(stored) if stored else ConversationState(scope_fingerprint=scope.fingerprint())
            request_state = {"conversation":state.model_dump(mode="json"),"history":prepared.history,
                             "preference":prepared.profile.get("preferred_language","auto"),
                             "attachment_ids":[a['attachment_id'] for a in prepared.attachments]}
            await conn.execute("""INSERT INTO evidence.runs(run_id,user_id,session_id,scope,question,state)
                                VALUES($1,$2,$3,$4,$5,$6)""",run_id,scope.user_id,scope.session_id,
                                scope.model_dump(mode="json"),payload.message,request_state)
    except Exception as exc:
        import asyncpg
        if isinstance(exc,asyncpg.UniqueViolationError):
            raise HTTPException(409,"A request is already active in this conversation") from exc
        logger.exception("Evidence request initialization failed")
        raise HTTPException(503,"The evidence runtime is not ready") from exc
    try:
        await gateway.save_message(str(scope.session_id),"user",payload.message,
                                   degradation_trace={"evidence_run_id":str(run_id)})
    except asyncio.CancelledError:
        # Preparation expired after the durable insert. Release the session
        # lock instead of letting a worker answer an unacknowledged request.
        async with database.connection(scope,write=True) as conn:
            await conn.execute("UPDATE evidence.runs SET status='cancelled',cancel_requested=true,updated_at=now() WHERE run_id=$1 AND status='pending'",run_id)
        raise
    except Exception:
        async with database.connection(scope,write=True) as conn:
            await conn.execute("UPDATE evidence.runs SET status='unavailable',updated_at=now() WHERE run_id=$1",run_id)
        raise HTTPException(503,"The conversation could not be saved")
    task = asyncio.create_task(perform(run_id,scope,gateway,deadline=deadline))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run_id,scope,task


async def perform(run_id,scope,gateway,*,deadline=None):
    async with database.connection(scope,write=True) as conn:
        row = await conn.fetchrow("""UPDATE evidence.runs SET status='running',attempts=attempts+1,
          lease_until=now()+interval '100 seconds',updated_at=now() WHERE run_id=$1
          AND NOT cancel_requested AND attempts<3 AND (status='pending' OR (status='running' AND lease_until<now()))
          AND NOT EXISTS(SELECT 1 FROM evidence.discovery_jobs j WHERE j.run_id=evidence.runs.run_id AND j.status IN ('pending','running'))
          RETURNING *""",run_id)
    if not row:
        return
    payload = row["state"]
    state = ConversationState.model_validate(payload["conversation"])

    async def cancelled():
        async with database.connection(scope) as conn:
            current = await conn.fetchrow("SELECT cancel_requested,attempts,status FROM evidence.runs WHERE run_id=$1",run_id)
            return not current or current['cancel_requested'] or current['attempts'] != row['attempts'] or current['status'] != 'running'

    async def progress(value):
        async with database.connection(scope,write=True) as conn:
            await conn.execute("UPDATE evidence.runs SET progress=$2,updated_at=now() WHERE run_id=$1 AND attempts=$3 AND status='running'",
                               run_id,value,row['attempts'])

    async def heartbeat():
        while True:
            await asyncio.sleep(20)
            async with database.connection(scope,write=True) as conn:
                updated = await conn.execute("""UPDATE evidence.runs SET lease_until=now()+interval '100 seconds'
                    WHERE run_id=$1 AND attempts=$2 AND status='running' AND NOT cancel_requested""",run_id,row['attempts'])
                if updated == 'UPDATE 0': return

    heartbeat_task = asyncio.create_task(heartbeat())
    queued_terms = []
    deadline = deadline if deadline is not None else _run_deadline()
    runtime_task=None;cancel_task=None

    try:
        store = ScopedStore(scope)
        async def watch_cancellation():
            while True:
                await asyncio.sleep(.5)
                if await cancelled():return
        runtime_task=asyncio.create_task(Runtime(store).run(run_id,row["question"],scope,state,payload.get("history",[]),
            payload.get("preference","auto"),deadline=deadline,progress=progress,cancelled=cancelled))
        cancel_task=asyncio.create_task(watch_cancellation())
        done,_=await asyncio.wait({runtime_task,cancel_task},return_when=asyncio.FIRST_COMPLETED)
        if cancel_task in done:
            await cancel_task  # Do not mislabel a watcher/database failure as cancellation.
            runtime_task.cancel()
            await asyncio.gather(runtime_task,return_exceptions=True)
            answer=Answer(run_id=run_id,status='cancelled',text='Request cancelled.')
        else:answer=await runtime_task
        if await cancelled():
            answer = Answer(run_id=run_id,status="cancelled",text="Request cancelled.")
        terms = answer.plan.discovery_terms if answer.plan else []
        if answer.status not in {"cancelled", "unavailable", "clarification"} and (terms or answer.status == "not_extracted") and row["attempts"] < 2:
            queued_terms = terms or [row['question']]
            answer.status = "pending"
            answer.text += "\n\n" + ({"ur":"مزید ماخذ کی جانچ قطار میں شامل ہے۔", "roman_ur":"Mazeed source review queue mein hai."}.get(
                answer.plan.language if answer.plan else "en","Further source review is queued."))
        await reauthorize(gateway,scope)
        async with _until(deadline):
            answer = await create_report(answer,scope)
    except asyncio.CancelledError:
        # A process shutdown leaves a durable lease that the worker can recover.
        raise
    except Exception:
        logger.exception("Evidence run %s failed",run_id)
        answer = Answer(run_id=run_id,status="unavailable",text="The evidence service could not complete this request. Please try again.")
        queued_terms = []
    finally:
        heartbeat_task.cancel()
        cleanup=[heartbeat_task]+[task for task in (runtime_task,cancel_task) if task is not None]
        for task in cleanup:task.cancel()
        await asyncio.gather(*cleanup,return_exceptions=True)
    next_state = ConversationState(scope_fingerprint=scope.fingerprint(),plan=answer.plan,
                  result_ids=[r.result_id for r in answer.results],pending_clarification=answer.clarification,
                  revision=state.revision+1,
                  previous_results=[{'output_id':r.output_id,'result_id':str(r.result_id),'label':r.label,
                                     'status':r.status,'rows':[{k:v for k,v in row.items() if k != 'span_ids'} for row in r.rows[:10]]} for r in answer.results])
    async with database.connection(scope,write=True) as conn:
        current = await conn.fetchrow("SELECT cancel_requested,attempts,status FROM evidence.runs WHERE run_id=$1 FOR UPDATE",run_id)
        if not current or current['attempts'] != row['attempts'] or current['status'] != 'running':
            return Answer(run_id=run_id,status='unavailable',text='This attempt no longer owns the request.')
        current_cancel = current['cancel_requested']
        if current_cancel:
            answer = Answer(run_id=run_id,status="cancelled",text="Request cancelled.")
        elif queued_terms and answer.status == 'pending':
            requested=[r.operation.model_dump(mode='json') for r in answer.results
                       if r.status in {'not_extracted','partial'}]
            await conn.execute("""INSERT INTO evidence.discovery_jobs(job_id,run_id,user_id,scope,terms,progress)
                VALUES($1,$2,$3,$4,$5,$6)""",uuid4(),run_id,scope.user_id,scope.model_dump(mode='json'),queued_terms,
                {'requested_outputs':requested})
        await conn.execute("""UPDATE evidence.runs SET status=$2,response=$3,lease_until=NULL,updated_at=now()
                              WHERE run_id=$1""",run_id,answer.status,answer.model_dump(mode="json"))
        if answer.status not in {"cancelled","unavailable"}:
            await conn.execute("""INSERT INTO evidence.conversations(session_id,user_id,state,revision)
              VALUES($1,$2,$3,$4) ON CONFLICT(session_id) DO UPDATE SET state=EXCLUDED.state,
              revision=EXCLUDED.revision,updated_at=now()""",scope.session_id,scope.user_id,next_state.model_dump(mode="json"),next_state.revision)
    try:
        await gateway.save_message(str(scope.session_id),"assistant",answer.text,
                                   degradation_trace={"evidence_run_id":str(run_id)})
        await gateway.touch_session(str(scope.session_id))
    except Exception:
        # The authoritative answer is already durable in evidence.runs.
        # Session history reconstructs evidence turns from that record.
        logger.exception("Legacy message mirror failed for evidence run %s",run_id)
    return answer


async def stream(payload,user,gateway):
    deadline=_run_deadline()
    try:
        async with _until(deadline):
            run_id,scope,task = await start(payload,user,gateway,deadline=deadline)
    except TimeoutError as exc:
        raise HTTPException(504,'Request preparation reached its deadline; no factual zero was inferred') from exc

    async def events():
        yield sse({"step":"routing","status":"active","answer_status":"running","detail":"Understanding the question","run_id":str(run_id)})
        # Every trace step is emitted ONCE, in order, as it appears.
        #
        # The progress column holds only the latest value, so polling it
        # showed a single stage and threw away everything before it. The
        # run records an accumulating list instead, and this loop sends
        # whatever is new since the last poll.
        sent = 0
        while not task.done():
            done,_ = await asyncio.wait({task},timeout=1)
            if done:
                break
            try:
                row = await get_run(await reauthorize(gateway,scope),run_id)
            except HTTPException:
                async with database.connection(scope,write=True) as conn:
                    await conn.execute('UPDATE evidence.runs SET cancel_requested=true WHERE run_id=$1',run_id)
                yield sse({'step':'response','status':'streaming','detail':'Access to this request has changed.','run_id':str(run_id)})
                yield sse({'step':'response','status':'done','answer_status':'unavailable','run_id':str(run_id)})
                return
            steps = (row.get("progress") or {}).get("steps") or []
            for entry in steps[sent:]:
                yield sse({"step":"trace","status":"active","run_id":str(run_id),
                           "trace":entry,"detail":entry.get("title")})
            sent = max(sent,len(steps))
        try:
            row = await get_run(await reauthorize(gateway,scope),run_id)
        except HTTPException:
            yield sse({'step':'response','status':'streaming','detail':'Access to this request has changed.','run_id':str(run_id)})
            yield sse({'step':'response','status':'done','answer_status':'unavailable','run_id':str(run_id)})
            return
        for entry in ((row.get("progress") or {}).get("steps") or [])[sent:]:
            yield sse({"step":"trace","status":"active","run_id":str(run_id),
                       "trace":entry,"detail":entry.get("title")})
        try:
            answer = await task
            if answer is None:
                raise RuntimeError("run was not claimed")
        except Exception:
            logger.exception("Evidence task failed before completion")
            answer = Answer(run_id=run_id,status="unavailable",text="The evidence service is unavailable.")
        # Recheck permissions and current sources at delivery, after potentially
        # long-running work. A saved scope is not an enduring access grant.
        try:
            from .conversation import prepare
            from src.api.evidence import ExistingConversation,sources_accessible
            fresh=await prepare(gateway,user,ExistingConversation(session_id=str(scope.session_id)),create=False)
            await get_run(fresh.scope,run_id)
            if not await sources_accessible(fresh.scope,answer.model_dump(mode='json'),fresh.attachments):
                raise HTTPException(403,'Access to a cited source has changed')
        except HTTPException:
            answer=Answer(run_id=run_id,status='unavailable',text='Access to this request or its evidence has changed.')
        citations = {s.span_id:s for r in answer.results for s in r.citations}
        yield sse({"step":"retrieval","status":"done","run_id":str(run_id),
                   "sources":[{"filename":s.title,"doc_id":str(s.document_id),"span_id":str(s.span_id),
                               "snippet":s.text,"start":s.start,"end":s.end} for s in citations.values()],
                   "coverage":[r.coverage.model_dump() for r in answer.results]})
        yield sse({"step":"response","status":"streaming","detail":answer.text,"run_id":str(run_id)})
        yield sse({"step":"response","status":"done","run_id":str(run_id),"answer_status":answer.status,
                   "clarification":answer.clarification,"artifact_url":answer.artifact_url,
                   # The complete trace, so a finished run can be read back in
                   # full even if the client missed steps while streaming.
                   "trace":(answer.health or {}).get("trace") or [],
                   "detail":"Response ready"})
    return StreamingResponse(events(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
