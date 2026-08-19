"""
Muhafiz Data API client — src/data_gateway/muhafiz_api/.

All tests use httpx.MockTransport (see client.py's docstring on why that,
specifically, is what lets these run under conftest.py's autouse
`no_network` fixture with zero real sockets). No live API call happens in
this file.
"""
import json

import httpx
import pytest

from src.data_gateway.muhafiz_api.client import MuhafizApiClient
from src.data_gateway.muhafiz_api.errors import (
    MuhafizApiAuthError,
    MuhafizApiError,
    MuhafizApiNotFoundError,
    MuhafizApiUnavailableError,
    MuhafizApiValidationError,
)
from src.data_gateway.muhafiz_api.models import FirRecord, PkmApplication
from src.data_gateway.muhafiz_api.snapshot import (
    dump_snapshot,
    load_snapshot,
    records_for,
)


def _client(handler) -> MuhafizApiClient:
    return MuhafizApiClient(
        base_url="https://muhafiz.test",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def _envelope(data: list, page: int = 1, page_size: int = 100, total: int = None) -> dict:
    return {"data": data, "meta": {"page": page, "page_size": page_size, "total": total if total is not None else len(data)}}


@pytest.mark.asyncio
async def test_health_true_on_ok_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    async with _client(handler) as client:
        assert await client.health() is True


@pytest.mark.asyncio
async def test_health_false_on_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with _client(handler) as client:
        assert await client.health() is False


@pytest.mark.asyncio
async def test_get_page_sends_required_pagination_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_envelope([{"fir_id": "fir-1-26"}]))

    async with _client(handler) as client:
        await client.get_page("fir", page=1, page_size=50)

    assert seen["params"] == {"page": "1", "page_size": "50"}


@pytest.mark.asyncio
async def test_get_page_includes_updated_since_when_given():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_envelope([]))

    async with _client(handler) as client:
        await client.get_page("fir", page=1, updated_since="2026-08-18T15:10:00Z")

    assert seen["params"]["updated_since"] == "2026-08-18T15:10:00Z"


@pytest.mark.asyncio
async def test_get_page_rejects_updated_since_on_roznamcha():
    """API_CONSUMER_GUIDE.md: /roznamcha does not support updated_since."""
    async with _client(lambda r: httpx.Response(200, json=_envelope([]))) as client:
        with pytest.raises(MuhafizApiError, match="does not support"):
            await client.get_page("roznamcha", page=1, updated_since="2026-08-18T15:10:00Z")


