"""What the cards count to, what the pill says while they do, and the formatting the
card leans on: a time split from its zone, and the length of a hop the rule shows before
there is anything to measure."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from flighter.models import Airport, Booking, FlightSnapshot
from flighter.phase import (
    AIRBORNE,
    CANCELLED,
    DAY_OF,
    DIVERTED,
    LANDED,
    TAXIING,
    UPCOMING,
    compute_phase,
)
from flighter.views import FlightView, Milestone, clock, milestone, until, zone

DEPARTURE = datetime(2026, 9, 12, 18, 40, tzinfo=UTC)
ARRIVAL = datetime(2026, 9, 12, 22, 15, tzinfo=UTC)

JFK = Airport(iata="JFK", name="Kennedy", city="New York", country="US", tz="America/New_York")
LAX = Airport(
    iata="LAX", name="Los Angeles", city="Los Angeles", country="US", tz="America/Los_Angeles"
)

PAGE_SCRIPT = Path(__file__).parents[1] / "src" / "flighter" / "templates" / "base.html"


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


def view(booked: Booking, snap: FlightSnapshot | None) -> FlightView:
    return FlightView(booking=booked, snapshot=snap, origin=JFK, dest=LAX)


# --- the milestone ---------------------------------------------------------------------


def test_days_out_is_scheduled_rather_than_counted_down() -> None:
    assert milestone(UPCOMING, booking(), None) == Milestone("Scheduled", DEPARTURE)


def test_on_the_day_the_card_counts_to_departure() -> None:
    moved = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(minutes=20))
    assert milestone(DAY_OF, booking(), moved) == Milestone(
        "Departs in", DEPARTURE + timedelta(minutes=20)
    )


def test_pushback_skips_wheels_up_and_counts_to_the_landing() -> None:
    """Nobody upstream estimates wheels up, so the next rung with a time is the landing."""
    taxiing = snapshot(
        actual_out=DEPARTURE + timedelta(minutes=3),
        estimated_on=ARRIVAL - timedelta(minutes=9),
    )
    assert milestone(TAXIING, booking(), taxiing) == Milestone(
        "Lands in", ARRIVAL - timedelta(minutes=9)
    )


def test_in_the_air_counts_to_touchdown_not_the_gate() -> None:
    airborne = snapshot(
        actual_out=DEPARTURE,
        actual_off=DEPARTURE + timedelta(minutes=12),
        estimated_on=ARRIVAL - timedelta(minutes=9),
        estimated_in=ARRIVAL,
    )
    assert milestone(AIRBORNE, booking(), airborne) == Milestone(
        "Lands in", ARRIVAL - timedelta(minutes=9)
    )


def test_without_a_runway_time_the_landing_is_the_gate_time() -> None:
    airborne = snapshot(actual_out=DEPARTURE, actual_off=DEPARTURE + timedelta(minutes=12))
    assert milestone(AIRBORNE, booking(), airborne) == Milestone("Lands in", ARRIVAL)


def test_wheels_down_counts_to_the_gate() -> None:
    landed = snapshot(
        actual_off=DEPARTURE + timedelta(minutes=12),
        actual_on=ARRIVAL - timedelta(minutes=10),
        estimated_in=ARRIVAL + timedelta(minutes=4),
    )
    assert milestone(LANDED, booking(), landed) == Milestone(
        "At the gate in", ARRIVAL + timedelta(minutes=4)
    )


def test_at_the_gate_there_is_nothing_left_to_count() -> None:
    done = snapshot(actual_off=DEPARTURE, actual_on=ARRIVAL, actual_in=ARRIVAL)
    assert milestone(LANDED, booking(), done) is None


def test_a_diversion_still_lands_somewhere() -> None:
    diverted = snapshot(diverted=True, actual_off=DEPARTURE, estimated_on=ARRIVAL)
    assert milestone(DIVERTED, booking(), diverted) == Milestone("Lands in", ARRIVAL)


def test_nothing_known_ahead_is_no_milestone_rather_than_a_guess() -> None:
    no_arrival = booking(scheduled_arrival_utc=None)
    taxiing = snapshot(actual_out=DEPARTURE)
    assert milestone(TAXIING, no_arrival, taxiing) is None
    assert milestone(LANDED, no_arrival, snapshot(actual_on=ARRIVAL)) is None


def test_a_cancelled_flight_counts_to_nothing() -> None:
    assert milestone(CANCELLED, booking(), snapshot(cancelled=True)) is None


def test_the_view_asks_the_same_question() -> None:
    soon = datetime.now(UTC) + timedelta(hours=3)
    v = view(
        booking(scheduled_departure_utc=soon, scheduled_arrival_utc=soon + timedelta(hours=5)), None
    )
    assert compute_phase(v.booking, None, datetime.now(UTC)) == DAY_OF
    assert v.milestone == Milestone("Departs in", soon)


# --- the figure -------------------------------------------------------------------------


FIGURES = [
    (timedelta(days=3, hours=5), "3d"),
    (timedelta(hours=24, minutes=1), "1d"),
    (timedelta(hours=23, minutes=59, seconds=30), "23h 59m"),
    (timedelta(hours=1, minutes=5, seconds=30), "1h 05m"),
    (timedelta(minutes=12, seconds=30), "12m"),
    (timedelta(seconds=40), "<1m"),
    (timedelta(minutes=-20, seconds=-30), "20m ago"),
    (timedelta(seconds=-20), "<1m ago"),
]


@pytest.mark.parametrize(("ahead", "figure"), FIGURES)
def test_the_figure_never_shows_seconds_and_only_days_past_a_day(
    ahead: timedelta, figure: str
) -> None:
    assert until(datetime.now(UTC) + ahead) == figure


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_page_script_builds_the_same_figure() -> None:
    """The server paints first and the script repaints; the two must never disagree."""
    script = PAGE_SCRIPT.read_text()
    start = script.index("function figure(ms)")
    figure = script[start : script.index("function tick()", start)]
    offsets = [int(ahead.total_seconds() * 1000) for ahead, _ in FIGURES]
    program = (
        f"{figure} console.log(JSON.stringify({json.dumps(offsets)}.map("
        'function (ms) { return figure(ms) + (ms < 0 ? " ago" : ""); })));'
    )
    rendered = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(rendered) == [figure for _, figure in FIGURES]


# --- the pill ---------------------------------------------------------------------------


def now_ish(**shift: float) -> datetime:
    return datetime.now(UTC) + timedelta(**shift)


def test_a_late_departure_is_a_departure_delay_until_pushback() -> None:
    scheduled = now_ish(hours=2)
    waiting = snapshot(scheduled_out=scheduled, estimated_out=scheduled + timedelta(minutes=30))
    v = view(booking(scheduled_departure_utc=scheduled), waiting)
    assert v.status.label == "Departure delayed"
    assert v.status.tone == "warn"


def test_a_late_pushback_is_history_once_the_aircraft_is_moving() -> None:
    scheduled = now_ish(minutes=-40)
    rolling = snapshot(
        scheduled_out=scheduled,
        actual_out=scheduled + timedelta(minutes=30),
        scheduled_in=now_ish(hours=5),
        estimated_in=now_ish(hours=5),
    )
    v = view(booking(scheduled_departure_utc=scheduled), rolling)
    assert v.status.label == "Taxiing"

    flying = snapshot(
        scheduled_out=scheduled,
        actual_out=scheduled + timedelta(minutes=30),
        actual_off=scheduled + timedelta(minutes=38),
        scheduled_in=now_ish(hours=5),
        estimated_in=now_ish(hours=5),
    )
    v = view(booking(scheduled_departure_utc=scheduled), flying)
    assert v.status.label == "In the air"
    assert v.status.tone == "live"


def test_in_the_air_the_pill_judges_the_arrival() -> None:
    scheduled = now_ish(minutes=-40)
    gets_in = now_ish(hours=5)
    late = snapshot(
        scheduled_out=scheduled,
        actual_out=scheduled + timedelta(minutes=30),
        actual_off=scheduled + timedelta(minutes=38),
        scheduled_in=gets_in,
        estimated_in=gets_in + timedelta(minutes=25),
    )
    v = view(booking(scheduled_departure_utc=scheduled, scheduled_arrival_utc=gets_in), late)
    assert v.status == ("Arriving late", "warn")

    recovered = snapshot(
        scheduled_out=scheduled,
        actual_out=scheduled + timedelta(minutes=30),
        actual_off=scheduled + timedelta(minutes=38),
        scheduled_in=gets_in,
        estimated_in=gets_in + timedelta(minutes=10),
    )
    v = view(booking(scheduled_departure_utc=scheduled, scheduled_arrival_utc=gets_in), recovered)
    assert v.status == ("In the air", "live")


def test_landed_is_landed_however_late_it_left() -> None:
    scheduled = now_ish(hours=-6)
    down = snapshot(
        scheduled_out=scheduled,
        actual_out=scheduled + timedelta(hours=1),
        actual_off=scheduled + timedelta(hours=1, minutes=10),
        actual_on=now_ish(minutes=-5),
    )
    v = view(booking(scheduled_departure_utc=scheduled), down)
    assert v.status == ("Landed", "ok")


# --- the clock and the zone -------------------------------------------------------------


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


# --- the block time ---------------------------------------------------------------------


def test_block_time_is_gate_to_gate_as_currently_expected() -> None:
    leaves = now_ish(days=2)
    gets_in = leaves + timedelta(hours=5, minutes=20)
    booked = booking(scheduled_departure_utc=leaves, scheduled_arrival_utc=gets_in)
    assert view(booked, None).block_time == timedelta(hours=5, minutes=20)
    later = snapshot(scheduled_in=gets_in, estimated_in=gets_in + timedelta(minutes=30))
    assert view(booked, later).block_time == timedelta(hours=5, minutes=50)


def test_block_time_needs_an_arrival_to_count_to() -> None:
    leaves = now_ish(days=2)
    no_arrival = booking(scheduled_departure_utc=leaves, scheduled_arrival_utc=None)
    assert view(no_arrival, None).block_time is None
    backwards = snapshot(estimated_in=leaves - timedelta(hours=1))
    assert view(booking(scheduled_departure_utc=leaves), backwards).block_time is None


def test_block_time_stops_once_the_flight_is_under_way() -> None:
    """From pushback on, the aircraft's place on the rule is the answer."""
    leaves = now_ish()
    taxiing = snapshot(actual_out=leaves - timedelta(minutes=5))
    assert view(booking(scheduled_departure_utc=leaves), taxiing).block_time is None
    airborne = snapshot(
        actual_out=leaves - timedelta(hours=1), actual_off=leaves - timedelta(minutes=50)
    )
    assert view(booking(scheduled_departure_utc=leaves), airborne).block_time is None
    cancelled = snapshot(cancelled=True)
    assert view(booking(scheduled_departure_utc=now_ish(days=2)), cancelled).block_time is None
