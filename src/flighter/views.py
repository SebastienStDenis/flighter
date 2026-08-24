"""What a template is handed, and the formatting every page shares.

A template asks a view for a value rather than reaching into a snapshot, so "actual
beats estimated beats scheduled" is decided once, in `phase`, instead of once per page.
The widget reads the same status, the same milestone and the same ranking from here,
which is what keeps the lock screen and the board saying the same thing about a flight.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from . import bookings as booking_repo
from . import notices
from .airports import get_airport
from .cadence import ABANDON_AFTER
from .caldav import calendar_link
from .ingest import set_aside
from .models import Airport, Booking, BookingStatus, FlightSnapshot, IngestLog, IngestOutcome
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
    expected_progress,
    expected_window,
    landing_estimate,
    progress_estimate,
    wheels_down,
)
from .timezones import ensure_utc, format_local, parse_instant, to_local

# What a value looks like when we simply do not have it. Gates and baggage belts stay
# null until close to the event, so this is one of the most common things on the page.
MISSING = "-"

# Flights this far apart are two journeys, not two legs of one. A same-day connection
# and a red-eye that lands tomorrow both fall inside a day; a return a week later does
# not, which is the split a person means by "trip".
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
    def friend_hue(self) -> int:
        name = self.booking.friend_name or ""
        return sum(index * ord(character) for index, character in enumerate(name, 1)) % 360

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
    def flown(self) -> bool:
        return flown(self.booking)

    @property
    def down(self) -> bool:
        """Whether the feed has seen it arrive, whichever phase that is filed under."""
        return wheels_down(self.snapshot)

    @property
    def progress_percent(self) -> int | None:
        """How far along the rule the aircraft is drawn.

        A flight that has landed is all the way there whatever the feed last said: its
        figure stops at the last poll, which may have been well short of the runway, and
        a diverted one that is down landed where the rule now ends. A cancelled one never
        left, however the poller closed it.

        With nothing observed at all the schedule is what is left to go on: a flight
        imported while it was already in the air has no snapshot until the poller's first
        look, and one whose feed has gone quiet may never get another, and in both the
        aircraft belongs where the ticket says it is rather than pinned to the airport it
        has plainly left. `progress_confirmed` is what says which of the two a figure is,
        and the rule draws an unconfirmed one in the tone of the dashes around it.
        """
        if self.cancelled:
            return None
        if self.phase == LANDED or self.flown or self.down:
            return 100
        now = datetime.now(UTC)
        observed = progress_estimate(self.booking, self.snapshot, now)
        if observed is not None:
            return observed
        return expected_progress(self.booking, self.snapshot, now)

    @property
    def progress_confirmed(self) -> bool:
        """Whether anything upstream stands behind the figure, or the ticket is all of it."""
        if self.cancelled:
            return False
        if self.phase == LANDED or self.flown or self.down:
            return True
        return progress_estimate(self.booking, self.snapshot, datetime.now(UTC)) is not None

    @property
    def airborne_window(self) -> tuple[datetime, datetime] | None:
        """The span the aircraft crosses the rule over, for the page to move it between
        loads: wheels-up to wheels-down where those are known, and the schedule's own
        span for a flight nothing has been seen of."""
        if self.flown or self.down:
            return None
        now = datetime.now(UTC)
        seen = airborne_window(self.booking, self.snapshot, now)
        if seen is not None:
            return seen
        if progress_estimate(self.booking, self.snapshot, now) is not None:
            # The feed has a figure of its own and no window to move it across; the
            # aircraft stands where the last poll put it rather than drifting off it.
            return None
        return expected_window(self.booking, self.snapshot, now)

    @property
    def phase(self) -> Phase:
        return compute_phase(self.booking, self.snapshot, datetime.now(UTC))

    @property
    def block_time(self) -> timedelta | None:
        """Gate to gate as currently expected, for a flight that has yet to leave one.

        The rule between the airports has nothing to measure until wheels up, so until
        then it says how long the hop is. Once the flight is under way the aircraft's
        place on the rule is the answer, and afterwards there is nothing left to expect.

        Due out and not seen to have gone counts as under way: the phase can only say
        day-of, because nothing observed wheels-up, but a rule saying how long the hop
        will take sits under a footer saying the departure is three hours overdue. The
        aircraft goes there instead, where the schedule puts it.
        """
        if self.flown or self.phase not in (UPCOMING, DAY_OF):
            return None
        if self.departure <= datetime.now(UTC):
            return None
        arrival = self.arrival
        if arrival is None or arrival <= self.departure:
            return None
        return arrival - self.departure

    @property
    def milestone(self) -> Milestone | None:
        """The one number worth looking at, and what to call it."""
        return milestone(self.phase, self.booking, self.snapshot, now=datetime.now(UTC))

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
        return status(
            self.phase,
            self.booking,
            self.snapshot,
            now=datetime.now(UTC),
            origin_tz=self.origin_tz,
        )

    @property
    def at_the_gate(self) -> bool:
        return at_the_gate(self.phase, self.booking, self.snapshot, datetime.now(UTC))

    @property
    def watched(self) -> bool:
        """Whether there is anything on the flight to watch yet.

        Inside its day a flight has a gate to walk to and a time to count to. Days out
        it has neither, and called off it has nothing left, so the card keeps to what is
        settled: the number, the route and the times at each end.
        """
        return watched(self.phase)

    @property
    def ended(self) -> datetime:
        return ended(self.booking, self.snapshot)

    @property
    def off_board_at(self) -> datetime:
        return off_board_at(self.booking, self.snapshot)


def watched(phase: Phase) -> bool:
    """Whether there is anything on the flight to watch yet: see `FlightView.watched`."""
    return phase not in (UPCOMING, CANCELLED)


def ended(booking: Booking, snapshot: FlightSnapshot | None) -> datetime:
    """When this flight stopped being something to wait for.

    The same rule `list_bookings(upcoming_only=True)` applies: a flight in the air has
    departed but is still very much upcoming to the person meeting it.
    """
    arrival = arrival_estimate(booking, snapshot)
    if arrival is not None:
        return arrival
    # Nothing anywhere says when it lands, so assume the longest plausible hop rather
    # than pinning a departed flight to the top of the list forever.
    return departure_estimate(booking, snapshot) + timedelta(hours=3)


def off_board_at(booking: Booking, snapshot: FlightSnapshot | None) -> datetime:
    """When the board files this flight under Flown, and the widget lets it go.

    Not the moment it is at the gate: the carousel and the terminal are what someone is
    reading while they wait for their bags and find their way out.
    """
    return ended(booking, snapshot) + LINGER


def flown(booking: Booking) -> bool:
    """Whether the poller has closed the book on this flight.

    Usually that is an hour and a half after the wheels stopped. It is also what happens
    when the feed loses a flight part-way through and nothing more is ever heard: the
    snapshot then says airborne, or taxiing, or nothing at all, for good. Whatever it
    says, there is nothing left to count to, and a countdown past its time would go on
    growing for as long as the page exists.
    """
    return booking.status == BookingStatus.COMPLETED


def scheduled_departure(booking: Booking, snapshot: FlightSnapshot | None) -> datetime:
    """The planned gate departure: the feed's schedule once it has one, else the ticket's."""
    if snapshot is not None and snapshot.scheduled_out is not None:
        return snapshot.scheduled_out
    return booking.scheduled_departure_utc


def scheduled_arrival(booking: Booking, snapshot: FlightSnapshot | None) -> datetime | None:
    if snapshot is not None and snapshot.scheduled_in is not None:
        return snapshot.scheduled_in
    return booking.scheduled_arrival_utc


def status(
    phase: Phase,
    booking: Booking,
    snapshot: FlightSnapshot | None,
    *,
    now: datetime,
    origin_tz: str,
) -> Status:
    """The pill: where the flight stands, as a word and the tone it is drawn in.

    `now` and `origin_tz` serve the one word that names a day: a flight inside its
    24-hour window is "Today" or "Tomorrow" by the clock at the airport it leaves from.
    """
    if phase == CANCELLED:
        return Status("Cancelled", "stop")
    if phase == DIVERTED:
        return Status("Diverted", "stop")
    if phase == LANDED:
        # Wheels down and at the gate are ten minutes apart and two different waits.
        if snapshot is not None and snapshot.actual_in is not None:
            return Status("Arrived", "ok")
        return Status("Landed", "ok")
    if flown(booking):
        return Status("Flown", "quiet")
    if phase == AIRBORNE:
        # A late pushback is history once the aircraft is off the ground; the only
        # question left is whether it still gets in late.
        scheduled = scheduled_arrival(booking, snapshot)
        expected = arrival_estimate(booking, snapshot)
        late = expected - scheduled if scheduled is not None and expected is not None else None
        if late is not None and late >= ARRIVAL_DELAY_THRESHOLD:
            return Status("Arriving late", "warn")
        return Status("In the air", "live")
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
        word = day_word(departure_estimate(booking, snapshot), now, origin_tz)
        if word is not None:
            return Status(word, "plan")
    return Status("Scheduled", "quiet")


def at_the_gate(
    phase: Phase, booking: Booking, snapshot: FlightSnapshot | None, now: datetime
) -> bool:
    """Whether the aircraft is parked, as far as anyone can tell.

    On-blocks is the one event the feed confirms unreliably: wheels down comes off the
    radar, but the gate needs a word from the airline or the airport that often comes
    late or never, and the poller stops asking ninety minutes after the landing. So a
    landed flight whose gate time has come and gone counts as parked rather than left
    taxiing for a confirmation that may not arrive. With no gate time at all there is
    nothing to wait for, and it is parked too.
    """
    if phase != LANDED:
        return False
    if snapshot is not None and snapshot.actual_in is not None:
        return True
    expected = arrival_estimate(booking, snapshot)
    return expected is None or expected <= now


def day_word(instant: datetime, now: datetime, tz: str) -> str | None:
    """The day the flight leaves, as the calendar at `tz` has it, else nothing.

    The day-of window is a rolling twenty-four hours, so a morning flight enters it the
    morning before. The pill still names a day, so it takes the day from the airport's
    own date rather than from the width of the window. A departure already behind the
    clock with no word from the feed is inside the window too, and gets no day at all.
    """
    ahead = (to_local(instant, tz).date() - to_local(now, tz).date()).days
    if ahead == 0:
        return "Today"
    if ahead == 1:
        return "Tomorrow"
    return None


def milestone(
    phase: Phase, booking: Booking, snapshot: FlightSnapshot | None, *, now: datetime
) -> Milestone | None:
    """The first thing still ahead of the flight that has a time against it.

    The ladder is departure, wheels up, landing, the gate. A rung is skipped once it has
    happened, and also when nobody has said when it will: nothing upstream estimates
    wheels up, so a flight taxiing out counts to its landing, and a flight with no
    arrival time at all has no milestone rather than a made-up one.

    A rung whose time passed as long ago as the poller's own patience is not waiting on
    the feed any more. Whatever stopped the news - a lost flight, a tripped budget, no
    key to poll with - the count would otherwise grow for as long as the booking stands.
    """
    if phase == CANCELLED or flown(booking):
        return None
    next_up = _next_rung(phase, booking, snapshot)
    if next_up is None or next_up.target <= ensure_utc(now) - ABANDON_AFTER:
        return None
    return next_up


def _next_rung(phase: Phase, booking: Booking, snapshot: FlightSnapshot | None) -> Milestone | None:
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


def logo_url(carrier: str) -> str:
    """The airline's mark, from the set Google draws for its own flight search."""
    return f"https://www.gstatic.com/flights/airline_logos/70px/{carrier}.png"


