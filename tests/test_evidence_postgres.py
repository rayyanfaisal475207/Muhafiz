"""Integration tests only against a separately provisioned test database.

EVIDENCE_TEST_ENABLED=1 opts in. Never falls back to the production DSN.
"""
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from dotenv import dotenv_values

from src.evidence.database import _init,install_scope
from src.evidence.ingestion import put_document,put_span
from src.evidence.store import EvidenceStore
from src.evidence.types import Concept,QueryOperation,Scope

pytestmark=[pytest.mark.requires_postgres,pytest.mark.skipif(os.getenv("EVIDENCE_TEST_ENABLED")!="1",reason="isolated evidence database not enabled")]


@pytest.mark.asyncio
async def test_search_respects_entity_and_document_constraints(evidence_db,monkeypatch):
    from src.evidence import vectors
    from unittest.mock import AsyncMock
    db=evidence_db
    vector=AsyncMock(return_value=([],None))
    monkeypatch.setattr(vectors,"search",vector)
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        store=EvidenceStore(db["reader"],db["scope"])
        result=await store.execute(QueryOperation(output_id="selected",operation="search",text="Widget",
                                  source_scope="case_documents",entity_ids=[db["entities"][0]]),await store.catalog())
        assert {s.document_id for s in result.citations}=={db["docs"][0]}
        assert vector.call_args.kwargs["document_ids"]==[db["docs"][0]]
        assert result.status=="partial"


@pytest.mark.asyncio
async def test_month_boundary_excludes_next_month(evidence_db):
    from datetime import datetime,timezone
    db=evidence_db
    boundary=datetime(2026,7,1,tzinfo=timezone.utc)
    await db["writer"].execute("UPDATE evidence.assertions SET event_time=$2 WHERE document_id=$1",
                               db["docs"][0],boundary)
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        store=EvidenceStore(db["reader"],db["scope"])
        result=await store.execute(QueryOperation(output_id="june",operation="timeline",
                      entity_ids=[db["entities"][0]],occurred_before=boundary),await store.catalog())
        assert result.rows==[]
        july=await store.execute(QueryOperation(output_id="july",operation="timeline",
                      entity_ids=[db["entities"][0]],occurred_after=boundary),await store.catalog())
        assert july.rows


@pytest.mark.asyncio
async def test_duration_uses_both_dates_and_blocks_missing_endpoints(evidence_db):
    from datetime import datetime,timedelta,timezone
    from src.evidence.answer import render
    from src.evidence.types import QuestionPlan
    db=evidence_db;start=datetime(2026,1,1,tzinfo=timezone.utc)
    predicates=[]
    for label in ["start","end"]:
        key=uuid4();predicates.append(key)
        await db["writer"].execute("""INSERT INTO evidence.concepts(concept_id,label,kind,meaning,value_type,grain,origin_document_id,status)
          VALUES($1,$2,'property','Recorded event time','date','One widget',$3,'validated')""",key,label,db["docs"][0])
        for doc,entity in zip(db["docs"],db["entities"]):
            span=await db["writer"].fetchval("SELECT span_id FROM evidence.entities WHERE entity_id=$1",entity)
            await db["writer"].execute("""INSERT INTO evidence.assertions(assertion_id,document_id,subject_id,predicate_id,value_date,span_id,status,extraction_version)
             VALUES($1,$2,$3,$4,$5,$6,'validated','duration-test')""",uuid4(),doc,entity,key,
             start if label=="start" else start+timedelta(seconds=60),span)
            await db["writer"].execute("""INSERT INTO evidence.coverage(document_id,concept_id,concept_version,content_hash,status,extraction_version)
             SELECT document_id,$2,1,content_hash,'complete','duration-test' FROM evidence.documents WHERE document_id=$1""",doc,key)
    op=QueryOperation(output_id="elapsed",operation="average_duration",subject_type=db["type"].concept_id,
                      start_predicate_id=predicates[0],end_predicate_id=predicates[1])
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        store=EvidenceStore(db["reader"],db["scope"])
        result=await store.execute(op,await store.catalog())
        assert float(result.rows[0]["value"])==60 and result.coverage.complete
        assert result.unit=="seconds"
    await db["writer"].execute("UPDATE evidence.assertions SET status='superseded',retracted_at=now() WHERE subject_id=$1 AND predicate_id=$2",db["entities"][0],predicates[1])
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        store=EvidenceStore(db["reader"],db["scope"])
        result=await store.execute(op,await store.catalog())
        assert result.coverage.missing_values==1 and result.status=="partial"
        answer=render(uuid4(),QuestionPlan(question="elapsed?",standalone_question="elapsed?",outputs=[op]),[result],db["scope"])
        assert not answer.claims


@pytest.mark.asyncio
async def test_grouped_counts_use_confirmed_links_not_matching_names(evidence_db):
    db=evidence_db;targets=[];relation=uuid4()
    await db["writer"].execute("""INSERT INTO evidence.concepts(concept_id,label,kind,meaning,value_type,grain,origin_document_id,status)
      VALUES($1,'Recorded at','relation','Explicit source relation','entity','One target',$2,'validated')""",relation,db["docs"][0])
    for doc,subject in zip(db["docs"],db["entities"]):
        target=uuid4();targets.append(target)
        span=await db["writer"].fetchval("SELECT span_id FROM evidence.entities WHERE entity_id=$1",subject)
        await db["writer"].execute("""INSERT INTO evidence.entities(entity_id,document_id,concept_id,label,span_id,identity_status)
          VALUES($1,$2,$3,'Same station name',$4,'confirmed')""",target,doc,db["type"].concept_id,span)
        await db["writer"].execute("""INSERT INTO evidence.assertions(assertion_id,document_id,subject_id,predicate_id,object_entity_id,span_id,status,extraction_version)
          VALUES($1,$2,$3,$4,$5,$6,'validated','group-test')""",uuid4(),doc,subject,relation,target,span)
        await db["writer"].execute("""INSERT INTO evidence.coverage(document_id,concept_id,concept_version,content_hash,status,extraction_version)
          SELECT document_id,$2,1,content_hash,'complete','group-test' FROM evidence.documents WHERE document_id=$1""",doc,relation)
    op=QueryOperation(output_id="stations",operation="count_entities",subject_type=db["type"].concept_id,
                      entity_ids=db["entities"],group_by=relation,order_by="value_desc")
    async def query():
        async with db["reader"].transaction(readonly=True):
            await install_scope(db["reader"],db["scope"])
            store=EvidenceStore(db["reader"],db["scope"])
            return await store.execute(op,await store.catalog())
    separate=await query()
    assert len(separate.rows)==2  # Same spelling is not identity evidence.
    left,right=sorted(targets,key=str)
    await db["writer"].execute("""INSERT INTO evidence.identity_links(left_id,right_id,status,method,justification)
      VALUES($1,$2,'confirmed','reviewed-test','{}')""",left,right)
    joined=await query()
    assert len(joined.rows)==1 and joined.rows[0]["value"]==2
    assert joined.rows[0]["group_value"]=="Same station name"
    assert joined.coverage.complete


