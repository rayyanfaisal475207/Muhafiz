import json

import httpx
import pytest

from src import config
from src.evidence.model import LocalModel,ModelOutputError,ModelUnavailable,decode_object
from src.evidence.types import QuestionPlan


@pytest.mark.parametrize("body",["", "{", "[]", '{"x":1} trailing', '<think>reason</think>{"x":1}'])
def test_invalid_structured_output(body):
    with pytest.raises(ModelOutputError): decode_object(body)



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
@pytest.mark.parametrize("protocol",["custom","openai","ollama"])
async def test_configured_local_protocols(monkeypatch,protocol):
    monkeypatch.setattr(config,"LOCAL_LLM_URL","https://models.test/chat")
    monkeypatch.setattr(config,"LOCAL_LLM_MODEL","local-test")
    monkeypatch.setenv("EVIDENCE_LOCAL_PROTOCOL",protocol)
    monkeypatch.setenv("EVIDENCE_MODEL_STRUCTURED_OUTPUT","true")
    monkeypatch.setenv("EVIDENCE_MODEL_THINK_CONTROL","true")
    answer={"question":"hi","standalone_question":"hi","conversational_response":"Hello"}
    def respond(request):
        data=json.loads(request.content)
        assert data["model"]=="local-test"
        if protocol=="openai":
            assert data["response_format"]["type"]=="json_schema"
            return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(answer)},"finish_reason":"stop"}]})
        if protocol=="ollama":
            assert data["think"] is False
            return httpx.Response(200,json={"message":{"content":json.dumps(answer)},"done_reason":"stop"})
        return httpx.Response(200,json={"response":json.dumps(answer)})
    model=LocalModel(transport=httpx.MockTransport(respond))
    result=await model.structured(QuestionPlan,"test",{})
    assert result.conversational_response=="Hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("response",[{"response":""},{"response":"{}","finish_reason":"length"},{"choices":[]}])
async def test_empty_and_truncated_content_fails(monkeypatch,response):
    monkeypatch.setattr(config,"LOCAL_LLM_URL","https://models.test/chat")
    monkeypatch.setenv("EVIDENCE_LOCAL_PROTOCOL","custom")
    model=LocalModel(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=response)))
    with pytest.raises(ModelOutputError): await model.structured(QuestionPlan,"test",{})


@pytest.mark.asyncio
async def test_local_outage_has_no_cloud_fallback(monkeypatch):
    monkeypatch.setattr(config,"LOCAL_LLM_URL","https://models.test/chat")
    calls=[]
    def fail(request):
        calls.append(str(request.url))
        return httpx.Response(503)
    model=LocalModel(transport=httpx.MockTransport(fail))
    with pytest.raises(ModelUnavailable): await model.structured(QuestionPlan,"test",{})
    assert calls==["https://models.test/chat"]
def test_schema_compaction_preserves_properties_named_like_schema_annotations():
    """Annotation NOISE is dropped; property names and GUIDANCE are not.

    `description` used to be stripped here along with `title` and `default`,
    and it does not belong with them: the descriptions on QuestionPlan are
    instructions to the planner, written against mistakes it actually makes
    ("NEVER a name -- use object_name for that"). They were authored and then
    discarded on both routes, so no model ever read them. This now pins all
    three facts at once -- a property may be NAMED `title` or `default` and
    survive, the annotation keys are removed, and guidance is kept.
    """
    from src.evidence.model import compact_schema
    schema={'type':'object','title':'A plan','required':['title','default'],
            'properties':{'title':{'type':'string','description':'User title'},
                          'default':{'type':'number','default':0,'examples':[1]}},
            'additionalProperties':False}
    result=compact_schema(schema)
    assert result=={'type':'object','required':['title','default'],'properties':{
        'title':{'type':'string','description':'User title'},
        'default':{'type':'number'}},'additionalProperties':False}


@pytest.mark.asyncio
async def test_gateway_v2_requires_controls_and_forwards_deadline_priority_and_auth(monkeypatch):
    import time
    monkeypatch.setattr(config,'LOCAL_LLM_URL','https://gateway.invalid/llm')
    monkeypatch.setenv('EVIDENCE_LOCAL_PROTOCOL','custom')
    monkeypatch.setenv('EVIDENCE_MODEL_WRAPPER_V2','true')
    monkeypatch.setenv('EVIDENCE_MODEL_API_KEY','fixture-key')
    captured=[]
    def respond(request):
        captured.append(json.loads(request.content))
        assert request.headers['Authorization']=='Bearer fixture-key'
        return httpx.Response(200,json={'response':json.dumps({'question':'hi','standalone_question':'hi',
            'conversational_response':'Hello'}),'finish_reason':'stop','effective_controls':{'think':False,'schema':True}})
    model=LocalModel(transport=httpx.MockTransport(respond),priority='background')
    await model.structured(QuestionPlan,'Synthetic',{},deadline=time.monotonic()+20)
    assert captured[0]['priority']=='background' and 0<captured[0]['timeout_seconds']<=20
    assert captured[0]['think'] is False and captured[0]['json_schema']['type']=='object'
    async def unsupported(request):return httpx.Response(200,json={'response':'{}'})
    with pytest.raises(ModelUnavailable,match='did not confirm'):
        await LocalModel(transport=httpx.MockTransport(unsupported)).structured(QuestionPlan,'Synthetic',{})
