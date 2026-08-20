"""The polling worker: decides when each booking is next worth an AeroAPI call, and
spends the budget on the ones that are due.

The cadence is a pure function so it can be reasoned about and tested without a clock, a
database, or a network. Everything else in here is plumbing around it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .aeroapi import BudgetExceeded, budget_status, fetch_flight, record_snapshot
from .config import get_settings
from .db import session_scope
from .events import detect_changes
from .models import Booking, FlightSnapshot

log = logging.getLogger(__name__)

# Cadence, from furthest out to nearest.
FAR_HORIZON = timedelta(days=7)
DAY_HORIZON = timedelta(hours=24)
FINAL_HORIZON = timedelta(hours=3)
DAILY_INTERVAL = timedelta(days=1)
HOURLY_INTERVAL = timedelta(minutes=30)
CLOSE_INTERVAL = timedelta(minutes=10)

# Baggage claim and the on-blocks time are published minutes after the wheels stop, so
# the last few polls happen after the flight has, as far as the passenger is concerned,
# finished.
LANDED_TAIL = timedelta(minutes=90)

# A cancellation or a diversion is the one moment where the details change by the minute.
DISRUPTION_WINDOW = timedelta(hours=2)

# A flight whose departure passed this long ago without ever going airborne is not going
# to start now; something upstream lost it. Stop rather than poll it forever.
ABANDON_AFTER = timedelta(hours=24)

TICK_SECONDS = 30
BATCH_SIZE = 5
# A booking AeroAPI cannot resolve yet, or a call that failed: back off, do not hammer.
RETRY_INTERVAL = timedelta(minutes=30)


class SnapshotLike(Protocol):
    """The parts of a `FlightSnapshot` the cadence depends on.

    Read-only by design: the cadence never writes, and a Protocol of plain attributes
    would force every caller to match the column types exactly.
    """

    @property
    def scheduled_out(self) -> datetime | None: ...
    @property
    def estimated_out(self) -> datetime | None: ...
    @property
    def actual_off(self) -> datetime | None: ...
    @property
    def actual_on(self) -> datetime | None: ...
    @property
    def cancelled(self) -> bool | None: ...
    @property
    def diverted(self) -> bool | None: ...
    @property
    def observed_at(self) -> datetime | None: ...


def next_poll_at(now: datetime, snapshot_like: SnapshotLike) -> datetime | None:
    """When to look at this flight again, or None when there is nothing left to see.

    None is the signal to complete the booking; the caller owns that write.
    """
    now = _aware(now) or datetime.now(UTC)
    departure = _aware(snapshot_like.estimated_out) or _aware(snapshot_like.scheduled_out)
    actual_off = _aware(snapshot_like.actual_off)
    actual_on = _aware(snapshot_like.actual_on)

    if snapshot_like.cancelled or snapshot_like.diverted:
        observed = _aware(snapshot_like.observed_at) or now
        if now < observed + DISRUPTION_WINDOW:
            return now + CLOSE_INTERVAL

    if actual_on is not None:
        return now + CLOSE_INTERVAL if now <= actual_on + LANDED_TAIL else None

    if actual_off is not None:
        return now + CLOSE_INTERVAL

    if departure is None:
        # No usable estimate at all. Keep a moderate cadence rather than dropping a
        # booking on the strength of one thin response.
        return now + HOURLY_INTERVAL

    if now > departure + ABANDON_AFTER:
        return None

    remaining = departure - now
    if remaining > FAR_HORIZON:
        # Nothing to learn this far out, but the booking still needs a wake-up or it
        # would never be picked up again.
        return departure - FAR_HORIZON
    if remaining > DAY_HORIZON:
        return min(now + DAILY_INTERVAL, departure - DAY_HORIZON)
    if remaining > FINAL_HORIZON:
        return min(now + HOURLY_INTERVAL, departure - FINAL_HORIZON)
    return now + CLOSE_INTERVAL


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def run_poller(stopping: asyncio.Event) -> None:
    """Tick until asked to stop. Nothing here is allowed to raise out of the loop."""
    if not get_settings().aeroapi_configured:
        log.warning("AEROAPI_KEY is not set; the poller will not run")
        return

    log.info("poller started, ticking every %ss", TICK_SECONDS)
    while not stopping.is_set():
        try:
            await poll_once()
        except Exception:
            log.exception("poll tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=TICK_SECONDS)
    log.info("poller stopped")


async def poll_once() -> int:
    """One tick, and the number of bookings it polled. Also the CLI's manual pass."""
    async with session_scope() as session:
        status = await budget_status(session)
        if status.tripped:
            log.debug("budget breaker tripped for %s; skipping tick", status.month)
            return 0

        now = datetime.now(UTC)
        bookings = await _claim_due(session, now)
        for booking in bookings:
            try:
                await _poll_booking(session, booking, now)
            except BudgetExceeded:
                # The cap was hit part-way through the batch; the rest wait for a month
                # that has budget in it.
                log.error("budget exhausted mid-tick; abandoning the rest of the batch")
                break
            except Exception:
                log.exception("polling booking %s failed", booking.id)
                booking.next_poll_at = now + RETRY_INTERVAL
        return len(bookings)


async def _claim_due(session: AsyncSession, now: datetime) -> list[Booking]:
    """The next small batch of bookings that are due.

    No row locking, and none needed: the whole tick is one write transaction, and SQLite
    admits one writer at a time, so a second tick cannot read this batch until the first
    has finished rescheduling it and committed.
    """
    result = await session.scalars(
        select(Booking)
        .where(
            Booking.status == "active",
            Booking.next_poll_at.is_not(None),
            Booking.next_poll_at <= now,
        )
        .order_by(Booking.next_poll_at)
        .limit(BATCH_SIZE)
    )
    return list(result)


async def _poll_booking(session: AsyncSession, booking: Booking, now: datetime) -> None:
    previous = await _latest_snapshot(session, booking)
    flight = await fetch_flight(session, booking)
    if flight is None:
        booking.next_poll_at = now + RETRY_INTERVAL
        return

    current = await record_snapshot(session, booking, flight)
    await detect_changes(session, booking, previous, current)

    following = next_poll_at(now, current)
    if following is None:
        booking.status = "completed"
        booking.next_poll_at = None
        log.info("booking %s completed", booking.id)
    else:
        booking.next_poll_at = following


async def _latest_snapshot(session: AsyncSession, booking: Booking) -> FlightSnapshot | None:
    snapshot: FlightSnapshot | None = await session.scalar(
        select(FlightSnapshot)
        .where(FlightSnapshot.booking_id == booking.id)
        .order_by(FlightSnapshot.observed_at.desc(), FlightSnapshot.id.desc())
        .limit(1)
    )
    return snapshot
