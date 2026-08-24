"""Where a flight is in its life, decided in one place so the widget and web UI agree.

Deliberately a pure function over plain attributes: the phase drives the countdown, the
subtitle, the sort order and the notification copy, and three implementations of it that
disagree at the boundaries is how a widget ends up naming the wrong one to someone
already in the air.

Every phase here is something a snapshot observed or the clock can prove. There is no
boarding phase because no upstream source reports boarding, and the lead time varies by
carrier and airframe from about twenty-five minutes to an hour, so any single guess is
wrong for most flights. A countdown to a departure time is true at every instant and
tells the reader more than a word standing where the numbers were.

The "best known time" ladders live here for the same reason. Departure, gate arrival
and landing each have one answer, and the calendar, the pushes, the page and the widget
all read it from here rather than keeping a ladder of their own.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from .timezones import ensure_utc

Phase = Literal["upcoming", "day_of", "taxiing", "airborne", "landed", "cancelled", "diverted"]

UPCOMING: Final = "upcoming"
DAY_OF: Final = "day_of"
TAXIING: Final = "taxiing"
AIRBORNE: Final = "airborne"
LANDED: Final = "landed"
CANCELLED: Final = "cancelled"
DIVERTED: Final = "diverted"

DAY_OF_LEAD: Final = timedelta(hours=24)

# The industry calls anything under a quarter hour on time, and so does its own on-time
# statistic. Below this a change is noise: airlines nudge an estimate by a minute or two
# on every update, and nobody wants a push or a red badge for each wobble. Arrival keeps
# its own name because an arrival estimate moves all flight long and may one day earn a
# wider band than departure.
DEPARTURE_DELAY_THRESHOLD: Final = timedelta(minutes=15)
ARRIVAL_DELAY_THRESHOLD: Final = timedelta(minutes=15)


class BookingLike(Protocol):
    """What a phase decision needs off a booking; `Booking` satisfies it structurally."""

    scheduled_departure_utc: datetime
    scheduled_arrival_utc: datetime | None


class SnapshotLike(Protocol):
    """The observed fields of a snapshot, in AeroAPI's out/off/on/in vocabulary."""

    cancelled: bool | None
    diverted: bool | None
    scheduled_out: datetime | None
    estimated_out: datetime | None
    actual_out: datetime | None
    actual_off: datetime | None
    scheduled_on: datetime | None
    estimated_on: datetime | None
    actual_on: datetime | None
    scheduled_in: datetime | None
    estimated_in: datetime | None
    actual_in: datetime | None
    progress_percent: int | None


def compute_phase(booking: BookingLike, snapshot: SnapshotLike | None, now: datetime) -> Phase:
    """The flight's phase at `now`, from the newest snapshot and the booking's schedule.

    Order matters. Cancelled outranks everything: a cancelled flight that never left is
    not under way just because its scheduled time has passed. Diverted outranks landed
    for the same reason, since where it landed is the whole point.
    """
    moment = ensure_utc(now)
    if snapshot is not None:
        if snapshot.cancelled:
            return CANCELLED
        if snapshot.diverted:
            return DIVERTED
        # Wheels down is enough; gate-in only confirms it.
        if snapshot.actual_on is not None or snapshot.actual_in is not None:
            return LANDED
        if snapshot.actual_off is not None:
            return AIRBORNE
        # Pushback is observed, so it is the one thing that can be said about a flight
        # that has left the gate but is not yet off the ground.
        if snapshot.actual_out is not None:
            return TAXIING

    departure = departure_estimate(booking, snapshot)
    if departure - moment <= DAY_OF_LEAD:
        return DAY_OF
    return UPCOMING


def departure_estimate(booking: BookingLike, snapshot: SnapshotLike | None) -> datetime:
    """Best known gate departure. Always answers: the booking's schedule is the floor.

    What happened beats what is expected, which beats what was planned, which beats
    what the ticket said.
    """
    if snapshot is not None:
        for candidate in (snapshot.actual_out, snapshot.estimated_out, snapshot.scheduled_out):
            if candidate is not None:
                return ensure_utc(candidate)
    return ensure_utc(booking.scheduled_departure_utc)


def arrival_estimate(booking: BookingLike, snapshot: SnapshotLike | None) -> datetime | None:
    """Best known time at the gate, or None when nobody has ever stated one.

    This is the planning answer: when the doors open and the trip is over. It is what
    the calendar entry ends at and what a flight days away counts down to.
    """
    if snapshot is not None:
        for candidate in (snapshot.actual_in, snapshot.estimated_in, snapshot.scheduled_in):
            if candidate is not None:
                return ensure_utc(candidate)
    if booking.scheduled_arrival_utc is not None:
        return ensure_utc(booking.scheduled_arrival_utc)
    return None


