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
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, PlainSerializer
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import prefs
from .aeroapi import budget_status
from .config import Settings, get_settings
from .db import get_session
from .models import Booking, FlightSnapshot
from .phase import (
    AIRBORNE,
    BOARDING,
    BOARDING_LEAD,
    CANCELLED,
    DAY_OF,
    DIVERTED,
    LANDED,
    Phase,
    boarding_time,
    compute_phase,
    departure_estimate,
    landing_estimate,
)

log = logging.getLogger(__name__)

router = APIRouter()

# A lock screen has room for one flight and a home screen for three. Anything past that
# is a trip itinerary, which is what the web UI is for.
MAX_FLIGHTS: Final = 3
# Wider than MAX_FLIGHTS because relevance order is not departure order: an airborne
# flight sorts ahead of one that departs sooner.
CANDIDATE_LIMIT: Final = 12
# A flight leaves the widget once it has landed, not once it has departed: the whole
# point of the airborne row is the "Lands in" countdown. This mirrors the rule in
# bookings.list_bookings(upcoming_only=True), plus a grace period so a flight that just
# landed sticks around long enough to tell you which carousel to walk to.
LANDED_GRACE: Final = timedelta(hours=2)

REFRESH_IDLE_SECONDS: Final = 900
REFRESH_ACTIVE_SECONDS: Final = 600

# Feeds restate scheduled times with a minute of jitter; below this a "delay" is noise.
DELAY_THRESHOLD: Final = timedelta(minutes=5)

# The poller runs a close flight every 10 minutes and a same-day one every 30, so a
# snapshot this old means polling has stopped rather than that nothing has changed.
POLL_STALE_AFTER: Final = timedelta(minutes=45)

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
    return build_payload(
        rows, settings=settings, now=now, degraded_reason=await read_degraded(session)
    )


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
            .where(
                Booking.status == "active",
                or_(
                    Booking.scheduled_arrival_utc >= now - LANDED_GRACE,
                    and_(
                        Booking.scheduled_arrival_utc.is_(None),
                        Booking.scheduled_departure_utc >= now - LANDED_GRACE,
                    ),
                ),
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
    degraded_reason: str | None = None,
) -> WidgetPayload:
    ranked: list[tuple[int, datetime, WidgetFlight]] = []
    observed: list[datetime] = []
    for booking, snapshot in rows:
        flight = _flight(booking, snapshot, settings=settings, now=now)
        ranked.append((_rank(flight.phase), departure_estimate(booking, snapshot), flight))
        if flight.phase in PHASES_IMMINENT and snapshot is not None and snapshot.observed_at:
            observed.append(snapshot.observed_at)
    ranked.sort(key=lambda row: (row[0], row[1]))
    flights = [flight for _, _, flight in ranked[:MAX_FLIGHTS]]

    reason = degraded_reason or _stale_reason(min(observed, default=None), now)
    return WidgetPayload(
        generated_at=now,
        flights=flights,
        refresh_seconds=_refresh_seconds(flights),
        degraded=reason is not None,
        degraded_reason=reason,
    )


def _flight(
    booking: Booking, snapshot: FlightSnapshot | None, *, settings: Settings, now: datetime
) -> WidgetFlight:
    phase = compute_phase(booking, snapshot, now)
    label, countdown_to = _countdown(phase, booking, snapshot)
    return WidgetFlight(
        id=booking.id,
        detail_url=f"{prefs.current().public_base_url}/f/{booking.id}",
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
    )


def _countdown(
    phase: Phase, booking: Booking, snapshot: FlightSnapshot | None
) -> tuple[str | None, datetime | None]:
    """The one instant the phone counts to, and what to call it."""
    if phase == BOARDING:
        return "Boards in", boarding_time(snapshot) or (
            departure_estimate(booking, snapshot) - BOARDING_LEAD
        )
    if phase in (AIRBORNE, DIVERTED):
        # Wheels down, not the gate: this is the number someone stares at from a seat,
        # and taxiing is not part of what they are counting.
        landing = landing_estimate(booking, snapshot)
        return ("Lands in", landing) if landing is not None else (None, None)
    if phase in (LANDED, CANCELLED):
        return None, None
    return "Departs in", departure_estimate(booking, snapshot)


def _subtitle(phase: Phase, booking: Booking, snapshot: FlightSnapshot | None) -> str | None:
    """Gate, terminal or carousel, whichever is the one to walk towards now."""
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


def _rank(phase: Phase) -> int:
    """In progress first, then what is still coming, then what has already landed.

    Departure time breaks the tie in every band, including for a cancelled flight, which
    still belongs on the day it was supposed to leave.
    """
    if phase in PHASES_IN_PROGRESS:
        return 0
    if phase == LANDED:
        return 2
    return 1


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
