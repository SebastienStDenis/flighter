"""Pushover payload shaping, and the promise that a push is only ever counted as sent
when Pushover says it took it."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from flighter.config import Settings
from flighter.models import Booking, EventKind, FlightEvent
from flighter.notify import MESSAGES_URL, PRIORITY_QUIET, Notifier, PushFailed, message_url
from flighter.phase import CANCELLED_NOTICE

ORIGIN_TZ = "America/New_York"
DEST_TZ = "America/Los_Angeles"

# 19:00 and 19:35 in New York.
DEPARTS = datetime(2026, 9, 12, 23, 0, tzinfo=UTC)
DELAYED = datetime(2026, 9, 12, 23, 35, tzinfo=UTC)


class Recorder:
    """A MockTransport that keeps every request, and answers the way Pushover does."""

    def __init__(self, *, fail: bool = False, refuse: list[str] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._fail = fail
        self._refuse = refuse
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._fail:
            raise httpx.ConnectError("Pushover is unreachable", request=request)
        if self._refuse is not None:
            return httpx.Response(400, json={"status": 0, "errors": self._refuse, "request": "x"})
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
    sent = (await push(settings, event(EventKind.GATE_ASSIGNED, new="B22"))).only

    # The title is the flight and nothing else: no glyph in front of it to decode.
    assert sent["title"] == "DL1234 JFK -> LAX"
    assert sent["url"] == "https://flights.example.com/f/7"
    assert sent["url_title"] == "Open flight"
    assert sent["message"] == "Gate B22"


@pytest.mark.parametrize(
    ("flight_event", "priority", "message"),
    [
        (event(EventKind.GATE_ASSIGNED, new="B22"), "0", "Gate B22"),
        (event(EventKind.GATE_CHANGED, old="B22", new="C14"), "1", "Gate changed from B22 to C14"),
        (
            event(EventKind.DEPARTURE_DELAYED, old=DEPARTS.isoformat(), new=DELAYED.isoformat()),
            "0",
            "Delayed 35 min. Departs 19:35 EDT",
        ),
        (event(EventKind.DEPARTED, new=DELAYED.isoformat()), "0", "Departed 19:35 EDT"),
        (event(EventKind.LANDED, new=DELAYED.isoformat()), "0", "Landed 16:35 PDT"),
        (
            event(EventKind.BAGGAGE_CLAIM_ASSIGNED, new="carousel 3"),
            "0",
            "Baggage claim carousel 3",
        ),
        (event(EventKind.CANCELLED, old="false", new="true"), "1", CANCELLED_NOTICE),
        (event(EventKind.DIVERTED, old="false", new="true"), "1", "Diverted"),
    ],
)
async def test_message_and_priority_per_kind(
    settings: Settings, flight_event: FlightEvent, priority: str, message: str
) -> None:
    sent = (await push(settings, flight_event)).only
    assert sent["message"] == message
    assert sent["priority"] == priority
    # Plain text on the lock screen: no symbols, no emoji, nothing to decode.
    assert sent["title"].isascii()
    assert sent["message"].isascii()


async def test_the_credentials_travel_with_every_push(settings: Settings) -> None:
    sent = (await push(settings, event(EventKind.GATE_ASSIGNED, new="B22"))).only
    assert sent["token"] == "app-token"
    assert sent["user"] == "user-key"


async def test_a_missing_credential_means_no_request(settings: Settings) -> None:
    half = settings.model_copy(update={"pushover_user_key": ""})
    recorder = await push(half, event(EventKind.GATE_ASSIGNED, new="B22"))
    assert recorder.requests == []


async def test_an_unreachable_pushover_is_a_failure_the_caller_hears_about(
    settings: Settings,
) -> None:
    """Stamping an event as notified on a connection error is how a gate change gets
    lost for good; the dispatcher has to be told so it can try again."""
    with pytest.raises(PushFailed):
        await push(settings, event(EventKind.CANCELLED, new="true"), fail=True)


async def test_a_refusal_carries_pushovers_own_reason(settings: Settings) -> None:
    """A 4xx names the key it refused, which is the difference between a bad
    application token and a bad user key."""
    with pytest.raises(PushFailed, match="application token is invalid"):
        await push(
            settings,
            event(EventKind.GATE_ASSIGNED, new="B22"),
            refuse=["application token is invalid"],
        )


async def test_budget_alert_is_high_priority(settings: Settings) -> None:
    recorder = Recorder()
    await Notifier(settings, transport=recorder.transport).budget_tripped(
        Decimal("4.12"), Decimal("4.00")
    )
    sent = recorder.only
    assert sent["priority"] == "1"
    assert sent["title"] == "AeroAPI budget reached"
    assert "$4.12 of the $4.00 monthly cap" in sent["message"]


async def test_the_check_push_does_not_buzz_a_pocket(settings: Settings) -> None:
    recorder = Recorder()
    await Notifier(settings, transport=recorder.transport).check()
    assert recorder.only["priority"] == str(PRIORITY_QUIET)


# -- importing a marked email --------------------------------------------------------


async def imported(settings: Settings, outcome: str) -> dict[str, str]:
    recorder = Recorder()
    await Notifier(settings, transport=recorder.transport).mail_imported(
        [booking()], outcome=outcome
    )
    return recorder.only


async def test_an_import_links_to_the_flight_page(settings: Settings) -> None:
    """Not the calendar: iCloud publishes no web address for one event, and the flight
    page carries the live gate and status anyway."""
    sent = await imported(settings, "created")

    assert sent["title"] == "Flight added"
    assert sent["message"] == "DL1234 JFK -> LAX"
    assert sent["url"] == "https://flights.example.com/f/7"
    assert sent["priority"] == "0"


@pytest.mark.parametrize(
    ("outcome", "title"),
    [("review", "Flight needs review"), ("duplicate", "Already tracked")],
)
async def test_every_import_outcome_has_its_own_words(
    settings: Settings, outcome: str, title: str
) -> None:
    sent = await imported(settings, outcome)
    assert sent["title"] == title
    assert "DL1234 JFK -> LAX" in sent["message"]


async def test_a_failed_import_links_to_the_problems_page(settings: Settings) -> None:
    """Where the email can be tried again, written off, or opened in Mail."""
    recorder = Recorder()
    await Notifier(settings, transport=recorder.transport).mail_failed(
        message_id="<abc.123@mail.example.com>",
        subject="Your itinerary",
        reason="the model timed out",
    )
    sent = recorder.only

    assert sent["title"] == "Import failed"
    assert sent["url"] == "https://flights.example.com/problems"
    assert sent["url_title"] == "Open flighter"
    assert sent["priority"] == "0"
    assert sent["message"] == "Your itinerary\nthe model timed out"


async def test_mail_pushes_are_best_effort(settings: Settings) -> None:
    """The ingest log is the record of the decision and has no retry column; a push
    that fails is logged, and the email still counts as handled."""
    recorder = Recorder(fail=True)
    notifier = Notifier(settings, transport=recorder.transport)
    await notifier.mail_imported([booking()], outcome="created")
    await notifier.mail_failed(message_id="<x@y>", subject="s", reason="r")
    assert len(recorder.requests) == 2


@pytest.mark.parametrize("message_id", ["<abc@x.example>", "abc@x.example"])
def test_a_message_url_is_encoded_the_same_either_way(message_id: str) -> None:
    """The log stores whatever the header said, brackets or not."""
    assert message_url(message_id) == "message://%3Cabc@x.example%3E"
