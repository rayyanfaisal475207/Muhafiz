import os
from src import config

# mcp-server-pg (node) uses standard postgres:// URLs, not SQLAlchemy's
# postgresql+asyncpg:// — strip the +asyncpg driver suffix, otherwise reuse
# the same local Postgres credentials the rest of the app connects with.
# No separate readonly role: this is a local/self-hosted dev Postgres, not a
# shared prod instance, so the least-privilege split isn't provisioned here.
READONLY_DATABASE_URL = config.DATABASE_URL.replace("+asyncpg", "") if config.DATABASE_URL else config.DATABASE_URL
from urllib.parse import urlparse

_parsed = urlparse(READONLY_DATABASE_URL)
DB_MAIN_USER = _parsed.username
DB_MAIN_PASSWORD = _parsed.password
DB_MAIN_HOST = _parsed.hostname
DB_MAIN_PORT = str(_parsed.port) if _parsed.port else "5432"
DB_MAIN_NAME = _parsed.path.lstrip('/')

# We'll spawn the local custom server.js
MCP_SERVER_SCRIPT = os.path.join(
    config._PROJECT_ROOT, "src", "mcp", "server.js"
)
