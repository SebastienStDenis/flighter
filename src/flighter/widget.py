"""The phone widget's only endpoint: every display decision, already made.

The Scriptable script on the phone is deliberately stupid. It draws strings, colours
one of them by a tone it is told, and fetches one picture from an address it is handed;
it does not know what a diversion is or which gate belongs to which end of the flight.
The status, the rung a time is named for and the rule for when a flight has had its day
are the web UI's own, read from the same functions, so the lock screen and the board
never disagree. Everything that could be got wrong is got wrong here, once, where it is
covered by tests.

Two rules hold the contract together. Every time in the payload is already a clock face
and nothing on the widget moves between reloads: a figure counted from the clock at draw
time is a quarter of an hour wrong by the time anybody reads it, and a stated time never
is. And each of them is read on one clock only. What the flight is due to do next is on
the phone's own - it sends the zone it is in - and carries no zone with it, because the
clock it is on is the clock in the same hand; the day it leaves is on the clock at the
airport it leaves from and carries that airport's zone, because that one is not. The
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
from typing import Annotated, Final, NamedTuple
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
from .timezones import FALLBACK_TZ, to_local

log = logging.getLogger(__name__)

router = APIRouter()

# A lock screen has room for one flight and a home screen for three. Anything past that
# is a trip itinerary, which is what the web UI is for.
MAX_FLIGHTS: Final = 3

REFRESH_IDLE_SECONDS: Final = 900
REFRESH_ACTIVE_SECONDS: Final = 600
# iOS budgets reloads across every widget on the phone and ignores an eager request
# anyway, so this is the floor the script clamps to as well.
REFRESH_FLOOR_SECONDS: Final = 60

# The poller runs a close flight every 10 minutes and a same-day one every 30, so a
# snapshot this old means polling has stopped rather than that nothing has changed.
POLL_STALE_AFTER: Final = timedelta(minutes=45)

PHASES_IMMINENT: Final = frozenset({DAY_OF, TAXIING, AIRBORNE, DIVERTED})

# The script is served from here rather than fetched from a repository, so the phone
# always runs the version that matches the server answering it.
SCRIPT_FILE: Final = Path(__file__).parent / "static" / "flights-widget.js"
# Fixed because the Connect link runs the script by name. The bundle installs it under
# this name, so the only way to break the link is to rename the script by hand.
SCRIPT_NAME: Final = "Flighter"
SCRIPT_ICON: Final = {"color": "deep-blue", "glyph": "plane-departure"}

LAST_SEEN_KEY: Final = "widget_last_seen"

# Between the places on a line. A boarding pass sets them apart with rules and white
# space, neither of which a widget row has to spend, and three words run together read
# as one thing rather than three.
BETWEEN_PLACES: Final = " · "


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class WidgetFlight(BaseModel):
    detail_url: str
    # For the server's own refresh cadence. The script never reads it: what it draws is
    # the status and the times, which are words already chosen.
    phase: Phase
    # Whose flight it is, where it is not the reader's own: the initial the board draws
    # in a disc, and the hue it takes for that disc from the name.
    friend_initial: str | None
    friend_hue: int | None
    logo_url: str
    number: str
    route: str
    status_label: str
    status_tone: str
    # The line under the heading: the day it leaves while that is the whole story, then
    # where in the building to be, and once it is off the ground, where to be at the
    # other end.
    detail: str | None
    # The end of the row, under the pill: the rung the flight is next due to climb and
    # the time it is due, on the phone's own clock. Parked, no rung is left and the belt
    # takes the line instead.
    target_label: str | None
    target_value: str | None


class Target(NamedTuple):
    """The end of a row: the words, and the one figure that goes beside them.

    `at` is the instant behind a stated time. It is not sent - the phone draws the clock
    face and counts nothing - but it is the moment the words in front of it change, and
    so the moment the server asks for its next reload.
    """

    label: str
    value: str
    at: datetime | None = None


class Built(NamedTuple):
    """One drawn row, and the instant behind the time on it that the payload leaves out."""

    flight: WidgetFlight
    target_at: datetime | None


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
    ordered: list[tuple[datetime, Built]] = []
    observed: list[datetime] = []
    for booking, snapshot in rows:
        built = _flight(
            booking,
            snapshot,
            now=now,
            base_url=base_url,
            airports=known,
            viewer_tz=viewer_tz,
        )
        ordered.append((departure_estimate(booking, snapshot), built))
        if built.flight.phase in PHASES_IMMINENT and snapshot is not None and snapshot.observed_at:
            observed.append(snapshot.observed_at)
    # The board's order: by the time each is now leaving, landed or not.
    ordered.sort(key=lambda row: row[0])
    drawn = [built for _, built in ordered[:MAX_FLIGHTS]]

    reason = degraded_reason or _stale_reason(min(observed, default=None), now)
    return WidgetPayload(
        flights=[built.flight for built in drawn],
        refresh_seconds=_refresh_seconds(drawn, now),
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
) -> Built:
    phase = compute_phase(booking, snapshot, now)
    origin_tz = _zone(airports, booking.origin_iata)
    # The board's word for it, with nothing rephrased on the way out: a pill that
    # reads Departed on the phone and Taxiing on the page is two answers to one
    # question, and the reader has no way to tell which is the stale one.
    pill = views.status(phase, booking, snapshot, now=now, origin_tz=origin_tz)
    target = _target(phase, booking, snapshot, now=now, origin_tz=origin_tz, viewer_tz=viewer_tz)
    friend = booking.friend_name
    return Built(
        WidgetFlight(
            detail_url=f"{base_url}/f/{booking.id}",
            phase=phase,
            friend_initial=friend[0].upper() if friend else None,
            friend_hue=views.friend_hue(friend) if friend else None,
            logo_url=views.logo_url(booking.marketing_carrier),
            number=f"{booking.marketing_carrier}{booking.marketing_number}",
            route=f"{booking.origin_iata} → {views.destination_iata(booking, snapshot)}",
            status_label=pill.label,
            status_tone=pill.tone,
            detail=_detail(phase, booking, snapshot, now=now, origin_tz=origin_tz),
            target_label=target.label if target else None,
            target_value=target.value if target else None,
        ),
        target.at if target else None,
    )


def _zone(airports: Mapping[str, Airport | None], iata: str) -> str:
    airport = airports.get(iata)
    return airport.tz if airport else FALLBACK_TZ


def _target(
    phase: Phase,
    booking: Booking,
    snapshot: FlightSnapshot | None,
    *,
    now: datetime,
    origin_tz: str,
    viewer_tz: str | None,
) -> Target | None:
    """The end of the row: what the flight is next due to do, and when it is due.

    The board's own rung, named the board's own way - "Departs" while it is ahead, "Due
    to depart" once its time has gone by with no word that it happened - and dropped in
    the same two places the board drops it. The time it is due rather than the hours
    left to it: the hours would be a quarter of an hour stale by the time anybody read
    them, and a time that has passed is still the time it was due.

    Parked, there is no rung ahead of the flight and the belt takes the line, dashed
    until the airport says it, the way the card draws it: the words are the news either
    way, and a line that arrives late is a row that moves under the eye.
    """
    if views.at_the_gate(phase, booking, snapshot, now):
        belt = snapshot.baggage_claim if snapshot else None
        return Target("Baggage claim", views.dash(belt))
    next_up = views.milestone(phase, booking, snapshot, now=now)
    if next_up is None:
        return None
    return Target(
        views.milestone_time_label(next_up, now),
        _stated(next_up.target, now, origin_tz, viewer_tz),
        at=next_up.target,
    )


def _detail(
    phase: Phase,
    booking: Booking,
    snapshot: FlightSnapshot | None,
    *,
    now: datetime,
    origin_tz: str,
) -> str | None:
    """The line under the heading: the day it leaves, and then where to be.

    Days out there is nothing to walk to yet, and the only thing to say is when it goes.
    It is said on the clock at the airport it goes from, with the zone named: that clock
    is not the one in the reader's hand, and every other time on the widget is. The other
    end of the row states the same departure on the phone's own clock, so the two of them
    together answer "when, and when is that here".

    Inside its day the terminal, the gate and the seat are what somebody is walking to,
    in the order a boarding pass prints them and dashed where the airport has not said
    yet. Off the ground it is the same three read the other way about, because the seat
    is where they are now and the gate and the terminal are where they are going.

    Parked there is nothing left to find but the belt, which is the other end of the row,
    and a flight the poller has closed the book on has nothing left to walk to at all.
    """
    if views.flown(booking) or views.at_the_gate(phase, booking, snapshot, now):
        return None
    if not views.watched(phase):
        if views.milestone(phase, booking, snapshot, now=now) is None:
            return None
        return views.at(departure_estimate(booking, snapshot), origin_tz, with_date=True)
    if phase == DAY_OF:
        parts = [
            f"TERM {views.dash(snapshot.terminal_origin if snapshot else None)}",
            f"GATE {views.dash(snapshot.gate_origin if snapshot else None)}",
        ]
        if booking.seat:
            parts.append(f"SEAT {booking.seat}")
        return BETWEEN_PLACES.join(parts)
    parts = [f"SEAT {booking.seat}"] if booking.seat else []
    parts.append(f"GATE {views.dash(snapshot.gate_destination if snapshot else None)}")
    parts.append(f"TERM {views.dash(snapshot.terminal_destination if snapshot else None)}")
    return BETWEEN_PLACES.join(parts)


def _stated(instant: datetime, now: datetime, origin_tz: str, viewer_tz: str | None) -> str:
    """A time on the clock in the reader's hand, and on no other.

    The zone is not named with it. The clock it is read on is the one in the same hand,
    and the widget's own footer says so once for every time on it. The day is named when
    it is not today's, because a bare 04:50 read at ten in the evening is a time that
    looks like it has gone.

    A phone that did not say where it is gets the airport's clock instead. It is the one
    reading here that is not the reader's own, and there is nothing better to draw.
    """
    here = viewer_tz or origin_tz
    if views.day_word(instant, now, here) == "Today":
        return views.clock(instant, here)
    return to_local(instant, here).strftime("%a %H:%M")


def _refresh_seconds(drawn: Sequence[Built], now: datetime) -> int:
    """Mirror the server's own cadence, and never sleep through a rung falling due.

    Polling faster than the server updates buys nothing, so the cadence is the poller's.
    But a widget only redraws when iOS reloads it, and iOS reloads one about four times
    an hour: a row drawn as "Departs 18:40" goes on saying it after 18:40 has gone by,
    when what the reader needs to see is that the flight is due and has not left. Nothing
    on the phone can repair that - every glyph stays exactly where it was drawn - so the
    moment the wording changes is asked for as a reload, and the row is behind only for
    as long as iOS makes it wait.

    The floor is the same one the script clamps to. A rung a handful of seconds out is
    not worth a reload of its own; the one a minute later says the same thing.
    """
    cadence = (
        REFRESH_ACTIVE_SECONDS
        if any(built.flight.phase in PHASES_IMMINENT for built in drawn)
        else REFRESH_IDLE_SECONDS
    )
    ahead = [built.target_at for built in drawn if built.target_at and built.target_at > now]
    if not ahead:
        return cadence
    due_in = int((min(ahead) - now).total_seconds())
    return max(REFRESH_FLOOR_SECONDS, min(cadence, due_in))


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
