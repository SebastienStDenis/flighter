"""What a template is handed, and the formatting every page shares.

A template asks a view for a value rather than reaching into a snapshot, so "actual
beats estimated beats scheduled" is decided once, in `phase`, instead of once per page.
The widget reads the same status, the same milestone and the same ranking from here,
which is what keeps the lock screen and the board saying the same thing about a flight.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from . import bookings as booking_repo
from . import notices
from .airports import get_airport
from .caldav import calendar_link
from .models import Airport, Booking, BookingStatus, FlightSnapshot, IngestLog
from .phase import (
    AIRBORNE,
    ARRIVAL_DELAY_THRESHOLD,
    CANCELLED,
    DAY_OF,
    DEPARTURE_DELAY_THRESHOLD,
    DIVERTED,
    LANDED,
    TAXIING,
    UPCOMING,
    Phase,
    airborne_window,
    arrival_estimate,
    compute_phase,
    departure_estimate,
    landing_estimate,
    progress_estimate,
)
from .timezones import format_local, parse_instant, to_local

# What a value looks like when we simply do not have it. Gates and baggage belts stay
# null until close to the event, so this is one of the most common things on the page.
MISSING = "-"

# Flights this far apart are two journeys, not two legs of one. A same-day connection
# and a red-eye that lands tomorrow both fall inside a day; a return a week later does
# not, which is the split a person means by "trip".
TRIP_GAP = timedelta(hours=24)

# How long after the gate a flight stays on the board. Long enough to collect bags and
# get out of the building with the card still one tap away.
LINGER: Final = timedelta(hours=2)


class Status(NamedTuple):
    """A status is always a word plus a colour, never a colour on its own.

    `tone` is the badge variant the templates hand to Basecoat: quiet, plan, live, ok,
    warn or stop.
    """

    label: str
    tone: str


class Milestone(NamedTuple):
    """The next thing to happen to a flight that somebody has put a time on.

    `label` is the words in front of the countdown: "Departs in", "Lands in". A flight
    still days away is "Scheduled" instead, because nobody counts hours to it yet.
    """

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

    @property
    def late(self) -> bool:
        return self.moved is not None and self.moved > timedelta(0)


@dataclass(frozen=True)
class FlightView:
    """A booking, its newest snapshot and both airports, for a template."""

    booking: Booking
    snapshot: FlightSnapshot | None
    origin: Airport | None
    dest: Airport | None
    # Where a diverted flight was sent instead, when that airport is on file.
    diversion: Airport | None = None

    @property
    def flight_number(self) -> str:
        return f"{self.booking.marketing_carrier}{self.booking.marketing_number}"

    @property
    def operated(self) -> str | None:
        return booking_repo.operated_note(
            self.booking.operating_carrier, self.booking.operating_number
        )

    @property
    def origin_tz(self) -> str:
        return self.origin.tz if self.origin else "UTC"

    @property
    def diverted_to(self) -> str | None:
        """The code of the airport a diverted flight is now bound for, when it is a new one."""
        code = destination_iata(self.booking, self.snapshot)
        return code if code != self.booking.dest_iata else None

    @property
    def destination_iata(self) -> str:
        return destination_iata(self.booking, self.snapshot)

    @property
    def destination(self) -> Airport | None:
        """The airport the flight is actually heading for, diversion and all."""
        return self.diversion if self.diverted_to else self.dest

    @property
    def dest_tz(self) -> str:
        """The zone the arrival is read in: the diversion's once there is one, since the
        gate time the feed gives is at the airport it is actually going to."""
        where = self.destination
        return where.tz if where else "UTC"

    @property
    def scheduled_departure(self) -> datetime:
        return scheduled_departure(self.booking, self.snapshot)

    @property
    def scheduled_arrival(self) -> datetime | None:
        return scheduled_arrival(self.booking, self.snapshot)

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
    def arrival_delay(self) -> timedelta | None:
        scheduled, expected = self.scheduled_arrival, self.arrival
        if scheduled is None or expected is None:
            return None
        return expected - scheduled

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
        """How far along the rule the aircraft is drawn.

        A flight that has landed is all the way there whatever the feed last said: its
        figure stops at the last poll, which may have been well short of the runway.
        """
        if self.phase == LANDED or self.booking.status == BookingStatus.COMPLETED:
            return 100
        return progress_estimate(self.booking, self.snapshot, datetime.now(UTC))

    @property
    def airborne_window(self) -> tuple[datetime, datetime] | None:
        """Wheels-up and wheels-down, for the page to move the aircraft between loads."""
        return airborne_window(self.booking, self.snapshot, datetime.now(UTC))

    @property
    def phase(self) -> Phase:
        return compute_phase(self.booking, self.snapshot, datetime.now(UTC))

    @property
    def block_time(self) -> timedelta | None:
        """Gate to gate as currently expected, for a flight that has yet to leave one.

        The rule between the airports has nothing to measure until wheels up, so until
        then it says how long the hop is. Once the flight is under way the aircraft's
        place on the rule is the answer, and afterwards there is nothing left to expect.
        """
        if self.phase not in (UPCOMING, DAY_OF):
            return None
        arrival = self.arrival
        if arrival is None or arrival <= self.departure:
            return None
        return arrival - self.departure

    @property
    def milestone(self) -> Milestone | None:
        """The one number worth looking at, and what to call it."""
        return milestone(self.phase, self.booking, self.snapshot)

    @property
    def milestone_label(self) -> str | None:
        next_up = self.milestone
        return milestone_label(next_up, datetime.now(UTC)) if next_up else None

    @property
    def milestone_due(self) -> str | None:
        """What the page calls the milestone once its time passes while it is open."""
        next_up = self.milestone
        return DUE.get(next_up.label, next_up.label) if next_up else None

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
        return status(self.phase, self.booking, self.snapshot)

    @property
    def watched(self) -> bool:
        """Whether there is anything on the flight to watch yet.

        Inside its day a flight has a gate to walk to and a time to count to. Days out
        it has neither, and called off it has nothing left, so the card keeps to what is
        settled: the number, the route and the times at each end.
        """
        return self.phase not in (UPCOMING, CANCELLED)

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

    @property
    def off_board_at(self) -> datetime:
        """When the board files this flight under Flown.

        Not the moment it is at the gate: the carousel and the terminal are what someone
        is reading while they wait for their bags and find their way out.
        """
        return self.ended + LINGER


def scheduled_departure(booking: Booking, snapshot: FlightSnapshot | None) -> datetime:
    """The planned gate departure: the feed's schedule once it has one, else the ticket's."""
    if snapshot is not None and snapshot.scheduled_out is not None:
        return snapshot.scheduled_out
    return booking.scheduled_departure_utc


def scheduled_arrival(booking: Booking, snapshot: FlightSnapshot | None) -> datetime | None:
    if snapshot is not None and snapshot.scheduled_in is not None:
        return snapshot.scheduled_in
    return booking.scheduled_arrival_utc


def status(phase: Phase, booking: Booking, snapshot: FlightSnapshot | None) -> Status:
    """The pill: where the flight stands, as a word and the tone it is drawn in."""
    if phase == CANCELLED:
        return Status("Cancelled", "stop")
    if phase == DIVERTED:
        return Status("Diverted", "stop")
    if phase == LANDED:
        # Wheels down and at the gate are ten minutes apart and two different waits.
        if snapshot is not None and snapshot.actual_in is not None:
            return Status("Arrived", "ok")
        return Status("Landed", "ok")
    if phase == AIRBORNE:
        # A late pushback is history once the aircraft is off the ground; the only
        # question left is whether it still gets in late.
        scheduled = scheduled_arrival(booking, snapshot)
        expected = arrival_estimate(booking, snapshot)
        late = expected - scheduled if scheduled is not None and expected is not None else None
        if late is not None and late >= ARRIVAL_DELAY_THRESHOLD:
            return Status("Arriving late", "warn")
        return Status("In the air", "live")
    if booking.status == BookingStatus.COMPLETED:
        return Status("Flown", "quiet")
    if phase == TAXIING:
        return Status("Taxiing", "live")
    delay = departure_estimate(booking, snapshot) - scheduled_departure(booking, snapshot)
    if delay >= DEPARTURE_DELAY_THRESHOLD:
        return Status("Departure delayed", "warn")
    # Only a feed that has actually restated the departure can say it is on time; a
    # booking on its own is just a plan.
    if snapshot is not None and (snapshot.estimated_out or snapshot.scheduled_out):
        return Status("On time", "ok")
    if phase == DAY_OF:
        return Status("Today", "plan")
    return Status("Scheduled", "quiet")


def milestone(phase: Phase, booking: Booking, snapshot: FlightSnapshot | None) -> Milestone | None:
    """The first thing still ahead of the flight that has a time against it.

    The ladder is departure, wheels up, landing, the gate. A rung is skipped once it has
    happened, and also when nobody has said when it will: nothing upstream estimates
    wheels up, so a flight taxiing out counts to its landing, and a flight with no
    arrival time at all has no milestone rather than a made-up one.
    """
    if phase == CANCELLED:
        return None
    if phase == UPCOMING:
        return Milestone("Scheduled", departure_estimate(booking, snapshot))
    if phase == DAY_OF:
        return Milestone("Departs in", departure_estimate(booking, snapshot))
    ahead: list[tuple[str, datetime | None]] = []
    if phase in (TAXIING, AIRBORNE, DIVERTED):
        ahead.append(("Lands in", landing_estimate(booking, snapshot)))
    if snapshot is None or snapshot.actual_in is None:
        ahead.append(("At the gate in", arrival_estimate(booking, snapshot)))
    for label, target in ahead:
        if target is not None:
            return Milestone(label, target)
    return None


# What a milestone is called once its time has gone by with no word that it happened. The
# feed runs a few minutes behind the aircraft, so this is the normal state of things for
# a short while at every rung, and it should read as waiting rather than as arithmetic.
DUE: Final = {
    "Departs in": "Due to depart",
    "Lands in": "Due to land",
    "At the gate in": "Due at the gate",
}


def milestone_label(next_up: Milestone, now: datetime) -> str:
    if next_up.target <= now:
        return DUE.get(next_up.label, next_up.label)
    return next_up.label


def destination_iata(booking: Booking, snapshot: FlightSnapshot | None) -> str:
    """Where the flight is going: the booked airport until a diversion names another."""
    if snapshot is not None and snapshot.diverted and snapshot.destination_iata:
        return snapshot.destination_iata
    return booking.dest_iata


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

    Whole days once it is a day or more away, hours and minutes inside that, and never
    seconds: a number that changes while it is being read is noise. The page's script
    builds the same string from the same units, so nothing jumps when it takes over.
    """
    remaining = instant - datetime.now(UTC)
    minutes = int(abs(remaining).total_seconds() // 60)
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        figure = f"{days}d"
    elif hours:
        figure = f"{hours}h {minutes:02d}m"
    elif minutes:
        figure = f"{minutes}m"
    else:
        figure = "<1m"
    # A target in the past counts up: "20m ago" is a fact, "-20m" is arithmetic.
    return f"{figure} ago" if remaining.total_seconds() < 0 else figure


def day(instant: datetime, tz: str) -> str:
    """`Sat 12 Sep`, the heading a trip is filed under."""
    return to_local(instant, tz).strftime("%a %-d %b")


def at(instant: datetime | None, tz: str, *, with_date: bool = False) -> str:
    """A time at an airport, or the missing marker. Every time on every page uses it."""
    if instant is None:
        return MISSING
    return format_local(instant, tz, with_date=with_date)


def clock(instant: datetime | None, tz: str, *, with_date: bool = False) -> str:
    """`18:40`, or `Sat 12 Sep 18:40`: the time without its zone.

    For the two places that set the zone beside it in their own type, or have just
    shown it: the card's large time, and a struck time sitting next to the one that
    replaced it at the same airport.
    """
    if instant is None:
        return MISSING
    fmt = "%a %-d %b %H:%M" if with_date else "%H:%M"
    return to_local(instant, tz).strftime(fmt)


def zone(instant: datetime | None, tz: str) -> str:
    """`EDT`: the abbreviation `clock` left off, read at the same instant so that a
    flight either side of a clock change carries the right one."""
    if instant is None:
        return ""
    return to_local(instant, tz).strftime("%Z")


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
        bound_for = destination_iata(booking, snapshots.get(booking.id))
        for iata in (booking.origin_iata, booking.dest_iata, bound_for):
            if iata not in airports:
                airports[iata] = await get_airport(session, iata)

    views = [
        FlightView(
            booking=booking,
            snapshot=snapshots.get(booking.id),
            origin=airports.get(booking.origin_iata),
            dest=airports.get(booking.dest_iata),
            diversion=airports.get(destination_iata(booking, snapshots.get(booking.id))),
        )
        for booking in bookings
    ]
    # By the time each is now leaving, not the time it was booked to: a flight held
    # three hours belongs after the one that left in the meantime.
    views.sort(key=lambda view: view.departure)
    return views
