import asyncio
import httpx
from src.main import app
from src.auth.jwt import create_access_token

async def test_auth():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        print("Testing unauthenticated...")
        r1 = await client.post("/ingest", json={"files": []})
        r2 = await client.get("/documents")
        r3 = await client.delete("/documents/123")
        print("Unauthenticated ingest:", r1.status_code)
        print("Unauthenticated get:", r2.status_code)
        print("Unauthenticated delete:", r3.status_code)

        print("\nTesting non-admin...")
        token_non_admin = create_access_token({"sub": "00000000-0000-0000-0000-000000000123"})
        client.cookies.set("access_token", token_non_admin)
        client.cookies.set("csrf_token", "dummy")
        client.headers.update({"x-csrf-token": "dummy"})
        
        r1 = await client.post("/ingest", json={"files": []})
        r2 = await client.get("/documents")
        r3 = await client.delete("/documents/123")
        print("Non-admin ingest:", r1.status_code)
        print("Non-admin get:", r2.status_code)
        print("Non-admin delete:", r3.status_code)
        
        print("\nTesting admin...")
        # Since is_admin depends on the database, we need a real user ID.
        from src.data_gateway import get_gateway
        gateway = await get_gateway()
        admin = await gateway.get_user_by_email("admin@taxiq.pk")
        if admin:
            token_admin = create_access_token({"sub": str(admin["id"])})
            client.cookies.set("access_token", token_admin)
            r1 = await client.post("/ingest", json={"files": []})
            r2 = await client.get("/documents")
            r3 = await client.delete("/documents/123")
            print("Admin ingest:", r1.status_code)
            print("Admin get:", r2.status_code)
            print("Admin delete:", r3.status_code)
        else:
            print("Admin user not found!")

if __name__ == "__main__":
    asyncio.run(test_auth())
