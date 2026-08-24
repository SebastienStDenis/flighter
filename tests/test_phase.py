"""Phase boundaries. Every branch here is one a traveller notices when it is wrong."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flighter.models import Booking, FlightSnapshot
from flighter.phase import (
    AIRBORNE,
    ARRIVAL_DELAY_THRESHOLD,
    CANCELLED,
    DAY_OF,
    DEPARTURE_DELAY_THRESHOLD,
    DIVERTED,
    LANDED,
    TAXIING,
    UPCOMING,
    airborne_window,
    arrival_estimate,
    compute_phase,
    departure_estimate,
    expected_progress,
    expected_window,
    landing_estimate,
    progress_estimate,
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


def test_the_last_half_hour_is_still_counting_down_to_departure() -> None:
    """Nothing upstream reports boarding, so the run-up to departure stays day_of and
    keeps its countdown rather than being replaced by a word."""
    assert compute_phase(booking(), None, DEPARTURE - timedelta(minutes=30)) == DAY_OF
    assert compute_phase(booking(), None, DEPARTURE - timedelta(minutes=2)) == DAY_OF


def test_pushback_is_taxiing_until_wheels_up() -> None:
    pushed = snapshot(scheduled_out=DEPARTURE, actual_out=DEPARTURE + timedelta(minutes=4))
    assert compute_phase(booking(), pushed, DEPARTURE + timedelta(minutes=6)) == TAXIING


def test_wheels_up_beats_pushback() -> None:
    off = snapshot(
        scheduled_out=DEPARTURE,
        actual_out=DEPARTURE + timedelta(minutes=4),
        actual_off=DEPARTURE + timedelta(minutes=18),
    )
    assert compute_phase(booking(), off, DEPARTURE + timedelta(minutes=20)) == AIRBORNE


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


def test_departure_estimate_falls_back_to_the_booking() -> None:
    assert departure_estimate(booking(), None) == DEPARTURE
    assert departure_estimate(booking(), snapshot()) == DEPARTURE
    assert departure_estimate(booking(), snapshot(scheduled_out=DEPARTURE)) == DEPARTURE
    late = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(minutes=25))
    assert departure_estimate(booking(), late) == DEPARTURE + timedelta(minutes=25)


def test_departure_estimate_prefers_what_actually_happened() -> None:
    """Once the flight has pushed back, the estimate is history; the calendar block and
    the page both want the time it left."""
    left = snapshot(
        scheduled_out=DEPARTURE,
        estimated_out=DEPARTURE + timedelta(minutes=25),
        actual_out=DEPARTURE + timedelta(minutes=31),
    )
    assert departure_estimate(booking(), left) == DEPARTURE + timedelta(minutes=31)


def test_delay_thresholds_agree_with_the_industry() -> None:
    """Anything under a quarter hour is on time, to the airline statistic, the page, the
    widget and the push alike."""
    assert timedelta(minutes=15) == DEPARTURE_DELAY_THRESHOLD
    assert timedelta(minutes=15) == ARRIVAL_DELAY_THRESHOLD


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


def test_progress_is_read_off_the_clock_while_airborne() -> None:
    """The feed's figure is true only at the poll; the clock is true at every instant."""
    flying = snapshot(
        actual_off=DEPARTURE, scheduled_on=ARRIVAL, estimated_on=ARRIVAL, progress_percent=10
    )
    quarter = DEPARTURE + (ARRIVAL - DEPARTURE) / 4
    assert airborne_window(booking(), flying, quarter) == (DEPARTURE, ARRIVAL)
    assert progress_estimate(booking(), flying, quarter) == 25


def test_progress_follows_the_landing_estimate_as_it_moves() -> None:
    flying = snapshot(
        actual_off=DEPARTURE, scheduled_on=ARRIVAL, estimated_on=ARRIVAL + timedelta(hours=1)
    )
    halfway = DEPARTURE + timedelta(hours=2, minutes=17, seconds=30)
    assert progress_estimate(booking(), flying, halfway) == 50


