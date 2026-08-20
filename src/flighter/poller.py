"""The polling worker: spends the budget on the bookings that are due, and keeps the
tables from growing without bound.

When a booking is due is `cadence`'s decision; everything in here is plumbing around it.
No database transaction is held across an AeroAPI call: a tick claims its batch and
commits, asks FlightAware about each booking with no session open, then writes each
observation in a transaction of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .aeroapi import BudgetExceeded, budget_status, fetch_flight, record_snapshot
from .bookings import latest_snapshot
from .cadence import RETRY_INTERVAL, next_poll_at, retry_poll_at
from .config import get_settings
from .db import session_scope
from .events import detect_changes
from .models import ApiUsage, Booking, BookingStatus, FlightEvent, FlightSnapshot

log = logging.getLogger(__name__)

TICK_SECONDS = 30
BATCH_SIZE = 5

# How long the history is kept. Snapshots and events are the detail behind a flight that
# has flown, worth a season and no more; usage rows are what the breaker sums, and a
# year plus the month in progress is enough to compare one month's bill with last
# year's.
HISTORY_RETENTION = timedelta(days=90)
USAGE_RETENTION = timedelta(days=13 * 31)
PRUNE_INTERVAL = timedelta(days=1)


async def run_poller(stopping: asyncio.Event) -> None:
    """Tick until asked to stop. Nothing here is allowed to raise out of the loop."""
    log.info("poller started, ticking every %ss", TICK_SECONDS)
    prune_due = datetime.now(UTC)
    while not stopping.is_set():
        # Waited for rather than given up on: a key saved on the settings page is live in
        # this process immediately, and the poller is what has to notice.
        if not get_settings().aeroapi_configured:
            log.debug("no FlightAware key; there is nothing to poll with")
        else:
            try:
                await poll_once()
            except Exception:
                log.exception("poll tick failed")
        if datetime.now(UTC) >= prune_due:
            try:
                await prune_history()
            except Exception:
                log.exception("pruning old rows failed")
            prune_due = datetime.now(UTC) + PRUNE_INTERVAL
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=TICK_SECONDS)
    log.info("poller stopped")


async def poll_once() -> int:
    """One tick, and the number of bookings it polled. Also the CLI's manual pass."""
    now = datetime.now(UTC)
    async with session_scope() as session:
        status = await budget_status(session)
        if status.tripped:
            log.debug("budget breaker tripped for %s; skipping tick", status.month)
            return 0
        bookings = await _claim_due(session, now)

    for booking in bookings:
        try:
            await _poll_booking(booking, now)
        except BudgetExceeded:
            # The cap was hit part-way through the batch; the rest wait for a month
            # that has budget in it.
            log.error("budget exhausted mid-tick; abandoning the rest of the batch")
            break
        except Exception:
            # The lease taken on claim stands, so the booking comes round again after
            # the retry interval with nothing else to write here.
            log.exception("polling booking %s failed", booking.id)
    return len(bookings)


async def _claim_due(session: AsyncSession, now: datetime) -> list[Booking]:
    """The next small batch of bookings that are due, leased until the tick is done.

    The lease is the lock. Pushing `next_poll_at` out before the session closes means
    a second pass - the next tick, or a `flighter poll` run by hand - cannot pick the
    same booking up while this one is still waiting on FlightAware, and a crash
    mid-tick costs one retry interval rather than a booking stuck as due.
    """
    result = await session.scalars(
        select(Booking)
        .where(
            Booking.status == BookingStatus.ACTIVE,
            Booking.next_poll_at.is_not(None),
            Booking.next_poll_at <= now,
        )
        .order_by(Booking.next_poll_at)
        .limit(BATCH_SIZE)
    )
    bookings = list(result)
    for booking in bookings:
        booking.next_poll_at = now + RETRY_INTERVAL
    await session.flush()
    return bookings


async def _poll_booking(booking: Booking, now: datetime) -> None:
    async with session_scope() as session:
        previous = await latest_snapshot(session, booking.id)

    flight = await fetch_flight(booking)

    async with session_scope() as session:
        session.add(booking)
        if flight is None:
            _reschedule(booking, retry_poll_at(now, booking.scheduled_departure_utc))
            return
        current = await record_snapshot(session, booking, flight)
        await detect_changes(session, booking, previous, current)
        _reschedule(booking, next_poll_at(now, current, previous))


def _reschedule(booking: Booking, following: datetime | None) -> None:
    if following is None:
        booking.status = BookingStatus.COMPLETED
        booking.next_poll_at = None
        log.info("booking %s completed", booking.id)
    else:
        booking.next_poll_at = following


async def prune_history(now: datetime | None = None) -> None:
    """Drop what nobody will read again.

    Snapshots go only for bookings that are finished, since an active one's newest row
    is its current state however old it is. Events go on age alone: delivery stops
    retrying hours after an event, so anything this old is final whether or not it
    ever went out.
    """
    now = now or datetime.now(UTC)
    history_cutoff = now - HISTORY_RETENTION
    finished = select(Booking.id).where(
        Booking.status.in_([BookingStatus.COMPLETED, BookingStatus.ARCHIVED])
    )
    async with session_scope() as session:
        await session.execute(
            delete(FlightSnapshot).where(
                FlightSnapshot.observed_at < history_cutoff,
                FlightSnapshot.booking_id.in_(finished),
            )
        )
        await session.execute(delete(FlightEvent).where(FlightEvent.occurred_at < history_cutoff))
        await session.execute(delete(ApiUsage).where(ApiUsage.called_at < now - USAGE_RETENTION))
    log.info("pruned history older than %s", history_cutoff.date())
