# -*- coding: utf-8 -*-
"""Multi-part questions: decomposition, execution, and honest rendering.

These cover the three things that make multi-part answers safe rather than
merely possible:

  - a question decomposes only when the MODEL said it decomposes, and the
    parts it named are executable as standalone questions;
  - a part that failed is never dropped from what a reader sees;
  - a part's assurance belongs to that part and does not leak to another.

Nothing here contacts a model or a database. Parts are executed through an
injected stand-in for `answer_question`, so what is measured is the
orchestration, not the backend.
"""
from __future__ import annotations

import pytest

from src.pipeline.aggregate import multi
from src.pipeline.aggregate.multi import MAX_PARTS, MultiPartAnswer, PartAnswer
from src.pipeline.aggregate.nl_spec import RequestedOutput, SpecGeneration, _requested_outputs
from src.pipeline.aggregate.orchestrator import AggregateAnswer
from src.pipeline.aggregate.spec import AggregateSpec, PopulationNode, Scope
from src.pipeline.harness.tools import xagg_v1_adapter as adapter

SCOPE = Scope(kind="cross_case", user_role="supervisor", user_id="t")


def _spec(**kw) -> AggregateSpec:
    return AggregateSpec(
        question_text=kw.pop("question_text", "q"),
        measure=kw.pop("measure", "count_distinct"),
        population=kw.pop("population", PopulationNode(entity="Person")),
        grain=kw.pop("grain", "ENTITY"),
        scope=SCOPE,
        distinct_key=kw.pop("distinct_key", "entity_id"),
        **kw,
    )


def _gen(outputs) -> SpecGeneration:
    return SpecGeneration(spec=_spec(), requested_outputs=tuple(outputs))


def _out(asks: str, label: str = "") -> RequestedOutput:
    return RequestedOutput(asks=asks, label=label or asks)


def _answered(value, question="q", **kw) -> AggregateAnswer:
    return AggregateAnswer(
        question=question, request_id="r", status="ANSWERED",
        value=value, interpretation="count_distinct", grain="ENTITY",
        spec=_spec(), **kw,
    )


def _refused(code="spec_invalid", reason="nope", question="q") -> AggregateAnswer:
    return AggregateAnswer(
        question=question, request_id="r", status="REFUSED",
        refusal_code=code, refusal_reason=reason,
    )


# ══════════════════════════════════════════════════════════════════════
# Parsing the decomposition
# ══════════════════════════════════════════════════════════════════════
class TestRequestedOutputParsing:
    def test_new_shape_carries_a_standalone_question(self):
        outputs = _requested_outputs({"requested_outputs": [
            {"asks": "how many witnesses are there", "label": "witnesses"},
            {"asks": "how many victims are there", "label": "victims"},
        ]})
        assert [o.label for o in outputs] == ["witnesses", "victims"]
        assert all(o.answerable for o in outputs)

    def test_bare_strings_still_parse_but_are_not_answerable(self):
        """The old shape must not break, and must not be executed either.

        A label is not a question. Asking "witnesses" as though it were one
        is how a fragment gets answered as though it were the whole.
        """
        outputs = _requested_outputs({"requested_outputs": ["witnesses", "victims"]})
        assert len(outputs) == 2
        assert not any(o.answerable for o in outputs)

    def test_duplicate_figures_collapse(self):
        """One number asked for twice is one number, not two."""
        outputs = _requested_outputs({"requested_outputs": [
            {"asks": "how many cases are there", "label": "cases"},
            {"asks": "How many cases are there ", "label": "cases again"},
        ]})
        assert len(outputs) == 1

    def test_missing_or_malformed_field_yields_nothing(self):
        assert _requested_outputs({}) == ()
        assert _requested_outputs({"requested_outputs": "witnesses"}) == ()
        assert _requested_outputs({"requested_outputs": [{}, None]}) == ()

    def test_generation_to_dict_is_json_safe(self):
        """The receipt is serialised; a dataclass in it would break that."""
        import json
        gen = _gen([_out("how many witnesses are there", "witnesses")])
        json.dumps(gen.to_dict()["requested_outputs"])

    def test_to_dict_tolerates_bare_strings_from_a_caller(self):
        """Tests and the evaluation harness construct these by hand."""
        import json
        json.dumps(_gen(["witnesses"]).to_dict()["requested_outputs"])


