"""`evaluation/gold32_run.py` + `gold32_score.py` — a timeout is not an answer
(Module 42).

The defect this file guards against, in one sentence: **the runner gave up on a
slow question after 300 seconds and the scorer then judged the resulting empty
string, turning a transport failure into a published 0.0.**

It is not hypothetical, and it has a name. `EVALUATION_REPORT_POST_FIXES.md`
§4.1 lists KB6 as the only gold question scoring FactualCorrectness **0.0 AND**
AnswerRelevancy **0.0**, with **`route=None`**, and calls it a *"genuine error,
did not recover"* — the one failure that survived the credential fix which
rescued 15 of the other 17.

Measured live on this branch, five runs, `admin@example.com`, All Cases:
KB6 returns **`route='RAG'` 5 times out of 5**. Four of those five abstain and
take **428.8-628.2 seconds**; the fifth answers in **247.1s**. Passing the
relevance gate early is what makes a run fast, so the 300s ceiling did not
sample KB6 randomly -- it decided the published row by a coin flip.

KB6 is slow because it is a legal-KB question in All-Cases scope: it pays a
KB-only retrieval pass *and* a mixed-pool fallback pass at three evaluator
attempts each — six rounds of retrieve/rerank/evaluate. The old hard-coded
300s ceiling expired mid-request; `main()`'s exception handler wrote
`route=None` with an empty answer; and the scorer fed `"(no answer produced)"`
to the judge, which scored it 0.0 on both metrics.

Nothing in that row described the pipeline. `route=None` meant "the SSE stream
was never read", not "classification failed".

So three things are pinned here:

  1. The runner's timeout is a named, env-overridable constant, and is set
     above the slowest question actually measured on this machine.
  2. A failed request is recorded as `transport_ok: False` — distinguishable
     from a request that completed and returned a deliberate abstention.
  3. The scorer leaves such a row **unscored** (`None`), and `summarize()`
     therefore excludes it from every mean, instead of zeroing it.

The negative controls matter as much as the positives: a genuine abstention,
and a legacy outputs file with no `transport_ok` key at all, must both still be
scored. This fix must cost coverage, never hide a real failure.

No network anywhere in this file.
"""
import importlib.util
import json
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_GOLD = os.path.join(_ROOT, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")


def _load(name):
    path = os.path.join(_ROOT, "evaluation", name)
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gs = _load("gold32_score.py")
gr = _load("gold32_run.py")


# ── the literal gold text, asserted present ─────────────────────────────────
#
# Loaded from the dataset rather than retyped, and asserted rather than
# skipped: a test referencing a bare `Gold_QA_Dataset_Final32.json` (a file
# that does not exist) silently skipped for weeks before PR #21 caught it, and
# Module 30 pinned its KB tests this way for exactly that reason.

def _gold_by_id():
    assert os.path.exists(_GOLD), (
        "the Gold-32 dataset with answers is missing at %s — this test pins "
        "KB6's literal gold text and must fail loudly, not skip" % _GOLD
    )
    return {g["id"]: g for g in json.load(open(_GOLD, encoding="utf-8"))}


GOLD = _gold_by_id()


def test_kb6_gold_text_is_the_question_this_module_was_filed_for():
    """Pins the question itself, so a dataset edit can't quietly retarget this."""
    kb6 = GOLD["KB6"]
    assert kb6["language"].lower().startswith("roman")
    q = kb6["question"]
    # the norm clause and the our-data clause — KB6 is compound
    assert "forensics guidelines" in q
    assert "baramad shuda aslaha" in q
    assert "weapon register" in q
    # gold's statutory half is the Forensics guidelines' firearms rule
    a = kb6["answer"].lower()
    assert "unloaded" in a or "safety" in a


# ── 1. the runner's timeout ─────────────────────────────────────────────────

class TestRunnerTimeout:

    def test_timeout_is_not_the_300s_that_produced_the_kb6_row(self):
        assert gr.TIMEOUT_S != 300, (
            "300s is the ceiling that expired mid-KB6 and produced the "
            "route=None / 0.0 / 0.0 row reported as a genuine pipeline error"
        )

    def test_timeout_clears_the_slowest_question_measured_live(self):
        # KB6 measured at 628.2, 599.0, 513.5, 428.8 and 247.1s, on this
        # machine with two sibling worktrees running concurrently.
        assert gr.TIMEOUT_S >= 700, (
            "KB6 takes up to 628s live; a ceiling below that reintroduces the "
            "exact defect this module was filed for"
        )

    def test_timeout_is_env_overridable_for_a_slower_machine(self, monkeypatch):
        monkeypatch.setenv("GOLD32_TIMEOUT_S", "1234")
        reloaded = _load("gold32_run.py")
        assert reloaded.TIMEOUT_S == 1234

    def test_timeout_still_bounds_a_genuinely_hung_request(self):
        """Not infinite — a hung request must still fail, not wedge the run."""
        assert gr.TIMEOUT_S is not None and gr.TIMEOUT_S < 24 * 3600


# ── 2. route parsing: what route=None actually meant ────────────────────────

class TestRouteParsing:
    """`route=None` is a property of the STREAM, not of classification."""

    def _sse(self, *events):
        return "".join("data: %s\n\n" % json.dumps(e) for e in events)

    def test_route_is_read_from_the_dispatch_event(self):
        sse = self._sse(
            {"step": "supervisor:dispatch", "status": "active",
             "detail": "Classified query as route='RAG' -> sub-agent='Semantic Search'"},
            {"step": "response", "status": "done", "answer": "A" * 50},
        )
        assert gr.parse(sse)["route"] == "RAG"

    def test_route_is_none_when_the_stream_was_never_read(self):
        """The KB6 case: no dispatch event seen, so no route — not a failure
        to classify."""
        assert gr.parse("")["route"] is None

    def test_an_abstention_still_carries_its_route(self):
        """KB6 live: route='RAG' *and* an abstention. The report's row showed
        neither, because the request never returned at all."""
        sse = self._sse(
            {"step": "supervisor:dispatch", "status": "active",
             "detail": "Classified query as route='RAG' -> sub-agent='Semantic Search'"},
            {"step": "response", "status": "done",
             "answer": "No sufficiently relevant documents were found for this "
                       "question after retrying with query refinements."},
        )
        parsed = gr.parse(sse)
        assert parsed["route"] == "RAG"
        assert "No sufficiently relevant documents" in parsed["actual_answer"]


# ── 3. the scorer leaves a transport failure unscored ───────────────────────

class TestNoAnswerCapturedIsUnscored:

    def test_a_failed_request_is_flagged(self):
        assert gs.no_answer_captured(
            {"id": "KB6", "actual_answer": "", "route": None,
             "status": "error", "transport_ok": False,
             "error": "TimeoutError: The read operation timed out"}
        )

    def test_the_reason_says_it_is_not_a_zero(self):
        reason = gs.no_answer_reason(
            {"error": "TimeoutError: The read operation timed out"})
        assert "Not a zero" in reason
        assert "TimeoutError" in reason

    # ── negative controls ───────────────────────────────────────────────

    def test_a_genuine_abstention_is_still_scored(self):
        """The live KB6 answer. It is a real failure and must keep counting as
        one — this fix must not launder an abstention into an unscored row."""
        assert not gs.no_answer_captured({
            "id": "KB6", "transport_ok": True, "route": "RAG",
            "actual_answer": "No sufficiently relevant documents were found "
                             "for this question after retrying with query "
                             "refinements.",
        })

    def test_a_legacy_row_without_the_key_is_still_scored(self):
        """Outputs files written before this key existed were genuinely
        measured; a missing key must never retroactively unscore them."""
        assert not gs.no_answer_captured(
            {"id": "KB1", "actual_answer": "CrPC ss.154 and 155 ...", "route": "RAG"})

    def test_a_completed_request_with_an_empty_answer_is_still_scored(self):
        """`transport_ok: True` with nothing in it is a pipeline that returned
        emptiness — that IS a real 0.0, and must not be excused."""
        assert not gs.no_answer_captured(
            {"id": "X", "transport_ok": True, "actual_answer": ""})


# ── 4. summarize() excludes, never zeroes ───────────────────────────────────

class TestUnscoredIsExcludedFromTheMean:

    def _row(self, qid, fc, ar):
        return {"id": qid, "type": "KB", "language": "roman-urdu", "route": "RAG",
                "scores": {"FactualCorrectness": fc, "AnswerRelevancy": ar},
                "reasons": {"FactualCorrectness": "", "AnswerRelevancy": ""}}

    def test_a_transport_failure_does_not_drag_the_mean_down(self):
        scored_only = gs.summarize([self._row("KB1", 1.0, 1.0),
                                    self._row("KB8", 1.0, 1.0)])
        with_failure = gs.summarize([self._row("KB1", 1.0, 1.0),
                                     self._row("KB8", 1.0, 1.0),
                                     self._row("KB6", None, None)])
        assert scored_only["metrics"]["FactualCorrectness"]["mean"] == 1.0
        assert with_failure["metrics"]["FactualCorrectness"]["mean"] == 1.0, (
            "an unscored row must be excluded from the mean, not averaged in "
            "as a zero"
        )

    def test_the_unscored_row_is_reported_loudly(self):
        sm = gs.summarize([self._row("KB1", 1.0, 1.0), self._row("KB6", None, None)])
        fc = sm["metrics"]["FactualCorrectness"]
        assert fc["scored"] == 1
        assert fc["unscored"] == 1
        assert "KB6" in fc["unscored_ids"]

    def test_zeroing_it_would_have_shown_a_different_number(self):
        """Guards the claim, not just the code: this is the size of the error
        the old behaviour introduced."""
        zeroed = gs.summarize([self._row("KB1", 1.0, 1.0), self._row("KB8", 1.0, 1.0),
                               self._row("KB6", 0.0, 0.0)])
        assert zeroed["metrics"]["FactualCorrectness"]["mean"] == 0.667
