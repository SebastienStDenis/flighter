"""ntfy payload shaping, and the promise that a failed push never reaches the caller."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from flight_tracker.config import Settings
from flight_tracker.events import (
    BAGGAGE_CLAIM_ASSIGNED,
    CANCELLED,
    DEPARTED,
    DEPARTURE_DELAYED,
    DIVERTED,
    GATE_ASSIGNED,
    GATE_CHANGED,
    LANDED,
)
from flight_tracker.models import Booking, FlightEvent
from flight_tracker.notify import Notifier

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
            raise httpx.ConnectError("ntfy is down", request=request)
        return httpx.Response(200)

    @property
    def only(self) -> httpx.Request:
        assert len(self.requests) == 1
        return self.requests[0]


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


async def test_title_url_and_click_header(settings: Settings) -> None:
    recorder = await push(settings, event(GATE_ASSIGNED, new="B22"))
    request = recorder.only

    # httpx drops the default port; the topic is what matters.
    assert (request.url.host, request.url.path) == ("ntfy", "/flights")
    assert request.headers["X-Title"] == "DL1234 JFK -> LAX"
    assert request.headers["X-Click"] == "https://flights.example.com/f/7"
    assert request.content.decode() == "Gate B22"
    assert "Authorization" not in request.headers


@pytest.mark.parametrize(
    ("flight_event", "priority", "message"),
    [
        (event(GATE_ASSIGNED, new="B22"), "default", "Gate B22"),
        (event(GATE_CHANGED, old="B22", new="C14"), "high", "Gate changed from B22 to C14"),
        (
            event(DEPARTURE_DELAYED, old=DEPARTS.isoformat(), new=DELAYED.isoformat()),
            "default",
            "Delayed 35 min, now departing 19:35 EDT",
        ),
        (event(DEPARTED, new=DELAYED.isoformat()), "default", "Left the gate at 19:35 EDT"),
        (event(LANDED, new=DELAYED.isoformat()), "default", "Landed at 16:35 PDT"),
        (event(BAGGAGE_CLAIM_ASSIGNED, new="carousel 3"), "default", "Bag claim: carousel 3"),
        (
            event(CANCELLED, old="false", new="true"),
            "high",
            "Marked cancelled by FlightAware - confirm with the airline",
        ),
        (event(DIVERTED, old="false", new="true"), "high", "Flight diverted"),
    ],
)
async def test_message_and_priority_per_kind(
    settings: Settings, flight_event: FlightEvent, priority: str, message: str
) -> None:
    request = (await push(settings, flight_event)).only
    assert request.content.decode() == message
    assert request.headers["X-Priority"] == priority
    assert request.headers["X-Tags"]


async def test_bearer_token_is_sent_when_configured(settings: Settings) -> None:
    authed = settings.model_copy(update={"ntfy_token": "tk_secret"})
    recorder = await push(authed, event(GATE_ASSIGNED, new="B22"))
    assert recorder.only.headers["Authorization"] == "Bearer tk_secret"


async def test_no_topic_means_no_request(settings: Settings) -> None:
    silent = settings.model_copy(update={"ntfy_topic": ""})
    recorder = await push(silent, event(GATE_ASSIGNED, new="B22"))
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
    request = recorder.only
    assert request.headers["X-Priority"] == "high"
    assert request.headers["X-Title"] == "AeroAPI budget reached"
    assert "$4.12 of the $4.00 monthly cap" in request.content.decode()
