"""Change detection over consecutive AeroAPI snapshots, and the fan-out to push and calendar.

The poller writes a snapshot and calls straight in here. Everything a person or a
calendar ever hears about a flight starts life as a FlightEvent row written by this
module, which is what makes delivery restartable: the two `*_at` columns are the only
record of what has already gone out.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Airport, Booking, FlightEvent, FlightSnapshot
from .timezones import FALLBACK_TZ

log = logging.getLogger(__name__)

GATE_ASSIGNED = "GateAssigned"
GATE_CHANGED = "GateChanged"
TERMINAL_CHANGED = "TerminalChanged"
DEPARTURE_DELAYED = "DepartureDelayed"
DEPARTURE_MOVED_EARLIER = "DepartureMovedEarlier"
ARRIVAL_TIME_CHANGED = "ArrivalTimeChanged"
DEPARTED = "Departed"
LANDED = "Landed"
BAGGAGE_CLAIM_ASSIGNED = "BaggageClaimAssigned"
CANCELLED = "Cancelled"
DIVERTED = "Diverted"

# Airlines nudge estimates by a minute or two constantly; below these a change is noise.
DEPARTURE_DEAD_BAND = timedelta(minutes=10)
ARRIVAL_DEAD_BAND = timedelta(minutes=15)

# An arrival estimate moves all flight long and nobody wants a push for each wobble, but
# the calendar block should still be right, so this one kind syncs without notifying.
NOTIFIABLE_KINDS = frozenset(
    {
        GATE_ASSIGNED,
        GATE_CHANGED,
        TERMINAL_CHANGED,
        DEPARTURE_DELAYED,
        DEPARTURE_MOVED_EARLIER,
        DEPARTED,
        LANDED,
        BAGGAGE_CLAIM_ASSIGNED,
        CANCELLED,
        DIVERTED,
    }
)

# Which kinds restate a given time field. The newest of them carries the last value the
# user was actually told, which is the baseline the dead band has to be measured from.
BASELINE_KINDS: Mapping[str, tuple[str, ...]] = {
    "estimated_out": (DEPARTURE_DELAYED, DEPARTURE_MOVED_EARLIER),
    "estimated_in": (ARRIVAL_TIME_CHANGED,),
}


@dataclass(frozen=True)
class DetectedChange:
    """One material difference, before it becomes a row."""

    kind: str
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
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _time_change(
    *,
    kind_later: str,
    kind_earlier: str | None,
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
        # See the note on the `cancelled` flag below: already-true on first sight still
        # counts, since a flight AeroAPI is not tracking is news either way.
        if current.cancelled:
            changes.append(DetectedChange(CANCELLED, None, "true"))
        if current.diverted:
            changes.append(DetectedChange(DIVERTED, None, "true"))
        return changes

    changes = []

    # A field going value -> null is AeroAPI dropping it, not the airport unassigning a
    # gate, so only an arriving or differing value counts.
    if current.gate_origin and current.gate_origin != previous.gate_origin:
        kind = GATE_CHANGED if previous.gate_origin else GATE_ASSIGNED
        changes.append(DetectedChange(kind, previous.gate_origin, current.gate_origin))

    if current.terminal_origin and current.terminal_origin != previous.terminal_origin:
        changes.append(
            DetectedChange(TERMINAL_CHANGED, previous.terminal_origin, current.terminal_origin)
        )

    departure = _time_change(
        kind_later=DEPARTURE_DELAYED,
        kind_earlier=DEPARTURE_MOVED_EARLIER,
        current=_aware(current.estimated_out),
        baseline=_parse(baselines.get("estimated_out"))
        or _aware(previous.scheduled_out)
        or _aware(previous.estimated_out),
        dead_band=DEPARTURE_DEAD_BAND,
    )
    if departure:
        changes.append(departure)

    arrival = _time_change(
        kind_later=ARRIVAL_TIME_CHANGED,
        kind_earlier=None,
        current=_aware(current.estimated_in),
        baseline=_parse(baselines.get("estimated_in"))
        or _aware(previous.scheduled_in)
        or _aware(previous.estimated_in),
        dead_band=ARRIVAL_DEAD_BAND,
    )
    if arrival:
        changes.append(arrival)

    # Pushback, not wheels up. To anyone waiting on this flight "departed" means it left
    # the gate, and the two are twenty minutes apart. Wheels up gets no event of its own:
    # it moves the phase to airborne, which the widget already shows.
    if current.actual_out and not previous.actual_out:
        changes.append(DetectedChange(DEPARTED, None, _iso(current.actual_out)))

    # Wheels down, and here the runway time is the one people mean by "landed"; being at
    # the gate is a separate relief, and the bag claim event covers it.
    if current.actual_on and not previous.actual_on:
        changes.append(DetectedChange(LANDED, None, _iso(current.actual_on)))

    if current.baggage_claim and not previous.baggage_claim:
        changes.append(DetectedChange(BAGGAGE_CLAIM_ASSIGNED, None, current.baggage_claim))

    # AeroAPI's `cancelled` is an untracked flag, not an airline status: the spec warns
    # it goes true "for a number of reasons ... including cancellation by the airline,
    # but that will not always be the case". Still worth a push, but the copy in
    # notify.py deliberately attributes it rather than asserting the flight is off.
    if current.cancelled and not previous.cancelled:
        changes.append(DetectedChange(CANCELLED, "false", "true"))

    if current.diverted and not previous.diverted:
        changes.append(DetectedChange(DIVERTED, "false", "true"))

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


async def dispatch_pending(session: AsyncSession, notifier: Notifier, calendar: Calendar) -> None:
    """Deliver every undelivered event, stamping each column only once it has landed.

    The two consumers are stamped independently and neither is allowed to raise past
    this point, so a dead calendar credential never costs a push and a failed delivery
    simply stays pending for the next pass.
    """
    await _dispatch_notifications(session, notifier)
    await _dispatch_calendar(session, calendar)


async def _dispatch_notifications(session: AsyncSession, notifier: Notifier) -> None:
    stmt = (
        select(FlightEvent, Booking)
        .join(Booking, Booking.id == FlightEvent.booking_id)
        .where(FlightEvent.notified_at.is_(None))
        .order_by(FlightEvent.id)
    )
    zones: dict[int, tuple[str, str]] = {}
    for event, booking in (await session.execute(stmt)).all():
        if event.kind in NOTIFIABLE_KINDS:
            if booking.id not in zones:
                zones[booking.id] = await _zones(session, booking)
            origin_tz, dest_tz = zones[booking.id]
            try:
                await notifier.flight_event(booking, event, origin_tz=origin_tz, dest_tz=dest_tz)
            except Exception:
                log.warning("push for event %s failed; will retry", event.id, exc_info=True)
                continue
        # Kinds nobody is pushed about are stamped anyway, so they stop being selected.
        event.notified_at = datetime.now(UTC)
    await session.flush()


async def _dispatch_calendar(session: AsyncSession, calendar: Calendar) -> None:
    stmt = (
        select(FlightEvent).where(FlightEvent.calendar_synced_at.is_(None)).order_by(FlightEvent.id)
    )
    pending: dict[int, list[FlightEvent]] = {}
    for event in (await session.scalars(stmt)).all():
        pending.setdefault(event.booking_id, []).append(event)

    for booking_id, events in pending.items():
        # The calendar carries whole-flight state, not a per-event delta, so however
        # many events a poll produced the booking needs exactly one upsert.
        booking = await session.get(Booking, booking_id)
        if booking is None:
            continue
        snapshot = await _latest_snapshot(session, booking_id)
        try:
            uid = await calendar.upsert(booking, snapshot)
        except Exception:
            log.warning(
                "calendar sync for booking %s failed; will retry", booking_id, exc_info=True
            )
            continue
        if uid:
            booking.calendar_event_uid = uid
        synced_at = datetime.now(UTC)
        for event in events:
            event.calendar_synced_at = synced_at
    await session.flush()


async def _latest_snapshot(session: AsyncSession, booking_id: int) -> FlightSnapshot | None:
    stmt = (
        select(FlightSnapshot)
        .where(FlightSnapshot.booking_id == booking_id)
        .order_by(FlightSnapshot.observed_at.desc(), FlightSnapshot.id.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def _zones(session: AsyncSession, booking: Booking) -> tuple[str, str]:
    stmt = select(Airport.iata, Airport.tz).where(
        Airport.iata.in_([booking.origin_iata, booking.dest_iata])
    )
    found = {iata: tz for iata, tz in (await session.execute(stmt)).all()}
    return (
        found.get(booking.origin_iata, FALLBACK_TZ),
        found.get(booking.dest_iata, FALLBACK_TZ),
    )
