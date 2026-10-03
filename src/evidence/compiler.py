"""Compile a closed algebra to SQL. Values are parameters, identifiers are code.

An LLM cannot introduce a table, function, SQL fragment, or access scope.
Filters are correlated EXISTS clauses so a join cannot multiply quantities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from .types import Concept, PredicateFilter, QueryOperation


class InvalidPlan(ValueError):
    pass


def identity_key(alias):
    # Only visible, confirmed links participate. Names/identifiers are never
    # matched here. RLS applies to both links and entity labels.
    return (f"coalesce({alias}.canonical_id,(WITH RECURSIVE connected(node) AS ("
            f"SELECT {alias}.entity_id UNION SELECT CASE WHEN l.left_id=c.node THEN l.right_id ELSE l.left_id END "
            "FROM connected c JOIN evidence.identity_links l ON (l.left_id=c.node OR l.right_id=c.node) "
            "WHERE l.status='confirmed') SELECT min(node::text)::uuid FROM connected))")


@dataclass
class Compiled:
    sql: str
    params: list = field(default_factory=list)
    population_sql: str = ""
    population_params: list = field(default_factory=list)
    unknown_cohort_sql: str = ""
    unknown_cohort_params: list = field(default_factory=list)


class Builder:
    def __init__(self, op: QueryOperation, concepts: dict[UUID, Concept]):
        self.op, self.concepts, self.params = op, concepts, []

    def param(self, value):
        self.params.append(value)
        return f"${len(self.params)}"

    def document_predicate(self, alias="d"):
        clauses = []
        if self.op.source_scope == "case_documents":
            clauses.append(f"cardinality({alias}.case_ids)>0")
        elif self.op.source_scope == "global_documents":
            clauses.append(f"{alias}.is_global")
        if self.op.source_collections:
            clauses.append(f"coalesce({alias}.source_metadata->>'endpoint', "
                           f"{alias}.source_metadata->>'source','unknown')="
                           f"ANY({self.param(self.op.source_collections)}::text[])")
        return " AND ".join(clauses) or "true"

    def concept(self, key, kinds=None):
        concept = self.concepts.get(key)
        if not concept:
            raise InvalidPlan(f"Concept {key} is absent from the supplied catalog; request catalog lookup")
        if kinds and concept.kind not in kinds:
            raise InvalidPlan(f"Concept {key} has kind {concept.kind}; this argument requires {','.join(sorted(kinds))}")
        return concept

    def temporal(self, alias):
        op = self.op
        clauses = [f"{alias}.status IN ('validated','superseded')" if op.known_at else f"{alias}.status='validated'"]
        if op.known_at:
            when = self.param(op.known_at)
            clauses += [f"{alias}.recorded_at<={when}",
                        f"({alias}.retracted_at IS NULL OR {alias}.retracted_at>{when})"]
        else:
            clauses.append(f"{alias}.retracted_at IS NULL")
        if op.as_of:
            when = self.param(op.as_of)
            # Unknown start dates cannot establish truth at a historic instant.
            clauses += [f"{alias}.valid_from<={when}",
                        f"({alias}.valid_to IS NULL OR {alias}.valid_to>{when})"]
        elif op.operation != "timeline":
            clauses.append(f"{alias}.valid_to IS NULL")
        if op.occurred_after and op.operation != "average_duration":
            clauses.append(f"{alias}.event_time>={self.param(op.occurred_after)}")
        if op.occurred_before and op.operation != "average_duration":
            clauses.append(f"{alias}.event_time<{self.param(op.occurred_before)}")
        return " AND ".join(clauses)

    def predicate(self, f: PredicateFilter, alias):
        concept = self.concept(f.predicate_id, {"property", "relation", "event_type"})
        if f.direction == "inverse" and concept.kind != "relation":
            raise InvalidPlan("Only entity relationships have an inverse direction")
        target_column = "subject_id" if f.direction == "inverse" else "object_entity_id"
        parts = [f"{alias}.predicate_id={self.param(f.predicate_id)}", self.temporal(alias)]
        if f.operator in {"exists","not_exists"}:
            return " AND ".join(parts)
        ops = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
        if f.object_entity_id or f.object_name:
            if concept.value_type != "entity" or f.operator not in {"eq", "ne"}:
                raise InvalidPlan("Relationship filters require an entity and equality operator")
            if f.object_entity_id:
                parts.append(f"{alias}.{target_column} {ops[f.operator]} {self.param(f.object_entity_id)}")
            else:
                # Named related entity, matched the way resolve() matches:
                # exact label, or a cross-script candidate so a Roman
                # spelling reaches the Urdu records.
                nm = self.param(f.object_name)
                inside = (f"SELECT 1 FROM evidence.entities t WHERE "
                          f"t.entity_id={alias}.{target_column} AND ("
                          f"lower(btrim(t.label))=lower(btrim({nm})) OR EXISTS("
                          f"SELECT 1 FROM unnest(regexp_split_to_array(t.label,'\\s+')) w "
                          f"WHERE regexp_replace(lower({nm}),'[aeiou]','','g') <> '' "
                          f"AND regexp_replace(roman_skeleton(w),'[aeiou]','','g')"
                          f"=regexp_replace(lower({nm}),'[aeiou]','','g')))")
                negate = "NOT " if f.operator == "ne" else ""
                parts.append(f"{negate}EXISTS({inside})")
        else:
            field = {"text": "value_text", "number": "value_number", "boolean": "value_boolean", "date": "value_date"}.get(concept.value_type)
            if not field:
                raise InvalidPlan("Relationship values must resolve to entity ids")
            value = f.value
            if concept.value_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise InvalidPlan("Numeric predicates require numeric values with the catalog unit")
            if concept.value_type == "boolean" and not isinstance(value, bool):
                raise InvalidPlan("Boolean predicates require booleans")
            if concept.value_type == "date":
                from datetime import datetime
                try:
                    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if value.tzinfo is None:
                        from zoneinfo import ZoneInfo
                        from src import config
                        value=value.replace(tzinfo=ZoneInfo(config.EVIDENCE_TIMEZONE))
                except ValueError as exc:
                    raise InvalidPlan("Date predicates require an ISO timestamp") from exc
            if f.operator == "contains":
                if concept.value_type != "text":
                    raise InvalidPlan("Contains applies to text only")
                parts.append(f"strpos(lower({alias}.{field}),lower({self.param(str(value))}))>0")
            else:
                parts.append(f"{alias}.{field} {ops[f.operator]} {self.param(value)}")
        return " AND ".join(parts)

    def population(self):
        op = self.op
        parts = ["NOT EXISTS(SELECT 1 FROM evidence.documents newer WHERE newer.supersedes=e.document_id)"]
        if op.known_at:
            when = self.param(op.known_at)
            parts = [f"EXISTS(SELECT 1 FROM evidence.documents d WHERE d.document_id=e.document_id AND d.recorded_at<={when})",
                     f"NOT EXISTS(SELECT 1 FROM evidence.documents newer WHERE newer.supersedes=e.document_id AND newer.recorded_at<={when})"]
        if op.subject_type:
            self.concept(op.subject_type, {"entity_type", "event_type"})
            parts.append(f"e.concept_id={self.param(op.subject_type)}")
        parts.append(f"EXISTS(SELECT 1 FROM evidence.documents d WHERE d.document_id=e.document_id "
                     f"AND {self.document_predicate('d')})")
        if op.entity_ids:
            parts.append(f"e.entity_id=ANY({self.param(op.entity_ids)}::uuid[])")
        if op.text and op.operation not in {"search", "traverse", "network"}:
            # Select entities by the name they are recorded under.
            #
            # MEASURED: "In which cases was کاشف an accused?" could not be
            # answered by any plan, because the algebra had `entity_ids`
            # and no way to obtain them. A planner cannot know a UUID, so
            # every person question fell through to narrative search,
            # which reads JSON and establishes nothing. The data was there
            # the whole time: 94 `accused in` and 71 `complainant of`
            # assertions, on person entities keyed by CNIC.
            #
            # The match is exact, ignoring case and surrounding space. A
            # substring match would fold "کاشف" into "کاشف محمود", which
            # is a different person, and this corpus has 36 names shared
            # by more than one CNIC -- so a loose match here would merge
            # people, which is the failure the identity model exists to
            # prevent. Names written in another script do not match; that
            # needs transliteration, not a looser comparison.
            # Exact name, or the same cross-script candidate rule resolve()
            # uses -- otherwise a plan that selects "Ahmed" by name finds
            # nothing while شکیل احمد sits in the corpus. An Urdu query
            # devowels to itself and so never matches a roman skeleton,
            # which keeps this from widening same-script lookups.
            name = self.param(op.text)
            parts.append(
                f"(lower(btrim(e.label))=lower(btrim({name})) OR EXISTS("
                f"SELECT 1 FROM unnest(regexp_split_to_array(e.label,'\\s+')) w "
                f"WHERE regexp_replace(lower({name}),'[aeiou]','','g') <> '' "
                f"AND regexp_replace(roman_skeleton(w),'[aeiou]','','g')"
                f"=regexp_replace(lower({name}),'[aeiou]','','g')))")
        if op.operation=="count_entities" and op.predicate_id:
            # Counting entities THAT HAVE the named fact, within any
            # period asked for.
            #
            # MEASURED: "How many FIRs were reported in June 2026?" -- the
            # truth is 6 -- answered 73, the all-time total, and reported
            # coverage 73/73 as though it were exact. count_entities
            # counted every entity of the type and never joined the
            # assertions table, so both the predicate and the period were
            # dropped in silence. A dropped constraint is worse than a
            # refused one: the figure looks answered and complete.
            b = self.param(op.predicate_id)
            bounds = []
            if op.occurred_after:
                bounds.append(f"m.event_time>={self.param(op.occurred_after)}")
            if op.occurred_before:
                bounds.append(f"m.event_time<{self.param(op.occurred_before)}")
            common = [f"m.predicate_id={b}", self.temporal("m"), *bounds]

            # A RELATION is read from whichever end it is stored on.
            #
            # MEASURED, and this is the FOURTH surface with the same blind
            # spot -- traverse, filters, grouping, and now this: "how many
            # people are recorded as accused" planned correctly as
            # count_entities(person, accused in) and answered 0. The truth
            # is 92. `accused in` is recorded FIR --> person, so a person is
            # never its subject, and requiring one asked whether a person
            # has people accused in them.
            #
            # Which end a relation is stored from is an artefact of the
            # source's shape. A PROPERTY is not reversed: it has no other
            # end, its value hangs off the subject alone.
            measured = self.concepts.get(op.predicate_id)
            if op.measure_direction == "inverse" and getattr(measured, "kind", None) != "relation":
                raise InvalidPlan("Only relationships support inverse measurement")
            forward = ("EXISTS(SELECT 1 FROM evidence.assertions m WHERE "
                       + " AND ".join([f"m.subject_id=e.entity_id", *common]) + ")")
            if getattr(measured, "kind", None) == "relation":
                backward = ("EXISTS(SELECT 1 FROM evidence.assertions m WHERE "
                            + " AND ".join([f"m.object_entity_id=e.entity_id", *common]) + ")")
                parts.append(backward if op.measure_direction == "inverse" else forward)
            else:
                parts.append(forward)
        if op.operation=="average_duration" and (op.occurred_after or op.occurred_before):
            key=self.param(op.start_predicate_id)
            base=f"t.subject_id=e.entity_id AND t.predicate_id={key} AND {self.temporal('t')} AND t.value_date IS NOT NULL"
            bounds=[]
            if op.occurred_after:
                bounds.append(f"t.value_date>={self.param(op.occurred_after)}")
            if op.occurred_before:
                bounds.append(f"t.value_date<{self.param(op.occurred_before)}")
            # Keep unknown cohort dates visible as missing measurements, rather
            # than silently dropping them and claiming complete temporal coverage.
            parts.append(f"EXISTS(SELECT 1 FROM evidence.assertions t WHERE {base} AND {' AND '.join(bounds)})")
        for index, item in enumerate(op.filters):
            alias = f"f{index}"
            parent = "e.entity_id"
            chain = ""
            for hop,key in enumerate(item.relationship_path):
                self.concept(key,{"relation"})
                link = f"f{index}p{hop}"
                chain += (f"EXISTS(SELECT 1 FROM evidence.assertions {link} WHERE {link}.subject_id={parent} "
                          f"AND {link}.predicate_id={self.param(key)} AND {self.temporal(link)} "
                          f"AND {link}.object_entity_id IN (SELECT entity_id FROM evidence.entities) AND ")
                parent = f"{link}.object_entity_id"
            # A RELATIONSHIP filter matches from either end.
            #
            # MEASURED, and this is the third place the same defect appeared:
            # list(person, filter "accused in" exists) returned rows=0 and
            # rendered "No matching records in the checked population". The
            # relation is stored FIR --> person, so a person is never its
            # subject, and asking whether a person HAS an "accused in" link
            # found nothing -- for 94 recorded accusations. The traversal and
            # the grouping had the same blind spot; this one stayed silent.
            #
            # Which end the relation is stored from is an artefact of the
            # source's shape, not a claim about the relationship, so the
            # filter matches both. A VALUE filter is not reversed: only a
            # subject carries values, and an object is an entity, not a value.
            # PARTICIPATION IS DIRECTION-FREE; A NAMED COUNTERPART IS NOT.
            #
            # The comment above says the filter matches both ends. It did not:
            # it matched ONE end, chosen by whether the PLANNER had set
            # direction="inverse". MEASURED, on "how many people are recorded
            # as accused": the planner emitted list(person) with "accused in"
            # exists and no direction, so this checked the subject side, where
            # a person never appears -- the relation is stored FIR --> person.
            # Zero rows, rendered as "No matching records in the checked
            # population", status ANSWERED, against 92 recorded accusations.
            # This is the fifth surface on which that same blind spot has
            # produced a confident wrong answer.
            #
            # `exists` and `not_exists` ask whether the entity TAKES PART in
            # the relation at all, which cannot depend on which end the source
            # happened to store it from, so either end satisfies them. An
            # operator that names a counterpart ("the accused is Ahmed") is
            # a different question and stays direction-sensitive: there, which
            # end holds the counterpart is the whole meaning of the filter.
            relation = self.concepts.get(item.predicate_id)
            is_relation = getattr(relation, "kind", None) == "relation"
            if is_relation and item.operator in {"exists", "not_exists"}:
                side = (f"({alias}.subject_id={parent} "
                        f"OR {alias}.object_entity_id={parent})")
            elif is_relation and item.direction == "inverse":
                side = f"{alias}.object_entity_id={parent}"
            else:
                side = f"{alias}.subject_id={parent}"
            chain += (f"EXISTS(SELECT 1 FROM evidence.assertions {alias} "
                      f"WHERE {side} AND {self.predicate(item, alias)})")
            expression=chain+")"*len(item.relationship_path)
            parts.append("NOT ("+expression+")" if item.operator=="not_exists" else expression)
        if op.relationship_path:
            # Each hop is an explicit catalog relationship; no inferred joins.
            previous = "e.entity_id"
            nested = ""
            for index, key in enumerate(op.relationship_path):
                self.concept(key, {"relation"})
                alias = f"p{index}"
                direction = op.relationship_directions[index] if op.relationship_directions else "forward"
                origin, target = ("subject_id", "object_entity_id") if direction == "forward" else ("object_entity_id", "subject_id")
                nested += (f"EXISTS(SELECT 1 FROM evidence.assertions {alias} WHERE "
                           f"{alias}.{origin}={previous} AND {alias}.predicate_id={self.param(key)} "
                           f"AND {alias}.{target} IN (SELECT entity_id FROM evidence.entities) "
                           f"AND {self.temporal(alias)}")
                previous = f"{alias}.{target}"
                if index < len(op.relationship_path)-1:
                    nested += " AND "
            parts.append(nested + ")" * len(op.relationship_path))
        return ("SELECT e.entity_id,e.document_id,e.concept_id,e.label,e.span_id,e.identity_status,e.identifiers,"
                +identity_key("e")+" AS canonical_id FROM evidence.entities e WHERE " + " AND ".join(parts))


def compile_operation(op: QueryOperation, concepts: dict[UUID, Concept]) -> Compiled:
    b = Builder(op, concepts)
    if op.operation in {"search", "traverse", "network"}:
        raise InvalidPlan("This operation uses a bounded retrieval tool")
    population = b.population()
    population_params = list(b.params)
    prefix = f"WITH population AS ({population}) "
    entity_key = "coalesce(e.canonical_id,e.entity_id)"
    if op.operation in {"count_entities", "sum", "average", "average_duration", "count_assertions"}:
        measure = None
        missing = ""
        extra_spans = ""
        if op.operation == "count_entities":
            value = f"count(DISTINCT {entity_key})"
            rows = "population e"
            span = "e.span_id"
        elif op.operation == "average_duration":
            rows="population e"
            for alias,key in [("start_date",op.start_predicate_id),("end_date",op.end_predicate_id)]:
                concept=b.concept(key,{"property","event_type"})
                if concept.value_type!="date":
                    raise InvalidPlan("Duration endpoints must be date predicates")
                rows+=(f" LEFT JOIN LATERAL (SELECT count(DISTINCT t.value_date) AS n,min(t.value_date) AS value,"
                       f"(array_agg(t.span_id))[1] AS span_id FROM evidence.assertions t "
                       f"WHERE t.subject_id=e.entity_id AND t.predicate_id={b.param(key)} AND {b.temporal('t')}) {alias} ON true")
            valid="start_date.n=1 AND end_date.n=1 AND end_date.value>=start_date.value"
            value=f"avg(CASE WHEN {valid} THEN extract(epoch FROM end_date.value-start_date.value) END)"
            missing=(f"count(*) AS eligible_records, count(*) FILTER(WHERE {valid}) AS observed_records, "
                     "count(*) FILTER(WHERE start_date.n=0 OR end_date.n=0) AS missing_records, "
                     "count(*) FILTER(WHERE start_date.n>0 AND end_date.n>0 AND (start_date.n>1 OR end_date.n>1)) AS conflicting_records, "
                     "count(*) FILTER(WHERE start_date.n=1 AND end_date.n=1 AND end_date.value<start_date.value) AS negative_duration_records, "
                     f"count(*) FILTER(WHERE ({valid}) IS NOT TRUE) AS missing_measurements, ")
            span="start_date.span_id"
            extra_spans=" || array_agg(DISTINCT end_date.span_id)"
        else:
            measure = b.concept(op.predicate_id, {"property", "relation", "event_type"})
            if op.operation in {"sum", "average"} and measure.value_type != "number":
                raise InvalidPlan("Sum and average require a numeric predicate")
            fn = {"sum": "sum(a.value_number)", "average": "avg(a.value_number)", "count_assertions": "count(DISTINCT a.assertion_id)"}
            value = fn[op.operation]
            rows = (f"population e JOIN evidence.assertions a ON a.subject_id=e.entity_id "
                    f"AND a.predicate_id={b.param(op.predicate_id)} AND {b.temporal('a')}")
            span = "a.span_id"
        group_select, group_sql = "", ""
        if op.group_by:
            try:
                group_concept=b.concept(op.group_by, {"property", "relation"})
            except InvalidPlan as exc:
                choices=[f"{c.concept_id}: {c.label}" for c in concepts.values() if c.kind=="relation"]
                raise InvalidPlan(str(exc)+". group_by names the property or relationship ON the counted subject, "
                    "not the target entity type. Available relationship IDs: "+"; ".join(choices)) from exc
            # More than one group value per entity would duplicate a measure.
            # Expose it as missing/ambiguous and block exact publication.
            group_key=(identity_key("target")+"::text" if group_concept.value_type=="entity"
                       else "coalesce(g.value_text,g.value_number::text,g.value_boolean::text,g.value_date::text)")
            group_label="target.label" if group_concept.value_type=="entity" else group_key
            # Grouping by a RELATION reads it from either end.
            #
            # MEASURED, completing the pattern: traversal, filters and now
            # grouping each assumed the stored direction. `accused in` is
            # recorded FIR --> person, so grouping PEOPLE by it found no
            # assertion where a person was the subject, collapsed every row
            # into one null group and reported "Partial evidence" over a
            # population that was perfectly correct -- 92 accused people.
            #
            # Reading the relation from both ends makes "group people by the
            # cases they are accused in" mean what it says. A PROPERTY is not
            # reversed: it has no other end to read from.
            group_param = b.param(op.group_by)
            if group_concept.kind == "relation" and op.group_direction == "inverse":
                source = (f"(SELECT r.assertion_id,r.document_id,r.object_entity_id AS subject_id,"
                          f"r.predicate_id,r.subject_id AS object_entity_id,r.value_text,"
                          f"r.value_number,r.value_boolean,r.value_date,r.unit,r.span_id,"
                          f"r.event_time,r.valid_from,r.valid_to,r.recorded_at,r.retracted_at,"
                          f"r.status,r.extraction_version,r.validation,r.source_record_key "
                          f"FROM evidence.assertions r WHERE r.object_entity_id=e.entity_id) g")
                where = f"g.predicate_id={group_param} AND {b.temporal('g')}"
            else:
                source = "evidence.assertions g"
                where = (f"g.subject_id=e.entity_id AND g.predicate_id={group_param} "
                         f"AND {b.temporal('g')}")
            rows += (f" LEFT JOIN LATERAL (SELECT count(*) AS n, "
                     f"min({group_key}) AS key,min({group_label}) AS label "
                     f"FROM {source} LEFT JOIN evidence.entities target "
                     f"ON target.entity_id=g.object_entity_id WHERE {where}) grp ON true")
            group_select = "CASE WHEN grp.n=1 THEN grp.key ELSE NULL END AS group_key, min(CASE WHEN grp.n=1 THEN grp.label ELSE NULL END) AS group_value, "
            group_sql = " GROUP BY CASE WHEN grp.n=1 THEN grp.key ELSE NULL END"
        sql = (prefix + f"SELECT {group_select}{missing}{value} AS value, "
               f"array_remove(array_agg(DISTINCT {span}){extra_spans},NULL) AS span_ids, "
               f"count(DISTINCT e.entity_id) FILTER(WHERE e.identity_status<>'confirmed') AS unresolved_identities "
               f"FROM {rows}{group_sql}")
        if op.group_by:
            # The ungrouped bucket is never a competitor in a ranking.
            #
            # MEASURED: "flag the suspects who appear most frequently" put
            # "(not recorded): 31" at the top -- the FIRs with no accused --
            # above the man actually accused in two cases. It is reported,
            # because 31 unrecorded is worth knowing, but it is reported last.
            ungrouped_last = "CASE WHEN grp.n=1 THEN grp.key ELSE NULL END IS NULL"
            sql += {"group": f" ORDER BY {ungrouped_last}, group_value NULLS LAST",
                    "value_desc": f" ORDER BY {ungrouped_last}, value DESC,group_value NULLS LAST",
                    "value_asc": f" ORDER BY {ungrouped_last}, value ASC,group_value NULLS LAST"}[op.order_by]
        compiled = Compiled(sql, b.params, population, population_params)
        if op.operation == "average_duration" and (op.occurred_after or op.occurred_before):
            unknown = Builder(op.model_copy(update={"occurred_after": None, "occurred_before": None}), concepts)
            unallocated_population = unknown.population()
            key = unknown.param(op.start_predicate_id)
            compiled.unknown_cohort_sql = (f"WITH population AS ({unallocated_population}) SELECT count(*) FROM population e "
                f"WHERE NOT EXISTS(SELECT 1 FROM evidence.assertions t WHERE t.subject_id=e.entity_id "
                f"AND t.predicate_id={key} AND {unknown.temporal('t')} AND t.value_date IS NOT NULL)")
            compiled.unknown_cohort_params = unknown.params
        return compiled
    if op.operation == "list" and not op.predicate_id:
        sql = prefix + "SELECT e.entity_id,e.label,e.concept_id,e.identity_status,ARRAY[e.span_id] AS span_ids FROM population e ORDER BY e.entity_id"
    else:
        conditions = b.temporal("a")
        if op.predicate_id:
            b.concept(op.predicate_id)
            conditions += f" AND a.predicate_id={b.param(op.predicate_id)}"
        sql = (prefix + "SELECT a.assertion_id,e.label,a.predicate_id,k.label AS predicate_label,a.event_time,a.valid_from,a.valid_to, "
               "a.value_text,a.value_number,a.value_boolean,a.value_date,a.unit,a.object_entity_id,o.label AS object_label,ARRAY[a.span_id] AS span_ids "
               "FROM population e JOIN evidence.assertions a ON a.subject_id=e.entity_id "
               "JOIN evidence.concepts k ON k.concept_id=a.predicate_id "
               f"LEFT JOIN evidence.entities o ON o.entity_id=a.object_entity_id WHERE {conditions} "
               "ORDER BY a.event_time NULLS LAST,a.recorded_at,a.assertion_id")
    sql += f" LIMIT {b.param(op.limit+1)}"
    return Compiled(sql, b.params, population, population_params)
