"""The widget contract. Anything asserted here is something the phone renders literally."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from flight_tracker import widget
from flight_tracker.config import Settings, get_settings
from flight_tracker.db import get_session
from flight_tracker.models import KV, Booking, FlightSnapshot, Passenger
from flight_tracker.widget import (
    FlightRow,
    authorize,
    build_payload,
    read_degraded,
)

NOW = datetime(2026, 9, 12, 18, 0, tzinfo=UTC)
DEPARTURE = datetime(2026, 9, 12, 18, 40, tzinfo=UTC)
ARRIVAL = datetime(2026, 9, 12, 22, 15, tzinfo=UTC)

# Anything a phone could read as an instant.
ISO_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

SELF = Passenger(id=1, display_name="Sébastien", is_self=True)
OTHER = Passenger(id=2, display_name="Alex", is_self=False)


def booking(**kwargs: Any) -> Booking:
    defaults: dict[str, Any] = {
        "id": 42,
        "passenger": SELF,
        "marketing_carrier": "DL",
        "marketing_number": "1234",
        "origin_iata": "JFK",
        "dest_iata": "LAX",
        "scheduled_departure_utc": DEPARTURE,
        "scheduled_arrival_utc": ARRIVAL,
        "status": "active",
        "source": "manual",
    }
    return Booking(**(defaults | kwargs))


def snapshot(**kwargs: Any) -> FlightSnapshot:
    defaults: dict[str, Any] = {"booking_id": 42, "raw": {}}
    return FlightSnapshot(**(defaults | kwargs))


def payload(rows: Sequence[FlightRow], settings: Settings, **kwargs: Any) -> dict[str, Any]:
    built = build_payload(rows, settings=settings, now=NOW, **kwargs)
    return built.model_dump(mode="json")


# --- payload shaping ------------------------------------------------------------------


def test_upcoming_flight(settings: Settings) -> None:
    far = booking(scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings)["flights"][0]
    assert flight == {
        "id": 42,
        "detail_url": "https://flights.example.com/f/42",
        "phase": "upcoming",
        "title": "DL1234  JFK → LAX",
        "subtitle": None,
        "countdown_label": "Departs in",
        "countdown_to": "2026-09-18T18:00:00Z",
        "delayed": False,
        "progress_percent": None,
        "passenger": "Sébastien",
        "is_self": True,
    }


def test_day_of_shows_gate_and_terminal(settings: Settings) -> None:
    gated = snapshot(scheduled_out=DEPARTURE, gate_origin="B22", terminal_origin="4")
    flight = payload([(booking(), gated)], settings)["flights"][0]
    assert flight["phase"] == "day_of"
    assert flight["subtitle"] == "Gate B22 · Terminal 4"
    assert flight["countdown_label"] == "Departs in"
    assert flight["countdown_to"] == "2026-09-12T18:40:00Z"


def test_upcoming_falls_back_to_the_seat(settings: Settings) -> None:
    flight = payload([(booking(seat="14A"), snapshot())], settings)["flights"][0]
    assert flight["subtitle"] == "Seat 14A"


def test_boarding_counts_down_to_boarding(settings: Settings) -> None:
    boarding = snapshot(scheduled_out=NOW + timedelta(minutes=20), gate_origin="B22")
    flight = payload([(booking(), boarding)], settings)["flights"][0]
    assert flight["phase"] == "boarding"
    assert flight["countdown_label"] == "Boards in"
    assert flight["countdown_to"] == "2026-09-12T17:50:00Z"


def test_airborne_counts_down_to_landing_and_shows_progress(settings: Settings) -> None:
    flying = snapshot(
        scheduled_out=DEPARTURE - timedelta(hours=2),
        actual_out=DEPARTURE - timedelta(hours=2),
        actual_off=DEPARTURE - timedelta(hours=2),
        scheduled_in=ARRIVAL,
        estimated_in=ARRIVAL + timedelta(minutes=6),
        gate_destination="12",
        terminal_destination="B",
        progress_percent=64,
    )
    flight = payload([(booking(), flying)], settings)["flights"][0]
    assert flight["phase"] == "airborne"
    assert flight["countdown_label"] == "Lands in"
    assert flight["countdown_to"] == "2026-09-12T22:21:00Z"
    assert flight["progress_percent"] == 64
    assert flight["subtitle"] == "Gate 12 · Terminal B"
    assert flight["delayed"] is True


def test_landed_shows_the_carousel_and_no_countdown(settings: Settings) -> None:
    landed = snapshot(
        actual_off=DEPARTURE,
        actual_on=ARRIVAL,
        baggage_claim="7",
        terminal_destination="B",
        gate_origin="B22",
        progress_percent=100,
    )
    flight = payload([(booking(), landed)], settings)["flights"][0]
    assert flight["phase"] == "landed"
    assert flight["subtitle"] == "Bag claim 7 · Terminal B"
    assert flight["countdown_label"] is None
    assert flight["countdown_to"] is None
    # A landed flight must never show the departure gate it left hours ago.
    assert flight["progress_percent"] is None


def test_cancelled_has_nothing_to_count_down_to(settings: Settings) -> None:
    flight = payload([(booking(), snapshot(cancelled=True))], settings)["flights"][0]
    assert flight["phase"] == "cancelled"
    assert flight["subtitle"] == "Cancelled"
    assert flight["countdown_label"] is None
    assert flight["countdown_to"] is None


def test_diverted_still_lands_somewhere(settings: Settings) -> None:
    diverted = snapshot(diverted=True, actual_off=DEPARTURE, estimated_in=ARRIVAL)
    flight = payload([(booking(), diverted)], settings)["flights"][0]
    assert flight["phase"] == "diverted"
    assert flight["subtitle"] == "Diverted"
    assert flight["countdown_label"] == "Lands in"
    assert flight["countdown_to"] == "2026-09-12T22:15:00Z"


def test_a_minute_late_is_not_delayed(settings: Settings) -> None:
    jitter = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(minutes=1))
    assert payload([(booking(), jitter)], settings)["flights"][0]["delayed"] is False


def test_other_passengers_are_marked(settings: Settings) -> None:
    flight = payload([(booking(passenger=OTHER), None)], settings)["flights"][0]
    assert flight["passenger"] == "Alex"
    assert flight["is_self"] is False


# --- instants -------------------------------------------------------------------------


def _instants(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _instants(item)
    elif isinstance(value, list):
        for item in value:
            yield from _instants(item)
    elif isinstance(value, str) and ISO_LIKE.match(value):
        yield value


def test_every_instant_is_utc_with_a_z(settings: Settings) -> None:
    """The phone renders these relative to its own clock; a missing zone shifts it."""
    rows: list[FlightRow] = [
        (booking(id=1), snapshot(scheduled_out=NOW + timedelta(minutes=20))),
        (booking(id=2), snapshot(actual_off=DEPARTURE, estimated_in=ARRIVAL)),
        (booking(id=3, scheduled_departure_utc=NOW + timedelta(days=4)), None),
    ]
    body = payload(rows, settings)
    found = list(_instants(body))
    assert len(found) == 4  # generated_at plus one countdown per flight
    assert all(instant.endswith("Z") for instant in found)


def test_instants_survive_a_non_utc_input(settings: Settings) -> None:
    """AeroAPI states offsets; whatever arrives must leave as Z."""
    tokyo = datetime(2026, 9, 12, 18, 40, tzinfo=UTC).astimezone()
    flight = payload([(booking(scheduled_departure_utc=tokyo), None)], settings)["flights"][0]
    assert flight["countdown_to"] == "2026-09-12T18:40:00Z"


# --- ordering and cadence -------------------------------------------------------------


def test_in_progress_first_then_soonest_capped_at_three(settings: Settings) -> None:
    rows: list[FlightRow] = [
        (booking(id=1, scheduled_departure_utc=NOW + timedelta(days=1)), None),
        (booking(id=2, scheduled_departure_utc=NOW + timedelta(days=3)), None),
        (
            booking(id=3, scheduled_departure_utc=NOW - timedelta(hours=3)),
            snapshot(actual_off=NOW - timedelta(hours=3)),
        ),
        (
            booking(id=4, scheduled_departure_utc=NOW - timedelta(hours=8)),
            snapshot(actual_off=NOW - timedelta(hours=8), actual_on=NOW - timedelta(hours=1)),
        ),
        (booking(id=5, scheduled_departure_utc=NOW + timedelta(minutes=10)), None),
    ]
    body = payload(rows, settings)
    # Airborne, then boarding in ten minutes, then tomorrow. The landed flight and the
    # one three days out lose their seats.
    assert [flight["id"] for flight in body["flights"]] == [3, 5, 1]


def test_landed_sinks_below_what_is_still_coming(settings: Settings) -> None:
    rows: list[FlightRow] = [
        (
            booking(id=4, scheduled_departure_utc=NOW - timedelta(hours=8)),
            snapshot(actual_on=NOW - timedelta(hours=1)),
        ),
        (booking(id=2, scheduled_departure_utc=NOW + timedelta(days=3)), None),
    ]
    assert [flight["id"] for flight in payload(rows, settings)["flights"]] == [2, 4]


def test_refresh_slows_down_when_nothing_is_close(settings: Settings) -> None:
    far = booking(scheduled_departure_utc=NOW + timedelta(days=4))
    assert payload([(far, None)], settings)["refresh_seconds"] == 900


def test_refresh_speeds_up_on_the_day(settings: Settings) -> None:
    assert payload([(booking(), None)], settings)["refresh_seconds"] == 600


def test_no_flights(settings: Settings) -> None:
    body = payload([], settings)
    assert body["flights"] == []
    assert body["refresh_seconds"] == 900
    assert body["degraded"] is False


# --- degraded -------------------------------------------------------------------------


class FakeScalars:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = rows

    def all(self) -> Sequence[Any]:
        return self._rows


class FakeSession:
    """Just enough of AsyncSession for the KV lookup; no database anywhere."""

    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = rows

    async def scalars(self, statement: Any) -> FakeScalars:
        return FakeScalars(self._rows)


async def test_degraded_when_the_breaker_latch_is_present() -> None:
    session = FakeSession([KV(key="aeroapi_breaker", value={"tripped": True, "reason": "Cap hit"})])
    assert await read_degraded(session, NOW) == "Cap hit"  # type: ignore[arg-type]


async def test_degraded_latch_without_a_reason_still_degrades() -> None:
    session = FakeSession([KV(key="aeroapi_breaker", value={})])
    reason = await read_degraded(session, NOW)  # type: ignore[arg-type]
    assert reason is not None and "AeroAPI" in reason


async def test_untripped_latch_is_not_degraded() -> None:
    session = FakeSession([KV(key="aeroapi_breaker", value={"tripped": False})])
    assert await read_degraded(session, NOW) is None  # type: ignore[arg-type]


async def test_missing_keys_are_not_degraded() -> None:
    assert await read_degraded(FakeSession([]), NOW) is None  # type: ignore[arg-type]


async def test_stale_poll_is_degraded() -> None:
    stale = (NOW - timedelta(minutes=95)).isoformat().replace("+00:00", "Z")
    session = FakeSession([KV(key="poller_last_success", value={"at": stale})])
    assert await read_degraded(session, NOW) == "No status update in 95 min"  # type: ignore[arg-type]


async def test_recent_poll_is_not_degraded() -> None:
    recent = (NOW - timedelta(minutes=4)).isoformat()
    session = FakeSession([KV(key="poller_last_success", value={"at": recent})])
    assert await read_degraded(session, NOW) is None  # type: ignore[arg-type]


async def test_unparseable_poll_timestamp_is_tolerated() -> None:
    session = FakeSession([KV(key="poller_last_success", value={"at": "soon"})])
    assert await read_degraded(session, NOW) is None  # type: ignore[arg-type]


def test_degraded_reason_reaches_the_payload(settings: Settings) -> None:
    body = payload([], settings, degraded_reason="Cap hit")
    assert body["degraded"] is True
    assert body["degraded_reason"] == "Cap hit"


# --- auth -----------------------------------------------------------------------------


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def fake_rows(session: Any, now: datetime) -> list[FlightRow]:
        return [(booking(), snapshot(gate_origin="B22", terminal_origin="4"))]

    async def fake_degraded(session: Any, now: datetime) -> str | None:
        return None

    monkeypatch.setattr(widget, "load_flight_rows", fake_rows)
    monkeypatch.setattr(widget, "read_degraded", fake_degraded)

    async def fake_session() -> Any:
        yield None

    app = FastAPI()
    app.include_router(widget.router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = fake_session
    with TestClient(app) as test_client:
        yield test_client


def test_bearer_header_is_accepted(client: TestClient) -> None:
    response = client.get("/api/widget", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["flights"][0]["title"] == "DL1234  JFK → LAX"
    assert body["generated_at"].endswith("Z")


def test_query_token_is_accepted(client: TestClient) -> None:
    assert client.get("/api/widget?token=test-token").status_code == 200


def test_wrong_token_is_rejected(client: TestClient) -> None:
    assert client.get("/api/widget?token=nope").status_code == 401
    assert client.get("/api/widget", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_missing_token_is_rejected(client: TestClient) -> None:
    assert client.get("/api/widget").status_code == 401


def test_a_header_that_is_not_a_bearer_is_rejected(client: TestClient) -> None:
    response = client.get("/api/widget", headers={"Authorization": "test-token"})
    assert response.status_code == 401


def test_an_unset_token_refuses_everyone(settings: Settings) -> None:
    """A blank token must never mean "no authentication"."""
    unset = settings.model_copy(update={"widget_token": ""})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        authorize(unset, "Bearer test-token", None)
    assert caught.value.status_code == 503

    with pytest.raises(HTTPException):
        authorize(unset, None, "")
