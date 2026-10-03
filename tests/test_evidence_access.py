from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import json

import pytest

from src.api.evidence import sources_accessible
from src.evidence.ingestion import stable_id
from src.evidence.types import Scope


def setup_source():
    scope=Scope(user_id=uuid4(),session_id=uuid4())
    span={'span_id':str(uuid4()),'document_id':str(uuid4()),'start':0,'end':6,'text':'source'}
    return scope,span,{'results':[{'citations':[span]}]}


@pytest.mark.asyncio
async def test_saved_source_is_denied_after_document_revocation(monkeypatch):
    scope,span,response=setup_source()
    conn=SimpleNamespace(fetch=AsyncMock(return_value=[]))
    @asynccontextmanager
    async def connection(*args,**kwargs): yield conn
    monkeypatch.setattr('src.api.evidence.database.connection',connection)
    assert not await sources_accessible(scope,response,[])
    conn.fetch.return_value=[{'span_id':span['span_id'],'document_id':span['document_id'],
                             'start_offset':0,'end_offset':6,'text':'source'}]
    assert await sources_accessible(scope,response,[])
    conn.fetch.return_value[0]['text']='edited'
    assert not await sources_accessible(scope,response,[])


@pytest.mark.asyncio
async def test_deleted_attachment_cannot_be_read_from_saved_answer(monkeypatch):
    scope,span,response=setup_source()
    attachment={'attachment_id':str(uuid4()),'extracted_text':'source'}
    span['document_id']=str(stable_id('attachment',attachment['attachment_id']))
    assert await sources_accessible(scope,response,[attachment])
    conn=SimpleNamespace(fetch=AsyncMock(return_value=[]))
    @asynccontextmanager
    async def connection(*args,**kwargs): yield conn
    monkeypatch.setattr('src.api.evidence.database.connection',connection)
    assert not await sources_accessible(scope,response,[])


@pytest.mark.asyncio
async def test_session_export_does_not_leak_rejected_evidence_mirror(monkeypatch):
    from src.api import sessions
    user=SimpleNamespace(id=uuid4())
    session=str(uuid4())
    gateway=SimpleNamespace(get_session=AsyncMock(return_value={'user_id':str(user.id),'title':'Example'}),
        get_session_history=AsyncMock(return_value=[{'role':'assistant','content':'secret',
            'degradation_trace':{'evidence_run_id':str(uuid4())}},
            {'role':'user','content':'legacy','created_at':'2026-01-01'}]),touch_session=AsyncMock())
    monkeypatch.setattr(sessions,'get_gateway',AsyncMock(return_value=gateway))
    monkeypatch.setattr('src.config.EVIDENCE_RUNTIME_ENABLED',True)
    monkeypatch.setattr('src.api.evidence.session_runs',AsyncMock(return_value={'runs':[]}))
    history=await sessions.get_session_history(session,user)
    assert [item['content'] for item in history['history']]==['legacy']
    exported=await sessions.export_session(session,'json',user)
    assert [item['content'] for item in json.loads(exported.body)['history']]==['legacy']
