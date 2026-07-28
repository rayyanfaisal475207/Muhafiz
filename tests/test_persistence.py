"""
Chat persistence — the bug class that made conversations vanish.

Guards:
  * Sessions must ALWAYS be created with an owner. Ownerless rows are invisible
    in the sidebar (filtered by user_id) and 403 on reopen.
  * DirectGateway must implement the full DataGateway protocol surface — it's
    the only backend this repo has (local/self-hosted Postgres via direct SQL,
    no Supabase REST fallback).
"""
import uuid

import pytest

from src.data_gateway.base import DataGateway
from src.data_gateway.direct_backend import DirectGateway
from src.database.pipeline_logger import PgPipelineLogger


# ── Session ownership ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_saved_session_is_visible_to_its_owner(gateway, user_id, session_id):
    """A session created for a user must come back from the sidebar query."""
    await gateway.create_session(session_id, user_id, "Mobile Theft Section", None)

    listed = await gateway.get_sessions_for_user(user_id)

    assert [s["session_id"] for s in listed] == [session_id]


@pytest.mark.asyncio
async def test_history_round_trips(gateway, user_id, session_id):
    """Messages saved during a chat must load back in order."""
    await gateway.create_session(session_id, user_id, "T", None)
    await gateway.save_message(session_id, "user", "What PPC section covers mobile theft?")
    await gateway.save_message(session_id, "assistant", "379 PPC.")

    history = await gateway.get_session_history(session_id)

    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "What PPC section covers mobile theft?"),
        ("assistant", "379 PPC."),
    ]


@pytest.mark.asyncio
async def test_conversation_store_creates_session_with_owner(patched_gateway, user_id, session_id):
    """
    Regression: save_history used to create the session without a user_id when
    no row existed yet, orphaning every conversation.
    """
    from src.memory.conversation import async_save_history

    await async_save_history(session_id, "hello", "hi there", user_id)

    session = await patched_gateway.get_session(session_id)
    assert session is not None
    assert session["user_id"] == user_id, "session was created without an owner"


@pytest.mark.asyncio
async def test_save_history_rejects_writing_into_another_users_session(patched_gateway, user_id, session_id):
    """
    Phase 5, Module 5.3 regression: save_history previously had NO
    ownership check at all — asymmetric with load_history/delete_history
    in the same module. A caller supplying another user's session_id
    could have messages appended into that user's conversation.
    """
    from src.memory.conversation import async_save_history

    await patched_gateway.create_session(session_id, str(uuid.uuid4()), "someone else's", None)

    with pytest.raises(PermissionError):
        await async_save_history(session_id, "hijacked message", "hijacked response", user_id)

    history = await patched_gateway.get_session_history(session_id)
    assert history == [], "the message must not have been written"


@pytest.mark.asyncio
async def test_load_history_rejects_other_users_session(patched_gateway, user_id, session_id):
    """History must not leak across users."""
    from src.memory.conversation import async_load_history

    await patched_gateway.create_session(session_id, str(uuid.uuid4()), "someone else's", None)
    await patched_gateway.save_message(session_id, "user", "secret")

    history = await async_load_history(session_id, user_id)

    assert history == []


@pytest.mark.asyncio
async def test_load_history_applies_token_budget_truncation(
    patched_gateway, monkeypatch, user_id, session_id
):
    """
    Regression (Phase 8, Bug 3): _truncate_to_token_budget existed but was
    never wired into the load path, so history grew without bound. This test
    goes through the real assembly path (async_load_history) — not the pure
    function — so it fails if the truncation call is ever disconnected again.
    """
    from src import config
    from src.memory.conversation import async_load_history

    monkeypatch.setattr(config, "MAX_HISTORY_TOKENS", 500)  # ~2000 chars

    await patched_gateway.create_session(session_id, user_id, "long chat", None)
    # 15 turns of ~1200 chars each — far exceeds a 500-token (~2000-char) budget.
    for i in range(15):
        await patched_gateway.save_message(session_id, "user", f"question {i} " + "x" * 1200)
        await patched_gateway.save_message(session_id, "assistant", f"answer {i} " + "y" * 1200)

    history = await async_load_history(session_id, user_id)

    # Truncation must have dropped the oldest turns on load (all 30 messages
    # were saved; a wired budget collapses them toward the newest pair).
    assert len(history) < 30, "history was not truncated on load — budget not applied"
    # The newest turn must always survive (drops come from the oldest end).
    assert history[-1]["content"].startswith("answer 14"), "the newest turn must survive"
    # Each message here alone exceeds the budget, so the documented floor
    # (never drop below the last user+assistant pair) means it collapses to
    # exactly that pair — proving the loop ran to completion on load.
    assert len(history) == 2, "expected collapse to the minimum surviving pair"


