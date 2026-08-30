"""iCloud Calendar over CalDAV: one event per booking, restated in place as it moves.

The calendar is a mirror, never a log. Every sync rewrites the whole flight from the
newest snapshot, and a flight FlightAware has stopped tracking is marked tentative and
says so rather than being deleted, so the entry stays where the trip was planned and
the history survives.

Nothing here hard-codes a collection URL. iCloud serves each account from its own cluster
under a numeric principal id, so the account's calendars are found the way RFC 4791 says
to find them: ask the root who the current user is, ask that principal where its
calendars live, then list them. That is what fills the picker on the settings page, and
the URL picked there is what every write goes to afterwards - so the sync itself is one
request, and renaming the calendar in the Calendar app does not break it.

Authentication is HTTP Basic with the Apple ID and the same app-specific password the
mailbox uses; iCloud refuses the account password on CalDAV as flatly as it does on IMAP.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from urllib.parse import quote, urljoin

import httpx
from icalendar import Alarm, Calendar, Event, Timezone

from . import prefs
from .bookings import flight_label, operated_note
from .config import Settings
from .mail import message_url
from .models import Airport, Booking, FlightSnapshot
from .phase import arrival_estimate, departure_estimate
from .timezones import FALLBACK_TZ, to_local

log = logging.getLogger(__name__)

# The one fixed address in the protocol. Everything past it is discovered.
CALDAV_ROOT = "https://caldav.icloud.com"

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"

TIMEOUT_SECONDS = 30

# Discovery runs while somebody is waiting on a page, so it gives up long before a write
# would: a settings page that says iCloud is unreachable beats one that hangs.
DISCOVERY_TIMEOUT_SECONDS = 10

# Apple counts time from 2001-01-01 UTC, which is the instant a `calshow:` link carries.
APPLE_EPOCH_OFFSET = 978307200

# Long enough to still be at home when it fires for an airport run.
REMINDER_MINUTES = 180

# Only used when a booking carries no arrival time at all and no snapshot has landed yet;
# a VEVENT needs an end, and a wrong-length block is better than no calendar entry.
ASSUMED_DURATION = timedelta(hours=2)

# A VTIMEZONE is a table of transitions, so it is generated for a window rather than for
# all time. A day either side of the flight covers every instant the event names.
TIMEZONE_MARGIN = timedelta(days=1)

PRODID = "-//flighter//flight tracker//EN"

_XML_HEADERS = {"Content-Type": 'application/xml; charset="utf-8"'}

_PROPFIND_PRINCIPAL = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)

_PROPFIND_HOME = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><c:calendar-home-set/></d:prop></d:propfind>"
)

_PROPFIND_CALENDARS = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:displayname/><d:resourcetype/>"
    "<c:supported-calendar-component-set/></d:prop></d:propfind>"
)


class CalendarUnavailable(RuntimeError):
    """The calendar cannot be reached or cannot be found, and only a human can fix it."""


class Collection(NamedTuple):
    """One calendar the account offers: what it is called, and where it lives."""

    name: str
    url: str


def configured(settings: Settings) -> bool:
    """An Apple ID to sign in with, and a calendar of our own to write into."""
    return settings.icloud_configured and prefs.current().calendar_configured


def event_uid(booking: Booking) -> str:
    """The iCalendar UID for a booking, derived rather than remembered.

    Deriving it makes every write idempotent: a crash between the PUT and the commit
    replays as the same resource instead of leaving a second copy of the trip, and an
    entry deleted by hand comes back on the next sync rather than being lost.
    """
    return f"flighter-{booking.id}@flighter.invalid"


def calendar_link(departure: datetime, tz: str) -> str:
    """A link that opens the Calendar app on the day the flight leaves.

    Apple publishes no scheme for one event, only `calshow:`, which takes an instant -
    counted from Apple's own 2001 epoch - and renders it in whatever zone the phone is
    standing in. Aiming at noon at the departure airport keeps the date right even when
    the phone is most of a day away from it.
    """
    noon = to_local(departure, tz).replace(hour=12, minute=0, second=0, microsecond=0)
    return f"calshow:{int(noon.timestamp()) - APPLE_EPOCH_OFFSET}"


def macos_calendar_link(departure: datetime, tz: str) -> str:
    """Open macOS Calendar on the flight's departure day."""
    noon = to_local(departure, tz).replace(hour=12, minute=0, second=0, microsecond=0)
    return f"ical://date/{noon:%Y-%m-%d_%H-%M-%S}?method=show"


def _zone(airports: Mapping[str, Airport], iata: str) -> str:
    airport = airports.get(iata)
    return airport.tz if airport else FALLBACK_TZ


def _location(airports: Mapping[str, Airport], iata: str) -> str:
    """The airport as a postal-ish string, because a calendar geocodes a name and city
    and drops a pin on an IATA code."""
    airport = airports.get(iata)
    if airport is None:
        return iata
    parts = [airport.name, airport.city, airport.country]
    return ", ".join(part for part in parts if part)


