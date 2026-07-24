import asyncpg, os, asyncio
from dotenv import load_dotenv

async def run():
    load_dotenv()
    conn = await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://'))
    rows = await conn.fetch('SELECT email, is_admin FROM users;')
    for row in rows:
        print(f"{row['email']}: is_admin={row['is_admin']}")
    await conn.close()

asyncio.run(run())
