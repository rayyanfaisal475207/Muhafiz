import asyncio
from src.ingestion.service import ingest_directory

async def main():
    print("Starting ingestion...")
    result = await ingest_directory()
    print("Ingestion result:", result)

if __name__ == "__main__":
    asyncio.run(main())
