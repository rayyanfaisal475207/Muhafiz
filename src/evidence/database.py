"""Separate pools for evidence reads/control records and ingestion writes."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

import asyncpg

from .types import Scope

_pools: dict[str, asyncpg.Pool] = {}


def dsn(kind: str = "query") -> str:
    key = "EVIDENCE_QUERY_DATABASE_URL" if kind == "query" else "EVIDENCE_INGEST_DATABASE_URL"
    value = os.getenv(key, "")
    if not value:
        raise RuntimeError(f"{key} must be configured; application credentials are not a fallback")
    return value.replace("postgresql+asyncpg://", "postgresql://")


async def _init(conn):
    for name in ("json", "jsonb"):
        await conn.set_type_codec(name, encoder=lambda value:json.dumps(value,default=str), decoder=json.loads, schema="pg_catalog")


async def pool(kind: str = "query"):
    if kind not in _pools:
        instance = await asyncpg.create_pool(dsn(kind), min_size=1, max_size=4,
                                             command_timeout=20, init=_init)
        try:
            async with instance.acquire() as conn:
                access = await conn.fetchrow("""
                    SELECT rolsuper,rolbypassrls,
                      pg_has_role(current_user,'muhafiz_evidence_reader','member') AS reader,
                      pg_has_role(current_user,'muhafiz_evidence_ingestor','member') AS writer
                    FROM pg_roles WHERE rolname=current_user
                """)
                if not access or access["rolsuper"] or access["rolbypassrls"]:
                    raise RuntimeError("Evidence connections must not bypass row security")
                if kind == "query" and (not access["reader"] or access["writer"]):
                    raise RuntimeError("Query login requires reader membership and must not have ingestion privileges")
                if kind == "ingest" and not access["writer"]:
                    raise RuntimeError("Ingestion login requires ingestor membership")
        except BaseException:
            await instance.close()
            raise
        _pools[kind] = instance
    return _pools[kind]


async def install_scope(conn, scope: Scope):
    values = {
        "user_id": str(scope.user_id), "session_id": str(scope.session_id),
        "case_ids": json.dumps(scope.case_ids), "project_id": str(scope.project_id or ""),
        "include_global": str(scope.include_global).lower(),
        "scope_fingerprint":scope.fingerprint(),
    }
    for name, value in values.items():
        await conn.execute("SELECT set_config($1,$2,true)", f"evidence.{name}", value)
    await conn.execute("SET LOCAL statement_timeout = 15000")
    await conn.execute("SET LOCAL lock_timeout = 3000")


@asynccontextmanager
async def connection(scope: Scope, *, write: bool = False):
    instance = await pool()
    async with instance.acquire() as conn:
        async with conn.transaction(readonly=not write, isolation="repeatable_read"):
            await install_scope(conn, scope)
            yield conn


async def close():
    for instance in list(_pools.values()):
        await instance.close()
    _pools.clear()
