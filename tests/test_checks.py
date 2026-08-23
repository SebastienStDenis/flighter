"""`flighter check`: five probes, and the sentence each of them hands a person.

Every dependency is stood in for - the database is the in-memory one, AeroAPI answers
through a mock transport, and the mailbox, the calendar and Pushover are fakes - so the
suite proves the wording and the ok/failed verdict without an account anywhere.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flighter import caldav, checks, mail, prefs
from flighter.aeroapi import AeroAPIClient, TokenBucket
from flighter.caldav import Collection
from flighter.checks import run_checks
from flighter.config import Settings
from flighter.db import session_scope
from flighter.models import Airport
from flighter.notify import PushFailed
from flighter.prefs import Prefs

CALENDAR_URL = "https://p34-caldav.icloud.com/12345/calendars/6c1f4f0e-flights/"


async def seed_one_airport(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        session.add(
            Airport(
                iata="YYC",
                icao="CYYC",
                name="Calgary International",
                city="Calgary",
                country="CA",
                latitude=51.1,
                longitude=-114.0,
                tz="America/Edmonton",
            )
        )
        await session.commit()


def aeroapi(settings: Settings, handler: Any) -> AeroAPIClient:
    """A client that spends through the real meter but answers from the mock transport."""
    return AeroAPIClient(
        settings,
        transport=httpx.MockTransport(handler),
        limiter=TokenBucket(600),
        sessions=session_scope,
    )


class FakeMailbox:
    """Enough of the mailbox for the probe: it signs in and counts what carries the flag."""

    def __init__(self, settings: Settings, *, waiting: int = 2, fails: str | None = None) -> None:
        self.colour = "grey"
        self.mailboxes = ("INBOX", "Travel")
        self._waiting = waiting
        self._fails = fails
        self.closed = False

    async def connect(self) -> None:
        if self._fails:
            raise RuntimeError(self._fails)

    async def count_flagged(self) -> int:
        return self._waiting

    async def close(self) -> None:
        self.closed = True


class FakeCalendars:
    def __init__(self, settings: Settings, *offered: Collection, fails: str | None = None) -> None:
        self._offered = list(offered)
        self._fails = fails

    async def calendars(self) -> list[Collection]:
        if self._fails:
            raise caldav.CalendarUnavailable(self._fails)
        return self._offered


class FakeNotifier:
    def __init__(self, settings: Settings, refuses: str | None = None) -> None:
        self._refuses = refuses
        self.sent = 0

    async def check(self) -> None:
        if self._refuses:
            raise PushFailed(self._refuses)
        self.sent += 1


def one(results: list[checks.CheckResult], name: str) -> checks.CheckResult:
    return next(result for result in results if result.name == name)


# -- the database --------------------------------------------------------------------


async def test_the_database_probe_wants_airports_as_well_as_a_connection(
    database: async_sessionmaker[AsyncSession],
) -> None:
    """A schema with no airports is a boot that did not finish, not a healthy database."""
    result = await checks._check_database()

    assert (result.ok, result.detail) == (False, "reachable but no airports seeded")


async def test_the_database_probe_passes_once_the_table_is_seeded(
    database: async_sessionmaker[AsyncSession],
) -> None:
    await seed_one_airport(database)

    result = await checks._check_database()

    assert (result.ok, result.detail) == (True, "1 airports seeded")


async def test_a_database_that_will_not_open_is_reported_as_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextlib.asynccontextmanager
    async def refuse() -> AsyncIterator[None]:
        raise RuntimeError("unable to open database file")
        yield

    monkeypatch.setattr(checks, "session_scope", refuse)

    result = await checks._check_database()

    assert (result.ok, result.detail) == (False, "unable to open database file")


# -- AeroAPI -------------------------------------------------------------------------


async def test_aeroapi_is_not_probed_until_there_is_a_key(unconfigured: Settings) -> None:
    result = await checks._check_aeroapi(unconfigured)

    assert (result.ok, result.detail) == (False, "add a FlightAware key under Accounts")


async def test_a_rejected_aeroapi_key_says_so_rather_than_showing_a_status_code(
    settings: Settings, database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unauthorised(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"title": "Unauthorized"})

    monkeypatch.setattr(checks, "shared_client", lambda: aeroapi(settings, unauthorised))

    result = await checks._check_aeroapi(settings)

    assert (result.ok, result.detail) == (False, "key rejected")


async def test_a_working_aeroapi_key_spends_one_result_set(
    settings: Settings, database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe is a real call: a key that is never spent proves nothing about the key."""
    asked: list[httpx.URL] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.url)
        return httpx.Response(200, json={"flights": [{"ident": "UAL4"}]})

    monkeypatch.setattr(checks, "shared_client", lambda: aeroapi(settings, answer))

    result = await checks._check_aeroapi(settings)

    assert (result.ok, result.detail) == (True, "1 flights returned for UAL4")
    assert asked[0].params["ident_type"] == "designator"
    assert asked[0].params["max_pages"] == "1"


