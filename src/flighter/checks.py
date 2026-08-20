"""One command that exercises every external dependency and names the broken one.

Worth its weight the first time a flight silently fails to appear: the answer is always
one of five things, and guessing which costs more than asking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import func, select

from .config import Settings
from .db import session_scope
from .models import Airport

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
        return CheckResult("aeroapi", False, "AEROAPI_KEY is not set")
    # A known-good ident on a carrier that always has flights in the window. This spends
    # one result set, which is the point: an unspent key proves nothing.
    url = f"{settings.aeroapi_base_url}/flights/UAL4"
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


async def _check_ntfy(settings: Settings) -> CheckResult:
    if not settings.ntfy_configured:
        return CheckResult("ntfy", False, "NTFY_TOPIC is not set")
    headers = {"Title": "Flight tracker", "Priority": "low", "Tags": "white_check_mark"}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{settings.ntfy_url}/{settings.ntfy_topic}",
                content=b"Checks ran and this arrived.",
                headers=headers,
            )
            response.raise_for_status()
    except Exception as exc:
        return CheckResult("ntfy", False, str(exc))
    return CheckResult("ntfy", True, "test push sent; check your phone")


async def _check_gmail(settings: Settings) -> CheckResult:
    if not settings.gmail_configured:
        return CheckResult("gmail", False, "GMAIL_* credentials are not set")
    try:
        from .gmail import profile

        address = await profile()
    except Exception as exc:
        return CheckResult("gmail", False, str(exc))
    return CheckResult("gmail", True, f"authenticated as {address}")


async def _check_calendar(settings: Settings) -> CheckResult:
    if not settings.gcal_configured:
        return CheckResult("calendar", False, "GCAL_* credentials are not set")
    try:
        from .gcal import CalendarClient

        summary = await CalendarClient(settings).describe_calendar()
    except Exception as exc:
        return CheckResult("calendar", False, str(exc))
    return CheckResult("calendar", True, f"writing to {summary}")


async def run_checks(settings: Settings) -> list[CheckResult]:
    return [
        await _check_database(),
        await _check_aeroapi(settings),
        await _check_gmail(settings),
        await _check_calendar(settings),
        await _check_ntfy(settings),
    ]
