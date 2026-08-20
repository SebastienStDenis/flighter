"""One command that exercises every external dependency and names the broken one.

Worth its weight the first time a flight silently fails to appear: the answer is always
one of five things, and guessing which costs more than asking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import func, select

from . import prefs
from .aeroapi import BudgetExceeded, shared_client
from .config import Settings
from .db import session_scope
from .models import Airport
from .notify import Notifier

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


async def _check_database() -> CheckResult:
    try:
        async with session_scope() as session:
            airports = await session.scalar(select(func.count()).select_from(Airport))
    except Exception as exc:
        return CheckResult("database", False, str(exc))
    if not airports:
        return CheckResult("database", False, "reachable but no airports seeded")
    return CheckResult("database", True, f"{airports} airports seeded")


async def _check_aeroapi(settings: Settings) -> CheckResult:
    """Spends one result set on a known-good ident, through the same meter as a poll.

    An unspent key proves nothing, and a check that bypassed the breaker would be the
    one call path able to run the bill past the cap.
    """
    if not settings.aeroapi_configured:
        return CheckResult("aeroapi", False, "add a FlightAware key under Connections")
    try:
        payload = await shared_client().flight_info("UAL4", ident_type="designator")
    except BudgetExceeded as exc:
        return CheckResult("aeroapi", False, str(exc))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return CheckResult("aeroapi", False, "key rejected")
        return CheckResult(
            "aeroapi",
            False,
            f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
        )
    except Exception as exc:
        return CheckResult("aeroapi", False, str(exc))
    flights = payload.get("flights") or []
    return CheckResult("aeroapi", True, f"{len(flights)} flights returned for UAL4")


async def _check_pushover(settings: Settings) -> CheckResult:
    if not settings.pushover_configured:
        return CheckResult("pushover", False, "add a Pushover token and user key under Connections")
    try:
        await Notifier(settings).check()
    except Exception as exc:
        return CheckResult("pushover", False, str(exc))
    return CheckResult("pushover", True, "test push sent; check your phone")


async def _check_mail(settings: Settings) -> CheckResult:
    """Logs in and counts what carries the flag, across every mailbox the sweep looks in.

    That is the whole sweep short of doing the work, so it answers both questions at
    once: can the flag be seen at all, and is anything sitting there unimported.
    """
    if not settings.icloud_configured:
        return CheckResult(
            "mail", False, "add an Apple ID and app-specific password under Connections"
        )
    from .mail import Mailbox

    mailbox = Mailbox(settings)
    try:
        await mailbox.connect()
        waiting = await mailbox.count_flagged()
    except Exception as exc:
        return CheckResult("mail", False, str(exc))
    finally:
        await mailbox.close()
    return CheckResult(
        "mail",
        True,
        f"{waiting} message(s) flagged {mailbox.colour} across "
        f"{len(mailbox.mailboxes)} mailbox(es)",
    )


async def _check_calendar(settings: Settings) -> CheckResult:
    """Signs in over CalDAV and lists the account's calendars, then looks for ours in it.

    That is the one thing a sync cannot tell you on its own: writes go straight to a
    stored URL, so a calendar deleted in the Calendar app is a 404 on the next flight
    rather than something anybody was told about.
    """
    from .caldav import CalendarClient

    chosen = prefs.current().icloud_calendar_url
    if not chosen:
        return CheckResult("calendar", False, "pick a calendar under Connections")
    try:
        offered = await CalendarClient(settings).calendars()
    except Exception as exc:
        return CheckResult("calendar", False, str(exc))
    found = next((collection for collection in offered if collection.url == chosen), None)
    if found is None:
        return CheckResult(
            "calendar",
            False,
            f"the calendar that was picked is no longer on this account. It offers: "
            f"{', '.join(sorted(collection.name for collection in offered)) or 'nothing'}.",
        )
    return CheckResult("calendar", True, f"writing to {found.name} at {found.url}")


async def run_checks(settings: Settings) -> list[CheckResult]:
    return [
        await _check_database(),
        await _check_aeroapi(settings),
        await _check_mail(settings),
        await _check_calendar(settings),
        await _check_pushover(settings),
    ]
