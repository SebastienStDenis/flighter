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

from .config import Settings
from .db import dispose_engine, init_engine, session_scope

log = logging.getLogger(__name__)

# Slow enough to be invisible in API cost, fast enough that a gate change reaches the
# phone within a poll cycle rather than sitting in the table until the next one.
DISPATCH_INTERVAL_SECONDS = 20


async def _dispatch_loop(settings: Settings, stopping: asyncio.Event) -> None:
    """Drain undelivered events to ntfy and Google Calendar.

    Kept apart from the poller so a Google outage delays notifications instead of
    stalling the polling that produces them.
    """
    from .events import dispatch_pending
    from .gcal import CalendarClient
    from .notify import Notifier

    notifier = Notifier(settings)
    calendar = CalendarClient(settings)

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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    init_engine(settings)

    from .airports import seed_airports
    from .ingest import run_ingest_loop
    from .poller import run_poller

    async with session_scope() as session:
        count = await seed_airports(session)
    log.info("airports seeded: %d", count)

    stopping = asyncio.Event()
    tasks = [
        asyncio.create_task(_supervise("poller", run_poller(stopping)), name="poller"),
        asyncio.create_task(
            _supervise("dispatch", _dispatch_loop(settings, stopping)), name="dispatch"
        ),
    ]
    if settings.gmail_configured:
        tasks.append(
            asyncio.create_task(_supervise("ingest", run_ingest_loop(stopping)), name="ingest")
        )
    else:
        log.warning("Gmail is not configured; bookings must be added by hand")

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
