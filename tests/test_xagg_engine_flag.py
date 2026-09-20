# -*- coding: utf-8 -*-
"""
The XAGG engine flag: routing, translation, and what must not change.

`AGGREGATE_ENGINE_MODE` selects which engine answers a cross-case aggregate
question. These guard the three things that make the flag safe:

  1. OFF BY DEFAULT — an unset environment takes the legacy path, byte for
     byte. This is the property that lets the flag ship.
  2. The seam is a branch, not a rewrite — legacy behaviour is reached
     through exactly the code it always was.
  3. The boundary translates without reinterpreting — a refusal stays a
     refusal, a conflict serves no number, and assurance qualifiers survive
     into the text rather than being flattened into a bare figure.

No live database or model is used: the engines are stubbed, because what is
under test is the routing and the translation, not the aggregate itself.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Optional

import pytest

from src import config
from src.pipeline.harness.tools import xagg as xagg_tool_mod
from src.pipeline.harness.tools import xagg_v1_adapter as adapter
from src.pipeline.harness.types import ToolStatus


# ── Stand-ins for an AggregateAnswer ──────────────────────────────────
@dataclasses.dataclass
class _Answer:
    status: str = "ANSWERED"
    value: Any = 92
    interpretation: Optional[str] = "count_distinct"
    grain: Optional[str] = "ENTITY"
    refusal_code: Optional[str] = None
    refusal_reason: Optional[str] = None
    warnings: tuple = ()
    verification_note: str = ""
    structured: Any = None


class TestFlagDefaultsToLegacy:
    """The property the whole design rests on."""

    def test_default_mode_is_legacy(self, monkeypatch):
        """An UNSET environment selects legacy.

        This used to assert on the live `config.AGGREGATE_ENGINE_MODE`, which
        made the suite fail on any machine actually running the new engine —
        it was testing the deployment rather than the code. What matters is
        the default, so the environment is cleared and the module reloaded.
        """
        import importlib

        monkeypatch.delenv("AGGREGATE_ENGINE_MODE", raising=False)
        reloaded = importlib.reload(config)
        try:
            assert reloaded.AGGREGATE_ENGINE_MODE == reloaded.AGGREGATE_ENGINE_LEGACY
        finally:
            # Restore whatever this process was actually configured with, so
            # one reload does not leak into the rest of the suite.
            importlib.reload(config)

    def test_the_two_modes_are_distinct(self):
        assert config.AGGREGATE_ENGINE_LEGACY != config.AGGREGATE_ENGINE_V1


class TestRendering:
    """Translation must not reinterpret."""

    def test_a_scalar_answer_states_the_value(self):
        text = adapter.render_answer_text(_Answer())
        assert "92" in text
        assert "count_distinct" in text

    def test_a_refusal_renders_as_a_refusal_with_its_reason(self):
        text = adapter.render_answer_text(_Answer(
            status="REFUSED", value=None,
            refusal_code="constraint_lost",
            refusal_reason="the question requires a filter that was dropped",
        ))
        assert "not answered" in text.lower()
        assert "constraint_lost" in text
        assert "filter that was dropped" in text

    def test_a_refusal_never_renders_a_number(self):
        """The failure this whole engine exists to prevent."""
        text = adapter.render_answer_text(_Answer(
            status="REFUSED", value=None, refusal_code="spec_invalid",
            refusal_reason="no such field",
        ))
        assert "92" not in text

    def test_a_conflict_serves_no_figure(self):
        text = adapter.render_answer_text(_Answer(
            status="CONFLICT", value=None,
            refusal_reason="routes disagreed",
        ))
        assert "no figure" in text.lower()
        assert "routes disagreed" in text

    def test_assurance_qualifiers_survive_into_the_text(self):
        """"Verified" and "nothing checked this" must stay distinguishable."""
        text = adapter.render_answer_text(_Answer(
            verification_note="Not independently verified.",
            warnings=("INSUFFICIENT_DATA_COVERAGE: incomplete.",),
        ))
        assert "Not independently verified" in text
        assert "INSUFFICIENT_DATA_COVERAGE" in text

    def test_grouped_results_render_every_group_and_a_total(self):
        text = adapter.render_answer_text(_Answer(
            value=[{"key": "a", "count": 3}, {"key": "b", "count": 1}],
            interpretation="count",
        ))
        assert "a: 3" in text and "b: 1" in text
        assert "Total groups: 2" in text

    def test_a_null_group_key_is_named_not_hidden(self):
        text = adapter.render_answer_text(_Answer(
            value=[{"key": None, "count": 7}], interpretation="count",
        ))
        assert "no value recorded" in text

    def test_grouped_output_is_bounded(self):
        rows = [{"key": f"k{i}", "count": i} for i in range(200)]
        text = adapter.render_answer_text(_Answer(value=rows, interpretation="count"))
        assert "and 175 more group(s)" in text
        assert "Total groups: 200" in text


class TestCaseIdsTouched:
    def test_absent_provenance_yields_no_ids_rather_than_a_guess(self):
        assert adapter.case_ids_touched(_Answer()) == []

    def test_recorded_ids_are_carried(self):
        class _S:
            provenance = {"case_ids": ["C-1", "C-2"]}

        assert adapter.case_ids_touched(_Answer(structured=_S())) == ["C-1", "C-2"]


class TestToolResultTranslation:
    """The XAggToolResult contract, built from an AggregateAnswer."""

    def _build(self, answer):
        from src.pipeline.harness.types import ChunkMetadata, EvidenceChunk

        return adapter.to_tool_result(
            answer,
            result_cls=xagg_tool_mod.XAggToolResult,
            status_ok=ToolStatus.OK,
            chunk_cls=EvidenceChunk,
            metadata_cls=ChunkMetadata,
        )

    def test_an_answer_becomes_an_ok_result_with_evidence(self):
        result = self._build(_Answer())
        assert result.status is ToolStatus.OK
        assert len(result.chunks) == 1
        assert "92" in result.raw_summary_text

    def test_a_refusal_is_a_successful_call_not_a_tool_failure(self):
        """The tool worked; the answer is "this cannot be answered"."""
        result = self._build(_Answer(
            status="REFUSED", value=None, refusal_code="spec_invalid",
            refusal_reason="no such field",
        ))
        assert result.status is ToolStatus.OK
        assert "not answered" in result.raw_summary_text.lower()

    def test_aggregate_kind_is_left_unset(self):
        """It names the LEGACY engine's canned families; v1 has none."""
        assert self._build(_Answer()).aggregate_kind is None

    def test_raw_summary_text_matches_the_chunk(self):
        """The Verifier's fallback text and the evidence must not diverge."""
        result = self._build(_Answer())
        assert result.raw_summary_text == result.chunks[0].text


