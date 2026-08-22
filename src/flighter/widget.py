"""The phone widget's only endpoint: every display decision, already made.

The Scriptable script on the phone is deliberately stupid. It draws strings, colours
one of them by a tone it is told, and fetches one picture from an address it is handed;
it does not know what a diversion is or which gate belongs to which end of the flight.
The status, the time on the right and the rule for when a flight has had its day are
the web UI's own, read from the same functions, so the lock screen and the board never
disagree. Everything that could be got wrong is got wrong here, once, where it is
covered by tests.

Two rules hold the contract together. Nothing in the payload is an instant: iOS reloads
a widget about every quarter of an hour, so anything measured against the phone's clock
is a quarter of an hour wrong before it is drawn again, and every time here is a clock
read at its airport instead, which is right until the estimate itself moves. And the
payload is a pydantic model, so a field that drifts breaks a test rather than a lock
screen.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from . import bookings as booking_repo
from . import prefs, views
from .aeroapi import budget_status
from .airports import get_airport
from .config import Settings, get_settings
from .db import get_session
from .models import KV, Airport, Booking, BookingStatus, FlightSnapshot
from .phase import AIRBORNE, DAY_OF, DIVERTED, TAXIING, Phase, compute_phase, departure_estimate
from .timezones import FALLBACK_TZ

log = logging.getLogger(__name__)

router = APIRouter()

# A lock screen has room for one flight and a home screen for three. Anything past that
# is a trip itinerary, which is what the web UI is for.
MAX_FLIGHTS: Final = 3

REFRESH_IDLE_SECONDS: Final = 900
REFRESH_ACTIVE_SECONDS: Final = 600

# The poller runs a close flight every 10 minutes and a same-day one every 30, so a
# snapshot this old means polling has stopped rather than that nothing has changed.
POLL_STALE_AFTER: Final = timedelta(minutes=45)

PHASES_IMMINENT: Final = frozenset({DAY_OF, TAXIING, AIRBORNE, DIVERTED})

# What the line calls the rung it names a time for. The board counts down to it; the
# widget states it, so "Lands in" reads "Lands" here. A departure needs no word at all:
# it is the first thing on the line and the time it leaves is what a row is about.
ARRIVAL_WORDS: Final = {"Lands in": "Lands", "At the gate in": "At the gate"}

# The board names the day in the pill for a flight the feed has not picked up yet. Here
# the day is on the line with the time, so the pill has nothing to add but that it is
# booked.
DAY_WORDS: Final = frozenset({"Today", "Tomorrow"})

# The script is served from here rather than fetched from a repository, so the phone
# always runs the version that matches the server answering it.
SCRIPT_FILE: Final = Path(__file__).parent / "static" / "flights-widget.js"
# Fixed because the Connect link runs the script by name. The bundle installs it under
# this name, so the only way to break the link is to rename the script by hand.
SCRIPT_NAME: Final = "Flighter"
SCRIPT_ICON: Final = {"color": "deep-blue", "glyph": "plane-departure"}

LAST_SEEN_KEY: Final = "widget_last_seen"


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class WidgetFlight(BaseModel):
    detail_url: str
    # For the server's own refresh cadence. The script never reads it: what it draws is
    # the status and the time, which are words already chosen.
    phase: Phase
    logo_url: str
    number: str
    route: str
    status_label: str
    status_tone: str
    # The one line under the pill: the next time that matters, read at the airport it
    # happens at, with what there is to find at that airport after it. Everything the
    # row says beyond the flight itself is on this line.
    detail: str | None


class WidgetPayload(BaseModel):
    flights: list[WidgetFlight]
    refresh_seconds: int
    degraded: bool
    degraded_reason: str | None


FlightRow = tuple[Booking, FlightSnapshot | None]


@router.get("/api/widget")
async def read_widget(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
) -> WidgetPayload:
    authorize(settings, authorization, token)
    now = datetime.now(UTC)
    await mark_seen(session, now)
    rows = await load_flight_rows(session, now)
    return build_payload(
        rows,
        settings=settings,
        now=now,
        airports=await load_airports(session, rows),
        # The phone reached this address to ask, so the links it is handed back work
        # from wherever it is, saved address or not.
        base_url=prefs.public_base_url(str(request.base_url).rstrip("/")),
        degraded_reason=await read_degraded(session),
    )


@router.get(f"/widget/{SCRIPT_NAME}.scriptable")
async def read_script_bundle() -> Response:
    """The script as a Scriptable document, which the app imports in one tap.

    Nothing secret is in it. The server address and the token reach the phone through
    the Connect link and live in its Keychain, so this file is the same for everyone and
    the script can replace itself with a newer copy without carrying anything over.
    """
    bundle = {
        "name": SCRIPT_NAME,
        "icon": SCRIPT_ICON,
        "script": script_body(),
        "always_run_in_app": False,
        "share_sheet_inputs": [],
    }
    return Response(
        json.dumps(bundle),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{SCRIPT_NAME}.scriptable"'},
    )


def script_source() -> str:
    return SCRIPT_FILE.read_text()


def script_body() -> str:
    """The script without the header Scriptable maintains itself.

    The first comment block is the app's record of the icon, which a bundle carries as a
    field of its own; importing it twice leaves the app with two headers.
    """
    header, _, body = script_source().partition("\n\n")
    return body if header.startswith("// Variables used by Scriptable") else header


def connect_url(settings: Settings, base_url: str) -> str:
    """What the Connect button on the settings page opens.

    Scriptable runs the named script and hands it the query as `args.queryParameters`,
    so the phone learns the address and the token without anybody copying either.
    """
    query = urlencode({"api": base_url, "token": settings.widget_token})
    return f"scriptable:///run/{SCRIPT_NAME}?{query}"


def authorize(settings: Settings, authorization: str | None, token: str | None) -> None:
    """Bearer header, or `?token=` for pasting the URL into a browser to debug.

    An unset token refuses everything. The alternative reading, that a blank token means
    no authentication, publishes the user's travel plans the moment the token is cleared,
    so the failure is loud instead.
    """
    expected = settings.widget_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="widget_token is not configured",
        )
    presented = token or ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[len("bearer ") :].strip()
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid widget token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def mark_seen(session: AsyncSession, now: datetime) -> None:
    """Stamp the moment a phone last got its data, for the settings page to show.

    Short of standing next to the phone, this is the only evidence that the widget is
    talking to this server: a token that is wrong never gets here, so the stamp stops.
    """
    await session.merge(KV(key=LAST_SEEN_KEY, value={"at": _iso_z(now)}))


async def last_seen(session: AsyncSession) -> datetime | None:
    row = await session.get(KV, LAST_SEEN_KEY)
    if row is None:
        return None
    return datetime.fromisoformat(row.value["at"]).astimezone(UTC)


async def load_flight_rows(session: AsyncSession, now: datetime) -> list[FlightRow]:
    """Every flight the board still has on it at `now`, each with its newest snapshot.

    The same two lists the board reads, cut by the same rule: a flight stays until
    `off_board_at`, whatever its status says, so one the poller has already closed is
    still here while someone is walking off it, and one the feed lost goes when the
    board files it rather than hours later.
    """
    bookings = await booking_repo.list_bookings(session, statuses=[BookingStatus.ACTIVE])
    bookings += await booking_repo.list_recently_flown(session, MAX_FLIGHTS)
    if not bookings:
        return []

    latest = await booking_repo.latest_snapshots(session, [booking.id for booking in bookings])
    rows = [(booking, latest.get(booking.id)) for booking in bookings]
    return [(booking, snap) for booking, snap in rows if views.off_board_at(booking, snap) >= now]


async def load_airports(
    session: AsyncSession, rows: Sequence[FlightRow]
) -> dict[str, Airport | None]:
    """Both ends of every flight, and where a diverted one is bound, for the zone each
    clock is read in."""
    airports: dict[str, Airport | None] = {}
    for booking, snapshot in rows:
        bound_for = views.destination_iata(booking, snapshot)
        for iata in (booking.origin_iata, booking.dest_iata, bound_for):
            if iata not in airports:
                airports[iata] = await get_airport(session, iata)
    return airports


async def read_degraded(session: AsyncSession) -> str | None:
    """Why the numbers might be wrong, in words the widget can print verbatim.

    The breaker latch lives in KV and `budget_status` owns reading it, including the
    month scoping that unlatches it on the 1st. An absent latch is the healthy case.
    """
    budget = await budget_status(session)
    if budget.tripped:
        return f"AeroAPI budget reached (${budget.spend_usd} of ${budget.cap_usd})"
    return None


def build_payload(
    rows: Sequence[FlightRow],
    *,
    settings: Settings,
    now: datetime,
    base_url: str,
    airports: Mapping[str, Airport | None] | None = None,
    degraded_reason: str | None = None,
) -> WidgetPayload:
    known = airports or {}
    ordered: list[tuple[datetime, WidgetFlight]] = []
    observed: list[datetime] = []
    for booking, snapshot in rows:
        flight = _flight(booking, snapshot, now=now, base_url=base_url, airports=known)
        ordered.append((departure_estimate(booking, snapshot), flight))
        if flight.phase in PHASES_IMMINENT and snapshot is not None and snapshot.observed_at:
            observed.append(snapshot.observed_at)
    # The board's order: by the time each is now leaving, landed or not.
    ordered.sort(key=lambda row: row[0])
    flights = [flight for _, flight in ordered[:MAX_FLIGHTS]]

    reason = degraded_reason or _stale_reason(min(observed, default=None), now)
    return WidgetPayload(
        flights=flights,
        refresh_seconds=_refresh_seconds(flights),
        degraded=reason is not None,
        degraded_reason=reason,
    )


def _flight(
    booking: Booking,
    snapshot: FlightSnapshot | None,
    *,
    now: datetime,
    base_url: str,
    airports: Mapping[str, Airport | None],
) -> WidgetFlight:
    phase = compute_phase(booking, snapshot, now)
    origin_tz = _zone(airports, booking.origin_iata)
    pill = _status(phase, booking, snapshot, now=now, origin_tz=origin_tz)
    return WidgetFlight(
        detail_url=f"{base_url}/f/{booking.id}",
        phase=phase,
        logo_url=views.logo_url(booking.marketing_carrier),
        number=f"{booking.marketing_carrier}{booking.marketing_number}",
        route=f"{booking.origin_iata} → {views.destination_iata(booking, snapshot)}",
        status_label=pill.label,
        status_tone=pill.tone,
        detail=_detail(
            phase,
            booking,
            snapshot,
            now=now,
            origin_tz=origin_tz,
            destination_tz=_zone(airports, views.destination_iata(booking, snapshot)),
        ),
    )


def _zone(airports: Mapping[str, Airport | None], iata: str) -> str:
    airport = airports.get(iata)
    return airport.tz if airport else FALLBACK_TZ


def _status(
    phase: Phase,
    booking: Booking,
    snapshot: FlightSnapshot | None,
    *,
    now: datetime,
    origin_tz: str,
) -> views.Status:
    """The board's word, bar the two that a widget drawn a quarter of an hour apart
    cannot carry.

    Taxiing is ten minutes between pushback and wheels up, so it is as likely as not to
    be over by the time anyone reads it; Departed is true from pushback to the gate at
    the other end. And the board's Today and Tomorrow say when a flight the feed has not
    picked up leaves, which here is the word under its time.
    """
    pill = views.status(phase, booking, snapshot, now=now, origin_tz=origin_tz)
    if phase == TAXIING:
        return views.Status("Departed", "live")
    if pill.label in DAY_WORDS:
        return views.Status("Scheduled", "quiet")
    return pill


def _detail(
    phase: Phase,
    booking: Booking,
    snapshot: FlightSnapshot | None,
    *,
    now: datetime,
    origin_tz: str,
    destination_tz: str,
) -> str | None:
    """The line under the pill: the next time that matters, and what to find when it comes.

    The rung is the board's own, so the two never name different things. The departure
    is read at the origin and everything after it at the destination, the way a ticket
    and an arrivals board each read their own clock, and the time leads the line so that
    a row too narrow for all of it loses the seat rather than the flight.

    Parked there is no time left to give and the belt is the only thing anyone wants,
    and a flight the feed lost, or called off, has nothing to say the pill has not said.
    """
    if views.at_the_gate(phase, booking, snapshot, now):
        belt = snapshot.baggage_claim if snapshot else None
        return f"Baggage claim {belt}" if belt else None
    next_up = views.milestone(phase, booking, snapshot, now=now)
    if next_up is None:
        return None
    if next_up.label in ARRIVAL_WORDS:
        word = ARRIVAL_WORDS[next_up.label]
        return f"{word} {_when(next_up.target, now, destination_tz)}"

    parts = [_when(next_up.target, now, origin_tz)]
    if phase == DAY_OF:
        # The gate and the seat are worth the width only once there is a chance they
        # are filled in and a person is on their way to use them.
        if snapshot and snapshot.gate_origin:
            parts.append(f"Gate {snapshot.gate_origin}")
        if booking.seat:
            parts.append(f"Seat {booking.seat}")
    return " · ".join(parts)


def _when(instant: datetime, now: datetime, tz: str) -> str:
    """A time at an airport, with the day in front of it when it is not today's.

    `18:40 EDT` on its own reads as today's, so a flight leaving tomorrow morning would
    look hours overdue all evening. The zone is never dropped: it is the difference
    between a time and a missed flight, which is why every time in the app carries it.
    """
    day = views.day_word(instant, now, tz)
    if day == "Today":
        return views.at(instant, tz)
    if day is not None:
        return f"{day} {views.at(instant, tz)}"
    return views.at(instant, tz, with_date=True)


def _refresh_seconds(flights: Sequence[WidgetFlight]) -> int:
    """Mirror the server's own cadence; polling faster than it updates buys nothing."""
    if any(flight.phase in PHASES_IMMINENT for flight in flights):
        return REFRESH_ACTIVE_SECONDS
    return REFRESH_IDLE_SECONDS


def _stale_reason(observed: datetime | None, now: datetime) -> str | None:
    """Only ever judged against a flight that is close enough to be polled often.

    A flight that has never been polled is not evidence of anything: it may have been
    added a minute ago.
    """
    if observed is None:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    age = now - observed
    if age <= POLL_STALE_AFTER:
        return None
    return f"No status update in {int(age.total_seconds() // 60)} min"
