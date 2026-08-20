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
from .aeroapi import BASE_URL
from .config import Settings
from .db import session_scope
from .models import Airport
from .notify import MESSAGES_URL, PRIORITY_QUIET

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


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
    if not settings.aeroapi_configured:
        return CheckResult("aeroapi", False, "AEROAPI_KEY is not set in .env")
    # A known-good ident on a carrier that always has flights in the window. This spends
    # one result set, which is the point: an unspent key proves nothing.
    url = f"{BASE_URL}/flights/UAL4"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(
                url,
                headers={"x-apikey": settings.aeroapi_key},
                params={"max_pages": 1, "ident_type": "designator"},
            )
    except Exception as exc:
        return CheckResult("aeroapi", False, str(exc))
    if response.status_code == 401:
        return CheckResult("aeroapi", False, "key rejected")
    if response.status_code != 200:
        return CheckResult("aeroapi", False, f"HTTP {response.status_code}: {response.text[:200]}")
    flights = response.json().get("flights", [])
    return CheckResult("aeroapi", True, f"{len(flights)} flights returned for UAL4")


async def _check_pushover(settings: Settings) -> CheckResult:
    """Sends a real push, quietly, because a token that is never spent proves nothing."""
    if not settings.pushover_configured:
        return CheckResult(
            "pushover", False, "PUSHOVER_TOKEN and PUSHOVER_USER_KEY are not set in .env"
        )
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                MESSAGES_URL,
                data={
                    "token": settings.pushover_token,
                    "user": settings.pushover_user_key,
                    "title": "Flight tracker",
                    "message": "Checks ran and this arrived.",
                    "priority": str(PRIORITY_QUIET),
                },
            )
    except Exception as exc:
        return CheckResult("pushover", False, str(exc))
    # A rejection carries a JSON body naming the field it refused, which is the difference
    # between a bad application token and a bad user key.
    try:
        body = response.json()
    except ValueError:
        return CheckResult("pushover", False, f"HTTP {response.status_code}: {response.text[:200]}")
    if body.get("status") != 1:
        errors = "; ".join(body.get("errors", [])) or f"HTTP {response.status_code}"
        return CheckResult("pushover", False, errors)
    return CheckResult("pushover", True, "test push sent; check your phone")


async def _check_mail(settings: Settings) -> CheckResult:
    """Logs in and counts what carries the flag, across every mailbox the sweep looks in.

    That is the whole sweep short of doing the work, so it answers both questions at
    once: can the flag be seen at all, and is anything sitting there unimported.
    """
    if not settings.icloud_configured:
        return CheckResult(
            "mail", False, "ICLOUD_EMAIL and ICLOUD_APP_PASSWORD are not set in .env"
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
    """Signs in over CalDAV and walks discovery, which is everything a sync does but the PUT."""
    if not settings.icloud_configured:
        return CheckResult(
            "calendar", False, "ICLOUD_EMAIL and ICLOUD_APP_PASSWORD are not set in .env"
        )
    name = prefs.current().icloud_calendar_name
    if not name:
        return CheckResult("calendar", False, "no calendar named on the settings page")
    try:
        from .caldav import CalendarClient

        url = await CalendarClient(settings).describe_calendar()
    except Exception as exc:
        return CheckResult("calendar", False, str(exc))
    return CheckResult("calendar", True, f"writing to {name} at {url}")


async def run_checks(settings: Settings) -> list[CheckResult]:
    return [
        await _check_database(),
        await _check_aeroapi(settings),
        await _check_mail(settings),
        await _check_calendar(settings),
        await _check_pushover(settings),
    ]
