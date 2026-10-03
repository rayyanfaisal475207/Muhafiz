from datetime import datetime,timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.evidence.answer import render,validate_claim
from src.evidence.compiler import InvalidPlan,compile_operation
from src.evidence.ingestion import DiscoveredConcept,DiscoveredFact,FieldReview,check_fact,locate,numeric_token
from src.evidence.types import Claim,Concept,Coverage,EvidenceResult,QuestionPlan,QueryOperation,Scope,SourceSpan


def concept(kind="entity_type",value_type="text",unit=None):
    return Concept(concept_id=uuid4(),label="Invented domain type",kind=kind,
                   meaning="A source-defined item",grain="One distinct item",value_type=value_type,unit=unit)


def test_result_cannot_support_another_question_output():
    scope=Scope(user_id=uuid4(),session_id=uuid4())
    c=concept()
    op=QueryOperation(output_id="stations",operation="count_entities",subject_type=c.concept_id)
    result=EvidenceResult(output_id="stations",scope_fingerprint=scope.fingerprint(),operation=op,label="Stations",
                          rows=[{"value":19}],coverage=Coverage(population_documents=1,complete_documents=1))
    with pytest.raises(ValueError,match="measure"):
        validate_claim(Claim(result_id=result.result_id,output_id="weapons"),[result],scope)


@pytest.mark.parametrize("missing",["missing_values","unresolved_identities","disputed_assertions","truncated"])
def test_incomplete_measure_is_not_a_definitive_number(missing):
    scope=Scope(user_id=uuid4(),session_id=uuid4()); c=concept()
    op=QueryOperation(output_id="total",operation="count_entities",subject_type=c.concept_id)
    coverage=Coverage(population_documents=1,complete_documents=1,**{missing:True if missing=="truncated" else 1})
    result=EvidenceResult(output_id="total",scope_fingerprint=scope.fingerprint(),operation=op,label="Items",rows=[{"value":972}],coverage=coverage,status="partial")
    plan=QuestionPlan(question="count",standalone_question="count",outputs=[op])
    answer=render(uuid4(),plan,[result],scope)
    assert "972" not in answer.text
    with pytest.raises(ValueError):
        validate_claim(Claim(result_id=result.result_id,output_id="total"),[result],scope)


def test_foreign_scope_is_rejected():
    scope=Scope(user_id=uuid4(),session_id=uuid4()); c=concept()
    op=QueryOperation(output_id="x",operation="count_entities",subject_type=c.concept_id)
    result=EvidenceResult(output_id="x",scope_fingerprint="another-user",operation=op,label="Items")
    with pytest.raises(ValueError):
        render(uuid4(),QuestionPlan(question="q",standalone_question="q",outputs=[op]),[result],scope)


def test_sql_identifiers_and_functions_cannot_be_supplied():
    with pytest.raises(ValidationError):
        QueryOperation(output_id="x",operation="sql",text="SELECT * FROM users")
    with pytest.raises(ValidationError):
        QueryOperation(output_id="x",operation="list",table="users")


def test_unknown_concept_is_rejected_before_execution():
    op=QueryOperation(output_id="x",operation="count_entities",subject_type=uuid4())
    with pytest.raises(InvalidPlan):
        compile_operation(op,{})


@pytest.mark.parametrize("payload",["'; DROP TABLE users; --",'" FROM users',"Karachi_%", "لاہور", "30 بور"])
def test_filters_are_parameters_and_do_not_multiply_rows(payload):
    subject=concept(); predicate=concept("property")
    op=QueryOperation(output_id="x",operation="count_entities",subject_type=subject.concept_id,
                      filters=[{"predicate_id":predicate.concept_id,"value":payload}])
    query=compile_operation(op,{subject.concept_id:subject,predicate.concept_id:predicate})
    assert payload not in query.sql
    assert payload in query.params
    assert "EXISTS(SELECT 1 FROM evidence.assertions" in query.sql
    assert "count(DISTINCT" in query.sql


def test_sum_uses_quantities_not_record_count():
    predicate=concept("property","number","items")
    op=QueryOperation(output_id="x",operation="sum",predicate_id=predicate.concept_id)
    sql=compile_operation(op,{predicate.concept_id:predicate}).sql
    assert "sum(a.value_number)" in sql


