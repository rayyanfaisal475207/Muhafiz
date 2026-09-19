"""
Phase 5C — containment tests for the independent AGE evaluator.

WHAT THESE GUARD. Phase 5 established, destructively, that Apache AGE 1.5.0
has no read-only mode: SET, REMOVE and DETACH DELETE all execute under
SELECT-only grants and `default_transaction_read_only=on`. Model-generated
Cypher therefore cannot be allowed near production, and the containment is a
CONNECTION-level boundary rather than a query-level one. These tests assert
the boundary holds and, just as importantly, that it fails CLOSED — an
unavailable evaluator must refuse, never fall back.

TWO TIERS. The offline tests (default) cover configuration, the executor's
public contract, and refusal behaviour with no database at all. The
`requires_postgres` tests connect to the real evaluator database and prove
isolation and snapshot fidelity; they are skipped by default, like
tests/test_rls_integration.py, because the suite's first rule is that it
runs anywhere in seconds.

    pytest tests/test_age_eval_containment.py
    pytest tests/test_age_eval_containment.py -m requires_postgres
"""
import os

import pytest

from src import config
from src.pipeline.aggregate import age_eval_client as ec


# ── offline: configuration fails closed ──────────────────────────────────

class TestEvaluatorConfigurationFailsClosed:
    """An unset or misdirected evaluator must disable the route, not default."""

    def test_unset_url_refuses_rather_than_falling_back(self, monkeypatch):
        monkeypatch.setattr(config, "AGE_EVAL_DATABASE_URL", "")
        with pytest.raises(ec.EvaluatorUnavailable) as exc:
            ec._eval_dsn()
        # The message must say there is no fallback, because the whole
        # failure mode is an operator assuming there is one.
        assert "does NOT fall back" in str(exc.value) or "not configured" in str(exc.value)

    def test_url_naming_production_is_refused(self, monkeypatch):
        """The exact copy-paste mistake that would re-run Phase 5's incident."""
        monkeypatch.setattr(
            config, "AGE_EVAL_DATABASE_URL",
            "postgresql://u:p@localhost:5432/muhafiz",
        )
        with pytest.raises(ec.EvaluatorUnavailable) as exc:
            ec._eval_dsn()
        assert "production" in str(exc.value).lower()

    def test_url_naming_any_other_database_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            config, "AGE_EVAL_DATABASE_URL",
            "postgresql://u:p@localhost:5432/somewhere_else",
        )
        with pytest.raises(ec.EvaluatorUnavailable):
            ec._eval_dsn()

    def test_sqlalchemy_form_is_normalised(self, monkeypatch):
        monkeypatch.setattr(
            config, "AGE_EVAL_DATABASE_URL",
            "postgresql+asyncpg://u:p@localhost:5432/muhafiz_age_eval",
        )
        assert ec._eval_dsn().startswith("postgresql://")

    def test_validate_config_flags_production_url_as_critical(self, monkeypatch):
        """Startup must treat a production-pointed evaluator as critical."""
        monkeypatch.setattr(
            config, "AGE_EVAL_DATABASE_URL",
            "postgresql://u:p@localhost:5432/muhafiz",
        )
        _warnings, critical = config.validate_config()
        assert any("AGE_EVAL_DATABASE_URL" in m for m in critical)


# ── offline: the executor's contract ─────────────────────────────────────

class TestExecutorExposesNoTargetParameter:
    """The model controls the query body and params. Nothing else."""

    def test_public_signature_has_no_target_arguments(self):
        import inspect

        params = set(inspect.signature(ec.execute_eval_cypher).parameters)
        assert params == {"cypher", "params", "columns"}
        for forbidden in ("graph", "database", "database_url", "role", "dsn"):
            assert forbidden not in params

    def test_graph_name_is_a_module_constant_not_an_argument(self):
        assert ec.EVAL_GRAPH_NAME == "evidence_graph_eval"
        assert ec.EXPECTED_EVAL_DB_NAME == "muhafiz_age_eval"
        assert ec.FORBIDDEN_DB_NAME == "muhafiz"

    def test_database_name_parsing(self):
        assert ec._database_name_of("postgresql://u:p@h:5432/muhafiz") == "muhafiz"
        assert ec._database_name_of("postgresql://u:p@h:5432/x?ssl=require") == "x"
        assert ec._database_name_of("") == ""


