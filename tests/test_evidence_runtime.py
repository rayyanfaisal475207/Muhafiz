from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.evidence.model import ModelOutputError,ModelUnavailable
from src.evidence.runtime import Runtime
from src.evidence.types import ConversationState,EvidenceResult,QuestionPlan,QueryOperation,Scope


class Model:
    diagnostics=[]
    def __init__(self,*outputs):
        self.outputs=iter(outputs);self.payloads=[]
    async def structured(self,contract,system,payload,**kwargs):
        import copy
        # Reading the question is a separate concern from planning it, so it
        # is answered here rather than consuming a scripted plan. Without
        # this every test's first scripted output went to the understanding
        # call and the plans shifted by one.
        # CLASSIFICATION is its own call now, decided before the question is
        # read, so like QueryUnderstanding it is answered here instead of
        # consuming a scripted plan -- otherwise every scripted output
        # shifts by one.
        if contract.__name__ == "QuestionClass":
            return contract.model_validate(
                {"shape": "single", "category": "other", "asks": 1})
        if contract.__name__ == "QueryUnderstanding":
            return contract.model_validate({"language":"en","parts":[],"terms":[]})
        self.payloads.append(copy.deepcopy(payload))
        value=next(self.outputs)
        if isinstance(value,Exception):raise value
        return contract.model_validate(value)


def setup():
    scope=Scope(user_id=uuid4(),session_id=uuid4(),case_ids=["authorized"])
    store=type("Store",(),{})()
    store.catalog=AsyncMock(return_value={})
    store.resolve=AsyncMock(return_value=[])
    # The runtime resolves the NAMES MENTIONED in a question, not the
    # question itself -- passing the whole sentence to resolve() matched no
    # label and handed the planner zero candidates for every natural
    # question. The fake store needs the method the real one grew.
    store.resolve_mentions=AsyncMock(return_value=[])
    store.ground_forms=AsyncMock(return_value={})
    async def execute(op,concepts):
        return EvidenceResult(output_id=op.output_id,scope_fingerprint=scope.fingerprint(),operation=op,label="Passages",status="not_extracted")
    store.execute=AsyncMock(side_effect=execute)
    return scope,store


def plan(**kwargs):
    return {"question":"q","standalone_question":"q",**kwargs}


@pytest.mark.asyncio
async def test_source_change_blocks_definitive_ranked_counts_too():
    from src.evidence.types import Concept,Coverage
    scope,store=setup();subject,predicate=uuid4(),uuid4()
    store.snapshot=AsyncMock(side_effect=['initial','changed'])
    store.catalog.return_value={
        subject:Concept(concept_id=subject,label='Record',kind='entity_type',meaning='A source record',grain='One record'),
        predicate:Concept(concept_id=predicate,label='Group',kind='property',meaning='Recorded group',grain='One record')}
    op=QueryOperation(output_id='ranking',operation='count_entities',subject_type=subject,group_by=predicate,order_by='value_desc')
    store.execute=AsyncMock(return_value=EvidenceResult(output_id='ranking',operation=op,
        scope_fingerprint=scope.fingerprint(),label='Records by group',rows=[{'group_value':'A','value':19}],
        coverage=Coverage(population_documents=2,complete_documents=2),status='answered'))
    model=Model(plan(outputs=[op.model_dump(mode='json')]))
    answer=await Runtime(store,model).run(uuid4(),'rank records',scope)
    assert answer.status=='unavailable' and not answer.claims
    assert '19' not in answer.text


@pytest.mark.asyncio
async def test_followup_receives_previous_plan_and_history():
    scope,store=setup()
    prior=QuestionPlan(**plan(outputs=[{"output_id":"x","operation":"search","text":"weapons in Lahore"}]))
    state=ConversationState(scope_fingerprint=scope.fingerprint(),plan=prior)
    model=Model(plan(standalone_question="weapons in Karachi",continuation=True,
                     outputs=[{"output_id":"x","operation":"search","text":"weapons in Karachi"}]))
    answer=await Runtime(store,model).run(uuid4(),"and Karachi?",scope,state,[{"role":"user","content":"weapons in Lahore"}])
    assert model.payloads[0]["conversation"]["plan"]["outputs"][0]["text"]=="weapons in Lahore"
    assert model.payloads[0]["recent_turns"][0]["content"]=="weapons in Lahore"
    assert answer.plan.continuation


@pytest.mark.asyncio
async def test_scope_change_drops_prior_context():
    scope,store=setup();state=ConversationState(scope_fingerprint="foreign")
    model=Model(plan(clarification="Which case?"))
    answer=await Runtime(store,model).run(uuid4(),"him",scope,state,[{"content":"private"}])
    assert model.payloads[0]["conversation"] is None and model.payloads[0]["recent_turns"]==[]
    assert answer.status=="clarification"


