import logging
import json
import os
from typing import Dict, Any, List
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.config import DATABASE_URL

logger = logging.getLogger(__name__)

async def execute_query(statement: str, params: list = None) -> List[Dict[str, Any]]:
    """
    Execute a SQL query against the database using the official MCP postgres server via stdio.
    
    This function spawns the `npx @modelcontextprotocol/server-postgres` process,
    connects to it over stdin/stdout, initializes the MCP session, and calls the `query` tool.
    
    Note: If the network blocks outbound TCP 5432 (e.g., WinError 64 / ECONNRESET), 
    the npx process will crash/fail to connect to the database when initializing or executing.
    """
    
    # We must format the database URL for the Node process.
    # If using SQLAlchemy's asyncpg URL (postgresql+asyncpg://), strip the "+asyncpg" part
    # so the Node pg driver recognizes it.
    node_db_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    
    logger.info(f"Spawning MCP Postgres server via npx...")
    
    # Define the stdio parameters to run the npx command
    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-postgres", node_db_url],
        env=os.environ.copy() # Pass environment variables so npx can be found
    )
    
    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                logger.info("Initializing MCP Session...")
                await session.initialize()
                
                tool_args = {"sql": statement}
                logger.info(f"Executing MCP SQL Tool: {statement}")
                
                # The official postgres MCP server exposes a tool named "query"
                result = await session.call_tool("query", arguments=tool_args)
                
                if hasattr(result, "isError") and result.isError:
                    error_msg = result.content[0].text if result.content else "Unknown error"
                    logger.error(f"MCP SQL Error: {error_msg}")
                    raise Exception(f"MCP SQL Error: {error_msg}")
                    
                if not result.content:
                    return []
                    
                try:
                    data = json.loads(result.content[0].text)
                    return data
                except json.JSONDecodeError:
                    raise Exception(f"Failed to parse MCP query result as JSON: {result.content[0].text}")
                    
    except Exception as e:
        logger.error(f"MCP Stdio Process Failed: {str(e)}")
        raise e
