"""The phone widget's only endpoint: every display decision, already made.

The Scriptable script on the phone is deliberately stupid. It draws strings, colours
one of them by a tone it is told, and fetches one picture from an address it is handed;
it does not know what a diversion is or which gate belongs to which end of the flight.
The status pill, the milestone and the rule for when a flight has had its day are the
web UI's own, read from the same functions, so the lock screen and the board never
disagree. Everything that could be got wrong is got wrong here, once, where it is
covered by tests.

Two rules hold the contract together. Every instant is ISO-8601 UTC with a `Z`, because
the phone measures it against its own clock and a missing zone silently shifts the
figure. And the payload is a pydantic model, so a field that drifts breaks a test rather
than a lock screen.
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
from pydantic import BaseModel, PlainSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from . import bookings as booking_repo
from . import cadence, prefs, views
from .aeroapi import budget_status
from .airports import get_airport
from .config import Settings, get_settings
from .db import get_session
from .models import KV, Airport, Booking, BookingStatus, FlightSnapshot
from .phase import (
    AIRBORNE,
    DAY_OF,
    DIVERTED,
    LANDED,
    TAXIING,
    UPCOMING,
    Phase,
    airborne_window,
    compute_phase,
    departure_estimate,
    progress_estimate,
)
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

# Which flight the widget opens out into its card, when more than one could have it.
# An aircraft still moving, taxiing in included, comes first: nothing on the screen
# matters more than where it is. Then one about to leave, inside the poller's close
# window, which is as near as the server itself calls a departure imminent. A flight
# already parked comes last, because its card has nothing left to say but the belt: on a
# layover the leg just flown hands the screen to the leg about to be, the moment it is at
# the gate and not before, and keeps it while the next leg is still hours off. Ties go to
# the board's order.
UNDER_WAY: Final = 0
LEAVING_SOON: Final = 1
PARKED: Final = 2

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


UtcInstant = Annotated[datetime, PlainSerializer(_iso_z, return_type=str)]


class WidgetEnd(BaseModel):
    """One end of the card: the airport, the clock read there, and where in the building."""

    iata: str
    # The time it now leaves or arrives, in the tone the card draws it: stop once it
    # slipped later than booked, ok when it came forward, none while it is what was
    # booked. The day is the one that time falls on at this airport.
    time: str
    zone: str
    day: str | None
    tone: str | None
    terminal: str | None
    gate: str | None


class WidgetCard(BaseModel):
    """The flight's card opened out, for the one flight that is the widget's whole screen
    while it is under way. Every string is the card's own."""

    origin: WidgetEnd
    destination: WidgetEnd
    # The airport a diverted flight was booked for, beside the one it is now bound for.
    booked_destination: str | None
    # The aircraft's place on the rule between the two. Wheels-up and the landing
    # estimate let the phone move it between reloads by its own clock, the way the page
    # moves it between loads; without them the figure stands where the feed last put it.
    # Before wheels-up the rule says how long the hop is instead.
    progress: int | None
    airborne_off: UtcInstant | None
    airborne_on: UtcInstant | None
    block_time: str | None


class WidgetFlight(BaseModel):
    detail_url: str
    # For the server's own refresh cadence. The script never reads it: what it draws is
    # the pill and the milestone, which are words already chosen.
    phase: Phase
    logo_url: str
    number: str
    route: str
    status_label: str
    status_tone: str
    # The one line of the card that matters in this phase: the day it leaves while that
    # is still days off, then the gate and seat on the day, then nothing.
    detail: str | None
    milestone_label: str | None
    # A milestone is either an instant the phone counts to, or a figure handed over
    # ready-made: the belt, once there is nothing left to count to.
    milestone_to: UtcInstant | None
    milestone_text: str | None
    # What the label becomes once `milestone_to` has gone by, so the phone can turn
    # "Lands in" into "Due to land" between reloads the way the page does.
    milestone_due: str | None
    # The card, on at most one flight: the one the widget gives its whole screen to.
    card: WidgetCard | None


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
    ordered: list[tuple[datetime, FlightRow, WidgetFlight]] = []
    observed: list[datetime] = []
    for booking, snapshot in rows:
        flight = _flight(
            booking,
            snapshot,
            settings=settings,
            now=now,
            base_url=base_url,
            origin_tz=_zone(known, booking.origin_iata),
        )
        ordered.append((departure_estimate(booking, snapshot), (booking, snapshot), flight))
        if flight.phase in PHASES_IMMINENT and snapshot is not None and snapshot.observed_at:
            observed.append(snapshot.observed_at)
    # The board's order: by the time each is now leaving, landed or not.
    ordered.sort(key=lambda row: row[0])
    shown = ordered[:MAX_FLIGHTS]
    flights = [flight for _, _, flight in shown]

    featured = _featured([(row, flight.phase) for _, row, flight in shown], now)
    if featured is not None:
        booking, snapshot = shown[featured][1]
        card = _card(booking, snapshot, phase=flights[featured].phase, now=now, airports=known)
        flights[featured] = flights[featured].model_copy(update={"card": card})

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
    settings: Settings,
    now: datetime,
    base_url: str,
    origin_tz: str,
) -> WidgetFlight:
    phase = compute_phase(booking, snapshot, now)
    pill = views.status(phase, booking, snapshot, now=now, origin_tz=origin_tz)
    parked = views.at_the_gate(phase, booking, snapshot, now)

    label: str | None = None
    due: str | None = None
    text: str | None = None
    target: datetime | None = None
    if parked:
        # The card's footer once the aircraft is parked: the belt, or the dash that
        # says nobody has named one yet.
        label, text = "Baggage claim", views.dash(snapshot.baggage_claim if snapshot else None)
    elif views.watched(phase):
        next_up = views.milestone(phase, booking, snapshot, now=now)
        if next_up is not None:
            label = views.milestone_label(next_up, now)
            due = views.DUE.get(next_up.label, next_up.label)
            target = next_up.target

    return WidgetFlight(
        detail_url=f"{base_url}/f/{booking.id}",
        phase=phase,
        logo_url=views.logo_url(booking.marketing_carrier),
        number=f"{booking.marketing_carrier}{booking.marketing_number}",
        route=f"{booking.origin_iata} → {views.destination_iata(booking, snapshot)}",
        status_label=pill.label,
        status_tone=pill.tone,
        detail=_detail(phase, booking, snapshot, origin_tz=origin_tz),
        milestone_label=label,
        milestone_to=target,
        milestone_text=text,
        milestone_due=due,
        card=None,
    )