# ══════════════════════════════════════════════════════════════════════
# Planning
# ══════════════════════════════════════════════════════════════════════
class TestPlanParts:
    def test_single_output_is_not_multi_part(self):
        parts, refusal = multi.plan_parts(_gen([_out("how many cases are there")]))
        assert parts == () and refusal is None

    def test_no_generation_is_not_multi_part(self):
        """A caller-supplied spec declared nothing, so nothing is split."""
        assert multi.plan_parts(None) == ((), None)

    def test_two_answerable_parts_plan_cleanly(self):
        parts, refusal = multi.plan_parts(_gen([
            _out("how many witnesses are there", "witnesses"),
            _out("how many victims are there", "victims"),
        ]))
        assert len(parts) == 2 and refusal is None

    def test_over_the_cap_refuses_rather_than_truncating(self):
        """Answering the first MAX_PARTS and dropping the rest is the bug."""
        parts, refusal = multi.plan_parts(_gen([
            _out(f"how many things of kind {i} are there") for i in range(MAX_PARTS + 1)
        ]))
        assert refusal is not None and str(MAX_PARTS) in refusal
        # The parts are still reported, so the refusal can name them.
        assert len(parts) == MAX_PARTS + 1

    def test_a_label_without_a_question_refuses(self):
        """The old shape decomposes but cannot be executed; say so."""
        parts, refusal = multi.plan_parts(_gen(["witnesses", "victims"]))
        assert refusal is not None
        assert "on its own" in refusal


# ══════════════════════════════════════════════════════════════════════
# Execution
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestAnswerMultiPart:
    async def _run(self, parts, first, responder):
        return await multi.answer_multi_part(
            None, "q", SCOPE, tuple(parts),
            first_answer=first, request_id="rid",
            _answer_question=responder,
        )

    async def test_each_part_is_asked_its_own_question(self):
        asked: list[str] = []

        async def responder(snapshot, question, scope, **kw):
            asked.append(question)
            return _answered(9, question)

        parts = [
            _out("how many witnesses are there", "witnesses"),
            _out("how many victims are there", "victims"),
        ]
        result = await self._run(parts, _answered(37), responder)
        # Part 1 was computed by the caller and is NOT recomputed.
        assert asked == ["how many victims are there"]
        assert [p.answer.value for p in result.parts] == [37, 9]
        assert result.status == "ANSWERED"

    async def test_scope_is_passed_through_unchanged(self):
        """A part running under a wider scope would be an auth bypass."""
        seen: list[Scope] = []

        async def responder(snapshot, question, scope, **kw):
            seen.append(scope)
            return _answered(1, question)

        await self._run(
            [_out("how many a are there"), _out("how many b are there")],
            _answered(1), responder,
        )
        assert seen == [SCOPE]

    async def test_a_refused_part_is_kept_and_marks_the_whole_partial(self):
        async def responder(snapshot, question, scope, **kw):
            return _refused(reason="no such field 'victim_status'")

        result = await self._run(
            [_out("how many witnesses are there", "witnesses"),
             _out("how many victims are there", "victims")],
            _answered(37), responder,
        )
        assert len(result.parts) == 2          # RULE 1: never dropped
        assert result.status == "PARTIAL"
        assert len(result.answered_parts) == 1

    async def test_a_part_that_raises_becomes_a_refusal_not_a_crash(self):
        """The other parts produced real figures; discarding them serves nothing."""
        async def responder(snapshot, question, scope, **kw):
            raise RuntimeError("backend down")

        result = await self._run(
            [_out("how many witnesses are there"), _out("how many victims are there")],
            _answered(37), responder,
        )
        assert len(result.parts) == 2
        assert result.parts[1].answer.refusal_code == "part_execution_error"
        assert "backend down" in result.parts[1].answer.refusal_reason
        assert result.status == "PARTIAL"

    async def test_all_parts_failing_is_refused_not_answered(self):
        async def responder(snapshot, question, scope, **kw):
            return _refused()

        result = await self._run(
            [_out("how many a are there"), _out("how many b are there")],
            _refused(), responder,
        )
        assert result.status == "REFUSED"

    async def test_part_one_loses_the_single_answer_caveat(self):
        """It says 'this answers only the first' — false once the rest ran."""
        async def responder(snapshot, question, scope, **kw):
            return _answered(9, question)

        first = _answered(37, warnings=(
            multi.PARTIAL_WARNING_PREFIX + "2 figures the question asks for.",
            "Interpretation adjusted: something else",
        ))
        result = await self._run(
            [_out("how many witnesses are there"), _out("how many victims are there")],
            first, responder,
        )
        kept = result.parts[0].answer.warnings
        assert not any(w.startswith(multi.PARTIAL_WARNING_PREFIX) for w in kept)
        # Unrelated warnings survive — only the now-false one is removed.
        assert any("Interpretation adjusted" in w for w in kept)

    async def test_to_dict_is_json_safe(self):
        import json

        async def responder(snapshot, question, scope, **kw):
            return _answered(9, question)

        result = await self._run(
            [_out("how many a are there"), _out("how many b are there")],
            _answered(1), responder,
        )
        json.dumps(result.to_dict())