class TestNoProductionFallbackPath:
    """Structural: no code path here may reference the production DSN."""

    def test_module_never_reads_database_url(self):
        import inspect

        src = inspect.getsource(ec)
        body = "\n".join(
            ln for ln in src.splitlines()
            if not ln.strip().startswith("#")
        )
        # config.DATABASE_URL is the production connection. A single
        # reference to it in this module would be a fallback.
        assert "config.DATABASE_URL" not in body
        assert "AGE_EVAL_DATABASE_URL" in body

    def test_route_does_not_execute_raw_cypher_anywhere(self):
        """PHASE 5D UPDATED THIS TEST.

        It previously asserted the route executed through the EVALUATOR
        (`age_eval_client.execute_eval_cypher`) — correct while Phase 5C's
        containment was the runtime architecture. Phase 5D removed the
        untrusted text entirely: the model emits a typed plan, trusted code
        compiles it, and execution goes through
        `age_execution.execute_compiled_age_query`, which accepts only a
        compiler-produced object. The route must therefore reach NEITHER the
        raw production client NOR the evaluator at runtime.

        Checked against EXECUTABLE lines only: the docstring legitimately
        discusses both, explaining why it calls neither.

        PHASE 6 NARROWED THE SCOPE to `run()` -- the path that handles model
        output. A module-wide scan began failing when
        `collect_value_examples()` was added, which reads example property
        values for the PROMPT using fixed templates built from registry
        measurements. That helper cannot carry model text (see
        tests/test_age_safe_gateway.py), so banning it module-wide measured
        the wrong thing.
        """
        import inspect

        from src.pipeline.aggregate import route_age

        src = inspect.getsource(route_age.run)
        code = "\n".join(
            line for line in src.splitlines()
            if not line.strip().startswith("#")
        )
        if (doc := route_age.run.__doc__):
            code = code.replace(doc, "")

        assert "age_execution.execute_compiled_age_query" in code
        assert "age_client.execute_cypher(" not in code
        # The evaluator is test infrastructure now; no runtime dependency.
        assert "age_eval_client" not in code


class TestResourceLimitsAreConfigured:
    def test_limits_have_bounded_defaults(self):
        assert config.AGE_EVAL_STATEMENT_TIMEOUT_MS > 0
        assert config.AGE_EVAL_MAX_ROWS > 0
        assert config.AGE_EVAL_MAX_POOL_SIZE > 0

    def test_row_cap_is_enforced_by_over_fetching(self):
        """The SQL asks for max_rows+1 so the cap can be detected, not hidden."""
        import inspect

        assert "max_rows + 1" in inspect.getsource(ec.execute_eval_cypher)


# ── offline: reconciliation is snapshot-aware ────────────────────────────

