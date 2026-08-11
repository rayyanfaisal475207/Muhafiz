"""
Serving mode: the harness answers real chat queries through the live endpoint.

This is the cutover path — the one place harness output reaches an actual
investigator — so these tests defend the two things that would break a user's
experience without raising anything:

  * the ANSWER must arrive on `{step:"response", status:"streaming"}`. The
    frontend store appends `detail` to the message body for those frames and
    no others, so an answer sent any other way renders as an empty bubble.
  * SOURCES must arrive on `{step:"retrieval"|"web_search", status:"done"}`.
    `extractSources()` reads no other step, so chips sent elsewhere vanish.

Both are contracts with `frontend/src/store/chatStore.ts`, read from it rather
than assumed, and neither would fail loudly if broken.
"""
from __future__ import annotations

import pytest

from src import config
from src.pipeline.harness import serve
from src.pipeline.harness.contracts import (
    Citation, SubAgentResult, SubAgentStatus, ToolError,
)

KNOWN_STATUS = {"active", "done", "error", "retry", "skipped", "streaming"}


class _FakeState:
    def __init__(self, result, selected="semantic_search"):
        self.result = result
        self.selected_agent = selected
        self.events = []


async def _collect(monkeypatch, result, *, role="investigator", query="q"):
    """Run the adapter with the harness stubbed, and gather every frame."""
    async def fake_route(_q):
        return {"route": "RAG", "case_scope": "within_case", "target_entity": None}

    async def fake_invoke(agent_input, route_result, events=None, **kwargs):
        return _FakeState(result)

    async def fake_save(*args, **kwargs):
        return None

    # route_query is imported function-locally inside the adapter (harness
    # isolation rule), so it must be patched at its source module.
    import src.pipeline.router as router_mod
    monkeypatch.setattr(router_mod, "route_query", fake_route)
    monkeypatch.setattr(serve.supervisor, "invoke", fake_invoke)
    monkeypatch.setattr(serve, "async_save_history", fake_save)

    frames = []
    async for ev in serve.process_query_harness(
        "sess-1", query, case_id="CASE-1", user_id="u1", user_role=role,
        gateway=object(),
    ):
        frames.append(ev)
    return frames


def _answer_of(frames):
    return "".join(
        f.get("detail") or "" for f in frames
        if f["step"] == "response" and f["status"] == "streaming"
    )


def _sources_of(frames):
    out = []
    for f in frames:
        if f["step"] in ("retrieval", "web_search") and f["status"] == "done":
            out.extend(f.get("sources") or [])
    return out


# ══════════════════════════════════════════════════════════════════════════
# Off by default — this is the switch that changes what users see
# ══════════════════════════════════════════════════════════════════════════

def test_serve_mode_is_off_by_default():
    assert config.HARNESS_SERVE_MODE is False
    assert config.harness_serves(user_id="anyone") is False


def test_allow_list_restricts_serving_to_named_users(monkeypatch):
    """
    The allow-list exists so the harness can be demonstrated through the real UI
    without moving every account onto new code at once.
    """
    monkeypatch.setattr(config, "HARNESS_SERVE_MODE", True)
    monkeypatch.setattr(config, "HARNESS_SERVE_USERS", frozenset({"demo@example.com"}))

    assert config.harness_serves(email="demo@example.com") is True
    assert config.harness_serves(email="DEMO@EXAMPLE.COM") is True, "must be case-insensitive"
    assert config.harness_serves(email="someone.else@example.com") is False
    assert config.harness_serves(user_id="unknown-id") is False


def test_empty_allow_list_means_everyone(monkeypatch):
    monkeypatch.setattr(config, "HARNESS_SERVE_MODE", True)
    monkeypatch.setattr(config, "HARNESS_SERVE_USERS", frozenset())
    assert config.harness_serves(user_id="anyone") is True


# ══════════════════════════════════════════════════════════════════════════
# The frontend contract
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_answer_arrives_as_streaming_response_frames(monkeypatch):
    """
    The store appends `detail` to the message body ONLY for
    {step:"response", status:"streaming"}. Any other shape renders empty.
    """
    result = SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text="The complainant is Muhammad Ali [Document 1].",
        citations=[Citation(document_index=1, source_tool="RAG", source_file="f.pdf")],
        tools_used=["RAG"],
    )
    frames = await _collect(monkeypatch, result)
    assert _answer_of(frames) == "The complainant is Muhammad Ali [Document 1]."


@pytest.mark.asyncio
async def test_sources_arrive_on_the_step_extract_sources_reads(monkeypatch):
    """`extractSources()` looks at retrieval/web_search only."""
    result = SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text="answer",
        citations=[
            Citation(document_index=1, source_tool="RAG", source_file="a.pdf",
                     confidence=0.9),
            Citation(document_index=2, source_tool="GRAPH", source_file="b.pdf"),
        ],
        tools_used=["RAG", "GRAPH"],
    )
    frames = await _collect(monkeypatch, result)
    sources = _sources_of(frames)

    assert len(sources) == 2
    assert sources[0]["filename"] == "a.pdf"
    assert sources[0]["score"] == 0.9
    # [RESOLVED-1a] investigator-facing label, never the raw enum name.
    assert sources[0]["type"] == "document search"
    assert sources[1]["type"] == "case-graph search"


@pytest.mark.asyncio
async def test_every_frame_uses_the_known_status_vocabulary(monkeypatch):
    """An unknown status silently fails to update any step card."""
    result = SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="a", tools_used=["RAG"],
        caveats=["a stated gap"],
    )
    frames = await _collect(monkeypatch, result)
    for f in frames:
        assert "step" in f and "status" in f
        assert f["status"] in KNOWN_STATUS, f"unknown status: {f}"