class _FakeDb:
    """Minimal async-session double for exercising PgPipelineLogger."""

    def __init__(self, existing=None):
        self.existing = existing
        self.added = []
        self.committed = False

    async def execute(self, _stmt):
        existing = self.existing

        class _Result:
            def scalars(self):
                class _Scalars:
                    def first(self_inner):
                        return existing
                return _Scalars()
        return _Result()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_pipeline_logger_creates_session_with_owner(monkeypatch, user_id, session_id):
    """
    Regression (the original root cause): the pipeline logger ran FIRST and
    inserted Session(session_id=...) with no user_id. save_history then saw a
    row already existed and never set the owner — so user_id stayed NULL forever.
    """
    from contextlib import asynccontextmanager

    db = _FakeDb(existing=None)

    @asynccontextmanager
    async def _fake_session():
        yield db

    monkeypatch.setattr("src.database.postgres.get_session", _fake_session)

    await PgPipelineLogger().upsert_session(session_id, user_id=user_id)

    assert db.added, "no session row was created"
    assert db.added[0].user_id == uuid.UUID(user_id), "session created without an owner"


@pytest.mark.asyncio
async def test_pipeline_logger_backfills_legacy_null_owner(monkeypatch, user_id, session_id):
    """Legacy ownerless sessions self-heal on the next message."""
    from contextlib import asynccontextmanager

    class _LegacySession:
        def __init__(self):
            self.session_id = uuid.UUID(session_id)
            self.user_id = None
            self.updated_at = None

    legacy = _LegacySession()
    db = _FakeDb(existing=legacy)

    @asynccontextmanager
    async def _fake_session():
        yield db

    monkeypatch.setattr("src.database.postgres.get_session", _fake_session)

    await PgPipelineLogger().upsert_session(session_id, user_id=user_id)

    assert legacy.user_id == uuid.UUID(user_id), "legacy NULL owner was not backfilled"


# ── Gateway protocol coverage ───────────────────────────────────────────────────

def test_direct_gateway_implements_full_protocol():
    """
    DirectGateway is the only backend this repo has. A method declared on the
    DataGateway protocol but missing here is a crash waiting to happen.
    """
    required = [
        name for name in vars(DataGateway)
        if not name.startswith("_") and callable(getattr(DataGateway, name, None))
    ]

    missing = [name for name in required if not hasattr(DirectGateway, name)]

    assert not missing, f"DirectGateway is missing: {missing}"


# ── Direct-backend SQL correctness (regressions found during live IPv6 testing) ──
# These guard bugs the fake-gateway suite can't see, since it never touches SQL.
# They exercise pure/static logic only — no live DB — so they stay network-free.

def test_direct_naive_utc_strips_timezone():
    """
    Regression: pipeline_runs / pipeline_steps / error_logs are
    `timestamp without time zone`. Binding a tz-aware datetime raised
    "can't subtract offset-naive and offset-aware datetimes" under asyncpg,
    breaking every dashboard latency/usage/error query on the direct backend.
    """
    from src.data_gateway.direct_backend import DirectGateway

    dt = DirectGateway._naive_utc("2026-06-15T07:57:11.104980+00:00")
    assert dt.tzinfo is None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 6, 15, 7)

    # A "Z" suffix must also parse to a naive datetime.
    dt2 = DirectGateway._naive_utc("2026-06-15T07:57:11Z")
    assert dt2.tzinfo is None


def test_direct_mcp_calls_uses_real_columns():
    """
    Regression: get_mcp_calls referenced started_at / completed_at /
    error_message, none of which exist on mcp_tool_calls — it raised on every
    call. It must use created_at / duration_ms / output_summary.
    """
    import inspect
    from src.data_gateway.direct_backend import DirectGateway

    src = inspect.getsource(DirectGateway.get_mcp_calls)
    # Check attribute ACCESS, not comment text (the comment names the old columns).
    assert "McpToolCall.created_at" in src
    assert "c.started_at" not in src
    assert "c.completed_at" not in src
    assert "c.error_message" not in src
