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

from . import bookings as booking_repo
from . import prefs
from .aeroapi import budget_status
from .config import Settings, get_settings
from .db import get_session
from .models import Booking, BookingStatus, FlightSnapshot
from .phase import (
    AIRBORNE,
    ARRIVAL_DELAY_THRESHOLD,
    CANCELLED,
    CANCELLED_NOTICE,
    DAY_OF,
    DEPARTURE_DELAY_THRESHOLD,
    DIVERTED,
    LANDED,
    TAXIING,
    Phase,
    compute_phase,
    departure_estimate,
)
from .views import countdown, phase_rank

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


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


UtcInstant = Annotated[datetime, PlainSerializer(_iso_z, return_type=str)]


class WidgetFlight(BaseModel):
    detail_url: str
    phase: Phase
    title: str
    subtitle: str | None
    countdown_label: str | None
    countdown_to: UtcInstant | None
    delayed: bool
    progress_percent: int | None


class WidgetPayload(BaseModel):
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
        ranked.append((phase_rank(flight.phase), departure_estimate(booking, snapshot), flight))
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
    booking: Booking, snapshot: FlightSnapshot | None, *, settings: Settings, now: datetime
) -> WidgetFlight:
    phase = compute_phase(booking, snapshot, now)
    label, countdown_to = countdown(phase, booking, snapshot)
    return WidgetFlight(
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


def _subtitle(phase: Phase, booking: Booking, snapshot: FlightSnapshot | None) -> str | None:
    """Gate, terminal or carousel, whichever is the one to walk towards now."""
    if phase == CANCELLED:
        return CANCELLED_NOTICE
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


def _delayed(snapshot: FlightSnapshot | None) -> bool:
    if snapshot is None:
        return False
    arrival = (snapshot.estimated_in or snapshot.actual_in, snapshot.scheduled_in)
    departure = (snapshot.estimated_out or snapshot.actual_out, snapshot.scheduled_out)
    # Once the aircraft is off the ground a late pushback is history, and the only
    # question left is whether it still arrives late.
    pairs = (
        ((arrival, ARRIVAL_DELAY_THRESHOLD),)
        if snapshot.actual_off is not None
        else ((departure, DEPARTURE_DELAY_THRESHOLD), (arrival, ARRIVAL_DELAY_THRESHOLD))
    )
    return any(
        expected is not None and scheduled is not None and expected - scheduled >= threshold
        for (expected, scheduled), threshold in pairs
    )


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
