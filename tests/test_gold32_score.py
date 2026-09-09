"""`evaluation/gold32_score.py` — scoring integrity (Module 45).

The defect this file guards against, in one sentence: **the judge sometimes
returns no score at all, and a `null` read as a zero silently understates every
published mean.**

It is not hypothetical. In the 2026-09-08 post-fix run, CR8 came back with no
FactualCorrectness value — a bare `null` — and re-scoring that single question
returned 1.0/1.0. Had that `null` been averaged in as 0.0 it would have pulled
the all-32 mean down by 0.031 on its own, and nothing in the results file would
have said so. `EVALUATION_REPORT_POST_FIXES.md` §5 states the rule plainly:
*"A null score should be re-run, never treated as a zero."*

So two things are pinned here:

  1. :func:`measure` retries a metric that produces no number, and — when it
     still cannot — returns ``None``, meaning **unscored**, with a reason that
     says so out loud.
  2. :func:`summarize` **excludes** unscored rows from the mean rather than
     zeroing them, and reports them loudly.

No network: the judge is a stub. `measure`'s backoff sleeps are injected.
"""
import importlib.util
import json
import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPT = os.path.join(_ROOT, "evaluation", "gold32_score.py")

_spec = importlib.util.spec_from_file_location("gold32_score", _SCRIPT)
gs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gs)


# ── stub judge metrics (no network, no deepeval) ─────────────────────────

