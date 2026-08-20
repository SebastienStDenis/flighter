"""The AeroAPI client: what we spend, how fast we spend it, and what we read back."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from flighter.aeroapi import (
    DEFAULT_PRICE_USD,
    FLIGHT_INFO_ENDPOINT,
    AeroAPIClient,
    BudgetExceeded,
    TokenBucket,
    budget_status,
    ensure_budget,
    estimate_cost,
    fetch_flight,
    flight_ident,
    resolve_flight,
    select_match,
    to_snapshot_fields,
)
from flighter.config import Settings
from flighter.models import KV, ApiUsage, Booking

DEPARTURE = datetime(2026, 9, 12, 18, 40, tzinfo=UTC)

# Shaped after the documented BaseFlight schema: flat, `Z`-suffixed UTC timestamps,
# nested FlightAirportRef objects for origin and destination.
FLIGHT: dict[str, Any] = {
    "ident": "BAW112",
    "ident_icao": "BAW112",
    "ident_iata": "BA112",
    "fa_flight_id": "BAW112-1757600000-airline-0123",
    "operator": "BAW",
    "operator_icao": "BAW",
    "operator_iata": "BA",
    "flight_number": "112",
    "registration": "G-ZBKF",
    "atc_ident": None,
    "inbound_fa_flight_id": "BAW113-1757500000-airline-0456",
    "codeshares": ["AAL6141"],
    "codeshares_iata": ["AA6141"],
    "blocked": False,
    "diverted": False,
    "cancelled": False,
    "position_only": False,
    "origin": {
        "code": "EGLL",
        "code_icao": "EGLL",
        "code_iata": "LHR",
        "code_lid": None,
        "timezone": "Europe/London",
        "name": "London Heathrow",
        "city": "London",
        "airport_info_url": "/airports/EGLL",
    },
    "destination": {
        "code": "KJFK",
        "code_icao": "KJFK",
        "code_iata": "JFK",
        "code_lid": None,
        "timezone": "America/New_York",
        "name": "John F Kennedy Intl",
        "city": "New York",
        "airport_info_url": "/airports/KJFK",
    },
    "departure_delay": 900,
    "arrival_delay": -300,
    "filed_ete": 28800,
    "progress_percent": 42,
    "status": "En Route / On Time",
    "aircraft_type": "B77W",
    "route_distance": 3451,
    "filed_airspeed": 480,
    "filed_altitude": 380,
    "route": "DET L6 WELIN...",
    "baggage_claim": "4",
    "seats_cabin_business": 48,
    "seats_cabin_coach": 122,
    "seats_cabin_first": 14,
    "gate_origin": "A12",
    "gate_destination": "B22",
    "terminal_origin": "5",
    "terminal_destination": "7",
    "type": "Airline",
    "scheduled_out": "2026-09-12T18:40:00Z",
    "estimated_out": "2026-09-12T18:55:00Z",
    "actual_out": "2026-09-12T18:57:00Z",
    "scheduled_off": "2026-09-12T18:55:00Z",
    "estimated_off": "2026-09-12T19:10:00Z",
    "actual_off": "2026-09-12T19:12:00Z",
    "scheduled_on": "2026-09-12T21:30:00Z",
    "estimated_on": "2026-09-12T21:25:00Z",
    "actual_on": None,
    "scheduled_in": "2026-09-12T21:40:00Z",
    "estimated_in": "2026-09-12T21:35:00Z",
    "actual_in": None,
    "foresight_predictions_available": False,
}


def booking(**overrides: Any) -> Booking:
    defaults: dict[str, Any] = {
        "id": 1,
        "source": "email",
        "marketing_carrier": "AA",
        "marketing_number": "6141",
        "operating_carrier": "BAW",
        "operating_number": "112",
        "origin_iata": "LHR",
        "dest_iata": "JFK",
        "scheduled_departure_utc": DEPARTURE,
        "status": "active",
    }
    return Booking(**{**defaults, **overrides})


class FakeSession:
    """Enough AsyncSession for the budget and usage paths. No database anywhere."""

    def __init__(self, spend: Decimal = Decimal("0"), kv: dict[str, KV] | None = None) -> None:
        self.spend = spend
        self.kv = kv or {}
        self.added: list[Any] = []

    async def scalar(self, _statement: Any) -> Decimal:
        return self.spend

    async def get(self, _model: Any, key: str) -> KV | None:
        return self.kv.get(key)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def merge(self, obj: Any) -> Any:
        self.kv[obj.key] = obj
        return obj

    @property
    def usage(self) -> list[ApiUsage]:
        return [row for row in self.added if isinstance(row, ApiUsage)]


def make_client(session: FakeSession, handler: Any) -> AeroAPIClient:
    settings = Settings(aeroapi_key="test-key", aeroapi_base_url="https://aeroapi.example/aeroapi")
    return AeroAPIClient(
        settings,
        transport=httpx.MockTransport(handler),
        # A bucket wide enough that no test ever waits on it; timing is tested directly.
        limiter=TokenBucket(6000),
    )


def json_response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload), headers={"Content-Type": "text/json"})


# --- Snapshot mapping ----------------------------------------------------------------


def test_to_snapshot_fields_extracts_the_denormalised_columns() -> None:
    assert to_snapshot_fields(FLIGHT) == {
        "status_text": "En Route / On Time",
        "cancelled": False,
        "diverted": False,
        "gate_origin": "A12",
        "gate_destination": "B22",
        "terminal_origin": "5",
        "terminal_destination": "7",
        "baggage_claim": "4",
        "aircraft_type": "B77W",
        "registration": "G-ZBKF",
        "progress_percent": 42,
        "scheduled_out": datetime(2026, 9, 12, 18, 40, tzinfo=UTC),
        "estimated_out": datetime(2026, 9, 12, 18, 55, tzinfo=UTC),
        "actual_out": datetime(2026, 9, 12, 18, 57, tzinfo=UTC),
        "actual_off": datetime(2026, 9, 12, 19, 12, tzinfo=UTC),
        "scheduled_on": datetime(2026, 9, 12, 21, 30, tzinfo=UTC),
        "estimated_on": datetime(2026, 9, 12, 21, 25, tzinfo=UTC),
        "actual_on": None,
        "scheduled_in": datetime(2026, 9, 12, 21, 40, tzinfo=UTC),
        "estimated_in": datetime(2026, 9, 12, 21, 35, tzinfo=UTC),
        "actual_in": None,
    }


def test_to_snapshot_fields_survives_an_all_nulls_response() -> None:
    nulls = dict.fromkeys(FLIGHT)
    assert set(to_snapshot_fields(nulls)) == set(to_snapshot_fields(FLIGHT))
    assert all(value is None for value in to_snapshot_fields(nulls).values())


def test_to_snapshot_fields_never_raises_on_missing_keys() -> None:
    """AeroAPI omits non-required keys outright; a KeyError here would lose an event."""
    assert all(value is None for value in to_snapshot_fields({}).values())


def test_to_snapshot_fields_ignores_unparseable_timestamps() -> None:
    fields = to_snapshot_fields({**FLIGHT, "scheduled_out": "not a date", "progress_percent": "x"})
    assert fields["scheduled_out"] is None
    assert fields["progress_percent"] is None


def test_timestamps_without_a_zone_are_read_as_utc() -> None:
    fields = to_snapshot_fields({"scheduled_out": "2026-09-12T18:40:00"})
    assert fields["scheduled_out"] == datetime(2026, 9, 12, 18, 40, tzinfo=UTC)


# --- Match selection -----------------------------------------------------------------


def leg(
    offset: timedelta, *, origin: str = "LHR", dest: str = "JFK", **extra: Any
) -> dict[str, Any]:
    return {
        **FLIGHT,
        "fa_flight_id": f"BAW112-{int(offset.total_seconds())}",
        "scheduled_out": (DEPARTURE + offset).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "origin": {**FLIGHT["origin"], "code_iata": origin, "code_icao": None, "code": origin},
        "destination": {
            **FLIGHT["destination"],
            "code_iata": dest,
            "code_icao": None,
            "code": dest,
        },
        **extra,
    }


def test_match_picks_the_nearest_scheduled_departure() -> None:
    candidates = [
        leg(timedelta(days=-1)),
        leg(timedelta(hours=2)),
        leg(timedelta(minutes=-5)),
        leg(timedelta(days=1)),
    ]
    match = select_match(candidates, booking())
    assert match is not None
    assert match["fa_flight_id"] == leg(timedelta(minutes=-5))["fa_flight_id"]


def test_match_accepts_just_inside_six_hours() -> None:
    assert select_match([leg(timedelta(hours=5, minutes=59))], booking()) is not None


def test_match_rejects_just_outside_six_hours() -> None:
    assert select_match([leg(timedelta(hours=6, minutes=1))], booking()) is None


def test_match_rejects_the_right_time_at_the_wrong_airport() -> None:
    assert select_match([leg(timedelta(0), dest="BOS")], booking()) is None
    assert select_match([leg(timedelta(0), origin="LGW")], booking()) is None


def test_match_falls_back_to_scheduled_off_when_there_is_no_gate_time() -> None:
    candidate = leg(timedelta(0))
    candidate["scheduled_out"] = None
    candidate["scheduled_off"] = (DEPARTURE + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert select_match([candidate], booking()) is not None


def test_match_tolerates_junk_in_the_flights_array() -> None:
    assert select_match(["nonsense", {}, leg(timedelta(0))], booking()) is not None


def test_ident_prefers_the_operating_carrier() -> None:
    assert flight_ident(booking()) == "BAW112"
    assert flight_ident(booking(aeroapi_ident="BAW112")) == "BAW112"
    assert flight_ident(booking(operating_number="0112")) == "BAW112"


def test_ident_is_converted_to_icao_form() -> None:
    # FlightAware reads a two-letter IATA code ambiguously, so the marketing code on the
    # ticket is translated before it is ever sent.
    assert flight_ident(booking(operating_carrier=None, operating_number=None)) == "AAL6141"


# --- Token bucket --------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_token_bucket_lets_a_full_bucket_through_without_waiting() -> None:
    clock = FakeClock()
    bucket = TokenBucket(8, clock=clock, sleep=clock.sleep)
    for _ in range(8):
        await bucket.acquire()
    assert clock.slept == []
    assert bucket.tokens == pytest.approx(0.0)


async def test_token_bucket_blocks_rather_than_raising_when_empty() -> None:
    clock = FakeClock()
    bucket = TokenBucket(8, clock=clock, sleep=clock.sleep)
    for _ in range(10):
        await bucket.acquire()
    # 8 per minute is one every 7.5 seconds once the initial burst is gone.
    assert clock.slept == pytest.approx([7.5, 7.5])
    assert clock.now == pytest.approx(15.0)


async def test_token_bucket_refills_but_never_past_capacity() -> None:
    clock = FakeClock()
    bucket = TokenBucket(8, clock=clock, sleep=clock.sleep)
    for _ in range(8):
        await bucket.acquire()
    clock.now = 3600.0
    for _ in range(8):
        await bucket.acquire()
    assert clock.slept == []
    await bucket.acquire()
    assert clock.slept == pytest.approx([7.5])


# --- Cost and the breaker ------------------------------------------------------------


def test_cost_is_per_result_set() -> None:
    assert estimate_cost(FLIGHT_INFO_ENDPOINT, 1) == Decimal("0.005000")
    assert estimate_cost(FLIGHT_INFO_ENDPOINT, 3) == Decimal("0.015000")
    # A page count AeroAPI never returns is still billed as at least one.
    assert estimate_cost(FLIGHT_INFO_ENDPOINT, 0) == Decimal("0.005000")


def test_unpriced_endpoints_fall_back_to_the_default() -> None:
    assert estimate_cost("/flights/search", 2) == (DEFAULT_PRICE_USD * 2).quantize(
        Decimal("0.000001")
    )


async def test_budget_is_untripped_one_poll_short_of_the_cap() -> None:
    # 799 polls at $0.005 is $3.995 against a $4.00 cap.
    session = FakeSession(spend=estimate_cost(FLIGHT_INFO_ENDPOINT, 1) * 799)
    status = await budget_status(session)  # type: ignore[arg-type]
    assert status.spend_usd == Decimal("3.995000")
    assert status.tripped is False


async def test_budget_trips_exactly_at_the_cap() -> None:
    session = FakeSession(spend=estimate_cost(FLIGHT_INFO_ENDPOINT, 1) * 800)
    status = await budget_status(session)  # type: ignore[arg-type]
    assert status.spend_usd == Decimal("4.000000")
    assert status.tripped is True


async def test_ensure_budget_latches_and_raises() -> None:
    session = FakeSession(spend=Decimal("4.50"))
    with pytest.raises(BudgetExceeded):
        await ensure_budget(session)  # type: ignore[arg-type]
    latch = session.kv["aeroapi_budget_breaker"]
    assert latch.value["month"] == datetime.now(UTC).strftime("%Y-%m")
    assert latch.value["spend_usd"] == "4.50"


async def test_a_latch_from_this_month_trips_the_breaker_on_its_own() -> None:
    """The UI and the worker must stay stopped across a restart, not recompute their way
    back in on a rounding difference."""
    month = datetime.now(UTC).strftime("%Y-%m")
    session = FakeSession(kv={"aeroapi_budget_breaker": KV(key="k", value={"month": month})})
    status = await budget_status(session)  # type: ignore[arg-type]
    assert status.tripped is True


async def test_a_latch_from_last_month_is_ignored() -> None:
    session = FakeSession(kv={"aeroapi_budget_breaker": KV(key="k", value={"month": "2020-01"})})
    status = await budget_status(session)  # type: ignore[arg-type]
    assert status.tripped is False


# --- HTTP ----------------------------------------------------------------------------


async def test_flight_info_authenticates_and_always_asks_for_one_page() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response({"links": None, "num_pages": 1, "flights": [FLIGHT]})

    session = FakeSession()
    client = make_client(session, handler)
    try:
        await client.flight_info(session, "BAW112", ident_type="designator")  # type: ignore[arg-type]
    finally:
        await client.aclose()

    request = seen[0]
    assert request.headers["x-apikey"] == "test-key"
    assert request.url.path == "/aeroapi/flights/BAW112"
    # max_pages caps the bill: a bare ident otherwise returns ~14 days of flights, and
    # every page of 15 records is charged again.
    assert request.url.params["max_pages"] == "1"
    assert request.url.params["ident_type"] == "designator"
    assert [(row.endpoint, row.result_sets, row.est_cost_usd) for row in session.usage] == [
        (FLIGHT_INFO_ENDPOINT, 1, 0.005)
    ]


async def test_usage_is_billed_per_page_returned() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"links": None, "num_pages": 3, "flights": [FLIGHT]})

    session = FakeSession()
    client = make_client(session, handler)
    try:
        await client.flight_info(session, "BAW112")  # type: ignore[arg-type]
    finally:
        await client.aclose()

    assert session.usage[0].result_sets == 3
    assert session.usage[0].est_cost_usd == 0.015


async def test_the_budget_is_checked_before_the_call_is_made() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("spent money with the breaker tripped")

    session = FakeSession(spend=Decimal("99"))
    client = make_client(session, handler)
    try:
        with pytest.raises(BudgetExceeded):
            await client.flight_info(session, "BAW112")  # type: ignore[arg-type]
    finally:
        await client.aclose()
    assert session.usage == []


async def test_resolve_pins_the_fa_flight_id_and_the_icao_ident() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response(
            {"links": None, "num_pages": 1, "flights": [leg(timedelta(days=-1)), leg(timedelta(0))]}
        )

    session = FakeSession()
    client = make_client(session, handler)
    target = booking(marketing_carrier="AA", operating_carrier="BAW")
    try:
        match = await resolve_flight(session, target, client)  # type: ignore[arg-type]
        flight = await fetch_flight(session, target, client)  # type: ignore[arg-type]
    finally:
        await client.aclose()

    assert match is not None
    assert match.fa_flight_id == "BAW112-0"
    assert flight is not None
    assert target.aeroapi_fa_flight_id == "BAW112-0"
    assert target.aeroapi_ident == "BAW112"


async def test_resolve_returns_none_rather_than_raising_when_nothing_matches() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"links": None, "num_pages": 1, "flights": []})

    session = FakeSession()
    client = make_client(session, handler)
    try:
        assert await resolve_flight(session, booking(), client) is None  # type: ignore[arg-type]
    finally:
        await client.aclose()


async def test_a_pinned_diversion_returns_the_current_leg_not_the_first() -> None:
    """A diverted flight comes back as two entries sharing one fa_flight_id, the original
    first. Taking flights[0] would report the abandoned leg forever."""
    original = {
        **FLIGHT,
        "diverted": True,
        "actual_out": "2026-09-12T18:57:00Z",
        "gate_destination": "B22",
    }
    diversion = {
        **FLIGHT,
        "diverted": True,
        "actual_out": "2026-09-12T22:10:00Z",
        "gate_destination": "C4",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ident_type"] == "fa_flight_id"
        return json_response(
            {
                "links": None,
                "num_pages": 1,
                "flights": [original, diversion, {**FLIGHT, "fa_flight_id": "other"}],
            }
        )

    session = FakeSession()
    client = make_client(session, handler)
    target = booking(aeroapi_fa_flight_id=FLIGHT["fa_flight_id"])
    try:
        flight = await fetch_flight(session, target, client)  # type: ignore[arg-type]
    finally:
        await client.aclose()

    assert flight is not None
    assert flight["gate_destination"] == "C4"


async def test_a_pinned_id_that_returns_nothing_is_not_an_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"links": None, "num_pages": 1, "flights": []})

    session = FakeSession()
    client = make_client(session, handler)
    try:
        result = await fetch_flight(session, booking(aeroapi_fa_flight_id="x"), client)  # type: ignore[arg-type]
    finally:
        await client.aclose()
    assert result is None
