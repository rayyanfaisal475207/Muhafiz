"""All tools share one scoped transaction and one provenance/coverage boundary."""
from __future__ import annotations

import hashlib
import json
import os
import re
from uuid import UUID

from .compiler import Builder, InvalidPlan, compile_operation, identity_key
from .types import Concept, Coverage, EvidenceResult, MetricEvidence, QueryOperation, Scope, SourceSpan


class EvidenceStore:
    def __init__(self, conn, scope: Scope):
        self.conn, self.scope = conn, scope

    async def snapshot(self):
        record = await self.conn.fetchrow('''SELECT
            (SELECT md5(coalesce(string_agg(document_id::text||content_hash||case_ids::text||coalesce(project_id::text,'')
                ||coalesce(owner_id::text,'')||coalesce(session_id::text,'')||is_global::text||source_metadata::text,',' ORDER BY document_id),''))
             FROM evidence.documents d WHERE NOT EXISTS(SELECT 1 FROM evidence.documents n WHERE n.supersedes=d.document_id)) AS documents,
            (SELECT md5(coalesce(string_agg(to_jsonb(a)::text,',' ORDER BY assertion_id),'')) FROM evidence.assertions a) AS assertions,
            (SELECT md5(coalesce(string_agg(to_jsonb(c)::text,',' ORDER BY concept_id),'')) FROM evidence.concepts c) AS catalog,
            (SELECT md5(coalesce(string_agg(to_jsonb(e)::text,',' ORDER BY entity_id),'')) FROM evidence.entities e) AS entities,
            (SELECT md5(coalesce(string_agg(to_jsonb(v)::text,',' ORDER BY document_id,concept_id,concept_version,extraction_version),'')) FROM evidence.coverage v) AS coverage,
            (SELECT md5(coalesce(string_agg(to_jsonb(i)::text,',' ORDER BY left_id,right_id),'')) FROM evidence.identity_links i) AS identities,
            (SELECT md5(coalesce(string_agg(to_jsonb(b)::text,',' ORDER BY binding_id),'')) FROM evidence.source_bindings b) AS bindings,
            (SELECT md5(coalesce(string_agg(to_jsonb(p)::text,',' ORDER BY profile_id),'')) FROM evidence.source_profiles p) AS profiles''')
        return hashlib.sha256(json.dumps(dict(record),sort_keys=True).encode()).hexdigest()

    async def discovery_population(self, operations):
        """Enumerate source collections independently of already extracted facts."""
        documents=set()
        for op in operations:
            if op.operation in {'difference','ratio'}:continue
            selected=op.model_copy(deep=True)
            if not selected.source_collections:
                keys=[key for key in (op.subject_type,op.predicate_id,op.start_predicate_id,op.end_predicate_id) if key]
                rows=await self.conn.fetch('''SELECT DISTINCT b.source_namespace AS collection
                    FROM evidence.source_bindings b JOIN evidence.concepts k ON k.concept_id=b.concept_id
                    WHERE b.status='validated' AND b.concept_version=k.version
                      AND (b.concept_id=ANY($1::uuid[]) OR b.subject_type=ANY($1::uuid[]))
                    UNION SELECT DISTINCT coalesce(d.source_metadata->>'endpoint',d.source_metadata->>'source')
                    FROM evidence.concepts k JOIN evidence.documents d ON d.document_id=k.origin_document_id
                    WHERE k.concept_id=ANY($1::uuid[]) AND NOT EXISTS(
                      SELECT 1 FROM evidence.source_bindings b JOIN evidence.concepts p ON p.concept_id=b.concept_id
                      WHERE b.status='validated' AND b.concept_version=p.version
                        AND (b.concept_id=k.concept_id OR b.subject_type=k.concept_id))''',keys)
                selected.source_collections=[r['collection'] for r in rows if r['collection']]
            builder=Builder(selected,{})
            where=builder.document_predicate('d')
            rows=await self.conn.fetch(f'''SELECT d.document_id FROM evidence.documents d
                WHERE {where} AND NOT EXISTS(SELECT 1 FROM evidence.documents n WHERE n.supersedes=d.document_id)
                ORDER BY d.document_id''',*builder.params)
            documents.update(row['document_id'] for row in rows)
        return [{'document_id':key} for key in sorted(documents,key=str)]

    async def catalog(self, query: str = "", limit: int = 100) -> dict[UUID, Concept]:
        """Concepts for a question, ORDERED BY RELEVANCE to it.

        MEASURED: this ranked with `plainto_tsquery`, which builds an AND
        query. No concept's label or meaning contains every word of a
        question, so ts_rank scored all 47 concepts at exactly 0.0 and the
        `label` tiebreaker took over -- the "ranking" was ALPHABETICAL. On
        "how many FIRs cite the PPC" that put `statute act` at position 37
        and `statute section code` at 38 of 41, and the runtime's `index <
        24` cap then dropped both. The planner could only reach the field it
        needed through a mid-run catalog lookup, which inflated the request
        until the repair came back HTTP 413.

        Any word may match, so the lexemes are OR-ed and a concept matching
        more of them ranks higher. The `english` configuration is used for
        both sides so that stop words drop out instead of scoring every
        concept whose meaning contains "the", and so "recorded" and "record"
        are the same lexeme. Urdu tokens pass through unstemmed, which is
        why the caller ranks against the question PLUS the forms the records
        actually use.
        """
        lexemes = " | ".join(dict.fromkeys(
            w for w in re.split(r"[^0-9A-Za-z؀-ۿ]+", (query or "")) if len(w) > 1))
        rows = await self.conn.fetch("""
            SELECT concept_id,label,kind,meaning,aliases,value_type,unit,grain,version
            FROM evidence.concepts WHERE status='validated'
            ORDER BY ts_rank(to_tsvector('english',label||' '||meaning||' '||array_to_string(aliases,' ')),
                             to_tsquery('english',nullif($1,''))) DESC NULLS LAST,
                     label,concept_id LIMIT $2
        """, lexemes, limit)
        concepts = {r["concept_id"]: Concept(**dict(r)) for r in rows}
        relations = [key for key,c in concepts.items() if c.kind == "relation"]
        if relations:
            endpoints = await self.conn.fetch('''SELECT a.predicate_id,
                array_agg(DISTINCT s.concept_id) AS subject_types,array_agg(DISTINCT o.concept_id) AS object_types
                FROM evidence.assertions a JOIN evidence.entities s ON s.entity_id=a.subject_id
                JOIN evidence.entities o ON o.entity_id=a.object_entity_id
                WHERE a.predicate_id=ANY($1::uuid[]) AND a.status='validated' AND a.retracted_at IS NULL
                GROUP BY a.predicate_id''',relations)
            for row in endpoints:
                concepts[row['predicate_id']].recorded_subject_types = row['subject_types']
                concepts[row['predicate_id']].recorded_object_types = row['object_types']
        return concepts

    async def resolve(self, query: str, limit: int = 15, *, exact_only: bool = False):
        # Exact aliases/names are candidates, never automatic identity merges.
        rows = await self.conn.fetch(f"""
            SELECT entity_id,concept_id,label,identity_status,
                   canonical_id,identifiers->>'cnic' AS cnic,'same_script' AS match
            FROM evidence.entities
            WHERE lower(label)=lower($1)
              {"" if exact_only else "OR strpos(lower(label),lower($1))>0"}
            ORDER BY (lower(label)=lower($1)) DESC,entity_id LIMIT $2
        """, query, limit)
        if rows:
            return [dict(r) for r in rows]
        # CROSS-SCRIPT FALLBACK, only when same-script found nothing.
        #
        # MEASURED: "who is Ahmed and is he nominated in any cases during
        # the last 2 months" established nothing. The corpus stores
        # شکیل احمد, چوہدری بشیر احمد and سرفراز احمد; an English question
        # never reached Urdu data, so a name that is plainly present read
        # as absent.
        #
        # A skeleton shorter than three consonants is not a name.
        # MEASURED: asked "his name is sarfraz ahmed", the resolver returned
        # نعیم اختر, because the English word "name" devowels to "nm" and so
        # does نعیم. "his" -> "hs", "and" -> "nd" and "is" -> "s" are the
        # same trap. Three consonants keeps ahmed (hmd), kashif (kshf) and
        # sarfraz (srfrz) while refusing the collisions.
        #
        # roman_skeleton() (migration 058) romanises the Arabic script,
        # but Urdu omits short vowels, so احمد becomes 'ahmd' and never
        # equals the typed 'ahmed'. Removing vowels from BOTH sides makes
        # the two comparable, and the comparison runs per name-PART so a
        # given name matches inside a full name.
        #
        # Measured on this corpus: ahmed -> exactly the 3 احمد names,
        # kashif -> کاشف, bilal -> the 3 بلال names, and a nonsense term
        # -> nothing. Devowelling is lossy, so these are CANDIDATES, not
        # identities: they are labelled cross_script so more than one of
        # them is a reason to ask which person is meant, never to merge.
        # THE QUERY TERM IS SKELETONISED TOO, NOT JUST THE STORED LABEL.
        #
        # This compared `lower($1)` devowelled -- the term exactly as typed --
        # against the ROMANISED skeleton of each stored name. That works for
        # a Roman query and cannot work for an Urdu one: Urdu text has no
        # [aeiou] to remove and no Latin letters to match, so the comparison
        # was Urdu-against-Latin and always false.
        #
        # MEASURED: the records spell the name ذیشان, and Urdu typists
        # routinely write زیشان -- ذ and ز are both "z" and the skeleton
        # function already collapses them (both give 'zishan', verified).
        #
        #     resolve_mentions("زیشان …")  ->  0 matches, run died after 203s
        #     resolve_mentions("ذیشان …")  -> 10 matches
        #     resolve_mentions("zeeshan")  -> 11 matches
        #
        # So a Pakistani officer typing the commoner spelling got nothing,
        # while the English spelling worked. Passing the term through the
        # same skeleton makes Urdu-to-Urdu variants comparable, and leaves
        # Roman input untouched: roman_skeleton() only rewrites Arabic
        # script, so 'zeeshan' comes back 'zeeshan'.
        return [dict(r) for r in await self.conn.fetch(r"""
            WITH q AS (SELECT regexp_replace(roman_skeleton(lower($1)),
                                             '[aeiou]','','g') AS k)
            SELECT DISTINCT e.entity_id,e.concept_id,e.label,e.identity_status,
                            e.canonical_id,e.identifiers->>'cnic' AS cnic,
                            'cross_script' AS match
            FROM evidence.entities e,
                 unnest(regexp_split_to_array(e.label,'\s+')) AS w, q
            WHERE length(q.k) >= 3
              AND regexp_replace(roman_skeleton(w),'[aeiou]','','g') = q.k
            ORDER BY e.label, e.entity_id LIMIT $2
        """, query, limit)]

    async def same_name_forms(self, term: str, candidates: list[str]) -> list[str]:
        """Which candidates are the SAME NAME as `term`, in another spelling.

        WHY OCCURRENCE IS NOT ENOUGH

        A form suggested by an outside source is checked against the corpus
        to see whether it occurs. That proves the form EXISTS in the
        records; it says nothing about whether it is a reading of the term
        that was asked about -- the distinction this codebase already draws
        as "occurrence is a retrieval signal, not semantic validation".

        MEASURED: asked for readings of "Bhara Kahu", an open-web lookup
        offered "Islamabad Capital Territory", "Khyber Pakhtunkhwa" and
        "Indian Rosewood". Two of those occur in these records, so an
        occurrence check accepted them -- along with the word "According".
        Feeding those into retrieval as readings of "Bhara Kahu" would have
        steered the search towards Islamabad and quietly changed what the
        question found.

        A READING of a name is the same name written differently, so that
        is what is tested: each word of each side is reduced to BOTH its
        devowelled lowercase form and its devowelled romanised form -- the
        two halves `resolve()` compares across scripts -- and a candidate is
        kept when any word skeleton matches. Both are needed because the
        term may arrive in either script: romanising a Latin word mangles
        it, and lowercasing an Urdu one does nothing. "بھارہ کہو" passes;
        "Islamabad" does not.
        """
        candidates = [c for c in dict.fromkeys(candidates or []) if c and c.strip()]
        if not term or not term.strip() or not candidates:
            return []
        # Each side collapses to ONE skeleton for the WHOLE phrase, not one
        # per word. MEASURED: "Bhara Kahu" against "بھارہ کہو" matches on the
        # second word only (کہو -> kh, Kahu -> kh), and the ">= 3 characters"
        # guard that `resolve()` needs to stop "nm" matching نعیم threw that
        # single match away. Comparing whole phrases keeps the signal and
        # keeps the guard's purpose, because a short accidental overlap
        # barely moves the similarity of a longer skeleton.
        #
        # The Urdu side is romanised and the Latin side lowercased:
        # romanising a Latin word mangles it, lowercasing an Urdu one does
        # nothing.
        #
        # MEASURED separation, for offered readings of "Bhara Kahu":
        #     بھارہ کہو                    0.444   kept
        #     Islamabad Capital Territory  0.000   dropped
        #     Indian Rosewood              0.000   dropped
        #     According                    0.000   dropped
        # and for "Kashif": کاشف 1.000, Kashif Ali 0.571, Islamabad 0.000.
        # Nothing observed falls between 0.133 and 0.444, so the threshold
        # sits in that gap rather than on top of any measurement.
        threshold = float(os.getenv("EVIDENCE_READING_SIMILARITY", "0.35"))
        rows = await self.conn.fetch(r"""
            WITH raw AS (
              SELECT $1::text AS text, 0 AS side
              UNION ALL
              SELECT f, 1 FROM unnest($2::text[]) AS f),
            skeleton AS (
              SELECT r.text, r.side,
                     regexp_replace(coalesce((
                       SELECT string_agg(
                         CASE WHEN w ~ ARABIC_RANGE
                              THEN regexp_replace(roman_skeleton(w),'[aeiou]','','g')
                              ELSE regexp_replace(lower(w),'[aeiou]','','g') END, '')
                       FROM unnest(regexp_split_to_array(btrim(r.text),'\s+')) AS w),''),
                     '[^a-z0-9]','','g') AS k
              FROM raw r)
            SELECT c.text AS form, similarity(t.k, c.k) AS score
            FROM skeleton c, skeleton t
            WHERE t.side = 0 AND c.side = 1
              AND length(t.k) >= 2 AND length(c.k) >= 2
              AND similarity(t.k, c.k) >= $3
            ORDER BY score DESC
        """.replace("ARABIC_RANGE", "'[؀-ۿ]'"), term, candidates, threshold)
        keep = {r["form"] for r in rows}
        return [c for c in candidates if c in keep]

    async def field_meanings(self, document_ids):
        rows = await self.conn.fetch('''SELECT DISTINCT a.document_id,
            a.validation->>'pointer' AS pointer,c.label,c.meaning,c.grain,c.unit
            FROM evidence.assertions a JOIN evidence.concepts c ON c.concept_id=a.predicate_id
            WHERE a.document_id=ANY($1::uuid[]) AND a.status='validated' AND c.status='validated'
            AND a.validation->>'pointer' IS NOT NULL''',document_ids)
        return [{'document_id':str(r['document_id']),'field':r['pointer'].rsplit('/',1)[-1].replace('~1','/').replace('~0','~'),
                 'pointer':r['pointer'],'means':r['label'],'definition':r['meaning'],'grain':r['grain'],'unit':r['unit']} for r in rows]

    async def resolve_mentions(self, text: str, limit: int = 15):
        """Entities named ANYWHERE in a question, not just as the whole of it.

        MEASURED, and this is the defect that made person questions fail:

            resolve("kashif")                               -> 10 people
            resolve("is there anyone named kashif in the
                     system")                               -> 0
            resolve("who is Ahmed")                         -> 0

        The runtime passed the whole question to resolve(), which compares
        against an entity's recorded label. A label is a NAME -- "کاشف" --
        and no name equals a sentence, so every natural-language question
        produced zero candidates. The planner was then asked to build a
        plan about a person whose id it had never been given, guessed at
        concepts, and had its plan rejected. That is the failure behind
        "who is Ahmed", "who is kashif then", and the rest.

        The phrase is tried first, so typing just a name still works and
        a multi-word name still matches as a whole. Then each word is
        tried on its own, by EXACT match only: a single short word is a
        weak signal, and substring matching on it would let "the" pull in
        anyone whose name contains those letters.
        """
        seen: dict[str, dict] = {}
        ordered: list[dict] = []

        async def take(term: str, exact_only: bool):
            if len(seen) >= limit:
                return
            for row in await self.resolve(term, limit=limit, exact_only=exact_only):
                key = str(row.get("entity_id"))
                if key not in seen:
                    seen[key] = row
                    ordered.append({**row, "matched_term": term})

        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            return []
        await take(cleaned, False)
        words = [w.strip("?.,!:;\"'()[]{}") for w in cleaned.split()]
        for word in words:
            if len(word) < 3 or len(seen) >= limit:
                continue
            await take(word, True)

        # One row per PERSON, not one per document.
        #
        # MEASURED: asked "his name is sarfraz ahmed", the resolver returned
        # سرفراز احمد twice -- once from FIR 620/26 and once from a PKM
        # application -- and the planner reported "multiple individuals named
        # Sarfraz Ahmed". There is one: CNIC 00000-1000055-1. Entities are
        # document-local by design, so a person appears once per document,
        # and handing those rows over as separate candidates manufactures an
        # ambiguity that does not exist. The user answered the question and
        # was asked it again.
        #
        # Every document-local id is kept on the row, because assertions hang
        # off the specific entity and a traversal needs all of them as seeds.
        people: dict[str, dict] = {}
        for row in ordered:
            identity = (row.get("canonical_id") or row.get("cnic")
                        or row.get("entity_id"))
            key = str(identity)
            person = people.get(key)
            if person is None:
                person = {**row, "entity_ids": [], "documents": 0}
                people[key] = person
            person["entity_ids"].append(str(row["entity_id"]))
            person["documents"] += 1
        return list(people.values())[:limit]

    async def spans(self, ids: list[UUID]) -> list[SourceSpan]:
        if not ids:
            return []
        rows = await self.conn.fetch("""
            SELECT s.span_id,s.document_id,s.char_start AS start,s.char_end AS end,s.text,d.title
            FROM evidence.spans s JOIN evidence.documents d USING(document_id)
            WHERE s.span_id=ANY($1::uuid[])
              AND substring(d.content FROM s.char_start+1 FOR s.char_end-s.char_start)=s.text
            ORDER BY s.span_id
        """, ids)
        return [SourceSpan(**dict(r)) for r in rows]

    async def coverage(self, op: QueryOperation, concepts: dict[UUID, Concept]) -> Coverage:
        required = {x for x in (op.subject_type,op.predicate_id,op.group_by,op.start_predicate_id,op.end_predicate_id) if x}
        required.update(f.predicate_id for f in op.filters)
        required.update(key for f in op.filters for key in f.relationship_path)
        required.update(op.relationship_path)
        if not required <= concepts.keys():
            raise InvalidPlan("Plan refers to concepts outside the authorized catalog")
        # ── The denominator is the REQUESTED population ─────────────────
        #
        # It used to be every authorized document. MEASURED: "how many
        # FIRs were reported in June 2026?" reported
        #
        #     Documents checked: 0/7528
        #
        # because the store holds 73 FIR records alongside ~7,000 chunks
        # of Punjab Police Rules. A reporting time cannot occur in a
        # rulebook, so those chunks can never be "complete" for that
        # concept and completeness is unreachable -- meaning no count can
        # ever be answered, however much evidence is extracted.
        #
        # A concept records the document it was defined from, and that
        # document's metadata names its source kind. "reported at" was
        # defined from a FIR record, so its population is FIR records.
        #
        # This is deliberately NOT "documents that already have an
        # assertion for this concept": that would make the denominator a
        # function of how much extraction has happened, so coverage would
        # always read 100% and a partial extraction would masquerade as a
        # complete survey.
        population_builder=Builder(op,concepts)
        population_builder.params=[list(required)]
        document_predicate=population_builder.document_predicate("d")
        # Explicit collection contracts take precedence over origin-based legacy inference.
        origin_filter=("true" if op.source_collections else
                       "coalesce(d.source_metadata->>'endpoint',d.source_metadata->>'source','unknown') IN (SELECT kind FROM kinds)")
        # Reviewed bindings identify record collections. Their supporting
        # definition document is evidence for meaning, not a population member.
        kinds_sql = """
            SELECT DISTINCT b.source_namespace AS kind
            FROM evidence.source_bindings b
            JOIN evidence.concepts k ON k.concept_id=b.concept_id
            WHERE b.status='validated' AND k.status='validated'
              AND b.concept_version=k.version
              AND (b.concept_id=ANY($1::uuid[]) OR b.subject_type=ANY($1::uuid[]))
            UNION
            SELECT DISTINCT coalesce(o.source_metadata->>'endpoint',
                                     o.source_metadata->>'source','unknown') AS kind
            FROM evidence.concepts k
            JOIN evidence.documents o ON o.document_id=k.origin_document_id
            WHERE k.concept_id=ANY($1::uuid[])
              AND NOT EXISTS(SELECT 1 FROM evidence.source_bindings b
                JOIN evidence.concepts p ON p.concept_id=b.concept_id
                WHERE b.status='validated' AND p.status='validated'
                  AND b.concept_version=p.version
                  AND (b.concept_id=k.concept_id OR b.subject_type=k.concept_id))
        """
        rows = await self.conn.fetch(f"""
            WITH kinds AS (
              {kinds_sql}
            )
            SELECT d.document_id, bool_and(EXISTS(
              SELECT 1 FROM evidence.coverage c WHERE c.document_id=d.document_id
              AND c.content_hash=d.content_hash AND c.concept_id=k.concept_id
              AND c.concept_version=k.version AND c.status='complete'
            )) AS complete
            FROM evidence.documents d CROSS JOIN evidence.concepts k
            WHERE k.concept_id=ANY($1::uuid[])
            AND {origin_filter} AND {document_predicate}
            AND NOT EXISTS(SELECT 1 FROM evidence.documents n WHERE n.supersedes=d.document_id)
            GROUP BY d.document_id
        """, *population_builder.params) if required else []

        # State the population in words, so an answer can say what it
        # covered rather than leaving the reader to assume "everything".
        kinds = await self.conn.fetch(kinds_sql, list(required)) if required else []
        names = sorted(set(op.source_collections) or {r["kind"] for r in kinds if r["kind"]})
        notes = ([f"population: {', '.join(names)} records"] if names else [])
        return Coverage(population_documents=len(rows),
                        complete_documents=sum(bool(r["complete"]) for r in rows),
                        notes=notes)

    async def search(self, op: QueryOperation, concepts=None) -> EvidenceResult:
        # PostgreSQL indexes the same source spans used by citations. E5 is an
        # optional ranking signal over a scoped candidate set, never evidence.
        builder=Builder(op,concepts or {})
        predicate=builder.document_predicate("d")
        if op.subject_type or op.entity_ids or op.filters or op.relationship_path:
            population=builder.population()
            predicate+=f" AND d.document_id IN (SELECT document_id FROM ({population}) requested_entities)"
        trace = getattr(self, "trace", None)
        document_sql=(f"""SELECT d.document_id FROM evidence.documents d
          WHERE {predicate} AND NOT EXISTS(SELECT 1 FROM evidence.documents n WHERE n.supersedes=d.document_id)""")
        await self._trace_sql(f"SQL scoping documents for search ({op.output_id})",
                              document_sql, builder.params)
        documents=await self.conn.fetch(document_sql,*builder.params)
        document_ids=[r["document_id"] for r in documents]
        terms = [term for term in op.text.split()[:30] if len(term)>2]
        if trace is not None:
            try:
                await trace.step("search_scope", "Documents search will look inside",
                                 output_id=op.output_id,
                                 documents_in_scope=len(document_ids),
                                 lexical_terms=terms)
            except Exception:                            # noqa: BLE001
                pass
        rows = await self.conn.fetch("""
          SELECT s.span_id,s.text,d.title,ts_rank(to_tsvector('simple',s.text),
                 websearch_to_tsquery('simple',$1)) +
                 (SELECT count(*)::float/100 FROM unnest($2::text[]) t
                  WHERE to_tsvector('simple',s.text) @@ plainto_tsquery('simple',t)) AS score
          FROM evidence.spans s JOIN evidence.documents d USING(document_id)
          WHERE NOT EXISTS(SELECT 1 FROM evidence.documents n WHERE n.supersedes=d.document_id)
          AND d.document_id=ANY($4::uuid[])
          AND (to_tsvector('simple',s.text) @@ websearch_to_tsquery('simple',$1)
               OR EXISTS(SELECT 1 FROM unnest($2::text[]) t WHERE strpos(lower(s.text),lower(t))>0))
          ORDER BY score DESC,s.span_id LIMIT $3
        """, op.text, terms, op.limit+1, document_ids)
        from .vectors import search as vector_search
        vector_ids,note = await vector_search(self.conn,op.text,op.limit*2,document_ids=document_ids)
        # A THIRD arm: trigram similarity, for what neither exact tokens nor
        # embeddings catch -- a misspelling, a different inflection, a name
        # written with other spacing.
        #
        # MEASURED on this corpus: "موٹرسائیکل چوری" scores 1.00 against the
        # span that actually records the motorcycle theft, which the lexical
        # arm misses when the tokens differ. An ENGLISH query against these
        # Urdu records scores 0.44 on unrelated statute text -- the same
        # drift the vector arm has -- which is why this is fused by rank and
        # never trusted alone: a weak arm contributes 1/61 while a span two
        # arms agree on accumulates.
        fuzzy = await self.conn.fetch("""
            SELECT s.span_id, word_similarity($1, s.text) AS sim
            FROM evidence.spans s
            WHERE s.document_id=ANY($3::uuid[])
              AND word_similarity($1, s.text) > $4
            ORDER BY sim DESC, s.span_id LIMIT $2
        """, op.text, op.limit*2, document_ids,
             float(os.getenv("EVIDENCE_FUZZY_THRESHOLD","0.45")))
        fuzzy_ids = [r["span_id"] for r in fuzzy]
        lexical_ids = [r["span_id"] for r in rows]
        # ── A FOURTH ARM: THE GRAPH ───────────────────────────────────────
        #
        # The fusion was lexical, semantic and fuzzy -- three ways of asking
        # "which passage LOOKS like this question". None of them can reach a
        # passage that never repeats the question's words: ask "who else was
        # involved with Kashif" and the passage naming his co-accused may
        # not contain "Kashif" at all, because the link lives in a recorded
        # relation rather than in the prose.
        #
        # So the entities the question NAMES are resolved, and the spans
        # that record their relationships are offered as a fourth ordering.
        # A span connected to more of the named entities ranks higher,
        # because a passage tying two of them together is more likely to be
        # about the question than one mentioning either alone.
        #
        # Fused, never trusted alone: reciprocal rank fusion gives a single
        # weak arm 1/61 while a span that several arms agree on accumulates.
        # An arm that finds nothing costs nothing.
        graph_ids: list = []
        try:
            mentions = (await self.resolve_mentions(op.text or "")) or []
            seeds = [m["entity_id"] for m in mentions if m.get("entity_id")][:8]
            if seeds:
                linked = await self.conn.fetch("""
                    SELECT a.span_id, count(DISTINCT
                             CASE WHEN a.subject_id = ANY($1::uuid[]) THEN a.subject_id
                                  ELSE a.object_entity_id END) AS reached
                    FROM evidence.assertions a
                    WHERE (a.subject_id = ANY($1::uuid[])
                           OR a.object_entity_id = ANY($1::uuid[]))
                      AND a.span_id IS NOT NULL
                      AND a.document_id = ANY($2::uuid[])
                      AND a.status='validated' AND a.retracted_at IS NULL
                      AND a.valid_to IS NULL
                    GROUP BY a.span_id
                    ORDER BY reached DESC, a.span_id
                    LIMIT $3
                """, seeds, document_ids, op.limit * 2)
                graph_ids = [r["span_id"] for r in linked]
        except Exception:                                # noqa: BLE001
            # An arm that cannot run contributes nothing and breaks nothing.
            graph_ids = []
        scores = {}
        for ordering in (lexical_ids,vector_ids,fuzzy_ids,graph_ids):
            for rank,span_id in enumerate(ordering,1):
                scores[span_id] = scores.get(span_id,0)+1/(60+rank)
        ranked = sorted(scores,key=lambda key:(-scores[key],str(key)))[:op.limit]
        # Retrieval was invisible in the trace, so a hybrid search read as
        # "one SQL query and nothing else". Both arms and the fusion are
        # recorded, including when the semantic arm returns nothing and why.
        if trace is not None:
            try:
                await trace.step("retrieval", "Lexical, semantic, fuzzy and graph arms fused",
                                 output_id=op.output_id,
                                 lexical_hits=len(lexical_ids),
                                 semantic_hits=len(vector_ids),
                                 graph_hits=len(graph_ids),
                                 semantic_note=note or "semantic arm ran",
                                 fuzzy_hits=len(fuzzy_ids),
                                 overlap=len(set(lexical_ids) & set(vector_ids)),
                                 fusion="reciprocal rank fusion over lexical, semantic, fuzzy and graph, k=60",
                                 kept=len(ranked))
            except Exception:                            # noqa: BLE001
                pass
        hydrated = {s.span_id:s for s in await self.spans(ranked)}
        citations = [hydrated[sid] for sid in ranked if sid in hydrated
                     and hydrated[sid].document_id in set(document_ids)]
        valid = {s.span_id for s in citations}
        data = [{"span_id":str(s.span_id),"text":s.text,"title":s.title} for s in citations]
        # ── IS THE SEARCHED PHRASE IN THE CORPUS AT ALL? ──────────────────
        #
        # MEASURED, on "does Bhara Kahu appear anywhere in the records": the
        # fuzzy and semantic arms returned ten near-misses, composition
        # correctly refused every one of them for want of support, and the
        # answer read "Candidate sources were retrieved; an answer has not
        # yet been established." Every stage was right and the sentence was
        # useless -- the reader cannot tell a phrase that is absent from one
        # the system merely failed to handle, and those need opposite
        # responses.
        #
        # An exhaustive existence check settles it, and it is the one
        # negative this system can state rather than infer: `ground_forms`
        # asks the whole corpus, not the filtered population, so "this
        # phrase is in no recorded span, entity name or value" is a
        # measurement. Recorded, never asserted here -- rendering decides
        # what to say with it.
        absent: list[str] = []
        phrase = (op.text or "").strip()
        # ONLY A NAME OR A VALUE CAN BE "ABSENT". A SENTENCE CANNOT.
        #
        # MEASURED, and this is my own feature misfiring: asked "how many
        # FIRs were registered in June 2026", the planner hit a rate refusal,
        # fell back to a passage search carrying the WHOLE QUESTION as its
        # text, and this then announced that "'how many FIRs were registered
        # in June 2026' does not appear in any recorded passage, entity name
        # or value". Perfectly true, and worthless: almost no sentence
        # appears verbatim in a corpus, so the claim carries no information
        # while reading as a definite finding.
        #
        # An absence is worth stating about the kind of thing a record could
        # hold -- a name, a code, a place. A phrase long enough to be a
        # sentence is not that, and a phrase ending in a question mark is the
        # question itself.
        words = phrase.split()
        checkable = (phrase and len(words) <= 6 and len(phrase) <= 60
                     and not phrase.endswith("?"))
        if checkable and not any(phrase.lower() in (s.text or "").lower() for s in citations):
            try:
                if not (await self.ground_forms([phrase])).get(phrase):
                    absent = [phrase[:200]]
            except Exception:                            # noqa: BLE001
                # A check that could not run establishes nothing, and an
                # unknown is never reported as an absence.
                absent = []
        return EvidenceResult(output_id=op.output_id, scope_fingerprint=self.scope.fingerprint(),
                              operation=op, label="Source passages", rows=data, citations=citations,
                              coverage=Coverage(population_documents=len(document_ids),truncated=len(rows)>op.limit,
                                                absent_literals=absent,
                                                notes=["Search passages do not establish an exhaustive count."]+([note] if note else [])),
                              status="partial" if data else "not_extracted")

    async def ground_forms(self, forms: list[str]) -> dict[str, int]:
        """How often each written form actually occurs in the records.

        The VALIDATOR for the understanding stage. A model asked for the
        Urdu of "vehicle" will answer something; whether the records use
        that word is a different question, and only the corpus can settle
        it. A form that occurs is confirmed evidence that the translation
        landed; a form that occurs nowhere is a guess, and searching for it
        can only add noise.

        No dictionary is consulted and none is written. The judgement is
        made against the documents as they are, so it changes when they do.
        """
        if not forms:
            return {}
        # OCCURS, not how many times.
        #
        # This counted every match in three tables for every form, and on a
        # question with a dozen forms it exceeded the statement timeout --
        # a validator slow enough to be skipped validates nothing. Whether
        # a form appears is the whole question, so EXISTS short-circuits on
        # the first hit and the trigram indexes carry it.
        # Checked in SMALL BATCHES, because one statement asking about
        # thirty forms exceeds the statement timeout and takes the whole
        # validation down with it. MEASURED: 4 forms 0.2s, 31 forms
        # cancelled at 15s.
        out: dict[str, int] = {}
        batch = int(os.getenv("EVIDENCE_GROUNDING_BATCH", "8"))
        unique = list(dict.fromkeys(f.strip() for f in forms if f and f.strip()))
        for start in range(0, len(unique), batch):
            out.update(await self._ground_batch(unique[start:start + batch]))
        return out

    async def visible_documents(self) -> int:
        """How many documents this scope can actually see.

        MEASURED: an account with no case assignments asked "how many FIRs
        cite the PPC". Row-level security correctly showed it nothing, and
        the runtime then spent 115 seconds and seven planning attempts
        failing to build a filter over an empty corpus before reporting
        "the local evidence service could not complete this request" --
        which reads as a malfunction rather than as "you have no cases".

        Asked once, up front, so an empty scope is answered in a second and
        in words the reader can act on.
        """
        return int(await self.conn.fetchval(
            "SELECT count(*) FROM evidence.documents") or 0)

    async def case_documents(self) -> int:
        """How many CASE records this scope can reach, which is the real question.

        `visible_documents` counts everything readable, and that cannot
        answer it: MEASURED, an account with no assignments still reads
        6,977 documents -- statutes, circulars, general reference material
        that row-level security lets through on purpose. A non-zero count
        there says nothing about whether one FIR is in reach.

        It is also not answerable from the scope's case id list. An empty
        list means two different things -- "this user has no cases" and "no
        case filter was asked for" -- and treating the second as the first
        told a caller that drives the runtime without a case filter that it
        had no access, which is false.

        So it is counted under this scope's own row-level security, which is
        the only thing that knows which case records this reader may see.
        """
        return int(await self.conn.fetchval(
            "SELECT count(*) FROM evidence.documents "
            "WHERE case_ids IS NOT NULL AND cardinality(case_ids) > 0") or 0)

    async def carriers(self, predicate_ids: list) -> dict:
        """Which entity TYPES actually carry each predicate, measured.

        A plan can be well formed, use real concept ids, compile to valid SQL
        and still be unable to match anything, because the property it
        filters on does not hang off the entity it is listing.

        MEASURED, on "which accused were arrested": the reader translated
        "arrested" to گرفتار, the value lookup found it in `arrest status`,
        and the planner filtered on exactly that -- then listed PERSONS.
        `arrest status` is recorded against the FIR in all 94 of its
        assertions, so a person is never its subject and the answer came
        back "No matching records in the checked population", which reads
        as "there are none".

        The corpus knows what carries what, so it is asked rather than
        assumed. No grain string is parsed and no rule is written down: the
        answer changes when the records do.
        """
        wanted = [k for k in dict.fromkeys(predicate_ids or []) if k]
        if not wanted:
            return {}
        rows = await self.conn.fetch("""
            SELECT a.predicate_id, e.concept_id AS subject_type,
                   count(*) AS n
            FROM evidence.assertions a
            JOIN evidence.entities e ON e.entity_id = a.subject_id
            WHERE a.predicate_id = ANY($1::uuid[])
              AND a.status='validated' AND a.retracted_at IS NULL
              AND a.valid_to IS NULL
            GROUP BY a.predicate_id, e.concept_id
        """, wanted)
        out: dict = {}
        for row in rows:
            out.setdefault(row["predicate_id"], []).append(
                (row["subject_type"], int(row["n"])))
        for key in out:
            out[key].sort(key=lambda pair: -pair[1])
        return out

    async def recorded_vocabulary(self) -> list[str]:
        """Every label this corpus actually carries data under.

        Populations and fields only, and only those with rows behind them: a
        concept nothing was ever recorded against is not part of what these
        records can answer about.

        Used to tell a reader that the thing they asked about is not a field
        here. MEASURED: asked whether FIRs use the standard Form 154, the
        system searched 602 documents, established nothing, and reported
        "an answer has not yet been established" after seven planning
        attempts and two minutes. There is no form field in these records
        and never was -- none of the 41 predicates mentions one -- so no
        plan could have answered it. The same question asked of the
        "malkhana" evidence store has the same shape: `weapon register item
        detail` exists with 32 rows, an evidence-store field does not.

        "The records carry no field for that, and here is what they do
        carry" is both true and useful. Silence after two minutes is
        neither.
        """
        rows = await self.conn.fetch(
            """
            SELECT c.label, c.kind
            FROM evidence.concepts c
            WHERE (c.kind = 'entity_type'
                   AND EXISTS (SELECT 1 FROM evidence.entities e
                               WHERE e.concept_id = c.concept_id))
               OR (c.kind IN ('property', 'relation', 'event_type')
                   AND EXISTS (SELECT 1 FROM evidence.assertions a
                               WHERE a.predicate_id = c.concept_id
                                 AND a.status = 'validated'
                                 AND a.retracted_at IS NULL))
            ORDER BY c.label
            """)
        return [r["label"] for r in rows if r["label"]]

    async def population_by_scope(self, subject_type) -> dict:
        """How many records of this type sit inside case documents, and in total.

        THE RECORD TYPES A CASE-SCOPED QUESTION CANNOT SEE.

        `source_scope="case_documents"` compiles to `cardinality(case_ids)>0`,
        which is the right meaning for "documents attached to a case". The
        trouble is what that silently excludes. MEASURED on this corpus:

            endpoint            docs   with no case link
            fir                   73    0
            roznamcha             74    1
            criminal-records      33   33      <- every one
            pkm                   14   14      <- every one
            cms                    4    4      <- every one

        So criminal records, citizen-service applications and walk-in
        complaints are attached to no case at all. A plan that counts them
        under `case_documents` returns 0 with a population of 0 documents,
        and the run reports that it could not complete the part -- for a
        question whose answer is 33.

        This was found from a real user's session: four questions in a row
        failed, every one of them about a record type with no case link,
        while their fifth question about police stations answered correctly.

        Counting both ways makes the difference visible, so the scope can be
        corrected from measurement instead of guessed at.
        """
        row = await self.conn.fetchrow(
            """
            SELECT count(*) FILTER (
                       WHERE EXISTS (SELECT 1 FROM evidence.documents d
                                     WHERE d.document_id = e.document_id
                                       AND cardinality(d.case_ids) > 0)) AS in_cases,
                   count(*) AS everywhere
            FROM evidence.entities e
            WHERE e.concept_id = $1
              AND EXISTS (SELECT 1 FROM evidence.documents d
                          WHERE d.document_id = e.document_id)
            """,
            subject_type)
        return {"in_case_documents": int(row["in_cases"] or 0),
                "anywhere": int(row["everywhere"] or 0)}

    async def recorded_values(self, predicate_id, *, limit: int = 60) -> list[dict]:
        """Every value this predicate actually holds, with how many rows hold it.

        THE SET A FILTER MAY CHOOSE FROM.

        MEASURED, and the reason this exists: "کرمنل ریکارڈ سسٹم میں کتنے
        کیس مکمل ہو چکے ہیں اور کتنے ابھی زیرِ کارروائی ہیں" -- how many
        cases are completed and how many still in process -- compiled to
        `conviction status = 'completed'` and matched nothing, because the
        values this corpus stores are

            Under trial                               30
            Convicted, on bail pending appeal          1
            Sub judice, external prior case            1
            Under trial, priority hearing requested    1

        "completed" is not one of them and never will be: it is a READING of
        the ones that are. With the filter matched literally the plan
        returned zero, the ladder fell back to `search`, and search cannot
        count -- so a question whose answer is "1 completed, 32 in process"
        reported that 0 of 602 documents had been checked.

        Returning the values makes the choice resolvable instead of guessed:
        the caller matches the asked word against these, and filters on the
        ones it covers. Measured from the records, so it changes when they
        do, and no status vocabulary is stored anywhere.
        """
        rows = await self.conn.fetch(
            """
            SELECT a.value_text AS value, count(*) AS rows
            FROM evidence.assertions a
            WHERE a.predicate_id = $1
              AND a.value_text IS NOT NULL AND a.value_text <> ''
              AND a.status = 'validated' AND a.retracted_at IS NULL
              AND a.valid_to IS NULL
              AND length(a.value_text) <= 120
              AND EXISTS (SELECT 1 FROM evidence.documents d
                          WHERE d.document_id = a.document_id)
            GROUP BY a.value_text
            ORDER BY count(*) DESC, a.value_text
            LIMIT $2
            """,
            predicate_id, limit)
        return [{"value": r["value"], "rows": int(r["rows"])} for r in rows]

    async def where_values_occur(self, forms: list[str], *,
                                per_form: int = 3,
                                max_concepts: int = 6) -> list[dict]:
        """Which concept actually HOLDS each written form as a recorded value.

        THE SIGNAL THE PLANNER NEVER HAD

        MEASURED, on "how many FIRs cite the PPC": the planner filtered
        `statute section code` for 'PPC' and compiled valid SQL that returned
        0. It could not have known better. "PPC" is a VALUE in this corpus,
        not a word in any concept's label or meaning, so no amount of
        catalog ranking connects the two -- ts_rank scored every one of the
        47 concepts at exactly 0 and the catalog came back in ALPHABETICAL
        order, with `statute act` at position 37 of 41.

        The corpus knows the answer and answers it in under a millisecond:
        'PPC' appears in 61 subjects of `statute act`, and in 2 of `offence
        summary`. That is the truth (61 of 73) handed over before a single
        plan is written, rather than discovered by `suspicious_zero` after a
        wrong one has already run.

        This is a lookup, not a vocabulary. Nothing is stored, no synonym
        list is consulted, and the result changes when the records do. A
        form is reported with the number of subjects that hold it and a few
        sample values, so the planner can tell a discriminating field from a
        coincidence and choose on evidence instead of on a label.

        A form matching more than `max_concepts` concepts is not reported:
        it is a word like "the" that occurs everywhere and distinguishes
        nothing. That threshold is a property of the match, not a stopword
        list -- no word is named anywhere in this method.

        Scope is enforced by row-level security on `evidence.assertions`,
        as it is for every other read on this connection.
        """
        unique = [f.strip() for f in dict.fromkeys(forms or []) if f and len(f.strip()) > 1]
        if not unique:
            return []
        # WHOLE WORDS, not substrings.
        #
        # MEASURED: a plain ILIKE '%in%' reported that "in" is held by
        # `statute act` in 33 subjects -- it was matching the "in" inside
        # "Ordinance". Every short function word produced a confident false
        # location that way. A word-boundary match drops those and leaves
        # the real ones: 'PPC' still resolves to `statute act`, 61 subjects.
        patterns = [r"\m" + _posix_escape(f) + r"\M" for f in unique]
        out: list[dict] = []
        batch = int(os.getenv("EVIDENCE_GROUNDING_BATCH", "8"))
        for start in range(0, len(unique), batch):
            rows = await self.conn.fetch("""
                WITH wanted AS (
                    SELECT DISTINCT btrim(f.form) AS form, f.pat
                    FROM unnest($1::text[], $4::text[]) AS f(form, pat)
                    WHERE length(btrim(f.form)) > 1),
                hits AS (
                    SELECT w.form, a.predicate_id,
                           count(DISTINCT a.subject_id) AS subjects,
                           count(DISTINCT a.subject_id)
                             FILTER (WHERE lower(btrim(a.value_text)) = lower(w.form))
                             AS exact_subjects,
                           (array_agg(DISTINCT left(a.value_text, 40)))[1:3] AS samples
                    FROM wanted w
                    JOIN evidence.assertions a
                      ON a.value_text ~* w.pat
                     AND a.status='validated' AND a.retracted_at IS NULL
                     AND a.valid_to IS NULL
                     -- A DERIVED SUMMARY IS NOT A RECORDED VALUE.
                     --
                     -- This lookup answers "which field HOLDS this literal",
                     -- and every recorded value is short: a code, a name, a
                     -- status. Summaries are prose, so once they existed every
                     -- common word matched one. MEASURED, on the Urdu form of
                     -- "how many FIRs cite the PPC": کا ("of") was reported as
                     -- held by `roznamcha entry text` in 73 subjects and میں
                     -- ("in") by `FIR summary` in 7 -- noise that would point
                     -- the planner at a narrative field for a count.
                     --
                     -- Derived text is excluded by its extraction version, so
                     -- the signal keeps describing the records rather than
                     -- what was written about them.
                     AND a.extraction_version <> 'derived-summary-v1'
                     -- A VALUE IS ABOUT AS LONG AS THE TERM THAT NAMES IT.
                     --
                     -- Excluding derived text was not enough: `roznamcha
                     -- entry text` and `offence summary` are prose fields the
                     -- corpus already had, and an Urdu function word appears
                     -- in all of them. MEASURED, for the Urdu form of "how
                     -- many FIRs cite the PPC": کا ("of") was reported as held
                     -- by `roznamcha entry text` in 73 subjects, میں ("in") by
                     -- `reporting delay reason` in 6.
                     --
                     -- Every one of those matched with exact=0, while the real
                     -- signal, PPC in `statute act`, matched 61 of 61 exactly.
                     -- The difference is proportion: "PPC" IS the value, while
                     -- کا is two characters inside two hundred. A value may be
                     -- somewhat longer than its term -- "Arms Ordinance"
                     -- against "Arms Ordinance 1965" -- so the bound is
                     -- generous, and it is a property of the match rather than
                     -- a list of words.
                     AND length(a.value_text) <= greatest(40, length(w.form) * 4) 
                    GROUP BY w.form, a.predicate_id),
                ranked AS (
                    SELECT h.*,
                           row_number() OVER (PARTITION BY form
                                              ORDER BY subjects DESC, predicate_id) AS rn,
                           count(*) OVER (PARTITION BY form) AS concepts_matched
                    FROM hits h)
                SELECT r.form, r.predicate_id, k.label, r.subjects,
                       r.exact_subjects, r.samples, r.concepts_matched
                FROM ranked r JOIN evidence.concepts k ON k.concept_id = r.predicate_id
                WHERE r.rn <= $2 AND r.concepts_matched <= $3
                ORDER BY r.form, r.subjects DESC
            """, unique[start:start + batch], per_form, max_concepts,
                patterns[start:start + batch])
            out.extend({"form": r["form"], "concept_id": str(r["predicate_id"]),
                        "holds_this_value": r["label"],
                        "subjects_with_this_value": int(r["subjects"]),
                        # An exact value is far stronger evidence than a
                        # value that merely contains the word.
                        "subjects_where_value_is_exactly_this": int(r["exact_subjects"] or 0),
                        "sample_values": [x for x in (r["samples"] or []) if x]}
                       for r in rows)
        return out

    async def _ground_batch(self, forms: list[str]) -> dict[str, int]:
        rows = await self.conn.fetch("""
            WITH wanted AS (SELECT DISTINCT btrim(f) AS form
                            FROM unnest($1::text[]) f WHERE btrim(f) <> '')
            SELECT w.form,
                   (EXISTS(SELECT 1 FROM evidence.spans s
                            WHERE s.text ILIKE '%'||w.form||'%')
                 OR EXISTS(SELECT 1 FROM evidence.entities e
                            WHERE e.label ILIKE '%'||w.form||'%')
                 OR EXISTS(SELECT 1 FROM evidence.concepts c
                            WHERE c.label ILIKE '%'||w.form||'%'
                               OR c.meaning ILIKE '%'||w.form||'%')) AS present
            FROM wanted w
        """, forms)
        return {r["form"]: (1 if r["present"] else 0) for r in rows}

    async def suspicious_zero(self, op, concepts):
        """Why a measure came back zero: a true absence, or the wrong field.

        A count of 0 over a COMPLETE population reads as "I checked every
        record and there are none". That is the most authoritative thing
        this system says, and it is exactly what a wrong field produces.

        MEASURED, both answered `0` with coverage 73/73:

          "how many cases involve the PPC legal code"
             filtered `statute section code` contains 'PPC'
             that concept holds 34, 13, 392, 9(c) -- PPC is the ACT.
             Truth: 61 of 73.

          "how many people are recorded as accused"
             filtered on `accused in`, which has 94 recorded links.
             Truth: 92.

        So: if the concept the plan filtered on holds values in the corpus
        and the filter matched none of them, the plan is asking the wrong
        question rather than reporting a true absence. The distinction is
        checkable, and the answer names the concept, how many values it
        holds and what they look like, so the next rung can correct it.

        A concept with NO values anywhere is a real absence and passes.
        """
        keys = [f.predicate_id for f in op.filters if f.predicate_id]
        if op.predicate_id:
            keys.append(op.predicate_id)
        for key in dict.fromkeys(keys):
            concept = concepts.get(key)
            row = await self.conn.fetchrow("""
                SELECT count(*) AS n,
                       array_agg(DISTINCT left(coalesce(a.value_text,
                           a.value_number::text, a.value_date::text,''),40))
                         FILTER (WHERE a.value_text IS NOT NULL
                                    OR a.value_number IS NOT NULL
                                    OR a.value_date IS NOT NULL) AS samples
                FROM evidence.assertions a
                WHERE a.predicate_id=$1 AND a.status='validated'
                  AND a.retracted_at IS NULL AND a.valid_to IS NULL
            """, key)
            if row and int(row["n"] or 0) > 0:
                samples = [x for x in (row["samples"] or []) if x][:6]
                label = getattr(concept, "label", str(key))
                return (f"'{label}' holds {row['n']} recorded value(s) and this "
                        f"filter matched none of them, so the population is not "
                        f"empty -- the field is wrong."
                        + (f" Recorded values look like: {', '.join(samples)}."
                           if samples else "")
                        + " Choose the concept whose MEANING holds the kind of "
                          "value the question names.")
        return None

    async def unmatched_names(self, op, concepts):
        """Names the question used that match no entity on record.

        "No FIRs at that station" and "there is no such station" are
        different answers, and an empty result says only the first.

        MEASURED, on "Summarize the FIR registered at Bhara Kahu police
        station for mobile or vehicle theft": the answer was "No matching
        records in the checked population". Correct -- this corpus records
        19 stations and none is Bhara Kahu -- but indistinguishable from a
        malfunction. The reader cannot tell whether the station has no
        cases, or does not exist, or the system broke.

        Returns one entry per unmatched name, each with the names that ARE
        recorded for that type, so the answer can say which it was and what
        the alternatives are. Names come from the corpus, never a list.
        """
        wanted = []
        if op.text and op.subject_type:
            wanted.append((op.text, op.subject_type))
        for item in op.filters:
            name = getattr(item, "object_name", None)
            if not name:
                continue
            # Which TYPE the relation actually points at, read from the
            # recorded assertions rather than assumed: "recorded at police
            # station" points at police stations, so those are the names
            # worth listing back.
            points_at = await self.conn.fetchval("""
                SELECT e.concept_id FROM evidence.assertions a
                JOIN evidence.entities e ON e.entity_id=a.object_entity_id
                WHERE a.predicate_id=$1 GROUP BY e.concept_id
                ORDER BY count(*) DESC LIMIT 1""", item.predicate_id)
            wanted.append((name, points_at))
        missing = []
        for name, concept_id in wanted:
            found = await self.resolve(name, limit=1)
            if found:
                continue
            available = await self.conn.fetch("""
                SELECT DISTINCT e.label FROM evidence.entities e
                JOIN evidence.concepts k ON k.concept_id=e.concept_id
                WHERE ($1::uuid IS NULL OR k.concept_id=$1)
                  AND EXISTS(SELECT 1 FROM evidence.documents d
                             WHERE d.document_id=e.document_id)
                ORDER BY e.label LIMIT 25""", concept_id)
            missing.append({"name": name,
                            "known": [r["label"] for r in available]})
        return missing

    async def _trace_sql(self, title, sql, params):
        """Show the statement that was actually sent. Never raises."""
        trace = getattr(self, "trace", None)
        if trace is None:
            return
        try:
            from .trace import describe_sql
            await trace.step("sql", title, **describe_sql(sql, params))
        except Exception:                                # noqa: BLE001
            pass

    async def execute(self, op: QueryOperation, concepts: dict[UUID, Concept]) -> EvidenceResult:
        named = [(op.text,op.name_match_mode)] if op.text and op.operation in {'list','count_entities'} and not op.entity_ids else []
        named.extend((f.object_name,f.name_match_mode) for f in op.filters if f.object_name)
        for name,mode in named:
            if mode != 'unique': continue
            candidates = await self.resolve(name,limit=16)
            identities = {str(r.get('canonical_id') or r.get('cnic') or r['entity_id']) for r in candidates}
            # SEVERAL RECORDS OF ONE THING IS NOT AN AMBIGUITY.
            #
            # MEASURED, on "summarize the cases recorded at Model Town": the
            # station تھانہ ماڈل ٹاؤن، لاہور exists as SEVEN entities, one per
            # FIR, never linked into a single identity. Counting identities
            # therefore saw seven and asked "several recorded identities match
            # this name; which do you mean?" -- offering a choice between seven
            # things with the SAME name, which no reader can answer.
            #
            # A question worth asking needs two DIFFERENT names behind it: two
            # people called کاشف is a real ambiguity, one station recorded
            # seven times is not. So the distinct LABELS decide, and a name
            # that denotes one thing recorded many times simply filters on all
            # of them -- which is what the question meant.
            distinct_names = {" ".join(str(r.get('label') or '').lower().split())
                              for r in candidates}
            distinct_names.discard('')
            if len(identities) > 1 and len(distinct_names) > 1:
                return EvidenceResult(output_id=op.output_id,scope_fingerprint=self.scope.fingerprint(),operation=op,
                    label=name,status='ambiguous',rows=[{'label':r['label']} for r in candidates],
                    coverage=Coverage(notes=['Several recorded identities match this name; clarify before filtering.']))
        if op.operation == "search":
            return await self.search(op,concepts)
        if op.operation in {"traverse", "network"}:
            return await self.graph(op, concepts)
        compiled = compile_operation(op, concepts)
        await self._trace_sql(f"SQL compiled for {op.operation} ({op.output_id})",
                              compiled.sql, compiled.params)
        rows = [dict(r) for r in await self.conn.fetch(compiled.sql, *compiled.params)]
        unknown_names = [] if rows else await self.unmatched_names(op, concepts)
        # A zero that is really a wrong field must not reach the reader as
        # "checked everything, found none". Raised so the planning ladder
        # can reconsider the concept; see EvidenceStore.suspicious_zero.
        # AN EMPTY LIST IS A ZERO TOO.
        #
        # This covered counts only, so a `list` that matched nothing rendered
        # "No matching records in the checked population" -- the most
        # authoritative sentence this system has -- without the check that
        # distinguishes a true absence from a wrong field. MEASURED twice
        # today: "which accused were arrested" and "summarize the cases
        # recorded at Model Town" both said it while the records held the
        # answer.
        empty_list = (op.operation in {"list", "timeline"}
                      and (op.filters or op.predicate_id)
                      and not rows and not op.group_by)
        if empty_list or (op.operation in {"count_entities", "count_assertions", "sum"}
                and (op.filters or op.predicate_id)
                and rows and not op.group_by
                and (rows[0].get("value") in (0, "0"))):
            complaint = await self.suspicious_zero(op, concepts)
            if complaint:
                raise InvalidPlan(complaint)
        if op.group_by and not rows:
            # A grouping that produces NO groups is a wrong direction, not an
            # empty world -- and it renders as a confident blank answer.
            #
            # MEASURED, on "which people are accused in more than one case":
            # the plan grouped PERSONS by `accused in` and returned rows=0 with
            # complete coverage 73/73, reported as `answered`. The relation is
            # stored FIR --> person, so a person is never its subject and there
            # was nothing to group. The same question over FIR returns exactly
            # what was asked: عاصم رشید 2, and so on.
            #
            # The store knows which type IS the subject of that relation, so it
            # says so, and the planning ladder can act on it. Silence here
            # cannot be repaired by anything.
            subject = await self.conn.fetchrow("""
                SELECT k.label, count(*) AS n
                FROM evidence.assertions a
                JOIN evidence.entities e ON e.entity_id=a.subject_id
                JOIN evidence.concepts k ON k.concept_id=e.concept_id
                WHERE a.predicate_id=$1 GROUP BY k.label ORDER BY n DESC LIMIT 1
            """, op.group_by)
            if subject and subject["label"]:
                raise InvalidPlan(
                    f"Grouping by this relation produced no groups: it is recorded on "
                    f"'{subject['label']}', not on the requested subject type. Set "
                    f"subject_type to '{subject['label']}' and group by the same relation.")
        coverage = await self.coverage(op, concepts)
        coverage.truncated = len(rows)>op.limit
        rows = rows[:op.limit]
        ids = {i for r in rows for i in (r.get("span_ids") or [])}
        citations = await self.spans(list(ids))
        if len(citations) != len(ids):
            raise InvalidPlan("Some result provenance is missing or no longer resolves")
        if op.operation in {"count_entities", "sum", "average", "average_duration"}:
            coverage.unresolved_identities = sum(int(r.get("unresolved_identities") or 0) for r in rows)
        coverage.missing_values += sum(int(r.get("missing_measurements") or 0) for r in rows)
        if op.operation in {"sum", "average"}:
            # An inner join cannot hide entities whose measurement is missing.
            probe = (f"WITH population AS ({compiled.population_sql}) SELECT count(*) FROM population e "
                     f"WHERE NOT EXISTS(SELECT 1 FROM evidence.assertions a WHERE a.subject_id=e.entity_id "
                     f"AND a.predicate_id=${len(compiled.population_params)+1} AND a.status='validated' "
                     "AND a.retracted_at IS NULL AND a.valid_to IS NULL AND a.value_number IS NOT NULL)")
            coverage.missing_values = await self.conn.fetchval(probe, *compiled.population_params, op.predicate_id)
            # More than one active measure for an entity may be corroboration,
            # contradiction, or multiple events. Do not sum it as extra items.
            duplicate_probe = (f"WITH population AS ({compiled.population_sql}) SELECT count(*) FROM ("
                               f"SELECT coalesce(e.canonical_id,e.entity_id) FROM population e JOIN evidence.assertions a ON a.subject_id=e.entity_id "
                               f"WHERE a.predicate_id=${len(compiled.population_params)+1} AND a.status='validated' "
                               "AND a.retracted_at IS NULL AND a.valid_to IS NULL GROUP BY coalesce(e.canonical_id,e.entity_id) HAVING count(*)>1) ambiguous")
            duplicates = await self.conn.fetchval(duplicate_probe,*compiled.population_params,op.predicate_id)
            if duplicates:
                coverage.notes.append("Multiple active measurements require reconciliation before aggregation.")
                coverage.missing_values += duplicates
        if op.group_by:
            coverage.missing_values += sum(1 for r in rows if r.get("group_value") is None)
        # Disputes are not hidden by the validated-only query.
        required = [i for i in [op.predicate_id,op.start_predicate_id,op.end_predicate_id, *(f.predicate_id for f in op.filters), op.group_by] if i]
        coverage.disputed_assertions = await self.conn.fetchval("""
          SELECT count(*) FROM evidence.assertions WHERE status='disputed'
          AND (predicate_id=ANY($1::uuid[]) OR $2::uuid IS NOT NULL AND subject_id IN (
            SELECT entity_id FROM evidence.entities WHERE concept_id=$2))
        """, required, op.subject_type)
        if op.as_of or op.known_at:
            unknown_time = await self.conn.fetchval("""SELECT count(*) FROM evidence.assertions a
              WHERE a.status IN ('validated','superseded') AND a.predicate_id=ANY($1::uuid[])
              AND a.valid_from IS NULL""",required)
            if unknown_time:
                coverage.missing_values += unknown_time
                coverage.notes.append("Some assertions lack the validity dates required for a historical total.")
        measure = concepts.get(op.predicate_id or op.subject_type)
        # Name what was counted and what it was broken down by, so the
        # answer opens with a sentence instead of a bare concept name.
        #
        # MEASURED: a grouped ranking opened with the single word "FIR" and
        # the reader had no way to tell what the numbers beside each name
        # were counting.
        subject = concepts.get(op.subject_type) if op.subject_type else None
        grouping = concepts.get(op.group_by) if op.group_by else None
        label = measure.label if measure else (subject.label if subject else op.operation)
        if grouping is not None:
            label = f"{subject.label if subject else label} by {grouping.label}"
        exact = op.operation in {"count_entities", "count_assertions", "sum", "average", "average_duration"}
        status = "answered"
        if exact and not coverage.complete:
            status = "partial" if coverage.complete_documents else "not_extracted"
        elif coverage.truncated:
            status = "partial"
        elif not rows:
            status = "not_extracted" if not coverage.complete else "answered"
        for entry in unknown_names:
            known = ", ".join(entry["known"][:12])
            coverage.notes.append(
                f"No record carries the name '{entry['name']}'."
                + (f" Recorded names include: {known}." if known else ""))
        metric = None
        if op.operation == "average_duration":
            unknown = (await self.conn.fetchval(compiled.unknown_cohort_sql, *compiled.unknown_cohort_params)
                       if compiled.unknown_cohort_sql else 0)
            counts = {key: sum(int(row.get(key) or 0) for row in rows)
                      for key in ("eligible_records", "observed_records", "missing_records",
                                  "conflicting_records", "negative_duration_records")}
            metric = MetricEvidence(**counts, unknown_cohort_records=unknown,
                start_predicate_id=op.start_predicate_id, end_predicate_id=op.end_predicate_id,
                period_start=op.occurred_after, period_end=op.occurred_before,
                basis="exact_population" if coverage.complete and not unknown else "observed_subset")
            if metric.basis == "observed_subset":
                status = "partial" if coverage.complete_documents else "not_extracted"
            start = concepts[op.start_predicate_id].label
            end = concepts[op.end_predicate_id].label
            label = f"{start} → {end}"
            if op.occurred_after or op.occurred_before:
                label += f" [{op.occurred_after.isoformat() if op.occurred_after else '*'}, {op.occurred_before.isoformat() if op.occurred_before else '*'})"
        return EvidenceResult(output_id=op.output_id, scope_fingerprint=self.scope.fingerprint(),
                              operation=op, label=label,
                              measured=measured_parts(op, concepts),
                              unit="seconds" if op.operation=="average_duration" else measure.unit if measure else None,
                              rows=json.loads(json.dumps(rows, default=str)), citations=citations,
                              coverage=coverage, status=status,
                              query_hash=hashlib.sha256(compiled.sql.encode()).hexdigest(), metric=metric)

    async def graph(self, op: QueryOperation, concepts: dict[UUID, Concept]) -> EvidenceResult:
        # Reconstruct a graph from the visible relational projection each time.
        # A global precomputed community must never disclose another case.
        import networkx as nx
        for key in op.relationship_path:
            if key not in concepts or concepts[key].kind != "relation":
                raise InvalidPlan("Unknown relationship")
        b = Builder(op, concepts)
        temporal = b.temporal("r")
        path_param = b.param(op.relationship_path)
        document_predicate=b.document_predicate("d")
        edges = await self.conn.fetch(f"""
           SELECT r.assertion_id,r.subject_id,r.object_entity_id,r.predicate_id,r.span_id,
                  a.label AS subject_label,b.label AS object_label,k.label AS predicate_label,
                  {identity_key('a')} AS subject_node,
                  {identity_key('b')} AS object_node,
                  a.identity_status AS subject_identity,b.identity_status AS object_identity
           FROM evidence.assertions r
           JOIN evidence.entities a ON a.entity_id=r.subject_id
           JOIN evidence.entities b ON b.entity_id=r.object_entity_id
           JOIN evidence.concepts k ON k.concept_id=r.predicate_id
           WHERE {temporal} AND ({path_param}::uuid[]='{{}}' OR r.predicate_id=ANY({path_param}))
           AND EXISTS(SELECT 1 FROM evidence.documents d WHERE d.document_id=r.document_id AND {document_predicate})
           AND NOT EXISTS(SELECT 1 FROM evidence.documents n WHERE n.supersedes=r.document_id)
           ORDER BY r.assertion_id LIMIT 5001
        """, *b.params)
        trace = getattr(self, "trace", None)
        if trace is not None:
            try:
                await trace.step("graph", "Relationship edges loaded",
                                 output_id=op.output_id, edges=len(edges),
                                 relationship_path=[str(k) for k in op.relationship_path],
                                 sample=[{"subject": e["subject_label"],
                                          "object": e["object_label"]} for e in edges[:8]])
            except Exception:                            # noqa: BLE001
                pass
        truncated = len(edges)>5000
        edges = edges[:5000]
        graph = nx.DiGraph()
        for edge in edges:
            graph.add_edge(edge["subject_node"], edge["object_node"])
        # Named seeds identify themselves; a type filter can only remove them.
        #
        # MEASURED: "Which cases is Ahmed accused in?" planned correctly --
        # list the people named Ahmed, then traverse `accused in` from that
        # output -- and returned nothing. The traverse carried
        # subject_type=FIR, meaning the CASES it wanted back, and the seed
        # population applied it to the SEEDS: "entities that are FIRs and
        # are also these four people", which is empty. The answer came back
        # "No matching records in the checked population" for a person who
        # is accused in FIR 620/26.
        #
        # When the entities are given explicitly there is nothing left to
        # select, so the type is dropped for seeding only. It still applies
        # everywhere else, and a traverse without explicit ids is untouched.
        seed_op = op.model_copy(update={"relationship_path": []})
        if seed_op.entity_ids and seed_op.subject_type:
            seed_op = seed_op.model_copy(update={"subject_type": None})
        seed_builder = Builder(seed_op, concepts)
        seed_rows = await self.conn.fetch(seed_builder.population(), *seed_builder.params)
        seeds = {r["canonical_id"] or r["entity_id"] for r in seed_rows}
        if trace is not None:
            try:
                await trace.step("graph_seeds", "Entities the traversal starts from",
                                 output_id=op.output_id, seeds=len(seeds),
                                 labels=[r.get("label") for r in seed_rows[:10]])
            except Exception:                            # noqa: BLE001
                pass
        if op.operation == "traverse":
            frontier, selected, seen = seeds, [], set()
            for hop,predicate in enumerate(op.relationship_path or [None]*4):
                direction = op.relationship_directions[hop] if op.relationship_directions else "forward"
                next_frontier = set()
                for edge in edges:
                    if predicate is not None and edge["predicate_id"]!=predicate:
                        continue
                    # A sourced relationship is a fact about BOTH of its
                    # endpoints, so it is navigable from either one.
                    #
                    # MEASURED: "In which cases was کاشف an accused?"
                    # traversed to nothing. The relation is stored FIR -->
                    # person, because the accused entry lives inside the
                    # FIR record, and following it only forwards asks
                    # "who is accused in this person", which is not a
                    # question. The concept is even NAMED "accused in",
                    # which reads person --> FIR: the stored direction is
                    # an artefact of the source's shape, not a claim about
                    # the relationship. Four such assertions existed and
                    # none was reachable.
                    #
                    # Following an edge backwards asserts nothing new: the
                    # same assertion, span and citation stand behind it,
                    # and `network` already treats these edges as
                    # undirected when it reports connectivity.
                    reached = None
                    if direction == "forward" and edge["subject_node"] in frontier:
                        reached = edge["object_node"]
                    elif direction == "inverse" and edge["object_node"] in frontier:
                        reached = edge["subject_node"]
                    if reached is None:
                        continue
                    next_frontier.add(reached)
                    if edge["assertion_id"] not in seen:
                        selected.append(edge);seen.add(edge["assertion_id"])
                frontier = next_frontier
        else:
            # Components report supported connectivity, never inferred guilt.
            components = list(nx.connected_components(graph.to_undirected())) if graph else []
            components = [c for c in components if c & seeds]
            nodes = set().union(*components) if components else set()
            selected = [r for r in edges if r["subject_node"] in nodes]
        citations = await self.spans([r["span_id"] for r in selected[:op.limit]])
        valid = {s.span_id for s in citations}
        rows = [json.loads(json.dumps(dict(r), default=str)) for r in selected[:op.limit] if r["span_id"] in valid]
        coverage = await self.coverage(op, concepts)
        coverage.truncated = truncated or len(selected)>op.limit
        coverage.unresolved_identities = len({r[key] for r in selected for key,status in
            [("subject_id","subject_identity"),("object_entity_id","object_identity")] if r[status]!="confirmed"})
        coverage.notes.append("Connections describe sourced relationships; unconfirmed identities remain separate.")
        if op.known_at:
            coverage.missing_values += 1
            coverage.notes.append("Historical identity adjudication has not been versioned; this is a partial historical projection.")
        if op.operation=="network":
            component_ids={node:index+1 for index,component in enumerate(components) for node in component}
            for row in rows:
                row["component"] = component_ids[UUID(row["subject_node"])]
            coverage.notes.append(f"The bounded projection contains {len(components)} connected groups.")
        return EvidenceResult(output_id=op.output_id, scope_fingerprint=self.scope.fingerprint(),
                              operation=op, label="Documented relationships", rows=rows,
                              citations=citations, coverage=coverage,
                              status="answered" if coverage.complete else "partial")