def destination_iata(booking: Booking, snapshot: FlightSnapshot | None) -> str:
    """Where the flight is going: the booked airport until a diversion names another."""
    if snapshot is not None and snapshot.diverted and snapshot.destination_iata:
        return snapshot.destination_iata
    return booking.dest_iata


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


# Where an email that came to something stands, as a word and the tone it is drawn in.
# A failure is not in here: what to say about one is decided by `import_status` from the
# retry state, which is the difference between a wait and a decision to make.
_SETTLED: dict[str, Status] = {
    IngestOutcome.CREATED: Status("Imported", "ok"),
    IngestOutcome.DUPLICATE: Status("Already added", "quiet"),
    IngestOutcome.IGNORED: Status("Ignored", "quiet"),
    IngestOutcome.NO_FLIGHT: Status("Ignored", "quiet"),
}

# The line under the subject, for the outcomes that are the same sentence every time. A
# failure says what went wrong instead, in its own words. Plainly, and about the email
# rather than about the service: a person reading these has flagged something and wants
# to know where it got to, not to be told what the importer did with its afternoon.
_ACCOUNT: dict[str, str] = {
    IngestOutcome.DUPLICATE: "Every flight in this email was already on the board.",
    IngestOutcome.IGNORED: "Marked as holding no flight. The email flag will be unset shortly.",
    IngestOutcome.NO_FLIGHT: "Marked as holding no flight.",
}