# ══════════════════════════════════════════════════════════════════════════
# What the user is told
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_an_abstention_says_so_instead_of_rendering_empty(monkeypatch):
    """
    A sub-agent that abstains returns answer_text=None. Passing that straight
    through would leave the user staring at an empty bubble with no
    explanation — worse than the abstention itself.
    """
    result = SubAgentResult(
        status=SubAgentStatus.ABSTAINED, answer_text=None,
        tools_used=[], degraded_from=["RAG"],
    )
    frames = await _collect(monkeypatch, result)
    answer = _answer_of(frames)
    assert answer, "an abstention must still produce readable text"
    assert "could not find evidence" in answer.lower()


@pytest.mark.asyncio
async def test_caveats_are_appended_to_the_answer_the_user_reads(monkeypatch):
    """A stated gap the user never sees is the same as no gap at all."""
    result = SubAgentResult(
        status=SubAgentStatus.PARTIAL,
        answer_text="Here is what I found.",
        tools_used=["GRAPH"], degraded_from=["RAG"],
        caveats=["Case documents were unavailable for this summary."],
    )
    frames = await _collect(monkeypatch, result)
    answer = _answer_of(frames)
    assert "Here is what I found." in answer
    assert "Case documents were unavailable" in answer


@pytest.mark.asyncio
async def test_a_denial_shows_the_reason_not_a_blank_message(monkeypatch):
    result = SubAgentResult(
        status=SubAgentStatus.DENIED, answer_text=None,
        error=ToolError(kind="permission_denied",
                        message="Cross-case queries require supervisor role."),
    )
    frames = await _collect(monkeypatch, result)
    assert "supervisor role" in _answer_of(frames)


@pytest.mark.asyncio
async def test_an_unrecognised_role_is_refused_not_guessed(monkeypatch):
    """The cross-case gates are driven by this value; never default it."""
    result = SubAgentResult(status=SubAgentStatus.OK, answer_text="should not appear")
    frames = await _collect(monkeypatch, result, role="root")
    assert any(f["step"] == "system" and f["status"] == "error" for f in frames)
    assert "should not appear" not in _answer_of(frames)


@pytest.mark.asyncio
async def test_a_crash_still_produces_a_readable_message(monkeypatch):
    """
    By the time this runs the user is watching an empty bubble. An exception
    that escapes leaves it empty forever.
    """
    async def fake_route(_q):
        return {"route": "RAG", "case_scope": "within_case"}

    async def exploding_invoke(*args, **kwargs):
        raise RuntimeError("harness exploded")

    async def fake_save(*args, **kwargs):
        return None

    # route_query is imported function-locally inside the adapter (harness
    # isolation rule), so it must be patched at its source module.
    import src.pipeline.router as router_mod
    monkeypatch.setattr(router_mod, "route_query", fake_route)
    monkeypatch.setattr(serve.supervisor, "invoke", exploding_invoke)
    monkeypatch.setattr(serve, "async_save_history", fake_save)

    frames = []
    async for ev in serve.process_query_harness(
        "sess-1", "q", case_id="CASE-1", user_id="u1", gateway=object(),
    ):
        frames.append(ev)

    assert any(f["step"] == "system" and f["status"] == "error" for f in frames)
    assert _answer_of(frames), "the user must still get readable text"


@pytest.mark.asyncio
async def test_a_history_save_failure_does_not_blank_the_answer(monkeypatch):
    """The answer is already on screen; a save failure must not disturb it."""
    async def fake_route(_q):
        return {"route": "RAG", "case_scope": "within_case"}

    async def fake_invoke(*args, **kwargs):
        return _FakeState(SubAgentResult(
            status=SubAgentStatus.OK, answer_text="a real answer", tools_used=["RAG"],
        ))

    async def failing_save(*args, **kwargs):
        raise RuntimeError("database down")

    # route_query is imported function-locally inside the adapter (harness
    # isolation rule), so it must be patched at its source module.
    import src.pipeline.router as router_mod
    monkeypatch.setattr(router_mod, "route_query", fake_route)
    monkeypatch.setattr(serve.supervisor, "invoke", fake_invoke)
    monkeypatch.setattr(serve, "async_save_history", failing_save)

    frames = []
    async for ev in serve.process_query_harness(
        "sess-1", "q", case_id="CASE-1", user_id="u1", gateway=object(),
    ):
        frames.append(ev)

    assert _answer_of(frames) == "a real answer"


@pytest.mark.asyncio
async def test_unknown_keyword_arguments_are_tolerated(monkeypatch):
    """
    The endpoint passes `enable_web_search` (and may grow more) to whichever
    pipeline is selected. A TypeError here would take down the chat endpoint.
    """
    result = SubAgentResult(status=SubAgentStatus.OK, answer_text="ok", tools_used=["RAG"])

    async def fake_route(_q):
        return {"route": "RAG", "case_scope": "within_case"}

    async def fake_invoke(*args, **kwargs):
        return _FakeState(result)

    async def fake_save(*args, **kwargs):
        return None

    # route_query is imported function-locally inside the adapter (harness
    # isolation rule), so it must be patched at its source module.
    import src.pipeline.router as router_mod
    monkeypatch.setattr(router_mod, "route_query", fake_route)
    monkeypatch.setattr(serve.supervisor, "invoke", fake_invoke)
    monkeypatch.setattr(serve, "async_save_history", fake_save)

    frames = []
    async for ev in serve.process_query_harness(
        "sess-1", "q", case_id="CASE-1", user_id="u1",
        enable_web_search=True, some_future_flag=123, gateway=object(),
    ):
        frames.append(ev)

    assert _answer_of(frames) == "ok"
