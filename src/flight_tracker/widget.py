"""The phone widget's only endpoint: every display decision, already made.

The Scriptable script on the phone is deliberately stupid. It draws strings and starts
one live timer; it does not know what a diversion is or which gate belongs to which end
of the flight. Everything that could be got wrong is got wrong here, once, where it is
covered by tests.

Two rules hold the contract together. Every instant is ISO-8601 UTC with a `Z`, because
the phone renders it relative to its own clock and a missing zone silently shifts the
countdown. And the payload is a pydantic model, so a field that drifts breaks a test
rather than a lock screen.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, PlainSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import Settings, get_settings
from .db import get_session
from .models import KV, Booking, FlightSnapshot
from .phase import (
    AIRBORNE,
    BOARDING,
    CANCELLED,
    DAY_OF,
    DIVERTED,
    LANDED,
    Phase,
    arrival_estimate,
    boarding_time,
    compute_phase,
    departure_estimate,
)

log = logging.getLogger(__name__)

router = APIRouter()

# A lock screen has room for one flight and a home screen for three. Anything past that
# is a trip itinerary, which is what the web UI is for.
MAX_FLIGHTS: Final = 3
# Wider than MAX_FLIGHTS because relevance order is not departure order: an airborne
# flight sorts ahead of one that departs sooner.
CANDIDATE_LIMIT: Final = 12
# A flight stays on the widget through the day it lands, then falls off on its own.
RECENT_WINDOW: Final = timedelta(hours=24)

REFRESH_IDLE_SECONDS: Final = 900
REFRESH_ACTIVE_SECONDS: Final = 600

# Feeds restate scheduled times with a minute of jitter; below this a "delay" is noise.
DELAY_THRESHOLD: Final = timedelta(minutes=5)

# Written by the poller. Absent is the normal, healthy case.
KV_BREAKER_KEY: Final = "aeroapi_breaker"
KV_LAST_POLL_KEY: Final = "poller_last_success"
POLL_STALE_AFTER: Final = timedelta(minutes=30)
# The poller states its last success under one of these; tolerated so a rename there
# degrades the staleness check rather than the endpoint.
_TIMESTAMP_FIELDS: Final = ("at", "last_success_at", "observed_at")

PHASES_IN_PROGRESS: Final = frozenset({BOARDING, AIRBORNE, DIVERTED})
PHASES_IMMINENT: Final = frozenset({DAY_OF, BOARDING, AIRBORNE, DIVERTED})


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


UtcInstant = Annotated[datetime, PlainSerializer(_iso_z, return_type=str)]


class WidgetFlight(BaseModel):
    id: int
    detail_url: str
    phase: Phase
    title: str
    subtitle: str | None
    countdown_label: str | None
    countdown_to: UtcInstant | None
    delayed: bool
    progress_percent: int | None
    passenger: str
    is_self: bool


class WidgetPayload(BaseModel):
    generated_at: UtcInstant
    flights: list[WidgetFlight]
    refresh_seconds: int
    degraded: bool
    degraded_reason: str | None


FlightRow = tuple[Booking, FlightSnapshot | None]


@router.get("/api/widget")
async def read_widget(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
) -> WidgetPayload:
    authorize(settings, authorization, token)
    now = datetime.now(UTC)
    rows = await load_flight_rows(session, now)
    return build_payload(rows, settings=settings, now=now, degraded_reason=await read_degraded(session, now))


def authorize(settings: Settings, authorization: str | None, token: str | None) -> None:
    """Bearer header, or `?token=` for pasting the URL into a browser to debug.

    An unset token refuses everything. The alternative reading, that a blank token means
    no authentication, publishes the user's travel plans the moment the environment
    variable is forgotten, so the failure is loud instead.
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


async def load_flight_rows(session: AsyncSession, now: datetime) -> list[FlightRow]:
    """Active bookings around `now`, each with its newest snapshot."""
    bookings = (
        await session.scalars(
            select(Booking)
            .options(selectinload(Booking.passenger))
            .where(
                Booking.status == "active",
                Booking.scheduled_departure_utc >= now - RECENT_WINDOW,
            )
            .order_by(Booking.scheduled_departure_utc)
            .limit(CANDIDATE_LIMIT)
        )
    ).all()
    if not bookings:
        return []

    latest = (
        await session.scalars(
            select(FlightSnapshot)
            .where(FlightSnapshot.booking_id.in_([b.id for b in bookings]))
            .distinct(FlightSnapshot.booking_id)
            .order_by(FlightSnapshot.booking_id, FlightSnapshot.observed_at.desc())
        )
    ).all()
    by_booking = {snapshot.booking_id: snapshot for snapshot in latest}
    return [(booking, by_booking.get(booking.id)) for booking in bookings]


