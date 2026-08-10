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

The same reasoning covers ANSWER GENERATION. Sub-agents now call the real
generator (`src/pipeline/harness/generation.py` -> `call_llm`), which reaches
the model server over the network. That is deliberately stubbed here for the
identical reason the tools are: a contract test asserting the citation
contract, the bounded handoff, or degradation bookkeeping must not depend on a
live model — and must never make a billable call. `_stub_generation` below
patches generation at ITS boundary, in the modules that imported it.
"""
import pytest

from src.pipeline.harness.tools import registry


@pytest.fixture(autouse=True)
def _stub_tools():
    """Pin every harness tool to its stub for the duration of a test."""
    registry.use_stubs()
    yield
    registry.use_real()


@pytest.fixture(autouse=True)
def _stub_generation(monkeypatch):
    """
    Replace real answer generation with a deterministic stand-in.

    WHAT THIS MUST PRESERVE, and why each part is load-bearing:

    * `[Document N]` markers, 1-based over the chunk list, because the
      Verifier's deterministic checks and `Citation.document_index` both depend
      on that positional correspondence (design §5). A stub that omitted them
      would make every grounding assertion vacuous.
    * `UNGROUNDED_TRIGGER` passthrough, so the verifier-rejection path stays
      reachable — the verifier inspects the ANSWER, so a trigger that never
      reached it could not exercise the rejection.
    * Deliberately NOT concatenating chunk text, so answer-boundedness
      assertions keep testing the sub-agent rather than the stub.

    Patched per-module rather than at `generation.<fn>`: each sub-agent does
    `from ...generation import generate_case_scoped_answer`, binding the
    function into its own namespace at import, so patching the source module
    afterwards would not affect the already-bound reference.
    """
    from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER

    def _fake_answer(query_text: str, n_chunks: int) -> str:
        markers = " ".join(f"[Document {i}]" for i in range(1, n_chunks + 1))
        prefix = f"{UNGROUNDED_TRIGGER} " if UNGROUNDED_TRIGGER in query_text else ""
        return (
            f"{prefix}Based on the case documents, the retrieved passages "
            f"address the query. {markers}"
        )

    async def fake_case_scoped(*, query_text, chunks, **kwargs):
        return _fake_answer(query_text, len(chunks))

    async def fake_cross_case(*, query_text, chunks, **kwargs):
        return _fake_answer(query_text, len(chunks))

    async def fake_text_block(*, query_text, text_block, **kwargs):
        # XAGG's single computed block is always one citable "document".
        return _fake_answer(query_text, 1)

    targets = {
        "generate_case_scoped_answer": fake_case_scoped,
        "generate_cross_case_answer": fake_cross_case,
        "generate_from_text_block": fake_text_block,
    }

    import importlib
    import pkgutil

    import src.pipeline.harness.agents as agents_pkg

    # Patch the source module too, for any call site resolving it lazily.
    import src.pipeline.harness.generation as generation_mod

    for name, fake in targets.items():
        monkeypatch.setattr(generation_mod, name, fake, raising=False)

    for mod_info in pkgutil.iter_modules(agents_pkg.__path__):
        module = importlib.import_module(f"{agents_pkg.__name__}.{mod_info.name}")
        for name, fake in targets.items():
            if hasattr(module, name):
                monkeypatch.setattr(module, name, fake)


@pytest.fixture(autouse=True)
def _restore_supervisor_registry():
    """
    Restore `_NODES` and `_route` after every test.

    Several tests register a node and then `pop()` it in a finally block. That
    was harmless while only Semantic Search was registered — the popped names
    were all temporary. Once the real wiring registered all seven, those same
    pops began DELETING legitimate entries, so a test asserting the registry's
    contents passed alone and failed in a full run, depending on ordering.

    Snapshotting here makes registry mutation safe by default, rather than
    depending on every test cleaning up perfectly.
    """
    from src.pipeline.harness import supervisor

    saved_nodes = dict(supervisor._NODES)
    saved_route = supervisor._route
    yield
    supervisor._NODES.clear()
    supervisor._NODES.update(saved_nodes)
    supervisor._route = saved_route