# ══════════════════════════════════════════════════════════════════════
# Rendering
# ══════════════════════════════════════════════════════════════════════
class TestRendering:
    def _multi(self, *parts) -> MultiPartAnswer:
        return MultiPartAnswer(question="q", request_id="r", parts=tuple(parts))

    def test_every_part_appears_with_its_figure(self):
        text = adapter.render_multi_part_text(self._multi(
            PartAnswer(label="witnesses", question="a", answer=_answered(37)),
            PartAnswer(label="victims", question="b", answer=_answered(9)),
        ))
        assert "witnesses" in text and "37" in text
        assert "victims" in text and "9" in text
        assert "Part 1 of 2" in text and "Part 2 of 2" in text

    def test_a_failed_part_is_rendered_with_its_reason(self):
        """RULE 1, at the boundary a reader actually sees."""
        text = adapter.render_multi_part_text(self._multi(
            PartAnswer(label="witnesses", question="a", answer=_answered(37)),
            PartAnswer(label="victims", question="b",
                       answer=_refused(reason="no such field 'victim_status'")),
        ))
        assert "37" in text
        assert "victims" in text
        assert "could not be answered" in text
        assert "victim_status" in text
        assert "1 of 2" in text        # the shortfall is stated, not implied

    def test_assurance_does_not_leak_between_parts(self):
        """A verified figure beside an unverified one must stay distinguishable."""
        verified = _answered(37)
        object.__setattr__(verified, "warnings", ("checked and agreed",))
        plain = _answered(9)
        text = adapter.render_multi_part_text(self._multi(
            PartAnswer(label="witnesses", question="a", answer=verified),
            PartAnswer(label="victims", question="b", answer=plain),
        ))
        before, _, after = text.partition("Part 2 of 2")
        assert "checked and agreed" in before
        assert "checked and agreed" not in after

    def test_a_grouped_part_renders_beside_a_counted_one(self):
        """Mixed shapes: a count and a breakdown in one answer."""
        grouped = _answered([{"key": "Islamabad", "count": 14},
                             {"key": "Rawalpindi", "count": 9}])
        text = adapter.render_multi_part_text(self._multi(
            PartAnswer(label="accused persons", question="a", answer=_answered(92)),
            PartAnswer(label="districts", question="b", answer=grouped),
        ))
        assert "92" in text
        assert "Islamabad: 14" in text and "Rawalpindi: 9" in text
        assert "Total groups: 2" in text

    def test_tool_result_dispatches_on_shape(self):
        """A multi-part answer must not be rendered as a single answer."""
        class _Chunk:
            def __init__(self, id, text, metadata):
                self.id, self.text, self.metadata = id, text, metadata

        class _Meta:
            def __init__(self, **kw):
                pass

        class _Result:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        result = adapter.to_tool_result(
            self._multi(
                PartAnswer(label="witnesses", question="a", answer=_answered(37)),
                PartAnswer(label="victims", question="b", answer=_answered(9)),
            ),
            result_cls=_Result, status_ok="OK",
            chunk_cls=_Chunk, metadata_cls=_Meta,
        )
        assert "Part 1 of 2" in result.raw_summary_text
        assert "37" in result.raw_summary_text and "9" in result.raw_summary_text

    def test_no_parts_never_claims_completeness(self):
        """"All 0 were computed" is a completeness claim over nothing."""
        text = adapter.render_multi_part_text(self._multi())
        assert "All 0" not in text
        assert "No figures were computed" in text

    def test_single_answers_render_exactly_as_before(self):
        """The single-output path must be untouched by any of this."""
        text = adapter.render_answer_text(_answered(73))
        assert "Part 1" not in text
        assert "73" in text
