import logging

# Importing config guarantees .env is loaded before anything else in this
# module runs.
from src import config  # noqa: F401

logger = logging.getLogger(__name__)

from src.data_gateway.base import DataGateway

_gateway_instance = None


async def get_gateway() -> DataGateway:
    """
    Returns the DataGateway implementation: direct PostgreSQL via async
    SQLAlchemy + asyncpg. This repo has one backend, one connection path —
    no Supabase REST fallback, no mode probing.
    """
    global _gateway_instance
    if _gateway_instance is not None:
        return _gateway_instance

    from src.data_gateway.direct_backend import DirectGateway
    _gateway_instance = DirectGateway()
    return _gateway_instance
