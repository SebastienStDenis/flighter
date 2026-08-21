"""Reading and writing bookings - the one door into the table.

Ingestion, the web app and the poller all go through here so that two rules hold
everywhere: a local wall-clock time becomes UTC exactly once, using the airport's zone,
and a booking that becomes active is queued for the poller in the same transaction that
activated it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .airports import airport_tz
from .cadence import first_poll_at
from .models import Booking, BookingStatus, EventKind, FlightEvent, FlightSnapshot
from .timezones import same_local_date, to_local, to_utc

log = logging.getLogger(__name__)

# id, the timestamps and the dedupe date are the database's to set, not a caller's.
_EDITABLE_FIELDS = frozenset(Booking.__table__.columns.keys()) - {
    "id",
    "created_at",
    "updated_at",
    "departure_local_date",
}

# Poller bookkeeping, not a fact about the trip; moving it is not an edit worth a sync.
_UNTRACKED_EDITS = frozenset({"next_poll_at"})

# Wide enough to catch every booking that could share a local calendar day with the
# departure being checked, on any pair of zones; the local date decides from there.
_DEDUPE_WINDOW = timedelta(days=1)


def flight_label(booking: Booking) -> str:
    """`DL1234 JFK -> LAX`, the one string that identifies a flight to a human."""
    return (
        f"{booking.marketing_carrier}{booking.marketing_number} "
        f"{booking.origin_iata} -> {booking.dest_iata}"
    )


def operated_note(carrier: str | None, number: str | None) -> str | None:
    """`Operated as LH479`: the number on the aeroplane, when it is not the one booked.

    An email sometimes names the airline flying the leg without its number, and then
    `Operated by LH` is as much as can be said.
    """
    if not carrier:
        return None
    return f"Operated as {carrier}{number}" if number else f"Operated by {carrier}"


def to_booking_times(
    departure_local: datetime,
    origin_tz: str,
    arrival_local: datetime | None,
    dest_tz: str,
) -> tuple[datetime, datetime | None]:
    """Convert a booking's two wall-clock readings into UTC instants.

    Each end is read in its own airport's zone, which is the whole point: an arrival
    that reads earlier on the clock than the departure is an ordinary date-line flight,
    and one that lands the next calendar day is an ordinary overnight. Neither is a
    mistake to fix up, so nothing here compares the two or shifts a date.
    """
    departure_utc = to_utc(departure_local, origin_tz)
    arrival_utc = to_utc(arrival_local, dest_tz) if arrival_local is not None else None
    return departure_utc, arrival_utc


async def create_booking(
    session: AsyncSession,
    *,
    marketing_carrier: str,
    marketing_number: str,
    origin_iata: str,
    dest_iata: str,
    departure_local: datetime,
    arrival_local: datetime | None = None,
    source: str,
    source_message_id: str | None = None,
    confirmation_code: str | None = None,
    seat: str | None = None,
    notes: str | None = None,
    status: str = BookingStatus.ACTIVE,
    extraction_confidence: float | None = None,
    operating_carrier: str | None = None,
    operating_number: str | None = None,
) -> Booking:
    """Create a booking from times stated in each airport's local clock.

    `departure_local` is naive wall clock at the origin and `arrival_local` naive wall
    clock at the destination. This is the only place either becomes UTC.
    """
    origin = _code(origin_iata)
    dest = _code(dest_iata)
    origin_tz = await airport_tz(session, origin)
    departure_utc, arrival_utc = to_booking_times(
        departure_local, origin_tz, arrival_local, await airport_tz(session, dest)
    )

    now = datetime.now(UTC)
    booking = Booking(
        source=source,
        source_message_id=source_message_id,
        marketing_carrier=_carrier(marketing_carrier),
        marketing_number=_number(marketing_number),
        operating_carrier=_carrier(operating_carrier) if operating_carrier else None,
        operating_number=_number(operating_number) if operating_number else None,
        origin_iata=origin,
        dest_iata=dest,
        scheduled_departure_utc=departure_utc,
        departure_local_date=to_local(departure_utc, origin_tz).date(),
        scheduled_arrival_utc=arrival_utc,
        confirmation_code=confirmation_code,
        seat=seat,
        notes=notes,
        status=status,
        extraction_confidence=extraction_confidence,
        next_poll_at=first_poll_at(now, departure_utc) if status == BookingStatus.ACTIVE else None,
    )
    session.add(booking)
    await session.flush()
    if booking.status == BookingStatus.ACTIVE:
        _record(session, booking, EventKind.BOOKING_ADDED)
        await session.flush()
    return booking


def _record(session: AsyncSession, booking: Booking, kind: EventKind) -> None:
    """Queue the booking for the calendar by the same road every snapshot change takes.

    Writing the calendar from here would put a network call inside a web request and
    lose the entry outright when iCloud is down; a FlightEvent row is delivered by the
    dispatcher, retried until it lands, and pushes nobody because neither kind is one
    the notifier sends.
    """
    session.add(FlightEvent(booking_id=booking.id, kind=kind))


async def get_booking(session: AsyncSession, booking_id: int) -> Booking | None:
    return await session.get(Booking, booking_id)


async def update_booking(
    session: AsyncSession, booking_id: int, **fields: object
) -> Booking | None:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        return None

    unknown = set(fields) - _EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"not booking columns: {', '.join(sorted(unknown))}")

    before = {name: getattr(booking, name) for name in fields}
    for name, value in fields.items():
        setattr(booking, name, _normalised(name, value))
    changed = {name for name in fields if getattr(booking, name) != before[name]}

    if "scheduled_departure_utc" in fields or "origin_iata" in fields:
        booking.departure_local_date = await _local_date(
            session, booking.scheduled_departure_utc, booking.origin_iata
        )

    # Reactivating a booking has to hand it back to the poller, or it sits untouched
    # until something else happens to write a next_poll_at.
    if (
        booking.status == BookingStatus.ACTIVE
        and "next_poll_at" not in fields
        and booking.next_poll_at is None
    ):
        booking.next_poll_at = first_poll_at(datetime.now(UTC), booking.scheduled_departure_utc)

    # A booking kept after review is reaching the calendar for the first time, so it is
    # added rather than edited; one that was already active and moved is restated.
    if booking.status == BookingStatus.ACTIVE and changed - _UNTRACKED_EDITS:
        kind = EventKind.BOOKING_ADDED if "status" in changed else EventKind.BOOKING_EDITED
        _record(session, booking, kind)

    await session.flush()
    return booking


async def delete_booking(session: AsyncSession, booking_id: int) -> Booking | None:
    """Archive rather than delete, and stop polling it.

    Archiving keeps the snapshots and events that reference the booking, and the dedupe
    index skips archived rows, so the same flight can be added again afterwards. The
    calendar event is the caller's to clean up.
    """
    booking = await session.get(Booking, booking_id)
    if booking is None:
        return None

    booking.status = BookingStatus.ARCHIVED
    booking.next_poll_at = None
    await session.flush()
    return booking


async def list_bookings(
    session: AsyncSession,
    *,
    statuses: Sequence[str] | None = None,
    upcoming_only: bool = False,
) -> list[Booking]:
    stmt = select(Booking).order_by(Booking.scheduled_departure_utc)
    if statuses is not None:
        stmt = stmt.where(Booking.status.in_(list(statuses)))
    if upcoming_only:
        # A flight that is in the air has departed but is still very much upcoming to
        # the person waiting for it, so a booking drops off only once it has landed.
        now = datetime.now(UTC)
        stmt = stmt.where(
            or_(
                Booking.scheduled_arrival_utc >= now,
                and_(
                    Booking.scheduled_arrival_utc.is_(None),
                    Booking.scheduled_departure_utc >= now,
                ),
            )
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_duplicate(
    session: AsyncSession,
    carrier: str,
    number: str,
    departure_utc: datetime,
) -> Booking | None:
    """The booking already on the list for this flight on this day, if there is one.

    Same day means the calendar day at the origin airport, read off the candidate's own
    origin since a duplicate necessarily shares it: a 23:30 departure re-sent with a
    corrected time must be recognised, and it is already tomorrow in UTC. The flight is
    matched on either pair of codes, because the ticket's marketing number and the
    number FlightAware tracks are the same aeroplane.
    """
    flight_carrier = _carrier(carrier)
    flight_number = _number(number)
    departure_utc = to_utc(departure_utc, "UTC")
    stmt = (
        select(Booking)
        .where(
            or_(
                and_(
                    Booking.marketing_carrier == flight_carrier,
                    Booking.marketing_number == flight_number,
                ),
                and_(
                    Booking.operating_carrier == flight_carrier,
                    Booking.operating_number == flight_number,
                ),
            ),
            Booking.status != BookingStatus.ARCHIVED,
            Booking.scheduled_departure_utc >= departure_utc - _DEDUPE_WINDOW,
            Booking.scheduled_departure_utc <= departure_utc + _DEDUPE_WINDOW,
        )
        .order_by(Booking.id)
    )
    for candidate in (await session.scalars(stmt)).all():
        tz = await airport_tz(session, candidate.origin_iata)
        if same_local_date(candidate.scheduled_departure_utc, departure_utc, tz):
            return candidate
    return None


async def latest_snapshot(session: AsyncSession, booking_id: int) -> FlightSnapshot | None:
    """The newest observation of one booking, or None before the first poll."""
    return (await latest_snapshots(session, [booking_id])).get(booking_id)


async def latest_snapshots(
    session: AsyncSession, booking_ids: Sequence[int]
) -> dict[int, FlightSnapshot]:
    """The newest snapshot per booking, in one query.

    Snapshots are append-only, so "newest row wins" is the whole of the read model. The
    newest is picked per booking by a correlated subquery rather than by `DISTINCT ON`,
    which SQLite does not have and would quietly ignore.
    """
    if not booking_ids:
        return {}
    other = aliased(FlightSnapshot)
    newest_id = (
        select(other.id)
        .where(other.booking_id == FlightSnapshot.booking_id)
        .order_by(other.observed_at.desc(), other.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    stmt = select(FlightSnapshot).where(
        FlightSnapshot.booking_id.in_(list(booking_ids)), FlightSnapshot.id == newest_id
    )
    return {snapshot.booking_id: snapshot for snapshot in (await session.scalars(stmt)).all()}


async def _local_date(session: AsyncSession, departure_utc: datetime, origin_iata: str) -> date:
    return to_local(departure_utc, await airport_tz(session, origin_iata)).date()


# Normalised on the way in, because all three are part of the dedupe key: "aa" and "AA"
# have to be the same airline for the unique index to do its job.
def _code(iata: str) -> str:
    return iata.strip().upper()


def _carrier(carrier: str) -> str:
    return carrier.strip().upper()


def _number(number: str) -> str:
    """Some airlines zero-pad the flight number and some do not; the dedupe index
    cannot tell AA0100 from AA100 unless both are stored the same way."""
    stripped = number.strip()
    return stripped.lstrip("0") or stripped


_NORMALISERS = {
    "marketing_carrier": _carrier,
    "operating_carrier": _carrier,
    "marketing_number": _number,
    "operating_number": _number,
    "origin_iata": _code,
    "dest_iata": _code,
}


def _normalised(name: str, value: object) -> object:
    """Apply the same cleanup an insert would.

    An edit that wrote `ac871` verbatim would read as a different airline to the dedupe
    index than the `AC871` an insert stores, so the two paths have to agree.
    """
    normalise = _NORMALISERS.get(name)
    if normalise is None or not isinstance(value, str):
        return value
    return normalise(value)
