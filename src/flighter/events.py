"""Change detection over consecutive AeroAPI snapshots, and the fan-out to push and calendar.

The poller writes a snapshot and calls straight in here. Everything a person or a
calendar ever hears about a flight starts life as a FlightEvent row written by this
module, which is what makes delivery restartable: the two `*_at` columns are the only
record of what has already gone out.

Delivery never holds a database transaction open across a network call. Each pass reads
what is pending and closes the session, talks to Pushover or iCloud, then opens a fresh
session to stamp each success on its own, so a failure part-way through a batch keeps
everything delivered before it and never locks the web UI out meanwhile.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from .bookings import latest_snapshots
from .db import session_scope
from .models import Airport, Booking, BookingStatus, EventKind, FlightEvent, FlightSnapshot
from .phase import ARRIVAL_DELAY_THRESHOLD, DEPARTURE_DELAY_THRESHOLD
from .timezones import FALLBACK_TZ, ensure_utc, parse_instant

log = logging.getLogger(__name__)

# How long a delivery keeps being retried after the event happened. A push about a gate
# that changed six hours ago is not news, and a calendar a day out of date is corrected
# by the next event anyway, so an outage longer than this is allowed to lose the event
# rather than keep every later pass busy with it.
NOTIFY_WINDOW = timedelta(hours=6)
CALENDAR_WINDOW = timedelta(hours=24)

# An arrival estimate moves all flight long and nobody wants a push for each wobble, but
# the calendar block should still be right, so this one kind syncs without notifying.
NOTIFIABLE_KINDS = frozenset(
    {
        EventKind.GATE_ASSIGNED,
        EventKind.GATE_CHANGED,
        EventKind.TERMINAL_CHANGED,
        EventKind.DEPARTURE_DELAYED,
        EventKind.DEPARTURE_MOVED_EARLIER,
        EventKind.DEPARTED,
        EventKind.LANDED,
        EventKind.BAGGAGE_CLAIM_ASSIGNED,
        EventKind.CANCELLED,
        EventKind.DIVERTED,
    }
)

# Which kinds restate a given time field. The newest of them carries the last value the
# user was actually told, which is the baseline the dead band has to be measured from.
BASELINE_KINDS: Mapping[str, tuple[EventKind, ...]] = {
    "estimated_out": (EventKind.DEPARTURE_DELAYED, EventKind.DEPARTURE_MOVED_EARLIER),
    "estimated_in": (EventKind.ARRIVAL_TIME_CHANGED,),
}


@dataclass(frozen=True)
class DetectedChange:
    """One material difference, before it becomes a row."""

    kind: EventKind
    old_value: str | None
    new_value: str | None


class Notifier(Protocol):
    async def flight_event(
        self, booking: Booking, event: FlightEvent, *, origin_tz: str, dest_tz: str
    ) -> None: ...


class Calendar(Protocol):
    async def upsert(self, booking: Booking, snapshot: FlightSnapshot | None) -> str | None: ...


def _iso(value: datetime | None) -> str | None:
    """Times live in the value columns as UTC ISO-8601, so they round-trip as baselines."""
    return None if value is None else ensure_utc(value).isoformat()


def _time_change(
    *,
    kind_later: EventKind,
    kind_earlier: EventKind | None,
    current: datetime | None,
    baseline: datetime | None,
    dead_band: timedelta,
) -> DetectedChange | None:
    if current is None or baseline is None:
        return None
    delta = current - baseline
    if delta > dead_band:
        return DetectedChange(kind_later, _iso(baseline), _iso(current))
    if -delta > dead_band:
        return DetectedChange(kind_earlier or kind_later, _iso(baseline), _iso(current))
    return None


def diff_snapshots(
    previous: FlightSnapshot | None,
    current: FlightSnapshot,
    *,
    baselines: Mapping[str, str] | None = None,
) -> list[DetectedChange]:
    """The material differences between two observations.

    `baselines` maps a time field to the last value already reported for it. Measuring
    the dead band against the previous *snapshot* would let a flight slip 8 minutes on
    every poll and never trip it; measuring against the last value the user was told
    means the band bounds how wrong their information is, not how fast it changes. With
    nothing reported yet the scheduled time plays that role, since that is what the
    ticket says.
    """
    baselines = baselines or {}

    if previous is None:
        # A first observation states the world rather than changing it, so nothing has
        # happened yet. A flight that is already cancelled or diverted is the exception:
        # that is news whether or not we watched it happen.
        changes = []
        if current.cancelled:
            changes.append(DetectedChange(EventKind.CANCELLED, None, "true"))
        if current.diverted:
            changes.append(DetectedChange(EventKind.DIVERTED, None, "true"))
        return changes

    changes = []

    # A field going value -> null is AeroAPI dropping it, not the airport unassigning a
    # gate, so only an arriving or differing value counts.
    if current.gate_origin and current.gate_origin != previous.gate_origin:
        kind = EventKind.GATE_CHANGED if previous.gate_origin else EventKind.GATE_ASSIGNED
        changes.append(DetectedChange(kind, previous.gate_origin, current.gate_origin))

    if current.terminal_origin and current.terminal_origin != previous.terminal_origin:
        changes.append(
            DetectedChange(
                EventKind.TERMINAL_CHANGED, previous.terminal_origin, current.terminal_origin
            )
        )

    departure = _time_change(
        kind_later=EventKind.DEPARTURE_DELAYED,
        kind_earlier=EventKind.DEPARTURE_MOVED_EARLIER,
        current=ensure_utc(current.estimated_out),
        baseline=parse_instant(baselines.get("estimated_out"))
        or ensure_utc(previous.scheduled_out)
        or ensure_utc(previous.estimated_out),
        dead_band=DEPARTURE_DELAY_THRESHOLD,
    )
    if departure:
        changes.append(departure)

    arrival = _time_change(
        kind_later=EventKind.ARRIVAL_TIME_CHANGED,
        kind_earlier=None,
        current=ensure_utc(current.estimated_in),
        baseline=parse_instant(baselines.get("estimated_in"))
        or ensure_utc(previous.scheduled_in)
        or ensure_utc(previous.estimated_in),
        dead_band=ARRIVAL_DELAY_THRESHOLD,
    )
    if arrival:
        changes.append(arrival)

    # Pushback, not wheels up. To anyone waiting on this flight "departed" means it left
    # the gate, and the two are twenty minutes apart. Wheels up gets no event of its own:
    # it moves the phase to airborne, which the widget already shows.
    if current.actual_out and not previous.actual_out:
        changes.append(DetectedChange(EventKind.DEPARTED, None, _iso(current.actual_out)))

    # Wheels down, and here the runway time is the one people mean by "landed"; being at
    # the gate is a separate relief, and the bag claim event covers it.
    if current.actual_on and not previous.actual_on:
        changes.append(DetectedChange(EventKind.LANDED, None, _iso(current.actual_on)))

    if current.baggage_claim and not previous.baggage_claim:
        changes.append(
            DetectedChange(EventKind.BAGGAGE_CLAIM_ASSIGNED, None, current.baggage_claim)
        )

    # AeroAPI's `cancelled` is an untracked flag, not an airline status: the spec warns
    # it goes true "for a number of reasons ... including cancellation by the airline,
    # but that will not always be the case". Still worth a push, but the copy
    # deliberately attributes it rather than asserting the flight is off.
    if current.cancelled and not previous.cancelled:
        changes.append(DetectedChange(EventKind.CANCELLED, "false", "true"))

    if current.diverted and not previous.diverted:
        changes.append(DetectedChange(EventKind.DIVERTED, "false", "true"))

    return changes


async def detect_changes(
    session: AsyncSession,
    booking: Booking,
    previous: FlightSnapshot | None,
    current: FlightSnapshot,
) -> list[FlightEvent]:
    """Diff the newest two snapshots and persist whatever materially moved."""
    baselines = await _reported_baselines(session, booking)
    events = [
        FlightEvent(
            booking_id=booking.id,
            kind=change.kind,
            old_value=change.old_value,
            new_value=change.new_value,
        )
        for change in diff_snapshots(previous, current, baselines=baselines)
    ]
    if events:
        session.add_all(events)
        await session.flush()
        log.info("booking %s: %s", booking.id, ", ".join(event.kind for event in events))
    return events


async def _reported_baselines(session: AsyncSession, booking: Booking) -> dict[str, str]:
    baselines: dict[str, str] = {}
    for field, kinds in BASELINE_KINDS.items():
        stmt = (
            select(FlightEvent.new_value)
            .where(FlightEvent.booking_id == booking.id, FlightEvent.kind.in_(kinds))
            .order_by(FlightEvent.occurred_at.desc(), FlightEvent.id.desc())
            .limit(1)
        )
        value = (await session.execute(stmt)).scalar_one_or_none()
        if value:
            baselines[field] = value
    return baselines


async def dispatch_pending(notifier: Notifier, calendar: Calendar) -> None:
    """Deliver every undelivered event, stamping each column only once it has landed.

    The two consumers are stamped independently and neither is allowed to raise past
    this point, so a dead calendar credential never costs a push and a failed delivery
    simply stays pending for the next pass.
    """
    await _dispatch_notifications(notifier)
    await _dispatch_calendar(calendar)


def _pending(
    column: InstrumentedAttribute[datetime | None], since: datetime
) -> Select[tuple[FlightEvent, Booking]]:
    """Events still owed to one consumer, for bookings that are still on the list.

    An archived booking was deleted by the person it belonged to; a push or a calendar
    entry about it would put the flight straight back.
    """
    return (
        select(FlightEvent, Booking)
        .join(Booking, Booking.id == FlightEvent.booking_id)
        .where(
            column.is_(None),
            FlightEvent.occurred_at >= since,
            Booking.status != BookingStatus.ARCHIVED,
        )
        .order_by(FlightEvent.id)
    )


async def _dispatch_notifications(notifier: Notifier) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        rows = (await session.execute(_pending(FlightEvent.notified_at, now - NOTIFY_WINDOW))).all()
        zones = {
            booking.id: await _zones(session, booking)
            for booking in {booking.id: booking for _, booking in rows}.values()
        }

    for event, booking in rows:
        if event.kind in NOTIFIABLE_KINDS:
            origin_tz, dest_tz = zones[booking.id]
            try:
                await notifier.flight_event(booking, event, origin_tz=origin_tz, dest_tz=dest_tz)
            except Exception:
                log.warning("push for event %s failed; will retry", event.id, exc_info=True)
                continue
        # Kinds nobody is pushed about are stamped anyway, so they stop being selected.
        await _stamp(FlightEvent.notified_at, [event.id])


async def _dispatch_calendar(calendar: Calendar) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        rows = (
            await session.execute(_pending(FlightEvent.calendar_synced_at, now - CALENDAR_WINDOW))
        ).all()
        bookings = {booking.id: booking for _, booking in rows}
        snapshots = await latest_snapshots(session, list(bookings))

    # The calendar carries whole-flight state, not a per-event delta, so however many
    # events a poll produced the booking needs exactly one upsert.
    pending: dict[int, list[int]] = {}
    for event, booking in rows:
        pending.setdefault(booking.id, []).append(event.id)

    for booking_id, event_ids in pending.items():
        booking = bookings[booking_id]
        try:
            uid = await calendar.upsert(booking, snapshots.get(booking_id))
        except Exception:
            log.warning(
                "calendar sync for booking %s failed; will retry", booking_id, exc_info=True
            )
            continue
        if uid and uid != booking.calendar_event_uid:
            async with session_scope() as session:
                await session.execute(
                    update(Booking).where(Booking.id == booking_id).values(calendar_event_uid=uid)
                )
        await _stamp(FlightEvent.calendar_synced_at, event_ids)


async def _stamp(column: InstrumentedAttribute[datetime | None], event_ids: Sequence[int]) -> None:
    async with session_scope() as session:
        await session.execute(
            update(FlightEvent)
            .where(FlightEvent.id.in_(list(event_ids)))
            .values({column.key: datetime.now(UTC)})
        )


async def _zones(session: AsyncSession, booking: Booking) -> tuple[str, str]:
    stmt = select(Airport.iata, Airport.tz).where(
        Airport.iata.in_([booking.origin_iata, booking.dest_iata])
    )
    found = {iata: tz for iata, tz in (await session.execute(stmt)).all()}
    return (
        found.get(booking.origin_iata, FALLBACK_TZ),
        found.get(booking.dest_iata, FALLBACK_TZ),
    )