def _posix_escape(text: str) -> str:
    """Quote POSIX regex metacharacters so a form matches itself literally."""
    return "".join("\\" + ch if ch in r".^$*+?()[]{}|\-" else ch for ch in text)


def measured_parts(op, concepts) -> dict:
    """What an operation actually measured, named rather than numbered.

    The renderer had the number and the entity type and nothing else, so it
    could only ever print "<entity> <metric>: <n>". Two questions that differ
    only by their filter produced the same sentence. These are the parts a
    sentence needs; they are collected here because this is where the concept
    labels are known.
    """
    def name(key):
        concept = concepts.get(key) if concepts else None
        return getattr(concept, "label", None)

    conditions = []
    for item in getattr(op, "filters", None) or []:
        field = name(item.predicate_id)
        if not field:
            continue
        value = item.value
        if value is None:
            value = getattr(item, "object_name", None)
        conditions.append({"field": field, "operator": item.operator,
                           "value": None if value is None else str(value)[:120],
                           # An `in` filter's values live here, and the
                           # answer has to be able to name them: "statute act
                           # in None" is what the reader saw before this was
                           # carried through.
                           "values": [str(v)[:120]
                                      for v in (getattr(item, "values", None) or [])]})
    period = None
    if getattr(op, "occurred_after", None) or getattr(op, "occurred_before", None):
        period = {"from": op.occurred_after.isoformat() if op.occurred_after else None,
                  "to": op.occurred_before.isoformat() if op.occurred_before else None}
    # A RELATIONSHIP PATH IS A NARROWING TOO.
    #
    # MEASURED: "how many complainants are recorded" planned
    # count_entities(FIR) reached through `complainant of` -- 71 FIRs have
    # one -- and rendered as "71 FIR records are recorded", which is what an
    # unfiltered count would say, and that would be 75. The number was right
    # and the sentence described a different question.
    through = [name(key) for key in (getattr(op, "relationship_path", None) or [])]
    return {"operation": op.operation, "subject": name(op.subject_type),
            "of": name(getattr(op, "predicate_id", None)),
            "grouped_by": name(getattr(op, "group_by", None)),
            "through": [t for t in through if t],
            "conditions": conditions, "period": period}


