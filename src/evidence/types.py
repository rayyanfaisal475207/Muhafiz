"""Public contracts. No model response is executable code or an authorization grant."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(Contract):
    user_id: UUID
    session_id: UUID
    case_ids: list[str] = Field(default_factory=list)
    project_id: UUID | None = None
    include_global: bool = True

    def fingerprint(self) -> str:
        import hashlib
        import json
        value = self.model_dump(mode="json")
        value["case_ids"] = sorted(set(self.case_ids))
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class SourceSpan(Contract):
    span_id: UUID
    document_id: UUID
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str
    title: str = ""

    @model_validator(mode="after")
    def valid_extent(self):
        if self.end <= self.start or len(self.text) != self.end - self.start:
            raise ValueError("span extent must match the original text")
        return self


class Concept(Contract):
    concept_id: UUID
    label: str = Field(min_length=1, max_length=200)
    kind: Literal["entity_type", "property", "relation", "event_type"]
    meaning: str = Field(min_length=1, max_length=2000)
    aliases: list[str] = Field(default_factory=list, max_length=40)
    value_type: Literal["text", "number", "boolean", "date", "entity"] = "text"
    unit: str | None = None
    grain: str = Field(min_length=1, max_length=500)
    version: int = Field(default=1, ge=1)
    recorded_subject_types: list[UUID] = Field(default_factory=list)
    recorded_object_types: list[UUID] = Field(default_factory=list)


Scalar = str | float | bool


class PredicateFilter(Contract):
    relationship_path: list[UUID] = Field(default_factory=list, max_length=4)
    predicate_id: UUID
    direction: Literal["forward", "inverse"] = "forward"
    name_match_mode: Literal["unique", "all_matches"] = "unique"
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "exists",
                      "not_exists", "in", "not_in"] = "eq"
    value: Scalar | None = None
    values: list[str] = Field(
        default_factory=list, max_length=40,
        description="For 'in' and 'not_in': the RECORDED values to match, "
                    "exactly as the records write them. A question's own word "
                    "is usually not one of them -- 'completed' is a reading of "
                    "the stored statuses, not a stored status -- so the runtime "
                    "resolves the asked word against the values that actually "
                    "occur and fills this. Do not invent entries here.")
    object_entity_id: UUID | None = Field(
        default=None,
        description="The related entity's UUID, when one is already known. "
                    "NEVER a name -- use object_name for that.")
    object_name: str | None = Field(
        default=None, max_length=200,
        description="The related entity's NAME as the question gives it, for "
                    "filters like 'the accused is Ahmed'. Matched across "
                    "scripts, so a Roman spelling reaches Urdu records. Use "
                    "this instead of object_entity_id when only a name is "
                    "known.")

    @field_validator('operator', mode='before')
    @classmethod
    def equivalent_operator_notation(cls, value):
        # Infrastructure algebra only: no domain terms or question routes.
        return {'=':'eq','==':'eq','!=':'ne','<>':'ne','>':'gt','>=':'gte','<':'lt','<=':'lte'}.get(value,value)

    @model_validator(mode="after")
    def value_required(self):
        """MEASURED: asked "who is Ahmed and is he nominated in any cases",
        the planner put the name into object_entity_id, which is a UUID
        field, and the plan was rejected twice before the runtime gave up.
        Told not to, it moved the same string into entity_ids. It was not
        misusing the contract so much as working around it: there was no
        way to say "the accused is Ahmed" without an id it could not know.
        """
        given = [x for x in (self.value, self.object_entity_id, self.object_name)
                 if x is not None]
        # `in`/`not_in` carry their values in `values`, not `value`. Without
        # this, every plan that correctly used the new operator was rejected
        # as "filter requires a value" -- MEASURED on the Urdu question
        # asking how many cases are completed, which the planner finally
        # planned correctly and which failed validation six times running.
        if self.operator in {"in", "not_in"}:
            if not [v for v in (self.values or []) if str(v).strip()]:
                raise ValueError("a value set filter requires values")
            return self
        if self.values:
            raise ValueError("values applies to the 'in' and 'not_in' operators")
        if self.operator not in {"exists", "not_exists"} and not given:
            raise ValueError("filter requires a value")
        if len(given) > 1:
            raise ValueError("use a literal, an entity id, or an entity name")
        return self


class ResultCell(Contract):
    output_id: str
    row_index: int = Field(default=0, ge=0)
    field: str = "value"


class QueryOperation(Contract):
    output_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    operation: Literal["search", "list", "count_entities", "count_assertions", "sum", "average", "average_duration", "timeline", "traverse", "network", "difference", "ratio"]
    operands: list[ResultCell] = Field(default_factory=list, max_length=2)
    name_match_mode: Literal["unique", "all_matches"] = "unique"
    subject_type: UUID | None = Field(default=None, description="Required for count_entities and average_duration: the catalog UUID of the entity_type whose records form the population.")
    predicate_id: UUID | None = None
    start_predicate_id: UUID | None = Field(default=None, description="Required for average_duration: catalog UUID of the starting date property. Do not substitute a filter or predicate_id.")
    end_predicate_id: UUID | None = Field(default=None, description="Required for average_duration: catalog UUID of the ending date property. Do not substitute a filter or predicate_id.")
    entity_ids: list[UUID] = Field(
        default_factory=list, max_length=100,
        description="UUIDs returned by an earlier lookup or output. NEVER a "
                    "person's name or any other text. To select by name, use "
                    "text.")
    filters: list[PredicateFilter] = Field(default_factory=list, max_length=12)
    relationship_path: list[UUID] = Field(default_factory=list, max_length=4)
    relationship_directions: list[Literal["forward", "inverse"]] = Field(default_factory=list, max_length=4)
    measure_direction: Literal["forward", "inverse"] = "forward"
    group_direction: Literal["forward", "inverse"] = "forward"
    group_by: UUID | None = Field(default=None,description="Property/relation ON each counted subject. Never an entity_type ID.")
    order_by: Literal["group", "value_asc", "value_desc"] = "group"
    source_scope: Literal["all", "case_documents", "global_documents"] = "all"
    source_collections: list[str] = Field(default_factory=list, max_length=20)
    text: str = Field(
        default="", max_length=2000,
        description="For search: what to retrieve. For list and the count "
                    "operations: selects the entities whose recorded name is "
                    "exactly this, so a person or place is found by NAME "
                    "without needing its id. Use this, not entity_ids, when "
                    "the question names someone.")
    as_of: datetime | None = None
    known_at: datetime | None = None
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)
    requires_complete: bool = True
    allow_observed_subset: bool = False
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    entities_from: str | None = None

    @model_validator(mode="before")
    @classmethod
    def a_name_in_an_id_field_is_still_a_name(cls, data):
        """A person's name written into entity_ids is a name, not an id.

        MEASURED, on "who is Ahmed and is he nominated in any cases during
        the last 2 months": the planner repeatedly wrote a literal string
        into entity_ids, which takes UUIDs. Told not to, it moved the same
        string into filters[].object_entity_id -- another UUID field. Both
        plans were rejected, the repair budget was spent, and the user saw
        "The local evidence service could not complete this request" for a
        question whose answer is in the records.

        It was not misusing the contract at random. It knew WHICH entity it
        meant and had no id to name it with. `text` selects entities by the
        name they are recorded under, across scripts, so the intent has a
        home: move it there rather than failing the plan.

        The entry is never simply discarded -- that would drop the filter
        and silently widen "cases where Ahmed is accused" into "all cases".
        A name already in `text` wins, and the stray entry is dropped only
        because the operation is then narrowed by that name anyway.
        """
        if not isinstance(data, dict):
            return data
        ids = data.get("entity_ids")
        # A single value where a list belongs is the same mistake in a
        # thinner disguise; MEASURED alongside the name-in-an-id-field case.
        if isinstance(ids, (str, UUID)):
            ids = [ids]
        if not isinstance(ids, list) or not ids:
            return data
        kept, names = [], []
        for item in ids:
            if isinstance(item, str):
                try:
                    UUID(item)
                except ValueError:
                    if item.strip():
                        names.append(item.strip())
                    continue
            kept.append(item)
        if names:
            unique=list(dict.fromkeys(names))
            existing=str(data.get('text') or '').strip()
            if kept or len(unique)!=1 or existing and existing.casefold()!=unique[0].casefold():
                raise ValueError('Names and entity IDs require explicit separate filters; no restriction may be dropped')
            data = dict(data)
            data["entity_ids"] = kept
            if not str(data.get("text") or "").strip():
                data["text"] = names[0][:2000]
        return data

    @model_validator(mode="after")
    def search_needs_enough_support(self):
        """A search that returns one passage cannot ground an answer.

        MEASURED, asking "What weapon was recorded in FIR 1001/26?": the
        planner bound the question correctly -- source_scope=case_documents,
        subject_type=FIR, filter fir_number='1001/26' -- and then set
        limit=1. One passage came back, narrative grounding rejected the
        draft for want of support, and the answer was "candidate sources
        were retrieved; an answer has not yet been established".

        The planning was right and the budget was wrong. Composition needs
        corroborating passages to quote from, and a single hit gives it
        nothing to check a claim against.

        The floor is raised rather than the request refused: the plan is
        sound, so it is given enough evidence to work with instead of
        being thrown away.
        """
        if self.operation == "search" and self.limit < 5:
            self.limit = 5
        return self

    @model_validator(mode="after")
    def a_period_needs_the_date_it_applies_to(self):
        """MEASURED: count_entities carried June 2026 bounds and no
        predicate, so there was no date column to compare them against.
        The bounds were dropped and the all-time total was returned as an
        exact, complete answer. A constraint that cannot be applied is a
        reason to refuse the plan, not to answer a different question."""
        if (self.operation == "count_entities"
                and (self.occurred_after or self.occurred_before)
                and not self.predicate_id):
            raise ValueError(
                "counting entities within a period needs the date predicate "
                "the period applies to")
        return self

    @model_validator(mode="after")
    def meaningful(self):
        if self.relationship_directions and len(self.relationship_directions) != len(self.relationship_path):
            raise ValueError("Every relationship hop needs one direction")
        if self.operation in {"difference", "ratio"}:
            if len(self.operands) != 2:
                raise ValueError("derived arithmetic requires exactly two result cells")
            if any((self.subject_type, self.predicate_id, self.filters, self.group_by, self.entity_ids,
                    self.occurred_after, self.occurred_before, self.relationship_path)):
                raise ValueError("derived arithmetic consumes checked cells, not a new population")
            self.depends_on = list(dict.fromkeys([*self.depends_on, *(cell.output_id for cell in self.operands)]))
        elif self.operands:
            raise ValueError("result operands apply only to derived arithmetic")
        if self.operation == "average_duration":
            if not self.start_predicate_id or not self.end_predicate_id or not self.subject_type:
                missing = [field for field in ("subject_type", "start_predicate_id", "end_predicate_id") if not getattr(self, field)]
                raise ValueError("average_duration missing required fields: " + ", ".join(missing) + "; use the actual entity_type and date-property UUIDs from the catalog")
            if self.start_predicate_id == self.end_predicate_id:
                raise ValueError("Duration endpoints must be distinct event-time meanings")
        elif self.start_predicate_id or self.end_predicate_id:
            raise ValueError("date pairs apply only to duration measures")
        if self.operation in {"sum", "average", "count_assertions"} and self.predicate_id is None:
            raise ValueError("the measure requires a predicate")
        if self.operation == "count_entities" and self.subject_type is None:
            raise ValueError("entity count requires an explicitly resolved type")
        # A search with no text is NOT rejected here. The plan knows the
        # question, so QuestionPlan.a_search_defaults_to_the_question fills
        # it in. See that validator for what this cost.
        if self.operation == "traverse" and not self.entity_ids:
            if not self.entities_from:
                raise ValueError("traversal requires resolved seed entities")
        if self.entities_from and self.entities_from not in self.depends_on:
            # Naming an output to take entities FROM is the dependency.
            #
            # MEASURED: "Which cases is Ahmed accused in?" planned
            # correctly -- find the person, then traverse from that output
            # -- and was rejected because `entities_from` named the step
            # without also repeating it in `depends_on`. The plan was
            # right and the bookkeeping was incomplete, so the question
            # went unanswered. Recording it is not a guess: the dependency
            # is exactly the output already named.
            if len(self.depends_on) >= 8:
                raise ValueError("entities_from must name an explicit dependency")
            self.depends_on = [*self.depends_on, self.entities_from]
        if self.group_by and self.operation not in {"sum", "average", "average_duration", "count_entities", "count_assertions"}:
            raise ValueError("grouping applies to measures only")
        if self.order_by != "group" and not self.group_by:
            raise ValueError("value ordering requires a grouped measure")
        if self.operation == "search" and (self.predicate_id or self.as_of or self.known_at
                or self.occurred_after or self.occurred_before):
            raise ValueError("search cannot apply measure or historical constraints; use typed filters or a timeline")
        if self.occurred_after and self.occurred_before and self.occurred_after > self.occurred_before:
            raise ValueError("time range is reversed")
        return self


class TermReading(Contract):
    """One term from the question, in every form the corpus might record it.

    Produced PER REQUEST. Nothing here is stored: the same term asked
    tomorrow is read again. That is the whole point -- a stored synonym
    table is a dictionary that rots, and it was the reason an English
    question could not reach Urdu records.
    """

    term: str = Field(min_length=1, max_length=120)
    variants: list[str] = Field(default_factory=list, max_length=24)
    role: Literal["entity", "attribute", "value", "measure", "time", "other"] = "other"


class QuestionPart(Contract):
    """One answerable question. A multi-part ask becomes several."""

    text: str = Field(min_length=1, max_length=2000)
    asks_for: Literal["value", "count", "list", "relationship", "narrative",
                      "comparison", "other"] = "other"


QUESTION_CATEGORIES = ("value", "count", "list", "relationship", "narrative",
                       "comparison", "existence", "other")


class QuestionClass(Contract):
    """What KIND of question this is, decided before it is read or planned.

    WHY THIS IS ITS OWN STEP

    Shape and category were previously a field on `QueryUnderstanding`:
    the same call that translated the terms also decided how many asks
    there were, and nothing routed on the answer. So a one-part question
    and a five-part question were read by the identical prompt, and a
    count and a narrative were translated the same way -- although a count
    needs the exact written form of a VALUE and a narrative needs the
    words a clerk would use to describe an event.

    Deciding first, in its own call, makes the decision inspectable and
    lets everything after it differ: the reading prompt is shaped by the
    category, and the plan is required to cover every ask when the shape
    says there are several.

    The categories are the ones the query algebra can already serve, not a
    second invented taxonomy -- `QuestionPart.asks_for` uses the same set,
    plus `existence` for "does X appear at all", which is answered by an
    exhaustive check rather than by a measure over a population.
    """

    shape: Literal["single", "multi"] = "single"
    category: Literal["value", "count", "list", "relationship", "narrative",
                      "comparison", "existence", "other"] = "other"
    asks: int = Field(default=1, ge=1, le=6,
                      description="How many separate answers the question demands.")
    reason: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def shape_and_count_must_agree(self):
        """A shape that contradicts its own count is not a classification.

        MEASURED: the model returned shape="multi" with asks=1 and
        shape="single" with asks=3. Both were accepted and both were then
        used to decide whether the plan had to cover several outputs, so a
        self-contradictory label silently chose the routing.

        `asks` is the number that can be checked against the parts the
        reader actually finds, so the shape is derived from it rather than
        trusted alongside it.
        """
        self.shape = "multi" if self.asks > 1 else "single"
        return self


class QueryUnderstanding(Contract):
    """What the question is asking, before anything is planned or retrieved.

    Three jobs, all per request:

      * READ each meaningful term and give every form the records might
        use -- the other script, the other language, singular and plural,
        the spelling a clerk would type.
      * SPLIT a question that asks several things into one ask each.
      * NAME the language to answer in.

    MEASURED, and the reason this exists: "vehicle theft" retrieved the
    Pakistan Criminal Procedure Code instead of the vehicle-theft FIRs,
    because every retriever matched the English words as typed against a
    corpus written in Urdu. Concepts carried hand-written aliases to paper
    over it -- a stored dictionary in the live path, which is exactly what
    this replaces.
    """

    language: Literal["en", "ur", "roman_ur"] = "en"
    requested_language: Literal["en", "ur", "roman_ur"] | None = Field(default=None, description="Only an explicitly requested response language; otherwise null.")
    parts: list[QuestionPart] = Field(default_factory=list, max_length=6)
    terms: list[TermReading] = Field(default_factory=list, max_length=20)
    unresolved: list[str] = Field(default_factory=list, max_length=20)

    def all_forms(self) -> list[str]:
        """Every written form worth searching for, the typed one first.

        A form carrying DEVANAGARI is dropped here rather than searched
        for. MEASURED: asked about 2026 and 2024, the model returned
        "دو हज़ार بیسیس" and "दो सौ छिहत्तर" -- Hindi spliced into Urdu
        script, and a spelled-out year at that. These records are written
        in Arabic script and Latin letters; a Devanagari form cannot occur
        in them, so proposing one is a guess that costs a lookup.

        The corpus check still runs on what survives. This only removes
        forms that are impossible by writing system, which is arithmetic
        about scripts, not a judgement about words.
        """
        seen: list[str] = []
        for reading in self.terms:
            for form in [reading.term, *reading.variants]:
                cleaned = " ".join(str(form).split())
                if not cleaned or cleaned in seen:
                    continue
                if any("ऀ" <= ch <= "ॿ" for ch in cleaned):
                    continue
                seen.append(cleaned)
        return seen


class QuestionPlan(Contract):
    question: str
    standalone_question: str
    language: Literal["en", "ur", "roman_ur"] = "en"
    continuation: bool = False
    outputs: list[QueryOperation] = Field(default_factory=list, max_length=8)
    clarification: str | None = None
    conversational_response: str | None = None
    discovery_terms: list[str] = Field(default_factory=list, max_length=8)
    entity_queries: list[str] = Field(default_factory=list, max_length=6)
    catalog_queries: list[str] = Field(default_factory=list, max_length=6)
    output_format: Literal["chat", "pdf", "docx", "xlsx"] = "chat"
    catalog_versions: dict[str, int] = Field(default_factory=dict)
    data_snapshot: str | None = None
    workflow_version: str = "2"

    @model_validator(mode="after")
    def a_search_defaults_to_the_question(self):
        """An unstated search text is the question itself.

        MEASURED, asking "who is Ahmed and is he nominated in any cases
        during the last 2 months": the planner produced THREE plans. The
        second validated cleanly. It was still discarded, because the
        first and third each carried a search operation whose `text` the
        model had not echoed, and "search requires text" failed the whole
        plan. With the repair budget spent the runtime gave up and the
        user saw "The local evidence service could not complete this
        request" -- for a question the system could answer.

        Rejecting a plan because the model did not repeat the question
        back is strictness that buys nothing: a search with no text of its
        own is a search for what was asked. Filling it is not a guess.

        An operation that is genuinely unanswerable still fails, further
        down, on the things that carry meaning -- predicate, subject type,
        seed entities.
        """
        fallback = (self.standalone_question or self.question or "").strip()
        if fallback:
            for output in self.outputs:
                if output.operation == "search" and not output.text.strip():
                    output.text = fallback[:2000]
        return self

    @model_validator(mode="after")
    def answers_in_the_language_asked(self):
        """Free text the model writes must be in the language of the plan.

        MEASURED: an Urdu question returned an English clarification --
        "Please specify what type of information you are looking for" --
        because render() translates its own scaffolding but passes
        model-written clarification and small-talk through verbatim.

        The prompt already asks for the right language. It was ignored, so
        the requirement lives here instead, where the runtime's existing
        single plan repair can act on it.

        Script, not vocabulary: an Urdu answer legitimately contains FIR
        numbers, section codes and "PPC", so the test is which script
        CARRIES the sentence, not whether Latin appears at all.
        """
        import re
        for field_name in ("clarification", "conversational_response"):
            text = getattr(self, field_name) or ""
            if len(text.strip()) < 12:
                continue
            arabic = len(re.findall(r"[؀-ۿݐ-ݿ]", text))
            latin = len(re.findall(r"[A-Za-z]", text))
            if self.language == "ur" and latin > arabic:
                raise ValueError(
                    f"{field_name} must be written in Urdu when the question "
                    f"was asked in Urdu")
            if self.language == "en" and arabic > latin:
                raise ValueError(
                    f"{field_name} must be written in English when the "
                    f"question was asked in English")
        return self

    @model_validator(mode="after")
    def dependency_order(self):
        seen = set()
        for op in self.outputs:
            if op.output_id in seen or not set(op.depends_on) <= seen:
                raise ValueError("output ids must be unique; dependencies must precede their consumers")
            seen.add(op.output_id)
        if not self.outputs and not (self.clarification or self.conversational_response or self.discovery_terms or self.entity_queries or self.catalog_queries):
            raise ValueError("a plan must answer, clarify, or request discovery")
        if self.conversational_response and self.outputs:
            raise ValueError("conversation and evidence outputs cannot be mixed")
        return self


class Coverage(Contract):
    population_documents: int = Field(default=0, ge=0)
    complete_documents: int = Field(default=0, ge=0)
    missing_values: int = Field(default=0, ge=0)
    unresolved_identities: int = Field(default=0, ge=0)
    disputed_assertions: int = Field(default=0, ge=0)
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)
    # Forms checked against the WHOLE corpus and found nowhere.
    #
    # This is a different fact from incomplete coverage and must not be
    # confused with it: "I looked in every recorded span, entity name and
    # value and this phrase is in none of them" is an exhaustive check,
    # whereas population/complete_documents describe how much of a filtered
    # population was measured. An absent literal therefore does NOT make
    # coverage incomplete -- it is the one case where a negative is
    # measured rather than inferred, so it can be stated plainly.
    absent_literals: list[str] = Field(default_factory=list, max_length=8)

    @property
    def complete(self) -> bool:
        return (self.population_documents > 0 and self.complete_documents == self.population_documents
                and not any((self.missing_values, self.unresolved_identities,
                             self.disputed_assertions, self.truncated)))

    def complete_for(self, operation) -> bool:
        """Whether this coverage supports publishing THIS operation's rows.

        Truncation ends an exhaustive TOTAL, because the total is the one
        number and a cut-off list cannot produce it. It does not end a
        RANKED GROUPED result: each group's count is computed over the whole
        population, so the rows that came back are exact, and truncation
        only says there are further groups below the ones shown.

        MEASURED, on "flag the suspects who appear most frequently": the
        grouped count returned 50 rows with exact counts and coverage 73/73,
        and every one was suppressed as "Partial evidence; this does not
        establish a complete total" because the 50-row limit set truncated.
        The ranking the question asked for was computed and then withheld.
        """
        ranked = bool(getattr(operation, "group_by", None)) and             getattr(operation, "order_by", "group") in {"value_desc", "value_asc"}

        if not ranked:
            return self.complete
        # `missing_values` on a GROUPED result counts the entities that have
        # no value for the grouping relation -- here, the 31 FIRs with no
        # accused recorded. That is an absence, not an unmeasured group: it
        # is reported on its own row as "(not recorded)", and it leaves every
        # named group's count exact. Unresolved identities and disputed
        # assertions still block, because those DO make a count wrong.
        return (self.population_documents > 0
                and self.complete_documents == self.population_documents
                and not any((self.unresolved_identities, self.disputed_assertions)))


class QuoteSupport(Contract):
    span_id: UUID
    quote: str = Field(min_length=1,max_length=2000)


class SupportedFinding(Contract):
    output_id: str
    text: str = Field(min_length=1,max_length=2000)
    supports: list[QuoteSupport] = Field(min_length=1,max_length=5)


class MetricEvidence(Contract):
    """An explicit denominator, separate from document extraction coverage."""

    eligible_records: int = Field(ge=0)
    observed_records: int = Field(ge=0)
    missing_records: int = Field(default=0, ge=0)
    unknown_cohort_records: int = Field(default=0, ge=0)
    conflicting_records: int = Field(default=0, ge=0)
    negative_duration_records: int = Field(default=0, ge=0)
    start_predicate_id: UUID
    end_predicate_id: UUID
    period_start: datetime | None = None
    period_end: datetime | None = None
    basis: Literal["exact_population", "observed_subset"] = "observed_subset"

    @model_validator(mode="after")
    def denominator_consistency(self):
        excluded = self.missing_records + self.conflicting_records + self.negative_duration_records
        if self.observed_records + excluded != self.eligible_records:
            raise ValueError("observations and mutually exclusive exclusions must reconcile")
        if self.basis == "exact_population" and (excluded or self.unknown_cohort_records):
            raise ValueError("an exact population cannot contain unknown or excluded records")
        return self


class EvidenceResult(Contract):
    result_id: UUID = Field(default_factory=uuid4)
    output_id: str
    scope_fingerprint: str
    operation: QueryOperation
    label: str
    unit: str | None = None
    rows: list[dict] = Field(default_factory=list)
    citations: list[SourceSpan] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    status: Literal["answered", "partial", "not_extracted", "ambiguous", "unavailable"] = "answered"
    query_hash: str | None = None
    # WHAT WAS MEASURED, in nameable parts, so the answer can say it in words.
    #
    # MEASURED: "how many people are recorded as accused" and "how many
    # complainants are recorded" both rendered as "person Distinct recorded
    # entities: N" -- textually identical apart from the number, because the
    # filter that distinguishes them was never named. The store knows the
    # concept labels when it executes; the answer knows the language when it
    # renders. So the parts are recorded here and phrased there.
    measured: dict | None = None
    findings: list[SupportedFinding] = Field(default_factory=list)
    metric: MetricEvidence | None = None
    derivation: dict | None = None


class Claim(Contract):
    """Models select evidence, never rewrite its values. Prose quotes are exact."""
    result_id: UUID
    output_id: str
    row_index: int = Field(default=0, ge=0)
    field: str = Field(default="value", max_length=80)
    span_id: UUID | None = None
    quote: str | None = None
    population_basis: Literal["exact_population", "observed_subset"] = "exact_population"


class Answer(Contract):
    run_id: UUID
    status: Literal["answered", "partial", "clarification", "not_extracted", "unavailable", "pending", "cancelled"]
    text: str
    plan: QuestionPlan | None = None
    results: list[EvidenceResult] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    clarification: str | None = None
    artifact_url: str | None = None
    health: dict = Field(default_factory=dict)


class ConversationState(Contract):
    scope_fingerprint: str
    plan: QuestionPlan | None = None
    result_ids: list[UUID] = Field(default_factory=list)
    pending_clarification: str | None = None
    revision: int = 0
    previous_results: list[dict] = Field(default_factory=list, max_length=8)
