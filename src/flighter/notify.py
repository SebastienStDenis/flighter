"""Phone pushes via Pushover: flight changes worth interrupting someone for, and budget alarms."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal

import httpx

from . import notices, prefs
from .bookings import flight_label
from .config import Settings
from .models import Booking, EventKind, FlightEvent, IngestOutcome
from .timezones import FALLBACK_TZ, format_local, parse_instant

log = logging.getLogger(__name__)

# One form-encoded POST, with the application token and the user key as ordinary fields.
# https://pushover.net/api
MESSAGES_URL = "https://api.pushover.net/1/messages.json"

TIMEOUT_SECONDS = 10

# An over-long field is rejected rather than trimmed at the far end, so every value is cut
# here first: a truncated push beats no push.
# https://pushover.net/api#limits
MAX_MESSAGE_LENGTH = 1024
MAX_TITLE_LENGTH = 250
MAX_URL_LENGTH = 512

# The scale runs -2 (no notification at all) to 2 (repeats until acknowledged).
#
# Priority 1 is the only one that sounds during the quiet hours a person has set, so it is
# reserved for the changes that cost you the flight if you sleep through them: a gate
# change, a cancellation, a diversion. Everything else - an assigned gate, a delay, a
# landing, a bag belt - is news you act on when you next look at the phone, so it goes out
# at 0 and respects quiet hours.
#
# Nothing here uses 2: emergency priority re-alerts every few minutes until the phone is
# unlocked and the notification acknowledged, and none of this is an outage page. Even a
# cancellation is read once and then acted on with the airline.
PRIORITY_HIGH = 1
PRIORITY_NORMAL = 0
PRIORITY_QUIET = -1

_HIGH_PRIORITY_KINDS = frozenset({EventKind.CANCELLED, EventKind.DIVERTED, EventKind.GATE_CHANGED})

# The lock screen shows the title above the message, so the title names the flight and
# the message says what happened, in as few words as still make a sentence.
OPEN_FLIGHT = "Open flight"
OPEN_APP = "Open flighter"

# What each import outcome is called on the lock screen, and the line under it.
_IMPORTED = {
    IngestOutcome.CREATED: ("Flight added", "{flights}"),
    IngestOutcome.DUPLICATE: ("Already tracked", "{flights}. Nothing added."),
}


class PushFailed(RuntimeError):
    """Pushover did not take the message; the text is its own reason where it gave one."""


def _minutes_between(old: str | None, new: str | None) -> int | None:
    start, end = parse_instant(old), parse_instant(new)
    if start is None or end is None:
        return None
    return round(abs((end - start).total_seconds()) / 60)


def _moved(event: FlightEvent, tz: str, *, verb: str, fallback: str, now: str) -> str:
    """`Delayed 35 min. Departs 19:15 EDT`.

    Degrades to whichever half survives when a value is missing, because format_local
    renders a missing time as a dash and that is not a sentence.
    """
    when = parse_instant(event.new_value)
    minutes = _minutes_between(event.old_value, event.new_value)
    head = fallback if minutes is None else f"{verb} {minutes} min"
    return head if when is None else f"{head}. {now} {format_local(when, tz)}"


def _at(verb: str, value: str | None, tz: str) -> str:
    instant = parse_instant(value)
    return f"{verb} {format_local(instant, tz)}" if instant else verb


def event_message(event: FlightEvent, *, origin_tz: str, dest_tz: str) -> str:
    """One plain line a person can act on without opening anything."""
    kind, old, new = event.kind, event.old_value, event.new_value

    if kind == EventKind.GATE_ASSIGNED:
        return f"Gate {new}"
    if kind == EventKind.GATE_CHANGED:
        return f"Gate changed from {old} to {new}"
    if kind == EventKind.TERMINAL_CHANGED:
        return f"Terminal changed from {old} to {new}" if old else f"Terminal {new}"
    if kind == EventKind.DEPARTURE_DELAYED:
        return _moved(event, origin_tz, verb="Delayed", fallback="Delayed", now="Departs")
    if kind == EventKind.DEPARTURE_MOVED_EARLIER:
        return _moved(event, origin_tz, verb="Moved up", fallback="Moved up", now="Departs")
    if kind == EventKind.ARRIVAL_TIME_CHANGED:
        return _moved(
            event, dest_tz, verb="Arrival moved", fallback="Arrival time changed", now="Arrives"
        )
    if kind == EventKind.DEPARTED:
        return _at("Departed", new, origin_tz)
    if kind == EventKind.LANDED:
        return _at("Landed", new, dest_tz)
    if kind == EventKind.BAGGAGE_CLAIM_ASSIGNED:
        return f"Baggage claim {new}"
    if kind == EventKind.CANCELLED:
        return "Cancelled"
    if kind == EventKind.DIVERTED:
        return f"Diverted to {new}" if new and new != "true" else "Diverted"
    return kind


_http: httpx.AsyncClient | None = None


def shared_http() -> httpx.AsyncClient:
    """One connection pool for the process; `close_client` is wired into shutdown."""
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    return _http


async def close_client() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
    _http = None


class Notifier:
    """Pushes to the phone; a no-op until Pushover credentials are set.

    A send that Pushover refuses or that never reaches it raises `PushFailed`, and the
    caller decides what a failure means: the event dispatcher leaves the event pending
    and tries again next pass, the budget breaker logs and carries on, and the import
    pipeline, which has no retry of its own, logs and lets the email count as handled.
    """

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._http = (
            httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS)
            if transport is not None
            else None
        )

    async def flight_event(
        self,
        booking: Booking,
        event: FlightEvent,
        *,
        origin_tz: str = FALLBACK_TZ,
        dest_tz: str = FALLBACK_TZ,
    ) -> None:
        if booking.friend_name and not prefs.current().notify_for_friend_flights:
            return
        priority = PRIORITY_HIGH if event.kind in _HIGH_PRIORITY_KINDS else PRIORITY_NORMAL
        await self._send(
            title=flight_label(booking),
            message=event_message(event, origin_tz=origin_tz, dest_tz=dest_tz),
            priority=priority,
            url=f"{prefs.public_base_url()}/f/{booking.id}",
            url_title=OPEN_FLIGHT,
        )

    async def mail_imported(self, bookings: Sequence[Booking], *, outcome: str) -> None:
        """A marked email became flights.

        Priority 0: knowing the import worked is worth a glance at the phone, never worth
        waking somebody up, and the flight page is the link because it carries the live
        gate and status. iCloud publishes no web address for a single calendar event, so
        there is nothing to link to on that side even when one has been written.

        Best effort: the ingest log is the record of the decision and is written whether
        or not the phone heard about it, and there is no column to retry a push from.
        """
        title, body = _IMPORTED[IngestOutcome(outcome)]
        flights = ", ".join(flight_label(booking) for booking in bookings)
        try:
            await self._send(
                title=title,
                message=body.format(flights=flights or "The flight"),
                priority=PRIORITY_NORMAL,
                url=self._flight_url(bookings),
                url_title=OPEN_FLIGHT,
            )
        except PushFailed:
            log.warning("push about an imported email failed", exc_info=True)

    async def mail_failed(self, *, message_id: str, subject: str, reason: str | None) -> None:
        """Nothing came of an email that was marked.

        Priority 0 as well: an import that did not happen costs nobody a flight. The link
        opens the email page, which is where the email can be tried again, written off,
        or opened in Mail. Best effort, for the same reason as above.

        The words come from `notices` so that the push and that page say the same thing.
        """
        notice = notices.import_failed(subject=subject, reason=reason)
        try:
            await self._send(
                title=notice.headline,
                message=notice.body,
                priority=PRIORITY_NORMAL,
                url=f"{prefs.public_base_url()}/mail",
                url_title=OPEN_APP,
            )
        except PushFailed:
            log.warning("push about a failed import of %s failed", message_id, exc_info=True)

    async def budget_tripped(self, spend: Decimal, cap: Decimal) -> None:
        """The AeroAPI breaker has latched: tracking is stale until someone raises the cap."""
        await self._send(
            title="AeroAPI budget reached",
            message=(
                f"${spend:.2f} of the ${cap:.2f} monthly cap spent. "
                "Updates are paused until the cap is raised or the month ends."
            ),
            priority=PRIORITY_HIGH,
            url=prefs.public_base_url(),
            url_title=OPEN_APP,
        )

    async def check(self) -> None:
        """A real push, quietly, because a token that is never spent proves nothing."""
        await self._send(title="flighter", message="Test notification", priority=PRIORITY_QUIET)

    @staticmethod
    def _flight_url(bookings: Sequence[Booking]) -> str:
        base = prefs.public_base_url()
        return f"{base}/f/{bookings[0].id}" if bookings else base

    async def _send(
        self,
        *,
        title: str,
        message: str,
        priority: int,
        url: str | None = None,
        url_title: str | None = None,
    ) -> None:
        settings = self._settings
        if not settings.pushover_configured:
            return
        data = {
            "token": settings.pushover_token,
            "user": settings.pushover_user_key,
            "title": title[:MAX_TITLE_LENGTH],
            "message": message[:MAX_MESSAGE_LENGTH],
            "priority": str(priority),
        }
        if url:
            data["url"] = url[:MAX_URL_LENGTH]
            if url_title:
                data["url_title"] = url_title
        try:
            response = await (self._http or shared_http()).post(MESSAGES_URL, data=data)
        except httpx.HTTPError as exc:
            raise PushFailed(f"Pushover is unreachable: {exc}") from exc
        _accepted(response)


def _accepted(response: httpx.Response) -> None:
    """A 200 only says the request parsed; the body says whether the message was taken.

    A rejection names the field it refused, which is the difference between a bad
    application token and a bad user key, so that is what the error carries.
    """
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict) and body.get("status") == 1:
        return
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors:
        raise PushFailed("; ".join(str(error) for error in errors))
    raise PushFailed(f"Pushover answered HTTP {response.status_code}: {response.text[:200]}")