def _zone(airports: Mapping[str, Airport | None], iata: str) -> str:
    airport = airports.get(iata)
    return airport.tz if airport else FALLBACK_TZ


def _featured(shown: Sequence[tuple[FlightRow, Phase]], now: datetime) -> int | None:
    """Which of the flights on the widget gets the card, by the order set out above."""
    ranked = [
        (rank, index)
        for index, ((booking, snapshot), phase) in enumerate(shown)
        if (rank := _rank(phase, booking, snapshot, now)) is not None
    ]
    return min(ranked)[1] if ranked else None


def _rank(
    phase: Phase, booking: Booking, snapshot: FlightSnapshot | None, now: datetime
) -> int | None:
    if phase in (TAXIING, AIRBORNE, DIVERTED):
        # A booking the poller closed while the feed still said airborne was lost, not
        # flown: its card would show an aircraft pinned mid-route for good.
        return None if views.flown(booking) else UNDER_WAY
    if phase == LANDED:
        return PARKED if views.at_the_gate(phase, booking, snapshot, now) else UNDER_WAY
    if phase == DAY_OF and departure_estimate(booking, snapshot) - now <= cadence.FINAL_HORIZON:
        return LEAVING_SOON
    return None


def _card(
    booking: Booking,
    snapshot: FlightSnapshot | None,
    *,
    phase: Phase,
    now: datetime,
    airports: Mapping[str, Airport | None],
) -> WidgetCard:
    """The card's facts, read the way the page reads them so the two cannot differ."""
    view = views.FlightView(
        booking=booking,
        snapshot=snapshot,
        origin=airports.get(booking.origin_iata),
        dest=airports.get(booking.dest_iata),
        diversion=airports.get(views.destination_iata(booking, snapshot)),
    )
    window = airborne_window(booking, snapshot, now)
    if phase == LANDED:
        # All the way there whatever the feed last said: its figure stops at the last
        # poll, which may have been well short of the runway.
        progress: int | None = 100
    else:
        progress = progress_estimate(booking, snapshot, now)
    block_time: str | None = None
    if phase == DAY_OF and view.arrival is not None and view.arrival > view.departure:
        block_time = views.duration(view.arrival - view.departure)
    return WidgetCard(
        origin=_end(
            booking.origin_iata,
            view.origin,
            view.departs,
            terminal=snapshot.terminal_origin if snapshot else None,
            gate=snapshot.gate_origin if snapshot else None,
        ),
        destination=_end(
            view.destination_iata,
            view.destination,
            view.arrives,
            terminal=snapshot.terminal_destination if snapshot else None,
            gate=snapshot.gate_destination if snapshot else None,
        ),
        booked_destination=booking.dest_iata if view.diverted_to else None,
        progress=progress,
        airborne_off=window[0] if window else None,
        airborne_on=window[1] if window else None,
        block_time=block_time,
    )


def _end(
    iata: str,
    airport: Airport | None,
    line: views.Timeline,
    *,
    terminal: str | None,
    gate: str | None,
) -> WidgetEnd:
    tz = airport.tz if airport else FALLBACK_TZ
    return WidgetEnd(
        iata=iata,
        time=views.clock(line.best, tz),
        zone=views.zone(line.best, tz),
        day=views.day(line.best, tz) if line.best is not None else None,
        tone=("stop" if line.late else "ok") if line.moved else None,
        terminal=terminal,
        gate=gate,
    )


def _detail(
    phase: Phase,
    booking: Booking,
    snapshot: FlightSnapshot | None,
    *,
    origin_tz: str,
) -> str | None:
    """What the card says that matters most right now, on one line.

    Days out, the day it leaves. On the day, the gate and the seat to find; the time it
    leaves is what the milestone is counting to, so the line does not repeat it. Once
    it has pushed back there is nothing left to find: the gate it left is behind it,
    and the other end is the milestone's to count to and the belt's to name.
    """
    if phase == UPCOMING:
        return views.at(departure_estimate(booking, snapshot), origin_tz, with_date=True)
    if phase != DAY_OF:
        return None
    parts: list[str] = []
    if snapshot and snapshot.gate_origin:
        parts.append(f"Gate {snapshot.gate_origin}")
    if booking.seat:
        parts.append(f"Seat {booking.seat}")
    return " · ".join(parts) if parts else None


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