class TestScopeIsCarriedNotWidened:
    """Authorization must reach the new engine unchanged."""

    @pytest.mark.asyncio
    async def test_scope_is_built_from_the_authenticated_caller(self, monkeypatch):
        seen: dict = {}

        async def _fake_answer(snapshot, question, scope, **kwargs):
            seen["role"] = scope.user_role
            seen["user_id"] = scope.user_id
            seen["kind"] = scope.kind
            seen["question"] = question
            return _Answer()

        monkeypatch.setattr(config, "AGGREGATE_ENGINE_MODE", config.AGGREGATE_ENGINE_V1)
        from src.pipeline.aggregate import orchestrator as agg_orch
        from src.pipeline.aggregate import registry as agg_reg
        from src.pipeline.aggregate import route_age as agg_age

        monkeypatch.setattr(agg_orch, "answer_question", _fake_answer)
        monkeypatch.setattr(agg_reg, "get_registry", lambda **k: _async(None))
        monkeypatch.setattr(agg_age, "collect_value_examples", lambda s: _async({}))
        monkeypatch.setattr(agg_age, "build_schema_card", lambda s, e=None: "card")

        result = await xagg_tool_mod.xagg_tool(_tool_input("how many?", "supervisor"))

        assert seen["role"] == "supervisor"
        assert seen["kind"] == "cross_case"
        assert seen["question"] == "how many?"
        assert result.status is ToolStatus.OK

    @pytest.mark.asyncio
    async def test_a_scope_denial_is_reported_as_denied(self, monkeypatch):
        async def _denied(snapshot, question, scope, **kwargs):
            return _Answer(
                status="REFUSED", value=None,
                refusal_code="scope_denied",
                refusal_reason="requires supervisor role or higher",
            )

        monkeypatch.setattr(config, "AGGREGATE_ENGINE_MODE", config.AGGREGATE_ENGINE_V1)
        from src.pipeline.aggregate import orchestrator as agg_orch
        from src.pipeline.aggregate import registry as agg_reg
        from src.pipeline.aggregate import route_age as agg_age

        monkeypatch.setattr(agg_orch, "answer_question", _denied)
        monkeypatch.setattr(agg_reg, "get_registry", lambda **k: _async(None))
        monkeypatch.setattr(agg_age, "collect_value_examples", lambda s: _async({}))
        monkeypatch.setattr(agg_age, "build_schema_card", lambda s, e=None: "card")

        # `investigator` is a real, non-privileged role — the orchestrator's
        # scope gate refuses cross-case aggregates below supervisor.
        result = await xagg_tool_mod.xagg_tool(
            _tool_input("how many?", "investigator")
        )

        assert result.status is ToolStatus.DENIED
        assert result.error.kind == "permission_denied"

    @pytest.mark.asyncio
    async def test_an_engine_crash_is_an_upstream_failure(self, monkeypatch):
        async def _boom(snapshot, question, scope, **kwargs):
            raise RuntimeError("engine exploded")

        monkeypatch.setattr(config, "AGGREGATE_ENGINE_MODE", config.AGGREGATE_ENGINE_V1)
        from src.pipeline.aggregate import orchestrator as agg_orch
        from src.pipeline.aggregate import registry as agg_reg
        from src.pipeline.aggregate import route_age as agg_age

        monkeypatch.setattr(agg_orch, "answer_question", _boom)
        monkeypatch.setattr(agg_reg, "get_registry", lambda **k: _async(None))
        monkeypatch.setattr(agg_age, "collect_value_examples", lambda s: _async({}))
        monkeypatch.setattr(agg_age, "build_schema_card", lambda s, e=None: "card")

        result = await xagg_tool_mod.xagg_tool(_tool_input("how many?", "supervisor"))

        assert result.status is ToolStatus.FAILED
        assert result.error.kind == "upstream_failure"


