"""Google Calendar: one event per booking, patched in place as the flight moves.

The calendar is a mirror, never a log. Every sync restates the whole flight from the
newest snapshot, and a cancellation is patched to `status: cancelled` rather than
deleted, so the entry stays where the trip was planned and the history survives.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import inspect as sa_inspect

from .config import Settings
from .models import Airport, Booking, FlightSnapshot
from .notify import flight_label
from .timezones import FALLBACK_TZ, to_local

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Long enough to still be at home when it fires for an airport run.
REMINDER_MINUTES = 180

# Only used when a booking carries no arrival time at all and no snapshot has landed yet;
# Google demands an end, and a wrong-length block is better than no calendar entry.
ASSUMED_DURATION = timedelta(hours=2)

# Google's own rule: "A time zone offset is required unless a time zone is explicitly
# specified in timeZone." Naive local wall time plus the IANA name is therefore the only
# spelling that neither pins the wrong instant nor floats.
_GONE = (404, 410)


class CredentialsExpired(RuntimeError):
    """The refresh token is dead and only a human can fix it."""


def load_credentials(settings: Settings) -> Credentials:
    """Credentials from the stored refresh token alone; the library mints the access
    token on first use."""
    return Credentials(
        token=None,
        refresh_token=settings.gcal_refresh_token,
        client_id=settings.gcal_client_id,
        client_secret=settings.gcal_client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )


def _zone(airports: Mapping[str, Airport], iata: str) -> str:
    airport = airports.get(iata)
    return airport.tz if airport else FALLBACK_TZ


def _wall_clock(instant: datetime, tz: str) -> str:
    return to_local(instant, tz).replace(tzinfo=None).isoformat()


def _first(*values: datetime | None) -> datetime | None:
    return next((value for value in values if value is not None), None)


def _location(airports: Mapping[str, Airport], iata: str) -> str:
    """The airport as a postal-ish string, because Google geocodes a name and city and
    drops a pin on an IATA code."""
    airport = airports.get(iata)
    if airport is None:
        return iata
    parts = [airport.name, airport.city, airport.country]
    return ", ".join(part for part in parts if part)


def _passenger_name(booking: Booking) -> str | None:
    """Never triggers a lazy load, which would explode under asyncio; the caller is
    expected to have eager-loaded the relationship."""
    if "passenger" in sa_inspect(booking).unloaded:
        return None
    passenger = booking.passenger
    return passenger.display_name if passenger else None


def event_body(
    booking: Booking,
    snapshot: FlightSnapshot | None,
    airports: Mapping[str, Airport],
    *,
    base_url: str,
) -> dict[str, Any]:
    """Render the current belief about one flight as a Google Calendar event resource."""
    origin_tz = _zone(airports, booking.origin_iata)
    dest_tz = _zone(airports, booking.dest_iata)
    cancelled = bool(snapshot and snapshot.cancelled) or booking.status == "cancelled"

    start = _first(
        snapshot.actual_out if snapshot else None,
        snapshot.estimated_out if snapshot else None,
        snapshot.scheduled_out if snapshot else None,
        booking.scheduled_departure_utc,
    )
    assert start is not None
    end = _first(
        snapshot.actual_in if snapshot else None,
        snapshot.estimated_in if snapshot else None,
        snapshot.scheduled_in if snapshot else None,
        booking.scheduled_arrival_utc,
    )
    if end is None or end <= start:
        end = start + ASSUMED_DURATION

    label = flight_label(booking)
    body: dict[str, Any] = {
        "summary": f"CANCELLED - {label}" if cancelled else label,
        "location": _location(airports, booking.origin_iata),
        "start": {"dateTime": _wall_clock(start, origin_tz), "timeZone": origin_tz},
        "end": {"dateTime": _wall_clock(end, dest_tz), "timeZone": dest_tz},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": REMINDER_MINUTES}],
        },
    }

    lines = []
    passenger = _passenger_name(booking)
    if passenger:
        lines.append(f"Passenger: {passenger}")
    if booking.confirmation_code:
        lines.append(f"Confirmation: {booking.confirmation_code}")
    if booking.seat:
        lines.append(f"Seat: {booking.seat}")
    gate = snapshot.gate_origin if snapshot else None
    terminal = snapshot.terminal_origin if snapshot else None
    if gate and terminal:
        lines.append(f"Gate: {gate} (Terminal {terminal})")
    elif gate:
        lines.append(f"Gate: {gate}")
    elif terminal:
        lines.append(f"Terminal: {terminal}")
    if snapshot and snapshot.baggage_claim:
        lines.append(f"Baggage claim: {snapshot.baggage_claim}")
    lines.extend(["", f"{base_url}/f/{booking.id}"])
    body["description"] = "\n".join(lines)

    if cancelled:
        body["status"] = "cancelled"

    return body


class CalendarClient:
    """Best-effort calendar mirroring; a no-op that logs until Google is configured.

    The Google client is blocking and does its own HTTP, so every call crosses into a
    worker thread rather than parking the event loop for the length of a round trip.
    """

    def __init__(
        self,
        settings: Settings,
        airports: Mapping[str, Airport] | None = None,
        service: Any | None = None,
    ) -> None:
        self._settings = settings
        self._airports = airports if airports is not None else {}
        self._service = service

    async def upsert(self, booking: Booking, snapshot: FlightSnapshot | None) -> str | None:
        """Create or update this booking's event; returns the id to store on the booking."""
        if not self._settings.gcal_configured:
            log.debug("Google Calendar is not configured; skipping booking %s", booking.id)
            return None
        body = event_body(
            booking, snapshot, self._airports, base_url=self._settings.public_base_url
        )
        return await asyncio.to_thread(self._upsert_blocking, booking.gcal_event_id, body)

    async def delete(self, booking: Booking) -> None:
        if not self._settings.gcal_configured or not booking.gcal_event_id:
            return
        await asyncio.to_thread(self._delete_blocking, booking.gcal_event_id)

    async def describe_calendar(self) -> str:
        """The configured calendar's human name.

        Unlike the rest of the class this raises, because it exists for the `check`
        command whose whole job is to put the failure text in front of a person.
        """
        if not self._settings.gcal_configured:
            raise CredentialsExpired("Google Calendar is not configured")
        return await asyncio.to_thread(self._describe_blocking)

    def _client(self) -> Any:
        """Built on first use, so an unconfigured or offline deployment still starts."""
        if self._service is None:
            self._service = build(
                "calendar",
                "v3",
                credentials=load_credentials(self._settings),
                cache_discovery=False,
            )
        return self._service

    def _execute(self, request: Any) -> dict[str, Any]:
        try:
            result: dict[str, Any] = request.execute()
        except RefreshError as exc:
            raise CredentialsExpired(
                "Google refused the refresh token. If the OAuth app is still in 'Testing' "
                "publishing status its tokens expire after 7 days: set it to 'In production' "
                "and re-authorise."
            ) from exc
        return result

    def _upsert_blocking(self, event_id: str | None, body: dict[str, Any]) -> str:
        calendar_id = self._settings.gcal_calendar_id
        events = self._client().events()
        if event_id:
            try:
                patched = self._execute(
                    events.patch(calendarId=calendar_id, eventId=event_id, body=body)
                )
                return str(patched["id"])
            except HttpError as exc:
                if exc.resp.status not in _GONE:
                    raise
                # Someone deleted it by hand; re-create rather than losing the trip.
                log.info("calendar event %s is gone, inserting a replacement", event_id)
        created = self._execute(events.insert(calendarId=calendar_id, body=body))
        return str(created["id"])

    def _delete_blocking(self, event_id: str) -> None:
        try:
            self._execute(
                self._client()
                .events()
                .delete(calendarId=self._settings.gcal_calendar_id, eventId=event_id)
            )
        except HttpError as exc:
            if exc.resp.status not in _GONE:
                raise
            log.info("calendar event %s was already gone", event_id)

    def _describe_blocking(self) -> str:
        calendar_id = self._settings.gcal_calendar_id
        calendar = self._execute(self._client().calendars().get(calendarId=calendar_id))
        summary = calendar.get("summary")
        return str(summary) if summary else calendar_id
