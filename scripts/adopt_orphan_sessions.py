"""
One-time repair: assign sessions with user_id = NULL to a user.

Older builds created session rows without an owner (via the pipeline
logger), which makes them invisible in the sidebar and 403 on reopen.
New code no longer creates ownerless sessions; this script adopts the
legacy ones so their history becomes reachable again.

Usage:
    python scripts/adopt_orphan_sessions.py --email you@example.com [--dry-run]

Only safe on a single-user (or effectively single-user) database, since
NULL rows carry no signal about who owned them.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Email of the user who should adopt the orphan sessions")
    parser.add_argument("--dry-run", action="store_true", help="Only report, don't write")
    args = parser.parse_args()

    from src.data_gateway import get_gateway
    gateway = await get_gateway()

    user = await gateway.get_user_by_email(args.email)
    if not user:
        print(f"ERROR: no user found with email {args.email}")
        sys.exit(1)
    user_id = str(user["id"])
    print(f"Adopting orphan sessions for {args.email} ({user_id})")

    backend = type(gateway).__name__
    if backend == "RestGateway":
        res = await asyncio.to_thread(
            gateway.client.table("sessions").select("session_id,title").is_("user_id", "null").execute
        )
        orphans = res.data or []
        print(f"Found {len(orphans)} orphan session(s).")
        for s in orphans:
            print(f"  - {s['session_id']}  {s.get('title', '')!r}")
        if orphans and not args.dry_run:
            await asyncio.to_thread(
                gateway.client.table("sessions").update({"user_id": user_id}).is_("user_id", "null").execute
            )
            print("Updated.")
    else:
        import uuid
        from sqlalchemy import select
        from src.database.postgres import get_session
        from src.database.models import Session as SessionModel

        async with get_session() as db:
            res = await db.execute(select(SessionModel).where(SessionModel.user_id == None))  # noqa: E711
            orphans = res.scalars().all()
            print(f"Found {len(orphans)} orphan session(s).")
            for s in orphans:
                print(f"  - {s.session_id}  {s.title!r}")
            if orphans and not args.dry_run:
                for s in orphans:
                    s.user_id = uuid.UUID(user_id)
                await db.commit()
                print("Updated.")

    if args.dry_run:
        print("(dry run — nothing written)")


if __name__ == "__main__":
    asyncio.run(main())
