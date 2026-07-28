"""
Phase 7, Module 7.1 — blocking vision-OCR retry loop.

`ingest_file` used to call `route_and_load` directly on the event loop.
For a scanned PDF that falls back to vision and hits rate limits, that
chain includes a blocking `time.sleep(120)` retried up to 10 times inside
`pdf_loader._load_scanned_page_with_vision` — with no thread offload, that
freezes the entire event loop (every other request) for the duration.

This test doesn't need real PDFs or vision calls: it monkeypatches
`route_and_load` itself with a plain blocking `time.sleep`, which is
enough to prove whether the call is on the event loop or off it. If the
fix (`asyncio.to_thread`) is reverted, this test fails/times out because
the concurrent heartbeat task starves during the sleep.
"""
import asyncio
import time

import pytest

from src.ingestion import service


def _blocking_loader(file_path):
    time.sleep(0.4)
    return []


async def test_ingest_file_load_step_does_not_block_the_event_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "route_and_load", _blocking_loader)

    fake_file = tmp_path / "scan.pdf"
    fake_file.write_bytes(b"%PDF-fake")

    heartbeats = []

    async def heartbeat():
        while True:
            heartbeats.append(time.monotonic())
            await asyncio.sleep(0.05)

    hb_task = asyncio.create_task(heartbeat())
    try:
        # route_and_load returns [] here, so ingest_file exits early via its
        # "no documents extracted" branch right after the load step — that's
        # fine, the load step is the only thing under test.
        result = await service.ingest_file(fake_file)
    finally:
        hb_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await hb_task

    assert result["chunks_added"] == 0

    # A blocked event loop would starve the heartbeat entirely during the
    # 0.4s sleep, leaving ~1 tick. Off-thread, the loop keeps ticking every
    # ~0.05s throughout, so several heartbeats land during that window.
    assert len(heartbeats) >= 5, (
        f"only {len(heartbeats)} heartbeats recorded — event loop was blocked "
        "during the load step (asyncio.to_thread offload missing or reverted)"
    )


async def test_route_and_load_is_offloaded_via_to_thread(monkeypatch, tmp_path):
    """Narrower unit check: ingest_file's load step must go through
    asyncio.to_thread, not call route_and_load directly on the loop."""
    calls = []

    async def fake_to_thread(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    monkeypatch.setattr(service, "route_and_load", lambda p: [])
    monkeypatch.setattr(service.asyncio, "to_thread", fake_to_thread)

    fake_file = tmp_path / "doc.pdf"
    fake_file.write_bytes(b"%PDF-fake")
    await service.ingest_file(fake_file)

    assert any(fn is service.route_and_load for fn, args, kwargs in calls), (
        "ingest_file must call asyncio.to_thread(route_and_load, file_path)"
    )