@pytest_asyncio.fixture
async def evidence_db():
    cfg=dotenv_values(Path(__file__).resolve().parents[1]/".env.evidence-test")
    for key in ("EVIDENCE_QUERY_DATABASE_URL","EVIDENCE_INGEST_DATABASE_URL"):
        assert "/muhafiz_evidence_test_" in cfg[key],"refusing a non-test database"
    writer=await asyncpg.connect(cfg["EVIDENCE_INGEST_DATABASE_URL"])
    reader=await asyncpg.connect(cfg["EVIDENCE_QUERY_DATABASE_URL"])
    await _init(writer); await _init(reader)
    user,session=uuid4(),uuid4()
    case1,case2=f"C-{uuid4()}",f"C-{uuid4()}"
    docs=[]; entities=[]
    ctype=Concept(concept_id=uuid4(),label="Widgets",kind="entity_type",meaning="A source-defined widget",grain="One widget")
    quantity=Concept(concept_id=uuid4(),label="Quantity",kind="property",meaning="Item quantity",grain="One widget",value_type="number",unit="items")
    flag=Concept(concept_id=uuid4(),label="Flag",kind="property",meaning="A recorded flag",grain="One widget")
    async with writer.transaction():
        for case,n in [(case1,2),(case2,3)]:
            text=f"Widget has quantity {n}. Flag active."
            doc=await put_document(writer,source_key=str(uuid4()),text=text,case_ids=[case])
            docs.append(doc)
            span=await put_span(writer,doc,text,0,len(text))
            if len(docs)==1:
                for concept in [ctype,quantity,flag]:
                    await writer.execute("""INSERT INTO evidence.concepts(concept_id,label,kind,meaning,aliases,value_type,unit,grain,origin_document_id,status)
                      VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'validated')""",concept.concept_id,concept.label,concept.kind,concept.meaning,[],concept.value_type,concept.unit,concept.grain,doc)
            entity=uuid4(); entities.append(entity)
            await writer.execute("""INSERT INTO evidence.entities(entity_id,document_id,concept_id,label,span_id,identity_status)
                                  VALUES($1,$2,$3,'Widget',$4,'confirmed')""",entity,doc,ctype.concept_id,span)
            await writer.execute("""INSERT INTO evidence.assertions(assertion_id,document_id,subject_id,predicate_id,value_number,unit,span_id,status,extraction_version)
                      VALUES($1,$2,$3,$4,$5,'items',$6,'validated','test')""",uuid4(),doc,entity,quantity.concept_id,n,span)
            # Multiple matching filters must not multiply the sum.
            for _ in range(2):
                await writer.execute("""INSERT INTO evidence.assertions(assertion_id,document_id,subject_id,predicate_id,value_text,span_id,status,extraction_version)
                      VALUES($1,$2,$3,$4,'active',$5,'validated','test')""",uuid4(),doc,entity,flag.concept_id,span)
            for c in [ctype,quantity,flag]:
                await writer.execute("""INSERT INTO evidence.coverage(document_id,concept_id,concept_version,content_hash,status,extraction_version)
                      SELECT document_id,$2,1,content_hash,'complete','test' FROM evidence.documents WHERE document_id=$1""",doc,c.concept_id)
    try:
        yield {"writer":writer,"reader":reader,"scope":Scope(user_id=user,session_id=session,case_ids=[case1,case2],include_global=False),
               "docs":docs,"entities":entities,"type":ctype,"quantity":quantity,"flag":flag}
    finally:
        await reader.close(); await writer.close()


@pytest.mark.asyncio
async def test_reader_without_context_sees_no_evidence(evidence_db):
    conn=evidence_db["reader"]
    assert await conn.fetchval("SELECT count(*) FROM evidence.documents")==0
    assert await conn.fetchval("SELECT count(*) FROM evidence.entities")==0
    assert await conn.fetchval("SELECT count(*) FROM evidence.concepts")==0


@pytest.mark.asyncio
async def test_reader_cannot_mutate_evidence_or_acquire_ingestor(evidence_db):
    for sql in ["DELETE FROM evidence.documents", "SET ROLE muhafiz_evidence_ingestor", "CREATE TABLE evidence.escape(x int)"]:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await evidence_db["reader"].execute(sql)


@pytest.mark.asyncio
async def test_case_scope_applies_to_sql_catalog_and_citations(evidence_db):
    db=evidence_db; scope=db["scope"].model_copy(update={"case_ids":db["scope"].case_ids[:1]})
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],scope)
        store=EvidenceStore(db["reader"],scope)
        concepts=await store.catalog()
        result=await store.execute(QueryOperation(output_id="count",operation="count_entities",subject_type=db["type"].concept_id),concepts)
        assert result.rows[0]["value"]==1 and result.coverage.complete
        assert {s.document_id for s in result.citations}=={db["docs"][0]}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,expected",[("count_entities",2),("count_assertions",2),("sum",5),("average",2.5)])
async def test_measures_and_filter_fanout(evidence_db,operation,expected):
    db=evidence_db
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        store=EvidenceStore(db["reader"],db["scope"]); concepts=await store.catalog()
        op=QueryOperation(output_id="value",operation=operation,subject_type=db["type"].concept_id,
             predicate_id=db["quantity"].concept_id if operation!="count_entities" else None,
             filters=[{"predicate_id":db["flag"].concept_id,"value":"active"}])
        result=await store.execute(op,concepts)
        assert float(result.rows[0]["value"])==expected
        assert result.status=="answered" and result.coverage.complete


@pytest.mark.asyncio
async def test_missing_coverage_does_not_certify_zero(evidence_db):
    db=evidence_db
    await db["writer"].execute("UPDATE evidence.coverage SET status='partial' WHERE document_id=$1",db["docs"][1])
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        store=EvidenceStore(db["reader"],db["scope"])
        result=await store.execute(QueryOperation(output_id="n",operation="count_entities",subject_type=db["type"].concept_id),await store.catalog())
        assert not result.coverage.complete and result.status=="partial"


@pytest.mark.asyncio
async def test_scope_is_transaction_local(evidence_db):
    db=evidence_db
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        assert await db["reader"].fetchval("SELECT count(*) FROM evidence.documents")==2
    assert await db["reader"].fetchval("SELECT count(*) FROM evidence.documents")==0


@pytest.mark.asyncio
async def test_session_attachment_document_is_not_shared(evidence_db):
    db=evidence_db; scope=db["scope"]
    doc=await put_document(db["writer"],source_key=str(uuid4()),text="Private attachment",owner_id=scope.user_id,session_id=scope.session_id)
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],scope.model_copy(update={"session_id":uuid4()}))
        assert not await db["reader"].fetchval("SELECT EXISTS(SELECT 1 FROM evidence.documents WHERE document_id=$1)",doc)


@pytest.mark.asyncio
async def test_multiple_case_document_requires_access_to_every_case(evidence_db):
    db=evidence_db; scope=db["scope"]
    doc=await put_document(db["writer"],source_key=str(uuid4()),text="Two cases",case_ids=scope.case_ids)
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],scope.model_copy(update={"case_ids":scope.case_ids[:1]}))
        assert not await db["reader"].fetchval("SELECT EXISTS(SELECT 1 FROM evidence.documents WHERE document_id=$1)",doc)


