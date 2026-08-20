"""Process wiring: the API, the poll worker, the mail loop and the event dispatcher.

They all live in one process on purpose. There is one user and a handful of flights in
flight at a time, so the coordination a second process would need costs more than it
saves. The pieces are still independent tasks: any one of them failing leaves the others
running, and the health page says which is unhappy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI

from . import prefs
from .config import Settings, ensure_widget_token
from .db import dispose_engine, init_engine, session_scope

log = logging.getLogger(__name__)

# Slow enough to be invisible in API cost, fast enough that a gate change reaches the
# phone within a poll cycle rather than sitting in the table until the next one.
DISPATCH_INTERVAL_SECONDS = 20


async def _dispatch_loop(settings: Settings, stopping: asyncio.Event) -> None:
    """Drain undelivered events to Pushover and iCloud Calendar.

    Kept apart from the poller so an Apple outage delays notifications instead of
    stalling the polling that produces them.
    """
    from sqlalchemy import select

    from .caldav import CalendarClient
    from .events import dispatch_pending
    from .models import Airport
    from .notify import Notifier

    # Loaded once: the calendar needs an airport's name and zone to write a geocodable
    # location and a correctly zoned time, and the table is seeded at startup and static
    # thereafter.
    async with session_scope() as session:
        rows = (await session.scalars(select(Airport))).all()
    airports = {airport.iata: airport for airport in rows}

    notifier = Notifier(settings)
    calendar = CalendarClient(settings, airports)

    while not stopping.is_set():
        try:
            async with session_scope() as session:
                await dispatch_pending(session, notifier, calendar)
        except Exception:
            log.exception("event dispatch failed; retrying next pass")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), DISPATCH_INTERVAL_SECONDS)


async def _supervise(name: str, coro: object) -> None:
    """Run a background loop, logging rather than silently swallowing its death."""
    try:
        await coro  # type: ignore[misc]
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("background task %s stopped", name)


def migrate() -> None:
    """Bring the schema up to head.

    Run on every boot rather than left to the operator: a container serving against a
    schema it cannot read is a worse failure than a few seconds of startup, and the
    upgrade is a no-op once there is nothing left to apply. `alembic/env.py` reads the
    database URL from the environment, so there is nothing to pass in here.
    """
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    await asyncio.to_thread(migrate)
    init_engine(settings)

    from .airports import seed_airports
    from .ingest import run_ingest_loop
    from .poller import run_poller

    async with session_scope() as session:
        count = await seed_airports(session)
        await prefs.load(session)
    log.info("airports seeded: %d", count)
    # Logging was configured before the database existed, so the stored level is only
    # applied once there is a row to read it from.
    logging.getLogger().setLevel(prefs.current().log_level.upper())
    ensure_widget_token()

    stopping = asyncio.Event()
    tasks = [
        asyncio.create_task(_supervise("poller", run_poller(stopping)), name="poller"),
        asyncio.create_task(
            _supervise("dispatch", _dispatch_loop(settings, stopping)), name="dispatch"
        ),
        asyncio.create_task(_supervise("ingest", run_ingest_loop(stopping)), name="ingest"),
    ]
    if not settings.icloud_configured:
        log.warning("iCloud is not configured; bookings must be added by hand")
    elif not prefs.current().calendar_configured:
        log.warning("no iCloud calendar is picked; nothing will reach the calendar")

    app.state.background_tasks = tasks
    try:
        yield
    finally:
        stopping.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    from .config import get_settings
    from .web import create_app as create_web_app

    settings = settings or get_settings()
    app = create_web_app(settings)
    app.state.settings = settings
    app.router.lifespan_context = lifespan
    return app
