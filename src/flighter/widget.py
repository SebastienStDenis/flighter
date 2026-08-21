"""The phone widget's only endpoint: every display decision, already made.

The Scriptable script on the phone is deliberately stupid. It draws strings and colours
one of them by a tone it is told; it does not know what a diversion is or which gate
belongs to which end of the flight. The status pill and the milestone are the web UI's
own, read from the same functions, so the lock screen and the board never disagree.
Everything that could be got wrong is got wrong here, once, where it is covered by tests.

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
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import bookings as booking_repo
from . import prefs, views
from .aeroapi import budget_status
from .airports import get_airport
from .config import Settings, get_settings
from .db import get_session
from .models import KV, Booking, BookingStatus, FlightSnapshot
from .phase import (
    AIRBORNE,
    CANCELLED,
    DAY_OF,
    DIVERTED,
    LANDED,
    TAXIING,
    Phase,
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
# Wider than MAX_FLIGHTS because relevance order is not departure order: an airborne
# flight sorts ahead of one that departs sooner.
CANDIDATE_LIMIT: Final = 12
# How far past its ticketed arrival a flight is still fetched. A flight leaves the widget
# once it has landed rather than once it has departed - the whole point of the airborne
# row is the "Lands in" countdown - so this has to cover the delay as well as the stretch
# afterwards where it still says which carousel to walk to.
LATE_ARRIVAL_ALLOWANCE: Final = timedelta(hours=14)

REFRESH_IDLE_SECONDS: Final = 900
REFRESH_ACTIVE_SECONDS: Final = 600

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


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


UtcInstant = Annotated[datetime, PlainSerializer(_iso_z, return_type=str)]


class WidgetFlight(BaseModel):
    detail_url: str
    # For the server's own ranking and refresh cadence. The script never reads it: what
    # it draws is the pill and the milestone, which are words already chosen.
    phase: Phase
    title: str
    subtitle: str | None
    status_label: str
    status_tone: str
    milestone_label: str | None
    milestone_to: UtcInstant | None
    progress_percent: int | None


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
        zones=await load_zones(session, rows),
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
    """Active bookings around `now`, each with its newest snapshot."""
    bookings = (
        await session.scalars(
            select(Booking)
            .where(
                Booking.status == BookingStatus.ACTIVE,
                # Measured against the schedule plus the longest delay worth still
                # showing, so a flight running hours late stays on the widget until it
                # has actually landed rather than vanishing while it is still in the air.
                or_(
                    Booking.scheduled_arrival_utc >= now - LATE_ARRIVAL_ALLOWANCE,
                    and_(
                        Booking.scheduled_arrival_utc.is_(None),
                        Booking.scheduled_departure_utc >= now - LATE_ARRIVAL_ALLOWANCE,
                    ),
                ),
            )
            .order_by(Booking.scheduled_departure_utc)
            .limit(CANDIDATE_LIMIT)
        )
    ).all()
    if not bookings:
        return []

    latest = await booking_repo.latest_snapshots(session, [booking.id for booking in bookings])
    return [(booking, latest.get(booking.id)) for booking in bookings]


async def load_zones(session: AsyncSession, rows: Sequence[FlightRow]) -> dict[str, str]:
    """The zone at each flight's origin, for the one pill that names a day."""
    zones: dict[str, str] = {}
    for booking, _ in rows:
        if booking.origin_iata not in zones:
            airport = await get_airport(session, booking.origin_iata)
            zones[booking.origin_iata] = airport.tz if airport else FALLBACK_TZ
    return zones


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
    zones: Mapping[str, str] | None = None,
    degraded_reason: str | None = None,
) -> WidgetPayload:
    origins = zones or {}
    ranked: list[tuple[int, datetime, WidgetFlight]] = []
    observed: list[datetime] = []
    for booking, snapshot in rows:
        flight = _flight(
            booking,
            snapshot,
            settings=settings,
            now=now,
            base_url=base_url,
            origin_tz=origins.get(booking.origin_iata, FALLBACK_TZ),
        )
        ranked.append(
            (views.phase_rank(flight.phase), departure_estimate(booking, snapshot), flight)
        )
        if flight.phase in PHASES_IMMINENT and snapshot is not None and snapshot.observed_at:
            observed.append(snapshot.observed_at)
    ranked.sort(key=lambda row: (row[0], row[1]))
    flights = [flight for _, _, flight in ranked[:MAX_FLIGHTS]]

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
    next_up = views.milestone(phase, booking, snapshot)
    return WidgetFlight(
        detail_url=f"{base_url}/f/{booking.id}",
        phase=phase,
        title=(
            f"{booking.marketing_carrier}{booking.marketing_number}"
            f"  {booking.origin_iata} → {views.destination_iata(booking, snapshot)}"
        ),
        subtitle=_subtitle(phase, booking, snapshot, now),
        status_label=pill.label,
        status_tone=pill.tone,
        milestone_label=views.milestone_label(next_up, now) if next_up else None,
        milestone_to=next_up.target if next_up else None,
        # Only while airborne: the feed reports 0 on the ground and 100 after landing,
        # either of which draws a bar that says nothing.
        progress_percent=progress_estimate(booking, snapshot, now) if phase == AIRBORNE else None,
    )


def _subtitle(
    phase: Phase, booking: Booking, snapshot: FlightSnapshot | None, now: datetime
) -> str | None:
    """Gate, terminal or carousel, whichever is the one to walk towards now."""
    if phase == CANCELLED:
        return None
    if phase == DIVERTED:
        bound_for = views.destination_iata(booking, snapshot)
        return f"Diverted to {bound_for}" if bound_for != booking.dest_iata else "Diverted"

    parts: list[str] = []
    if phase == LANDED:
        # The belt once parked; until then the gate, which is still where the walk ends.
        if views.at_the_gate(phase, booking, snapshot, now):
            if snapshot and snapshot.baggage_claim:
                parts.append(f"Bag claim {snapshot.baggage_claim}")
        elif snapshot and snapshot.gate_destination:
            parts.append(f"Gate {snapshot.gate_destination}")
        if snapshot and snapshot.terminal_destination:
            parts.append(f"Terminal {snapshot.terminal_destination}")
        return " · ".join(parts) if parts else "Landed"

    if phase == AIRBORNE:
        if snapshot and snapshot.gate_destination:
            parts.append(f"Gate {snapshot.gate_destination}")
        if snapshot and snapshot.terminal_destination:
            parts.append(f"Terminal {snapshot.terminal_destination}")
        return " · ".join(parts) if parts else None

    # The aircraft has left the origin gate, so naming it would send someone backwards.
    if phase == TAXIING:
        return None

    if snapshot and snapshot.gate_origin:
        parts.append(f"Gate {snapshot.gate_origin}")
    if snapshot and snapshot.terminal_origin:
        parts.append(f"Terminal {snapshot.terminal_origin}")
    if parts:
        return " · ".join(parts)
    return f"Seat {booking.seat}" if booking.seat else None


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
