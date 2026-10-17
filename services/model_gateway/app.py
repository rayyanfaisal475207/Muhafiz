"""Private, bounded GPU gateway preserving Muhafiz's custom /generate contract.

Run ONE gateway worker per GPU capacity group. All API/ingestion workers share
this gateway; it must not be exposed directly to browser clients.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
import time
from typing import Literal
from uuid import UUID,uuid4

from fastapi import FastAPI,HTTPException,Request
import httpx
from pydantic import BaseModel,ConfigDict,Field


class GenerateRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    model: str|None=Field(default=None,min_length=1,max_length=200)
    system: str=Field(default='',max_length=150000)
    prompt: str=Field(max_length=150000)
    temperature: float=Field(default=0,ge=0,le=2)
    max_tokens: int=Field(default=4096,ge=1,le=16384)
    json_schema: dict|None=None
    think: bool|None=None
    request_id: UUID=Field(default_factory=uuid4)
    priority: Literal['interactive','background']='interactive'
    timeout_seconds: float=Field(default=90,gt=0,le=600)


class Admission:
    def __init__(self,capacity=1,queue_limit=32):
        self.capacity=capacity;self.queue_limit=queue_limit
        self.active=0;self.waiting=[];self.condition=asyncio.Condition()
        self.sequence=0

    @asynccontextmanager
    async def slot(self,priority):
        acquired=False
        async with self.condition:
            if len(self.waiting)>=self.queue_limit:
                raise HTTPException(503,'GPU request queue is full',headers={'Retry-After':'2'})
            self.sequence+=1
            ticket=(0 if priority=='interactive' else 1,self.sequence)
            self.waiting.append(ticket)
            try:
                await self.condition.wait_for(lambda:self.active<self.capacity and ticket==min(self.waiting))
                self.waiting.remove(ticket);self.active+=1;acquired=True
                self.condition.notify_all()
            except BaseException:
                self.waiting.remove(ticket);self.condition.notify_all();raise
        try: yield
        finally:
            if acquired:
                async with self.condition:
                    self.active-=1;self.condition.notify_all()


def create_app(*,transport=None):
    app=FastAPI(title='Muhafiz private GPU gateway')
    admission=Admission(int(os.getenv('MUHAFIZ_GPU_CAPACITY','1')),int(os.getenv('MUHAFIZ_GPU_QUEUE_LIMIT','32')))
    if admission.capacity<1 or admission.queue_limit<1: raise ValueError('GPU limits must be positive')
    active={}
    app.state.admission=admission

    def authorize(request):
        key=os.getenv('MUHAFIZ_GPU_GATEWAY_KEY','')
        if not key: raise HTTPException(503,'GPU gateway authentication is not configured')
        supplied=request.headers.get('authorization','')
        if not hmac.compare_digest(supplied,'Bearer '+key): raise HTTPException(401,'Unauthorized')

    @app.get('/health')
    async def health(request:Request):
        authorize(request)
        return {'native_protocol':'ollama','schema_control':True,'thinking_control':True,
                'capacity':admission.capacity,'active':admission.active,'queued':len(admission.waiting),
                'backend_verified':False}

    async def _generate(payload:GenerateRequest,request:Request,default_model=None,default_thinking=False):
        authorize(request)
        upstream=os.getenv('MUHAFIZ_OLLAMA_BASE_URL','').rstrip('/')
        if not upstream: raise HTTPException(503,'Native GPU backend is not configured')
        if payload.request_id in active: raise HTTPException(409,'Request identity is already active')
        started=time.monotonic()
        selected_model=payload.model or default_model or os.getenv('MUHAFIZ_LLM_MODEL','qwen3:14b')
        selected_thinking=payload.think if payload.think is not None else default_thinking
        async def consume():
            async with asyncio.timeout(payload.timeout_seconds):
                async with admission.slot(payload.priority):
                    body={'model':selected_model,'system':payload.system,'prompt':payload.prompt,
                          'think':selected_thinking,'stream':True,
                          'options':{'temperature':payload.temperature,'num_predict':payload.max_tokens}}
                    if payload.json_schema is not None: body['format']=payload.json_schema
                    pieces=[];size=0;terminal=None
                    async with httpx.AsyncClient(timeout=payload.timeout_seconds,transport=transport) as client:
                        async with client.stream('POST',upstream+'/api/generate',json=body) as response:
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if not line.strip():continue
                                item=json.loads(line)
                                if item.get('error'):raise ValueError('GPU backend generation failed')
                                text=item.get('response','')
                                if not isinstance(text,str):raise ValueError('Invalid native response')
                                pieces.append(text);size+=len(text)
                                if size>150000:raise ValueError('GPU output exceeded its bound')
                                if item.get('done'):terminal=item;break
                    if terminal is None:raise ValueError('GPU stream ended before completion')
                    return {'response':''.join(pieces),'finish_reason':terminal.get('done_reason'),
                            'usage':{'prompt_tokens':terminal.get('prompt_eval_count',0),
                                     'completion_tokens':terminal.get('eval_count',0)},
                            'request_id':str(payload.request_id),'seconds':round(time.monotonic()-started,3),
                            'model':selected_model,
                            'effective_controls':{'think':selected_thinking,'schema':payload.json_schema is not None}}

        async def disconnected():
            # Body parsing has completed. Await the next ASGI event directly;
            # polling is_disconnected's nested cancellation scope can swallow
            # cancellation of this watcher during response cleanup.
            while True:
                event=await request.receive()
                if event['type']=='http.disconnect':return

        task=asyncio.create_task(consume());watcher=asyncio.create_task(disconnected())
        active[payload.request_id]=task
        try:
            done,_=await asyncio.wait({task,watcher},return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:raise HTTPException(499,'GPU caller disconnected')
            return await task
        except asyncio.CancelledError:
            raise HTTPException(499,'GPU request cancelled')
        except (TimeoutError,httpx.TimeoutException):
            raise HTTPException(504,'GPU request deadline expired')
        except (httpx.HTTPError,ValueError,TypeError):
            raise HTTPException(502,'GPU backend returned an invalid or unavailable response')
        finally:
            active.pop(payload.request_id,None)
            task.cancel();watcher.cancel()
            await asyncio.gather(task,watcher,return_exceptions=True)

    @app.post('/generate')
    async def generate(payload:GenerateRequest,request:Request):
        return await _generate(payload,request)

    @app.post('/llm')
    async def llm(payload:GenerateRequest,request:Request):
        return await _generate(payload,request,os.getenv('MUHAFIZ_LLM_MODEL','qwen3:14b'),True)

    @app.post('/qalb')
    async def qalb(payload:GenerateRequest,request:Request):
        return await _generate(payload,request,os.getenv('MUHAFIZ_QALB_MODEL','mtaimoorhassan/qalb-llm-urdu-improved'),False)

    @app.post('/requests/{request_id}/cancel')
    async def cancel(request_id:UUID,request:Request):
        authorize(request)
        task=active.get(request_id)
        if task:task.cancel()
        return {'request_id':str(request_id),'cancel_requested':bool(task)}
    return app


app=create_app()
