"""Reading and writing bookings - the one door into the table.

Ingestion, the web app and the poller all go through here so that two rules hold
everywhere: a local wall-clock time becomes UTC exactly once, using the airport's zone,
and a booking that becomes active is queued for the poller in the same transaction that
activated it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .airports import airport_tz
from .cadence import first_poll_at
from .models import Booking, BookingStatus, EventKind, FlightEvent, FlightSnapshot
from .timezones import same_local_date, to_local, to_utc

log = logging.getLogger(__name__)

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
    friend_name: str | None = None,
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
        friend_name=_friend_name(friend_name),
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


async def update_ticket(
    session: AsyncSession,
    booking_id: int,
    *,
    confirmation_code: str | None,
    seat: str | None,
    notes: str | None,
    friend_name: str | None,
) -> Booking | None:
    """Change what is written on the ticket, which is all a person is allowed to change.

    The flight itself - number, airports, times - is the airline's statement and is never
    edited: a booking that names the wrong flight is deleted and the right one added.
    """
    booking = await session.get(Booking, booking_id)
    if booking is None:
        return None
    shown = (booking.confirmation_code, booking.seat, booking.friend_name)
    booking.confirmation_code = confirmation_code
    booking.seat = seat
    booking.notes = notes
    booking.friend_name = _friend_name(friend_name)
    # The calendar entry carries the code and the seat; notes never leave the app.
    if (
        booking.status != BookingStatus.ARCHIVED
        and (
            confirmation_code,
            seat,
            booking.friend_name,
        )
        != shown
    ):
        _record(session, booking, EventKind.BOOKING_EDITED)
    await session.flush()
    return booking


async def queue_friend_calendar_updates(session: AsyncSession) -> list[Booking]:
    result = await session.execute(
        select(Booking).where(
            Booking.friend_name.is_not(None), Booking.status != BookingStatus.ARCHIVED
        )
    )
    bookings = list(result.scalars())
    for booking in bookings:
        _record(session, booking, EventKind.BOOKING_EDITED)
    await session.flush()
    return bookings


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


async def list_recently_flown(
    session: AsyncSession, limit: int, *, include_friends: bool = True
) -> list[Booking]:
    """The last few flights that have been taken, newest first and never more."""
    stmt = select(Booking).where(Booking.status == BookingStatus.COMPLETED)
    if not include_friends:
        stmt = stmt.where(Booking.friend_name.is_(None))
    result = await session.execute(
        stmt.order_by(Booking.scheduled_departure_utc.desc()).limit(limit)
    )
    return list(result.scalars())


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


async def on_board_from_message(session: AsyncSession, message_id: str) -> bool:
    """Whether any flight this email put on the board is still there.

    Deleting archives, so an email whose every flight is gone has nothing left to show
    for having been read; the dedupe index lets the same flight back on, and flagging
    the email again is how a person asks for it.
    """
    stmt = (
        select(Booking.id)
        .where(
            Booking.source_message_id == message_id,
            Booking.status != BookingStatus.ARCHIVED,
        )
        .limit(1)
    )
    return await session.scalar(stmt) is not None


async def from_messages(
    session: AsyncSession, message_ids: Sequence[str]
) -> dict[str, list[Booking]]:
    """The flights each of these emails put on the board and that are still there.

    One query for a page of them rather than one per row. An email whose flights have
    all been deleted is absent rather than empty: nothing on the board came of it, which
    is the same answer as never having booked anything.
    """
    if not message_ids:
        return {}
    result = await session.execute(
        select(Booking)
        .where(
            Booking.source_message_id.in_(message_ids),
            Booking.status != BookingStatus.ARCHIVED,
        )
        .order_by(Booking.scheduled_departure_utc)
    )
    found: dict[str, list[Booking]] = {}
    for booking in result.scalars():
        found.setdefault(str(booking.source_message_id), []).append(booking)
    return found


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


def _friend_name(name: str | None) -> str | None:
    if name is None:
        return None
    return name.strip() or None
