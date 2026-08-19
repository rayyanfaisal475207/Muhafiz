# ============================================================
# Muhafiz Data API — offline snapshot
#
# tests/conftest.py's autouse `no_network` fixture hard-blocks every socket
# and httpx call in the test suite (three layers — sync socket, async
# getaddrinfo, httpx transport; see conftest.py's own comments on why one
# layer alone wasn't enough). A live-API client is therefore untestable
# in-suite unless something records real responses to disk once, outside
# the test run, for fixtures to replay. That's what this module is for:
# a snapshot is a plain JSON file, {endpoint: [raw record dicts]}, taken by
# calling fetch_snapshot() against the real API (e.g. from a script, not a
# test), then loaded back with load_snapshot() by both tests and — should
# an operator want a dry run without hitting the network — M9's
# scripts/sync_muhafiz_data.py --dry-run.
# ============================================================

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.data_gateway.muhafiz_api.client import ENDPOINTS, MuhafizApiClient

logger = logging.getLogger(__name__)


async def fetch_snapshot(
    client: Optional[MuhafizApiClient] = None,
    endpoints: tuple[str, ...] = ENDPOINTS,
) -> dict:
    """
    Pulls every record from every given endpoint (full fetch, no
    updated_since — a snapshot is meant to be a complete point-in-time
    copy). Returns {"fetched_at": iso8601, "endpoints": {name: [records]}}.
    """
    owns_client = client is None
    client = client or MuhafizApiClient()
    try:
        result: dict[str, list[dict]] = {}
        for endpoint in endpoints:
            records = await client.fetch_all(endpoint)
            result[endpoint] = records
            logger.info("muhafiz_api snapshot: %s — %d records", endpoint, len(records))
        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "endpoints": result,
        }
    finally:
        if owns_client:
            await client.aclose()


def dump_snapshot(snapshot: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("muhafiz_api snapshot written: %s", path)


def load_snapshot(path: Path) -> dict:
    """
    Raises FileNotFoundError if the snapshot doesn't exist yet — callers
    (tests, --dry-run) should treat that as "run fetch_snapshot()+
    dump_snapshot() once first," not silently fall back to empty data.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def records_for(snapshot: dict, endpoint: str) -> list[dict]:
    return snapshot.get("endpoints", {}).get(endpoint, [])
