"""
Apply a .sql migration to the configured Postgres database.

    python scripts/apply_migration.py migrations/003_admin_dashboard_and_attachments.sql

Requires a reachable Postgres connection (DATABASE_URL) — local/self-hosted only.

Uses a raw asyncpg connection rather than SQLAlchemy: asyncpg runs everything
through prepared statements, which reject multi-statement scripts
("cannot insert multiple commands into a prepared statement"). The raw
connection's .execute() runs a whole DDL script in one call.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> None:
    # Force UTF-8 output so a non-ASCII char in an error message can't crash
    # the reporting on a cp1252 Windows console.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    sql_path = Path(sys.argv[1])
    if not sql_path.exists():
        print(f"ERROR: no such file: {sql_path}")
        sys.exit(1)

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    raw_url = os.getenv("DATABASE_URL")
    if not raw_url:
        print("ERROR: DATABASE_URL is not configured.")
        sys.exit(1)
    # asyncpg wants a plain postgresql:// URL, not the SQLAlchemy +asyncpg form.
    raw_url = raw_url.replace("postgresql+asyncpg://", "postgresql://")

    import asyncpg

    sql = sql_path.read_text(encoding="utf-8")
    print(f"Applying {sql_path.name} ...")

    conn = None
    try:
        conn = await asyncio.wait_for(asyncpg.connect(raw_url), timeout=30)
        # A DDL script is idempotent here (CREATE ... IF NOT EXISTS / OR REPLACE),
        # so a plain execute of the whole file is safe and atomic enough.
        await conn.execute(sql)
        print("OK — migration applied.")
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        print(
            "\nIf this is a connection error, confirm Postgres is running "
            "(docker compose up -d) and DATABASE_URL in .env points at it."
        )
        sys.exit(1)
    finally:
        if conn is not None:
            await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
