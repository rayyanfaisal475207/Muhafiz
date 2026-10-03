import httpx
import pytest

from scripts.evidence_replay import replay,wait_for_run


@pytest.mark.asyncio
async def test_pending_timeout_is_recorded_as_pending_instead_of_completion():
    client=httpx.AsyncClient(base_url='https://staging.invalid',transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json={'run_id':'R1','status':'pending'})))
    async with client:
        result=await wait_for_run(client,'R1',timeout=0)
    assert result['status']=='pending'


@pytest.mark.asyncio
async def test_resume_does_not_post_completed_turns_again():
    requests=[]
    def handler(request):
        requests.append((request.method,request.url.path))
        if request.method=='POST':
            return httpx.Response(200,text='data: {"run_id":"R2"}\n\n')
        return httpx.Response(200,json={'run_id':request.url.path.split('/')[-1],'status':'answered','response':{'text':'supported'}})
    previous={'id':'scenario','session_id':'same-session','turns':[
        {'request':{'message':'first'},'run':{'run_id':'R1','status':'pending'}}]}
    scenario={'id':'scenario','turns':[{'message':'first'},{'message':'follow-up'}]}
    async with httpx.AsyncClient(base_url='https://staging.invalid',transport=httpx.MockTransport(handler)) as client:
        result=await replay(client,scenario,previous=previous,pending_timeout=0)
    assert result['session_id']=='same-session' and len(result['turns'])==2
    assert sum(method=='POST' for method,path in requests)==1
    assert requests[0]==('GET','/api/evidence/runs/R1')