def test_temporal_plan_distinguishes_valid_and_recorded_time():
    subject=concept(); now=datetime(2026,1,1,tzinfo=timezone.utc)
    op=QueryOperation(output_id="x",operation="timeline",subject_type=subject.concept_id,as_of=now,known_at=now)
    sql=compile_operation(op,{subject.concept_id:subject}).sql
    assert "recorded_at<=" in sql and "valid_from<=" in sql and "retracted_at>" in sql


def test_bore_is_not_quantity_even_with_a_real_quote():
    source="30 بور پستول بمعہ 6 گولیاں"
    concept=DiscoveredConcept(key="quantity",label="Quantity",kind="property",meaning="Number of physical weapons",grain="One recovery",value_type="number",unit="items")
    fact=DiscoveredFact(key="q",subject="p",predicate="quantity",value_number=30,unit="items",quote=source,start=0,value_quote="30",role_quote="بور")
    review=FieldReview(fact_key="q",entailed=False,correct_value_role=False,reason="30 is the bore, not a quantity")
    with pytest.raises(ValueError,match="meaning"):
        check_fact(fact,concept,source,review)


@pytest.mark.parametrize("token,expected",[("۳۰",30),("٣٠",30),("1,234",1234),("3.5",Decimal("3.5"))])
def test_source_numerals(token,expected):
    assert numeric_token(token)==expected


@pytest.mark.parametrize("token",["30 bore","nan","infinity","six",""])
def test_measurements_require_an_actual_numeric_token(token):
    with pytest.raises(ValueError): numeric_token(token)


def test_duplicate_quote_requires_an_occurrence():
    with pytest.raises(ValueError): locate("same same","same",3)
    assert locate("same same","same",5)==(5,9)


def test_source_extent_matches_unicode_characters():
    source="لاہور"
    SourceSpan(span_id=uuid4(),document_id=uuid4(),start=0,end=len(source),text=source)
    with pytest.raises(ValidationError):
        SourceSpan(span_id=uuid4(),document_id=uuid4(),start=0,end=1,text=source)


def test_cyclic_or_missing_output_dependencies_are_rejected():
    with pytest.raises(ValidationError):
        QuestionPlan(question="q",standalone_question="q",outputs=[{"output_id":"x","operation":"list","depends_on":["y"]}])


def test_a_period_that_cannot_be_applied_is_refused_not_dropped():
    """MEASURED: "How many FIRs were reported in June 2026?" answered 73 --
    the all-time total -- when the truth is 6, and reported coverage 73/73
    as though the figure were exact.

    count_entities counted every entity of the type without ever joining
    the assertions table, so both the predicate and the period were
    dropped in silence. A dropped constraint is worse than a refused one:
    the answer looks complete and is wrong.
    """
    import pytest
    from datetime import datetime, timezone
    from uuid import uuid4

    from src.evidence.types import QueryOperation

    bounds = {"occurred_after": datetime(2026, 6, 1, tzinfo=timezone.utc),
              "occurred_before": datetime(2026, 7, 1, tzinfo=timezone.utc)}

    # With the date predicate the period applies to, the plan is valid.
    op = QueryOperation(output_id="c", operation="count_entities",
                        subject_type=uuid4(), predicate_id=uuid4(), **bounds)
    assert op.occurred_after and op.predicate_id

    # Without it there is no date column to compare against: refuse.
    with pytest.raises(ValueError, match="date predicate"):
        QueryOperation(output_id="c", operation="count_entities",
                       subject_type=uuid4(), **bounds)


