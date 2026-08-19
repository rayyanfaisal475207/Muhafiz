# ============================================================
# One-shot: pull a full snapshot of the Muhafiz Data API to disk.
#
# Used to (re)generate the fixture tests/fixtures/muhafiz_api_snapshot.json
# that the offline test suite replays against (see snapshot.py's module
# docstring for why a live client can't be exercised directly in tests).
# Also usable standalone by an operator who wants a point-in-time copy
# before running scripts/sync_muhafiz_data.py (M9).
#
# Usage:
#   python scripts/fetch_muhafiz_snapshot.py [output_path]
# ============================================================

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_gateway.muhafiz_api.snapshot import dump_snapshot, fetch_snapshot

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "muhafiz_api_snapshot.json"


async def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    snapshot = await fetch_snapshot()
    dump_snapshot(snapshot, output)
    for endpoint, records in snapshot["endpoints"].items():
        print(f"{endpoint}: {len(records)} records")
    print(f"Written to {output}")


if __name__ == "__main__":
    asyncio.run(main())
