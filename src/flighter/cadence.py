"""When a booking is next worth an AeroAPI call.

The whole of the project's spend control is in here, as pure functions over a clock and
a few timestamps so that every band can be asserted without a database or a network.
Both the poller and the booking repository schedule through these and nothing else: a
booking created months out, one whose flight AeroAPI cannot see yet, and one in the air
all get their next look from the same table.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from .timezones import ensure_utc

# `GET /flights/{ident}` answers for flights up to about two days ahead. A call made
# before that is a result set spent on an empty list, so nothing is polled until the
# flight can be in the answer.
FEED_HORIZON = timedelta(days=2)

# Cadence, from furthest out to nearest.
DAY_HORIZON = timedelta(hours=24)
FINAL_HORIZON = timedelta(hours=3)
DAILY_INTERVAL = timedelta(days=1)
HOURLY_INTERVAL = timedelta(minutes=30)
CLOSE_INTERVAL = timedelta(minutes=10)

# Baggage claim and the on-blocks time are published minutes after the wheels stop, so
# the last few polls happen after the flight has, as far as the passenger is concerned,
# finished.
LANDED_TAIL = timedelta(minutes=90)

# A flight whose departure passed this long ago without ever going airborne is not going
# to start now, and one whose landing was due this long ago is not still in the air;
# something upstream lost it. Stop rather than poll it forever.
ABANDON_AFTER = timedelta(hours=24)

# A booking AeroAPI could not resolve, or a pinned flight it no longer returns, is looked
# at no faster than this whatever the table says: a flight number that will never match
# is otherwise polled six times an hour through its whole departure window.
RETRY_INTERVAL = timedelta(minutes=30)


class SnapshotLike(Protocol):
    """The parts of a `FlightSnapshot` the cadence depends on.

    Read-only by design: the cadence never writes, and a Protocol of plain attributes
    would force every caller to match the column types exactly.
    """

    @property
    def scheduled_out(self) -> datetime | None: ...
    @property
    def estimated_out(self) -> datetime | None: ...
    @property
    def actual_off(self) -> datetime | None: ...
    @property
    def scheduled_on(self) -> datetime | None: ...
    @property
    def estimated_on(self) -> datetime | None: ...
    @property
    def actual_on(self) -> datetime | None: ...
    @property
    def cancelled(self) -> bool | None: ...


def before_departure(now: datetime, departure: datetime) -> datetime | None:
    """The cadence for a flight still on the ground, from how far off its departure is.

    None once the departure is so far past that the flight is not going to happen.
    """
    now = ensure_utc(now)
    departure = ensure_utc(departure)
    if now > departure + ABANDON_AFTER:
        return None

    remaining = departure - now
    if remaining > FEED_HORIZON:
        return departure - FEED_HORIZON
    if remaining > DAY_HORIZON:
        return min(now + DAILY_INTERVAL, departure - DAY_HORIZON)
    if remaining > FINAL_HORIZON:
        return min(now + HOURLY_INTERVAL, departure - FINAL_HORIZON)
    return now + CLOSE_INTERVAL


def first_poll_at(now: datetime, departure: datetime) -> datetime:
    """When a booking that has never been polled should be looked at for the first time.

    Straight away once the flight can be in the feed, including for one that has already
    flown: AeroAPI keeps ten days of history, and one poll is what closes the booking.
    """
    return max(ensure_utc(now), ensure_utc(departure) - FEED_HORIZON)


def next_poll_at(
    now: datetime,
    current: SnapshotLike,
    previous: SnapshotLike | None = None,
    *,
    booked: datetime,
) -> datetime | None:
    """When to look at this flight again after an observation, or None when there is
    nothing left to see.

    `booked` is the ticket's departure, for an observation that carries no time of its
    own. None is the signal to complete the booking; the caller owns that write.
    """
    now = ensure_utc(now)

    if current.cancelled:
        # One more look, in case the flag was a gap in FlightAware's feed rather than
        # the airline's decision; a second sighting settles it.
        return None if previous is not None and previous.cancelled else now + CLOSE_INTERVAL

    actual_on = ensure_utc(current.actual_on)
    if actual_on is not None:
        return now + CLOSE_INTERVAL if now <= actual_on + LANDED_TAIL else None

    off = ensure_utc(current.actual_off)
    if off is not None:
        # Wheels down is published minutes after the fact, so a landing long overdue
        # with no word of it means the feed has lost the flight, not that it is still
        # flying. Measured from the take-off when nobody has put a time on the landing.
        landing = ensure_utc(current.estimated_on) or ensure_utc(current.scheduled_on) or off
        return None if now > landing + ABANDON_AFTER else now + CLOSE_INTERVAL

    departure = ensure_utc(current.estimated_out) or ensure_utc(current.scheduled_out)
    if departure is not None:
        return before_departure(now, departure)
    # No usable estimate at all. Read the table off the ticket instead, at a moderate
    # cadence rather than dropping a booking on the strength of one thin response, and
    # still give up once the ticket's departure is long past.
    following = before_departure(now, booked)
    if following is None:
        return None
    return max(following, now + HOURLY_INTERVAL)


def retry_poll_at(now: datetime, departure: datetime) -> datetime | None:
    """When to try again after AeroAPI returned nothing for the booking.

    The same table as a successful poll, read off the booked departure since there is
    no observation to read it off, and floored at the retry interval.
    """
    now = ensure_utc(now)
    following = before_departure(now, departure)
    if following is None:
        return None
    return max(following, now + RETRY_INTERVAL)