@pytest.mark.asyncio
async def test_provenance_cannot_be_moved_to_another_document(evidence_db):
    db=evidence_db
    span=await db["writer"].fetchval("SELECT span_id FROM evidence.entities WHERE entity_id=$1",db["entities"][0])
    with pytest.raises(asyncpg.RaiseError,match="different source"):
        await db["writer"].execute("UPDATE evidence.entities SET span_id=$2 WHERE entity_id=$1",db["entities"][1],span)
    with pytest.raises(asyncpg.RaiseError,match="outside its source"):
        await db["writer"].execute("UPDATE evidence.assertions SET span_id=$2 WHERE subject_id=$1",db["entities"][1],span)
    with pytest.raises(asyncpg.RaiseError,match="does not match"):
        await db["writer"].execute("UPDATE evidence.spans SET text=repeat('x',char_end-char_start) WHERE span_id=$1",span)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation",["dispute","missing","duplicate","identity","group"])
async def test_measurement_gaps_cannot_produce_definitive_counts(evidence_db,mutation):
    db=evidence_db;writer=db["writer"]
    if mutation=="dispute":
        await writer.execute("UPDATE evidence.assertions SET status='disputed' WHERE subject_id=$1 AND predicate_id=$2",db["entities"][0],db["quantity"].concept_id)
    elif mutation=="missing":
        await writer.execute("UPDATE evidence.assertions SET status='candidate' WHERE subject_id=$1 AND predicate_id=$2",db["entities"][0],db["quantity"].concept_id)
    elif mutation=="duplicate":
        await writer.execute("""INSERT INTO evidence.assertions(assertion_id,document_id,subject_id,predicate_id,value_number,unit,span_id,status,extraction_version)
          SELECT $1,document_id,subject_id,predicate_id,value_number,unit,span_id,status,extraction_version
          FROM evidence.assertions WHERE subject_id=$2 AND predicate_id=$3""",uuid4(),db["entities"][0],db["quantity"].concept_id)
    elif mutation=="identity":
        await writer.execute("UPDATE evidence.entities SET identity_status='unresolved' WHERE entity_id=$1",db["entities"][0])
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        store=EvidenceStore(db["reader"],db["scope"])
        op=QueryOperation(output_id="q",operation="sum",subject_type=db["type"].concept_id,predicate_id=db["quantity"].concept_id,
            group_by=db["flag"].concept_id if mutation=="group" else None)
        result=await store.execute(op,await store.catalog())
        assert not result.coverage.complete
        from src.evidence.answer import render
        from src.evidence.types import QuestionPlan
        answer=render(uuid4(),QuestionPlan(question="q",standalone_question="q",outputs=[op]),[result],db["scope"])
        assert not answer.claims and "Total quantity:" not in answer.text


@pytest.mark.asyncio
async def test_owner_scopes_runs_and_reports(evidence_db):
    db=evidence_db;scope=db["scope"];run=uuid4()
    await db["writer"].execute("INSERT INTO evidence.runs(run_id,user_id,session_id,scope,question) VALUES($1,$2,$3,$4,'q')",
        run,scope.user_id,scope.session_id,scope.model_dump(mode="json"))
    async with db["reader"].transaction():
        await install_scope(db["reader"],scope.model_copy(update={"user_id":uuid4()}))
        assert not await db["reader"].fetchval("SELECT EXISTS(SELECT 1 FROM evidence.runs WHERE run_id=$1)",run)
        assert await db["reader"].execute("UPDATE evidence.runs SET cancel_requested=true WHERE run_id=$1",run)=="UPDATE 0"


@pytest.mark.asyncio
async def test_graph_follows_ordered_relations_and_keeps_unknown_identities_separate(evidence_db):
    db=evidence_db;writer=db["writer"];doc=db["docs"][0]
    span=await writer.fetchval("SELECT span_id FROM evidence.entities WHERE entity_id=$1",db["entities"][0])
    relations=[uuid4(),uuid4()];nodes=[db["entities"][0],uuid4(),uuid4(),uuid4()]
    for relation in relations:
        await writer.execute("""INSERT INTO evidence.concepts(concept_id,label,kind,meaning,value_type,grain,origin_document_id,status)
            VALUES($1,'Link','relation','A documented directed link','entity','One link',$2,'validated')""",relation,doc)
    for i,node in enumerate(nodes[1:]):
        await writer.execute("INSERT INTO evidence.entities(entity_id,document_id,concept_id,label,span_id) VALUES($1,$2,$3,$4,$5)",
            node,doc,db["type"].concept_id,f"N{i}",span)
    for left,right,relation in [(nodes[0],nodes[1],relations[0]),(nodes[1],nodes[2],relations[1]),(nodes[0],nodes[3],relations[1])]:
        await writer.execute("""INSERT INTO evidence.assertions(assertion_id,document_id,subject_id,predicate_id,object_entity_id,span_id,status,extraction_version)
            VALUES($1,$2,$3,$4,$5,$6,'validated','test')""",uuid4(),doc,left,relation,right,span)
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        store=EvidenceStore(db["reader"],db["scope"])
        result=await store.execute(QueryOperation(output_id="g",operation="traverse",entity_ids=[nodes[0]],relationship_path=relations),await store.catalog())
        assert {r["object_entity_id"] for r in result.rows}=={str(nodes[1]),str(nodes[2])}
        assert result.status=="partial" and result.coverage.unresolved_identities==2


@pytest.mark.asyncio
async def test_concept_change_invalidates_old_coverage_and_preserves_definition(evidence_db):
    db=evidence_db;cid=db["quantity"].concept_id
    await db["writer"].execute("UPDATE evidence.concepts SET meaning='Revised reviewed quantity definition' WHERE concept_id=$1",cid)
    assert await db["writer"].fetchval("SELECT version FROM evidence.concepts WHERE concept_id=$1",cid)==2
    assert await db["writer"].fetchval("SELECT definition->>'meaning' FROM evidence.concept_revisions WHERE concept_id=$1",cid)==db["quantity"].meaning
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        store=EvidenceStore(db["reader"],db["scope"])
        result=await store.execute(QueryOperation(output_id="q",operation="sum",predicate_id=cid),await store.catalog())
        assert not result.coverage.complete


@pytest.mark.asyncio
async def test_original_source_is_immutable(evidence_db):
    with pytest.raises(asyncpg.RaiseError,match="immutable"):
        await evidence_db["writer"].execute("UPDATE evidence.documents SET content='replaced' WHERE document_id=$1",evidence_db["docs"][0])


@pytest.mark.asyncio
async def test_project_only_sources_require_authorized_project_scope(evidence_db):
    db=evidence_db;project=uuid4()
    doc=await put_document(db["writer"],source_key=str(uuid4()),text="Project-only source",project_id=project)
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"])
        assert not await db["reader"].fetchval("SELECT EXISTS(SELECT 1 FROM evidence.documents WHERE document_id=$1)",doc)
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"],db["scope"].model_copy(update={"project_id":project}))
        assert await db["reader"].fetchval("SELECT EXISTS(SELECT 1 FROM evidence.documents WHERE document_id=$1)",doc)


