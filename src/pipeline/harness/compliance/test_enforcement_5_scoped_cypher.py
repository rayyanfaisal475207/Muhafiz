"""
Enforcement point 5 (AGENT_HARNESS_DESIGN.md §4.5) — `scoped_cypher()`'s
structural guard (src/graph/case_scope.py), the one true chokepoint for
WITHIN-CASE Cypher templates. It refuses (`ValueError`) to execute any
template that doesn't literally reference `$case_id`.

None of Phase 0's 7 tool wrappers write Cypher directly — they all
delegate graph access to existing functions (retrieve_graph /
run_aggregate / run_network_query) that already route their own
within-case templates through this chokepoint. Two things checked
independently:

  (a) STATIC — no tool wrapper file imports `age_client` or calls
      `execute_cypher` directly. [PRESERVE — design §4.5] "If the harness
      introduces new within-case Cypher templates anywhere ... route them
      through `scoped_cypher()`, not raw `age_client.execute_cypher()`."
      A wrapper reaching for age_client directly would be exactly that
      violation, regardless of whether the template it built happened to
      be safe today.
  (b) REGRESSION — `scoped_cypher()` itself still refuses a missing
      case_id and a template with no `$case_id` reference. This doesn't
      change with Phase 0 (the harness doesn't touch case_scope.py at
      all), but a compliance suite that never re-verifies its own
      chokepoint is still standing is checking everything except the one
      thing every other check in this module assumes holds.
"""

import pytest

from src.graph.case_scope import scoped_cypher
from src.pipeline.harness.compliance._source_scan import all_tool_wrapper_sources

_FORBIDDEN_CYPHER_PATTERNS = ("age_client", "execute_cypher")


@pytest.mark.parametrize("module_name,source", all_tool_wrapper_sources().items())
def test_tool_wrapper_never_bypasses_scoped_cypher_chokepoint(module_name, source):
    for pattern in _FORBIDDEN_CYPHER_PATTERNS:
        assert pattern not in source, (
            f"{module_name} references {pattern!r} — a tool wrapper must "
            "never execute Cypher directly. Any new within-case Cypher "
            "template must go through src.graph.case_scope.scoped_cypher() "
            "(design §4.5); today's wrappers should have no reason to "
            "touch age_client at all, since they delegate to existing "
            "functions that already do this correctly."
        )


@pytest.mark.asyncio
async def test_scoped_cypher_refuses_missing_case_id():
    with pytest.raises(ValueError):
        await scoped_cypher("MATCH (n) WHERE n.x = $case_id RETURN n", case_id=None)


@pytest.mark.asyncio
async def test_scoped_cypher_refuses_missing_case_id_empty_string():
    with pytest.raises(ValueError):
        await scoped_cypher("MATCH (n) WHERE n.x = $case_id RETURN n", case_id="")


@pytest.mark.asyncio
async def test_scoped_cypher_refuses_template_without_case_id_reference():
    with pytest.raises(ValueError):
        await scoped_cypher("MATCH (n) RETURN n", case_id="CASE-001")


@pytest.mark.asyncio
async def test_scoped_cypher_refuses_case_id_collision_in_params():
    with pytest.raises(ValueError):
        await scoped_cypher(
            "MATCH (n {case_id: $case_id}) RETURN n",
            case_id="CASE-001",
            params={"case_id": "CASE-002"},
        )