def event_body(
    booking: Booking,
    snapshot: FlightSnapshot | None,
    airports: Mapping[str, Airport],
    *,
    base_url: str,
) -> Calendar:
    """Render the current belief about one flight as a calendar object resource."""
    origin_tz = _zone(airports, booking.origin_iata)
    dest_tz = _zone(airports, booking.dest_iata)
    cancelled = bool(snapshot and snapshot.cancelled)

    start = departure_estimate(booking, snapshot)
    end = arrival_estimate(booking, snapshot)
    if end is None or end <= start:
        end = start + ASSUMED_DURATION

    label = flight_label(booking)
    if booking.friend_name:
        label = f"{booking.friend_name} - {label}"
    event = Event()
    event.add("uid", event_uid(booking))
    event.add("dtstamp", datetime.now(UTC))
    event.add("summary", f"{label} (cancelled)" if cancelled else label)
    event.add("location", _location(airports, booking.origin_iata))
    # Wall clock plus the airport's IANA zone. An offset pins the wrong instant the day
    # the rules change and a bare local time floats to wherever the phone is standing;
    # naming the zone is the only spelling that does neither.
    event.add("dtstart", to_local(start, origin_tz))
    event.add("dtend", to_local(end, dest_tz))

    lines = []
    if cancelled:
        # Struck through rather than deleted: the trip stays visible where it was planned.
        event.add("status", "CANCELLED")
    operated = operated_note(booking.operating_carrier, booking.operating_number)
    if operated:
        lines.append(operated)
    for confirmation in booking.confirmations:
        named = f"Confirmation ({confirmation.name})" if confirmation.name else "Confirmation"
        lines.append(f"{named}: {confirmation.code}")
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
    if booking.source_message_id:
        # The confirmation itself, one tap from the calendar entry it became.
        lines.append(f"Email: {message_url(booking.source_message_id)}")
    event.add("description", "\n".join(lines))

    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", label)
    alarm.add("trigger", timedelta(minutes=-REMINDER_MINUTES))
    event.add_component(alarm)

    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    # RFC 4791 makes a VTIMEZONE mandatory for every TZID the object names, so the two
    # ends carry their own definitions - one of them when both are the same zone.
    for tzid in dict.fromkeys([origin_tz, dest_tz]):
        calendar.add_component(
            Timezone.from_tzid(
                tzid,
                first_date=(start - TIMEZONE_MARGIN).date(),
                last_date=(end + TIMEZONE_MARGIN).date(),
            )
        )
    calendar.add_component(event)
    return calendar