@pytest.mark.asyncio
async def test_a_roman_name_reaches_urdu_records(evidence_db):
    """MEASURED: asked "who is Ahmed and is he nominated in any cases
    during the last 2 months", the system established nothing. The corpus
    holds شکیل احمد, چوہدری بشیر احمد and سرفراز احمد -- the name was
    plainly present, and an English question never reached Urdu data.

    roman_skeleton() romanises Arabic script, but Urdu omits short vowels,
    so احمد becomes 'ahmd' and never equals the typed 'ahmed'. Removing
    vowels from both sides makes them comparable, per name-part so a given
    name matches inside a full name.

    These are CANDIDATES, not identities: devowelling is lossy, so the
    matches are labelled cross_script and more than one is a reason to ask
    which person is meant.
    """
    db = evidence_db
    for name in ["کاشف", "شکیل احمد", "بشیر احمد"]:
        doc = await put_document(db['writer'], source_key=str(uuid4()), text=name,
                                 case_ids=db['scope'].case_ids)
        span = await put_span(db['writer'], doc, name, 0, len(name))
        await db['writer'].execute('''INSERT INTO evidence.entities(entity_id,document_id,concept_id,label,span_id,identity_status)
            VALUES($1,$2,$3,$4,$5,'confirmed')''',uuid4(),doc,db['type'].concept_id,name,span)
    async with db['reader'].transaction(readonly=True):
        await install_scope(db['reader'],db['scope'])
        evidence_store = EvidenceStore(db['reader'],db['scope'])
        await check_roman_candidates(evidence_store)


async def check_roman_candidates(evidence_store):
    same = await evidence_store.resolve("کاشف")
    assert same, "an exact Urdu name must still resolve"
    assert {r["match"] for r in same} == {"same_script"}

    crossed = await evidence_store.resolve("Ahmed")
    assert crossed, "a Roman spelling must reach the Urdu records"
    assert {r["match"] for r in crossed} == {"cross_script"}
    assert all("احمد" in r["label"] for r in crossed)

    # Same-script matches win outright; the fallback never runs for them.
    assert {r["match"] for r in await evidence_store.resolve("Kashif")} == {"cross_script"}

    # A term that romanises to nothing in the corpus stays empty.
    assert await evidence_store.resolve("Nonexistentname") == []


@pytest.mark.asyncio
async def test_duration_cohorts_have_independent_denominators(evidence_db):
    from datetime import datetime,timedelta,timezone
    db = evidence_db; start_key,end_key = uuid4(),uuid4()
    for key,label in [(start_key,'Occurrence'),(end_key,'Notification')]:
        await db['writer'].execute('''INSERT INTO evidence.concepts(concept_id,label,kind,meaning,value_type,grain,origin_document_id,status)
            VALUES($1,$2,'property',$2,'date','One event',$3,'validated')''',key,label,db['docs'][0])
    examples = [(2024,600),(2024,1200),(2024,None),(2026,1800),(2026,3600),(None,None)]
    for year,delay in examples:
        doc = await put_document(db['writer'],source_key=str(uuid4()),text='A synthetic sourced event',case_ids=db['scope'].case_ids)
        span = await put_span(db['writer'],doc,'A synthetic sourced event',0,24)
        entity = uuid4()
        await db['writer'].execute('''INSERT INTO evidence.entities(entity_id,document_id,concept_id,label,span_id,identity_status)
            VALUES($1,$2,$3,'Synthetic event',$4,'confirmed')''',entity,doc,db['type'].concept_id,span)
        for key,date in [(start_key,datetime(year,1,2,tzinfo=timezone.utc) if year else None),
                         (end_key,datetime(year,1,2,tzinfo=timezone.utc)+timedelta(seconds=delay) if year and delay is not None else None)]:
            if date is not None:
                # Identical corroborating assertions must not double the event.
                for _ in range(2):
                    await db['writer'].execute('''INSERT INTO evidence.assertions(assertion_id,document_id,subject_id,predicate_id,value_date,span_id,status,extraction_version)
                        VALUES($1,$2,$3,$4,$5,$6,'validated','metric-test')''',uuid4(),doc,entity,key,date,span)
            await db['writer'].execute('''INSERT INTO evidence.coverage(document_id,concept_id,concept_version,content_hash,status,extraction_version)
                SELECT document_id,$2,1,content_hash,'complete','metric-test' FROM evidence.documents WHERE document_id=$1''',doc,key)
    async with db['reader'].transaction(readonly=True):
        await install_scope(db['reader'],db['scope'])
        store = EvidenceStore(db['reader'],db['scope']); concepts = await store.catalog()
        for year,expected,eligible,missing in [(2024,900,3,1),(2026,2700,2,0)]:
            op = QueryOperation(output_id=f'y{year}',operation='average_duration',subject_type=db['type'].concept_id,
                start_predicate_id=start_key,end_predicate_id=end_key,allow_observed_subset=True,
                occurred_after=datetime(year,1,1,tzinfo=timezone.utc),occurred_before=datetime(year+1,1,1,tzinfo=timezone.utc))
            result = await store.execute(op,concepts)
            assert float(result.rows[0]['value']) == expected
            assert result.metric.observed_records == 2 and result.metric.eligible_records == eligible
            assert result.metric.missing_records == missing
            # Two original fixture widgets also have no occurrence time.
            assert result.metric.unknown_cohort_records == 3


@pytest.mark.asyncio
async def test_unseen_concept_uses_reviewed_binding_and_exact_source(evidence_db):
    from src.evidence.catalog import (BindingProposal, SchemaProposal, import_bound_record,
                                     profile_records, publish_bindings)
    db = evidence_db; namespace = f'novel-{uuid4()}'
    documentation = 'Sensor lag means observed notification delay in seconds per sensor reading.'
    doc = await put_document(db['writer'],source_key=str(uuid4()),text=documentation,case_ids=db['scope'].case_ids)
    records = [{'id':'A','lag':8},{'id':'B','lag':12}]
    profile = profile_records(records)
    proposal = SchemaProposal(entity_label='Sensor reading',entity_meaning='A source observation',grain='One sensor reading',
        fields=[BindingProposal(pointer='/lag',label='Notification lag',meaning=documentation,grain='One sensor reading',
            transform='number',unit='seconds',evidence_quote=documentation)])
    candidate = await publish_bindings(db['writer'],document_id=doc,namespace=namespace,profile=profile,
        proposal=proposal,documentation=documentation,reviewer='fixture-reviewer',decisions={})
    assert candidate['published'] == 0
    published = await publish_bindings(db['writer'],document_id=doc,namespace=namespace,profile=profile,
        proposal=proposal,documentation=documentation,reviewer='fixture-reviewer',decisions={'__entity__':True,'/lag':True})
    assert published['published'] == 1
    bindings = await db['writer'].fetch('SELECT * FROM evidence.source_bindings WHERE profile_id=$1',published['profile_id'])
    for record in records:
        async with db['writer'].transaction():
            imported = await import_bound_record(db['writer'],namespace=namespace,record_key=record['id'],record=record,
                schema_fingerprint=profile['schema_fingerprint'],bindings=bindings,case_ids=db['scope'].case_ids)
            assert imported['assertions'] == 1 and imported['failed'] == 0
    async with db['reader'].transaction(readonly=True):
        await install_scope(db['reader'],db['scope'])
        store = EvidenceStore(db['reader'],db['scope']); concepts = await store.catalog()
        result = await store.execute(QueryOperation(output_id='novel',operation='average',subject_type=bindings[0]['subject_type'],
            predicate_id=bindings[0]['concept_id'],source_collections=[namespace]),concepts)
        assert float(result.rows[0]['value']) == 10 and result.coverage.complete
        assert {span.text for span in result.citations} == {'8','12'}
        inferred = await store.execute(QueryOperation(output_id='inferred',operation='average',
            subject_type=bindings[0]['subject_type'],predicate_id=bindings[0]['concept_id']),concepts)
        assert float(inferred.rows[0]['value']) == 10
        assert inferred.coverage.population_documents == 2
        assert inferred.coverage.complete and inferred.status == 'answered'
    assert await db['reader'].fetchval('SELECT count(*) FROM evidence.source_bindings') == 0
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await db['reader'].execute("UPDATE evidence.source_bindings SET status='validated'")