def landing_estimate(booking: BookingLike, snapshot: SnapshotLike | None) -> datetime | None:
    """Best known wheels-down, or None when nobody has ever stated one.

    Distinct from `arrival_estimate` on purpose. Sitting on a plane, the question is
    when it touches down, not when it finishes taxiing to a gate: those differ by five
    to fifteen minutes, and a countdown aimed at the wrong one is wrong for the entire
    part of the flight anyone is actually watching it. Falls back to the gate time,
    which is late but never absurd, when the runway times are missing.
    """
    if snapshot is not None:
        for candidate in (snapshot.actual_on, snapshot.estimated_on, snapshot.scheduled_on):
            if candidate is not None:
                return ensure_utc(candidate)
    return arrival_estimate(booking, snapshot)


def wheels_down(snapshot: SnapshotLike | None) -> bool:
    """Whether the feed has seen this flight arrive, whichever phase it is filed under.

    A diverted flight is filed under the diversion for as long as it exists - where it
    landed is the whole point - so `phase == LANDED` is not the question to ask about
    one that is on the ground at its alternate.
    """
    if snapshot is None:
        return False
    return snapshot.actual_on is not None or snapshot.actual_in is not None


def airborne_window(
    booking: BookingLike, snapshot: SnapshotLike | None, now: datetime
) -> tuple[datetime, datetime] | None:
    """Wheels-up and best known wheels-down, for a flight that is in the air at `now`.

    Diverted counts. A flight bound somewhere it was not booked for is as much in the
    air as one that is not, and the rule under it has an aircraft to place all the same;
    only what the feed has seen come down is out of the air.

    None on the ground, once landed, and whenever the two do not make a span: a landing
    estimate that has not caught up with a late take-off is nothing to divide by.
    """
    if snapshot is None or wheels_down(snapshot):
        return None
    if compute_phase(booking, snapshot, now) not in (AIRBORNE, DIVERTED):
        return None
    off = ensure_utc(snapshot.actual_off)
    landing = landing_estimate(booking, snapshot)
    if off is None or landing is None or landing <= off:
        return None
    return off, landing


def expected_window(
    booking: BookingLike, snapshot: SnapshotLike | None, now: datetime
) -> tuple[datetime, datetime] | None:
    """The span the ticket says the flight is flying, for one nothing has been seen of.

    A flight imported while it is already in the air has no observation at all until the
    poller's first look, and one whose feed has gone quiet - a spent budget, a number
    FlightAware cannot resolve - may never get another. The clock cannot prove wheels-up,
    so nothing here says the flight departed; it says where the aircraft would be if the
    schedule held, which is the same schedule every other figure on the card falls back
    to when the feed is silent.

    None until the flight is due out, which is the stretch the rule has the length of the
    hop to say instead.
    """
    departure = departure_estimate(booking, snapshot)
    landing = landing_estimate(booking, snapshot)
    if ensure_utc(now) < departure or landing is None or landing <= departure:
        return None
    return departure, landing


def progress_estimate(
    booking: BookingLike, snapshot: SnapshotLike | None, now: datetime
) -> int | None:
    """How far along the flight is at `now`, as a percentage, or None if nobody can say.

    AeroAPI states a `progress_percent`, but it is true at the moment it was polled and
    an airborne flight is polled every ten minutes, so on its own the figure sits still
    between polls and for as long as a poll keeps failing. Wheels-up is observed and the
    landing estimate moves all flight long, so elapsed over expected airborne time is
    current at every instant and agrees with the feed each time the feed is asked. The
    feed's figure stands whenever the clock has nothing to go on.
    """
    window = airborne_window(booking, snapshot, now)
    if window is None:
        return snapshot.progress_percent if snapshot is not None else None
    return _fraction(window, now)


def expected_progress(
    booking: BookingLike, snapshot: SnapshotLike | None, now: datetime
) -> int | None:
    """Where the ticket alone puts the aircraft. None while the flight is not due out."""
    window = expected_window(booking, snapshot, now)
    return None if window is None else _fraction(window, now)


def _fraction(window: tuple[datetime, datetime], now: datetime) -> int:
    off, landing = window
    fraction = (ensure_utc(now) - off) / (landing - off)
    return round(100 * min(max(fraction, 0.0), 1.0))
