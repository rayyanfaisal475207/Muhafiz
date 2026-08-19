# ============================================================
# Muhafiz Data API — async HTTP client
#
# Thin wrapper over the REST API documented in API_CONSUMER_GUIDE.md. Not
# part of src/data_gateway's DataGateway protocol (that protocol is the
# app's own users/sessions/cases persistence layer — see selector.py's own
# "one backend, one connection path" docstring) — this is a client for an
# EXTERNAL evidence source, consumed by src/ingestion/muhafiz_records.py
# (M3) and src/ingestion/muhafiz_cases.py (M4), not by DirectGateway.
#
# Confirmed (docs/decisions/0001-muhafiz-api-migration.md): this endpoint is
# a same-schema STAND-IN, not the real police system. No air-gap awareness
# is added here on purpose — it's a plain internet call, gated entirely by
# MUHAFIZ_API_BASE_URL being configured (see src/config.py's pairing check).
# ============================================================

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src import config
from src.data_gateway.muhafiz_api.errors import (
    MuhafizApiAuthError,
    MuhafizApiError,
    MuhafizApiNotFoundError,
    MuhafizApiUnavailableError,
    MuhafizApiValidationError,
)

logger = logging.getLogger(__name__)

# The five data endpoints this client covers (API_CONSUMER_GUIDE.md). Kept
# as a module constant so scripts/sync_muhafiz_data.py (M9) can iterate them
# without hardcoding the list a second time.
ENDPOINTS = ("fir", "roznamcha", "cms", "pkm", "criminal-records")

# /roznamcha is the one list endpoint that does NOT accept updated_since
# (API_CONSUMER_GUIDE.md, "Incremental sync"). Recorded here so a caller
# can't accidentally pass it and get a silently-ignored parameter.
_NO_UPDATED_SINCE = {"roznamcha"}


def _retryable() -> retry:
    return retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
        reraise=True,
    )


class MuhafizApiClient:
    """
    Usage:
        client = MuhafizApiClient()
        async for fir in client.iter_all("fir"):
            ...
        await client.aclose()

    Or as an async context manager:
        async with MuhafizApiClient() as client:
            ...
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
        page_size: Optional[int] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        """
        `transport` exists so tests can pass an `httpx.MockTransport` —
        tests/conftest.py's autouse `no_network` fixture patches
        `httpx.HTTPTransport`/`AsyncHTTPTransport` (the real network
        transports) at the class level; `MockTransport` is a distinct
        class the patch never touches, which is what makes an in-memory
        fake response reach this client without any real socket call.
        """
        self.base_url = (base_url or config.MUHAFIZ_API_BASE_URL).rstrip("/")
        self.api_key = api_key or config.MUHAFIZ_API_KEY
        self.timeout = timeout or config.MUHAFIZ_API_TIMEOUT
        self.page_size = page_size or config.MUHAFIZ_API_PAGE_SIZE
        if not self.base_url:
            raise MuhafizApiError(
                "MUHAFIZ_API_BASE_URL is not configured — this source is disabled. "
                "See docs/decisions/0001-muhafiz-api-migration.md."
            )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": self.api_key} if self.api_key else {},
            timeout=self.timeout,
            transport=transport,
        )

    async def __aenter__(self) -> "MuhafizApiClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── low-level request ────────────────────────────────────────────────

    @_retryable()
    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        try:
            response = await self._client.get(path, params=params or {})
        except httpx.TimeoutException as exc:
            raise MuhafizApiUnavailableError(f"Timed out calling {path}: {exc}") from exc
        except httpx.TransportError as exc:
            raise MuhafizApiUnavailableError(f"Connection failure calling {path}: {exc}") from exc

        if response.status_code == 401:
            raise MuhafizApiAuthError(f"401 from {path} — check MUHAFIZ_API_KEY.")
        if response.status_code == 404:
            body = _safe_json(response)
            raise MuhafizApiNotFoundError(
                (body or {}).get("error", f"404 from {path}"), resource_id=path
            )
        if response.status_code == 422:
            body = _safe_json(response)
            raise MuhafizApiValidationError(
                (body or {}).get("error", f"422 from {path}: {params}")
            )
        if response.status_code >= 500:
            raise MuhafizApiUnavailableError(f"{response.status_code} from {path}")
        response.raise_for_status()  # catches any other unexpected 4xx

        body = _safe_json(response)
        if body is None:
            raise MuhafizApiUnavailableError(f"Non-JSON response from {path}")
        return body

    # ── health ────────────────────────────────────────────────────────────

    async def health(self) -> bool:
        """GET /health — no auth required (API_CONSUMER_GUIDE.md)."""
        try:
            body = await self._get("/health")
        except MuhafizApiError:
            return False
        return body.get("status") == "ok"

    # ── single-record fetch ──────────────────────────────────────────────

    async def get_one(self, endpoint: str, record_id: str) -> dict:
        """GET /{endpoint}/{record_id}."""
        return await self._get(f"/{endpoint}/{record_id}")

    # ── paginated listing ────────────────────────────────────────────────

    async def get_page(
        self, endpoint: str, page: int, page_size: Optional[int] = None,
        updated_since: Optional[str] = None,
    ) -> dict:
        """
        One page. Returns the raw {data, meta} envelope
        (API_CONSUMER_GUIDE.md) unchanged — page/page_size are always
        required by the API, so both are always sent.
        """
        params: dict = {"page": page, "page_size": page_size or self.page_size}
        if updated_since:
            if endpoint in _NO_UPDATED_SINCE:
                raise MuhafizApiError(
                    f"/{endpoint} does not support updated_since (API_CONSUMER_GUIDE.md)."
                )
            params["updated_since"] = updated_since
        return await self._get(f"/{endpoint}", params=params)

    async def iter_all(
        self, endpoint: str, updated_since: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """
        Pages through `endpoint` from page=1 until at least meta.total
        records have been seen, yielding each raw record dict in order.
        Mirrors API_CONSUMER_GUIDE.md's "Pagination" section exactly: "Start
        with page=1 ... Stop after receiving at least meta.total records."
        """
        page = 1
        seen = 0
        while True:
            envelope = await self.get_page(endpoint, page, updated_since=updated_since)
            data = envelope.get("data") or []
            meta = envelope.get("meta") or {}
            total = meta.get("total", 0)
            for record in data:
                yield record
            seen += len(data)
            logger.debug("muhafiz_api: %s page %d — %d/%d", endpoint, page, seen, total)
            if not data or seen >= total:
                return
            page += 1

    async def fetch_all(
        self, endpoint: str, updated_since: Optional[str] = None,
    ) -> list[dict]:
        """Convenience: iter_all() collected into a list."""
        return [record async for record in self.iter_all(endpoint, updated_since=updated_since)]


def _safe_json(response: httpx.Response) -> Optional[dict]:
    try:
        return response.json()
    except ValueError:
        return None
