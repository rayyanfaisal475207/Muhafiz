from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.retrieval import engine,describe,agent,bilingual
from src.retrieval.planner import Plan,Route,Capability,Escalation


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal",[
    "No passage describes the method.",
    "The answering model was unavailable.",
    "The answering model returned nothing usable.",
])
async def test_descriptive_failure_cannot_start_sql_agent(monkeypatch,refusal):
    plan=Plan(question="Describe the event",route=Route.DATA,
              capabilities={Capability.TEXT_SEARCH},escalation=Escalation(unbound_terms=["method"]))
    monkeypatch.setattr(engine,"enhance",AsyncMock(return_value=SimpleNamespace(semantic="q",lexical=["q"])))
    monkeypatch.setattr(engine,"build_plan",AsyncMock(return_value=plan))
    monkeypatch.setattr(engine,"interpret",AsyncMock(return_value=None))
    monkeypatch.setattr(bilingual,"alternate",AsyncMock(return_value=""))
    monkeypatch.setattr(engine,"hybrid_search",AsyncMock(return_value=SimpleNamespace(passages=[SimpleNamespace(span_id=1)],note="")))
    monkeypatch.setattr(describe,"describe",AsyncMock(return_value=describe.Description(searched=1,refusal=refusal)))
    sql_agent=AsyncMock(side_effect=AssertionError("A descriptive refusal must not invoke SQL"))
    monkeypatch.setattr(agent,"run_agent",sql_agent)
    result=await engine._one(None,"Describe the event")
    assert result["answer"] is None and result["refusal"]==refusal
    assert result["passages"][0].span_id==1
    sql_agent.assert_not_called()