class ScopedStore:
    """Acquire a scoped snapshot per tool, never hold a connection during inference.

    Each result carries its own provenance and coverage. This also leaves pool
    capacity for cancellation/progress when several model requests are running.
    """
    def __init__(self, scope: Scope):
        self.scope = scope

    async def _call(self, method, *args, **kwargs):
        from .database import connection
        async with connection(self.scope) as conn:
            store = EvidenceStore(conn, self.scope)
            # A fresh store per call, so the run's trace has to travel with it
            # or every SQL statement is executed untraced.
            store.trace = getattr(self, "trace", None)
            return await getattr(store, method)(*args, **kwargs)

    async def catalog(self, *args, **kwargs):
        return await self._call("catalog", *args, **kwargs)

    async def snapshot(self, *args, **kwargs):
        return await self._call("snapshot", *args, **kwargs)

    async def field_meanings(self, *args, **kwargs):
        return await self._call("field_meanings", *args, **kwargs)

    async def resolve(self, *args, **kwargs):
        return await self._call("resolve", *args, **kwargs)

    async def resolve_mentions(self, *args, **kwargs):
        return await self._call("resolve_mentions", *args, **kwargs)

    async def same_name_forms(self, *args, **kwargs):
        return await self._call("same_name_forms", *args, **kwargs)

    async def visible_documents(self, *args, **kwargs):
        return await self._call("visible_documents", *args, **kwargs)

    async def case_documents(self, *args, **kwargs):
        return await self._call("case_documents", *args, **kwargs)

    async def carriers(self, *args, **kwargs):
        return await self._call("carriers", *args, **kwargs)

    async def where_values_occur(self, *args, **kwargs):
        return await self._call("where_values_occur", *args, **kwargs)

    async def recorded_values(self, *args, **kwargs):
        return await self._call("recorded_values", *args, **kwargs)

    async def population_by_scope(self, *args, **kwargs):
        return await self._call("population_by_scope", *args, **kwargs)

    async def recorded_vocabulary(self, *args, **kwargs):
        return await self._call("recorded_vocabulary", *args, **kwargs)

    async def ground_forms(self, *args, **kwargs):
        return await self._call("ground_forms", *args, **kwargs)

    async def execute(self, *args, **kwargs):
        op=args[0] if args else kwargs.get('op')
        if not op or op.operation not in {'network','traverse'} or os.getenv('EVIDENCE_GRAPH_CACHE_ENABLED','true').lower()!='true':
            return await self._call('execute',*args,**kwargs)
        from .database import connection
        snapshot=await self.snapshot()
        version='scoped-graph-v2'
        key=hashlib.sha256(json.dumps([version,self.scope.fingerprint(),snapshot,op.model_dump(mode='json')],sort_keys=True).encode()).hexdigest()
        async with connection(self.scope) as conn:
            cached=await conn.fetchval('SELECT result FROM evidence.projections WHERE cache_key=$1 AND expires_at>now()',key)
        if cached:
            result=EvidenceResult.model_validate(cached)
            if result.scope_fingerprint!=self.scope.fingerprint() or result.operation!=op:
                raise InvalidPlan('Cached projection does not match the authorized operation')
            return result
        result=await self._call('execute',*args,**kwargs)
        if snapshot==await self.snapshot():
            async with connection(self.scope,write=True) as conn:
                await conn.execute('DELETE FROM evidence.projections WHERE expires_at<=now()')
                await conn.execute('''INSERT INTO evidence.projections(cache_key,user_id,session_id,scope_fingerprint,
                    source_snapshot,projection_version,result,source_documents) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING''',
                    key,self.scope.user_id,self.scope.session_id,self.scope.fingerprint(),snapshot,version,result.model_dump(mode='json'),
                    list({s.document_id for s in result.citations}))
        return result
