"""Where a flight is in its life, decided in one place so the widget and web UI agree.

Deliberately a pure function over plain attributes: the phase drives the countdown, the
subtitle, the sort order and the notification copy, and three implementations of it that
disagree at the boundaries is how a widget ends up saying "Boards in" to someone already
in the air.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, Literal, Protocol

Phase = Literal["upcoming", "day_of", "boarding", "airborne", "landed", "cancelled", "diverted"]

UPCOMING: Final = "upcoming"
DAY_OF: Final = "day_of"
BOARDING: Final = "boarding"
AIRBORNE: Final = "airborne"
LANDED: Final = "landed"
CANCELLED: Final = "cancelled"
DIVERTED: Final = "diverted"

PHASES: Final[tuple[Phase, ...]] = (
    UPCOMING,
    DAY_OF,
    BOARDING,
    AIRBORNE,
    LANDED,
    CANCELLED,
    DIVERTED,
)

# Boarding opens roughly half an hour out on a narrowbody. It is the moment the flight
# stops being something you are travelling towards and starts being something you are in.
BOARDING_LEAD: Final = timedelta(minutes=30)
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
    scheduled_in: datetime | None
    estimated_in: datetime | None
    actual_on: datetime | None
    actual_in: datetime | None


def compute_phase(booking: BookingLike, snapshot: SnapshotLike | None, now: datetime) -> Phase:
    """The flight's phase at `now`, from the newest snapshot and the booking's schedule.

    Order matters. Cancelled outranks everything: a cancelled flight that never left is
    not "boarding" just because its scheduled time has passed. Diverted outranks landed
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

    departure = departure_estimate(booking, snapshot)
    boards_at = boarding_time(snapshot) or departure - BOARDING_LEAD
    if moment >= boards_at:
        return BOARDING
    if departure - moment <= DAY_OF_LEAD:
        return DAY_OF
    return UPCOMING


def boarding_time(snapshot: SnapshotLike | None) -> datetime | None:
    """When boarding began: pushback if it happened, else the lead before departure.

    Returns None when the snapshot carries no departure time at all, so callers that
    have a booking to fall back on can supply their own estimate.
    """
    if snapshot is None:
        return None
    if snapshot.actual_out is not None:
        return _utc(snapshot.actual_out)
    departure = snapshot.estimated_out or snapshot.scheduled_out
    if departure is None:
        return None
    return _utc(departure) - BOARDING_LEAD


def departure_estimate(booking: BookingLike, snapshot: SnapshotLike | None) -> datetime:
    """Best known departure. Always answers: the booking's schedule is the floor."""
    if snapshot is not None:
        for candidate in (snapshot.estimated_out, snapshot.scheduled_out):
            if candidate is not None:
                return _utc(candidate)
    return _utc(booking.scheduled_departure_utc)


def arrival_estimate(booking: BookingLike, snapshot: SnapshotLike | None) -> datetime | None:
    """Best known arrival, or None when nobody has ever stated one."""
    if snapshot is not None:
        for candidate in (snapshot.estimated_in, snapshot.actual_on, snapshot.scheduled_in):
            if candidate is not None:
                return _utc(candidate)
    if booking.scheduled_arrival_utc is not None:
        return _utc(booking.scheduled_arrival_utc)
    return None


def _utc(value: datetime) -> datetime:
    """A naive datetime here can only have come from a store that dropped the zone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
