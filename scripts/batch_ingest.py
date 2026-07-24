import asyncio
import logging
import os
import sys
from pathlib import Path

# Add project root to python path
sys.path.append(str(Path(__file__).parent.parent))

from src import config
from src.ingestion.service import ingest_file
from sqlalchemy import select
from src.database.postgres import AsyncSessionLocal
from src.database.models import Document

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def run_batch_ingest():
    dir_path = Path("data")
        
    all_pdfs = list(dir_path.rglob("*.pdf"))
    logger.info(f"Found {len(all_pdfs)} total PDFs on disk.")
    
    # 1. Fetch already ingested filenames from PostgreSQL to avoid duplicates
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Document.filename))
            existing_filenames = set(result.scalars().all())
    except Exception as e:
        logger.error(f"Failed to query database for existing documents: {e}")
        return
        
    logger.info(f"Found {len(existing_filenames)} documents already recorded in PostgreSQL.")
    
    # 2. Filter out already processed files
    to_process = [f for f in all_pdfs if f.name not in existing_filenames]
    
    # Sort by filename descending to process newest (e.g. 2026, 2025) documents first
    to_process.sort(key=lambda f: f.name, reverse=True)
    
    logger.info(f"Total PDFs remaining to process: {len(to_process)}")
    
    if not to_process:
        logger.info("Everything is up to date!")
        return
    
    # 3. Process each file
    success_count = 0
    fail_count = 0
    total_chunks = 0
    
    API_KEYS = [
        config.GEMINI_API_KEY, # Original key from env
        "REDACTED",
        "REDACTED",
        "REDACTED"
    ]
    current_key_idx = 0
    
    idx = 0
    while idx < len(to_process):
        file_path = to_process[idx]
        logger.info(f"\n--- [{idx+1}/{len(to_process)}] Processing {file_path.name} ---")
        try:
            stats = await ingest_file(file_path)
            
            if stats.get("chunks_added", 0) > 0:
                success_count += 1
                total_chunks += stats["chunks_added"]
                logger.info(f"✅ Success: {file_path.name} -> {stats['chunks_added']} chunks.")
            else:
                fail_count += 1
                logger.warning(f"⚠️ Warning: {file_path.name} processed but yielded 0 chunks. Error: {stats.get('error')}")
            
            idx += 1 # Only advance if not retrying due to quota
                
        except Exception as e:
            err_str = str(e).lower()
            if "limit: 20" in err_str or "quota" in err_str or "429" in err_str:
                logger.warning(f"🚨 API Quota Exhausted on key {current_key_idx+1}/{len(API_KEYS)}!")
                current_key_idx += 1
                if current_key_idx < len(API_KEYS):
                    logger.info(f"🔄 Swapping to backup API key {current_key_idx+1} and retrying {file_path.name}...")
                    new_key = API_KEYS[current_key_idx]
                    config.GEMINI_API_KEY = new_key
                    os.environ["GEMINI_API_KEY"] = new_key
                    # Do not advance idx, loop will retry same file
                else:
                    logger.error("🚨 All backup API keys exhausted! Aborting entire batch run.")
                    break
            else:
                fail_count += 1
                logger.error(f"❌ Critical failure on {file_path.name}: {e}")
                idx += 1 # Advance on non-quota errors

if __name__ == "__main__":
    asyncio.run(run_batch_ingest())
