"""Phone pushes via ntfy: flight changes worth interrupting someone for, and budget alarms."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

import httpx

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

# ntfy takes its parameters as HTTP headers. Canonical spellings are X-Title, X-Priority,
# X-Tags and X-Click; the docs state parameter names are case-insensitive as headers and
# must be lowercase only as query params, so the canonical form is used throughout.
# https://docs.ntfy.sh/publish/#list-of-all-parameters
TITLE_HEADER = "X-Title"
PRIORITY_HEADER = "X-Priority"
TAGS_HEADER = "X-Tags"
CLICK_HEADER = "X-Click"

# The default server refuses a body over 4KB outright; better a truncated push than none.
MAX_MESSAGE_LENGTH = 4000

PRIORITY_HIGH = "high"
PRIORITY_DEFAULT = "default"

# Only a change that costs you the flight if you miss it gets to break through.
_HIGH_PRIORITY_KINDS = frozenset({CANCELLED, DIVERTED, GATE_CHANGED})

_TAGS = {
    GATE_ASSIGNED: "door",
    GATE_CHANGED: "warning,door",
    TERMINAL_CHANGED: "warning,office",
    DEPARTURE_DELAYED: "hourglass",
    DEPARTURE_MOVED_EARLIER: "fast_forward",
    ARRIVAL_TIME_CHANGED: "clock3",
    DEPARTED: "flight_departure",
    LANDED: "flight_arrival",
    BAGGAGE_CLAIM_ASSIGNED: "luggage",
    CANCELLED: "x",
    DIVERTED: "warning",
}


def flight_label(booking: Booking) -> str:
    """`DL1234 JFK -> LAX`, the one string that identifies a flight to a human."""
    return (
        f"{booking.marketing_carrier}{booking.marketing_number} "
        f"{booking.origin_iata} -> {booking.dest_iata}"
    )


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
            event, dest_tz, verb="Arrival moved", fallback="Arrival time changed", now="now arriving"
        )
    if kind == DEPARTED:
        return _at("Departed", new, origin_tz)
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
    """Fire-and-forget pushes; a no-op until an ntfy topic is configured.

    Delivery is strictly best-effort: an ntfy outage must never stall or fail a poll, so
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
        priority = PRIORITY_HIGH if event.kind in _HIGH_PRIORITY_KINDS else PRIORITY_DEFAULT
        await self._send(
            title=flight_label(booking),
            message=event_message(event, origin_tz=origin_tz, dest_tz=dest_tz),
            priority=priority,
            tags=_TAGS.get(event.kind, "airplane"),
            click=f"{self._settings.public_base_url}/f/{booking.id}",
        )

    async def budget_tripped(self, spend: Decimal, cap: Decimal) -> None:
        """The AeroAPI breaker has latched: tracking is stale until someone raises the cap."""
        await self._send(
            title="AeroAPI budget reached",
            message=(
                f"Spent ${spend:.2f} of the ${cap:.2f} monthly cap. "
                "Flight polling is paused until the cap is raised or the month rolls over."
            ),
            priority=PRIORITY_HIGH,
            tags="money_with_wings",
            click=self._settings.public_base_url,
        )

    async def _send(
        self,
        *,
        title: str,
        message: str,
        priority: str,
        tags: str,
        click: str | None = None,
    ) -> None:
        settings = self._settings
        if not settings.ntfy_configured:
            return
        headers = {TITLE_HEADER: title, PRIORITY_HEADER: priority, TAGS_HEADER: tags}
        if click:
            headers[CLICK_HEADER] = click
        if settings.ntfy_token:
            headers["Authorization"] = f"Bearer {settings.ntfy_token}"
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=10) as client:
                response = await client.post(
                    f"{settings.ntfy_url}/{settings.ntfy_topic}",
                    content=message[:MAX_MESSAGE_LENGTH].encode(),
                    headers=headers,
                )
                response.raise_for_status()
        except Exception:
            log.warning("ntfy notification failed", exc_info=True)