# -- mail ----------------------------------------------------------------------------


async def test_mail_is_not_probed_until_there_is_an_apple_id(unconfigured: Settings) -> None:
    result = await checks._check_mail(unconfigured)

    assert result.ok is False
    assert "app-specific password" in result.detail


async def test_the_mail_probe_says_how_much_is_waiting(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mail, "Mailbox", FakeMailbox)

    result = await checks._check_mail(settings)

    assert (result.ok, result.detail) == (True, "2 message(s) flagged grey across 2 mailbox(es)")


async def test_a_mailbox_that_will_not_sign_in_is_hung_up_on(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection is closed on the way out, or the account loses one of its five."""
    refused = FakeMailbox(settings, fails="[AUTHENTICATIONFAILED] Authentication failed")
    monkeypatch.setattr(mail, "Mailbox", lambda settings: refused)

    result = await checks._check_mail(settings)

    assert result.ok is False
    assert "Authentication failed" in result.detail
    assert refused.closed


# -- the calendar --------------------------------------------------------------------


async def test_the_calendar_probe_waits_for_one_to_be_picked(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefs, "_current", Prefs())

    result = await checks.check_calendar(settings)

    assert (result.ok, result.detail) == (False, "pick a calendar under Preferences")


async def test_the_calendar_probe_finds_the_picked_calendar_on_the_account(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefs, "_current", Prefs(icloud_calendar_url=CALENDAR_URL))
    monkeypatch.setattr(
        caldav,
        "CalendarClient",
        lambda settings: FakeCalendars(settings, Collection("Flights", CALENDAR_URL)),
    )

    result = await checks.check_calendar(settings)

    assert result.ok is True
    assert "Flights" in result.detail


async def test_a_calendar_deleted_in_the_calendar_app_is_named_as_gone(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writes go to a stored URL, so nothing else would ever notice it had gone."""
    monkeypatch.setattr(prefs, "_current", Prefs(icloud_calendar_url=CALENDAR_URL))
    monkeypatch.setattr(
        caldav,
        "CalendarClient",
        lambda settings: FakeCalendars(settings, Collection("Home", "https://example.invalid/h/")),
    )

    result = await checks.check_calendar(settings)

    assert result.ok is False
    assert "no longer on this account" in result.detail
    assert "Home" in result.detail


async def test_a_calendar_that_cannot_be_reached_is_reported_rather_than_raised(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefs, "_current", Prefs(icloud_calendar_url=CALENDAR_URL))
    monkeypatch.setattr(
        caldav, "CalendarClient", lambda settings: FakeCalendars(settings, fails="CalDAV said 503")
    )

    result = await checks.check_calendar(settings)

    assert (result.ok, result.detail) == (False, "CalDAV said 503")


# -- Pushover ------------------------------------------------------------------------


async def test_pushover_is_not_probed_until_there_are_keys(unconfigured: Settings) -> None:
    result = await checks._check_pushover(unconfigured)

    assert result.ok is False
    assert "Pushover token" in result.detail


async def test_the_pushover_probe_sends_a_real_push(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checks, "Notifier", FakeNotifier)

    result = await checks._check_pushover(settings)

    assert (result.ok, result.detail) == (True, "test push sent; check your phone")


async def test_a_refused_push_carries_pushovers_own_reason(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which key is wrong is the whole question, and only Pushover's body answers it."""
    monkeypatch.setattr(
        checks, "Notifier", lambda settings: FakeNotifier(settings, refuses="user key is invalid")
    )

    result = await checks._check_pushover(settings)

    assert (result.ok, result.detail) == (False, "user key is invalid")


# -- all five ------------------------------------------------------------------------


async def test_every_dependency_is_named_once_whatever_it_answered(
    settings: Settings, database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_one_airport(database)
    monkeypatch.setattr(prefs, "_current", Prefs(icloud_calendar_url=CALENDAR_URL))
    monkeypatch.setattr(
        checks,
        "shared_client",
        lambda: aeroapi(settings, lambda request: httpx.Response(200, json={"flights": []})),
    )
    monkeypatch.setattr(mail, "Mailbox", FakeMailbox)
    monkeypatch.setattr(
        caldav,
        "CalendarClient",
        lambda settings: FakeCalendars(settings, Collection("Flights", CALENDAR_URL)),
    )
    monkeypatch.setattr(checks, "Notifier", FakeNotifier)

    results = await run_checks(settings)

    assert [result.name for result in results] == [
        "database",
        "aeroapi",
        "mail",
        "calendar",
        "pushover",
    ]
    assert all(result.ok for result in results)
    assert one(results, "aeroapi").detail == "0 flights returned for UAL4"
