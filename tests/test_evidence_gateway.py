import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from services.model_gateway.app import Admission,create_app


@pytest.mark.asyncio
async def test_gateway_forwards_native_controls_and_preserves_completion(monkeypatch):
    monkeypatch.setenv('MUHAFIZ_GPU_GATEWAY_KEY','fixture-key')
    monkeypatch.setenv('MUHAFIZ_OLLAMA_BASE_URL','https://native.invalid')
    captured=[]
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,text='{"response":"{\\"value\\":10}","done":true,"done_reason":"stop","eval_count":5}\n')
    app=create_app(transport=httpx.MockTransport(respond))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://gateway.invalid') as client:
        assert (await client.post('/generate',json={'model':'fixture','prompt':'q'})).status_code==401
        response=await client.post('/generate',headers={'Authorization':'Bearer fixture-key'},json={
            'model':'fixture','prompt':'synthetic','json_schema':{'type':'object'},'think':False})
    assert response.status_code==200 and response.json()['response']=='{"value":10}'
    assert captured[0]['format']=={'type':'object'} and captured[0]['think'] is False
    assert captured[0]['stream'] is True


@pytest.mark.asyncio
async def test_legacy_routes_preserve_defaults_and_accept_explicit_thinking_control(monkeypatch):
    monkeypatch.setenv('MUHAFIZ_GPU_GATEWAY_KEY','fixture-key')
    monkeypatch.setenv('MUHAFIZ_OLLAMA_BASE_URL','https://native.invalid')
    captured=[]
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,text='{"response":"ok","done":true,"done_reason":"stop"}\n')
    app=create_app(transport=httpx.MockTransport(respond))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://gateway.invalid',
                                 headers={'Authorization':'Bearer fixture-key'}) as client:
        for route,payload in [('/llm',{'prompt':'synthetic'}),('/llm',{'prompt':'synthetic','think':False}),
                              ('/qalb',{'prompt':'synthetic'})]:
            assert (await client.post(route,json=payload)).status_code==200
    assert [body['think'] for body in captured]==[True,False,False]
    assert captured[0]['model']=='qwen3:14b'
    assert captured[2]['model']=='mtaimoorhassan/qalb-llm-urdu-improved'


@pytest.mark.asyncio
async def test_gateway_timeout_closes_native_stream_and_releases_admission(monkeypatch):
    monkeypatch.setenv('MUHAFIZ_GPU_GATEWAY_KEY','fixture-key')
    monkeypatch.setenv('MUHAFIZ_OLLAMA_BASE_URL','https://native.invalid')
    closed=asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"response":"part","done":false}\n'
            await asyncio.sleep(20)
        async def aclose(self):closed.set()
    app=create_app(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Stream())))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://gateway.invalid') as client:
        response=await client.post('/generate',headers={'Authorization':'Bearer fixture-key'},json={
            'model':'fixture','prompt':'synthetic','timeout_seconds':.03})
    assert response.status_code==504 and closed.is_set()
    assert app.state.admission.active==0


@pytest.mark.asyncio
async def test_admission_prioritizes_interactive_requests_and_cleans_cancelled_waiters():
    admission=Admission(capacity=1,queue_limit=2)
    entered=[]
    async def waiting(name,priority):
        async with admission.slot(priority):entered.append(name)
    async with admission.slot('interactive'):
        background=asyncio.create_task(waiting('background','background'))
        await asyncio.sleep(0)
        foreground=asyncio.create_task(waiting('foreground','interactive'))
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as error:
            async with admission.slot('interactive'):pass
        assert error.value.status_code==503
    await asyncio.gather(background,foreground)
    assert entered==['foreground','background'] and admission.active==0
    async with admission.slot('interactive'):
        cancelled=asyncio.create_task(waiting('cancelled','background'))
        await asyncio.sleep(0);cancelled.cancel()
        await asyncio.gather(cancelled,return_exceptions=True)
        assert not admission.waiting


@pytest.mark.asyncio
async def test_explicit_cancellation_closes_upstream_and_returns_cancelled(monkeypatch):
    monkeypatch.setenv('MUHAFIZ_GPU_GATEWAY_KEY','fixture-key')
    monkeypatch.setenv('MUHAFIZ_OLLAMA_BASE_URL','https://native.invalid')
    started,closed=asyncio.Event(),asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.sleep(20)
            yield b'{"response":"late","done":true}\n'
        async def aclose(self):closed.set()
    app=create_app(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Stream())))
    request_id=str(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://gateway.invalid',
                                 headers={'Authorization':'Bearer fixture-key'}) as client:
        pending=asyncio.create_task(client.post('/generate',json={'model':'fixture','prompt':'synthetic','request_id':request_id}))
        await asyncio.wait_for(started.wait(),2)
        cancellation=await client.post(f'/requests/{request_id}/cancel')
        response=await asyncio.wait_for(pending,2)
    assert cancellation.json()['cancel_requested'] is True
    assert response.status_code==499 and closed.is_set() and app.state.admission.active==0
