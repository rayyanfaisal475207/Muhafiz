"""Evidence-bound rendering: models choose references, not factual replacements."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from uuid import UUID

from .types import Answer, Claim, EvidenceResult, QuestionPlan, Scope


def display_number(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite(): return str(value)
        return format(number.quantize(Decimal('0.01')), 'f').rstrip('0').rstrip('.') if number else '0'
    except (InvalidOperation,ValueError): return str(value)


def display_measure(value, unit):
    text = f"{display_number(value)} {unit or ''}".strip()
    if unit == 'seconds':
        seconds = Decimal(str(value))
        text += f" ({display_number(seconds/Decimal(3600))} hours)"
    return text


def supports_observed_metric(result: EvidenceResult) -> bool:
    """Publish only a qualified duration over an enumerated, extracted population."""
    m, c, op = result.metric, result.coverage, result.operation
    return bool(m and op.operation == "average_duration" and op.allow_observed_subset
                and m.observed_records > 0 and result.status not in {"unavailable", "ambiguous", "not_extracted"}
                and c.population_documents > 0 and c.complete_documents == c.population_documents
                and not (c.disputed_assertions or c.unresolved_identities or c.truncated or op.as_of or op.known_at)
                and c.missing_values <= m.eligible_records - m.observed_records)


def validate_claim(claim: Claim, results: list[EvidenceResult], scope: Scope):
    result = next((r for r in results if r.result_id == claim.result_id), None)
    if result is None or result.scope_fingerprint != scope.fingerprint():
        raise ValueError("Claim has no authorized result")
    if claim.output_id != result.output_id:
        raise ValueError("Claim changes the requested measure")
    if result.status in {'ambiguous','unavailable','not_extracted'}:
        raise ValueError('The result does not establish this claim')
    if claim.span_id:
        span = next((s for s in result.citations if s.span_id == claim.span_id), None)
        if not span or not claim.quote or claim.quote not in span.text:
            raise ValueError("Claim is not an exact quote of its cited source")
        return claim.quote
    if claim.row_index >= len(result.rows) or claim.field not in result.rows[claim.row_index]:
        raise ValueError("Claim references no result cell")
    if result.operation.operation in {"difference", "ratio"}:
        from .arithmetic import verify_derived
        verify_derived(result, results, scope)
        if claim.field != "value" or claim.population_basis != result.derivation["basis"]:
            raise ValueError("Derived claim changes its population basis")
        return result.rows[claim.row_index][claim.field]
    if claim.population_basis == "observed_subset":
        if not supports_observed_metric(result) or claim.field != "value" or not result.rows[claim.row_index].get("observed_records"):
            raise ValueError("Claim lacks a validated observed-subset denominator")
    elif (result.operation.operation in {"count_entities", "count_assertions", "sum", "average", "average_duration"}
            and (not result.coverage.complete_for(result.operation)
                 or result.metric is not None and result.metric.basis != "exact_population")):
        raise ValueError("An incomplete population cannot establish a definitive measure")
    return result.rows[claim.row_index][claim.field]


def _condition_phrase(conditions, language):
    """The filters, said in words, grouped so a range reads as a range."""
    if not conditions:
        return ""
    joined = {}
    for item in conditions:
        joined.setdefault(item["field"], []).append(item)
    lower = {"gt": True, "gte": True}
    upper = {"lt": True, "lte": True}
    said = []
    for field, items in joined.items():
        ops = {i["operator"] for i in items}
        low = next((i["value"] for i in items if i["operator"] in lower), None)
        high = next((i["value"] for i in items if i["operator"] in upper), None)
        if low and high:
            said.append({"en": "“%s” from %s up to %s" % (field, low[:10], high[:10]),
                         "ur": "%s %s سے %s تک" % (field, low[:10], high[:10]),
                         "roman_ur": "%s %s se %s tak" % (field, low[:10], high[:10])}[language])
            continue
        for item in items:
            value, operator = item["value"], item["operator"]
            if operator == "exists":
                said.append({"en": "“%s” recorded" % field,
                             "ur": "“%s” درج ہے" % field,
                             "roman_ur": "“%s” darj hai" % field}[language])
            elif operator == "not_exists":
                said.append({"en": "no “%s” recorded" % field,
                             "ur": "“%s” درج نہیں" % field,
                             "roman_ur": "“%s” darj nahin" % field}[language])
            elif operator == "contains":
                said.append({"en": "%s containing “%s”" % (field, value),
                             "ur": "%s جس میں “%s” ہو" % (field, value),
                             "roman_ur": "%s jis mein “%s” ho" % (field, value)}[language])
            elif operator in ("eq", "ne"):
                word = {"en": ("%s of “%s”" if operator == "eq" else "%s other than “%s”"),
                        "ur": ("%s “%s”" if operator == "eq" else "%s جو “%s” نہ ہو"),
                        "roman_ur": ("%s “%s”" if operator == "eq" else "%s jo “%s” na ho")}[language]
                said.append(word % (field, value))
            else:
                said.append("%s %s %s" % (field, operator, value))
    tail = {"en": " and ", "ur": " اور ", "roman_ur": " aur "}[language]
    return said[0] if len(said) == 1 else tail.join([", ".join(said[:-1]), said[-1]])


def narrate_measure(result, value, unit, language):
    """One sentence that answers the question, with the measured number in it.

    WHY THIS EXISTS

    An answer read:

        FIR
        Distinct recorded entities: 61
        Documents checked: 73/73.

    -- a label, a metric name and a coverage line, none of which is a
    sentence. Worse, it was AMBIGUOUS: "how many people are recorded as
    accused" and "how many complainants are recorded" both rendered as
    "person Distinct recorded entities: N", identical apart from the number,
    because the filter that distinguishes them was never named.

    The number is taken verbatim from the result and the words around it are
    built from the plan's own parts, so the sentence cannot introduce a
    figure the query did not produce. Nothing is asked of a model here: a
    measure that reads well is a rendering problem, not a writing one.
    """
    parts = result.measured or {}
    subject = parts.get("subject") or result.label
    conditions = _condition_phrase(parts.get("conditions") or [], language)
    shown = display_measure(value, unit)
    operation = parts.get("operation") or result.operation.operation
    if operation == "count_entities":
        # A COUNT CAN BE NARROWED TWO WAYS, AND BOTH MUST BE SAID.
        #
        # The narrowing may arrive as a filter, or as the operation's own
        # predicate_id -- "count the people who have a complainant-of link"
        # needs no filter at all. MEASURED: "how many complainants are
        # recorded" planned the second shape and rendered as "71 person
        # records are recorded", which is what "how many people are
        # recorded" (164) would also say. The number was right and the
        # sentence did not describe it.
        narrowing = conditions
        # Reached THROUGH a relationship chain: that is what was counted.
        if not narrowing and parts.get("through"):
            chain = ", ".join('“%s”' % t for t in parts["through"])
            narrowing = {"en": "%s recorded" % chain, "ur": "%s درج ہے" % chain,
                         "roman_ur": "%s darj hai" % chain}[language]
        if not narrowing and parts.get("of"):
            narrowing = {"en": "“%s” recorded" % parts["of"],
                         "ur": "“%s” درج ہے" % parts["of"],
                         "roman_ur": "“%s” darj hai" % parts["of"]}[language]
        if narrowing:
            return {"en": "%s %s records have %s." % (shown, subject, narrowing),
                    "ur": "%s %s ریکارڈ ایسے ہیں جن میں %s۔" % (shown, subject, narrowing),
                    "roman_ur": "%s %s records aise hain jin mein %s." % (shown, subject, narrowing)}[language]
        return {"en": "%s %s records are recorded." % (shown, subject),
                "ur": "%s %s ریکارڈ درج ہیں۔" % (shown, subject),
                "roman_ur": "%s %s records darj hain." % (shown, subject)}[language]
    measure_word = {"count_assertions": {"en": "recorded statements", "ur": "ریکارڈ شدہ بیانات",
                                         "roman_ur": "recorded bayanaat"},
                    "sum": {"en": "in total", "ur": "کل", "roman_ur": "kul"},
                    "average": {"en": "on average", "ur": "اوسطاً", "roman_ur": "ausatan"},
                    "average_duration": {"en": "on average", "ur": "اوسطاً",
                                         "roman_ur": "ausatan"}}.get(operation)
    if measure_word:
        of = parts.get("of") or subject
        word = measure_word[language]
        base = {"en": "%s %s %s" % (shown, word, of),
                "ur": "%s %s %s" % (shown, word, of),
                "roman_ur": "%s %s %s" % (shown, word, of)}[language]
        if conditions:
            base += {"en": ", where %s" % conditions, "ur": "، جہاں %s" % conditions,
                     "roman_ur": ", jahan %s" % conditions}[language]
        return base + "."
    return None


_COPY = {
    "en": {"missing": "The evidence has not been fully extracted for this question.",
           "partial": "Partial evidence; this does not establish a complete total.",
           "coverage": "Documents checked", "none": "No matching records in the checked population.",
           "ungrouped": "not recorded", "by_group": "broken down by group",
           "highest": "highest",
           "unavailable": "The service could not complete this part. This is not a finding of zero.",
           "count_entities": "Distinct recorded entities", "count_assertions": "Recorded assertions",
           "sum": "Total quantity", "average": "Average", "pending": "Further source review is queued.",
           "unresolved": "Unresolved identities", "missing_values": "Missing measurements", "conflicts": "Disputed assertions"},
    "ur": {"missing": "اس سوال کے لیے شواہد کا مکمل استخراج ابھی نہیں ہوا۔",
           "partial": "یہ جزوی شواہد ہیں؛ ان سے مکمل تعداد ثابت نہیں ہوتی۔",
           "coverage": "جانچی گئی دستاویزات", "none": "جانچے گئے ریکارڈ میں کوئی متعلقہ اندراج نہیں ملا۔",
           "ungrouped": "درج نہیں", "by_group": "گروہ کے اعتبار سے",
           "highest": "سب سے زیادہ",
           "unavailable": "سروس یہ حصہ مکمل نہیں کر سکی۔ اس کا مطلب تعداد صفر ہونا نہیں ہے۔",
           "count_entities": "الگ ریکارڈ شدہ اکائیاں", "count_assertions": "ریکارڈ شدہ بیانات",
           "sum": "کل مقدار", "average": "اوسط", "pending": "مزید ماخذ کی جانچ قطار میں شامل ہے۔",
           "unresolved": "غیر واضح شناختیں", "missing_values": "ناموجود مقداریں", "conflicts": "متنازع بیانات"},
    "roman_ur": {"ungrouped": "darj nahin", "by_group": "group ke aetbar se",
           "highest": "sab se zyada",
           "missing": "Is sawal ke liye shawahid ka mukammal extraction abhi nahin hua.",
           "partial": "Yeh juzvi shawahid hain; mukammal tadaad sabit nahin hoti.",
           "coverage": "Jaanchi gayi documents", "none": "Jaanche gaye records mein koi mutaliqa indraj nahin mila.",
           "unavailable": "Service yeh hissa mukammal nahin kar saki; is ka matlab zero nahin.",
           "count_entities": "Alag recorded entities", "count_assertions": "Recorded bayanaat",
           "sum": "Kul miqdaar", "average": "Ausat", "pending": "Mazeed source review queue mein hai.",
           "unresolved": "Ghair wazeh shanakht", "missing_values": "Na-maujood miqdaar", "conflicts": "Mutanaza bayanaat"},
}


def render(run_id: UUID, plan: QuestionPlan, results: list[EvidenceResult], scope: Scope) -> Answer:
    words = _COPY[plan.language]
    if plan.clarification and not results:
        return Answer(run_id=run_id, status="clarification", text=plan.clarification,
                      clarification=plan.clarification, plan=plan)
    if plan.conversational_response:
        # A misclassified factual request must not publish model knowledge
        # through the small-talk branch. This branch contains no factual text.
        text = {"en":"How can I help you explore the records?",
                "ur":"ریکارڈ کی جانچ میں آپ کی کیا مدد کر سکتا ہوں؟",
                "roman_ur":"Records ki jaanch mein aap ki kya madad kar sakta hoon?"}[plan.language]
        return Answer(run_id=run_id, status="answered", text=text, plan=plan)
    parts, claims = [], []
    focused_clarification = None
    for result in results:
        if result.scope_fingerprint != scope.fingerprint():
            raise ValueError("Result scope differs from the request")
        lines = [result.label]
        suppress_label = bool(getattr(result.operation, "group_by", None))
        numeric = result.operation.operation in {"count_entities", "count_assertions", "sum", "average", "average_duration"}
        if result.status == "unavailable":
            lines.append(words["unavailable"])
        elif result.status == "not_extracted":
            lines.append(words["missing"])
        elif result.status == "ambiguous":
            candidates = ', '.join(dict.fromkeys(str(row.get('label','')) for row in result.rows if row.get('label')))
            question = {'en':f"Several recorded identities match this name. Which do you mean? {candidates}",
                          'roman_ur':f"Is naam se kai alag recorded shanakhtain milti hain. Aap kis ki baat kar rahe hain? {candidates}",
                          'ur':f"اس نام سے کئی الگ ریکارڈ شدہ شناختیں ملتی ہیں۔ آپ کس کی بات کر رہے ہیں؟ {candidates}"}[plan.language]
            if focused_clarification is None:
                focused_clarification = question
                lines.append(question)
        elif result.operation.operation in {"difference", "ratio"}:
            claim = Claim(result_id=result.result_id, output_id=result.output_id,
                          population_basis=result.derivation["basis"] if result.derivation else "exact_population")
            value = validate_claim(claim, results, scope)
            claims.append(claim)
            caption = {"en": {"difference":"Difference", "ratio":"Ratio"},
                       "roman_ur": {"difference":"Farq", "ratio":"Nisbat"},
                       "ur": {"difference":"فرق", "ratio":"تناسب"}}[plan.language][result.operation.operation]
            lines.append(f"{caption}: {display_measure(value,result.unit)}")
            if claim.population_basis == "observed_subset":
                lines.append({"en":"This comparison applies to usable records only.",
                              "roman_ur":"Yeh muqabla sirf qabil-e-pemaish records ka hai.",
                              "ur":"یہ موازنہ صرف قابل پیمائش ریکارڈ کا ہے۔"}[plan.language])
        elif numeric and not result.coverage.complete_for(result.operation) and not supports_observed_metric(result):
            # Do not print an unvalidated number and then try to repair it with
            # a footnote. Source passages remain available for inspection.
            lines.append(words["partial"] if result.coverage.complete_documents else words["missing"])
        elif numeric:
            measure = words["average" if result.operation.operation == "average_duration"
                            else result.operation.operation]
            unit = f" {result.unit}" if result.unit else ""
            grouped = bool(result.operation.group_by)
            entries = []
            for index, row in enumerate(result.rows):
                observed = result.metric is not None and result.metric.basis == "observed_subset"
                if observed and not supports_observed_metric(result):
                    continue
                if result.metric and not row.get("observed_records"):
                    continue
                claim = Claim(result_id=result.result_id, output_id=result.output_id, row_index=index,
                              population_basis="observed_subset" if observed else "exact_population")
                value = validate_claim(claim, results, scope)
                claims.append(claim)
                # MEASURED, on "flag the suspects who appear most frequently":
                # the grouped count put a null group of 31 at the top of the
                # ranking -- those are FIRs with NO accused recorded, and they
                # read as the most frequent suspect. An absent group value is
                # a statement that the thing was not recorded, never a group
                # that competes for the top of a list.
                if row.get("group_value") is None and grouped:
                    entries.append((f"({words['ungrouped']})", value))
                else:
                    entries.append((row.get("group_value"), value))
            if not grouped:
                for name, value in entries:
                    # A SENTENCE, not a label and a number on separate lines.
                    # The figure is the one the query produced; only the words
                    # around it are composed. See `narrate_measure`.
                    sentence = (narrate_measure(result, value, result.unit, plan.language)
                                if name is None else None)
                    if sentence:
                        # The sentence names the subject, so a bare label line
                        # above it just repeats the first word of the answer.
                        if lines and lines[0] == result.label:
                            lines = lines[1:]
                        lines.append(sentence)
                    else:
                        label = f" — {name}" if name is not None else ""
                        lines.append(f"{measure}{label}: {display_measure(value,result.unit)}")
                if result.metric:
                    m = result.metric
                    lines.append({
                        "en": f"Usable records: {m.observed_records}/{m.eligible_records}. Missing: {m.missing_records}; conflicting: {m.conflicting_records}; negative durations: {m.negative_duration_records}; unknown period: {m.unknown_cohort_records}.",
                        "roman_ur": f"Qabil-e-pemaish records: {m.observed_records}/{m.eligible_records}. Na-maujood: {m.missing_records}; mutazad: {m.conflicting_records}; manfi muddat: {m.negative_duration_records}; na-maloom arsa: {m.unknown_cohort_records}.",
                        "ur": f"قابل پیمائش ریکارڈ: {m.observed_records}/{m.eligible_records}۔ ناموجود: {m.missing_records}؛ متضاد: {m.conflicting_records}؛ منفی دورانیہ: {m.negative_duration_records}؛ نامعلوم مدت: {m.unknown_cohort_records}۔"
                    }[plan.language])
                    if m.basis == "observed_subset":
                        lines.append({"en": "This average describes usable records only; it does not establish the average for all records.",
                            "roman_ur": "Yeh ausat sirf qabil-e-pemaish records ka hai; tamam records ka ausat sabit nahin hota.",
                            "ur": "یہ اوسط صرف قابل پیمائش ریکارڈ کا ہے؛ تمام ریکارڈ کا اوسط ثابت نہیں ہوتا۔"}[plan.language])
            else:
                # A GROUPED result is a table, and repeating the measure's name
                # on every row is how it stopped reading like one.
                #
                # MEASURED, on "flag the suspects who appear most frequently":
                # 50 rows each opened with "Distinct recorded entities — " and
                # the answer ran to a wall of text in which no suspect stood
                # out. The reader wanted the ranking; the words in front of it
                # were the same 27 characters fifty times over.
                #
                # Said once as a heading, the rows become the answer. Markdown,
                # because the chat renders it.
                # A RANKING MUST BE RANKED.
                #
                # `entries` arrived in row order, which is alphabetical by
                # group label, and `top` was read off the FIRST one. MEASURED,
                # on "which police station recorded the most FIRs": the table
                # opened with تھانہ اقبال ٹاؤن (1) and the real answer,
                # تھانہ ماڈل ٹاؤن with 7, sat ten rows down. The question asks
                # for the largest and the answer buried it.
                #
                # The ungrouped bucket is never a competitor, so it sorts last
                # whatever its size.
                unnamed = f"({words['ungrouped']})"
                if entries and all(isinstance(v, (int, float)) for _, v in entries):
                    entries.sort(key=lambda item: (item[0] == unnamed, -item[1],
                                                   str(item[0])))
                top = entries[0][1] if entries and entries[0][0] != unnamed else None
                leaders = [name for name, value in entries
                           if value == top and name != unnamed]
                # A grouped result is a table, and its heading should say what
                # the table IS. "**police station by recorded at police
                # station** — distinct recorded entities (1):" names the
                # machinery rather than the finding.
                grouped_parts = result.measured or {}
                subject_name = grouped_parts.get("subject") or result.label
                by_name = grouped_parts.get("grouped_by")
                if by_name:
                    heading = {"en": "%s records, broken down by “%s” — %d group(s):"
                                     % (subject_name, by_name, len(entries)),
                               "ur": "%s ریکارڈ، “%s” کے اعتبار سے — %d گروہ:"
                                     % (subject_name, by_name, len(entries)),
                               "roman_ur": "%s records, “%s” ke aetbar se — %d group:"
                                     % (subject_name, by_name, len(entries))}[plan.language]
                else:
                    heading = f"**{result.label}** — {measure.lower()}{unit} ({len(entries)}):"
                if leaders and len(leaders) <= 3 and isinstance(top, (int, float)) and top > 1:
                    heading += {"en": " Highest: %s (%s)." % (", ".join(str(x) for x in leaders), top),
                                "ur": " سب سے زیادہ: %s (%s)۔" % ("، ".join(str(x) for x in leaders), top),
                                "roman_ur": " Sab se zyada: %s (%s)." % (", ".join(str(x) for x in leaders), top)}[plan.language]
                if suppress_label and lines and lines[0] == result.label:
                    lines = lines[1:]
                lines.append(heading)
                for name, value in entries:
                    lines.append(f"- {name}: **{value}**{unit}")
        elif result.operation.operation == "search":
            # Retrieved passages are candidates, not answers. Keep them available
            # in structured citations; do not promote unrelated quotes to claims.
            for finding in result.findings:
                if finding.output_id != result.output_id:
                    raise ValueError("Finding changes the requested output")
                for support in finding.supports:
                    claim=Claim(result_id=result.result_id,output_id=result.output_id,
                                span_id=support.span_id,quote=support.quote)
                    validate_claim(claim,results,scope)
                    claims.append(claim)
                lines.append(finding.text+" "+" ".join(f"[{s.span_id}]" for s in finding.supports))
            if not result.findings:
                # A MEASURED ABSENCE IS AN ANSWER; A FAILED SEARCH IS NOT.
                #
                # MEASURED, on "does Bhara Kahu appear anywhere in the
                # records": ten near-misses came back, composition refused
                # every one of them for want of support, and this rendered
                # "an answer has not yet been established" -- which is what
                # it also says when the machinery simply could not cope. The
                # reader could not tell those apart, and they call for
                # opposite next steps.
                #
                # `coverage.absent_literals` is filled only by an exhaustive
                # check over every recorded span, entity name and value, so
                # when it names the searched phrase the negative is measured
                # and can be stated. The near matches are still offered, so
                # the reader can see WHAT was looked at.
                absent = list(getattr(result.coverage, "absent_literals", []) or [])
                if absent:
                    quoted = ", ".join("'%s'" % a for a in absent)
                    lines.append({
                        "en": ("%s does not appear in any recorded passage, entity name "
                               "or value. The passages retrieved are near matches only."
                               % quoted),
                        "ur": ("%s کسی بھی ریکارڈ شدہ اقتباس، ادارے کے نام یا قیمت میں موجود نہیں۔ "
                               "جو اقتباسات ملے وہ صرف قریبی مماثلت ہیں۔" % quoted),
                        "roman_ur": ("%s kisi bhi record shuda iqtibas, entity ke naam ya value "
                                     "mein mojood nahin. Jo iqtibasaat mile woh sirf qareebi "
                                     "mumasilat hain." % quoted),
                    }[plan.language])
                else:
                    lines.append({"en":"Candidate sources were retrieved; an answer has not yet been established.",
                              "ur":"ممکنہ ماخذ ملے ہیں؛ ابھی جواب کی تصدیق نہیں ہوئی۔",
                              "roman_ur":"Mumkinah maakhaz mile hain; abhi jawab ki tasdeeq nahin hui."}[plan.language]
                             if result.citations else words["missing"])
        else:
            for index,row in enumerate(result.rows):
                values = {k: v for k, v in row.items() if v is not None and not k.endswith("_id")
                          and k not in {"span_ids", "unresolved_identities", "identity_status", "subject_node", "object_node",
                                        "subject_identity","object_identity"}}
                for field in values:
                    claim=Claim(result_id=result.result_id,output_id=result.output_id,row_index=index,field=field)
                    validate_claim(claim,results,scope)
                    claims.append(claim)
                subject=values.get('subject_label',values.get('label'))
                predicate=values.get('predicate_label')
                object_value=values.get('object_label')
                if object_value is None:
                    object_value=next((values[key] for key in ('value_text','value_number','value_boolean','value_date') if key in values),None)
                if subject is not None and predicate is not None and object_value is not None:
                    entry=f"{subject} → {predicate}: {object_value}"
                    if values.get('unit'):entry+=f" {values['unit']}"
                    if values.get('event_time'):entry+=f" ({values['event_time']})"
                else:
                    entry=" · ".join(str(v) for v in values.values())
                available={str(s.span_id) for s in result.citations}
                references=[str(key) for key in row.get('span_ids',[]) if str(key) in available]
                lines.append(entry+(' '+ ' '.join(f'[{key}]' for key in references) if references else ''))
            if not result.rows:
                lines.append(words["none"] if result.coverage.complete else words["missing"])
        # A name that matches nothing is the reason there are no rows, and
        # it is the one thing the reader needs. MEASURED: "the FIR at Bhara
        # Kahu police station" answered "No matching records" -- true, but
        # silent about the fact that no station of that name is recorded at
        # all. The store attaches that finding; it must reach the answer.
        for note in result.coverage.notes:
            if note.startswith("No record carries the name"):
                lines.append(note)
        # A LIST THAT DID NOT READ EVERYTHING MUST SAY SO.
        #
        # The coverage line was printed for numbers, searches and partial
        # results. A `list` marked `answered` skipped it -- so listing the
        # summaries of the June cases returned rows for the documents that
        # had one and said nothing about the documents that did not.
        # MEASURED: 29 of 73 FIRs were summarized and the answer showed 29
        # rows with no indication that 44 were missing.
        #
        # Reading fewer documents than the population is exactly the
        # condition a reader cannot detect from the rows themselves, so it
        # is stated whenever it holds, whatever the operation.
        short = (result.coverage.population_documents
                 and result.coverage.complete_documents < result.coverage.population_documents)
        if (numeric or result.operation.operation == "search"
                or result.status in {"partial", "not_extracted"}
                or short) and not result.derivation:
            c = result.coverage
            # A measured absence does not rest on how much of a filtered
            # population was read, so the population line would contradict
            # it: "'Bhara Kahu' does not appear ... Documents checked:
            # 0/7579" reads as though nothing was checked, when in fact
            # every recorded span, entity name and value was. The line is
            # replaced by what was actually done.
            # Only when the absence IS the answer. MEASURED: asked "what does
            # the record say about armed robbery at Model Town", the search
            # text does not occur verbatim -- the words are spread across the
            # passage -- so the phrase was correctly recorded as absent while
            # composition still grounded a real claim about FIR 213/26. The
            # exhaustive-check line then replaced the coverage line under an
            # answer that did not rest on it. A measurement worth recording is
            # not automatically a measurement worth reporting.
            if getattr(c, "absent_literals", None) and not result.findings:
                lines.append({"en": "Checked every recorded passage, entity name and value.",
                              "ur": "ہر ریکارڈ شدہ اقتباس، ادارے کے نام اور قیمت کی جانچ کی گئی۔",
                              "roman_ur": "Har record shuda iqtibas, entity ke naam aur value "
                                          "ki janch ki gayi."}[plan.language])
            elif c.population_documents and c.complete_documents == c.population_documents:
                # HOW MUCH WAS READ, said rather than tallied. "Documents
                # checked: 73/73." is a ratio; a reader wants to know whether
                # the answer rests on everything or on part of it.
                lines.append({"en": "All %d documents were checked." % c.population_documents,
                              "ur": "تمام %d دستاویزات جانچی گئیں۔" % c.population_documents,
                              "roman_ur": "Tamam %d documents jaanchi gayin."
                                          % c.population_documents}[plan.language])
            elif c.population_documents:
                lines.append({"en": "%d of %d documents were checked, so this does not "
                                    "cover the rest." % (c.complete_documents, c.population_documents),
                              "ur": "%d از %d دستاویزات جانچی گئیں، باقی شامل نہیں۔"
                                    % (c.complete_documents, c.population_documents),
                              "roman_ur": "%d mein se %d documents jaanchi gayin; baqi shamil nahin."
                                    % (c.population_documents, c.complete_documents)}[plan.language])
            else:
                lines.append(f"{words['coverage']}: {c.complete_documents}/{c.population_documents}.")
            for field, key in [("unresolved_identities", "unresolved"), ("missing_values", "missing_values"), ("disputed_assertions", "conflicts")]:
                if getattr(c, field):
                    lines.append(f"{words[key]}: {getattr(c, field)}.")
        parts.append("\n".join(lines))
    if not results:
        parts.append(words["missing"])
    statuses = {r.status for r in results}
    if any(r.operation.operation=="search" for r in results):
        statuses.add("partial")
    status = "answered" if results and statuses == {"answered"} else "partial" if "answered" in statuses or "partial" in statuses else "unavailable" if statuses == {"unavailable"} else "not_extracted"
    if focused_clarification:
        status = "partial" if claims else "clarification"
    return Answer(run_id=run_id, status=status, text="\n\n".join(parts), plan=plan, results=results,claims=claims,
                  clarification=focused_clarification)
