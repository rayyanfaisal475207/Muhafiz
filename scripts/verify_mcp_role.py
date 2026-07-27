"""
Manual verification for migrations/009_mcp_readonly_role.sql.

Requires a live Postgres connection and cannot be run in an environment
without one (there is none in the environment this was written in).

Confirms the muhafiz_mcp_readonly role can SELECT from police_reference_data
and is denied access to sensitive tables it must never be able to read.

    python scripts/verify_mcp_role.py

Reads MCP_DATABASE_URL from .env (the same variable src/mcp/client.py uses).
Run this only after:
  1. applying migrations/009_mcp_readonly_role.sql,
  2. setting a real password (ALTER ROLE muhafiz_mcp_readonly WITH PASSWORD ...),
  3. setting MCP_DATABASE_URL in .env to a connection string using that role.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    raw_url = os.getenv("MCP_DATABASE_URL")
    if not raw_url:
        print("ERROR: MCP_DATABASE_URL is not configured. Set it before running this check.")
        sys.exit(1)
    raw_url = raw_url.replace("postgresql+asyncpg://", "postgresql://")

    import asyncpg

    conn = None
    failures = []
    try:
        conn = await asyncio.wait_for(asyncpg.connect(raw_url), timeout=30)

        # Must succeed: this is the one table the MCP route is meant to read.
        try:
            await conn.fetch("SELECT * FROM police_reference_data LIMIT 1")
            print("OK   — SELECT on police_reference_data succeeded.")
        except Exception as exc:
            failures.append(f"SELECT on police_reference_data failed (should have succeeded): {exc}")

        # Must be denied: tables with credentials/PII/audit trail.
        for table in ("users", "audit_logs", "cases", "sessions", "messages"):
            try:
                await conn.fetch(f"SELECT * FROM {table} LIMIT 1")
                failures.append(f"SELECT on {table} succeeded — it must be denied for muhafiz_mcp_readonly.")
            except Exception as exc:
                if "permission denied" in str(exc).lower():
                    print(f"OK   — SELECT on {table} correctly denied.")
                else:
                    failures.append(f"SELECT on {table} failed for an unexpected reason: {exc}")

        # Must be denied: this role should never be able to write anywhere.
        try:
            await conn.execute("INSERT INTO police_reference_data (category) VALUES ('probe')")
            failures.append("INSERT on police_reference_data succeeded — role must be read-only.")
        except Exception as exc:
            if "permission denied" in str(exc).lower():
                print("OK   — INSERT on police_reference_data correctly denied.")
            else:
                failures.append(f"INSERT check failed for an unexpected reason: {exc}")

    finally:
        if conn is not None:
            await conn.close()

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("\nAll checks passed — muhafiz_mcp_readonly is correctly least-privilege.")


if __name__ == "__main__":
    asyncio.run(main())