class TestReconciliationRequiresSnapshotIdentity:
    def test_snapshot_gating_was_removed_in_phase_5d(self):
        """PHASE 5D REPLACED THIS TEST'S EXPECTATION.

        Phase 5C ran the AGE route against a disposable COPY of the graph,
        so an AGE figure described a possibly-stale vintage and results
        lacking a snapshot id were demoted to SINGLE_ROUTE_VALID. Phase 5D
        executes compiled read-only Cypher against production
        `evidence_graph` — the same data the structured route reads — so the
        asymmetry is gone and the gate would now suppress genuine
        agreements. This asserts the gate is absent, which is the whole
        point of the pivot.
        """
        from src.pipeline.aggregate import reconcile as rec
        from src.pipeline.aggregate.routes import NumericResult, RouteResult

        structured = RouteResult(
            route="structured", status="SUCCESS", result_shape="scalar",
            result=NumericResult(value=73, interpretation="count", grain="ENTITY"),
        )
        age = RouteResult(
            route="age_text2cypher", status="SUCCESS", result_shape="scalar",
            result=NumericResult(value=73, interpretation="count", grain="ENTITY"),
            provenance={"executed_against": "muhafiz.evidence_graph"},
        )
        out = rec.reconcile(structured=structured, age=age)
        assert out.classification == rec.AGREEMENT
        assert out.value == 73

    def test_non_evaluator_age_result_keeps_ordinary_semantics(self):
        """Phase 4 fixtures (no evaluator provenance) must be unaffected."""
        from src.pipeline.aggregate import reconcile as rec
        from src.pipeline.aggregate.routes import NumericResult, RouteResult

        structured = RouteResult(
            route="structured", status="SUCCESS", result_shape="scalar",
            result=NumericResult(value=4, interpretation="count", grain="ENTITY"),
        )
        age = RouteResult(
            route="age_text2cypher", status="SUCCESS", result_shape="scalar",
            result=NumericResult(value=70, interpretation="count", grain="ENTITY"),
            provenance={},
        )
        out = rec.reconcile(structured=structured, age=age)
        # The 4-vs-70 disagreement stays a CONFLICT, as Phase 4 established.
        assert out.classification == rec.CONFLICT

    def test_age_result_with_snapshot_is_compared_normally(self):
        from src.pipeline.aggregate import reconcile as rec
        from src.pipeline.aggregate.routes import NumericResult, RouteResult

        structured = RouteResult(
            route="structured", status="SUCCESS", result_shape="scalar",
            result=NumericResult(value=73, interpretation="count", grain="ENTITY"),
        )
        age = RouteResult(
            route="age_text2cypher", status="SUCCESS", result_shape="scalar",
            result=NumericResult(value=73, interpretation="count", grain="ENTITY"),
            provenance={"snapshot_id": "20260916T000000Z-abcd1234"},
        )
        out = rec.reconcile(structured=structured, age=age)
        assert out.classification == rec.AGREEMENT
        assert out.value == 73


# ── offline: the rebuild script's direction guard ────────────────────────