def test_progress_never_runs_past_the_destination() -> None:
    flying = snapshot(actual_off=DEPARTURE, estimated_on=ARRIVAL)
    assert progress_estimate(booking(), flying, ARRIVAL + timedelta(minutes=20)) == 100


def test_progress_on_the_ground_is_whatever_the_feed_said() -> None:
    assert progress_estimate(booking(), None, DEPARTURE) is None
    waiting = snapshot(scheduled_out=DEPARTURE, progress_percent=0)
    assert airborne_window(booking(), waiting, DEPARTURE - timedelta(hours=1)) is None
    assert progress_estimate(booking(), waiting, DEPARTURE - timedelta(hours=1)) == 0
    landed = snapshot(actual_off=DEPARTURE, actual_on=ARRIVAL, progress_percent=100)
    assert progress_estimate(booking(), landed, ARRIVAL + timedelta(minutes=5)) == 100


def test_a_diverted_flight_is_still_a_flight_in_the_air() -> None:
    """Where it is going changed; that it is going there has not. The rule under it has
    an aircraft to place, and the clock is what places it here as anywhere else."""
    diverted = snapshot(
        actual_off=DEPARTURE, estimated_on=ARRIVAL, diverted=True, progress_percent=61
    )
    halfway = DEPARTURE + (ARRIVAL - DEPARTURE) / 2
    assert airborne_window(booking(), diverted, halfway) == (DEPARTURE, ARRIVAL)
    assert progress_estimate(booking(), diverted, halfway) == 50


def test_a_diverted_flight_that_is_down_is_out_of_the_air() -> None:
    """Diverted outranks landed in the phase, because where it landed is the point. It
    does not outrank the wheels: a flight the feed has seen arrive is not still flying."""
    landed = snapshot(actual_off=DEPARTURE, actual_on=ARRIVAL, diverted=True, progress_percent=88)
    assert airborne_window(booking(), landed, ARRIVAL + timedelta(minutes=5)) is None


def test_the_ticket_places_the_aircraft_when_nothing_has_been_seen() -> None:
    """A flight imported while it is already flying has no snapshot until the poller's
    first look, and one whose feed has gone quiet may never get another. Neither is at
    the airport it has plainly left."""
    quarter = DEPARTURE + (ARRIVAL - DEPARTURE) / 4
    assert expected_window(booking(), None, quarter) == (DEPARTURE, ARRIVAL)
    assert expected_progress(booking(), None, quarter) == 25
    # Nothing is claimed about a flight that is not due out yet: the rule has the length
    # of the hop to say for that one.
    assert expected_window(booking(), None, DEPARTURE - timedelta(minutes=1)) is None
    assert expected_progress(booking(), None, DEPARTURE - timedelta(minutes=1)) is None


def test_the_ticket_follows_the_estimate_it_is_held_against() -> None:
    """A delay the feed did state moves the span with it, so the aircraft is not placed
    against a departure everybody already knows has slipped."""
    held = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(hours=1))
    assert expected_window(booking(), held, DEPARTURE + timedelta(minutes=30)) is None
    assert expected_progress(booking(), held, DEPARTURE + timedelta(hours=1)) == 0


def test_progress_falls_back_to_the_feed_when_the_times_make_no_span() -> None:
    """A landing estimate that has not caught up with a late take-off is not a span."""
    stale = snapshot(
        actual_off=ARRIVAL + timedelta(minutes=5), estimated_on=ARRIVAL, progress_percent=3
    )
    later = ARRIVAL + timedelta(minutes=30)
    assert airborne_window(booking(), stale, later) is None
    assert progress_estimate(booking(), stale, later) == 3
    unknown = snapshot(actual_off=DEPARTURE, progress_percent=40)
    assert progress_estimate(booking(scheduled_arrival_utc=None), unknown, later) == 40
