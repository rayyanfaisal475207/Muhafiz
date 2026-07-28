"""
Manual verification for migrations/015_app_least_privilege_role.sql.

Requires a live Postgres connection.

Confirms muhafiz_app (the application's intended runtime role, replacing
the superuser DATABASE_URL) can do everything the application legitimately
needs, is NOT superuser/BYPASSRLS, and that RLS policies actually take
effect under this role -- unlike under the superuser connection, where
they were always silently inert regardless of correctness.

    python scripts/verify_app_role.py

Reads APP_ROLE_DATABASE_URL from the environment (or falls back to prompting
via DATABASE_URL if it already points at muhafiz_app). Run this only after:
  1. applying migrations/015_app_least_privilege_role.sql,
  2. setting a real password (ALTER ROLE muhafiz_app WITH PASSWORD ...),
  3. setting DATABASE_URL (or APP_ROLE_DATABASE_URL for this check alone)
     to a connection string using that role.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_APP_TABLES = (
    "users", "user_context_profiles", "projects", "cases", "project_memory",
    "sessions", "messages", "pipeline_runs", "pipeline_steps", "documents",
    "police_reference_data", "mcp_tool_calls", "generated_files",
    "error_logs", "ingestion_jobs", "session_attachments",
    "case_assignments", "audit_logs",
)


async def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    raw_url = os.getenv("APP_ROLE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not raw_url:
        print("ERROR: set APP_ROLE_DATABASE_URL (or DATABASE_URL) to a muhafiz_app connection string.")
        sys.exit(1)
    raw_url = raw_url.replace("postgresql+asyncpg://", "postgresql://").split("?")[0]

    import asyncpg

    conn = None
    failures = []
    try:
        conn = await asyncio.wait_for(asyncpg.connect(raw_url), timeout=30)

        row = await conn.fetchrow(
            "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        print(f"Connected as: {row['current_user']}")
        if row["rolsuper"]:
            failures.append(f"{row['current_user']} is a SUPERUSER -- the exact problem this migration fixes.")
        else:
            print("OK   — role is not superuser.")
        if row["rolbypassrls"]:
            failures.append(f"{row['current_user']} has BYPASSRLS -- RLS policies remain inert under this role.")
        else:
            print("OK   — role does not have BYPASSRLS.")

        # Must succeed: every table the application's models/gateway touch.
        for table in _APP_TABLES:
            try:
                await conn.fetch(f"SELECT * FROM {table} LIMIT 1")
                print(f"OK   — SELECT on {table} succeeded.")
            except Exception as exc:
                failures.append(f"SELECT on {table} failed (should have succeeded): {exc}")

        # Must succeed: a real INSERT/UPDATE/DELETE round-trip on a
        # low-consequence table, confirming DML grants (not just SELECT).
        probe_email = f"verify-app-role-{uuid.uuid4()}@example.invalid"
        try:
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, role, plan) VALUES ($1, $2, 'x', 'investigator', 'free')",
                uuid.uuid4(), probe_email,
            )
            await conn.execute("UPDATE users SET role = 'supervisor' WHERE email = $1", probe_email)
            await conn.execute("DELETE FROM users WHERE email = $1", probe_email)
            print("OK   — INSERT/UPDATE/DELETE round-trip on users succeeded.")
        except Exception as exc:
            failures.append(f"INSERT/UPDATE/DELETE round-trip on users failed: {exc}")

        # RLS must actually apply now: with app.rls_active/app.case_id set,
        # a case-scoped row must be invisible when case_id doesn't match.
        # Uses a transaction so SET LOCAL and the probe row are scoped
        # together, then rolls back -- no real data is left behind.
        tx = conn.transaction()
        await tx.start()
        try:
            probe_case_a = f"VERIFY-A-{uuid.uuid4().hex[:8]}"
            probe_case_b = f"VERIFY-B-{uuid.uuid4().hex[:8]}"
            await conn.execute(
                "INSERT INTO cases (case_id) VALUES ($1), ($2)",
                probe_case_a, probe_case_b,
            )
            await conn.execute("SET LOCAL app.rls_active = 'true'")
            await conn.execute("SELECT set_config('app.case_id', $1, true)", probe_case_a)

            visible = await conn.fetch(
                "SELECT case_id FROM cases WHERE case_id IN ($1, $2)", probe_case_a, probe_case_b
            )
            visible_ids = {r["case_id"] for r in visible}
            if visible_ids == {probe_case_a}:
                print("OK   — RLS actually enforced: only the active case's row is visible.")
            else:
                failures.append(
                    f"RLS did not restrict visibility as expected: saw {visible_ids}, "
                    f"expected only {{{probe_case_a}}}. RLS may still be inert under this role."
                )
        except Exception as exc:
            failures.append(f"RLS enforcement probe failed to run: {exc}")
        finally:
            await tx.rollback()

        # Must be denied: this role has no reason to ever touch Postgres's
        # own catalogs/roles, or DROP/ALTER existing tables.
        try:
            await conn.execute("DROP TABLE IF EXISTS users")
            failures.append("DROP TABLE on users succeeded — muhafiz_app must not own/be able to drop tables.")
        except Exception as exc:
            msg = str(exc).lower()
            if "permission denied" in msg or "must be owner" in msg:
                print("OK   — DROP TABLE on users correctly denied.")
            else:
                failures.append(f"DROP TABLE check failed for an unexpected reason: {exc}")

        try:
            await conn.execute("CREATE ROLE verify_app_role_probe")
            await conn.execute("DROP ROLE verify_app_role_probe")
            failures.append("CREATE ROLE succeeded — muhafiz_app must not have role-management privileges.")
        except Exception as exc:
            if "permission denied" in str(exc).lower():
                print("OK   — CREATE ROLE correctly denied.")
            else:
                failures.append(f"CREATE ROLE check failed for an unexpected reason: {exc}")

    finally:
        if conn is not None:
            await conn.close()

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("\nAll checks passed — muhafiz_app is correctly least-privilege and RLS is actually enforced.")


if __name__ == "__main__":
    asyncio.run(main())
