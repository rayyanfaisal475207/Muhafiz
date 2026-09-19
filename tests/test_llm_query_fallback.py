# -*- coding: utf-8 -*-
"""
[Gold-QA fix — Module 178] The five bounds of the LLM-generated graph-query
fallback, one section per bound, plus the schema-sync and all-32 controls.

Offline by construction: `call_llm`, the read-only pool and the verifier
are all replaced with fakes; `tests/conftest.py` already refuses the live
database. The live half (the role's real refusals, the rollback proof, Q2
against the real graph, the investigator session) lives in
`evaluation/module178_*` and is recorded in MODULE178_RESULT.md.
"""
from __future__ import annotations

import json
import os
import re

import pytest

from src import config
from src.pipeline import llm_query_fallback as f
from src.pipeline import xagg
from src.pipeline.harness import supervisor as supervisor_mod
from src.pipeline.harness.supervisor import (
    CROSS_CASE_LINKAGE,
    META_ANALYSIS,
    Supervisor,
    classify_to_subagent,
)
from src.pipeline.harness.types import (
    CallerContext,
    ExecutionContext,
    PipelineEvent,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

Q2 = ("Is there anyone who used one of our citizen services who also turns out to be "
      "under investigation for a crime?")
Q3 = "Is anyone convicted in one matter but still being sought as a fugitive in another?"

Q2_QUERY = (
    "MATCH (s:StructuredRecord) WHERE s.record_type = 'pkm_application' "
    "MATCH (p:Person)-[r:INVOLVED_IN {role: 'accused'}]->(i:Incident)-[:PART_OF]->(c:Case) "
    "WHERE p.cnic = s.applicant_cnic "
    "RETURN DISTINCT p.canonical_name AS name, p.cnic AS cnic, c.case_id AS case_id LIMIT 50"
)
Q2_ROWS = [{"name": "سرفراز احمد", "cnic": "00000-1000055-1", "case_id": "fir-620-26"}]

XGRAPH_ROUTE = {"route": "XGRAPH", "case_scope": "cross_case", "output_format": "chat",
                "station": None, "district": None}


def _caller(role=Role.SUPERVISOR):
    return CallerContext(user_id="u-178", role=role, active_case_id=None)


def _input(text=Q2, role=Role.SUPERVISOR):
    return SubAgentInput(query_text=text, execution=ExecutionContext(caller=_caller(role)))


def _empty():
    return SubAgentResult(status=SubAgentStatus.EMPTY, answer_text="No cross-case connections were found.")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(config, "LLM_QUERY_FALLBACK_ENABLED", True)


@pytest.fixture
def fakes(monkeypatch):
    """Fake generation, execution and verification; records every call."""
    calls = {"generate": 0, "execute": [], "narrate": 0, "verify": [], "rollbacks": 0}
    state = {"query": Q2_QUERY, "rows": Q2_ROWS, "grounded": True, "run_error": None,
             "generate_error": None}

    async def _generate(question):
        calls["generate"] += 1
        if state["generate_error"]:
            raise state["generate_error"]
        return state["query"], "join citizens to accused on CNIC", "local:qwen3-14b"

    async def _execute(cypher, params, columns, *, timeout_s=None, graph=f.GRAPH_NAME):
        calls["execute"].append({"cypher": cypher, "params": params, "columns": columns})
        if state["run_error"]:
            raise state["run_error"]
        return list(state["rows"])

    async def _narrate(system_prompt, user_message, **kw):
        calls["narrate"] += 1
        return "One person: سرفراز احمد (CNIC 00000-1000055-1), accused in fir-620-26.", "local:qwen3-14b"

    async def _verify(answer, cited_chunks, case_id, cross_case_ids=None, target_date=None):
        calls["verify"].append({"answer": answer, "chunks": cited_chunks, "cross_case_ids": cross_case_ids})
        return {"grounded": state["grounded"], "off_topic": False, "reason": "fake"}

    monkeypatch.setattr(f, "generate_query", _generate)
    monkeypatch.setattr(f, "execute_readonly", _execute)
    monkeypatch.setattr(f, "_call_recorded", _narrate)
    import src.pipeline.verifier as verifier_mod
    monkeypatch.setattr(verifier_mod, "verify_grounding", _verify)
    return calls, state


def _gold32() -> list[dict]:
    path = os.path.join(ROOT, "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json")
    gold = json.load(open(path, encoding="utf-8"))
    assert len(gold) == 32
    return gold


def _route_baseline() -> dict[str, str]:
    path = os.path.join(ROOT, "evaluation", "gold32_route_baseline.json")
    return json.load(open(path, encoding="utf-8"))["routes"]


# ═══════════════════════════════════════════════════════════════════════
# Bound 1 — POSITION
# ═══════════════════════════════════════════════════════════════════════

def test_bound1_applies_only_after_every_layer_missed(enabled):
    # Q2's real shape: XGRAPH, cross_case, default sub-agent, EMPTY.
    assert f.applies(XGRAPH_ROUTE, Q2, CROSS_CASE_LINKAGE, _empty()) is True
    assert f.applies(XGRAPH_ROUTE, Q3, CROSS_CASE_LINKAGE, _empty()) is True
    abstained = SubAgentResult(status=SubAgentStatus.ABSTAINED)
    assert f.applies(XGRAPH_ROUTE, Q2, CROSS_CASE_LINKAGE, abstained) is True


@pytest.mark.parametrize("why, route, sub_agent, result, allow", [
    ("sub-agent answered", XGRAPH_ROUTE, CROSS_CASE_LINKAGE,
     SubAgentResult(status=SubAgentStatus.OK, answer_text="x"), True),
    ("sub-agent PARTIAL", XGRAPH_ROUTE, CROSS_CASE_LINKAGE,
     SubAgentResult(status=SubAgentStatus.PARTIAL, answer_text="x"), True),
    ("denied is not a miss", XGRAPH_ROUTE, CROSS_CASE_LINKAGE,
     SubAgentResult(status=SubAgentStatus.DENIED), True),
    ("infrastructure error is not a miss", XGRAPH_ROUTE, CROSS_CASE_LINKAGE,
     SubAgentResult(status=SubAgentStatus.ABSTAINED,
                    error=ToolError(kind="timeout", message="deadline")), True),
    ("selection hit: Meta-Analysis", XGRAPH_ROUTE, META_ANALYSIS, _empty(), True),
    ("nested sub-query", XGRAPH_ROUTE, CROSS_CASE_LINKAGE, _empty(), False),
    ("XAGG route (catch-all always answers)", {**XGRAPH_ROUTE, "route": "XAGG"},
     "Large-Scale Aggregate", _empty(), True),
    ("RAG route", {**XGRAPH_ROUTE, "route": "RAG"}, "Semantic Search", _empty(), True),
    ("within-case scope", {**XGRAPH_ROUTE, "case_scope": "within_case"}, CROSS_CASE_LINKAGE, _empty(), True),
    ("file output", {**XGRAPH_ROUTE, "output_format": "file_docx"}, CROSS_CASE_LINKAGE, _empty(), True),
])
def test_bound1_every_other_situation_is_refused(enabled, why, route, sub_agent, result, allow):
    assert f.applies(route, Q2, sub_agent, result, allow_meta_analysis=allow) is False, why


def test_bound1_flag_off_never_fires(monkeypatch):
    monkeypatch.setattr(config, "LLM_QUERY_FALLBACK_ENABLED", False)
    assert f.applies(XGRAPH_ROUTE, Q2, CROSS_CASE_LINKAGE, _empty()) is False
    assert f.applies(XGRAPH_ROUTE, Q2, CROSS_CASE_LINKAGE, _empty(), enabled=True) is True


def test_bound1_a_keyword_or_semantic_hit_above_blocks_it(enabled, monkeypatch):
    monkeypatch.setattr(xagg, "resolves_to_specific_aggregate", lambda q: True)
    assert f.applies(XGRAPH_ROUTE, Q2, CROSS_CASE_LINKAGE, _empty()) is False


def test_bound1_a_plan_or_trigger_hit_above_blocks_it(enabled):
    # A Meta-Analysis trigger phrase and a Module 79 plan phrase both count
    # as a hit one layer up, even when the route is XGRAPH.
    triggered = "Review the whole caseload and flag anything that looks worth monitoring"
    assert any(p.search(triggered) for p in supervisor_mod._META_ANALYSIS_TRIGGER_PATTERNS)
    assert f.dispatch_layers_missed(XGRAPH_ROUTE, triggered) is False
    assert f.dispatch_layers_missed(XGRAPH_ROUTE, Q2) is True


def test_bound1_zero_of_32_gold_questions_reach_this_path(enabled):
    """The all-32 control, structurally: with the measured route baseline,
    the sub-agent `classify_to_subagent()` picks, and the WORST case
    assumed for the sub-agent (EMPTY), `applies()` is False for all 32 —
    and the reason is always one layer above, never this one."""
    routes = _route_baseline()
    reached, reasons = [], {}
    for item in _gold32():
        route = routes[item["id"]]
        rr = {"route": route, "case_scope": "cross_case" if route in ("XAGG", "XGRAPH", "XNETWORK")
              else "within_case", "output_format": "chat", "station": None, "district": None}
        sub_agent = classify_to_subagent(rr, item["question"])
        if f.applies(rr, item["question"], sub_agent, _empty()):
            reached.append(item["id"])
        if route not in f.FALLBACK_ROUTES:
            reasons[item["id"]] = f"route {route}"
        elif sub_agent != CROSS_CASE_LINKAGE:
            reasons[item["id"]] = f"selection hit: {sub_agent}"
        else:
            reasons[item["id"]] = "dispatch hit above"
    assert reached == [], reached
    # And the three XNETWORK gold questions are kept out by SELECTION, not
    # by luck: each one is a Meta-Analysis trigger/plan hit.
    for qid in ("CR3", "G1", "G6"):
        assert reasons[qid].startswith("selection hit: Meta-Analysis"), (qid, reasons[qid])


@pytest.mark.asyncio
async def test_bound1_supervisor_calls_the_fallback_only_on_that_branch(monkeypatch, enabled):
    """Supervisor.handle(): the EMPTY Cross-Case Linkage result on an
    XGRAPH question is replaced by the fallback's result; an OK result on
    the same question is returned untouched; the flag off returns the
    EMPTY result untouched. Fails on origin/main (no branch exists)."""
    async def _route(q):
        return dict(XGRAPH_ROUTE)
    monkeypatch.setattr(supervisor_mod, "route_query", _route)
    served = SubAgentResult(status=SubAgentStatus.PARTIAL, answer_text="fallback", tools_used=["LLM_QUERY"])
    attempts = []

    async def _attempt(agent_input, route_result, prior, *, emit=None, gateway=None):
        attempts.append(agent_input.query_text)
        return served, f.FallbackOutcome(fired=True, served=True)
    monkeypatch.setattr(f, "attempt", _attempt)

    async def _handler_factory(result):
        async def _h(agent_input, *, on_event=None, gateway=None):
            return result
        _h.name = CROSS_CASE_LINKAGE
        return _h

    empty = _empty()
    sup = Supervisor(registry={CROSS_CASE_LINKAGE: await _handler_factory(empty)})
    assert (await sup.handle(_input())) is served
    assert attempts == [Q2]

    ok = SubAgentResult(status=SubAgentStatus.OK, answer_text="found")
    sup = Supervisor(registry={CROSS_CASE_LINKAGE: await _handler_factory(ok)})
    assert (await sup.handle(_input())) is ok
    assert attempts == [Q2]

    monkeypatch.setattr(config, "LLM_QUERY_FALLBACK_ENABLED", False)
    sup = Supervisor(registry={CROSS_CASE_LINKAGE: await _handler_factory(empty)})
    assert (await sup.handle(_input())) is empty
    assert attempts == [Q2]


# ═══════════════════════════════════════════════════════════════════════
# Bound 2 — READ-ONLY, LEAST PRIVILEGE
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("kw", sorted(f.FORBIDDEN_KEYWORDS))
def test_bound2_every_write_keyword_is_rejected_before_the_database(kw):
    q = f"MATCH (c:Case) {kw} c.case_id RETURN c.case_id AS id"
    with pytest.raises(f.QueryRejected) as exc:
        f.validate_query(q)
    assert kw in exc.value.reason
    # ...and case does not help.
    with pytest.raises(f.QueryRejected):
        f.validate_query(q.replace(kw, kw.lower()))
    # ...and hiding it in a string literal does not unlock it for a real
    # write either: the literal is blanked, so only a bare keyword is the
    # forbidden token — a string is data.
    assert f.validate_query(f"MATCH (c:Case) WHERE c.case_id = '{kw}' RETURN c.case_id AS id")


@pytest.mark.parametrize("q, why", [
    ("MATCH (c:Case) RETURN c.case_id AS id; MATCH (p:Person) RETURN p", "semicolon"),
    ("MATCH (c:Case) RETURN c.case_id AS id LIMIT $n", "parameter"),
    ("MATCH (c:Case) RETURN c.case_id AS id $cypher$", "dollar-quote"),
    ("MATCH (c:Case) RETURN c.case_id AS id // x", "comment"),
    ("MATCH (x:Suspect) RETURN x.case_id AS id", "unknown node label"),
    ("MATCH (p:Person)-[:KNOWS]->(q:Person) RETURN p.cnic AS c", "unknown relationship type"),
    ("MATCH (p:Person) RETURN p.nickname AS n", "unknown property"),
    ("MATCH (p:Person)-[:INVOLVED_IN]->(i:Incident) RETURN i.case_id AS id", "Incident has no property case_id"),
    ("MATCH (p:Person)-[r:INVOLVED_IN]->(i:Incident) RETURN r.cnic AS c", "relationships have no property cnic"),
    ("MATCH (p:Person) RETURN p.canonical_name AS n, shell(p) AS s", "function not allowed"),
    ("MATCH (p:Person)-[:INVOLVED_IN]->(x) RETURN x.entity_id AS e", "no label"),
    ("RETURN 1 AS one", "start with MATCH"),
    ("MATCH (p:Person) RETURN p.cnic AS a UNION MATCH (q:Person) RETURN q.cnic AS a", "exactly one RETURN"),
    ("MATCH (p:Person) RETURN `p`.cnic AS c", "backtick"),
    ("MATCH (p:Person) RETURN p.cnic AS c, mystery AS m", "unknown identifier"),
    # Structural checks against the live census: a hop that exists in no
    # direction, a hop written backwards, an invented enumerated value —
    # each of these RUNS CLEAN in AGE and returns nothing, which is the
    # plausible-wrong outcome the first live Q2 run served.
    ("MATCH (s:StructuredRecord)-[:APPEARS_IN]->(p:Person) RETURN p.cnic AS c",
     "no APPEARS_IN edge runs StructuredRecord -> Person (it runs Person -> StructuredRecord)"),
    ("MATCH (p:Person)<-[:INVOLVED_IN]-(i:Incident) RETURN p.cnic AS c", "no INVOLVED_IN edge runs Incident -> Person"),
    ("MATCH (c:Case)-[:INVOLVED_IN]->(p:Person) RETURN p.cnic AS c", "no INVOLVED_IN edge runs Case -> Person"),
    ("MATCH (p:Person)-[:INVOLVED_IN {role: 'suspect'}]->(i:Incident) RETURN p.cnic AS c", "role has no value 'suspect'"),
    ("MATCH (s:StructuredRecord) WHERE s.record_type IN ['pkm_application', 'licence'] RETURN s.record_id AS r",
     "record_type has no value 'licence'"),
])
def test_bound2_allow_list_rejects_anything_off_schema(q, why):
    with pytest.raises(f.QueryRejected) as exc:
        f.validate_query(q)
    assert why in exc.value.reason


def test_bound2_structural_checks_accept_what_the_graph_holds():
    for q in (
        "MATCH (p:Person)-[:INVOLVED_IN]-(i:Incident) RETURN p.cnic AS c",            # undirected: either way
        "MATCH (i:Incident)<-[:INVOLVED_IN]-(p:Person) RETURN p.cnic AS c",           # reversed arrow, right direction
        "MATCH (a:Person)-[:ASSOCIATED_WITH*1..3]-(b:Person) RETURN a.cnic AS c",     # variable length: skipped
        "MATCH (p:Person)-[r:RELATED_TO {role: 'بھائی'}]->(q:Person) RETURN p.cnic AS c",  # Urdu free text: not enumerated
        "MATCH (o:Officer)-[:ASSIGNED_TO]->(c:Case)<-[:PART_OF]-(i:Incident) RETURN o.belt_no AS b",
    ):
        assert f.validate_query(q)


def test_bound2_a_plain_read_over_the_schema_validates():
    v = f.validate_query(Q2_QUERY)
    assert v.node_vars == {"s": "StructuredRecord", "p": "Person", "i": "Incident", "c": "Case"}
    assert v.columns == ["name", "cnic", "case_id"]


class _FakeTx:
    def __init__(self, log):
        self.log = log

    async def start(self):
        self.log.append("BEGIN READ ONLY")

    async def rollback(self):
        self.log.append("ROLLBACK")

    async def commit(self):
        self.log.append("COMMIT")


class _FakeConn:
    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail

    def transaction(self, readonly=False):
        assert readonly is True
        return _FakeTx(self.log)

    async def execute(self, sql):
        self.log.append(sql)

    async def fetch(self, sql, params):
        self.log.append(("FETCH", sql, params))
        if self.fail:
            raise RuntimeError("syntax error at or near NOT")
        return []


class _FakePool:
    def __init__(self, log, fail=False):
        self.conn = _FakeConn(log, fail)

    async def close(self):
        pass

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()


@pytest.mark.asyncio
async def test_bound2_runs_on_the_readonly_dsn_only_and_always_rolls_back(monkeypatch):
    dsns = []

    async def _create_pool(dsn, **kw):
        dsns.append(dsn)
        return _FakePool(log)

    async def _noop_load(conn):
        pass
    import src.graph.age_client as age_client
    monkeypatch.setattr(age_client, "_load_age", _noop_load)
    monkeypatch.setattr(f.asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(config, "MCP_DATABASE_URL", "postgresql+asyncpg://muhafiz_mcp_readonly:pw@localhost:5432/muhafiz")
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql+asyncpg://muhafiz_app:pw@localhost:5432/muhafiz")
    await f.close_readonly_pool()
    log: list = []
    await f.execute_readonly("MATCH (c:Case) RETURN c.case_id AS id", {}, ["id"], timeout_s=15)
    assert dsns == ["postgresql://muhafiz_mcp_readonly:pw@localhost:5432/muhafiz"]
    assert log[0] == "BEGIN READ ONLY"
    assert log[1] == "SET LOCAL statement_timeout = 15000"
    assert log[-1] == "ROLLBACK" and "COMMIT" not in log
    await f.close_readonly_pool()

    # A failing statement still rolls back.
    async def _create_pool_fail(dsn, **kw):
        return _FakePool(log2, fail=True)
    monkeypatch.setattr(f.asyncpg, "create_pool", _create_pool_fail)
    log2: list = []
    with pytest.raises(RuntimeError):
        await f.execute_readonly("MATCH (c:Case) RETURN c.case_id AS id", {}, ["id"])
    assert log2[-1] == "ROLLBACK"
    await f.close_readonly_pool()


@pytest.mark.asyncio
async def test_bound2_no_readonly_role_means_no_query_not_the_app_role(monkeypatch):
    monkeypatch.setattr(config, "MCP_DATABASE_URL", "")
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql+asyncpg://muhafiz_app:pw@localhost:5432/muhafiz")
    await f.close_readonly_pool()
    with pytest.raises(RuntimeError, match="MCP_DATABASE_URL"):
        await f.execute_readonly("MATCH (c:Case) RETURN c.case_id AS id", {}, ["id"])


# ═══════════════════════════════════════════════════════════════════════
# Bound 3 — SCOPED
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bound3_investigator_is_denied_before_any_model_or_database_call(enabled, fakes):
    calls, _ = fakes
    events = []
    result, outcome = await f.attempt(_input(role=Role.INVESTIGATOR), XGRAPH_ROUTE, _empty(),
                                      emit=events.append)
    assert result.status is SubAgentStatus.DENIED
    assert result.error.kind == "permission_denied"
    assert calls["generate"] == 0 and calls["execute"] == [] and calls["narrate"] == 0
    assert outcome.reason == "denied" and outcome.query is None


def test_bound3_scope_is_injected_onto_every_bound_node_not_trusted_to_the_model():
    v = f.validate_query(Q2_QUERY)
    scoped = f.inject_case_scope(v.cypher, v.node_vars)
    for var in ("s", "p", "i"):
        assert f"MATCH ({var})-[:BELONGS_TO_CASE]->(_s" in scoped, var
    assert "MATCH (c:Case) WHERE c.case_id IN $case_ids" in scoped
    assert scoped.count("$case_ids") == 4
    # Every scope clause sits BEFORE the RETURN — i.e. it constrains the
    # rows, it is not appended after the projection.
    assert scoped.rfind("$case_ids") < scoped.find("RETURN")
    # Anonymous labelled nodes are named and scoped too.
    v2 = f.validate_query("MATCH (p:Person)-[:OWNS]->(:Weapon) RETURN p.cnic AS c")
    scoped2 = f.inject_case_scope(v2.cypher, v2.node_vars)
    assert "(_anon1:Weapon)" in scoped2 and "MATCH (_anon1)-[:BELONGS_TO_CASE]" in scoped2
    # Reference nodes are left alone.
    v3 = f.validate_query("MATCH (c:Case)-[:FILED_AT]->(ps:PoliceStation) RETURN ps.name AS n")
    scoped3 = f.inject_case_scope(v3.cypher, v3.node_vars)
    assert "MATCH (ps)" not in scoped3 and "MATCH (c:Case) WHERE c.case_id IN $case_ids" in scoped3
    # Scoping a WITH-segmented query scopes each variable where it is bound.
    v4 = f.validate_query("MATCH (p:Person) WITH p.cnic AS cnic MATCH (q:Person) WHERE q.cnic = cnic "
                          "RETURN q.canonical_name AS n")
    scoped4 = f.inject_case_scope(v4.cypher, v4.node_vars)
    assert scoped4.index("MATCH (p)-[:BELONGS_TO_CASE]") < scoped4.index("WITH")
    assert scoped4.index("MATCH (q)-[:BELONGS_TO_CASE]") > scoped4.index("WITH")


def test_bound3_optional_match_is_refused_under_scoping_fail_closed():
    v = f.validate_query("MATCH (p:Person) OPTIONAL MATCH (p)-[:OWNS]->(w:Weapon) "
                         "RETURN p.cnic AS c, w.canonical_name AS w")
    with pytest.raises(f.QueryRejected, match="OPTIONAL MATCH"):
        f.inject_case_scope(v.cypher, v.node_vars)


@pytest.mark.asyncio
async def test_bound3_jurisdiction_allow_list_is_bound_as_case_ids(enabled, fakes, monkeypatch):
    calls, _ = fakes

    async def _resolve(**kw):
        assert kw["user_role"] == "supervisor"
        return ["fir-620-26", "fir-142-26"]
    import src.retrieval.graph_retriever as gr
    monkeypatch.setattr(gr, "resolve_jurisdiction_case_ids", _resolve)
    route = {**XGRAPH_ROUTE, "station": "Khanna"}
    result, outcome = await f.attempt(_input(), route, _empty())
    assert result.status is SubAgentStatus.PARTIAL
    ran = calls["execute"][0]
    assert ran["params"] == {"case_ids": ["fir-620-26", "fir-142-26"]}
    assert "$case_ids" in ran["cypher"] and "BELONGS_TO_CASE]->(_s" in ran["cypher"]
    # Unscoped supervisor (no station/district in the question): no
    # injection, no params — the same behaviour every aggregate has.
    result, _ = await f.attempt(_input(), XGRAPH_ROUTE, _empty())
    assert calls["execute"][1]["params"] == {} and "$case_ids" not in calls["execute"][1]["cypher"]


# ═══════════════════════════════════════════════════════════════════════
# Bound 4 — BOUNDED
# ═══════════════════════════════════════════════════════════════════════

def test_bound4_row_cap_is_clamped_or_appended():
    assert f.apply_row_cap("MATCH (c:Case) RETURN c.case_id AS id", 200).endswith("LIMIT 200")
    assert f.apply_row_cap("MATCH (c:Case) RETURN c.case_id AS id LIMIT 999", 200).endswith("LIMIT 200")
    assert f.apply_row_cap("MATCH (c:Case) RETURN c.case_id AS id LIMIT 5", 200).endswith("LIMIT 5")
    assert f.apply_row_cap("MATCH (c:Case) RETURN c.case_id AS id ORDER BY id DESC LIMIT 1000", 200).endswith(
        "ORDER BY id DESC LIMIT 200")
    assert config.LLM_QUERY_FALLBACK_ROW_CAP == 200
    assert config.LLM_QUERY_FALLBACK_TIMEOUT_S == 15


@pytest.mark.asyncio
async def test_bound4_one_generation_attempt_never_a_retry(enabled, fakes):
    calls, state = fakes
    # (a) generation itself fails -> abstain, one call, nothing run
    state["generate_error"] = f.QueryRejected("the model did not return a query object")
    result, outcome = await f.attempt(_input(), XGRAPH_ROUTE, _empty())
    assert result.status is SubAgentStatus.ABSTAINED and calls["generate"] == 1 and calls["execute"] == []
    assert "could not answer" in result.caveats[0]
    # (b) the generated text fails validation -> abstain WITH the query shown, no run
    state["generate_error"] = None
    state["query"] = "MATCH (c:Case) SET c.x = 1 RETURN c.case_id AS id"
    result, outcome = await f.attempt(_input(), XGRAPH_ROUTE, _empty())
    assert result.status is SubAgentStatus.ABSTAINED and calls["generate"] == 2 and calls["execute"] == []
    assert any("SET" in c and state["query"] in c for c in result.caveats)
    assert result.error is None  # never `invalid_input` — cutover would hand that to the legacy path
    # (c) the query errors in the database -> abstain WITH the query and the error, no second run
    state["query"] = Q2_QUERY
    state["run_error"] = RuntimeError("syntax error at or near NOT")
    result, outcome = await f.attempt(_input(), XGRAPH_ROUTE, _empty())
    assert result.status is SubAgentStatus.ABSTAINED and calls["generate"] == 3 and len(calls["execute"]) == 1
    assert any("syntax error" in c for c in result.caveats) and any(Q2_QUERY in c for c in result.caveats)
    assert calls["narrate"] == 0


@pytest.mark.asyncio
async def test_bound4_timeout_is_a_statement_timeout_on_the_connection(monkeypatch):
    async def _create_pool(dsn, **kw):
        return _FakePool(log)

    async def _noop_load(conn):
        pass
    import src.graph.age_client as age_client
    monkeypatch.setattr(age_client, "_load_age", _noop_load)
    monkeypatch.setattr(f.asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(config, "MCP_DATABASE_URL", "postgresql://muhafiz_mcp_readonly:pw@localhost/muhafiz")
    monkeypatch.setattr(config, "LLM_QUERY_FALLBACK_TIMEOUT_S", 15.0)
    await f.close_readonly_pool()
    log: list = []
    await f.execute_readonly("MATCH (c:Case) RETURN c.case_id AS id", {}, ["id"])
    assert "SET LOCAL statement_timeout = 15000" in log
    await f.close_readonly_pool()


# ═══════════════════════════════════════════════════════════════════════
# Bound 5 — VISIBLE
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bound5_served_answer_shows_the_query_and_is_marked_unverified(enabled, fakes):
    calls, _ = fakes
    events: list[PipelineEvent] = []
    result, outcome = await f.attempt(_input(), XGRAPH_ROUTE, _empty(), emit=events.append)
    assert result.status is SubAgentStatus.PARTIAL
    assert result.tools_used == ["LLM_QUERY"] and result.degraded_from == ["XGRAPH", "XNETWORK"]
    assert result.caveats[0] == f.UNVERIFIED_CAVEAT
    assert Q2_QUERY in result.caveats[1]  # the query verbatim (plus the appended row cap)
    assert "سرفراز احمد" in result.answer_text
    # The UI signal: a citation_validator event whose detail says
    # "unverified" (MessageBubble's existing warning pill), and the
    # fallback's own event carrying the query as an extra.
    cv = [e for e in events if e.step == "citation_validator"]
    assert cv and "unverified" in cv[0].detail
    done = [e for e in events if e.step == "llm_query_fallback" and e.status == "done"]
    assert done and done[0].model_extra["query"].startswith("MATCH") and done[0].model_extra["rows"] == 1
    assert done[0].model_extra["model"] == "local:qwen3-14b"
    # The display label the frontend gets for this source says so too.
    from src.pipeline.harness.types import SOURCE_TOOL_DISPLAY_LABELS
    assert "unverified" in SOURCE_TOOL_DISPLAY_LABELS["LLM_QUERY"]


@pytest.mark.asyncio
async def test_bound5_prose_is_verified_against_the_rows_and_raw_rows_serve_when_it_fails(enabled, fakes):
    calls, state = fakes
    result, outcome = await f.attempt(_input(), XGRAPH_ROUTE, _empty())
    assert len(calls["verify"]) == 1
    chunk = calls["verify"][0]["chunks"][0]
    assert "00000-1000055-1" in chunk["text"] and calls["verify"][0]["cross_case_ids"] == ["fir-620-26"]
    assert outcome.verifier["grounded"] is True

    state["grounded"] = False
    result, outcome = await f.attempt(_input(), XGRAPH_ROUTE, _empty())
    assert result.status is SubAgentStatus.PARTIAL
    assert result.answer_text.startswith("1 row(s) returned")  # the deterministic rendering, not the prose
    assert any("did not pass grounding verification" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_bound5_empty_rows_are_served_as_a_stated_no_match_not_silence(enabled, fakes):
    calls, state = fakes
    state["rows"] = []
    result, outcome = await f.attempt(_input(), XGRAPH_ROUTE, _empty())
    assert result.status is SubAgentStatus.PARTIAL and outcome.rows == []
    assert result.caveats[0] == f.UNVERIFIED_CAVEAT


# ═══════════════════════════════════════════════════════════════════════
# Schema sync — the prompt's schema is docs/graph_schema.md's
# ═══════════════════════════════════════════════════════════════════════

def test_prompt_schema_names_only_labels_and_edges_documented_in_graph_schema_md():
    doc = open(os.path.join(ROOT, "docs", "graph_schema.md"), encoding="utf-8").read()
    for label in f.KNOWN_LABELS:
        assert f"`{label}`" in doc, label
    for edge in f.KNOWN_EDGE_TYPES:
        assert f"`{edge}`" in doc, edge
    for name in re.findall(r"\b([A-Z][a-z][a-zA-Z]+)\s*\{", f.SCHEMA_SUMMARY):
        assert name in f.KNOWN_LABELS, name
    for edge in re.findall(r"\[:([A-Z_]+)", f.SCHEMA_SUMMARY):
        assert edge in f.KNOWN_EDGE_TYPES, edge
    # The two AGE facts this codebase learned the hard way are in the rules.
    assert "INVOLVED_IN" in f.SCHEMA_SUMMARY and "NOT to Case" in f.SCHEMA_SUMMARY
    assert "WHERE NOT (a)-[:R]->(b)" in f.AGE_RULES


def test_default_is_off():
    """The mechanism ships; the default is the finding (MODULE178_RESULT.md §6)."""
    src = open(os.path.join(ROOT, "src", "config.py"), encoding="utf-8").read()
    assert 'os.getenv("LLM_QUERY_FALLBACK_ENABLED", "false")' in src