async def read_degraded(session: AsyncSession, now: datetime) -> str | None:
    """Why the numbers might be wrong, in words the widget can print verbatim."""
    rows = (
        await session.scalars(select(KV).where(KV.key.in_([KV_BREAKER_KEY, KV_LAST_POLL_KEY])))
    ).all()
    values = {row.key: row.value for row in rows}

    breaker = values.get(KV_BREAKER_KEY)
    if isinstance(breaker, dict) and breaker.get("tripped", True):
        reason = breaker.get("reason")
        return str(reason) if reason else "AeroAPI budget reached, status is frozen"

    last_poll = _timestamp(values.get(KV_LAST_POLL_KEY))
    if last_poll is not None and now - last_poll > POLL_STALE_AFTER:
        minutes = int((now - last_poll).total_seconds() // 60)
        return f"No status update in {minutes} min"
    return None


def build_payload(
    rows: Sequence[FlightRow],
    *,
    settings: Settings,
    now: datetime,
    degraded_reason: str | None = None,
) -> WidgetPayload:
    flights = [_flight(booking, snapshot, settings=settings, now=now) for booking, snapshot in rows]
    flights.sort(key=_relevance)
    flights = flights[:MAX_FLIGHTS]
    return WidgetPayload(
        generated_at=now,
        flights=flights,
        refresh_seconds=_refresh_seconds(flights),
        degraded=degraded_reason is not None,
        degraded_reason=degraded_reason,
    )


def _flight(
    booking: Booking, snapshot: FlightSnapshot | None, *, settings: Settings, now: datetime
) -> WidgetFlight:
    phase = compute_phase(booking, snapshot, now)
    label, countdown_to = _countdown(phase, booking, snapshot)
    return WidgetFlight(
        id=booking.id,
        detail_url=f"{settings.public_base_url}/f/{booking.id}",
        phase=phase,
        title=(
            f"{booking.marketing_carrier}{booking.marketing_number}"
            f"  {booking.origin_iata} → {booking.dest_iata}"
        ),
        subtitle=_subtitle(phase, booking, snapshot),
        countdown_label=label,
        countdown_to=countdown_to,
        delayed=_delayed(snapshot),
        # Only while airborne: the feed reports 0 on the ground and 100 after landing,
        # either of which draws a bar that says nothing.
        progress_percent=snapshot.progress_percent if snapshot and phase == AIRBORNE else None,
        passenger=booking.passenger.display_name,
        is_self=booking.passenger.is_self,
    )


def _countdown(
    phase: Phase, booking: Booking, snapshot: FlightSnapshot | None
) -> tuple[str | None, datetime | None]:
    """The one instant the phone counts to, and what to call it."""
    if phase == BOARDING:
        return "Boards in", boarding_time(snapshot) or (
            departure_estimate(booking, snapshot) - timedelta(minutes=30)
        )
    if phase in (AIRBORNE, DIVERTED):
        arrival = arrival_estimate(booking, snapshot)
        return ("Lands in", arrival) if arrival is not None else (None, None)
    if phase in (LANDED, CANCELLED):
        return None, None
    return "Departs in", departure_estimate(booking, snapshot)


def _subtitle(phase: Phase, booking: Booking, snapshot: FlightSnapshot | None) -> str | None:
    """Gate, terminal or carousel, whichever the traveller is walking towards now."""
    if phase == CANCELLED:
        return "Cancelled"
    if phase == DIVERTED:
        return "Diverted"

    parts: list[str] = []
    if phase == LANDED:
        if snapshot and snapshot.baggage_claim:
            parts.append(f"Bag claim {snapshot.baggage_claim}")
        if snapshot and snapshot.terminal_destination:
            parts.append(f"Terminal {snapshot.terminal_destination}")
        return " · ".join(parts) if parts else "Landed"

    if phase == AIRBORNE:
        if snapshot and snapshot.gate_destination:
            parts.append(f"Gate {snapshot.gate_destination}")
        if snapshot and snapshot.terminal_destination:
            parts.append(f"Terminal {snapshot.terminal_destination}")
        return " · ".join(parts) if parts else None

    if snapshot and snapshot.gate_origin:
        parts.append(f"Gate {snapshot.gate_origin}")
    if snapshot and snapshot.terminal_origin:
        parts.append(f"Terminal {snapshot.terminal_origin}")
    if parts:
        return " · ".join(parts)
    return f"Seat {booking.seat}" if booking.seat else None


def _delayed(snapshot: FlightSnapshot | None) -> bool:
    if snapshot is None:
        return False
    pairs = (
        (snapshot.estimated_out or snapshot.actual_out, snapshot.scheduled_out),
        (snapshot.estimated_in or snapshot.actual_in, snapshot.scheduled_in),
    )
    return any(
        expected is not None and scheduled is not None and expected - scheduled >= DELAY_THRESHOLD
        for expected, scheduled in pairs
    )


def _relevance(flight: WidgetFlight) -> tuple[int, datetime]:
    """In progress first, then soonest. Landed flights sink below what is still coming."""
    if flight.phase in PHASES_IN_PROGRESS:
        rank = 0
    elif flight.phase == LANDED:
        rank = 2
    else:
        rank = 1
    return rank, flight.countdown_to or datetime.max.replace(tzinfo=UTC)


def _refresh_seconds(flights: Sequence[WidgetFlight]) -> int:
    """Mirror the server's own cadence; polling faster than it updates buys nothing."""
    if any(flight.phase in PHASES_IMMINENT for flight in flights):
        return REFRESH_ACTIVE_SECONDS
    return REFRESH_IDLE_SECONDS


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, dict):
        return None
    for field in _TIMESTAMP_FIELDS:
        raw = value.get(field)
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            log.warning("unparseable timestamp in kv key: %r", raw)
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
