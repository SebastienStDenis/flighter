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
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, Literal, Protocol

Phase = Literal["upcoming", "day_of", "taxiing", "airborne", "landed", "cancelled", "diverted"]

UPCOMING: Final = "upcoming"
DAY_OF: Final = "day_of"
TAXIING: Final = "taxiing"
AIRBORNE: Final = "airborne"
LANDED: Final = "landed"
CANCELLED: Final = "cancelled"
DIVERTED: Final = "diverted"

DAY_OF_LEAD: Final = timedelta(hours=24)


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


def compute_phase(booking: BookingLike, snapshot: SnapshotLike | None, now: datetime) -> Phase:
    """The flight's phase at `now`, from the newest snapshot and the booking's schedule.

    Order matters. Cancelled outranks everything: a cancelled flight that never left is
    not under way just because its scheduled time has passed. Diverted outranks landed
    for the same reason, since where it landed is the whole point.
    """
    moment = _utc(now)
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
    """Best known departure. Always answers: the booking's schedule is the floor."""
    if snapshot is not None:
        for candidate in (snapshot.estimated_out, snapshot.scheduled_out):
            if candidate is not None:
                return _utc(candidate)
    return _utc(booking.scheduled_departure_utc)


def arrival_estimate(booking: BookingLike, snapshot: SnapshotLike | None) -> datetime | None:
    """Best known time at the gate, or None when nobody has ever stated one.

    This is the planning answer: when the doors open and the trip is over. It is what
    the calendar entry ends at and what a flight days away counts down to.
    """
    if snapshot is not None:
        for candidate in (snapshot.actual_in, snapshot.estimated_in, snapshot.scheduled_in):
            if candidate is not None:
                return _utc(candidate)
    if booking.scheduled_arrival_utc is not None:
        return _utc(booking.scheduled_arrival_utc)
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
                return _utc(candidate)
    return arrival_estimate(booking, snapshot)


def _utc(value: datetime) -> datetime:
    """A naive datetime here can only have come from a store that dropped the zone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
