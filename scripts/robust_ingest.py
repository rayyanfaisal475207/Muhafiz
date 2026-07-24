import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Add root to pythonpath
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.ingestion.service import ingest_file

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

STATE_FILE = Path("ingestion_state.json")

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

async def main():
    parser = argparse.ArgumentParser(description="Robust batch ingestion")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of documents to process")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually ingest, just list what would be done")
    args = parser.parse_args()

    directories = [
        Path("data/raw/fbr/legislation")
    ]

    all_pdfs = []
    for directory in directories:
        if directory.exists():
            all_pdfs.extend(list(directory.rglob("*.pdf")))
        else:
            logger.warning(f"Directory {directory} does not exist!")

    logger.info(f"Found {len(all_pdfs)} total PDFs.")

    state = load_state()
    
    # Filter out already ingested
    to_process = []
    for pdf in all_pdfs:
        pdf_key = str(pdf)
            
        # Skip if successfully processed and added chunks or if it was an empty file. 
        # Only re-process if we failed previously (error key present but no chunks added).
        if pdf_key in state and state[pdf_key].get("chunks_added", 0) > 0:
            continue
        to_process.append(pdf)
        
    import re
    def extract_year(path):
        match = re.search(r'(20\d{2})', path.name)
        if match:
            return int(match.group(1))
        return 0
        
    to_process.sort(key=extract_year, reverse=True)
            
    logger.info(f"{len(to_process)} PDFs remaining to ingest.")

    if args.limit:
        to_process = to_process[:args.limit]
        logger.info(f"Limiting to {args.limit} documents.")

    if args.dry_run:
        logger.info("Dry run complete.")
        return

    max_retries = 10
    
    import fitz

    for pdf in to_process:
        try:
            doc = fitz.open(pdf)
            num_pages = len(doc)
            doc.close()
            if num_pages > 50:
                year = extract_year(pdf)
                is_legislation = "legislation" in str(pdf).lower()
                if year >= 2020 and is_legislation:
                    logger.info(f"Bypassing page limit for {pdf.name} because it is from year {year} (>=2020) and is a Legislation document.")
                else:
                    logger.warning(f"Skipping {pdf.name} because it is too large ({num_pages} pages) to save DB storage.")
                    state[str(pdf)] = {"error": f"Skipped: too large ({num_pages} pages)", "chunks_added": 0}
                    save_state(state)
                    continue
        except Exception as e:
            pass
            
        retries = 0
        success = False
        
        while retries < max_retries and not success:
            try:
                logger.info(f"Ingesting: {pdf}")
                stats = await ingest_file(pdf)
                
                if "error" in stats:
                    err_msg = stats["error"].lower()
                    if "limit: 20" in err_msg:
                        logger.error(f"Daily Gemini Free Tier Quota exhausted on {pdf.name}. Skipping retries for this file.")
                        state[str(pdf)] = stats
                        save_state(state)
                        success = True
                    elif "quota" in err_msg or "429" in err_msg or "exhausted" in err_msg:
                        logger.warning(f"Rate limit / Quota hit. Retrying in 120s... ({retries+1}/{max_retries})")
                        await asyncio.sleep(120)
                        retries += 1
                        if retries >= max_retries:
                            logger.error("Max retries reached. Stopping.")
                            return
                    else:
                        logger.error(f"Non-transient error: {stats['error']}")
                        state[str(pdf)] = stats
                        save_state(state)
                        success = True
                else:
                    state[str(pdf)] = stats
                    save_state(state)
                    success = True
                    logger.info(f"Success: {pdf.name} - {stats}")
                    
            except Exception as e:
                err_msg = str(e).lower()
                if "limit: 20" in err_msg:
                    logger.error(f"Daily Gemini Free Tier Quota exhausted on {pdf}. Skipping retries.")
                    state[str(pdf)] = {"error": str(e)}
                    save_state(state)
                    success = True
                elif "quota" in err_msg or "429" in err_msg or "exhausted" in err_msg:
                    logger.warning(f"Exception (Rate limit): {e}. Retrying in 120s...")
                    await asyncio.sleep(120)
                    retries += 1
                    if retries >= max_retries:
                        logger.error("Max retries reached. Stopping.")
                        return
                else:
                    logger.error(f"Unhandled Exception on {pdf}: {e}")
                    state[str(pdf)] = {"error": str(e)}
                    save_state(state)
                    success = True

    logger.info("Batch ingestion complete.")
    
if __name__ == "__main__":
    asyncio.run(main())