class TestLegacyModeIsUntouched:
    @pytest.mark.asyncio
    async def test_legacy_mode_never_reaches_the_new_engine(self, monkeypatch):
        """With the flag off, `answer_question` must not be called at all."""
        called: list = []

        async def _should_not_run(*a, **k):
            called.append(1)
            return _Answer()

        async def _fake_legacy(*a, **k):
            return {"kind": "total_count", "total": 73}

        monkeypatch.setattr(
            config, "AGGREGATE_ENGINE_MODE", config.AGGREGATE_ENGINE_LEGACY
        )
        from src.pipeline.aggregate import orchestrator as agg_orch

        monkeypatch.setattr(agg_orch, "answer_question", _should_not_run)
        monkeypatch.setattr(xagg_tool_mod, "run_aggregate", _fake_legacy)
        monkeypatch.setattr(
            xagg_tool_mod, "_render_aggregate_text", lambda r: "73 cases"
        )
        monkeypatch.setattr(xagg_tool_mod, "_case_ids_touched", lambda r: [])
        monkeypatch.setattr(
            xagg_tool_mod, "get_gateway", lambda: _async(object())
        )

        result = await xagg_tool_mod.xagg_tool(_tool_input("how many?", "supervisor"))

        assert called == []
        assert result.status is ToolStatus.OK
        assert result.aggregate_kind == "total_count"


# ── helpers ───────────────────────────────────────────────────────────
async def _async(value):
    return value


def _tool_input(question: str, role: str):
    from src.pipeline.harness.types import CallerContext, ExecutionContext, Role

    return xagg_tool_mod.XAggToolInput(
        query_text=question,
        execution=ExecutionContext(
            caller=CallerContext(user_id="u-1", role=Role(role)),
        ),
    )


# ── Cross-case role gate ──────────────────────────────────────────────
class TestCrossCaseRoleGate:
    """The orchestrator's gate must admit exactly the roles route_age does.

    It did not. The set was written out by hand with UNDERSCORES —
    {"supervisor", "station_admin", "platform_admin"} — under a comment
    claiming it was "identical to the set route_age.run enforces", while
    `route_age` and the `Role` enum both use HYPHENS. So
    `"platform-admin" in _CROSS_CASE_ROLES` was False and every admin was
    refused a cross-case aggregate; only bare "supervisor" matched, being
    the one value with no separator. The gate runs before the engine, so
    with aggregate_v2 enabled no admin could reach the new engine at all.

    Caught on a live deployment, not by this suite, which is why it is here.
    """

    def test_gate_matches_route_age_exactly(self):
        from src.pipeline.aggregate.orchestrator import _CROSS_CASE_ROLES
        from src.pipeline.aggregate.route_age import (
            _CROSS_CASE_ROLES as ROUTE_AGE_ROLES,
        )

        assert set(_CROSS_CASE_ROLES) == set(ROUTE_AGE_ROLES)

    def test_every_admin_role_is_admitted(self):
        """The regression itself: these are real Role values, not guesses."""
        from src.pipeline.aggregate.orchestrator import _CROSS_CASE_ROLES
        from src.pipeline.harness.types import Role

        for role in (Role.SUPERVISOR, Role.STATION_ADMIN, Role.PLATFORM_ADMIN):
            assert role.value in _CROSS_CASE_ROLES, (
                f"{role.value} must reach the aggregate engine"
            )

    def test_investigator_is_still_refused(self):
        from src.pipeline.aggregate.orchestrator import _CROSS_CASE_ROLES
        from src.pipeline.harness.types import Role

        assert Role.INVESTIGATOR.value not in _CROSS_CASE_ROLES

    def test_no_role_in_the_gate_uses_an_underscore(self):
        """The shape of the original bug, guarded directly."""
        from src.pipeline.aggregate.orchestrator import _CROSS_CASE_ROLES

        assert not [r for r in _CROSS_CASE_ROLES if "_" in r]
