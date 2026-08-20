"""Pushover payload shaping, and the promise that a failed push never reaches the caller."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from flighter.config import Settings
from flighter.events import (
    BAGGAGE_CLAIM_ASSIGNED,
    CANCELLED,
    DEPARTED,
    DEPARTURE_DELAYED,
    DIVERTED,
    GATE_ASSIGNED,
    GATE_CHANGED,
    LANDED,
)
from flighter.models import Booking, FlightEvent
from flighter.notify import MESSAGES_URL, Notifier

ORIGIN_TZ = "America/New_York"
DEST_TZ = "America/Los_Angeles"

# 19:00 and 19:35 in New York.
DEPARTS = datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
DELAYED = datetime(2026, 9, 12, 23, 35, tzinfo=UTC)


class Recorder:
    """A MockTransport that keeps every request, or raises instead of answering."""

    def __init__(self, *, fail: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self._fail = fail
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._fail:
            raise httpx.ConnectError("Pushover is unreachable", request=request)
        return httpx.Response(200, json={"status": 1, "request": "a-uuid"})

    @property
    def only(self) -> dict[str, str]:
        """The one request's form fields, which is all a push ever is."""
        assert len(self.requests) == 1
        request = self.requests[0]
        assert str(request.url) == MESSAGES_URL
        return {name: values[0] for name, values in parse_qs(request.content.decode()).items()}


def booking() -> Booking:
    return Booking(
        id=7,
        source="email",
        marketing_carrier="DL",
        marketing_number="1234",
        origin_iata="JFK",
        dest_iata="LAX",
        scheduled_departure_utc=DEPARTS,
    )


def event(kind: str, old: str | None = None, new: str | None = None) -> FlightEvent:
    return FlightEvent(id=1, booking_id=7, kind=kind, old_value=old, new_value=new)


async def push(settings: Settings, flight_event: FlightEvent, **kwargs: object) -> Recorder:
    recorder = Recorder(**kwargs)  # type: ignore[arg-type]
    notifier = Notifier(settings, transport=recorder.transport)
    await notifier.flight_event(booking(), flight_event, origin_tz=ORIGIN_TZ, dest_tz=DEST_TZ)
    return recorder


async def test_title_and_url_name_the_flight(settings: Settings) -> None:
    sent = (await push(settings, event(GATE_ASSIGNED, new="B22"))).only

    # The glyph is the whole point of leading with one: the flight is still readable.
    assert sent["title"].endswith("DL1234 JFK -> LAX")
    assert sent["url"] == "https://flights.example.com/f/7"
    assert sent["url_title"]
    assert sent["message"] == "Gate B22"


@pytest.mark.parametrize(
    ("flight_event", "priority", "message"),
    [
        (event(GATE_ASSIGNED, new="B22"), "0", "Gate B22"),
        (event(GATE_CHANGED, old="B22", new="C14"), "1", "Gate changed from B22 to C14"),
        (
            event(DEPARTURE_DELAYED, old=DEPARTS.isoformat(), new=DELAYED.isoformat()),
            "0",
            "Delayed 35 min, now departing 19:35 EDT",
        ),
        (event(DEPARTED, new=DELAYED.isoformat()), "0", "Left the gate at 19:35 EDT"),
        (event(LANDED, new=DELAYED.isoformat()), "0", "Landed at 16:35 PDT"),
        (event(BAGGAGE_CLAIM_ASSIGNED, new="carousel 3"), "0", "Bag claim: carousel 3"),
        (
            event(CANCELLED, old="false", new="true"),
            "1",
            "Marked cancelled by FlightAware - confirm with the airline",
        ),
        (event(DIVERTED, old="false", new="true"), "1", "Flight diverted"),
    ],
)
async def test_message_and_priority_per_kind(
    settings: Settings, flight_event: FlightEvent, priority: str, message: str
) -> None:
    sent = (await push(settings, flight_event)).only
    assert sent["message"] == message
    assert sent["priority"] == priority


async def test_the_credentials_travel_with_every_push(settings: Settings) -> None:
    sent = (await push(settings, event(GATE_ASSIGNED, new="B22"))).only
    assert sent["token"] == "app-token"
    assert sent["user"] == "user-key"


async def test_a_missing_credential_means_no_request(settings: Settings) -> None:
    half = settings.model_copy(update={"pushover_user_key": ""})
    recorder = await push(half, event(GATE_ASSIGNED, new="B22"))
    assert recorder.requests == []


async def test_a_failing_transport_is_swallowed(settings: Settings) -> None:
    recorder = await push(settings, event(CANCELLED, new="true"), fail=True)
    # It tried, and the caller is none the wiser.
    assert len(recorder.requests) == 1


async def test_budget_alert_is_high_priority(settings: Settings) -> None:
    recorder = Recorder()
    await Notifier(settings, transport=recorder.transport).budget_tripped(
        Decimal("4.12"), Decimal("4.00")
    )
    sent = recorder.only
    assert sent["priority"] == "1"
    assert sent["title"].endswith("AeroAPI budget reached")
    assert "$4.12 of the $4.00 monthly cap" in sent["message"]
