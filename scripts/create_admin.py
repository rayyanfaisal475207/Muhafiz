import asyncio
import os
import sys

# Add the project root to PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data_gateway import get_gateway
from src.auth.jwt import get_password_hash

async def main():
    gateway = await get_gateway()
    email = "admin@example.com"
    password = "MuhafizAdmin2026!"
    
    print("Checking for existing user...")
    user = await gateway.get_user_by_email(email)
    
    if not user:
        print(f"Creating admin user {email}...")
        user_data = {
            "email": email,
            "password_hash": get_password_hash(password),
            "company_name": "Muhafiz Admin",
        }
        new_user = await gateway.create_user(user_data)
        print("User created.")
    else:
        print(f"User {email} already exists.")
        
    print("Promoting user to platform-admin...")
    # `is_admin` was removed by the RBAC migration (migrations/006_rbac.sql)
    # in favor of a `role` enum column (investigator/supervisor/
    # station-admin/platform-admin) — this script still referenced the old
    # column and raised `CompileError: Unconsumed column names: is_admin` on
    # every run since. Update the real column instead.
    from src.database.postgres import get_session
    from src.database.models import User
    from sqlalchemy import update

    async with get_session() as db:
        await db.execute(update(User).where(User.email == email).values(role="platform-admin"))
        await db.commit()
        
    print("--------------------------------------------------")
    print("Success! You can now log into the admin dashboard.")
    print(f"Email: {email}")
    print(f"Password: {password}")
    print("--------------------------------------------------")

if __name__ == "__main__":
    asyncio.run(main())