@pytest.mark.asyncio
async def test_attachment_revision_scope_and_revocation_apply_to_reader(evidence_db):
    from src.evidence.attachments import sync_attachments
    db=evidence_db; scope=db['scope']; attachment_id=str(uuid4())
    attachment={'attachment_id':attachment_id,'user_id':str(scope.user_id),
        'session_id':str(scope.session_id),'filename':'sensor.txt','extracted_text':'Original sensor evidence.'}
    docs=await sync_attachments(db['writer'],scope,[attachment])
    assert len(docs)==1
    assert await sync_attachments(db['writer'],scope,[attachment])==docs
    async def visible(current):
        async with db['reader'].transaction(readonly=True):
            await install_scope(db['reader'],current)
            return await db['reader'].fetch('SELECT document_id,content FROM evidence.documents WHERE source_key=$1',
                f'attachment:{scope.session_id}:{attachment_id}')
    assert len(await visible(scope))==1
    assert not await visible(scope.model_copy(update={'session_id':uuid4()}))
    assert not await visible(scope.model_copy(update={'user_id':uuid4()}))
    attachment['extracted_text']='Revised sensor evidence.'
    revisions=await sync_attachments(db['writer'],scope,[attachment])
    assert revisions!=docs and len(await visible(scope))==2
    async with db['reader'].transaction(readonly=True):
        await install_scope(db['reader'],scope)
        store=EvidenceStore(db['reader'],scope)
        result=await store.search(QueryOperation(output_id='attachment',operation='search',text='sensor',source_collections=['session_attachments']))
        assert {s.document_id for s in result.citations}==set(revisions)
        assert all(s.text=='Revised sensor evidence.' for s in result.citations)
    await sync_attachments(db['writer'],scope,[])
    assert not await visible(scope)
    async with db['reader'].transaction(readonly=True):
        await install_scope(db['reader'],scope)
        assert await db['reader'].fetchval('SELECT count(*) FROM evidence.spans WHERE document_id=ANY($1::uuid[])',docs+revisions)==0
    # Original evidence is retained for controlled administrative review.
    assert await db['writer'].fetchval('SELECT count(*) FROM evidence.documents WHERE document_id=ANY($1::uuid[])',docs+revisions)==2