def test_a_relation_is_read_from_whichever_end_it_is_stored_on():
    """The same blind spot appeared on FOUR query surfaces, each time as a
    confident wrong answer rather than an error:

        traverse    "which cases is Kashif accused in"   -> nothing
        filters     "people where accused in exists"     -> 0   (truth 92)
        grouping    grouped by a relation                -> one null group
        count       "how many people are recorded as
                     accused"                            -> 0   (truth 92)

    `accused in` is recorded FIR --> person, so a person is never its
    subject. Requiring one asks whether a person has people accused in
    them. Which end a relation is stored from is an artefact of the
    source's shape, not a claim about the relationship.

    A PROPERTY is different and must NOT be reversed: its value hangs off
    the subject alone and there is no other end to read from.
    """
    from src.evidence.compiler import compile_operation
    from src.evidence.types import Concept, QueryOperation
    from uuid import uuid4

    person, accused_in, reported_at = uuid4(), uuid4(), uuid4()
    concepts = {
        person: Concept(concept_id=person, label="person", kind="entity_type",
                        meaning="a person", value_type="entity", grain="one person"),
        accused_in: Concept(concept_id=accused_in, label="accused in", kind="relation",
                            meaning="accused on a FIR", value_type="entity",
                            grain="one accused entry"),
        reported_at: Concept(concept_id=reported_at, label="reported at", kind="property",
                             meaning="when reported", value_type="date",
                             grain="one FIR record"),
    }

    relation = compile_operation(
        QueryOperation(output_id="a", operation="count_entities",
                       subject_type=person, predicate_id=accused_in), concepts).sql
    assert "m.subject_id=e.entity_id" in relation
    assert "m.object_entity_id=e.entity_id" not in relation
    inverse = compile_operation(QueryOperation(output_id="inverse", operation="count_entities",
        subject_type=person, predicate_id=accused_in, measure_direction="inverse"), concepts).sql
    assert "m.object_entity_id=e.entity_id" in inverse
    assert "m.subject_id=e.entity_id" not in inverse

    prop = compile_operation(
        QueryOperation(output_id="b", operation="count_entities",
                       subject_type=person, predicate_id=reported_at), concepts).sql
    assert "m.subject_id=e.entity_id" in prop
    assert "m.object_entity_id=e.entity_id" not in prop, (
        "a property has no second end and must not be reversed")

    # ── FIFTH SURFACE: a participation FILTER ─────────────────────────────
    #
    # The comment in the compiler said a relationship filter "matches from
    # either end". It did not: it matched ONE end, chosen by whether the
    # planner had set direction="inverse". MEASURED, on "how many people are
    # recorded as accused": the planner emitted the filter with no direction,
    # so it checked the subject side where a person never appears, returned
    # zero rows and rendered "No matching records in the checked population"
    # with status ANSWERED -- against 92 recorded accusations.
    #
    # `exists` asks whether the entity TAKES PART in the relation, which
    # cannot depend on which end the source stored it from. Naming a
    # counterpart is a different question and stays direction-sensitive.
    from src.evidence.types import PredicateFilter

    participation = compile_operation(
        QueryOperation(output_id="c", operation="list", subject_type=person,
                       filters=[PredicateFilter(predicate_id=accused_in,
                                                operator="exists")]), concepts).sql
    assert "f0.subject_id=e.entity_id" in participation
    assert "f0.object_entity_id=e.entity_id" in participation, (
        "participation must be satisfied from either end of the relation")

    absent = compile_operation(
        QueryOperation(output_id="d", operation="list", subject_type=person,
                       filters=[PredicateFilter(predicate_id=accused_in,
                                                operator="not_exists")]), concepts).sql
    assert "f0.subject_id=e.entity_id" in absent
    assert "f0.object_entity_id=e.entity_id" in absent, (
        "absence is the dual of participation and reads both ends too")

    # A PROPERTY still has no second end, participation or not.
    prop_exists = compile_operation(
        QueryOperation(output_id="e", operation="list", subject_type=person,
                       filters=[PredicateFilter(predicate_id=reported_at,
                                                operator="exists")]), concepts).sql
    assert "f0.subject_id=e.entity_id" in prop_exists
    assert "f0.object_entity_id=e.entity_id" not in prop_exists, (
        "a property has no second end and must not be reversed")

    # Naming a counterpart is direction-sensitive: which end holds the
    # counterpart IS the meaning of the filter, so it must not match both.
    named = compile_operation(
        QueryOperation(output_id="f", operation="list", subject_type=person,
                       filters=[PredicateFilter(predicate_id=accused_in,
                                                operator="eq",
                                                object_name="Kashif")]), concepts).sql
    assert not ("f0.subject_id=e.entity_id" in named
                and "f0.object_entity_id=e.entity_id" in named), (
        "a filter naming a counterpart must pin one end")


@pytest.mark.parametrize('ids,text', [(['Ahmed','Bilal'],''),(['Ahmed'],'Karachi'),(['Ahmed',str(uuid4())],'')])
def test_name_repair_cannot_drop_or_replace_other_filters(ids,text):
    from src.evidence.types import QueryOperation
    with pytest.raises(ValueError,match='no restriction may be dropped'):
        QueryOperation(output_id='people',operation='list',entity_ids=ids,text=text)