class CalendarClient:
    """Best-effort calendar mirroring; a no-op that logs until iCloud is configured.

    A sync writes straight to the collection URL held in the preferences, because that
    URL was resolved once on the settings page when the calendar was picked. Discovery
    happens only when somebody asks what the account offers.
    """

    def __init__(
        self,
        settings: Settings,
        airports: Mapping[str, Airport] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._airports = airports if airports is not None else {}
        self._http = httpx.AsyncClient(transport=transport) if transport is not None else None

    async def upsert(self, booking: Booking, snapshot: FlightSnapshot | None) -> str | None:
        """Create or update this booking's event; returns the uid to store on the booking."""
        if not configured(self._settings):
            log.debug("iCloud Calendar is not configured; skipping booking %s", booking.id)
            return None
        current = prefs.current()
        # Only the writing stops. Deleting stays open, so switching sync off can take the
        # entries it already wrote back out rather than stranding them.
        if not current.calendar_sync_enabled:
            log.debug("calendar sync is off; skipping booking %s", booking.id)
            return None
        if booking.friend_name and not current.sync_friend_flights_to_calendar:
            log.debug("friend calendar sync is off; skipping booking %s", booking.id)
            return None
        body = event_body(booking, snapshot, self._airports, base_url=prefs.public_base_url())
        uid = event_uid(booking)
        # No If-Match: this restates the whole flight from the newest snapshot, so there
        # is no edit of ours to lose and an entry someone changed by hand is meant to be
        # corrected back.
        response = await self._request(
            "PUT",
            _event_url(prefs.current().icloud_calendar_url, uid),
            content=body.to_ical(),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        _written(response)
        return uid

    async def delete(self, booking: Booking) -> bool:
        if not configured(self._settings) or not booking.calendar_event_uid:
            return False
        response = await self._request(
            "DELETE", _event_url(prefs.current().icloud_calendar_url, booking.calendar_event_uid)
        )
        if response.status_code == 404:
            log.info("calendar event %s was already gone", booking.calendar_event_uid)
            return True
        _written(response)
        return True

    async def calendars(self) -> list[Collection]:
        """Every calendar on the account that can hold events, in the order iCloud lists them.

        Unlike the rest of the class this raises, because both its callers - the settings
        page and the `check` command - exist to put the failure text in front of a person.
        """
        if not self._settings.icloud_configured:
            raise CalendarUnavailable("add an Apple ID and app-specific password under Connections")
        principal = await self._href(
            f"{CALDAV_ROOT}/", _PROPFIND_PRINCIPAL, f"{{{DAV}}}current-user-principal"
        )
        home = await self._href(principal, _PROPFIND_HOME, f"{{{CALDAV}}}calendar-home-set")
        return await self._collections(home)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> httpx.Response:
        # The credentials ride on each request rather than on the client: the client is
        # shared for the life of the process and outlives a password replaced on the
        # settings page.
        return await (self._http or shared_http()).request(
            method,
            url,
            content=content,
            headers=headers,
            auth=(self._settings.icloud_email, self._settings.icloud_app_password),
            timeout=timeout,
            follow_redirects=True,
        )

    async def _href(self, url: str, body: str, tag: str) -> str:
        """The single href held by one property of one resource, resolved against its URL.

        iCloud answers with a path for the principal and a full URL on the account's
        cluster for the calendar home, so both are joined onto the address they came
        back from.
        """
        response = await self._request(
            "PROPFIND",
            url,
            content=body,
            headers={"Depth": "0", **_XML_HEADERS},
            timeout=DISCOVERY_TIMEOUT_SECONDS,
        )
        for entry in _multistatus(response):
            for prop in _ok_props(entry, tag):
                found = prop.findtext(f"{{{DAV}}}href")
                if found:
                    return urljoin(str(response.url), found.strip())
        raise CalendarUnavailable(f"iCloud returned no {tag.rsplit('}', 1)[-1]} for {url}")

    async def _collections(self, home: str) -> list[Collection]:
        """Every event-holding calendar in the home collection, named and addressed.

        A calendar is offered rather than created: iCloud will not let a client make
        one, so the collection has to exist already, made in the Calendar app. Its URL
        is a random uuid, which is exactly why nobody is asked to type it - it is picked
        from this list.
        """
        response = await self._request(
            "PROPFIND",
            home,
            content=_PROPFIND_CALENDARS,
            headers={"Depth": "1", **_XML_HEADERS},
            timeout=DISCOVERY_TIMEOUT_SECONDS,
        )
        found = []
        for entry in _multistatus(response):
            href = entry.findtext(f"{{{DAV}}}href")
            if not href or not _holds_events(entry):
                continue
            name = next((prop.text or "" for prop in _ok_props(entry, f"{{{DAV}}}displayname")), "")
            if name:
                found.append(Collection(name, urljoin(str(response.url), href.strip())))
        return found


_http: httpx.AsyncClient | None = None


def shared_http() -> httpx.AsyncClient:
    """One connection pool for the process; `close_client` is wired into shutdown."""
    global _http
    if _http is None:
        _http = httpx.AsyncClient()
    return _http


async def close_client() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
    _http = None


# -- the protocol --------------------------------------------------------------------


def _holds_events(entry: ElementTree.Element) -> bool:
    """A calendar collection that takes VEVENTs, rather than a reminders list.

    Both are calendar collections on iCloud, and both can carry the same name.
    """
    if not any(
        prop.find(f"{{{CALDAV}}}calendar") is not None
        for prop in _ok_props(entry, f"{{{DAV}}}resourcetype")
    ):
        return False
    components = [
        component.get("name")
        for prop in _ok_props(entry, f"{{{CALDAV}}}supported-calendar-component-set")
        for component in prop.iterfind(f"{{{CALDAV}}}comp")
    ]
    return not components or "VEVENT" in components


def _multistatus(response: httpx.Response) -> list[ElementTree.Element]:
    """The per-resource halves of a 207, or the reason there is nothing to read."""
    if response.status_code != 207:
        raise _refused(response)
    root = ElementTree.fromstring(response.text)
    return list(root.iterfind(f"{{{DAV}}}response"))


def _ok_props(entry: ElementTree.Element, tag: str) -> Iterator[ElementTree.Element]:
    """Properties from the 200 propstat; the ones a server does not have come back
    empty under a 404 propstat in the same response."""
    for propstat in entry.iterfind(f"{{{DAV}}}propstat"):
        if " 200 " not in (propstat.findtext(f"{{{DAV}}}status") or ""):
            continue
        prop = propstat.find(f"{{{DAV}}}prop")
        if prop is not None:
            yield from prop.iterfind(tag)


def _written(response: httpx.Response) -> None:
    if response.status_code >= 400:
        raise _refused(response)


def _refused(response: httpx.Response) -> CalendarUnavailable:
    """Why iCloud would not do it, in the words a person can act on.

    A 403 is left to speak for itself rather than read as a bad password: CalDAV answers
    a rejected write with 403 and an XML body naming the precondition it failed.
    """
    if response.status_code == 401:
        return CalendarUnavailable(
            "iCloud rejected the app-specific password. Changing your Apple ID password "
            "revokes every app-specific password, so generate a new one and enter it "
            "under Connections on the settings page."
        )
    return CalendarUnavailable(
        f"CalDAV {response.request.method} on {response.request.url} answered "
        f"HTTP {response.status_code}: {response.text[:200]}"
    )


def _event_url(calendar_url: str, uid: str) -> str:
    return urljoin(calendar_url.rstrip("/") + "/", quote(f"{uid}.ics", safe="@"))
