from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

from src.api.evidence import ExistingConversation
from src.evidence.conversation import prepare


def setup():
    user=SimpleNamespace(id=uuid4(),role="investigator")
    session=uuid4()
    gateway=SimpleNamespace(get_session=AsyncMock(return_value={"user_id":str(user.id),"case_id":"C1","project_id":None}),
        get_case=AsyncMock(return_value={"case_id":"C1"}),check_case_access=AsyncMock(return_value=True),
        get_attachments_for_session=AsyncMock(return_value=[]),get_session_history=AsyncMock(return_value=[{"role":"user","content":"earlier"}]),
        get_user_context_profile=AsyncMock(return_value={"preferred_language":"ur"}),create_session=AsyncMock(),
        get_cases=AsyncMock(return_value=[{"case_id":"C1"},{"case_id":"C2"}]))
    return user,session,gateway


@pytest.mark.asyncio
async def test_followup_loads_history_and_session_scope():
    user,session,gateway=setup()
    result=await prepare(gateway,user,ExistingConversation(session_id=str(session),message="اور لاہور؟"))
    assert result.history[0]["content"]=="earlier" and result.scope.case_ids==["C1"]


@pytest.mark.asyncio
async def test_foreign_session_denied_before_evidence_access():
    user,session,gateway=setup(); gateway.get_session.return_value["user_id"]=str(uuid4())
    with pytest.raises(HTTPException) as error: await prepare(gateway,user,ExistingConversation(session_id=str(session)))
    assert error.value.status_code==403
    gateway.get_case.assert_not_called()


@pytest.mark.asyncio
async def test_scope_switch_requires_new_conversation():
    user,session,gateway=setup()
    with pytest.raises(HTTPException) as error: await prepare(gateway,user,ExistingConversation(session_id=str(session),case_id=None))
    assert error.value.status_code==409


@pytest.mark.asyncio
async def test_unauthorized_case_is_denied():
    user,session,gateway=setup(); gateway.check_case_access.return_value=False
    with pytest.raises(HTTPException) as error: await prepare(gateway,user,ExistingConversation(session_id=str(session)))
    assert error.value.status_code==403


@pytest.mark.asyncio
async def test_foreign_attachment_is_denied():
    user,session,gateway=setup(); gateway.get_attachments_for_session.return_value=[{"user_id":str(uuid4()),"status":"ready"}]
    with pytest.raises(HTTPException): await prepare(gateway,user,ExistingConversation(session_id=str(session)))


@pytest.mark.asyncio
@pytest.mark.parametrize("role,expected",[("investigator",[]),("supervisor",["C1","C2"]),("platform-admin",["C1","C2"])])
async def test_cross_case_scope_is_server_derived(role,expected):
    user,session,gateway=setup(); user.role=role; gateway.get_session.return_value["case_id"]=None
    result=await prepare(gateway,user,ExistingConversation(session_id=str(session)))
    assert result.scope.case_ids==expected


@pytest.mark.asyncio
async def test_malformed_evidence_mirror_is_removed_without_database_lookup(monkeypatch):
    user,session,gateway=setup()
    gateway.get_session_history.return_value=[{'role':'assistant','content':'secret',
        'degradation_trace':{'evidence_run_id':'invalid-id'}},{'role':'user','content':'legacy'}]
    def forbidden(*args,**kwargs): raise AssertionError('Malformed ID must not reach SQL')
    monkeypatch.setattr('src.evidence.database.connection',forbidden)
    result=await prepare(gateway,user,ExistingConversation(session_id=str(session)))
    assert [item['content'] for item in result.history] == ['legacy']


@pytest.mark.asyncio
async def test_revoked_case_run_does_not_reenter_planner_through_message_mirror(monkeypatch):
    user,session,gateway=setup()
    forbidden_id,allowed_id=uuid4(),uuid4()
    gateway.get_session_history.return_value=[{'role':'assistant','content':content,
        'degradation_trace':{'evidence_run_id':str(key)}} for key,content in [(forbidden_id,'secret'),(allowed_id,'allowed')]]
    conn=SimpleNamespace(fetch=AsyncMock(return_value=[
        {'run_id':forbidden_id,'scope':{'case_ids':['C1','C2'],'project_id':None}},
        {'run_id':allowed_id,'scope':{'case_ids':['C1'],'project_id':None}}]))
    @asynccontextmanager
    async def connection(*args,**kwargs): yield conn
    monkeypatch.setattr('src.evidence.database.connection',connection)
    result=await prepare(gateway,user,ExistingConversation(session_id=str(session)))
    assert [item['content'] for item in result.history] == ['allowed']
