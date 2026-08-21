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

from flighter.models import Airport, Booking, BookingStatus, FlightSnapshot
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
from flighter.views import (
    FlightView,
    Milestone,
    Status,
    clock,
    day_word,
    destination_iata,
    milestone,
    milestone_label,
    until,
    zone,
)

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


# --- what there is to watch ------------------------------------------------------------


def test_a_flight_days_out_or_called_off_has_nothing_to_watch_yet() -> None:
    """Gate boxes and a countdown are for a flight inside its day."""
    assert not view(booking(), None).watched
    assert not view(booking(), snapshot(cancelled=True)).watched
    assert view(booking(), snapshot(actual_out=DEPARTURE, actual_off=DEPARTURE)).watched


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


def test_the_day_is_named_by_the_calendar_at_the_origin() -> None:
    """The window is a rolling day, so a morning flight enters it the morning before."""
    # 20:00 in New York on the 12th, where the clock is four hours behind UTC.
    evening = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)
    assert day_word(evening + timedelta(hours=3), evening, "America/New_York") == "Today"
    assert day_word(evening + timedelta(hours=6), evening, "America/New_York") == "Tomorrow"
    assert day_word(evening + timedelta(hours=6), evening, "UTC") == "Today"
    assert day_word(evening - timedelta(hours=22), evening, "America/New_York") is None


def test_a_flight_inside_its_day_without_a_feed_says_which_day() -> None:
    tomorrow = view(booking(scheduled_departure_utc=now_ish(hours=23)), None)
    assert tomorrow.status.tone == "plan"
    assert tomorrow.status.label in ("Today", "Tomorrow")
    assert tomorrow.status.label == day_word(tomorrow.departure, datetime.now(UTC), JFK.tz)

    far = view(booking(scheduled_departure_utc=now_ish(hours=25)), None)
    assert far.status == Status("Scheduled", "quiet")


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


def test_a_flight_that_has_landed_is_drawn_all_the_way_there() -> None:
    """The feed's figure stops at the last poll, which may be short of the runway."""
    leaves = now_ish(hours=-3)
    landed = snapshot(
        actual_out=leaves,
        actual_off=leaves + timedelta(minutes=15),
        actual_on=now_ish(minutes=-5),
        progress_percent=92,
    )
    assert view(booking(scheduled_departure_utc=leaves), landed).progress_percent == 100
    flown = booking(scheduled_departure_utc=now_ish(days=-3), status=BookingStatus.COMPLETED)
    assert view(flown, None).progress_percent == 100
    airborne = snapshot(
        actual_out=leaves,
        actual_off=leaves + timedelta(minutes=15),
        estimated_on=now_ish(hours=1),
        progress_percent=92,
    )
    assert view(booking(scheduled_departure_utc=leaves), airborne).progress_percent < 100


# --- arrived, and a milestone whose time has passed ----------------------------------------


def test_at_the_gate_is_arrived_not_merely_landed() -> None:
    leaves = now_ish(hours=-3)
    down = snapshot(actual_out=leaves, actual_off=leaves, actual_on=now_ish(minutes=-12))
    assert view(booking(scheduled_departure_utc=leaves), down).status == ("Landed", "ok")
    parked = snapshot(
        actual_out=leaves, actual_off=leaves, actual_on=now_ish(minutes=-12), actual_in=now_ish()
    )
    assert view(booking(scheduled_departure_utc=leaves), parked).status == ("Arrived", "ok")


def test_a_milestone_past_its_time_reads_as_due_rather_than_ago() -> None:
    now = datetime(2026, 9, 12, 22, 0, tzinfo=UTC)
    ahead = Milestone("At the gate in", now + timedelta(minutes=5))
    assert milestone_label(ahead, now) == "At the gate in"
    behind = Milestone("At the gate in", now - timedelta(minutes=18))
    assert milestone_label(behind, now) == "Due at the gate"
    assert milestone_label(Milestone("Lands in", now), now) == "Due to land"
    assert milestone_label(Milestone("Departs in", now), now) == "Due to depart"
    assert milestone_label(Milestone("Scheduled", now), now) == "Scheduled"


def test_the_view_swaps_the_words_once_the_time_has_gone() -> None:
    leaves = now_ish(hours=-3)
    overdue = snapshot(
        actual_out=leaves,
        actual_off=leaves,
        actual_on=now_ish(hours=-1),
        estimated_in=now_ish(minutes=-18),
    )
    v = view(booking(scheduled_departure_utc=leaves), overdue)
    assert v.milestone_label == "Due at the gate"
    assert v.milestone_due == "Due at the gate"


def test_a_flight_the_feed_lost_is_flown_with_nothing_left_to_count_to() -> None:
    """The poller closes a booking it never saw land, and the last snapshot stands.

    Left to itself the card would read "Due to land 3d ago", and keep growing.
    """
    leaves = now_ish(days=-3)
    flown = booking(scheduled_departure_utc=leaves, status=BookingStatus.COMPLETED)
    lost_airborne = snapshot(
        actual_out=leaves,
        actual_off=leaves + timedelta(minutes=12),
        estimated_on=leaves + timedelta(hours=5),
        progress_percent=40,
    )
    v = view(flown, lost_airborne)
    assert v.flown
    assert v.status == ("Flown", "quiet")
    assert v.milestone is None and v.milestone_label is None
    assert v.airborne_window is None
    assert v.progress_percent == 100

    lost_taxiing = snapshot(actual_out=leaves, estimated_in=leaves + timedelta(hours=5))
    assert view(flown, lost_taxiing).status == ("Flown", "quiet")
    assert view(flown, lost_taxiing).milestone is None

    never_seen = view(flown, None)
    assert never_seen.status == ("Flown", "quiet")
    assert never_seen.milestone is None
    assert never_seen.block_time is None


def test_a_flown_flight_that_was_seen_to_land_keeps_saying_so() -> None:
    leaves = now_ish(days=-3)
    flown = booking(scheduled_departure_utc=leaves, status=BookingStatus.COMPLETED)
    down = snapshot(
        actual_out=leaves,
        actual_off=leaves,
        actual_on=leaves + timedelta(hours=5),
        estimated_in=leaves + timedelta(hours=5, minutes=10),
    )
    assert view(flown, down).status == ("Landed", "ok")
    assert view(flown, down).milestone is None


# --- where a diverted flight is going --------------------------------------------------------

YOW = Airport(iata="YOW", name="Ottawa", city="Ottawa", country="CA", tz="America/Toronto")


def test_a_diversion_moves_the_destination_and_its_zone() -> None:
    booked = booking()
    diverted = snapshot(diverted=True, destination_iata="YOW", actual_off=DEPARTURE)
    v = FlightView(booked, diverted, JFK, LAX, diversion=YOW)
    assert v.diverted_to == "YOW"
    assert v.destination_iata == "YOW"
    assert v.destination is YOW
    assert v.dest_tz == "America/Toronto"
    assert v.dest is LAX


def test_a_flight_on_its_booked_route_has_no_diversion() -> None:
    booked = booking()
    assert view(booked, snapshot(destination_iata="LAX")).diverted_to is None
    # Flagged but not yet told where: still the booked airport, without pretending.
    v = view(booked, snapshot(diverted=True))
    assert v.diverted_to is None and v.destination_iata == "LAX"
    assert destination_iata(booked, None) == "LAX"
