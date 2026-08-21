"""The formatting the card leans on: a time split from its zone, and the length of a hop
the rule shows before there is anything to measure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flighter.models import Airport, Booking, FlightSnapshot
from flighter.views import FlightView, clock, zone

NOW = datetime.now(UTC)
DEPARTURE = NOW + timedelta(days=2)
ARRIVAL = DEPARTURE + timedelta(hours=5, minutes=20)

YUL = Airport(
    iata="YUL", name="Montreal-Trudeau", city="Montreal", country="CA", tz="America/Toronto"
)
LHR = Airport(iata="LHR", name="London Heathrow", city="London", country="GB", tz="Europe/London")


def booking(**kwargs: object) -> Booking:
    defaults: dict[str, object] = {
        "id": 1,
        "source": "manual",
        "marketing_carrier": "AC",
        "marketing_number": "871",
        "origin_iata": "YUL",
        "dest_iata": "LHR",
        "scheduled_departure_utc": DEPARTURE,
        "scheduled_arrival_utc": ARRIVAL,
        "status": "active",
    }
    return Booking(**(defaults | kwargs))


def snapshot(**kwargs: object) -> FlightSnapshot:
    defaults: dict[str, object] = {"booking_id": 1, "raw": {}}
    return FlightSnapshot(**(defaults | kwargs))


def view(b: Booking, s: FlightSnapshot | None) -> FlightView:
    return FlightView(booking=b, snapshot=s, origin=YUL, dest=LHR)


def test_the_clock_and_the_zone_are_the_two_halves_of_one_time() -> None:
    instant = datetime(2026, 9, 12, 22, 40, tzinfo=UTC)
    assert clock(instant, "America/Toronto") == "18:40"
    assert zone(instant, "America/Toronto") == "EDT"
    assert clock(instant, "America/Toronto", with_date=True) == "Sat 12 Sep 18:40"


def test_the_zone_follows_the_clocks_changing() -> None:
    """The same airport is EST in January, so the zone is read at the instant shown."""
    winter = datetime(2026, 1, 12, 22, 40, tzinfo=UTC)
    assert zone(winter, "America/Toronto") == "EST"
    assert clock(winter, "America/Toronto") == "17:40"


def test_a_time_nobody_has_stated_is_a_dash_with_no_zone() -> None:
    assert clock(None, "America/Toronto") == "-"
    assert zone(None, "America/Toronto") == ""


def test_block_time_is_gate_to_gate_as_currently_expected() -> None:
    assert view(booking(), None).block_time == timedelta(hours=5, minutes=20)
    later = snapshot(scheduled_in=ARRIVAL, estimated_in=ARRIVAL + timedelta(minutes=30))
    assert view(booking(), later).block_time == timedelta(hours=5, minutes=50)


def test_block_time_needs_an_arrival_to_count_to() -> None:
    assert view(booking(scheduled_arrival_utc=None), None).block_time is None
    backwards = snapshot(estimated_in=DEPARTURE - timedelta(hours=1))
    assert view(booking(), backwards).block_time is None


def test_block_time_stops_once_the_flight_is_under_way() -> None:
    """From pushback on, the aircraft's place on the rule is the answer."""
    taxiing = snapshot(actual_out=NOW - timedelta(minutes=5))
    assert view(booking(scheduled_departure_utc=NOW), taxiing).block_time is None
    airborne = snapshot(actual_out=NOW - timedelta(hours=1), actual_off=NOW - timedelta(minutes=50))
    assert view(booking(scheduled_departure_utc=NOW), airborne).block_time is None
    assert view(booking(), snapshot(cancelled=True)).block_time is None