class _StubMetric:
    """Stands in for a DeepEval metric.

    `outcomes` is consumed one per `measure()` attempt. Each entry is either a
    number (the judge scored it), ``None`` (the judge answered but produced no
    parseable number — the CR8 failure) or an Exception instance (raised).
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.score = None
        self.reason = "stub reason"

    def measure(self, tc):
        self.calls += 1
        out = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(out, Exception):
            self.score = None
            raise out
        self.score = out


def _no_sleep(_seconds):
    return None


_TC = object()  # measure() only hands the test case to the metric


# ── 1. a judge null is retried, then recorded as UNSCORED — never 0.0 ────

class TestNullIsNeverZero:
    def test_persistent_null_returns_none_not_zero(self):
        """The core defect. A judge that never scores must yield None."""
        m = _StubMetric([None, None, None])
        score, reason = gs.measure(m, _TC, sleeper=_no_sleep)
        assert score is None, "an unscored question must be None, never 0.0"
        # Explicit about the bug being guarded: `None` and `0.0` are different
        # facts about a question, and only one of them is a model failure.
        assert not isinstance(score, float)

    def test_persistent_null_is_retried_a_bounded_number_of_times(self):
        m = _StubMetric([None] * 20)
        gs.measure(m, _TC, tries=3, sleeper=_no_sleep)
        assert m.calls == 3, "must retry exactly `tries` times — bounded, not forever"

    def test_reason_says_out_loud_that_this_is_not_a_zero(self):
        m = _StubMetric([None, None, None])
        _, reason = gs.measure(m, _TC, sleeper=_no_sleep)
        assert "UNSCORED" in reason
        assert "NOT a zero" in reason

    def test_a_null_that_recovers_on_retry_is_scored(self):
        """CR8 exactly: null first, 1.0 on the re-run."""
        m = _StubMetric([None, 1.0])
        score, reason = gs.measure(m, _TC, sleeper=_no_sleep)
        assert score == 1.0
        assert m.calls == 2
        assert reason == "stub reason"

    def test_an_error_is_retried_too_not_swallowed_as_a_failure(self):
        m = _StubMetric([RuntimeError("judge blew up"), 0.9])
        score, _ = gs.measure(m, _TC, sleeper=_no_sleep)
        assert score == 0.9

    def test_a_timeout_yields_none_rather_than_hanging_the_run(self):
        """attempt_timeout=0 forces every attempt to look like a hang."""
        import time as _t

        class _SlowMetric(_StubMetric):
            def measure(self, tc):
                _t.sleep(0.3)
                super().measure(tc)

        m = _SlowMetric([1.0, 1.0, 1.0])
        score, reason = gs.measure(m, _TC, tries=2, attempt_timeout=0.01,
                                   sleeper=_no_sleep)
        assert score is None
        assert "TIMEOUT" in reason

    def test_rate_limits_do_not_consume_the_null_retry_budget(self):
        """A quota wobble says nothing about the question. It must not eat the
        retries reserved for genuine judge failures."""
        m = _StubMetric([RuntimeError("429 rate_limit"),
                         RuntimeError("429 rate_limit"),
                         RuntimeError("429 rate_limit"),
                         0.8])
        score, _ = gs.measure(m, _TC, tries=1, sleeper=_no_sleep)
        assert score == 0.8, "3 rate limits then a score, with only 1 null-retry"

    def test_rate_limit_retries_are_themselves_bounded(self):
        m = _StubMetric([RuntimeError("429 rate_limit")] * 50)
        score, reason = gs.measure(m, _TC, tries=3, rate_limit_tries=2,
                                   sleeper=_no_sleep)
        assert score is None
        assert "RATE-LIMITED" in reason


# ── 2. the mean excludes unscored rows rather than zeroing them ──────────

def _row(qid, fc, ar=1.0, qtype="Complex Reasoning"):
    return {"id": qid, "type": qtype, "language": "en", "route": "XAGG",
            "scores": {"FactualCorrectness": fc, "AnswerRelevancy": ar},
            "reasons": {"FactualCorrectness": "r", "AnswerRelevancy": "r"}}


class TestSummarizeExcludesUnscored:
    def test_null_is_excluded_from_the_mean_not_averaged_as_zero(self):
        rows = [_row("A", 1.0), _row("B", None), _row("C", 0.0)]
        sm = gs.summarize(rows)
        fc = sm["metrics"]["FactualCorrectness"]
        assert fc["mean"] == 0.5, "mean over the 2 SCORED rows"
        assert fc["mean"] != pytest.approx(1 / 3), "zeroing the null would give 0.333"
        assert fc["scored"] == 2
        assert fc["unscored"] == 1
        assert fc["unscored_ids"] == ["B"]

    def test_cr8_regression_a_single_null_does_not_drag_the_mean_down(self):
        """Pinned to the real shape: 31 perfect scores plus CR8 null.

        Zeroing CR8 gives 0.969; excluding it gives 1.0. The 0.031 gap is
        exactly the understatement §5 of the report warns about."""
        rows = [_row("Q%d" % i, 1.0) for i in range(31)] + [_row("CR8", None)]
        sm = gs.summarize(rows)
        assert sm["metrics"]["FactualCorrectness"]["mean"] == 1.0
        assert sm["metrics"]["FactualCorrectness"]["mean"] != 0.969
        assert "CR8" in sm["metrics"]["FactualCorrectness"]["unscored_ids"]

    def test_pass_rate_is_over_scored_rows_only(self):
        rows = [_row("A", 1.0), _row("B", None), _row("C", 0.2)]
        p = gs.summarize(rows)["pass"]
        assert (p["passed"], p["of_scored"]) == (1, 2), "the null is not a failure"

    def test_per_type_breakdown_also_excludes_unscored(self):
        rows = [_row("A", 1.0, qtype="Fact Retrieval"),
                _row("B", None, qtype="Fact Retrieval")]
        b = gs.summarize(rows)["by_type"]["Fact Retrieval"]
        assert b["mean"] == 1.0
        assert b["scored"] == 1 and b["unscored"] == 1

    def test_all_metrics_unscored_yields_none_mean_not_zero(self):
        sm = gs.summarize([_row("A", None, ar=None)])
        assert sm["metrics"]["FactualCorrectness"]["mean"] is None
        assert sm["metrics"]["AnswerRelevancy"]["mean"] is None

    def test_a_missing_key_counts_as_unscored_not_zero(self):
        rows = [_row("A", 1.0), {"id": "B", "type": "x", "scores": {}, "reasons": {}}]
        assert gs.summarize(rows)["metrics"]["FactualCorrectness"]["mean"] == 1.0


class TestSummaryIsLoud:
    def test_unscored_questions_are_shouted_not_silent(self):
        rows = [_row("A", 1.0), _row("B", None)]
        rows[1]["reasons"]["FactualCorrectness"] = "UNSCORED after 3 attempt(s)"
        text = gs.format_summary(gs.summarize(rows))
        assert "UNSCORED" in text
        assert "NOT a zero" in text
        assert "  B " in text or "B " in text, "the offending id must be named"

    def test_a_clean_run_says_so_explicitly(self):
        text = gs.format_summary(gs.summarize([_row("A", 1.0)]))
        assert "no nulls" in text


# ── 3. the answer cap the judge actually sees ────────────────────────────

class TestTruncateForScoring:
    def test_short_answers_are_untouched(self):
        assert gs.truncate_for_scoring("short", 900) == "short"

    def test_long_answers_are_capped_and_marked(self):
        out = gs.truncate_for_scoring("x" * 2000, 900)
        assert out.startswith("x" * 900)
        assert out.endswith(gs.TRUNCATION_MARKER)

    def test_zero_disables_capping_entirely(self):
        long = "x" * 5000
        assert gs.truncate_for_scoring(long, 0) == long

    def test_cap_is_a_named_env_overridable_constant(self):
        """It used to be a bare `900` inside a loop, justified by a metric
        (Faithfulness) that is no longer in `_METRICS`. A report cannot state
        the value it scored under if the value has no name."""
        assert isinstance(gs.MAX_ANSWER_CHARS, int)
        assert "Faithfulness" in gs.truncate_for_scoring.__doc__


# ── 4. the real results file, whatever it currently holds ───────────────

class TestRealResultsFile:
    """Robust to Module 27 overwriting the file: asserts the RELATION between
    the file and the published mean, not a frozen number."""

    def _rows(self):
        path = os.path.join(_ROOT, "evaluation", "gold32_results.json")
        if not os.path.exists(path):
            pytest.skip("no captured results file in this checkout")
        return json.load(open(path, encoding="utf-8"))

    def test_published_mean_equals_the_mean_of_non_null_scores(self):
        rows = self._rows()
        sm = gs.summarize(rows)
        for metric, d in sm["metrics"].items():
            vals = [r["scores"][metric] for r in rows
                    if isinstance(r.get("scores", {}).get(metric), (int, float))]
            expected = round(sum(vals) / len(vals), 3) if vals else None
            assert d["mean"] == expected
            assert d["scored"] == len(vals)

    def test_every_null_in_the_file_is_reported_as_unscored(self):
        rows = self._rows()
        sm = gs.summarize(rows)
        nulls = {(r["id"], m) for r in rows for m in gs._METRICS
                 if r.get("scores", {}).get(m) is None}
        reported = {(u["id"], u["metric"]) for u in sm["unscored"]}
        assert nulls == reported


# ── 5. the judge itself: model and prompt (Module 87) ────────────────────

def _fake_llm():
    """A DeepEval-shaped judge that never reaches the network.

    Built lazily and behind `importorskip` because DeepEval is an evaluation-only
    dependency and **CI does not install it** — the rest of this file is
    deliberately import-free of it, and a module-level `from deepeval...` here
    took the whole suite down with `ModuleNotFoundError`. `build_metrics` only
    has to accept this object; these tests read the metric's configuration, they
    never run it.
    """
    base = pytest.importorskip("deepeval.models.base_model")

    class _FakeLLM(base.DeepEvalBaseLLM):
        def __init__(self):
            super().__init__(model_name="fake-judge")

        def load_model(self):
            return None

        def generate(self, *a, **k):
            raise AssertionError("no judge call should happen in a unit test")

        async def a_generate(self, *a, **k):
            raise AssertionError("no judge call should happen in a unit test")

        def get_model_name(self):
            return "fake-judge"

    return _FakeLLM()


class TestJudgeModelIsNamedAndOverridable:
    """The judge model was the literal `"gemini-flash-lite-latest"` inside
    `_judge()` for the whole of wave 2 — the weakest tier Google offers, and
    the model that produced every score in the Module 27 three-pass run. Like
    the answer cap before it (Module 46), a value that shapes every published
    number must have a name a report can quote and an env var an experiment
    can move."""

    def test_default_model_is_a_named_constant(self):
        assert isinstance(gs.DEFAULT_JUDGE_MODEL, str)
        assert gs.DEFAULT_JUDGE_MODEL.startswith("gemini-")

    def test_the_retired_judge_is_no_longer_the_default(self):
        """`gemini-flash-lite-latest` graded the whole of wave 2, including the
        three-pass Module 27 run in which M7 was scored 0.1/0.3/0.2 on a
        correct answer."""
        assert gs.DEFAULT_JUDGE_MODEL != "gemini-flash-lite-latest"

    def test_the_default_is_a_pinned_version_not_a_moving_alias(self):
        """A `-latest` alias silently changes the judge under the reports that
        cite it, which is how wave 2 ended up unable to say which model
        produced its numbers. Module 87 pins a version instead."""
        assert not gs.DEFAULT_JUDGE_MODEL.endswith("-latest")

    def test_model_is_env_overridable(self, monkeypatch):
        monkeypatch.setenv("GOLD32_JUDGE_MODEL", "some-other-model")
        spec = importlib.util.spec_from_file_location("gold32_score_reload", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.JUDGE_MODEL == "some-other-model"

    def test_temperature_is_pinned_not_inherited(self):
        assert gs.JUDGE_TEMPERATURE == 0.0


class TestFactualPromptRules:
    """The FactualCorrectness prompt is the whole of the grading standard, so
    each rule it encodes is pinned to a real case that proved it was needed."""

    STEPS = property(lambda self: gs.FACTUAL_EVALUATION_STEPS)

    def test_the_prompt_is_a_module_level_constant(self):
        """So a test can pin one rule and an experiment can diff two prompts
        without re-declaring the metric."""
        assert isinstance(gs.FACTUAL_EVALUATION_STEPS, list)
        assert all(isinstance(s, str) for s in gs.FACTUAL_EVALUATION_STEPS)

    def test_the_close_numbers_rule_still_carries_its_worked_example(self):
        """Module 20's rule. Regression guard: it was an abstract sentence
        first and the judge ignored it until the real A1 case was attached."""
        joined = " ".join(gs.FACTUAL_EVALUATION_STEPS)
        assert "5-10%" in joined
        assert "67 males" in joined and "65 males" in joined

    def test_a_polarity_rule_is_present(self):
        """Module 87's rule, and the defect it exists for: M7 was scored
        0.1 / 0.3 / 0.2 across three passes on an answer carrying gold's
        figures EXACTLY, because gold opens 'Haan' (yes) and the answer opens
        'No'. Both say reporting became dramatically slower."""
        polarity = [s for s in gs.FACTUAL_EVALUATION_STEPS
                    if "SUBSTANTIVE CLAIM" in s]
        assert len(polarity) == 1, (
            "no polarity step in the FactualCorrectness prompt — a 'no' "
            "answering the literal question and a 'yes' answering an implied "
            "one are equivalent when the supporting facts agree"
        )
        step = polarity[0]
        assert "yes/no" in step
        assert "not a contradiction" in step.lower() or "NOT an error" in step

    def test_the_polarity_rule_carries_its_own_worked_example(self):
        """Module 20 proved an abstract rule alone is not honoured. The
        example is M7's real figures, in the house style of the close-numbers
        rule above it."""
        step = next(s for s in gs.FACTUAL_EVALUATION_STEPS
                    if "SUBSTANTIVE CLAIM" in s)
        assert "15.0 minutes" in step and "1401.3" in step
        assert "score HIGH" in step

    def test_the_polarity_rule_is_fenced_so_it_cannot_excuse_an_omission(self):
        """Measured regression, and the reason this fence exists. The first
        version of the polarity rule took KB9 from 0.27 (retired judge) to
        **1.0 on all three passes** on an answer that never reaches gold's
        CrPC s.174 or its 10 PPC-302 FIRs — the judge settled polarity and
        stopped checking facts. The same model with the rule removed scored
        KB9 0.3, so the rule caused it. Fenced, KB9 scores 0.4."""
        step = next(s for s in gs.FACTUAL_EVALUATION_STEPS
                    if "SUBSTANTIVE CLAIM" in s)
        assert "OPENING WORD AND NOTHING ELSE" in step
        assert "materially INCOMPLETE" in step

    def test_build_metrics_uses_the_constant_by_default(self):
        m = gs.build_metrics(judge=_fake_llm())["FactualCorrectness"]
        assert m.evaluation_steps == gs.FACTUAL_EVALUATION_STEPS

    def test_build_metrics_accepts_an_override_for_experiments(self):
        m = gs.build_metrics(judge=_fake_llm(),
                             evaluation_steps=["only step"])["FactualCorrectness"]
        assert m.evaluation_steps == ["only step"]


class TestDocsMatchTheJudgeContract:
    """`HOW_TO_REPRODUCE_THIS_EVALUATION.md` §3 is the judge contract as
    published to the testing team. It has to name the model that actually
    grades and list the rules that actually apply, or the team reproduces a
    different evaluation than the one reported."""

    def _doc(self):
        return open(os.path.join(_ROOT, "HOW_TO_REPRODUCE_THIS_EVALUATION.md"),
                    encoding="utf-8").read()

    def test_the_doc_names_the_default_judge_model(self):
        assert gs.DEFAULT_JUDGE_MODEL in self._doc()

    def test_the_doc_does_not_still_prescribe_the_retired_judge(self):
        doc = self._doc()
        for line in doc.splitlines():
            if "gemini-flash-lite-latest" in line and "GeminiModel(" in line:
                pytest.fail("§3.1 still prescribes the retired judge: " + line)

    def test_the_doc_lists_the_polarity_rule(self):
        doc = self._doc().lower()
        assert "polarity" in doc
        assert "1401.3" in self._doc()

    def test_the_doc_lists_as_many_rules_as_the_prompt_has_steps(self):
        """A numbered list in §3.3 that is shorter than the prompt is how the
        two drift apart."""
        doc = self._doc()
        section = doc.split("### 3.3")[1].split("### 3.4")[0]
        numbered = [l for l in section.splitlines()
                    if re.match(r"^\d+\. ", l)]
        assert len(numbered) == len(gs.FACTUAL_EVALUATION_STEPS), (
            "§3.3 lists %d rules but the prompt has %d steps"
            % (len(numbered), len(gs.FACTUAL_EVALUATION_STEPS))
        )
