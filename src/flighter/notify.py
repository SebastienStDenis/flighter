"""Phone pushes via Pushover: flight changes worth interrupting someone for, and budget alarms."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote

import httpx

from . import prefs
from .config import Settings
from .events import (
    ARRIVAL_TIME_CHANGED,
    BAGGAGE_CLAIM_ASSIGNED,
    CANCELLED,
    DEPARTED,
    DEPARTURE_DELAYED,
    DEPARTURE_MOVED_EARLIER,
    DIVERTED,
    GATE_ASSIGNED,
    GATE_CHANGED,
    LANDED,
    TERMINAL_CHANGED,
)
from .models import Booking, FlightEvent
from .timezones import FALLBACK_TZ, format_local

log = logging.getLogger(__name__)

# One form-encoded POST, with the application token and the user key as ordinary fields.
# https://pushover.net/api
MESSAGES_URL = "https://api.pushover.net/1/messages.json"

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

_HIGH_PRIORITY_KINDS = frozenset({CANCELLED, DIVERTED, GATE_CHANGED})

# The lock screen shows the title above the message, so each kind leads with one glyph
# that says what happened before a word of it is read.
_EMOJI = {
    GATE_ASSIGNED: "🚪",
    GATE_CHANGED: "⚠️",
    TERMINAL_CHANGED: "⚠️",
    DEPARTURE_DELAYED: "⏳",
    DEPARTURE_MOVED_EARLIER: "⏩",
    ARRIVAL_TIME_CHANGED: "🕒",
    DEPARTED: "🛫",
    LANDED: "🛬",
    BAGGAGE_CLAIM_ASSIGNED: "🧳",
    CANCELLED: "❌",
    DIVERTED: "⚠️",
}
DEFAULT_EMOJI = "✈️"
BUDGET_EMOJI = "💸"
IMPORT_FAILED_EMOJI = "📭"

# What each import outcome is called on the lock screen, and the sentence under it.
_IMPORTED = {
    "created": ("Flight added", "{flights}"),
    "review": ("Flight needs a look", "{flights} - the extraction was not confident enough."),
    "duplicate": ("Already tracked", "{flights} was already on the list; nothing was added."),
}


def flight_label(booking: Booking) -> str:
    """`DL1234 JFK -> LAX`, the one string that identifies a flight to a human."""
    return (
        f"{booking.marketing_carrier}{booking.marketing_number} "
        f"{booking.origin_iata} -> {booking.dest_iata}"
    )


def message_url(message_id: str) -> str:
    """A link that opens the email itself, in Mail, on the phone and on the Mac.

    The angle brackets around a Message-ID have to be percent-encoded or Mail ignores the
    URL entirely; what sits between them is left as it stands.
    """
    return f"message://%3C{quote(message_id.strip().strip('<>'), safe='@')}%3E"


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _minutes_between(old: str | None, new: str | None) -> int | None:
    start, end = _parse(old), _parse(new)
    if start is None or end is None:
        return None
    return round(abs((end - start).total_seconds()) / 60)


def _moved(event: FlightEvent, tz: str, *, verb: str, fallback: str, now: str) -> str:
    """`Delayed 35 min, now departing 19:15 EDT`.

    Degrades to whichever half survives when a value is missing, because format_local
    renders a missing time as a dash and that is not a sentence.
    """
    when = _parse(event.new_value)
    minutes = _minutes_between(event.old_value, event.new_value)
    head = fallback if minutes is None else f"{verb} {minutes} min"
    return head if when is None else f"{head}, {now} {format_local(when, tz)}"


def _at(verb: str, value: str | None, tz: str) -> str:
    instant = _parse(value)
    return f"{verb} at {format_local(instant, tz)}" if instant else verb


def event_message(event: FlightEvent, *, origin_tz: str, dest_tz: str) -> str:
    """One plain sentence a person can act on without opening anything."""
    kind, old, new = event.kind, event.old_value, event.new_value

    if kind == GATE_ASSIGNED:
        return f"Gate {new}"
    if kind == GATE_CHANGED:
        return f"Gate changed from {old} to {new}"
    if kind == TERMINAL_CHANGED:
        return f"Terminal changed from {old} to {new}" if old else f"Terminal {new}"
    if kind == DEPARTURE_DELAYED:
        return _moved(
            event, origin_tz, verb="Delayed", fallback="Departure delayed", now="now departing"
        )
    if kind == DEPARTURE_MOVED_EARLIER:
        return _moved(
            event, origin_tz, verb="Moved up", fallback="Departure moved up", now="now departing"
        )
    if kind == ARRIVAL_TIME_CHANGED:
        return _moved(
            event,
            dest_tz,
            verb="Arrival moved",
            fallback="Arrival time changed",
            now="now arriving",
        )
    if kind == DEPARTED:
        return _at("Left the gate", new, origin_tz)
    if kind == LANDED:
        return _at("Landed", new, dest_tz)
    if kind == BAGGAGE_CLAIM_ASSIGNED:
        return f"Bag claim: {new}"
    if kind == CANCELLED:
        # AeroAPI's `cancelled` only means FlightAware stopped tracking, so the copy
        # reports who said it rather than asserting the flight is off.
        return "Marked cancelled by FlightAware - confirm with the airline"
    if kind == DIVERTED:
        return "Flight diverted"
    return kind


class Notifier:
    """Fire-and-forget pushes; a no-op until Pushover credentials are set.

    Delivery is strictly best-effort: a Pushover outage must never stall or fail a poll, so
    send failures are logged and swallowed. The caller decides when a send counts as
    delivered, and only a clean return says so.
    """

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def flight_event(
        self,
        booking: Booking,
        event: FlightEvent,
        *,
        origin_tz: str = FALLBACK_TZ,
        dest_tz: str = FALLBACK_TZ,
    ) -> None:
        priority = PRIORITY_HIGH if event.kind in _HIGH_PRIORITY_KINDS else PRIORITY_NORMAL
        emoji = _EMOJI.get(event.kind, DEFAULT_EMOJI)
        await self._send(
            title=f"{emoji} {flight_label(booking)}",
            message=event_message(event, origin_tz=origin_tz, dest_tz=dest_tz),
            priority=priority,
            url=f"{prefs.current().public_base_url}/f/{booking.id}",
            url_title="Open the flight",
        )

    async def mail_imported(self, bookings: Sequence[Booking], *, outcome: str) -> None:
        """A marked email became flights.

        Priority 0: knowing the import worked is worth a glance at the phone, never worth
        waking somebody up, and the flight page is the link because it carries the live
        gate and status. iCloud publishes no web address for a single calendar event, so
        there is nothing to link to on that side even when one has been written.
        """
        title, body = _IMPORTED[outcome]
        flights = ", ".join(flight_label(booking) for booking in bookings)
        await self._send(
            title=f"{DEFAULT_EMOJI} {title}",
            message=body.format(flights=flights or "The flight"),
            priority=PRIORITY_NORMAL,
            url=self._flight_url(bookings),
            url_title="Open the flight",
        )

    async def mail_failed(self, *, message_id: str, subject: str, reason: str) -> None:
        """Nothing came of an email that was marked.

        Priority 0 as well: an import that did not happen costs nobody a flight. The link
        opens the email itself in Mail, on the phone or on the Mac, which is where the
        next move is made either way.
        """
        await self._send(
            title=f"{IMPORT_FAILED_EMOJI} Nothing imported",
            message=f"{subject}\n{reason}" if subject else reason,
            priority=PRIORITY_NORMAL,
            url=message_url(message_id),
            url_title="Open the email",
        )

    async def budget_tripped(self, spend: Decimal, cap: Decimal) -> None:
        """The AeroAPI breaker has latched: tracking is stale until someone raises the cap."""
        await self._send(
            title=f"{BUDGET_EMOJI} AeroAPI budget reached",
            message=(
                f"Spent ${spend:.2f} of the ${cap:.2f} monthly cap. "
                "Flight polling is paused until the cap is raised or the month rolls over."
            ),
            priority=PRIORITY_HIGH,
            url=prefs.current().public_base_url,
            url_title="Open flighter",
        )

    @staticmethod
    def _flight_url(bookings: Sequence[Booking]) -> str:
        base = prefs.current().public_base_url
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
            async with httpx.AsyncClient(transport=self._transport, timeout=10) as client:
                response = await client.post(MESSAGES_URL, data=data)
                response.raise_for_status()
        except Exception:
            log.warning("Pushover notification failed", exc_info=True)
