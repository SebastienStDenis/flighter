"""What a template is handed, and the formatting every page shares.

A template asks a view for a value rather than reaching into a snapshot, so "actual
beats estimated beats scheduled" is decided once, in `phase`, instead of once per page.
The widget reads the same countdown and the same ranking from here, which is what keeps
the lock screen and the board naming the same flight.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from . import bookings as booking_repo
from . import notices
from .airports import get_airport
from .caldav import calendar_link
from .models import Airport, Booking, BookingStatus, FlightSnapshot, IngestLog
from .phase import (
    AIRBORNE,
    CANCELLED,
    DAY_OF,
    DEPARTURE_DELAY_THRESHOLD,
    DIVERTED,
    LANDED,
    TAXIING,
    Phase,
    arrival_estimate,
    compute_phase,
    departure_estimate,
    landing_estimate,
)
from .timezones import format_local, parse_instant, to_local

# What a value looks like when we simply do not have it. Gates and baggage belts stay
# null until close to the event, so this is one of the most common things on the page.
MISSING = "-"

# Flights this far apart are two journeys, not two legs of one. A same-day connection
# and a red-eye that lands tomorrow both fall inside a day; a return a week later does
# not, which is the split a person means by "trip".
TRIP_GAP = timedelta(hours=24)


class Status(NamedTuple):
    """A status is always a word plus a colour, never a colour on its own.

    `tone` is the badge variant the templates hand to Basecoat: quiet, plan, live, ok,
    warn or stop.
    """

    label: str
    tone: str


class Countdown(NamedTuple):
    """A moment to count towards, and the words that go in front of it."""

    label: str
    target: datetime


@dataclass(frozen=True)
class Timeline:
    """One event's three-state answer, collapsed to what a reader needs to see.

    Scheduled, estimated and actual describe the same moment at three levels of
    certainty, so showing them as three rows makes a reader work out which is current.
    This carries the best answer, and the original only when it is worth striking out.
    """

    scheduled: datetime | None
    best: datetime | None
    actual: datetime | None

    @property
    def confirmed(self) -> bool:
        return self.actual is not None

    @property
    def moved(self) -> timedelta | None:
        """How far the best answer has drifted from the schedule, when it matters."""
        if self.scheduled is None or self.best is None:
            return None
        shift = self.best - self.scheduled
        return shift if abs(shift) >= DEPARTURE_DELAY_THRESHOLD else None


@dataclass(frozen=True)
class FlightView:
    """A booking, its newest snapshot and both airports, for a template."""

    booking: Booking
    snapshot: FlightSnapshot | None
    origin: Airport | None
    dest: Airport | None

    @property
    def flight_number(self) -> str:
        return f"{self.booking.marketing_carrier}{self.booking.marketing_number}"

    @property
    def origin_tz(self) -> str:
        return self.origin.tz if self.origin else "UTC"

    @property
    def dest_tz(self) -> str:
        return self.dest.tz if self.dest else "UTC"

    @property
    def scheduled_departure(self) -> datetime:
        snap = self.snapshot
        if snap is not None and snap.scheduled_out is not None:
            return snap.scheduled_out
        return self.booking.scheduled_departure_utc

    @property
    def scheduled_arrival(self) -> datetime | None:
        snap = self.snapshot
        if snap is not None and snap.scheduled_in is not None:
            return snap.scheduled_in
        return self.booking.scheduled_arrival_utc

    @property
    def departure(self) -> datetime:
        return departure_estimate(self.booking, self.snapshot)

    @property
    def arrival(self) -> datetime | None:
        return arrival_estimate(self.booking, self.snapshot)

    @property
    def delay(self) -> timedelta:
        return self.departure - self.scheduled_departure

    @property
    def departs(self) -> Timeline:
        """The gate departure, resolved: what it is now and what it was booked as."""
        snap = self.snapshot
        return Timeline(
            scheduled=self.scheduled_departure,
            best=self.departure,
            actual=snap.actual_out if snap else None,
        )

    @property
    def arrives(self) -> Timeline:
        """Gate arrival: the end of the trip, and what the calendar entry runs to."""
        snap = self.snapshot
        return Timeline(
            scheduled=self.scheduled_arrival,
            best=self.arrival,
            actual=snap.actual_in if snap else None,
        )

    @property
    def lands(self) -> Timeline:
        """Wheels down, which is the question once the doors are shut."""
        snap = self.snapshot
        return Timeline(
            scheduled=snap.scheduled_on if snap else None,
            best=landing_estimate(self.booking, snap) if snap else None,
            actual=snap.actual_on if snap else None,
        )

    @property
    def cancelled(self) -> bool:
        return bool(self.snapshot is not None and self.snapshot.cancelled)

    @property
    def progress_percent(self) -> int | None:
        return self.snapshot.progress_percent if self.snapshot else None

    @property
    def phase(self) -> Phase:
        return compute_phase(self.booking, self.snapshot, datetime.now(UTC))

    @property
    def countdown(self) -> Countdown | None:
        """The one number worth looking at, decided the same way the widget decides it."""
        label, target = countdown(self.phase, self.booking, self.snapshot)
        if label is None or target is None:
            return None
        return Countdown(label, target)

    @property
    def calendar_link(self) -> str | None:
        """A way into the Calendar app, once this flight has an entry there.

        Offered here rather than on the push about the import, which goes on pointing at
        this page: the calendar entry is a copy of what was known when it was written,
        and this page is where the gate and the delay are live.
        """
        if not self.booking.calendar_event_uid:
            return None
        return calendar_link(self.scheduled_departure, self.origin_tz)

    @property
    def gate(self) -> str | None:
        """The gate to walk to now: the one it leaves from, then the one it arrives at."""
        snap = self.snapshot
        if snap is None:
            return None
        if self.phase in (AIRBORNE, DIVERTED, LANDED):
            return snap.gate_destination
        return snap.gate_origin

    @property
    def terminal(self) -> str | None:
        snap = self.snapshot
        if snap is None:
            return None
        if self.phase in (AIRBORNE, DIVERTED, LANDED):
            return snap.terminal_destination
        return snap.terminal_origin

    @property
    def status(self) -> Status:
        snap = self.snapshot
        if self.cancelled:
            # FlightAware's flag means "no longer tracked", so the badge asks rather
            # than asserts and the flight page carries the sentence that explains it.
            return Status("Maybe cancelled", "stop")
        if snap is not None and snap.diverted:
            return Status("Diverted", "stop")
        if self.delay >= DEPARTURE_DELAY_THRESHOLD:
            return Status(f"Delayed {duration(self.delay)}", "warn")
        phase = self.phase
        if phase == LANDED:
            return Status("Landed", "ok")
        if phase == AIRBORNE:
            return Status("In the air", "live")
        if self.booking.status == BookingStatus.COMPLETED:
            return Status("Flown", "quiet")
        if phase == TAXIING:
            return Status("Taxiing", "live")
        # Only a feed that has actually restated the departure can say it is on time;
        # a booking on its own is just a plan.
        if snap is not None and (snap.estimated_out or snap.scheduled_out):
            return Status("On time", "ok")
        if phase == DAY_OF:
            return Status("Today", "plan")
        return Status("Scheduled", "quiet")

    @property
    def ended(self) -> datetime:
        """When this flight stopped being something to wait for.

        The same rule `list_bookings(upcoming_only=True)` applies: a flight in the air
        has departed but is still very much upcoming to the person meeting it.
        """
        arrival = self.arrival
        if arrival is not None:
            return arrival
        # Nothing anywhere says when it lands, so assume the longest plausible hop
        # rather than pinning a departed flight to the top of the list forever.
        return self.departure + timedelta(hours=3)


def countdown(
    phase: Phase, booking: Booking, snapshot: FlightSnapshot | None
) -> tuple[str | None, datetime | None]:
    """The one instant to count to, and what to call it.

    The lock screen and the flight page count to the same moment, so this lives here
    once rather than being decided twice.
    """
    if phase in (AIRBORNE, DIVERTED):
        # Wheels down, not the gate: this is the number someone stares at from a seat,
        # and taxiing is not part of what they are counting.
        landing = landing_estimate(booking, snapshot)
        return ("Lands in", landing) if landing is not None else (None, None)
    # Nothing upstream predicts how long a taxi takes, so the honest answer is no number.
    if phase in (TAXIING, LANDED, CANCELLED):
        return None, None
    return "Departs in", departure_estimate(booking, snapshot)


def phase_rank(phase: Phase) -> int:
    """In progress first, then what is still coming, then what has already landed.

    Departure time breaks the tie in every band, including for a cancelled flight, which
    still belongs on the day it was supposed to leave. The board leads with the same
    flight the widget does, because it asks this the same question.
    """
    if phase in (TAXIING, AIRBORNE, DIVERTED):
        return 0
    if phase == LANDED:
        return 2
    return 1


def duration(delta: timedelta) -> str:
    """`45m`, `1h 20m`. Used for delays, so the caller carries the sign."""
    minutes = int(abs(delta).total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def until(instant: datetime) -> str:
    """A countdown as the server can render it, before the page's own clock takes over.

    Every unit the page's script uses, in the same order, so the string does not jump
    the moment the script replaces it.
    """
    remaining = instant - datetime.now(UTC)
    total = int(abs(remaining).total_seconds())
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        figure = f"{days}d {hours}h"
    elif hours:
        figure = f"{hours}h {minutes:02d}m"
    else:
        figure = f"{minutes}m {seconds:02d}s"
    # A target in the past counts up: "20m ago" is a fact, "-20m" is arithmetic.
    return f"{figure} ago" if remaining.total_seconds() < 0 else figure


def day(instant: datetime, tz: str) -> str:
    """`Sat 12 Sep`, the heading a trip is filed under."""
    return to_local(instant, tz).strftime("%a %-d %b")


def same_day(a: FlightView, b: FlightView) -> bool:
    """Whether two flights leave on the same day, each read at its own airport."""
    return (
        to_local(a.scheduled_departure, a.origin_tz).date()
        == to_local(b.scheduled_departure, b.origin_tz).date()
    )


def at(instant: datetime | None, tz: str, *, with_date: bool = False) -> str:
    """A time at an airport, or the missing marker. Every time on every page uses it."""
    if instant is None:
        return MISSING
    return format_local(instant, tz, with_date=with_date)


def problem_notice(row: IngestLog) -> notices.Notice:
    """What the Problems page says about one email the service gave up on.

    The same words the phone was sent when it gave up, from the same place, so that a
    person who read the push and then opened the page is not told two different stories.
    """
    return notices.import_failed(subject=row.subject, reason=row.error)


def dash(value: object) -> str:
    if value is None or value == "":
        return MISSING
    return str(value)


def change_title(kind: str) -> str:
    """`GateChanged` as `Gate changed`, which is how a person reads it."""
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind.replace("_", " ")).split()
    return " ".join(words).capitalize() if words else kind


def change_value(value: str | None, tz: str) -> str:
    """One side of a change, as a time whenever it is one.

    Times are stored on the event as ISO instants because that is what a diff of two
    snapshots produces; nobody reads a gate change out of `2026-09-12T18:40:00+00:00`.
    """
    instant = parse_instant(value)
    return at(instant, tz) if instant is not None else dash(value)


def most_urgent(views: Sequence[FlightView]) -> int | None:
    """The booking id the board leads with, ranked exactly as the lock screen ranks it."""
    ranked = sorted(
        views,
        key=lambda view: (
            phase_rank(view.phase),
            departure_estimate(view.booking, view.snapshot),
        ),
    )
    return ranked[0].booking.id if ranked else None


def group_into_trips(views: Sequence[FlightView]) -> list[list[FlightView]]:
    """Split departure-ordered flights into the runs that belong to one journey."""
    trips: list[list[FlightView]] = []
    for view in views:
        if trips and view.departure - trips[-1][-1].ended <= TRIP_GAP:
            trips[-1].append(view)
        else:
            trips.append([view])
    return trips


async def build_views(session: AsyncSession, rows: Iterable[Booking]) -> list[FlightView]:
    bookings = list(rows)
    snapshots = await booking_repo.latest_snapshots(session, [booking.id for booking in bookings])

    airports: dict[str, Airport | None] = {}
    for booking in bookings:
        for iata in (booking.origin_iata, booking.dest_iata):
            if iata not in airports:
                airports[iata] = await get_airport(session, iata)

    views = [
        FlightView(
            booking=booking,
            snapshot=snapshots.get(booking.id),
            origin=airports.get(booking.origin_iata),
            dest=airports.get(booking.dest_iata),
        )
        for booking in bookings
    ]
    views.sort(key=lambda view: view.scheduled_departure)
    return views
