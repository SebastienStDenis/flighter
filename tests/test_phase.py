"""Phase boundaries. Every branch here is one a traveller notices when it is wrong."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from flight_tracker.models import Booking, FlightSnapshot
from flight_tracker.phase import (
    AIRBORNE,
    BOARDING,
    CANCELLED,
    DAY_OF,
    DIVERTED,
    LANDED,
    UPCOMING,
    arrival_estimate,
    boarding_time,
    compute_phase,
    departure_estimate,
    landing_estimate,
)

DEPARTURE = datetime(2026, 9, 12, 18, 40, tzinfo=UTC)
ARRIVAL = datetime(2026, 9, 12, 22, 15, tzinfo=UTC)


def booking(**kwargs: object) -> Booking:
    defaults: dict[str, object] = {
        "id": 1,
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


def snapshot(**kwargs: object) -> FlightSnapshot:
    defaults: dict[str, object] = {"booking_id": 1, "raw": {}}
    return FlightSnapshot(**(defaults | kwargs))


def test_days_out_is_upcoming() -> None:
    assert compute_phase(booking(), None, DEPARTURE - timedelta(days=3)) == UPCOMING


def test_all_null_snapshot_days_out_is_upcoming() -> None:
    """A booking polled before AeroAPI knows anything still has a phase."""
    assert compute_phase(booking(), snapshot(), DEPARTURE - timedelta(days=3)) == UPCOMING


def test_exactly_24h_out_is_day_of() -> None:
    assert compute_phase(booking(), None, DEPARTURE - timedelta(hours=24)) == DAY_OF


def test_just_over_24h_out_is_upcoming() -> None:
    assert compute_phase(booking(), None, DEPARTURE - timedelta(hours=24, seconds=1)) == UPCOMING


def test_boarding_starts_exactly_30_minutes_before_departure() -> None:
    assert compute_phase(booking(), None, DEPARTURE - timedelta(minutes=30)) == BOARDING
    assert compute_phase(booking(), None, DEPARTURE - timedelta(minutes=30, seconds=1)) == DAY_OF


def test_boarding_follows_the_estimate_not_the_schedule() -> None:
    """An hour-late flight must not claim to be boarding on the original schedule."""
    delayed = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(hours=1))
    assert compute_phase(booking(), delayed, DEPARTURE - timedelta(minutes=20)) == DAY_OF
    assert compute_phase(booking(), delayed, DEPARTURE + timedelta(minutes=40)) == BOARDING


def test_pushback_is_boarding_until_wheels_up() -> None:
    pushed = snapshot(scheduled_out=DEPARTURE, actual_out=DEPARTURE + timedelta(minutes=4))
    assert compute_phase(booking(), pushed, DEPARTURE + timedelta(minutes=6)) == BOARDING


def test_wheels_up_is_airborne() -> None:
    flying = snapshot(
        scheduled_out=DEPARTURE,
        actual_out=DEPARTURE,
        actual_off=DEPARTURE + timedelta(minutes=12),
    )
    assert compute_phase(booking(), flying, DEPARTURE + timedelta(hours=1)) == AIRBORNE


def test_wheels_down_is_landed() -> None:
    down = snapshot(actual_off=DEPARTURE, actual_on=ARRIVAL)
    assert compute_phase(booking(), down, ARRIVAL + timedelta(minutes=1)) == LANDED


def test_gate_in_without_wheels_down_is_still_landed() -> None:
    assert compute_phase(booking(), snapshot(actual_in=ARRIVAL), ARRIVAL) == LANDED


def test_cancelled_beats_every_other_signal() -> None:
    """Cancelled outranks even a snapshot that claims the flight took off and landed."""
    confused = snapshot(
        cancelled=True,
        diverted=True,
        actual_out=DEPARTURE,
        actual_off=DEPARTURE,
        actual_on=ARRIVAL,
        actual_in=ARRIVAL,
    )
    assert compute_phase(booking(), confused, ARRIVAL) == CANCELLED
    assert compute_phase(booking(), confused, DEPARTURE - timedelta(days=5)) == CANCELLED


def test_diverted_beats_landed() -> None:
    diverted = snapshot(diverted=True, actual_off=DEPARTURE, actual_on=ARRIVAL)
    assert compute_phase(booking(), diverted, ARRIVAL) == DIVERTED


def test_naive_datetimes_are_read_as_utc() -> None:
    naive_now = DEPARTURE.replace(tzinfo=None) - timedelta(hours=2)
    assert compute_phase(booking(), None, naive_now) == DAY_OF


@pytest.mark.parametrize(
    ("snap", "expected"),
    [
        (None, None),
        (snapshot(), None),
        (snapshot(scheduled_out=DEPARTURE), DEPARTURE - timedelta(minutes=30)),
        (
            snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(hours=1)),
            DEPARTURE + timedelta(minutes=30),
        ),
        (snapshot(scheduled_out=DEPARTURE, actual_out=DEPARTURE), DEPARTURE),
    ],
)
def test_boarding_time(snap: FlightSnapshot | None, expected: datetime | None) -> None:
    assert boarding_time(snap) == expected


def test_departure_estimate_falls_back_to_the_booking() -> None:
    assert departure_estimate(booking(), None) == DEPARTURE
    assert departure_estimate(booking(), snapshot()) == DEPARTURE
    assert departure_estimate(booking(), snapshot(scheduled_out=DEPARTURE)) == DEPARTURE
    late = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(minutes=25))
    assert departure_estimate(booking(), late) == DEPARTURE + timedelta(minutes=25)


def test_arrival_estimate_prefers_the_estimate_then_the_schedule() -> None:
    assert arrival_estimate(booking(), None) == ARRIVAL
    assert arrival_estimate(booking(scheduled_arrival_utc=None), None) is None
    early = snapshot(scheduled_in=ARRIVAL, estimated_in=ARRIVAL - timedelta(minutes=8))
    assert arrival_estimate(booking(), early) == ARRIVAL - timedelta(minutes=8)


# --- Landing versus gate arrival -----------------------------------------------------
# The distinction the whole arrival story rests on: in the air you are counting to the
# runway, on the ground you are counting to the door.


def test_landing_targets_the_runway_and_arrival_targets_the_gate() -> None:
    touchdown = ARRIVAL - timedelta(minutes=12)
    snap = snapshot(estimated_on=touchdown, estimated_in=ARRIVAL)
    assert landing_estimate(booking(), snap) == touchdown
    assert arrival_estimate(booking(), snap) == ARRIVAL


def test_landing_prefers_what_actually_happened() -> None:
    touchdown = ARRIVAL - timedelta(minutes=15)
    snap = snapshot(actual_on=touchdown, estimated_on=ARRIVAL - timedelta(minutes=5))
    assert landing_estimate(booking(), snap) == touchdown


def test_landing_falls_back_to_the_gate_when_no_runway_time_exists() -> None:
    # Late but never absurd: better a countdown that runs a few minutes long than none.
    snap = snapshot(estimated_in=ARRIVAL)
    assert landing_estimate(booking(), snap) == ARRIVAL


def test_arrival_prefers_the_gate_it_actually_reached_over_touchdown() -> None:
    # The bug this replaced: once wheels were down the "arrival" jumped to touchdown,
    # while the passenger was still taxiing with the seatbelt sign on.
    at_gate = ARRIVAL + timedelta(minutes=8)
    snap = snapshot(actual_on=ARRIVAL - timedelta(minutes=10), actual_in=at_gate)
    assert arrival_estimate(booking(), snap) == at_gate


def test_landing_without_any_snapshot_is_the_booked_arrival() -> None:
    assert landing_estimate(booking(), None) == ARRIVAL