@pytest.mark.asyncio
async def test_durable_service_restores_conversation_and_fences_stale_attempt(evidence_db,monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.evidence import service
    from src.evidence.answer import render
    from src.evidence.types import QuestionPlan
    db=evidence_db; scope=db['scope']; states=[]
    @asynccontextmanager
    async def connection(current,write=False):
        async with db['reader'].transaction(readonly=not write):
            await install_scope(db['reader'],current)
            yield db['reader']
    prepared=SimpleNamespace(scope=scope,history=[],profile={},attachments=[])
    monkeypatch.setattr(service.database,'connection',connection)
    monkeypatch.setattr(service,'prepare',AsyncMock(return_value=prepared))
    monkeypatch.setattr(service,'reauthorize',AsyncMock(return_value=scope))
    monkeypatch.setattr(service,'ingest_session_attachments',AsyncMock())
    gateway=SimpleNamespace(save_message=AsyncMock(),touch_session=AsyncMock())
    op=QueryOperation(output_id='quantity',operation='sum',subject_type=db['type'].concept_id,
        predicate_id=db['quantity'].concept_id)
    async with connection(scope) as conn:
        store=EvidenceStore(conn,scope)
        checked=await store.execute(op,await store.catalog())
    class ControlledRuntime:
        def __init__(self,store):pass
        async def run(self,run_id,question,current,state,*args,**kwargs):
            states.append(state)
            answer=render(run_id,QuestionPlan(question=question,standalone_question=question,outputs=[op]),[checked],current)
            if question=='stale':
                async with connection(scope,write=True) as conn:
                    await conn.execute('UPDATE evidence.runs SET attempts=attempts+1 WHERE run_id=$1',run_id)
            return answer
    monkeypatch.setattr(service,'Runtime',ControlledRuntime)
    user=SimpleNamespace(id=scope.user_id)
    for message in ['first','follow-up']:
        run_id,_,task=await service.start(SimpleNamespace(message=message),user,gateway)
        answer=await task
        assert answer.status=='answered' and '5 items' in answer.text
        saved=await service.get_run(scope,run_id)
        assert saved['response']['claims'] and saved['lease_until'] is None
    assert states[0].revision==0 and states[1].revision==1
    assert states[1].plan.outputs[0].predicate_id==db['quantity'].concept_id
    assert float(states[1].previous_results[0]['rows'][0]['value'])==5
    stale,_,task=await service.start(SimpleNamespace(message='stale'),user,gateway)
    rejected=await task
    assert rejected.status=='unavailable'
    saved=await service.get_run(scope,stale)
    assert saved['response'] is None and saved['attempts']==2


@pytest.mark.asyncio
async def test_authenticated_chat_adapters_reload_sources_and_session_isolation(evidence_db,monkeypatch):
    import json
    import httpx
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src import main
    from src.api import evidence,sessions
    from src.auth import jwt
    from src.evidence import service
    from src.evidence.runtime import Runtime
    from src.evidence.types import QuestionPlan
    db=evidence_db; scope=db['scope']; history=[]; saved_session={}
    cfg=dotenv_values(Path(__file__).resolve().parents[1]/'.env.evidence-test')
    @asynccontextmanager
    async def connection(current,write=False):
        conn=await asyncpg.connect(cfg['EVIDENCE_QUERY_DATABASE_URL'])
        await _init(conn)
        try:
            async with conn.transaction(readonly=not write):
                await install_scope(conn,current)
                yield conn
        finally:await conn.close()
    async def create_session(session,user,title,project,case):
        saved_session.update(user_id=user,session_id=session,title=title,project_id=project,case_id=case)
    async def save_message(session,role,content,**kwargs):
        history.append({'role':role,'content':content,**kwargs})
    user={'id':scope.user_id,'role':'supervisor'}
    gateway=SimpleNamespace(get_user_by_id=AsyncMock(return_value=user),
        get_session=AsyncMock(side_effect=lambda _:dict(saved_session) if saved_session else None),
        get_cases=AsyncMock(return_value=[{'case_id':c} for c in scope.case_ids]),
        get_attachments_for_session=AsyncMock(return_value=[]),
        get_session_history=AsyncMock(side_effect=lambda _:list(history)),
        get_user_context_profile=AsyncMock(return_value={}),create_session=create_session,
        save_message=save_message,touch_session=AsyncMock())
    getter=AsyncMock(return_value=gateway)
    for module in (main,evidence,sessions,jwt):monkeypatch.setattr(module,'get_gateway',getter)
    monkeypatch.setattr(main.config,'EVIDENCE_RUNTIME_ENABLED',True)
    monkeypatch.setattr(service.database,'connection',connection)
    monkeypatch.setattr(service,'ingest_session_attachments',AsyncMock())
    op=QueryOperation(output_id='quantity',operation='sum',subject_type=db['type'].concept_id,
        predicate_id=db['quantity'].concept_id)
    class Planner:
        diagnostics=[]
        async def structured(self,contract,system,payload,**kwargs):
            if contract.__name__=='QueryUnderstanding':return contract(language='en')
            return QuestionPlan(question='quantity?',standalone_question='quantity?',outputs=[op])
    monkeypatch.setattr(service,'Runtime',lambda store:Runtime(store,Planner()))
    token=jwt.create_access_token({'sub':str(scope.user_id)})
    transport=httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
        payload={'session_id':str(scope.session_id),'message':'quantity?'}
        assert (await client.post('/api/chat',json=payload)).status_code==403
        client.cookies.set('access_token',token);client.cookies.set('csrf_token','fixture-csrf')
        client.headers['x-csrf-token']='fixture-csrf'
        response=await client.post('/api/chat',json=payload)
        assert response.status_code==200
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        complete=next(e for e in events if e.get('step')=='response' and e.get('status')=='done')
        assert complete['answer_status']=='answered'
        run_id=complete['run_id']
        restored=(await client.get('/api/evidence/runs/'+run_id)).json()
        assert restored['status']=='answered' and '5 items' in restored['response']['text']
        span=restored['response']['results'][0]['citations'][0]
        source=await client.get('/api/evidence/runs/'+run_id+'/sources/'+span['span_id'])
        assert source.status_code==200 and source.json()['text']==span['text']
        reload=await client.get('/api/sessions/'+str(scope.session_id))
        assert reload.status_code==200 and '5 items' in reload.text
        adapted=await client.post('/api/ask',json={'session_id':str(scope.session_id),'question':'quantity again?'})
        assert adapted.status_code==200 and adapted.json()['answer_status']=='answered'
        assert '5 items' in adapted.json()['answer']
        # A second authenticated identity does not inherit the first user's session.
        other=uuid4();gateway.get_user_by_id.return_value={'id':other,'role':'supervisor'}
        client.cookies.set('access_token',jwt.create_access_token({'sub':str(other)}))
        assert (await client.get('/api/evidence/runs/'+run_id)).status_code==404
        assert (await client.get('/api/sessions/'+str(scope.session_id))).status_code==403


@pytest.mark.asyncio
async def test_reviewed_nested_entities_measurements_and_relations_need_no_domain_code(evidence_db):
    from src.evidence.catalog import (SchemaProposal,EntityBindingProposal,RelationBindingProposal,
        BindingProposal,profile_records,publish_bindings,import_bound_record)
    db=evidence_db;namespace='nested-'+str(uuid4())
    documentation='Each packet contains sensor readings. Each reading has a sensor name and numeric lag in seconds. The packet contains each reading.'
    record={'readings':[{'name':'Same name','lag':8},{'name':'Same name','lag':12}]}
    profile=profile_records([record])
    proposal=SchemaProposal(entity_label='Packet',entity_meaning='A source packet',grain='One packet',
        entities=[EntityBindingProposal(selector='/readings/*',label_pointer='/name',label='Reading',
            meaning='A separate reading in one packet',grain='One reading',evidence_quote=documentation)],
        fields=[BindingProposal(pointer='/readings/*/lag',subject_selector='/readings/*',label='Lag',
            meaning='Observed lag',grain='One reading',transform='number',unit='seconds',evidence_quote=documentation)],
        relations=[RelationBindingProposal(label='Contains',meaning='Packet contains a reading',grain='One containment',
            object_selector='/readings/*',evidence_quote=documentation)])
    doc=await put_document(db['writer'],source_key=str(uuid4()),text=documentation,case_ids=db['scope'].case_ids)
    promoted=await publish_bindings(db['writer'],document_id=doc,namespace=namespace,profile=profile,proposal=proposal,
        documentation=documentation,reviewer='independent fixture review',
        decisions={'__entity__':True,'entity:/readings/*':True,'/readings/*/lag':True,'relation:Contains':True})
    assert promoted['published']==3
    bindings=await db['writer'].fetch('SELECT * FROM evidence.source_bindings WHERE profile_id=$1',promoted['profile_id'])
    imported=await import_bound_record(db['writer'],namespace=namespace,record_key='packet-A',record=record,
        schema_fingerprint=profile['schema_fingerprint'],bindings=bindings,case_ids=db['scope'].case_ids)
    assert imported['failed']==0 and imported['assertions']==4
    entities=[b for b in bindings if b['review'].get('binding_kind')=='entity']
    measure=next(b for b in bindings if b['pointer']=='/readings/*/lag')
    relation=next(b for b in bindings if b['review'].get('binding_kind')=='relation')
    async with db['reader'].transaction(readonly=True):
        await install_scope(db['reader'],db['scope'])
        store=EvidenceStore(db['reader'],db['scope']);catalog=await store.catalog()
        average=await store.execute(QueryOperation(output_id='mean',operation='average',subject_type=entities[0]['concept_id'],
            predicate_id=measure['concept_id']),catalog)
        assert float(average.rows[0]['value'])==10 and average.coverage.complete
        assert {s.text for s in average.citations}=={'8','12'}
        count=await store.execute(QueryOperation(output_id='readings',operation='count_entities',subject_type=entities[0]['concept_id']),catalog)
        assert int(count.rows[0]['value'])==2  # Repeated names never merge source identities.
        graph=await store.execute(QueryOperation(output_id='links',operation='network',relationship_path=[relation['concept_id']]),catalog)
        assert len(graph.rows)==2 and all(r['predicate_label']=='Contains' for r in graph.rows)


@pytest.mark.asyncio
async def test_projection_cache_is_scoped_and_source_revocation_invalidates_it(evidence_db,monkeypatch):
    from src.evidence.store import ScopedStore
    from src.evidence import database
    from contextlib import asynccontextmanager
    db=evidence_db;scope=db['scope'];relation=uuid4()
    await db['writer'].execute('''INSERT INTO evidence.concepts(concept_id,label,kind,meaning,value_type,grain,origin_document_id,status)
        VALUES($1,'Documented link','relation','A sourced link','entity','One link',$2,'validated')''',relation,db['docs'][0])
    span=await db['writer'].fetchval('SELECT span_id FROM evidence.spans WHERE document_id=$1 LIMIT 1',db['docs'][0])
    target=uuid4()
    await db['writer'].execute('''INSERT INTO evidence.entities(entity_id,document_id,concept_id,label,span_id,identity_status)
        VALUES($1,$2,$3,'Target',$4,'confirmed')''',target,db['docs'][0],db['type'].concept_id,span)
    await db['writer'].execute('''INSERT INTO evidence.assertions(assertion_id,document_id,subject_id,predicate_id,object_entity_id,span_id,status,extraction_version)
        VALUES($1,$2,$3,$4,$5,$6,'validated','fixture')''',uuid4(),db['docs'][0],db['entities'][0],relation,target,span)
    @asynccontextmanager
    async def connection(current,write=False):
        async with db['reader'].transaction(readonly=not write):
            await install_scope(db['reader'],current)
            yield db['reader']
    monkeypatch.setattr(database,'connection',connection)
    store=ScopedStore(scope);catalog=await store.catalog()
    op=QueryOperation(output_id='network',operation='network',relationship_path=[relation])
    first=await store.execute(op,catalog);cached=await store.execute(op,catalog)
    assert len(first.rows)==1 and cached.result_id==first.result_id
    async with connection(scope.model_copy(update={'session_id':uuid4()})) as conn:
        assert await conn.fetchval('SELECT count(*) FROM evidence.projections')==0
    async with connection(scope.model_copy(update={'case_ids':scope.case_ids[:1]})) as conn:
        assert await conn.fetchval('SELECT count(*) FROM evidence.projections')==0
    await db['writer'].execute('''UPDATE evidence.documents SET source_metadata=source_metadata || '{"revision_note":"changed"}'::jsonb
        WHERE document_id=$1''',db['docs'][0])
    revised=await store.execute(op,await store.catalog())
    assert revised.result_id!=first.result_id and len(revised.rows)==1
    await db['writer'].execute("UPDATE evidence.documents SET case_ids=ARRAY['outside-scope']::text[] WHERE document_id=$1",db['docs'][0])
    async with connection(scope) as conn:
        assert await conn.fetchval('SELECT count(*) FROM evidence.projections')==0
    from src.evidence.compiler import InvalidPlan
    with pytest.raises(InvalidPlan):await store.execute(op,await store.catalog())


@pytest.mark.asyncio
async def test_foreground_cancellation_interrupts_blocked_model_work(evidence_db,monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.evidence import service
    db=evidence_db;scope=db['scope'];started=asyncio.Event();stopped=asyncio.Event()
    cfg=dotenv_values(Path(__file__).resolve().parents[1]/'.env.evidence-test')
    @asynccontextmanager
    async def connection(current,write=False):
        conn=await asyncpg.connect(cfg['EVIDENCE_QUERY_DATABASE_URL']);await _init(conn)
        try:
            async with conn.transaction(readonly=not write):
                await install_scope(conn,current);yield conn
        finally:await conn.close()
    monkeypatch.setattr(service.database,'connection',connection)
    monkeypatch.setattr(service,'prepare',AsyncMock(return_value=SimpleNamespace(scope=scope,history=[],profile={},attachments=[])))
    monkeypatch.setattr(service,'reauthorize',AsyncMock(return_value=scope))
    monkeypatch.setattr(service,'ingest_session_attachments',AsyncMock())
    class Blocked:
        def __init__(self,store):pass
        async def run(self,*args,**kwargs):
            started.set()
            try:await asyncio.Event().wait()
            finally:stopped.set()
    monkeypatch.setattr(service,'Runtime',Blocked)
    gateway=SimpleNamespace(save_message=AsyncMock(),touch_session=AsyncMock())
    run_id,_,task=await service.start(SimpleNamespace(message='cancel this synthetic request'),SimpleNamespace(id=scope.user_id),gateway)
    await asyncio.wait_for(started.wait(),2)
    await db['writer'].execute('UPDATE evidence.runs SET cancel_requested=true WHERE run_id=$1',run_id)
    answer=await asyncio.wait_for(task,2)
    assert answer.status=='cancelled' and stopped.is_set() and not answer.claims
    saved=await service.get_run(scope,run_id)
    assert saved['status']=='cancelled' and saved['lease_until'] is None


@pytest.mark.asyncio
async def test_exhausted_worker_leases_release_conversation(evidence_db):
    from scripts.evidence_worker import expire_unrecoverable
    db=evidence_db;scope=db['scope'];run=uuid4()
    await db['writer'].execute('''INSERT INTO evidence.runs(run_id,user_id,session_id,scope,question,status,attempts,lease_until)
        VALUES($1,$2,$3,$4,'synthetic stalled request','running',3,now()-interval '1 second')''',
        run,scope.user_id,scope.session_id,scope.model_dump(mode='json'))
    await expire_unrecoverable(db['writer'])
    row=await db['writer'].fetchrow('SELECT status,response FROM evidence.runs WHERE run_id=$1',run)
    assert row['status']=='unavailable' and 'exhausted' in row['response']['text']
    # The partial unique index must now permit a new foreground request.
    await db['writer'].execute('''INSERT INTO evidence.runs(run_id,user_id,session_id,scope,question)
        VALUES($1,$2,$3,$4,'new request')''',uuid4(),scope.user_id,scope.session_id,scope.model_dump(mode='json'))


@pytest.mark.asyncio
async def test_crashed_foreground_process_is_recovered_from_durable_lease(evidence_db,monkeypatch):
    import asyncio
    import json
    import subprocess
    import sys
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.evidence import service
    from src.evidence.types import Answer,ConversationState
    db=evidence_db;scope=db['scope'];run=uuid4()
    state={'conversation':ConversationState(scope_fingerprint=scope.fingerprint()).model_dump(mode='json')}
    await db['writer'].execute('''INSERT INTO evidence.runs(run_id,user_id,session_id,scope,question,state)
        VALUES($1,$2,$3,$4,'synthetic recovery question',$5)''',run,scope.user_id,scope.session_id,scope.model_dump(mode='json'),state)
    child_code='''import asyncio,json,os
from pathlib import Path
from dotenv import dotenv_values
cfg=dotenv_values(Path('.env.evidence-test'))
for key in ('EVIDENCE_QUERY_DATABASE_URL','EVIDENCE_INGEST_DATABASE_URL'):os.environ[key]=cfg[key]
from uuid import UUID
from src.evidence import service
from src.evidence.types import Scope
class Crash:
    def __init__(self,store):pass
    async def run(self,*args,**kwargs):os._exit(23)
service.Runtime=Crash
asyncio.run(service.perform(UUID(os.environ['QA_RUN']),Scope.model_validate_json(os.environ['QA_SCOPE']),None))
'''
    child_env=dict(os.environ,QA_RUN=str(run),QA_SCOPE=json.dumps(scope.model_dump(mode='json')))
    result=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',child_code],
        cwd=Path(__file__).resolve().parents[1],env=child_env,capture_output=True,timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert result.returncode==23
    row=await db['writer'].fetchrow('SELECT status,attempts,response FROM evidence.runs WHERE run_id=$1',run)
    assert row['status']=='running' and row['attempts']==1 and row['response'] is None
    # Advance the lease rather than sleeping for 100 seconds in a test.
    await db['writer'].execute("UPDATE evidence.runs SET lease_until=now()-interval '1 second' WHERE run_id=$1",run)
    @asynccontextmanager
    async def connection(current,write=False):
        async with db['reader'].transaction(readonly=not write):
            await install_scope(db['reader'],current);yield db['reader']
    monkeypatch.setattr(service.database,'connection',connection)
    monkeypatch.setattr(service,'reauthorize',AsyncMock(return_value=scope))
    class Recovered:
        def __init__(self,store):pass
        async def run(self,run_id,*args,**kwargs):return Answer(run_id=run_id,status='clarification',text='Which record?')
    monkeypatch.setattr(service,'Runtime',Recovered)
    answer=await service.perform(run,scope,SimpleNamespace(save_message=AsyncMock(),touch_session=AsyncMock()))
    assert answer.status=='clarification'
    saved=await service.get_run(scope,run)
    assert saved['attempts']==2 and saved['lease_until'] is None and saved['response']['text']=='Which record?'


@pytest.mark.asyncio
@pytest.mark.parametrize('format',['pdf','docx','xlsx'])
async def test_reports_are_built_from_checked_results_and_stored_for_the_run(evidence_db,monkeypatch,format):
    from contextlib import asynccontextmanager
    from src.evidence import reports
    from src.evidence.answer import render
    from src.evidence.types import QuestionPlan
    db=evidence_db;scope=db['scope'];run=uuid4()
    @asynccontextmanager
    async def connection(current,write=False):
        async with db['reader'].transaction(readonly=not write):
            await install_scope(db['reader'],current);yield db['reader']
    monkeypatch.setattr(reports,'connection',connection)
    await db['writer'].execute('''INSERT INTO evidence.runs(run_id,user_id,session_id,scope,question)
        VALUES($1,$2,$3,$4,'synthetic cited report')''',run,scope.user_id,scope.session_id,scope.model_dump(mode='json'))
    op=QueryOperation(output_id='quantity',operation='sum',subject_type=db['type'].concept_id,predicate_id=db['quantity'].concept_id)
    async with connection(scope) as conn:
        store=EvidenceStore(conn,scope);result=await store.execute(op,await store.catalog())
    answer=render(run,QuestionPlan(question='report?',standalone_question='report?',outputs=[op],output_format=format),[result],scope)
    created=await reports.create_report(answer,scope)
    assert created.artifact_url and created.claims==answer.claims
    artifact=await db['writer'].fetchrow('SELECT path,format FROM evidence.artifacts WHERE run_id=$1',run)
    path=Path(artifact['path']).resolve()
    assert path.is_file() and path.stat().st_size>100 and artifact['format']==format
    assert (Path(__file__).resolve().parents[1]/'data/generated').resolve() in path.parents
    if format=='pdf':assert path.read_bytes().startswith(b'%PDF')
    else:
        from zipfile import ZipFile
        with ZipFile(path) as archive:
            member='word/document.xml' if format=='docx' else 'xl/worksheets/sheet1.xml'
            content=archive.read(member).decode()
            assert '5 items' in content


@pytest.mark.asyncio
async def test_preparation_deadline_releases_durable_session_lock(evidence_db,monkeypatch):
    import asyncio
    import time
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from src.evidence import service
    db=evidence_db;scope=db['scope']
    @asynccontextmanager
    async def connection(current,write=False):
        async with db['reader'].transaction(readonly=not write):
            await install_scope(db['reader'],current);yield db['reader']
    monkeypatch.setattr(service.database,'connection',connection)
    monkeypatch.setattr(service,'prepare',AsyncMock(return_value=SimpleNamespace(
        scope=scope,history=[],profile={},attachments=[])))
    monkeypatch.setattr(service,'ingest_session_attachments',AsyncMock())
    monkeypatch.setattr(service,'_run_deadline',lambda:time.monotonic()+.2)
    async def blocked_mirror(*args,**kwargs):await asyncio.Event().wait()
    gateway=SimpleNamespace(save_message=blocked_mirror)
    with pytest.raises(HTTPException) as exc:
        await service.stream(SimpleNamespace(message='synthetic preparation deadline'),None,gateway)
    assert exc.value.status_code==504
    row=await db['writer'].fetchrow('SELECT status,cancel_requested FROM evidence.runs WHERE session_id=$1',scope.session_id)
    assert row['status']=='cancelled' and row['cancel_requested']


@pytest.mark.asyncio
async def test_a_wrong_field_cannot_publish_a_confident_zero(evidence_db):
    """A count of 0 over a COMPLETE population is the most authoritative
    thing this system says -- and it is exactly what a wrong field
    produces.

    MEASURED on the live corpus, both answered `0` at coverage 73/73:

      "how many cases involve the PPC legal code"
         filtered `statute section code` contains 'PPC'. That concept
         holds 218 values that look like 13, 14, 148, 279 -- PPC is the
         ACT. Truth: 61 of 73.

      "how many people are recorded as accused"
         filtered on `accused in`, which has 94 recorded links. Truth: 92.

    If the filtered concept holds values and the filter matched none of
    them, the population is not empty: the plan is asking the wrong
    question. The refusal names the concept, how many values it holds and
    what they look like, so the planning ladder can correct it.

    A concept with no values anywhere is a genuine absence and still
    publishes its zero.
    """
    from src.evidence.compiler import InvalidPlan
    from src.evidence.types import PredicateFilter

    db = evidence_db
    async with db["reader"].transaction(readonly=True):
        await install_scope(db["reader"], db["scope"])
        store = EvidenceStore(db["reader"], db["scope"])
        catalog = await store.catalog()
        measured = [c for c in catalog.values()
                    if c.kind == "property" and c.value_type == "text"]
        subject = next((c for c in catalog.values() if c.kind == "entity_type"), None)
        if not measured or subject is None:
            pytest.skip("fixture has no text property to filter on")

        op = QueryOperation(
            output_id="wrong", operation="count_entities",
            subject_type=subject.concept_id,
            filters=[PredicateFilter(predicate_id=measured[0].concept_id,
                                     operator="contains",
                                     value="a-value-no-record-carries")],
            requires_complete=True)
        try:
            result = await store.execute(op, catalog)
        except InvalidPlan as exc:
            assert "the field is wrong" in str(exc)
            assert measured[0].label in str(exc)
        else:
            # Only acceptable when that concept genuinely holds no values.
            assert result.rows and result.rows[0]["value"] == 0