class TestRebuildDirectionGuard:
    """Reversing source and target would destroy production."""

    @staticmethod
    def _load():
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "scripts" / "rebuild_age_eval.py"
        spec = importlib.util.spec_from_file_location("_rb", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    PROD = "postgresql://u:p@localhost:5432/muhafiz"
    EVAL = "postgresql://u:p@localhost:5432/muhafiz_age_eval"

    def test_correct_direction_is_accepted(self):
        rb = self._load()
        assert rb.assert_direction(self.PROD, self.EVAL) == ("muhafiz", "muhafiz_age_eval")

    @pytest.mark.parametrize(
        "source,target,why",
        [
            ("EVAL", "PROD", "reversed — would overwrite production"),
            ("PROD", "PROD", "same database"),
            ("EVAL", "EVAL", "same database"),
            ("PROD", "", "no target"),
            ("", "EVAL", "no source"),
        ],
    )
    def test_dangerous_combinations_are_refused(self, source, target, why):
        rb = self._load()
        resolve = {"PROD": self.PROD, "EVAL": self.EVAL, "": ""}
        with pytest.raises(rb.DirectionError):
            rb.assert_direction(resolve[source], resolve[target])

    def test_script_contains_no_statement_targeting_production_graph(self):
        """No DROP/DELETE in this script may name the production graph."""
        import inspect

        rb = self._load()
        src = inspect.getsource(rb)
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("--"):
                continue
            lowered = stripped.lower()
            if "drop_graph" in lowered or "delete from" in lowered:
                assert "production_graph" not in lowered
                assert "'evidence_graph'" not in lowered


# ══════════════════════════════════════════════════════════════════════
# LIVE — skipped by default
# ══════════════════════════════════════════════════════════════════════

_LIVE_EVAL_URL = os.environ.get("AGE_EVAL_DATABASE_URL", "")

#: Same gate tests/test_rls_integration.py uses: the marker alone does not
#: deselect (pytest.ini has no -m filter), so the skipif is what actually
#: keeps the default run offline and fast.
#:
#:     RUN_POSTGRES_TESTS=1 pytest tests/test_age_eval_containment.py
_live = pytest.mark.skipif(
    not os.environ.get("RUN_POSTGRES_TESTS"),
    reason="Needs the live evaluator database (muhafiz_age_eval) with a "
           "verified snapshot. Set RUN_POSTGRES_TESTS=1 and "
           "AGE_EVAL_DATABASE_URL to run for real.",
)


@pytest.mark.requires_postgres
@_live
class TestLiveIsolation:
    """Proves the boundary against the real cluster.

    conftest repoints DATABASE_URL at a disposable name and blocks
    non-loopback sockets, so these read the evaluator DSN from the real
    environment themselves and connect over loopback.
    """

    async def test_evaluator_role_cannot_connect_to_production(self):
        """The boundary: refused at CONNECT, before any Cypher is parsed.

        Reads the DSN from `config` rather than os.environ — conftest
        rewrites DATABASE_URL and the evaluator URL may not be exported
        into the test process's environment.

        The assertion is that the connection did NOT succeed, not that a
        particular message came back: the cluster may answer with
        `permission denied for database` (the CONNECT grant) or with an
        authentication failure, and both mean the evaluator role did not
        reach production. Asserting one exact string made this test fail
        for a reason unrelated to containment.
        """
        import asyncpg

        from src import config

        eval_url = (config.AGE_EVAL_DATABASE_URL or "").replace(
            "postgresql+asyncpg://", "postgresql://"
        ).split("?", 1)[0]
        if not eval_url:
            pytest.skip("AGE_EVAL_DATABASE_URL not configured")

        prod_dsn = eval_url.replace("/muhafiz_age_eval", "/muhafiz")
        assert prod_dsn.endswith("/muhafiz")

        with pytest.raises(asyncpg.PostgresError) as exc:
            conn = await asyncpg.connect(prod_dsn, statement_cache_size=0)
            await conn.close()

        message = str(exc.value).lower()
        assert (
            "permission denied" in message
            or "authentication failed" in message
        ), f"connection was not refused as expected: {message}"

    async def test_evaluator_reaches_its_own_database_and_graph(self):
        if not _LIVE_EVAL_URL:
            pytest.skip("AGE_EVAL_DATABASE_URL not set")
        identity = await ec.assert_isolated()
        try:
            assert identity["database"] == "muhafiz_age_eval"
            assert identity["graph"] == "evidence_graph_eval"
        finally:
            await ec.close_eval_pool()

    async def test_snapshot_matches_production_counts(self):
        if not _LIVE_EVAL_URL:
            pytest.skip("AGE_EVAL_DATABASE_URL not set")
        try:
            snap = await ec.require_verified_snapshot()
            assert snap["verified"] is True
            assert snap["case_count"] == 73
            assert snap["person_count"] == 430
            assert snap["same_as_count"] == 4702

            for query, expected in [
                ("MATCH (c:Case) RETURN count(*) AS v", 73),
                ("MATCH (p:Person) RETURN count(*) AS v", 430),
                ("MATCH ()-[r:SAME_AS]->() RETURN count(*) AS v", 4702),
            ]:
                rows = await ec.execute_eval_cypher(query, columns=["v"])
                assert rows[0]["v"] == expected
        finally:
            await ec.close_eval_pool()

    async def test_copied_edges_resolve_against_copied_vertices(self):
        """Row counts alone would not catch dangling graphids."""
        if not _LIVE_EVAL_URL:
            pytest.skip("AGE_EVAL_DATABASE_URL not set")
        try:
            rows = await ec.execute_eval_cypher(
                "MATCH (p:Person)-[:BELONGS_TO_CASE]->(c:Case) "
                "RETURN count(DISTINCT p) AS v",
                columns=["v"],
            )
            assert rows[0]["v"] > 0
        finally:
            await ec.close_eval_pool()
