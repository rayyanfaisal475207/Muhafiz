"""
Harness test fixtures.

The tool registry defaults to the REAL implementations, which reach retrieval,
the graph, the gateway, and the network. The harness's own tests are contract
tests: they verify the shapes and guarantees in docs/SUBAGENT_INTERFACES.md §1
(fallback_to_rag polarity, role-gate ordering, source_tool tagging, the bounded
handoff boundary), none of which need live infrastructure — and all of which
must stay runnable with no database, no model server, and no network.

So these tests pin the registry to the stub implementations. That is not a
weaker test: the stubs return deliberately verbose, near-duplicate evidence
specifically to keep the summarization boundary under realistic pressure.

Tests that want the real implementations should opt in explicitly and mock the
production boundary they exercise.
"""
import pytest

from src.pipeline.harness.tools import registry


@pytest.fixture(autouse=True)
def _stub_tools():
    """Pin every harness tool to its stub for the duration of a test."""
    registry.use_stubs()
    yield
    registry.use_real()