@pytest.mark.asyncio
async def test_iter_all_pages_until_total_reached():
    """Regression: must stop exactly at meta.total, not loop forever or under-fetch."""
    pages = {
        1: _envelope([{"fir_id": f"fir-{i}-26"} for i in range(1, 51)], page=1, total=73),
        2: _envelope([{"fir_id": f"fir-{i}-26"} for i in range(51, 74)], page=2, total=73),
    }
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params)["page"])
        calls.append(page)
        return httpx.Response(200, json=pages[page])

    async with _client(handler) as client:
        records = await client.fetch_all("fir")

    assert len(records) == 73
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_iter_all_stops_on_empty_page_even_if_total_not_reached():
    """Defensive: a short/empty page must not spin the loop forever."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope([], page=1, total=999))

    async with _client(handler) as client:
        records = await client.fetch_all("fir")

    assert records == []


@pytest.mark.asyncio
async def test_get_one_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fir/fir-891-24"
        return httpx.Response(200, json={"fir_id": "fir-891-24"})

    async with _client(handler) as client:
        record = await client.get_one("fir", "fir-891-24")
    assert record["fir_id"] == "fir-891-24"


@pytest.mark.asyncio
async def test_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key"})

    async with _client(handler) as client:
        with pytest.raises(MuhafizApiAuthError):
            await client.get_page("fir", page=1)


@pytest.mark.asyncio
async def test_404_raises_not_found_with_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "FIR fir-999-99 not found", "detail": None})

    async with _client(handler) as client:
        with pytest.raises(MuhafizApiNotFoundError, match="fir-999-99"):
            await client.get_one("fir", "fir-999-99")


@pytest.mark.asyncio
async def test_422_raises_validation_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "page_size required"})

    async with _client(handler) as client:
        with pytest.raises(MuhafizApiValidationError):
            await client.get_page("fir", page=1)


@pytest.mark.asyncio
async def test_5xx_raises_unavailable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _client(handler) as client:
        with pytest.raises(MuhafizApiUnavailableError):
            await client.get_page("fir", page=1)


@pytest.mark.asyncio
async def test_disabled_when_base_url_unset(monkeypatch):
    # base_url="" alone isn't enough to prove this — the constructor falls
    # back to config.MUHAFIZ_API_BASE_URL, which IS set in this repo's own
    # .env (the live stand-in). Clear that too, so this test actually
    # exercises "no source configured at all," not "just happens to fall
    # back to a real one."
    monkeypatch.setattr("src.config.MUHAFIZ_API_BASE_URL", "")
    with pytest.raises(MuhafizApiError, match="MUHAFIZ_API_BASE_URL"):
        MuhafizApiClient(base_url="", api_key="x")


# ── models: tolerant reads over drifted real-API shapes ────────────────────

def test_fir_record_falls_back_to_legacy_crime_scene_field():
    """
    Measured live: crime_scene_location (revision-11 merged field) can be
    null while the legacy crime_scene_description carries the value, or
    vice versa. FirRecord must read whichever is populated.
    """
    rec = FirRecord({"fir_id": "fir-1-26", "crime_scene_location": None,
                      "crime_scene_description": "شاہ فیصل کالونی"})
    assert rec.crime_scene_location == "شاہ فیصل کالونی"

    rec2 = FirRecord({"fir_id": "fir-2-26", "crime_scene_location": "ماڈل ٹاؤن",
                       "crime_scene_description": None})
    assert rec2.crime_scene_location == "ماڈل ٹاؤن"


def test_fir_record_child_rows_defaults_to_empty_list():
    rec = FirRecord({"fir_id": "fir-1-26"})
    assert rec.child_rows("fir_accused") == []
    assert rec.child_rows("weapon_register") == []


def test_fir_record_child_rows_returns_actual_rows():
    rec = FirRecord({"fir_id": "fir-1-26", "fir_accused": [{"full_name": "X"}]})
    assert rec.child_rows("fir_accused") == [{"full_name": "X"}]


def test_pkm_application_service_record_picks_the_populated_one():
    """
    Measured live: exactly one of the 7 service-specific keys is non-null
    per application, the rest are explicit null (not absent).
    """
    app = PkmApplication({
        "application_id": "pkm-1", "service_type": "vehicle_verification",
        "character_certificate": None, "driving_license": None,
        "employee_registration": None, "loss_report": None,
        "tenant_registration": None,
        "vehicle_verification": {"vehicle_registration_no": "FSD-19-8842"},
        "women_violence_report": None,
    })
    rec = app.service_record()
    assert rec["service_type"] == "vehicle_verification"
    assert rec["vehicle_registration_no"] == "FSD-19-8842"


def test_pkm_application_forwarded_fir_number_only_from_women_violence_report():
    app = PkmApplication({
        "application_id": "pkm-1", "service_type": "driving_license",
        "driving_license": {"license_category": "A"}, "women_violence_report": None,
    })
    assert app.forwarded_fir_number is None


# ── snapshot round-trip ─────────────────────────────────────────────────────

def test_snapshot_round_trip(tmp_path):
    snapshot = {
        "fetched_at": "2026-08-20T00:00:00Z",
        "endpoints": {"fir": [{"fir_id": "fir-1-26"}], "cms": []},
    }
    path = tmp_path / "snapshot.json"
    dump_snapshot(snapshot, path)
    loaded = load_snapshot(path)
    assert records_for(loaded, "fir") == [{"fir_id": "fir-1-26"}]
    assert records_for(loaded, "cms") == []
    assert records_for(loaded, "pkm") == []  # missing endpoint -> empty, not KeyError