UNTITLED = "(no subject)"


def import_status(row: IngestLog) -> Status:
    """Where one email stands with the importer.

    A failure is two different things to a person: one the service will have another go
    at on its own, and one that is waiting on them. The retry state is what tells them
    apart, so it is what the pill is read from.
    """
    if row.outcome == IngestOutcome.ERROR:
        return Status("Needs attention", "stop") if set_aside(row) else Status("Retrying", "warn")
    return _SETTLED.get(row.outcome, Status("Read", "quiet"))


@dataclass(frozen=True)
class MailImport:
    """One email the service has looked at, as the email page shows it."""

    row: IngestLog
    flights: tuple[Booking, ...] = ()

    @property
    def status(self) -> Status:
        return import_status(self.row)

    @property
    def waiting(self) -> bool:
        """Whether this one has stopped being history and is asking for a decision."""
        return set_aside(self.row)

    @property
    def subject(self) -> str:
        """An email names itself, or it is named by the only other thing we know: nothing."""
        return self.row.subject.strip() or UNTITLED

    @property
    def when(self) -> str:
        return ago(self.row.processed_at, datetime.now(UTC))

    @property
    def account(self) -> str | None:
        """The sentence under the subject, where there is one worth reading.

        An import says nothing here: the flights it added are shown underneath, and they
        say it better than a sentence about them would.
        """
        if self.row.outcome == IngestOutcome.ERROR:
            return notices.sentence(self.row.error)
        return _ACCOUNT.get(self.row.outcome)


def ago(then: datetime, now: datetime) -> str:
    """`4m ago`, `3h ago`, `3d ago`: how long since something last happened.

    One unit, always the largest that fits. Minutes stop mattering the moment there are
    hours to say instead: nobody reading how long ago an email was read is counting them.
    """
    minutes = int(max(now - then, timedelta()).total_seconds() // 60)
    days, rest = divmod(minutes, 1440)
    hours = rest // 60
    if days:
        return f"{days}d ago"
    if hours:
        return f"{hours}h ago"
    return f"{minutes}m ago"


async def build_mail_imports(session: AsyncSession, rows: Iterable[IngestLog]) -> list[MailImport]:
    """Attach to each email the flights of its that are still on the board."""
    log_rows = list(rows)
    flights = await booking_repo.from_messages(session, [row.message_id for row in log_rows])
    return [MailImport(row, tuple(flights.get(row.message_id, ()))) for row in log_rows]


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
