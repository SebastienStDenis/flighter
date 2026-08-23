"""Shared fixtures.

Tests here are deliberately database-free: everything worth testing in this project is a
pure function over data (timezone normalisation, poll cadence, snapshot diffing, payload
shaping), and a suite that needs a database on disk is a suite that stops being run.
The exception is SQL that has to be proven against SQLite itself - the dedupe rule, the
newest-snapshot query, delivery stamping, pruning - which runs against an in-memory
database that lives exactly as long as the test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from flighter import airports, db, prefs
from flighter.config import CREDENTIALS, Settings
from flighter.models import Base
from flighter.prefs import Prefs

# Every credential named explicitly, so that a developer's own .env or data/secrets.env
# can never be what a test is really asserting against.
BLANK = dict.fromkeys((*CREDENTIALS, "widget_token"), "")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        **BLANK
        | {
            "aeroapi_key": "test-key",
            "widget_token": "test-token",
            "icloud_email": "someone@icloud.com",
            "icloud_app_password": "abcd-efgh-ijkl-mnop",
            "pushover_token": "app-token",
            "pushover_user_key": "user-key",
        }
    )


@pytest.fixture
def unconfigured() -> Settings:
    """A deployment on its first boot, with nothing entered anywhere."""
    return Settings(**BLANK)


@pytest.fixture(autouse=True)
def preferences(monkeypatch: pytest.MonkeyPatch) -> Prefs:
    """A fully configured deployment, installed as the live preferences.

    Autouse because the modules under test read the live row rather than being handed
    one, and a developer's own database must never be what a test asserts against.
    """
    configured = Prefs(
        public_base_url="https://flights.example.com",
        icloud_calendar_url="https://p34-caldav.icloud.com/12345/calendars/6c1f4f0e-flights/",
        # The master switch starts off on a deployment nobody has set up; this one is
        # set up, and every test about what reaches a phone starts from that.
        notifications_enabled=True,
    )
    monkeypatch.setattr(prefs, "_current", configured)
    monkeypatch.setattr(prefs, "_last_seen_origin", None)
    return configured


@pytest.fixture
async def database(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """An empty in-memory database installed as the process's engine.

    One connection, shared, so that `session_scope()` everywhere in the code under test
    reaches the same tables - and so that a transaction left open across another is an
    immediate error here rather than a busy timeout in production.
    """
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_sessionmaker", None)
    monkeypatch.setattr(airports, "_tz_cache", {})
    engine = db.bind_engine(create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield db.sessionmaker()
    await db.dispose_engine()
