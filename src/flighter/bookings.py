"""Reading and writing bookings - the one door into the table.

Ingestion, the web app and the poller all go through here so that two rules hold
everywhere: a local wall-clock time becomes UTC exactly once, using the airport's zone,
and a booking that becomes active is queued for the poller in the same transaction that
activated it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Date, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .airports import airport_tz
from .models import Booking
from .timezones import to_utc

log = logging.getLogger(__name__)

ARCHIVED = "archived"
ACTIVE = "active"

# id and the timestamps are the database's to set, not a caller's.
_EDITABLE_FIELDS = frozenset(Booking.__table__.columns.keys()) - {"id", "created_at", "updated_at"}


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
    status: str = ACTIVE,
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
    departure_utc, arrival_utc = to_booking_times(
        departure_local,
        await airport_tz(session, origin),
        arrival_local,
        await airport_tz(session, dest),
    )

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
        scheduled_arrival_utc=arrival_utc,
        confirmation_code=confirmation_code,
        seat=seat,
        notes=notes,
        status=status,
        extraction_confidence=extraction_confidence,
        next_poll_at=datetime.now(UTC) if status == ACTIVE else None,
    )
    session.add(booking)
    await session.flush()
    return booking


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

    for name, value in fields.items():
        setattr(booking, name, _normalised(name, value))

    # Reactivating a booking has to hand it back to the poller, or it sits untouched
    # until something else happens to write a next_poll_at.
    if booking.status == ACTIVE and "next_poll_at" not in fields and booking.next_poll_at is None:
        booking.next_poll_at = datetime.now(UTC)

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

    booking.status = ARCHIVED
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
    """The row the `bookings_dedupe` index would collide with, if there is one.

    Mirrors the index exactly, including the UTC calendar date: the same booking re-sent
    with a corrected departure time must be recognised as the flight we already have.
    """
    day = to_utc(departure_utc, "UTC").date()
    stmt = select(Booking).where(
        Booking.marketing_carrier == _carrier(carrier),
        Booking.marketing_number == _number(number),
        Booking.status != ARCHIVED,
        cast(func.timezone("UTC", Booking.scheduled_departure_utc), Date) == day,
    )
    result = await session.execute(stmt)
    return result.scalars().first()


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