@pytest.mark.asyncio
async def test_only_one_invalid_plan_repair():
    """The repair budget is one, and a broken plan is never executed.

    The store IS reached afterwards, by a fixed fallback search the code
    constructs itself -- not by anything the model proposed. That
    distinction is the point: 34 of 60 real questions failed planning and
    returned a service error without the records ever being consulted.

    Planning is now an escalating LADDER rather than a single repair:
    correct the fields, then simplify, then answer one part, then just list
    the records. A rejection often names its own fix -- "group_by must be a
    relation, here are the valid ids" -- and with one attempt there was
    nothing left to use it with, so the run ended in a plain search that can
    count nothing.

    What must still hold, and is asserted below:
      * the ladder is BOUNDED -- it escalates a fixed number of times and
        stops, never loops
      * the model's own invalid plan is not the thing executed
      * no measure is published from a failed plan; the outcome can be
        `partial` (cited passages) but never `answered`
    """
    scope,store=setup()
    # More errors than rungs, so a loop would consume them all and raise
    # StopIteration instead of finishing.
    model=Model(*[ModelOutputError("broken %d" % i) for i in range(12)])
    answer=await Runtime(store,model).run(uuid4(),"q",scope)
    # BOUNDED, not a fixed number. This ceiling was hard-coded at 6 and broke
    # the moment a sixth rung was added -- reporting a deliberate change as a
    # failure while saying nothing about the property that matters. What is
    # asserted is that the ladder STOPS: one attempt per rung plus the first
    # try. Twelve errors are supplied above, so an unbounded loop consumes
    # them all and raises instead of passing.
    ceiling = 8
    assert 2 <= len(model.payloads) <= ceiling, (
        "planning must escalate a bounded number of times, got "
        f"{len(model.payloads)} attempts")
    assert answer.status in {"unavailable","partial"}
    assert answer.status != "answered"
    for call in store.execute.await_args_list:
        assert call.args[0].output_id == "unplanned", (
            "only the code-constructed fallback may execute after a failed plan")
        assert call.args[0].operation == "search", (
            "a failed plan must not lead to a measure being computed")


@pytest.mark.asyncio
async def test_local_failure_never_becomes_zero():
    scope,store=setup();model=Model(ModelUnavailable("offline"))
    answer=await Runtime(store,model).run(uuid4(),"count",scope)
    assert answer.status=="unavailable" and not answer.results


@pytest.mark.asyncio
async def test_six_tool_round_limit_and_compound_partial():
    scope,store=setup()
    model=Model(plan(outputs=[{"output_id":str(i),"operation":"search","text":f"part {i}"} for i in range(8)]))
    answer=await Runtime(store,model).run(uuid4(),"compound",scope)
    assert store.execute.await_count==6 and len(answer.results)==8
    assert answer.results[-1].status=="unavailable"


@pytest.mark.asyncio
async def test_failed_dependency_does_not_run_downstream_tool():
    scope,store=setup()
    model=Model(plan(outputs=[{"output_id":"first","operation":"search","text":"case"},
        {"output_id":"second","operation":"list","depends_on":["first"]}]))
    answer=await Runtime(store,model).run(uuid4(),"compound",scope)
    assert store.execute.await_count==1 and answer.results[-1].status=="not_extracted"


@pytest.mark.asyncio
async def test_entity_discovery_gets_candidates_then_clarifies():
    scope,store=setup()
    store.resolve.return_value=[{"entity_id":str(uuid4()),"label":"Same name"},{"entity_id":str(uuid4()),"label":"Same name"}]
    model=Model(plan(entity_queries=["Same name"]),plan(clarification="Which of the two people?"))
    answer=await Runtime(store,model).run(uuid4(),"what happened to Same name?",scope)
    assert len(model.payloads[1]["lookup_results"]["entity:Same name"])==2
    assert answer.status=="clarification"


@pytest.mark.asyncio
async def test_completed_lookups_do_not_starve_an_executable_plan():
    scope,store=setup()
    model=Model(plan(entity_queries=['same']),plan(entity_queries=['same'],
        outputs=[{'output_id':'answer','operation':'search','text':'same'}]))
    answer=await Runtime(store,model).run(uuid4(),'same',scope)
    assert store.resolve.await_count==1 and store.execute.await_count==1
    assert not answer.health['repair_used']


@pytest.mark.asyncio
async def test_catalog_lookup_payload_carries_each_definition_only_once():
    from src.evidence.types import Concept
    scope,store=setup()
    key=uuid4()
    concept=Concept(concept_id=key,label='Novel sensor lag',kind='property',meaning='Distinct documented notification duration',
                    value_type='number',unit='seconds',grain='One observation')
    store.catalog.return_value={key:concept}
    model=Model(plan(catalog_queries=['notification']),plan(clarification='Which observation?'))
    await Runtime(store,model).run(uuid4(),'notification',scope)
    observation=model.payloads[1]['lookup_results']['catalog:notification']
    assert observation==[{'concept_id':str(key),'label':concept.label}]
    assert len(model.payloads[1]['catalog'])==1


@pytest.mark.asyncio
async def test_cancellation_before_model_call():
    scope,store=setup();model=Model()
    answer=await Runtime(store,model).run(uuid4(),"q",scope,cancelled=AsyncMock(return_value=True))
    assert answer.status=="cancelled" and not model.payloads


@pytest.mark.asyncio
@pytest.mark.parametrize("language",["ur","en","roman_ur"])
async def test_explicit_response_language_is_preserved(language):
    # The clarification is written in the language being requested. The
    # previous fixture supplied ENGLISH text and asserted the plan was
    # relabelled "ur" -- which is the defect, not the behaviour: an Urdu
    # question was answered in English. A preference is honoured by
    # writing in that language, not by changing the label on English text.
    text = {"ur": "براہ کرم بتائیں کہ آپ کس مقدمے کی بات کر رہے ہیں",
            "en": "Specify a case",
            "roman_ur": "Bara-e-karam bataayein aap kis case ki baat kar rahe hain"}[language]
    scope,store=setup();model=Model(plan(language=language,clarification=text))
    answer=await Runtime(store,model).run(uuid4(),"q",scope,preference=language)
    assert answer.plan.language==language
