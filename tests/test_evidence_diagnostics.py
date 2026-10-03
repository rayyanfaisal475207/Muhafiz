import asyncio
import json
import time
from uuid import uuid4
from unittest.mock import AsyncMock

import httpx
import pytest

from src import config
from src.evidence.model import LocalModel, ModelOutputError, ModelUnavailable
from src.evidence.runtime import Runtime
from src.evidence.types import QuestionPlan, Scope



@pytest.fixture(autouse=True)
def _wrapper_v2_off(monkeypatch):
    """Pin the wrapper mode a test runs under, instead of inheriting it.

    src/config.py loads the developer's .env at import, so
    EVIDENCE_MODEL_WRAPPER_V2='true' on a working machine reached every
    test here and the openai/ollama protocol paths raised "Wrapper v2
    requires the custom gateway protocol" -- ten failures that said
    nothing about the code and everything about whose machine ran them.

    Tests that exercise wrapper v2 set it on themselves.
    """
    monkeypatch.setenv("EVIDENCE_MODEL_WRAPPER_V2", "false")

@pytest.mark.asyncio
@pytest.mark.parametrize("response,outcome", [
    ({"response":"{"},"malformed_json"),
    ({"response":"{}"},"contract_error"),
    ({"response":""},"empty"),
    ({"response":"{", "finish_reason":"length"},"truncated"),
    ([],"protocol_error"),
])
async def test_model_failure_diagnostics_are_safe(monkeypatch,response,outcome):
    monkeypatch.setattr(config,"LOCAL_LLM_URL","https://local.test/private")
    monkeypatch.setenv("EVIDENCE_LOCAL_PROTOCOL","custom")
    model=LocalModel(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=response)))
    with pytest.raises(ModelOutputError):
        await model.structured(QuestionPlan,"private-source-secret",{})
    assert model.diagnostics[0]["outcome"]==outcome
    assert "private-source-secret" not in json.dumps(model.diagnostics)
    assert "https://" not in json.dumps(model.diagnostics)


@pytest.mark.asyncio
async def test_http_outage_is_recorded(monkeypatch):
    monkeypatch.setattr(config,"LOCAL_LLM_URL","https://local.test/private")
    model=LocalModel(transport=httpx.MockTransport(lambda _:httpx.Response(503)))
    with pytest.raises(ModelUnavailable):
        await model.structured(QuestionPlan,"test",{})
    assert model.diagnostics[0]["outcome"]=="http_error"
    assert model.diagnostics[0]["http_status"]==503


@pytest.mark.asyncio
async def test_repair_receives_rejected_draft_and_health_survives_clarification():
    class Model:
        diagnostics=[{"outcome":"contract_error"}]
        calls=0
        async def structured(self,contract,system,payload,**kwargs):
            # CLASSIFICATION is its own call now, decided before the question is
            # read, so like QueryUnderstanding it is answered here instead of
            # consuming a scripted plan -- otherwise every scripted output
            # shifts by one.
            if contract.__name__ == "QuestionClass":
                return contract.model_validate(
                    {"shape": "single", "category": "other", "asks": 1})
            if contract.__name__ == "QueryUnderstanding":
                return contract.model_validate({"language":"en","parts":[],"terms":[]})
            self.calls+=1
            if self.calls==1:
                raise ModelOutputError("bad operation",raw='{"operation":"rank"}')
            assert payload["previous_plan"]=='{"operation":"rank"}'
            return QuestionPlan(question="q",standalone_question="q",clarification="Which period?")
    store=AsyncMock();store.catalog.return_value={};store.resolve.return_value=[]
    store.resolve_mentions.return_value=[];store.ground_forms.return_value={}
    scope=Scope(user_id=uuid4(),session_id=uuid4())
    answer=await Runtime(store,Model()).run(uuid4(),"q",scope)
    assert answer.status=="clarification"
    assert answer.health["repair_used"]
    assert answer.health["failures"][0]["error_type"]=="ModelOutputError"


@pytest.mark.asyncio
async def test_a_deadline_covers_the_work_before_planning():
    """A deadline bounds the whole run, including the reads that precede
    the planner -- not just the planner call itself.

    The first of those reads used to be the catalog. It is now the
    understanding stage, which reads the question term by term before
    anything is matched against the records, so that is the stage a
    too-short deadline lands in. What must hold is unchanged: the timeout
    is caught, reported as a timeout rather than a finding, and nothing
    downstream runs.
    """
    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    class Stub:
        """No network. This test is about the clock, not the endpoint."""
        diagnostics=[]
        async def structured(self,contract,system,payload,**kwargs):
            # CLASSIFICATION is its own call now, decided before the question is
            # read, so like QueryUnderstanding it is answered here instead of
            # consuming a scripted plan -- otherwise every scripted output
            # shifts by one.
            if contract.__name__ == "QuestionClass":
                return contract.model_validate(
                    {"shape": "single", "category": "other", "asks": 1})
            if contract.__name__ == "QueryUnderstanding":
                return contract.model_validate({"language":"en","parts":[],"terms":[]})
            await asyncio.sleep(1)

    store=AsyncMock();store.catalog.side_effect=slow
    scope=Scope(user_id=uuid4(),session_id=uuid4())
    answer=await Runtime(store,Stub()).run(uuid4(),"q",scope,deadline=time.monotonic()+.01)
    assert answer.status=="unavailable"
    assert answer.health["stage"] in {"understanding","catalog"}
    assert answer.health["failure"]=="TimeoutError"
    store.resolve.assert_not_called()
