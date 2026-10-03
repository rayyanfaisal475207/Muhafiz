"""An unset deadline means unbounded, on the HTTP path as much as anywhere.

Every other test in this suite drives the runtime directly. The web UI does
not: it goes through `src/evidence/service.py`, and that path had its own
arithmetic on the deadline. When "unset means unbounded" made None the
default, the first question asked through the browser returned HTTP 500 --

    unsupported operand type(s) for -: 'NoneType' and 'float'

-- while every command-line run kept passing. These pin the service path so
the two cannot drift apart again.
"""
import asyncio
import time

import pytest

from src.evidence import service


def test_an_unset_deadline_is_unbounded(monkeypatch):
    """0 or unset is no limit -- not a limit of zero, and not a default of 90."""
    monkeypatch.delenv("EVIDENCE_RUN_DEADLINE_SECONDS", raising=False)
    assert service._run_deadline() is None

    monkeypatch.setenv("EVIDENCE_RUN_DEADLINE_SECONDS", "0")
    assert service._run_deadline() is None


def test_a_configured_deadline_is_honoured_as_given(monkeypatch):
    """A positive value is used as written, and is NOT clamped.

    It used to be passed through `min(seconds, 90)`, so the setting could be
    read but never raised.
    """
    monkeypatch.setenv("EVIDENCE_RUN_DEADLINE_SECONDS", "600")
    remaining = service._run_deadline() - time.monotonic()
    assert 590 < remaining <= 600, "a configured deadline must not be clamped"


@pytest.mark.asyncio
async def test_the_service_path_accepts_no_deadline():
    """The guard the browser hits must cope with None, because None is normal."""
    async with service._until(None):
        await asyncio.sleep(0)

    async with service._until(time.monotonic() + 5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_an_expired_deadline_still_raises_rather_than_hanging():
    """Unbounded is a choice; an EXPIRED bound is still a bound."""
    with pytest.raises(TimeoutError):
        async with service._until(time.monotonic() - 1):
            await asyncio.sleep(0.05)
