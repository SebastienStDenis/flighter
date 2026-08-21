"""The calendar object, and the CalDAV round trip that puts it on iCloud.

Two risks live here. One is a wrong timezone, which turns into a missed flight. The
other is discovery: iCloud serves every account from a different cluster under a
different principal id, so a client that guesses a URL works for exactly one person.
The fake below answers the way the real thing is documented to, cluster hop included.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from flighter import prefs
from flighter.caldav import (
    CalendarClient,
    CalendarUnavailable,
    Collection,
    calendar_link,
    event_body,
    event_uid,
)
from flighter.config import Settings
from flighter.models import Airport, Booking, FlightSnapshot
from flighter.phase import CANCELLED_NOTICE
from flighter.prefs import Prefs

BASE_URL = "https://flights.example.com"

PRINCIPAL = "/12345/principal/"
HOME = "https://p34-caldav.icloud.com:443/12345/calendars/"
FLIGHTS = "/12345/calendars/6c1f4f0e-flights/"
FLIGHTS_URL = f"https://p34-caldav.icloud.com{FLIGHTS}"

AIRPORTS = {
    "JFK": Airport(
        iata="JFK",
        name="John F Kennedy International Airport",
        city="New York",
        country="US",
        latitude=40.6398,
        longitude=-73.7789,
        tz="America/New_York",
    ),
    "LAX": Airport(
        iata="LAX",
        name="Los Angeles International Airport",
        city="Los Angeles",
        country="US",
        latitude=33.9425,
        longitude=-118.4081,
        tz="America/Los_Angeles",
    ),
    "CDG": Airport(
        iata="CDG",
        name="Charles de Gaulle International Airport",
        city="Paris",
        country="FR",
        latitude=49.0128,
        longitude=2.55,
        tz="Europe/Paris",
    ),
}


def booking(**fields: object) -> Booking:
    base: dict[str, object] = {
        "id": 7,
        "source": "email",
        "marketing_carrier": "DL",
        "marketing_number": "1234",
        "origin_iata": "JFK",
        "dest_iata": "LAX",
        # 15:00 in New York.
        "scheduled_departure_utc": datetime(2026, 9, 12, 19, 0, tzinfo=UTC),
        # 15:20 in Los Angeles.
        "scheduled_arrival_utc": datetime(2026, 9, 12, 22, 20, tzinfo=UTC),
        "status": "active",
        "confirmation_code": "ABC123",
        "seat": "14A",
    }
    base.update(fields)
    return Booking(**base)


def snapshot(**fields: object) -> FlightSnapshot:
    base: dict[str, object] = {"raw": {}, "cancelled": False, "diverted": False}
    base.update(fields)
    return FlightSnapshot(**base)


def ical_for(**fields: object) -> str:
    body = event_body(booking(**fields), None, AIRPORTS, base_url=BASE_URL)
    return body.to_ical().decode()


# --- the calendar object -------------------------------------------------------------


def test_normal_flight() -> None:
    body = event_body(
        booking(),
        snapshot(gate_origin="B22", terminal_origin="4", baggage_claim="3"),
        AIRPORTS,
        base_url=BASE_URL,
    )
    text = body.to_ical().decode()

    assert "SUMMARY:DL1234 JFK -> LAX" in text
    assert "LOCATION:John F Kennedy International Airport\\, New York\\, US" in text
    assert "DTSTART;TZID=America/New_York:20260912T150000" in text
    assert "DTEND;TZID=America/Los_Angeles:20260912T152000" in text
    assert "TRIGGER:-PT3H" in text
    assert "STATUS:CANCELLED" not in text

    [event] = body.walk("VEVENT")
    assert event["UID"] == event_uid(booking())
    description = str(event["DESCRIPTION"])
    assert "Confirmation: ABC123" in description
    assert "Seat: 14A" in description
    assert "Gate: B22 (Terminal 4)" in description
    assert "Baggage claim: 3" in description
    assert description.endswith(f"\n\n{BASE_URL}/f/7")


def test_a_flight_from_an_email_carries_a_link_to_it() -> None:
    """The confirmation itself, one tap from the entry it became."""
    body = event_body(
        booking(source_message_id="<abc@icloud.invalid>"), None, AIRPORTS, base_url=BASE_URL
    )
    [event] = body.walk("VEVENT")
    assert str(event["DESCRIPTION"]).endswith(
        f"\n\n{BASE_URL}/f/7\nEmail: message://%3Cabc@icloud.invalid%3E"
    )


def test_the_entry_is_the_ticket_and_says_who_flies_it() -> None:
    text = ical_for(operating_carrier="BA", operating_number="112")

    assert "SUMMARY:DL1234 JFK -> LAX" in text
    assert "Operated as BA112" in text


def test_missing_values_are_omitted_not_printed() -> None:
    text = ical_for(confirmation_code=None, seat=None)
    assert "None" not in text
    assert "Confirmation" not in text
    assert "Seat" not in text


def test_a_flight_with_no_arrival_time_still_gets_an_end() -> None:
    """An email that never stated one still has to produce a valid event: iCalendar has
    no open-ended VEVENT, and a two-hour guess beats no entry at all."""
    text = ical_for(scheduled_arrival_utc=None)
    assert "DTSTART;TZID=America/New_York:20260912T150000" in text
    assert "DTEND;TZID=America/Los_Angeles:20260912T140000" in text


def test_cancelled_flight_is_marked_never_deleted() -> None:
    """AeroAPI's flag means FlightAware stopped tracking, which is not quite the same
    as the airline cancelling, so the entry says who said so rather than striking the
    trip through."""
    body = event_body(booking(), snapshot(cancelled=True), AIRPORTS, base_url=BASE_URL)
    text = body.to_ical().decode()
    assert "SUMMARY:DL1234 JFK -> LAX (marked cancelled)" in text
    assert "STATUS:TENTATIVE" in text
    assert "STATUS:CANCELLED" not in text
    [event] = body.walk("VEVENT")
    assert str(event["DESCRIPTION"]).startswith(CANCELLED_NOTICE)
    # Still a full event: the trip stays visible where it was planned.
    assert "DTSTART;TZID=America/New_York:20260912T150000" in text


def test_the_event_follows_the_flight_as_it_moves() -> None:
    """The block starts when the flight actually left and ends when it is expected at
    the gate, read off the same ladder the page and the widget use."""
    moved = snapshot(
        scheduled_out=datetime(2026, 9, 12, 19, 0, tzinfo=UTC),
        estimated_out=datetime(2026, 9, 12, 19, 40, tzinfo=UTC),
        actual_out=datetime(2026, 9, 12, 19, 45, tzinfo=UTC),
        estimated_in=datetime(2026, 9, 12, 23, 5, tzinfo=UTC),
    )
    text = event_body(booking(), moved, AIRPORTS, base_url=BASE_URL).to_ical().decode()
    assert "DTSTART;TZID=America/New_York:20260912T154500" in text
    assert "DTEND;TZID=America/Los_Angeles:20260912T160500" in text


def test_overnight_flight_ends_the_next_day() -> None:
    text = ical_for(
        dest_iata="CDG",
        # 23:00 in New York, arriving 12:30 the next day in Paris.
        scheduled_departure_utc=datetime(2026, 9, 13, 3, 0, tzinfo=UTC),
        scheduled_arrival_utc=datetime(2026, 9, 13, 10, 30, tzinfo=UTC),
    )
    assert "DTSTART;TZID=America/New_York:20260912T230000" in text
    assert "DTEND;TZID=Europe/Paris:20260913T123000" in text


@pytest.mark.parametrize("dest_iata", ["LAX", "CDG"])
def test_each_end_carries_its_own_zone_and_the_rules_to_read_it(dest_iata: str) -> None:
    body = event_body(booking(dest_iata=dest_iata), None, AIRPORTS, base_url=BASE_URL)
    text = body.to_ical().decode()

    # A UTC stamp pins the wrong wall clock the day the rules change and a bare local
    # time floats to wherever the phone is standing; naming the zone does neither. RFC
    # 4791 then requires the VTIMEZONE that says what the name means.
    zones = {str(component["TZID"]) for component in body.walk("VTIMEZONE")}
    assert zones == {"America/New_York", AIRPORTS[dest_iata].tz}

    [event] = body.walk("VEVENT")
    assert event["DTSTART"].params["TZID"] == "America/New_York"
    assert event["DTEND"].params["TZID"] == AIRPORTS[dest_iata].tz
    for edge in ("DTSTART", "DTEND"):
        assert f"{edge};TZID=" in text
        assert not event[edge].to_ical().endswith(b"Z")


def test_one_timezone_is_stated_once() -> None:
    body = event_body(booking(dest_iata="JFK"), None, AIRPORTS, base_url=BASE_URL)
    assert [str(component["TZID"]) for component in body.walk("VTIMEZONE")] == ["America/New_York"]


def test_unknown_airport_falls_back_without_inventing_a_zone() -> None:
    body = event_body(booking(origin_iata="ZZZ"), None, AIRPORTS, base_url=BASE_URL)
    assert "LOCATION:ZZZ" in body.to_ical().decode()
    assert {str(component["TZID"]) for component in body.walk("VTIMEZONE")} == {
        "UTC",
        "America/Los_Angeles",
    }


# --- the protocol --------------------------------------------------------------------


def _multistatus(body: str) -> httpx.Response:
    return httpx.Response(207, text=body, headers={"Content-Type": "application/xml"})


class FakeICloud:
    """Just enough of caldav.icloud.com to answer discovery and hold one collection.

    The calendar home deliberately lives on another host, because the real one does: the
    href a principal hands back names the cluster the account is served from.
    """

    def __init__(self) -> None:
        self.events: dict[str, str] = {}
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "PROPFIND":
            return self._propfind(request)
        path = request.url.path
        if request.method == "PUT":
            self.events[path] = request.content.decode()
            return httpx.Response(201, headers={"ETag": '"1"'})
        if request.method == "DELETE":
            if self.events.pop(path, None) is None:
                return httpx.Response(404)
            return httpx.Response(204)
        return httpx.Response(405)

    def _propfind(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return _multistatus(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<multistatus xmlns="DAV:"><response><href>/</href><propstat>'
                f"<prop><current-user-principal><href>{PRINCIPAL}</href>"
                "</current-user-principal></prop>"
                "<status>HTTP/1.1 200 OK</status></propstat></response></multistatus>"
            )
        if request.url.path == PRINCIPAL:
            return _multistatus(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                f"<response><href>{PRINCIPAL}</href><propstat>"
                f"<prop><c:calendar-home-set><href>{HOME}</href></c:calendar-home-set></prop>"
                "<status>HTTP/1.1 200 OK</status></propstat></response></multistatus>"
            )
        if request.url.path == "/12345/calendars/":
            return _multistatus(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                + _collection("/12345/calendars/", None, [])
                + _collection("/12345/calendars/9a-reminders/", "Flights", ["VTODO"])
                + _collection(FLIGHTS, "Flights", ["VEVENT"])
                + _collection("/12345/calendars/1b-home/", "Home", ["VEVENT"])
                + "</multistatus>"
            )
        return httpx.Response(404)


def _collection(href: str, name: str | None, components: list[str]) -> str:
    if name is None:
        return (
            f"<response><href>{href}</href><propstat><prop><resourcetype><collection/>"
            "</resourcetype></prop><status>HTTP/1.1 200 OK</status></propstat></response>"
        )
    comps = "".join(f'<c:comp name="{component}"/>' for component in components)
    return (
        f"<response><href>{href}</href><propstat><prop>"
        f"<displayname>{name}</displayname>"
        "<resourcetype><collection/><c:calendar/></resourcetype>"
        f"<c:supported-calendar-component-set>{comps}</c:supported-calendar-component-set>"
        "</prop><status>HTTP/1.1 200 OK</status></propstat></response>"
    )


@pytest.fixture
def icloud() -> FakeICloud:
    return FakeICloud()


@pytest.fixture
def calendar(settings: Settings, icloud: FakeICloud) -> CalendarClient:
    return CalendarClient(settings, AIRPORTS, transport=icloud.transport)


async def test_discovery_walks_from_the_root_to_every_calendar(
    calendar: CalendarClient, icloud: FakeICloud
) -> None:
    assert await calendar.calendars() == [
        Collection("Flights", FLIGHTS_URL),
        Collection("Home", "https://p34-caldav.icloud.com/12345/calendars/1b-home/"),
    ]
    # The principal href came back as a path and the home as a URL on another host; both
    # are resolved against the address they arrived from rather than assumed.
    assert [str(request.url) for request in icloud.requests] == [
        "https://caldav.icloud.com/",
        f"https://caldav.icloud.com{PRINCIPAL}",
        "https://p34-caldav.icloud.com/12345/calendars/",
    ]


async def test_a_sync_writes_without_discovering_anything(
    calendar: CalendarClient, icloud: FakeICloud
) -> None:
    """The URL was resolved once, when the calendar was picked. A flight costs one PUT."""
    await calendar.upsert(booking(), None)
    await calendar.upsert(booking(), None)
    assert [request.method for request in icloud.requests] == ["PUT", "PUT"]


async def test_a_reminders_list_is_never_offered_as_a_calendar(
    calendar: CalendarClient,
) -> None:
    """Both are calendar collections on iCloud, and a person can name them alike."""
    assert [collection.name for collection in await calendar.calendars()] == ["Flights", "Home"]


async def test_a_calendar_that_is_gone_is_not_among_the_ones_offered(
    calendar: CalendarClient,
) -> None:
    offered = {collection.url for collection in await calendar.calendars()}
    assert "https://p34-caldav.icloud.com/12345/calendars/6c1f4f0e-voyages/" not in offered


async def test_a_rejected_password_says_why(settings: Settings) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    with pytest.raises(CalendarUnavailable) as raised:
        await CalendarClient(settings, AIRPORTS, transport=transport).calendars()
    assert "app-specific password" in str(raised.value)


async def test_an_account_without_credentials_never_reaches_icloud(
    unconfigured: Settings,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"nothing should reach iCloud, but {request.method} did")

    client = CalendarClient(unconfigured, AIRPORTS, transport=httpx.MockTransport(refuse))
    with pytest.raises(CalendarUnavailable) as raised:
        await client.calendars()
    assert "under Connections" in str(raised.value)


def test_the_calendar_link_aims_at_noon_where_the_flight_leaves() -> None:
    """`calshow:` renders its instant wherever the phone is, so it is aimed at the middle
    of the departure day rather than at the flight, and the date survives the trip."""
    # 15:00 in New York; the link is 12:00 there, counted from Apple's 2001 epoch.
    link = calendar_link(datetime(2026, 9, 12, 19, 0, tzinfo=UTC), "America/New_York")
    assert link == "calshow:810921600"


def test_the_calendar_link_takes_the_day_from_the_airport_not_from_utc() -> None:
    """A 23:30 departure in New York is already tomorrow in UTC, and the entry is not."""
    link = calendar_link(datetime(2026, 9, 12, 3, 30, tzinfo=UTC), "America/New_York")
    assert link == "calshow:810835200"


async def test_the_event_lands_under_the_calendar(
    calendar: CalendarClient, icloud: FakeICloud
) -> None:
    uid = await calendar.upsert(booking(), None)
    assert uid == event_uid(booking())
    assert icloud.events[f"{FLIGHTS}{uid}.ics"].startswith("BEGIN:VCALENDAR")
    put = next(request for request in icloud.requests if request.method == "PUT")
    assert put.headers["Content-Type"] == "text/calendar; charset=utf-8"


async def test_an_event_deleted_by_hand_comes_back(
    calendar: CalendarClient, icloud: FakeICloud
) -> None:
    tracked = booking()
    tracked.calendar_event_uid = await calendar.upsert(tracked, None)
    icloud.events.clear()

    assert await calendar.upsert(tracked, snapshot(gate_origin="B22")) == tracked.calendar_event_uid
    [stored] = icloud.events.values()
    assert "Gate: B22" in stored


async def test_removing_a_booking_removes_its_event(
    calendar: CalendarClient, icloud: FakeICloud
) -> None:
    tracked = booking()
    tracked.calendar_event_uid = await calendar.upsert(tracked, None)
    await calendar.delete(tracked)
    assert icloud.events == {}


async def test_an_event_already_gone_is_not_an_error(
    calendar: CalendarClient, icloud: FakeICloud
) -> None:
    await calendar.delete(booking(calendar_event_uid="flighter-7@flighter.invalid"))
    assert [request.method for request in icloud.requests][-1] == "DELETE"


async def test_an_unpicked_calendar_is_a_quiet_no_op(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefs, "_current", Prefs())

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"nothing should reach iCloud, but {request.method} did")

    client = CalendarClient(settings, AIRPORTS, transport=httpx.MockTransport(refuse))
    assert await client.upsert(booking(), None) is None
    await client.delete(booking(calendar_event_uid="flighter-7@flighter.invalid"))
