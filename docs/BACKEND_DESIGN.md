# Backend Design

This document details the backend architecture of the RAG Chatbot, specifically focusing on the data access layer and the `DataGateway` abstraction.

## The DataGateway Abstraction

The backend accesses PostgreSQL through a `DataGateway` protocol (`src/data_gateway/base.py`) rather than calling SQLAlchemy directly from routes and pipeline code. `DirectGateway` (`src/data_gateway/direct_backend.py`) is the only implementation — async SQLAlchemy + asyncpg, straight to a local/self-hosted Postgres instance.

### Why the Gateway Exists

Even with a single backend, the abstraction is worth keeping: it isolates business logic from the storage mechanism, so routes and pipeline stages call `gateway.get_user(...)`, `gateway.query_police_reference_data(...)`, etc. without knowing anything about SQLAlchemy sessions or connection pooling. That keeps the door open to a different backend later without touching call sites.

### Dependency Injection (`get_gateway()`)

The `DataGateway` instance is provided to FastAPI routes and pipeline components via dependency injection.

```python
# Usage Example
from src.data_gateway import get_gateway

async def process_data():
    gateway = await get_gateway()
    user_data = await gateway.get_user(user_id)
```

**How it works**:
- `get_gateway()` (`src/data_gateway/selector.py`) returns a process-wide singleton `DirectGateway`, constructed on first call.
- `DirectGateway` implements every operation on the `DataGateway` protocol (`get_user`, `get_session_history`, `query_police_reference_data`, etc.) via async SQLAlchemy against `DATABASE_URL`. Vector retrieval goes through `src/retrieval/vector_store.py` (ChromaDB) instead of the gateway.
