"""A flight number and a date, resolved into the flight it names.

Adding a flight by hand would otherwise mean copying six fields off a ticket, four of
which the airline has already published. This asks FlightAware for the schedule instead,
so the only things typed are the two anybody can read off the pass in their hand.

The schedules endpoint rather than `/flights/{ident}`: the live feed only answers about
the next two days, and a flight is normally added to the board weeks before that.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .aeroapi import AeroAPIClient, shared_client, split_ident
from .airports import UnknownAirport, airport_tz
from .bookings import operated_note
from .timezones import parse_instant, to_local

log = logging.getLogger(__name__)

# A departure's UTC date is not its local date - a 23:00 flight from Honolulu leaves the
# next day in UTC - so the window asked about is the day either side of the one wanted,
# and the local date decides from there.
EDGE = timedelta(days=1)

# What the schedules endpoint will answer about - three months back, a year ahead - less
# the day this module reaches for on either side. Source: the AeroAPI description of
# `/schedules/{date_start}/{date_end}` (checked 2026-08).
BACK = timedelta(days=90) - EDGE
AHEAD = timedelta(days=365) - EDGE

# `AC871`, `ac 871`, `AC-0871`, and the ICAO spelling `ACA871`. The carrier is matched
# lazily so the digits win the tie: `ACA871` is ACA 871, not AC A871.
_TYPED = re.compile(r"^([A-Z0-9]{2,3}?)[\s-]*(\d{1,5})$")


class OutOfRange(ValueError):
    """A date no airline schedule has been published for, or no longer is."""

    def __init__(self, day: date) -> None:
        super().__init__(f"no published schedule for {day.isoformat()}")
        self.day = day


@dataclass(frozen=True)
class Candidate:
    """One leg the airline publishes, in the shape the add form takes.

    Both times are naive wall clock at their own airport, which is what a ticket says
    and the only reading `create_booking` accepts.
    """

    marketing_carrier: str
    marketing_number: str
    origin_iata: str
    dest_iata: str
    departure_local: datetime
    arrival_local: datetime | None = None
    operating_carrier: str | None = None
    operating_number: str | None = None

    @property
    def flight_number(self) -> str:
        return f"{self.marketing_carrier}{self.marketing_number}"

    @property
    def operated(self) -> str | None:
        return operated_note(self.operating_carrier, self.operating_number)

    def as_form(self) -> dict[str, str]:
        """The add form's fields, so a choice here is a filled-in form there."""
        posted = {
            "marketing_carrier": self.marketing_carrier,
            "marketing_number": self.marketing_number,
            "origin_iata": self.origin_iata,
            "dest_iata": self.dest_iata,
            "departure_local": _field(self.departure_local),
            "arrival_local": _field(self.arrival_local),
            "operating_carrier": self.operating_carrier or "",
            "operating_number": self.operating_number or "",
        }
        return {name: value for name, value in posted.items() if value}


def parse_flight_number(typed: str) -> tuple[str, str] | None:
    """`AC 871` as `("AC", "871")`, or nothing when it is not a flight number."""
    match = _TYPED.match(typed.strip().upper())
    if match is None:
        return None
    carrier, number = match.groups()
    # Every airline code carries a letter, so a carrier of nothing but digits is the
    # front of a number typed without one.
    if not any(character.isalpha() for character in carrier):
        return None
    return carrier, number.lstrip("0") or number


def in_range(day: date, today: date) -> bool:
    return today - BACK <= day <= today + AHEAD


async def find_flights(
    session: AsyncSession,
    carrier: str,
    number: str,
    day: date,
    client: AeroAPIClient | None = None,
    *,
    today: date | None = None,
) -> list[Candidate]:
    """Every leg published under that flight number, leaving that day at its origin.

    Usually exactly one. A number flown twice a day, or sold as a codeshare on two
    different legs, is why this answers with a list rather than a flight. `today` is the
    clock, and a parameter only so the window can be proven against a fixed date.
    """
    if not in_range(day, today or datetime.now(UTC).date()):
        raise OutOfRange(day)
    client = client or shared_client()
    payload = await client.schedules(
        day - EDGE, day + 2 * EDGE, airline=carrier, flight_number=number
    )

    found: dict[tuple[str, str, datetime], Candidate] = {}
    for row in payload.get("scheduled") or []:
        if not isinstance(row, dict):
            continue
        candidate = await _candidate(session, row, carrier, number, day)
        if candidate is None:
            continue
        # One leg can be published more than once - as the operator's flight and as the
        # codeshare sold on it - and both spellings describe the same aeroplane.
        found.setdefault(
            (candidate.origin_iata, candidate.dest_iata, candidate.departure_local), candidate
        )
    return sorted(found.values(), key=lambda candidate: candidate.departure_local)


async def _candidate(
    session: AsyncSession, row: dict[str, Any], carrier: str, number: str, day: date
) -> Candidate | None:
    """One schedule row as a candidate, or None when it is not one we can offer.

    A row we cannot place - no departure, an airport with no IATA code or none we know a
    timezone for - is dropped rather than raised on: the rest of the answer is still
    worth showing, and a flight can always be typed in by hand.
    """
    departure_utc = parse_instant(row.get("scheduled_out"))
    origin = _code(row.get("origin_iata"))
    dest = _code(row.get("destination_iata"))
    if departure_utc is None or origin is None or dest is None:
        return None

    try:
        origin_tz = await airport_tz(session, origin)
        dest_tz = await airport_tz(session, dest)
    except UnknownAirport as exc:
        log.info("skipping a %s%s schedule row: %s", carrier, number, exc)
        return None

    departure_local = to_local(departure_utc, origin_tz)
    if departure_local.date() != day:
        return None

    arrival_utc = parse_instant(row.get("scheduled_in"))
    typed = (carrier, number)
    published = split_ident(row.get("ident_iata"))
    published_icao = split_ident(row.get("ident_icao")) or split_ident(row.get("ident"))
    operator = split_ident(row.get("actual_ident_iata")) or split_ident(
        row.get("actual_ident_icao")
    )
    if typed in (published, published_icao):
        # Published under the number typed, so the row's IATA spelling is the ticket's
        # even when the ICAO one was typed.
        marketing = published or typed
    else:
        # A codeshare is answered with the operator's own row too. The number booked is
        # the one typed, whichever row came back, and the row's own number is who flies
        # it unless the row says otherwise.
        marketing = typed
        operator = operator or published or published_icao
    operating = operator if operator and operator != marketing else None
    return Candidate(
        marketing_carrier=marketing[0],
        marketing_number=marketing[1],
        origin_iata=origin,
        dest_iata=dest,
        departure_local=departure_local.replace(tzinfo=None),
        arrival_local=(
            to_local(arrival_utc, dest_tz).replace(tzinfo=None) if arrival_utc else None
        ),
        operating_carrier=operating[0] if operating else None,
        operating_number=operating[1] if operating else None,
    )


def _code(value: Any) -> str | None:
    """An airport's IATA code, or None - a booking has nowhere to put anything else."""
    code = str(value or "").strip().upper()
    return code if len(code) == 3 else None


def _field(local: datetime | None) -> str:
    """A wall-clock time as a `datetime-local` input reads it."""
    return "" if local is None else local.strftime("%Y-%m-%dT%H:%M")
