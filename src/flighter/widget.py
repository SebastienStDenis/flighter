"""The phone widget's only endpoint: every display decision, already made.

The Scriptable script on the phone is deliberately stupid. It draws strings, colours
one of them by a tone it is told, and fetches one picture from an address it is handed;
it does not know what a diversion is or which gate belongs to which end of the flight.
The status, the rung a time is named for and the rule for when a flight has had its day
are the web UI's own, read from the same functions, so the lock screen and the board
never disagree. Everything that could be got wrong is got wrong here, once, where it is
covered by tests.

Two rules hold the contract together. Every time in the payload is already a clock face,
read on the phone's own clock - it sends the zone it is in - and never carries a second
reading of the same instant beside it: a row is looked at for a second and a half, and a
line that states two times states neither. The single exception is what the flight is
counting to, which goes over as the instant itself: iOS counts a date down on its own,
without a reload, so the one figure that would be a quarter of an hour stale is the one
figure the phone draws for itself. And the payload is a pydantic model, so a field that
drifts breaks a test rather than a lock screen.
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
    # The one line under the pill: where in the building to be, and what seat to sit in
    # once there is a building to be in. Days out there is neither, and it is the time
    # the flight leaves instead.
    detail: str | None
    # The board's footer, which is the right-hand side of a row here: what the flight is
    # counting to, in the board's own words, and the instant it counts to. The phone
    # draws the figure itself and it ticks between reloads, which is why this is the one
    # instant that goes over rather than a figure that would be stale on arrival.
    milestone_label: str | None
    milestone_at: str | None


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
    tz: Annotated[str | None, Query()] = None,
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
        # Where the phone is, so the times it draws are the ones on its own clock. An
        # unknown name resolves to UTC rather than failing, the way every zone here does.
        viewer_tz=tz,
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
    include_friends = prefs.current().show_friend_flights_in_widget
    bookings = await booking_repo.list_bookings(session, statuses=[BookingStatus.ACTIVE])
    if not include_friends:
        bookings = [booking for booking in bookings if booking.friend_name is None]
    bookings += await booking_repo.list_recently_flown(
        session, MAX_FLIGHTS, include_friends=include_friends
    )
    if not bookings:
        return []

    latest = await booking_repo.latest_snapshots(session, [booking.id for booking in bookings])
    rows = [(booking, latest.get(booking.id)) for booking in bookings]
    return [(booking, snap) for booking, snap in rows if views.off_board_at(booking, snap) >= now]


async def load_airports(
    session: AsyncSession, rows: Sequence[FlightRow]
) -> dict[str, Airport | None]:
    """The airport every flight leaves from, which is the one clock still read here.

    The day a flight leaves is the day at the airport it leaves from, and it is the
    only time the payload states. What happens at the other end is a countdown, and a
    countdown has no zone to be read in.
    """
    airports: dict[str, Airport | None] = {}
    for booking, _ in rows:
        if booking.origin_iata not in airports:
            airports[booking.origin_iata] = await get_airport(session, booking.origin_iata)
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
    viewer_tz: str | None = None,
    degraded_reason: str | None = None,
) -> WidgetPayload:
    known = airports or {}
    ordered: list[tuple[datetime, WidgetFlight]] = []
    observed: list[datetime] = []
    for booking, snapshot in rows:
        flight = _flight(
            booking,
            snapshot,
            now=now,
            base_url=base_url,
            airports=known,
            viewer_tz=viewer_tz,
        )
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
    viewer_tz: str | None,
) -> WidgetFlight:
    phase = compute_phase(booking, snapshot, now)
    origin_tz = _zone(airports, booking.origin_iata)
    pill = _status(phase, booking, snapshot, now=now, origin_tz=origin_tz)
    counting_to = _milestone(phase, booking, snapshot, now=now)
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
            viewer_tz=viewer_tz,
        ),
        milestone_label=counting_to[0] if counting_to else None,
        milestone_at=_iso_z(counting_to[1]) if counting_to else None,
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


def _milestone(
    phase: Phase,
    booking: Booking,
    snapshot: FlightSnapshot | None,
    *,
    now: datetime,
) -> tuple[str, datetime] | None:
    """What the flight is counting to, and when, in the board's own words.

    The board's footer exactly: the same rung, named the same way - "Departs in" while
    it is ahead, "Due to depart" once its time has gone by with no word that it happened
    - and dropped in the same two places the board drops it. A flight days out is not
    counted in hours by anyone, and a parked one has the belt to give instead.
    """
    if not views.watched(phase) or views.at_the_gate(phase, booking, snapshot, now):
        return None
    next_up = views.milestone(phase, booking, snapshot, now=now)
    if next_up is None:
        return None
    return views.milestone_label(next_up, now), next_up.target


def _detail(
    phase: Phase,
    booking: Booking,
    snapshot: FlightSnapshot | None,
    *,
    now: datetime,
    origin_tz: str,
    viewer_tz: str | None,
) -> str | None:
    """The line under the pill: where to be, once being somewhere is the question.

    What the flight is counting to is on the right of the row and counts itself down, so
    this line never states a time twice over: the day a flight leaves is worth a line
    only while it is far enough off that nothing is counting to it. Inside its day the
    terminal, the gate and the seat are what somebody is walking to, drawn as the boxes
    the card draws and dashed where the airport has not said yet. Off the ground all of
    that is behind them and the seat is the last of it worth carrying.

    Parked there is nothing left to find but the belt, and a flight the feed lost, or
    called off, has nothing to say the pill has not said.
    """
    if views.at_the_gate(phase, booking, snapshot, now):
        belt = snapshot.baggage_claim if snapshot else None
        return f"Baggage claim {belt}" if belt else None
    if not views.watched(phase):
        if views.milestone(phase, booking, snapshot, now=now) is None:
            return None
        return _when(departure_estimate(booking, snapshot), now, origin_tz, viewer_tz)
    if phase == DAY_OF:
        parts = [
            f"T {views.dash(snapshot.terminal_origin if snapshot else None)}",
            f"G {views.dash(snapshot.gate_origin if snapshot else None)}",
        ]
        if booking.seat:
            parts.append(f"S {booking.seat}")
        return " · ".join(parts)
    return f"S {booking.seat}" if booking.seat else None


def _when(instant: datetime, now: datetime, airport_tz: str, viewer_tz: str | None) -> str:
    """The time on the reader's own clock, and on no other.

    A time at an airport four zones away is arithmetic, not information: the phone is
    what someone is holding, so its clock is the one the line is read on, and the day in
    front of it is that clock's day. The airport's own reading of the same instant is
    not printed beside it - two times on one line is a line that has to be worked out
    rather than read - and the zone is not named either, because the clock it is on is
    the clock in the same hand.

    A phone that did not say where it is gets the airport's clock instead, and that one
    keeps its zone: it is the one case where the time is not on the reader's own.
    """
    here = viewer_tz or airport_tz
    stated = views.clock if viewer_tz else views.at
    day = views.day_word(instant, now, here)
    if day == "Today":
        return stated(instant, here)
    if day is not None:
        return f"{day} {stated(instant, here)}"
    return stated(instant, here, with_date=True)


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
