import asyncio
import time
import aiohttp
import json
import subprocess
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Config
CONCURRENT_USERS = 5
ITERATIONS_PER_USER = 3
LLM_URL = "http://localhost:8000/v1/chat/completions"
EMBEDDING_URL = "http://localhost:8000/v1/embeddings"
MODEL_NAME = "Qwen/Qwen2.5-14B-Instruct"

# Sample Data
SAMPLE_QUERY = "What are the timeline inconsistencies in case FIR-2023-001?"
SAMPLE_DOC = "On 2023-10-12, suspect Ali was seen near the bank. On 2023-10-13, Ali was arrested in Lahore."

async def simulate_embedding(session):
    start = time.time()
    payload = {
        "model": "intfloat/multilingual-e5-large-instruct",
        "input": SAMPLE_QUERY
    }
    try:
        async with session.post(EMBEDDING_URL, json=payload, timeout=10) as resp:
            await resp.text()
            return time.time() - start
    except Exception as e:
        logger.error(f"Embedding failed: {e}")
        return None

async def simulate_generation(session):
    start = time.time()
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": f"Answer based on context: {SAMPLE_DOC}\n\nQuery: {SAMPLE_QUERY}"}
        ],
        "max_tokens": 150
    }
    try:
        async with session.post(LLM_URL, json=payload, timeout=30) as resp:
            await resp.text()
            return time.time() - start
    except Exception as e:
        logger.error(f"Generation failed: {e}")
        return None

async def simulate_verifier(session):
    start = time.time()
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": "You are a strict verifier."},
            {"role": "user", "content": f"Verify if the generated answer is grounded in the source text: {SAMPLE_DOC}"}
        ],
        "max_tokens": 50
    }
    try:
        async with session.post(LLM_URL, json=payload, timeout=20) as resp:
            await resp.text()
            return time.time() - start
    except Exception as e:
        logger.error(f"Verifier failed: {e}")
        return None

async def simulate_conflict_detection(session):
    start = time.time()
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": "You are an entity conflict detector."},
            {"role": "user", "content": f"Analyze timeline for contradictions: {SAMPLE_DOC}"}
        ],
        "max_tokens": 100
    }
    try:
        async with session.post(LLM_URL, json=payload, timeout=30) as resp:
            await resp.text()
            return time.time() - start
    except Exception as e:
        logger.error(f"Conflict detection failed: {e}")
        return None

async def user_session(user_id):
    async with aiohttp.ClientSession() as session:
        for i in range(ITERATIONS_PER_USER):
            # Simulate a full RAG request sequence
            logger.info(f"User {user_id} - Iteration {i+1}: Starting query sequence")
            
            # 1. Embedding request
            emb_lat = await simulate_embedding(session)
            
            # 2. Generation request
            gen_lat = await simulate_generation(session)
            
            # 3. Phase 6 Verifier call (happens right after generation to verify)
            ver_lat = await simulate_verifier(session)
            
            # 4. Concurrently, simulate background ingestion (Phase 8 Conflict detection)
            conf_lat = await simulate_conflict_detection(session)

            logger.info(
                f"User {user_id} - Iteration {i+1} completed. "
                f"Emb: {emb_lat:.2f}s | Gen: {gen_lat:.2f}s | Ver: {ver_lat:.2f}s | Conf: {conf_lat:.2f}s"
            )
            # Think time
            await asyncio.sleep(2)

async def monitor_vram():
    while True:
        try:
            # Requires nvidia-smi
            res = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                text=True
            )
            logger.info(f"Current VRAM Usage: {res.strip()} MB")
        except FileNotFoundError:
            # GPU not found
            pass
        except Exception as e:
            logger.warning(f"Failed to query VRAM: {e}")
        await asyncio.sleep(5)

async def main():
    logger.info(f"Starting load test with {CONCURRENT_USERS} concurrent users.")
    vram_task = asyncio.create_task(monitor_vram())
    
    tasks = [user_session(i) for i in range(CONCURRENT_USERS)]
    await asyncio.gather(*tasks)
    
    vram_task.cancel()
    logger.info("Load test completed.")

if __name__ == "__main__":
    asyncio.run(main())
