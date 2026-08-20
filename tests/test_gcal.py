"""The calendar event body: the one place a wrong timezone turns into a missed flight."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from flighter.gcal import REMINDER_MINUTES, event_body
from flighter.models import Airport, Booking, FlightSnapshot

BASE_URL = "https://flights.example.com"

AIRPORTS = {
    "JFK": Airport(
        iata="JFK",
        name="John F Kennedy International Airport",
        city="New York",
        country="US",
        latitude=40.6398,
        longitude=-73.7789,
        tz="America/New_York",
    ),
    "LAX": Airport(
        iata="LAX",
        name="Los Angeles International Airport",
        city="Los Angeles",
        country="US",
        latitude=33.9425,
        longitude=-118.4081,
        tz="America/Los_Angeles",
    ),
    "CDG": Airport(
        iata="CDG",
        name="Charles de Gaulle International Airport",
        city="Paris",
        country="FR",
        latitude=49.0128,
        longitude=2.55,
        tz="Europe/Paris",
    ),
}


def booking(**fields: object) -> Booking:
    base: dict[str, object] = {
        "id": 7,
        "source": "email",
        "marketing_carrier": "DL",
        "marketing_number": "1234",
        "origin_iata": "JFK",
        "dest_iata": "LAX",
        # 15:00 in New York.
        "scheduled_departure_utc": datetime(2026, 9, 12, 19, 0, tzinfo=UTC),
        # 15:20 in Los Angeles.
        "scheduled_arrival_utc": datetime(2026, 9, 12, 22, 20, tzinfo=UTC),
        "status": "active",
        "confirmation_code": "ABC123",
        "seat": "14A",
    }
    base.update(fields)
    return Booking(**base)


def snapshot(**fields: object) -> FlightSnapshot:
    base: dict[str, object] = {"raw": {}, "cancelled": False, "diverted": False}
    base.update(fields)
    return FlightSnapshot(**base)


def body_for(**fields: object) -> dict:
    return event_body(booking(**fields), None, AIRPORTS, base_url=BASE_URL)


def test_normal_flight() -> None:
    body = event_body(
        booking(),
        snapshot(gate_origin="B22", terminal_origin="4", baggage_claim="3"),
        AIRPORTS,
        base_url=BASE_URL,
    )

    assert body["summary"] == "DL1234 JFK -> LAX"
    assert body["location"] == "John F Kennedy International Airport, New York, US"
    assert body["start"] == {"dateTime": "2026-09-12T15:00:00", "timeZone": "America/New_York"}
    assert body["end"] == {"dateTime": "2026-09-12T15:20:00", "timeZone": "America/Los_Angeles"}
    assert body["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": REMINDER_MINUTES}],
    }
    assert "status" not in body

    description = body["description"]
    assert "Confirmation: ABC123" in description
    assert "Seat: 14A" in description
    assert "Gate: B22 (Terminal 4)" in description
    assert "Baggage claim: 3" in description
    assert description.endswith(f"\n\n{BASE_URL}/f/7")


def test_missing_values_are_omitted_not_printed() -> None:
    body = body_for(confirmation_code=None, seat=None)
    description = body["description"]
    assert "None" not in description
    assert "Confirmation" not in description
    assert "Seat" not in description


def test_cancelled_flight_is_patched_never_deleted() -> None:
    body = event_body(booking(), snapshot(cancelled=True), AIRPORTS, base_url=BASE_URL)
    assert body["summary"] == "CANCELLED - DL1234 JFK -> LAX"
    assert body["status"] == "cancelled"
    # Still a full event: the trip stays visible where it was planned.
    assert body["start"]["dateTime"] == "2026-09-12T15:00:00"


def test_overnight_flight_ends_the_next_day() -> None:
    body = event_body(
        booking(
            dest_iata="CDG",
            # 23:00 in New York, arriving 12:30 the next day in Paris.
            scheduled_departure_utc=datetime(2026, 9, 13, 3, 0, tzinfo=UTC),
            scheduled_arrival_utc=datetime(2026, 9, 13, 10, 30, tzinfo=UTC),
        ),
        None,
        AIRPORTS,
        base_url=BASE_URL,
    )
    assert body["start"]["dateTime"] == "2026-09-12T23:00:00"
    assert body["end"]["dateTime"] == "2026-09-13T12:30:00"


@pytest.mark.parametrize("end_iata", ["LAX", "CDG"])
def test_each_end_carries_its_own_zone_and_no_offset(end_iata: str) -> None:
    body = body_for(dest_iata=end_iata)

    assert body["start"]["timeZone"] == "America/New_York"
    assert body["end"]["timeZone"] == AIRPORTS[end_iata].tz

    for edge in ("start", "end"):
        stamp = body[edge]["dateTime"]
        # A `Z` pins the wrong wall clock and a bare local time floats; the only correct
        # spelling is naive local plus an explicit timeZone.
        assert not stamp.endswith("Z")
        assert "+" not in stamp
        assert datetime.fromisoformat(stamp).tzinfo is None


def test_unknown_airport_falls_back_without_inventing_a_zone() -> None:
    body = body_for(origin_iata="ZZZ")
    assert body["start"]["timeZone"] == "UTC"
    assert body["location"] == "ZZZ"
