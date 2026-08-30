"""The pages, rendered against a faked data layer.

Nothing here touches the database or the network. The interesting risk in a template is a
null: gates, baggage belts and every estimate are absent for most of a flight's life,
and a page that raises on one of them is a page that fails exactly when it is needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from flighter import notices, prefs, updates, web
from flighter.aeroapi import BREAKER_KEY, BudgetExceeded, BudgetStatus
from flighter.caldav import CalendarUnavailable, Collection
from flighter.checks import CheckResult
from flighter.config import Settings
from flighter.db import get_session
from flighter.lookup import Candidate
from flighter.models import (
    KV,
    Airport,
    Booking,
    BookingStatus,
    Confirmation,
    FlightEvent,
    FlightSnapshot,
    IngestLog,
)
from flighter.notify import Notifier
from flighter.timezones import to_local
from flighter.widget import LAST_SEEN_KEY

NOW = datetime.now(UTC)
# Two days out, and at a fixed hour rather than at whatever hour the suite is being run
# at. The cards here are read for the strings they draw - a time, the day under it, a
# rule through the one a delay replaced - and a departure that inherits the clock puts
# those within minutes of local midnight twice a day, where a flight held forty minutes
# lands on the next day and the day under it is struck through too. Midday UTC is the
# middle of the day in every zone this file reads a card in.
DEPARTURE = (NOW + timedelta(days=2)).replace(hour=12, minute=40, second=0, microsecond=0)
ARRIVAL = DEPARTURE + timedelta(hours=7)

CALDAV_HOME = "https://p34-caldav.icloud.com/12345/calendars/"
FLIGHTS_CALENDAR = f"{CALDAV_HOME}6c1f4f0e-flights/"

AIRPORTS = {
    "YUL": Airport(
        iata="YUL", name="Montreal-Trudeau", city="Montreal", country="CA", tz="America/Toronto"
    ),
    "LHR": Airport(
        iata="LHR", name="London Heathrow", city="London", country="GB", tz="Europe/London"
    ),
    "MAN": Airport(
        iata="MAN", name="Manchester", city="Manchester", country="GB", tz="Europe/London"
    ),
}

# Every field the preferences form posts, so a test can change one of them.
SETTINGS_FORM = {
    "public_base_url": "https://flights.example.com",
    "log_level": "INFO",
    "imap_flag_colour": "grey",
}

# What discovery answers with, so the settings page has a picker to render.
CALENDARS = [Collection("Flights", FLIGHTS_CALENDAR), Collection("Home", f"{CALDAV_HOME}1b-home/")]

# What a lookup answers with: one leg, in the shape the add form takes.
CANDIDATE = Candidate(
    marketing_carrier="AC",
    marketing_number="871",
    origin_iata="YUL",
    dest_iata="LHR",
    departure_local=datetime(2026, 9, 12, 18, 40),
    arrival_local=datetime(2026, 9, 13, 10, 25),
    operating_carrier="LH",
    operating_number="479",
)

CLEAR_BUDGET = BudgetStatus(
    spend_usd=Decimal("0.42"), cap_usd=Decimal("4.00"), tripped=False, month="2026-08"
)
SPENT_BUDGET = BudgetStatus(
    spend_usd=Decimal("4.01"), cap_usd=Decimal("4.00"), tripped=True, month="2026-08"
)


def booking(**kwargs: Any) -> Booking:
    defaults: dict[str, Any] = {
        "id": 1,
        "source": "manual",
        "marketing_carrier": "AC",
        "marketing_number": "871",
        "origin_iata": "YUL",
        "dest_iata": "LHR",
        "scheduled_departure_utc": DEPARTURE,
        "scheduled_arrival_utc": ARRIVAL,
        "status": "active",
    }
    return Booking(**(defaults | kwargs))


def empty_snapshot() -> FlightSnapshot:
    """What a snapshot looks like for a flight that is still three days out."""
    return FlightSnapshot(id=1, booking_id=1, raw={})


def replace_snapshot(**fields: Any) -> FlightSnapshot:
    """An empty snapshot with just these observed, for one point in a flight's life."""
    return FlightSnapshot(id=1, booking_id=1, observed_at=NOW, raw={}, **fields)


def full_snapshot() -> FlightSnapshot:
    return FlightSnapshot(
        id=2,
        booking_id=1,
        observed_at=NOW,
        cancelled=False,
        diverted=False,
        gate_origin="B27",
        gate_destination="A14",
        terminal_origin="3",
        terminal_destination="2",
        baggage_claim="7",
        scheduled_out=DEPARTURE,
        estimated_out=DEPARTURE + timedelta(minutes=20),
        actual_out=DEPARTURE + timedelta(minutes=25),
        actual_off=DEPARTURE + timedelta(minutes=35),
        scheduled_in=ARRIVAL,
        estimated_in=ARRIVAL + timedelta(minutes=10),
        progress_percent=64,
        aircraft_type="B789",
        registration="C-FVLQ",
        raw={},
    )


class FakeResult:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def scalars(self) -> Iterator[Any]:
        return iter(self._rows)

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """Answers the handful of direct queries the view layer makes, by model."""

    def __init__(self, **rows: Sequence[Any]) -> None:
        self.rows: dict[str, Sequence[Any]] = dict(rows)
        self.deleted: list[Any] = []

    async def execute(self, statement: Any) -> FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        return FakeResult(self.rows.get(entity.__name__, []))

    async def scalar(self, statement: Any) -> Any:
        return None

    async def get(self, model: type, pk: Any) -> Any:
        (key,) = inspect(model).primary_key
        for row in self.rows.get(model.__name__, []):
            if getattr(row, key.name) == pk:
                return row
        return None

    def add(self, instance: Any) -> None:
        self.rows.setdefault(type(instance).__name__, []).append(instance)  # type: ignore[attr-defined]

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def delete(self, instance: Any) -> None:
        self.deleted.append(instance)

    async def merge(self, instance: Any) -> Any:
        self.rows.setdefault(type(instance).__name__, [])
        return instance


def build_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, *, raising: bool = True
) -> TestClient:
    """The app with a faked data layer, ready for a request."""
    session = FakeSession()

    async def fake_get_airport(_session: Any, iata: str) -> Airport | None:
        return AIRPORTS.get(iata)

    async def fake_budget(_session: Any, _settings: Any = None) -> BudgetStatus:
        return CLEAR_BUDGET

    async def no_bookings(_session: Any, **_kwargs: Any) -> list[Booking]:
        return []

    async def fake_calendars(self: Any) -> list[Collection]:
        return list(CALENDARS)

    @contextlib.asynccontextmanager
    async def same_session() -> AsyncIterator[FakeSession]:
        yield session

    async def passing_check(_settings: Any, _service: str) -> CheckResult:
        return CheckResult("stub", True, "stubbed")

    async def passing_calendar(_settings: Any, _url: str | None = None) -> CheckResult:
        return CheckResult("calendar", True, "stubbed")

    monkeypatch.setattr(web, "session_scope", same_session)
    monkeypatch.setattr(web.views, "get_airport", fake_get_airport)
    monkeypatch.setattr(web, "budget_status", fake_budget)
    monkeypatch.setattr(web.booking_repo, "list_bookings", no_bookings)
    monkeypatch.setattr(web.CalendarClient, "calendars", fake_calendars)
    # Saving asks the service whether the details work. The suite stays off the network,
    # and the tests that care about a refusal put their own answer in.
    monkeypatch.setattr(web, "check_service", passing_check)
    monkeypatch.setattr(web, "check_calendar", passing_calendar)

    app = web.create_app(settings)
    app.dependency_overrides[get_session] = lambda: session
    test_client = TestClient(app, raise_server_exceptions=raising)
    test_client.session = session  # type: ignore[attr-defined]
    return test_client


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with build_client(settings, monkeypatch) as test_client:
        yield test_client


def record_secrets(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Catch what would be written, so the suite stays off the filesystem."""
    written: list[dict[str, str]] = []

    def write(values: Any) -> Settings:
        written.append(dict(values))
        return Settings()

    monkeypatch.setattr(web, "write_secrets", write)
    return written


def show(monkeypatch: pytest.MonkeyPatch, view_booking: Booking, snapshot: Any) -> None:
    """Make one booking and its newest snapshot the whole of the database."""

    async def get_booking(_session: Any, booking_id: int) -> Booking | None:
        return view_booking if booking_id == view_booking.id else None

    monkeypatch.setattr(web.booking_repo, "get_booking", get_booking)
    show_board(monkeypatch, [(view_booking, snapshot)])


def show_board(monkeypatch: pytest.MonkeyPatch, flights: Sequence[tuple[Booking, Any]]) -> None:
    """Make these bookings, each with its newest snapshot, the whole of the board."""

    async def latest(_session: Any, _ids: Any) -> dict[int, Any]:
        return {b.id: snap for b, snap in flights if snap is not None}

    async def list_bookings(_session: Any, **_kwargs: Any) -> list[Booking]:
        return [b for b, _ in flights]

    monkeypatch.setattr(web.booking_repo, "list_bookings", list_bookings)
    monkeypatch.setattr(web.views.booking_repo, "latest_snapshots", latest)


def looks_up(
    monkeypatch: pytest.MonkeyPatch, candidates: Sequence[Candidate]
) -> list[tuple[str, str, date]]:
    """Answer every lookup with these, and record what it was asked about."""
    asked: list[tuple[str, str, date]] = []

    async def find_flights(carrier: str, number: str, day: date) -> list[Candidate]:
        asked.append((carrier, number, day))
        return list(candidates)

    monkeypatch.setattr(web.lookup, "find_flights", find_flights)
    return asked


# --- The board -----------------------------------------------------------------------


def test_every_page_says_when_it_was_rendered(client: TestClient) -> None:
    """The page's script turns this into "Loaded 12m ago" once the page is old, whether
    it was left open or served from the cache with no network."""
    for path in ("/", "/f/new", "/mail", "/settings"):
        found = re.search(r'<meta name="rendered-at" content="([^"]+)">', client.get(path).text)
        assert found, path
        rendered = datetime.fromisoformat(found.group(1))
        assert abs(datetime.now(UTC) - rendered) < timedelta(seconds=5)


def test_a_page_with_a_live_flight_carries_the_stamp_its_script_polls_against(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside its day a flight's facts can move while the page is being read, so the
    board and the flight's own page carry the change stamp. Days out nothing moves
    faster than a person, and the pages carry nothing and will ask for nothing."""
    soon = booking(
        scheduled_departure_utc=NOW + timedelta(hours=2),
        scheduled_arrival_utc=NOW + timedelta(hours=9),
    )
    show(monkeypatch, soon, None)
    assert '<meta name="flight-stamp"' in client.get("/").text
    assert '<meta name="flight-stamp"' in client.get("/f/1").text

    show(monkeypatch, booking(), None)
    assert '<meta name="flight-stamp"' not in client.get("/").text
    assert '<meta name="flight-stamp"' not in client.get("/f/1").text


def test_a_flight_the_poller_has_closed_the_book_on_is_not_watched(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = booking(
        status="completed",
        scheduled_departure_utc=NOW - timedelta(hours=9),
        scheduled_arrival_utc=NOW - timedelta(hours=2),
    )
    show(monkeypatch, done, replace_snapshot(actual_on=NOW - timedelta(hours=2)))
    assert '<meta name="flight-stamp"' not in client.get("/f/1").text


def test_the_stamp_endpoint_answers_with_the_stamp_alone(client: TestClient) -> None:
    answer = client.get("/api/stamp")
    assert answer.status_code == 200
    assert answer.json() == {"stamp": "0.0.0"}


def test_the_board_says_what_to_do_when_there_are_no_flights(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "Nothing on the board" in page.text
    assert "/f/new" in page.text


def test_the_board_separates_mine_friends_and_all_flown_flights(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = booking(id=1)
    friend = booking(id=2, friend_name="Sam")
    mine_flown = booking(
        id=3,
        scheduled_departure_utc=NOW - timedelta(days=3),
        scheduled_arrival_utc=NOW - timedelta(days=3) + timedelta(hours=7),
    )
    friend_flown = booking(
        id=4,
        friend_name="Lee",
        scheduled_departure_utc=NOW - timedelta(days=2),
        scheduled_arrival_utc=NOW - timedelta(days=2) + timedelta(hours=7),
    )
    show_board(
        monkeypatch, [(mine, None), (friend, None), (mine_flown, None), (friend_flown, None)]
    )

    body = client.get("/").text
    first = body.index('id="flight-tabs-panel-1"')
    second = body.index('id="flight-tabs-panel-2"')
    third = body.index('id="flight-tabs-panel-3"')
    assert first < body.index('href="/f/1?from=mine"') < second
    assert second < body.index('href="/f/2?from=friends"') < third
    assert body.index('href="/f/3?from=flown"') > third
    assert body.index('href="/f/4?from=flown"') > third
    assert rows(body) == ["4", "3"]

    friend_card = board_card(body, 2)
    assert 'class="friend-avatar' in friend_card
    assert "--friend-hue:" in friend_card
    assert 'class="-mb-2 flex items-center gap-2 px-6"' in friend_card
    assert friend_card.index("friend-avatar") < friend_card.index("<header>")
    assert ">S</span>" in friend_card and "Sam" in friend_card
    assert logo("AC") in friend_card

    friend_row = board_card(body, 4)
    assert 'class="friend-avatar' in friend_row and ">L</span>" in friend_row
    assert "Lee" not in friend_row


def test_the_selected_board_tab_survives_a_trip_to_flight_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(friend_name="Sam"), None)

    board = client.get("/?tab=friends").text
    assert re.search(
        r'id="flight-tabs-tab-2"\s+aria-controls="[^"]+"\s+aria-selected="true"', board
    )
    assert 'href="/f/1?from=friends"' in board

    detail = client.get("/f/1?from=friends").text
    assert 'href="/?tab=friends"' in detail
    assert 'name="return_tab" value="friends"' in detail


def test_a_codeshare_is_shown_under_the_number_booked_with_a_note_on_who_flies_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(operating_carrier="LH", operating_number="479"), None)

    for path in ("/", "/f/1"):
        body = client.get(path).text
        assert "<span>AC871</span>" in body and logo("AC") in body
    # Who flies it is a detail for the flight page, drawn with its own mark; the board
    # stays the number that was booked.
    body = client.get("/f/1").text
    assert "Operated as" in body and "LH479" in body and logo("LH") in body
    board = client.get("/").text
    assert "Operated" not in board and logo("LH") not in board


def test_a_row_is_the_flight_its_route_and_when_it_leaves(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    landed = left + timedelta(hours=7)
    show(
        monkeypatch,
        booking(seat="14A", scheduled_departure_utc=left, scheduled_arrival_utc=landed),
        replace_snapshot(gate_origin="B27"),
    )
    body = client.get("/").text
    assert '<p class="font-mono">Sat 1 Aug 08:00 EDT</p>' in body
    # The seat, the gate and the landing belong to the card; a row is only what to open.
    assert "14A" not in body and "B27" not in body and "BST" not in body


def test_the_plus_is_the_tab_that_lights_on_the_add_page(client: TestClient) -> None:
    board = client.get("/").text
    assert 'href="/" data-variant="secondary" aria-current="page"' in " ".join(board.split())
    assert 'aria-label="Add a flight" data-variant="ghost"' in " ".join(board.split())

    add = " ".join(client.get("/f/new").text.split())
    assert 'href="/" data-variant="ghost"' in add
    assert 'aria-label="Add a flight" data-variant="secondary" aria-current="page"' in add


def test_the_board_offers_one_tap_out_of_a_spent_budget(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def spent(_session: Any) -> BudgetStatus:
        return SPENT_BUDGET

    monkeypatch.setattr(web, "budget_status", spent)

    body = client.get("/").text
    assert "Updates paused" in body
    assert "$4.01" in body
    assert "Raise limit to $6.00" in body
    assert 'action="/limit"' in body


def test_raising_the_limit_also_lets_polling_start_again(client: TestClient) -> None:
    """Raising the cap on its own changes nothing: the breaker latches until it is cleared."""
    latch = KV(key=BREAKER_KEY, value={"month": "2026-08"})
    client.session.rows["KV"] = [latch]  # type: ignore[attr-defined]

    response = client.post("/limit", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert prefs.current().aeroapi_monthly_cap_usd == Decimal("6.00")
    assert client.session.deleted == [latch]  # type: ignore[attr-defined]


def test_the_email_page_says_which_email_was_set_aside_and_why(client: TestClient) -> None:
    client.session.rows["IngestLog"] = [set_aside_row()]  # type: ignore[attr-defined]

    body = client.get("/mail").text

    assert ">Needs attention</span>" in body
    assert "Your booking is confirmed" in body
    assert "RuntimeError: the model timed out." in body
    assert "Try again" in body
    assert "Ignore" in body
    # The quoted subject is distinguished from the service's own account of it.
    assert '<span class="truncate pe-0.5 italic">Your booking is confirmed</span>' in body
    # The email itself, in Mail, is where the other half of the decision is made.
    assert 'href="message://%3Cabc@icloud.invalid%3E"' in body
    assert 'title="Open in Mail" aria-label="Open in Mail"' in body


def test_the_page_and_the_push_say_the_same_thing(client: TestClient, settings: Settings) -> None:
    """One email, one wording: a person who read the push and then opened the page is
    not told two different stories about it.

    The push names the email in a line of its own because a lock screen has nothing else
    to name it with, and says where it still is because it can offer nothing to do about
    it; the page has the subject at the top of the card and the buttons under it, so what
    has to match word for word is the reason in the middle.
    """
    row = set_aside_row()
    client.session.rows["IngestLog"] = [row]

    sent = asyncio.run(_push_about(row, settings))
    body = client.get("/mail").text

    subject, said = sent["message"].split("\n")
    assert subject == f"Subject: {row.subject}"
    assert row.subject in body
    assert said == f"{notices.sentence(row.error)} {notices.STILL_FLAGGED}"
    # Where the email is, is the one thing the page does not have to say: the buttons
    # that do something about it are right there under the words.
    assert notices.sentence(row.error) in body
    assert notices.STILL_FLAGGED not in body


async def _push_about(row: IngestLog, settings: Settings) -> dict[str, str]:
    """The form fields of the push the service would have sent about this email."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": 1, "request": "a-uuid"})

    await Notifier(settings, transport=httpx.MockTransport(handle)).mail_failed(
        message_id=row.message_id, subject=row.subject, reason=row.error
    )
    return {name: values[0] for name, values in parse_qs(requests[0].content.decode()).items()}


def test_a_set_aside_email_is_kept_off_the_board(client: TestClient) -> None:
    """The board is read in a hurry for a gate; an email that would not parse is not
    news about any flight on it."""
    client.session.rows["IngestLog"] = [set_aside_row()]  # type: ignore[attr-defined]
    board = client.get("/").text
    assert "Email could not be imported" not in board
    assert "Your booking is confirmed" not in board


def test_the_email_tab_is_marked_only_while_something_is_waiting(client: TestClient) -> None:
    """The tab is an envelope either way; what changes is whether it carries a mark.

    The count is said in words for anything that speaks the page rather than showing it,
    because the mark itself is a stroke on a glyph and says nothing out loud.
    """
    quiet = client.get("/").text
    assert 'href="/mail"' in quiet
    assert "waiting" not in quiet

    client.session.rows["IngestLog"] = [set_aside_row()]  # type: ignore[attr-defined]
    # On every page, not only the board: the mark is how you learn there is anything.
    for path in ("/", "/settings", "/mail"):
        assert "1 waiting" in client.get(path).text


def test_the_email_page_lists_what_became_of_every_email(client: TestClient) -> None:
    """The page is the history of the mailbox, not only the part of it that went wrong."""
    client.session.rows["IngestLog"] = [  # type: ignore[attr-defined]
        imported_row(),
        log_row(message_id="<dupe@icloud.invalid>", outcome="duplicate", subject="Same trip again"),
        log_row(message_id="<none@icloud.invalid>", outcome="no_flight", subject="Your receipt"),
        log_row(
            message_id="<soon@icloud.invalid>",
            outcome="error",
            subject="Trip to London",
            error="the model timed out",
            retry_at=NOW + timedelta(minutes=2),
        ),
    ]
    client.session.rows["Booking"] = [  # type: ignore[attr-defined]
        booking(source="email", source_message_id="<yes@icloud.invalid>")
    ]

    body = client.get("/mail").text

    for subject in ("Your booking is confirmed", "Same trip again", "Your receipt", "Trip"):
        assert subject in body
    for label in ("Imported", "Already added", "Ignored", "Retrying"):
        assert f">{label}</span>" in body
    # Opening the source email is available on history as well as failures needing attention.
    assert body.count('title="Open in Mail" aria-label="Open in Mail"') == 4
    # An imported email points at what it put on the board.
    assert 'href="/f/1"' in body
    assert "AC871" in body
    # A retry is the service still working, so it is history and not a decision to make.
    assert "Needs attention" not in body


def test_a_message_still_being_retried_is_not_asked_about(client: TestClient) -> None:
    """Only an email nothing more will happen to on its own is put in front of a person."""
    client.session.rows["IngestLog"] = [  # type: ignore[attr-defined]
        log_row(
            message_id="<soon@icloud.invalid>",
            outcome="error",
            subject="Trip to London",
            error="the model timed out",
            retry_at=NOW + timedelta(minutes=2),
        )
    ]

    body = client.get("/mail").text

    assert ">Retrying</span>" in body
    # The reason all the same: a wait is easier to sit through when it says what for.
    assert "The model timed out." in body
    assert "Try again" not in body


def test_the_email_page_says_when_nothing_has_been_read(client: TestClient) -> None:
    assert "No email read yet" in client.get("/mail").text


def test_trying_a_set_aside_message_again_puts_it_back_in_the_queue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = set_aside_row()
    client.session.rows["IngestLog"] = [row]
    woken = 0

    def wake() -> None:
        nonlocal woken
        woken += 1

    monkeypatch.setattr(web.ingest, "wake", wake)

    response = client.post(
        "/mail/retry", data={"message_id": row.message_id}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/mail"
    # The email never lost its flag, so clearing the give-up and waking the watcher is
    # all it takes.
    assert row.attempts == 0
    assert row.retry_at is not None
    assert woken == 1


def test_ignoring_a_set_aside_message_lets_the_next_sweep_unflag_it(
    client: TestClient,
) -> None:
    row = set_aside_row()
    client.session.rows["IngestLog"] = [row]

    response = client.post(
        "/mail/ignore", data={"message_id": row.message_id}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/mail"
    assert row.outcome == "ignored"
    assert row.retry_at is None


def test_a_message_that_is_not_set_aside_is_a_404(client: TestClient) -> None:
    for path in ("/mail/retry", "/mail/ignore"):
        assert client.post(path, data={"message_id": "<nope@icloud.invalid>"}).status_code == 404


def log_row(**kwargs: Any) -> IngestLog:
    defaults: dict[str, Any] = {"processed_at": NOW, "subject": "", "attempts": 0, "retry_at": None}
    return IngestLog(**(defaults | kwargs))


def imported_row() -> IngestLog:
    return log_row(
        message_id="<yes@icloud.invalid>",
        outcome="created",
        subject="Your booking is confirmed",
    )


def set_aside_row() -> IngestLog:
    return IngestLog(
        message_id="<abc@icloud.invalid>",
        processed_at=NOW,
        outcome="error",
        subject="Your booking is confirmed",
        error="RuntimeError: the model timed out",
        attempts=3,
        retry_at=None,
    )


# --- One flight ----------------------------------------------------------------------


def test_a_flight_from_an_email_links_back_to_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the Booking card beside the calendar link, where it is found when wanted and
    in nobody's way when not."""
    show(monkeypatch, booking(source="email", source_message_id="<abc@icloud.invalid>"), None)
    body = client.get("/f/1").text
    assert 'href="message://%3Cabc@icloud.invalid%3E"' in body
    assert "Open in Mail" in body


def test_a_flight_added_from_a_lookup_has_no_email_to_open(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), None)
    assert "Open in Mail" not in client.get("/f/1").text


def test_a_flight_with_nothing_known_yet_still_renders(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three days out, every gate, belt and estimate is null. That is not an error."""
    show(monkeypatch, booking(), empty_snapshot())

    page = client.get("/f/1")
    assert page.status_code == 200
    body = page.text
    assert "AC871" in body
    assert "YUL" in body and "LHR" in body
    assert "Montreal" in body and "London" in body
    # Days out there is nothing to walk to and nothing to count to yet, so the card
    # stops at the times rather than drawing a row of dashes under them.
    card = body[body.index('<div class="card gap-5">') : body.index('<div class="card mt-3"')]
    assert "Baggage claim" not in card and ">Gate</div>" not in card
    assert "<footer" not in card and "<time" not in card
    assert "None" not in body
    # A missing value is a dash in its row, never the page-level empty state.
    assert 'class="empty"' not in body
    assert "Scheduled" in body


def test_a_flight_in_the_air_renders_what_is_worth_knowing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(confirmations=[Confirmation("X7QW2P")], seat="14A"), full_snapshot())

    body = client.get("/f/1").text
    assert "B27" in body and "A14" in body
    assert "B789" in body
    assert "X7QW2P" in body and "14A" in body
    # The aircraft's place on the route rule is the clock's to keep, not the last poll's.
    assert f'data-off="{(DEPARTURE + timedelta(minutes=35)).isoformat()}"' in body
    assert f'data-on="{(ARRIVAL + timedelta(minutes=10)).isoformat()}"' in body
    # What is on the ticket, not what is in the flight plan.
    for gone in ("Filed route", "Distance", "Registration", "Timezone", "Last checked"):
        assert gone not in body
    # The runway time that matters from a seat, and only that one.
    assert "Lands in" in body
    assert "Departs in" not in body and "At the gate in" not in body


def test_the_card_counts_to_one_milestone_at_a_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Departure and arrival are always on the card. The bottom counts to whatever is
    next and has a time: nobody estimates wheels up, so pushback counts to the landing."""
    pushed_back = replace_snapshot(actual_out=DEPARTURE + timedelta(minutes=5))
    show(monkeypatch, booking(), pushed_back)
    body = client.get("/f/1").text
    assert "Lands in" in body
    assert "Departs in" not in body and "At the gate in" not in body
    assert "Wheels up" not in body

    landed = replace_snapshot(
        actual_out=DEPARTURE + timedelta(minutes=5),
        actual_off=DEPARTURE + timedelta(minutes=15),
        actual_on=ARRIVAL - timedelta(minutes=12),
        estimated_in=ARRIVAL + timedelta(minutes=20),
    )
    show(monkeypatch, booking(), landed)
    body = client.get("/f/1").text
    assert "At the gate in" in body
    assert f'data-to="{(ARRIVAL + timedelta(minutes=20)).isoformat()}"' in body
    assert "Lands in" not in body

    at_the_gate = replace_snapshot(
        actual_out=DEPARTURE + timedelta(minutes=5),
        actual_off=DEPARTURE + timedelta(minutes=15),
        actual_on=ARRIVAL - timedelta(minutes=12),
        actual_in=ARRIVAL,
    )
    show(monkeypatch, booking(), at_the_gate)
    body = client.get("/f/1").text
    assert '<time class="countdown' not in body


def struck(shown: str) -> str:
    """A line the move changed, as the end behind the tap draws it. The end is drawn
    again whole there, so the lines the move left alone carry no rule."""
    return f"<s>{shown}</s>"


def clock_of(instant: datetime, tz: str) -> str:
    return f"{to_local(instant, tz):%H:%M}"


def tap_for_was(tone: str, *, arrival: bool = False) -> str:
    """The box a value that moved sits in: the whole end, drawn in its tone, as the one
    tap that shows what it was."""
    side = "justify-self-end" if arrival else "justify-self-start"
    return f'<div class="replaced {tone} {side}"'


def big_time(instant: datetime, tz: str, *, arrival: bool = False) -> str:
    """The card's large time: the clock, with the zone small and grey on its outer side,
    which is after a departure and before an arrival."""
    local = to_local(instant, tz)
    abbr = (
        f'<span class="m{"r" if arrival else "l"}-1 text-[0.6875rem] font-medium '
        f'text-muted-foreground">{local:%Z}</span>'
    )
    shown = f"{abbr}{local:%H:%M}" if arrival else f"{local:%H:%M}{abbr}"
    return f'<div class="font-mono text-xl leading-tight font-bold">{shown}</div>'


def test_a_delay_is_shown_against_what_was_booked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    late = DEPARTURE + timedelta(minutes=40)
    show(monkeypatch, booking(), replace_snapshot(scheduled_out=DEPARTURE, estimated_out=late))
    body = client.get("/f/1").text
    assert big_time(late, "America/Toronto") in body
    assert tap_for_was("text-stop-soft") in body
    assert struck(clock_of(DEPARTURE, "America/Toronto")) in body
    # The colour says it, and the tap holds the rest; no words repeat it.
    assert "late " not in body and "early " not in body


def test_the_card_keeps_only_the_time_that_holds_now(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One time per end, whatever has happened to it. What it replaced is in the page
    for the tap to show, and nowhere else on the card. The day it leaves did not move,
    so the end behind the tap repeats it with no rule through it."""
    late = DEPARTURE + timedelta(minutes=40)
    tz = "America/Toronto"
    show(monkeypatch, booking(), replace_snapshot(scheduled_out=DEPARTURE, estimated_out=late))
    card = top_card(client.get("/f/1").text)
    assert struck(clock_of(DEPARTURE, tz)) in card
    assert card.count(clock_of(DEPARTURE, tz)) == 1
    assert struck(day_of(DEPARTURE, tz)) not in card


def test_a_time_brought_forward_is_green(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = DEPARTURE - timedelta(minutes=20)
    show(monkeypatch, booking(), replace_snapshot(scheduled_out=DEPARTURE, estimated_out=earlier))
    body = client.get("/f/1").text
    assert big_time(earlier, "America/Toronto") in body
    assert tap_for_was("text-ok-soft") in body
    assert struck(clock_of(DEPARTURE, "America/Toronto")) in body


def test_each_end_of_a_red_eye_names_its_own_day(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaves Saturday evening, lands Sunday morning: one heading cannot say both."""
    overnight = booking(
        scheduled_departure_utc=datetime(2026, 9, 12, 22, 40, tzinfo=UTC),
        scheduled_arrival_utc=datetime(2026, 9, 13, 9, 25, tzinfo=UTC),
    )
    show(monkeypatch, overnight, None)

    for path in ("/", "/f/1"):
        body = client.get(path).text
        assert "Sat 12 Sep" in body
        assert "Sun 13 Sep" in body


def test_a_delay_past_midnight_names_only_the_day_it_now_leaves(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move over midnight changed the day as well as the time, so the end behind the
    tap strikes both. The card in the open, and the heading over it, name Sunday alone."""
    tz = "America/Toronto"
    booked = datetime(2026, 9, 13, 3, 50, tzinfo=UTC)
    slipped = datetime(2026, 9, 13, 4, 30, tzinfo=UTC)
    show(
        monkeypatch,
        booking(scheduled_departure_utc=booked),
        replace_snapshot(scheduled_out=booked, estimated_out=slipped),
    )

    body = client.get("/f/1").text
    assert big_time(slipped, tz) in body
    assert struck(clock_of(booked, tz)) in body
    assert struck(day_of(booked, tz)) in body
    # The day it was booked for is behind the tap and nowhere else on the page.
    assert body.count(day_of(booked, tz)) == 1
    assert day_of(slipped, tz) in body
    heading = body[body.index("<h2") : body.index("</h2>")]
    assert day_of(slipped, tz) in heading


def test_an_arrival_past_midnight_names_only_the_day_it_now_lands(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tz = "Europe/London"
    booked = datetime(2026, 9, 13, 22, 40, tzinfo=UTC)
    slipped = datetime(2026, 9, 13, 23, 30, tzinfo=UTC)
    show(
        monkeypatch,
        booking(scheduled_arrival_utc=booked),
        replace_snapshot(scheduled_in=booked, estimated_in=slipped),
    )

    body = client.get("/f/1").text
    assert big_time(slipped, tz, arrival=True) in body
    assert tap_for_was("text-stop-soft", arrival=True) in body
    assert struck(clock_of(booked, tz)) in body
    assert struck(day_of(booked, tz)) in body
    assert body.count(day_of(booked, tz)) == 1
    assert day_of(slipped, tz) in body


def top_card(body: str) -> str:
    """The flight card's route and times, before its footer."""
    start = body.index('<div class="card gap-5">')
    return body[start : body.index("</section>", start)]


def day_of(instant: datetime, tz: str) -> str:
    return to_local(instant, tz).strftime("%a %-d %b")


def test_the_card_leads_with_the_city_over_the_code_and_the_time_over_the_day(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), empty_snapshot())
    card = top_card(client.get("/f/1").text)
    assert re.search(r"Montreal</div>\s*<div[^>]*>YUL</div>", card)
    assert re.search(r"London</div>\s*<div[^>]*>LHR</div>", card)
    assert "Departure" not in card and "Arrival" not in card
    # The time is the big fact; the day sits under it.
    ends = card[card.index('class="ends"') :]
    departs = big_time(DEPARTURE, "America/Toronto")
    assert ends.index(departs) < ends.index(day_of(DEPARTURE, "America/Toronto"))


def test_terminal_and_gate_share_a_line_and_read_as_one_pair(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), full_snapshot())
    card = top_card(client.get("/f/1").text)
    # Each is a small word over its value, so the two line up across the card.
    assert card.count(">Term</div>") == 2 and card.count(">Gate</div>") == 2
    assert place("Term", "3") in card and place("Gate", "B27", "font-semibold") in card
    assert place("Term", "2") in card and place("Gate", "A14", "font-semibold") in card
    assert "Terminal" not in card
    # The arrival side mirrors the departure side: the terminal on the outside, the
    # gate beside it.
    gate = "font-semibold"
    assert card.index(place("Term", "3")) < card.index(place("Gate", "B27", gate))
    assert card.index(place("Gate", "A14", gate)) < card.index(place("Term", "2"))
    # The belt is not a box: it is the footer's number once the aircraft is parked.
    assert "Baggage claim" not in card


def place(name: str, value: str, tone: str = "") -> str:
    """A terminal or gate box as the card draws it: the word, then the value under it."""
    shown = tone if value != "-" else "text-muted-foreground"
    return (
        '<div class="text-[0.6875rem] leading-4 font-medium tracking-wide '
        'text-muted-foreground uppercase">'
        f'{name}</div>\n  <div class="font-mono font-medium {shown}">{value}</div>'
    )


def test_a_place_not_yet_known_keeps_its_box_with_a_dash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = booking(
        scheduled_departure_utc=NOW + timedelta(hours=3),
        scheduled_arrival_utc=NOW + timedelta(hours=10),
    )
    show(monkeypatch, today, replace_snapshot(terminal_origin="3", gate_destination="A14"))
    card = top_card(client.get("/f/1").text)
    assert place("Term", "3") in card and place("Gate", "-") in card
    assert place("Term", "-") in card and place("Gate", "A14", "font-semibold") in card

    show(monkeypatch, today, empty_snapshot())
    card = top_card(client.get("/f/1").text)
    assert card.count(place("Term", "-")) == 2 and card.count(place("Gate", "-")) == 2


def test_the_rule_says_how_long_the_hop_is_until_there_is_something_to_measure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), None)
    for path in ("/", "/f/1"):
        body = client.get(path).text
        assert ">7h 00m</span>" in body
        assert "<svg" not in top_card(body) if path == "/f/1" else True

    show(monkeypatch, booking(), full_snapshot())
    for path in ("/", "/f/1"):
        body = client.get(path).text
        assert "7h 00m" not in body and "6h 45m" not in body
        assert 'class="route-mark rounded-full' in body


def test_the_board_card_colours_a_time_that_slipped_and_leaves_one_that_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Late is red and carries what was planned; ten minutes on arrival is not worth a
    mark, so that end is drawn plainly and has nothing behind it."""
    show(monkeypatch, booking(), full_snapshot())

    body = client.get("/").text
    late = DEPARTURE + timedelta(minutes=25)
    assert big_time(late, "America/Toronto") in body
    assert tap_for_was("text-stop-soft") in body
    assert struck(clock_of(DEPARTURE, "America/Toronto")) in body
    assert big_time(ARRIVAL + timedelta(minutes=10), "Europe/London", arrival=True) in body
    assert tap_for_was("text-stop-soft", arrival=True) not in body
    assert struck(clock_of(ARRIVAL, "Europe/London")) not in body


def cards(body: str) -> list[str]:
    """The flights the board draws as full cards, in order, by the id each links to."""
    return re.findall(r'<a class="card[^"]*" href="/f/(\d+)(?:\?[^\"]*)?"', body)


def rows(body: str) -> list[str]:
    """The flights the board draws as one-line rows, in order, by the id each links to."""
    return re.findall(r'<a class="item"[^>]*href="/f/(\d+)(?:\?[^\"]*)?"', body)


def board_card(body: str, booking_id: int) -> str:
    """One flight's card on the board, from its link to its closing tag."""
    start = body.index(f'href="/f/{booking_id}')
    return body[start : body.index("</a>", start)]


def logo(carrier: str) -> str:
    return f'src="https://www.gstatic.com/flights/airline_logos/70px/{carrier}.png"'


def watched(card: str) -> bool:
    """Whether a card carries the gate line and counts to something."""
    return ">Gate</div>" in card and "<footer" in card


def test_a_flight_page_keeps_the_app_in_the_header_and_draws_its_own_way_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header says which app this is on every page, so the way back up is in the
    page: the installed app has no browser chrome to go back with, and losing the app's
    own name to make room for that is a trade nothing asked for."""
    show(monkeypatch, booking(), None)

    header, content = client.get("/f/1").text.split("<main", 1)

    assert "Flighter" in header
    assert "Flights" not in header
    assert "Flights" in content


def test_the_board_card_is_the_flight_page_card_with_a_link_on_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(seat="14A"), full_snapshot())

    board = client.get("/").text
    assert cards(board) == ["1"]
    page = client.get("/f/1").text
    # Same header, cities, ends, gate line and footer: only the wrapper differs.
    inside = slice(board.index("<header>"), board.index("</footer>"))
    on_the_page = slice(page.index("<header>"), page.index("</footer>"))
    assert board[inside] == page[on_the_page]
    for piece in ("Montreal", 'class="ends"', "B27", "Lands in"):
        assert piece in board[inside]
    # What the Details card says stays on the flight page.
    assert "14A" not in board


def test_every_flight_is_a_card_and_only_those_inside_their_day_are_watched(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taxiing in, under way, or leaving today: each has a gate line and a figure to
    count to. A flight next week has the same card, stopped at the times."""
    down = NOW - timedelta(hours=7)
    landed = booking(
        id=1, scheduled_departure_utc=down, scheduled_arrival_utc=NOW + timedelta(minutes=15)
    )
    taxiing_in = replace_snapshot(
        actual_out=down,
        actual_off=down + timedelta(minutes=12),
        actual_on=NOW - timedelta(minutes=10),
        estimated_in=NOW + timedelta(minutes=15),
    )
    left = NOW - timedelta(hours=2)
    in_the_air = booking(
        id=2, scheduled_departure_utc=left, scheduled_arrival_utc=NOW + timedelta(hours=5)
    )
    flying = replace_snapshot(actual_out=left, actual_off=left + timedelta(minutes=12))
    today = booking(
        id=3,
        scheduled_departure_utc=NOW + timedelta(hours=6),
        scheduled_arrival_utc=NOW + timedelta(hours=13),
    )
    next_week = booking(
        id=4,
        scheduled_departure_utc=NOW + timedelta(days=6),
        scheduled_arrival_utc=NOW + timedelta(days=6, hours=7),
    )
    show_board(
        monkeypatch, [(landed, taxiing_in), (in_the_air, flying), (today, None), (next_week, None)]
    )

    body = client.get("/").text
    assert cards(body) == ["1", "2", "3", "4"]
    assert rows(body) == []
    assert all(watched(board_card(body, booking_id)) for booking_id in (1, 2, 3))
    assert not watched(board_card(body, 4))


def test_a_board_of_flights_weeks_away_is_cards_that_stop_at_the_times(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    soon = booking(
        id=1,
        scheduled_departure_utc=NOW + timedelta(days=10),
        scheduled_arrival_utc=NOW + timedelta(days=10, hours=7),
    )
    later = booking(
        id=2,
        scheduled_departure_utc=NOW + timedelta(days=20),
        scheduled_arrival_utc=NOW + timedelta(days=20, hours=7),
    )
    show_board(monkeypatch, [(soon, None), (later, None)])

    body = client.get("/").text
    assert cards(body) == ["1", "2"]
    assert rows(body) == []
    for booking_id in (1, 2):
        card = board_card(body, booking_id)
        assert not watched(card) and "Scheduled" in card
    # Each card names its own day, in its header, so the board has no headings to file
    # them under.
    assert "Sat " not in body[: body.index('<a class="card')]
    header = board_card(body, 1)[: board_card(body, 1).index("</header>")]
    assert day_of(soon.scheduled_departure_utc, "America/Toronto") in header


def test_the_board_runs_in_the_order_flights_now_leave(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flight held three hours belongs after the one that left in the meantime."""
    held = booking(
        id=1,
        scheduled_departure_utc=NOW - timedelta(hours=3),
        scheduled_arrival_utc=NOW + timedelta(hours=4),
    )
    waiting = replace_snapshot(
        scheduled_out=NOW - timedelta(hours=3), estimated_out=NOW + timedelta(hours=1)
    )
    left = NOW - timedelta(hours=1)
    gone = booking(
        id=2, scheduled_departure_utc=left, scheduled_arrival_utc=NOW + timedelta(hours=6)
    )
    flying = replace_snapshot(actual_out=left, actual_off=left + timedelta(minutes=12))
    show_board(monkeypatch, [(held, waiting), (gone, flying)])

    assert cards(client.get("/").text) == ["2", "1"]


def test_a_cancelled_flight_is_a_card_with_nothing_left_to_watch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called_off = booking(
        id=1,
        scheduled_departure_utc=NOW + timedelta(hours=3),
        scheduled_arrival_utc=NOW + timedelta(hours=10),
    )
    cancelled = replace_snapshot(cancelled=True)
    left = NOW - timedelta(hours=1)
    in_the_air = booking(
        id=2, scheduled_departure_utc=left, scheduled_arrival_utc=NOW + timedelta(hours=6)
    )
    flying = replace_snapshot(actual_out=left, actual_off=left + timedelta(minutes=12))
    show_board(monkeypatch, [(called_off, cancelled), (in_the_air, flying)])

    body = client.get("/").text
    assert cards(body) == ["2", "1"]
    assert rows(body) == []
    card = board_card(body, 1)
    assert "Cancelled" in card and not watched(card)


def test_a_cancelled_flight_says_so(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    show(monkeypatch, booking(), FlightSnapshot(id=3, booking_id=1, cancelled=True, raw={}))

    body = client.get("/f/1").text
    assert "Cancelled" in body


def test_the_newest_change_is_on_the_page_and_the_rest_are_behind_a_fold(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), full_snapshot())
    moved = (DEPARTURE + timedelta(minutes=20)).isoformat()
    client.session.rows["FlightEvent"] = [  # type: ignore[attr-defined]
        FlightEvent(id=2, booking_id=1, kind="GateChanged", old_value="B12", new_value="B27"),
        FlightEvent(
            id=1,
            booking_id=1,
            kind="DepartureDelayed",
            old_value=DEPARTURE.isoformat(),
            new_value=moved,
        ),
    ]

    body = client.get("/f/1").text
    assert "Gate changed" in body
    assert "B12" in body and "B27" in body
    assert "Departure delayed" in body
    # A stored instant is a time at the airport, never the ISO string it is kept as.
    assert moved not in body
    assert "Earlier changes" in body


def test_a_flight_that_is_not_there_is_a_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), None)

    page = client.get("/f/999")
    assert page.status_code == 404
    assert "No such flight." in page.text


def test_a_page_that_breaks_still_looks_like_the_app(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starlette's plain-text 500 is a dead end; this one has a way back to the board."""

    async def broken(_session: Any, **_kwargs: Any) -> list[Booking]:
        raise RuntimeError("the database went away")

    with build_client(settings, monkeypatch, raising=False) as client:
        monkeypatch.setattr(web.booking_repo, "list_bookings", broken)
        page = client.get("/")
    assert page.status_code == 500
    assert "Something went wrong." in page.text
    assert 'href="/"' in page.text


def test_adding_a_flight_asks_for_a_number_and_a_day_and_nothing_else(
    client: TestClient,
) -> None:
    page = client.get("/f/new")
    assert page.status_code == 200
    assert 'name="flight_number"' in page.text
    assert 'name="departure_date"' in page.text
    # The airports, the times and the operator are the airline's to state, not a
    # person's to copy off a ticket.
    for asked in ("origin_iata", "dest_iata", "departure_local"):
        assert asked not in page.text


def test_the_box_that_adds_a_flight_is_on_every_page_and_opens_on_its_own_address(
    client: TestClient,
) -> None:
    """The + is in every page's nav, so the box it opens is drawn under every page. It
    is shut everywhere but the address the + falls back to with no script to open it."""
    for path in ("/", "/mail", "/settings"):
        drawn = client.get(path).text
        assert 'id="add-flight"' in drawn, path
        assert re.search(r'<dialog\s+id="add-flight"[^>]*\bopen\b', drawn) is None, path

    standing = client.get("/f/new").text
    assert re.search(r'<dialog\s+id="add-flight"[^>]*\bopen\b', standing)
    # The board is still behind it: adding a flight is not a page of its own.
    assert 'id="flight-tabs"' in standing


def test_the_box_says_what_it_is_and_backing_out_of_it_is_the_x(client: TestClient) -> None:
    """One heading, two labelled boxes, and Add. Nothing in it explains a boarding pass
    to the person holding one, and the way out is the X every other dialog closes on."""
    body = " ".join(client.get("/f/new").text.split())
    assert '<h2 id="add-flight-title">Add Flight</h2>' in body
    assert '<label for="departure_date">Departure Date</label>' in body
    assert ">Add</button>" in body
    assert 'aria-label="Close dialog"' in body
    assert "Cancel" not in body
    assert "as the ticket writes them" not in body
    assert "read at the airport it leaves from" not in body


def records_creation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Catch what would be booked, so the suite stays off the database."""
    written: dict[str, Any] = {}

    async def create_booking(_session: Any, **fields: Any) -> Booking:
        written.update(fields)
        return booking()

    monkeypatch.setattr(web.booking_repo, "create_booking", create_booking)
    return written


def test_a_flight_number_that_names_one_flight_is_added_as_the_airline_states_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = looks_up(monkeypatch, [CANDIDATE])
    written = records_creation(monkeypatch)

    response = client.post(
        "/f/new",
        data={"flight_number": "ac 871", "departure_date": "2026-09-12"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/f/1"
    assert asked == [("AC", "871", date(2026, 9, 12))]
    # Every fact about the flight is the schedule's; nothing typed reached the booking.
    assert written["origin_iata"] == "YUL" and written["dest_iata"] == "LHR"
    assert written["departure_local"] == datetime(2026, 9, 12, 18, 40)
    assert written["arrival_local"] == datetime(2026, 9, 13, 10, 25)
    assert written["operating_carrier"] == "LH" and written["operating_number"] == "479"
    assert written["source"] == "manual"


def test_a_number_that_flies_twice_that_day_is_a_choice_rather_than_a_guess(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    evening = replace(CANDIDATE, departure_local=datetime(2026, 9, 12, 21, 15))
    looks_up(monkeypatch, [CANDIDATE, evening])
    written = records_creation(monkeypatch)

    body = client.post(
        "/f/new", data={"flight_number": "AC871", "departure_date": "2026-09-12"}
    ).text

    assert "flies more than once" in body
    assert "18:40" in body and "21:15" in body
    # Each choice posts the same two boxes back with the leg named, and nothing else.
    assert 'name="leg" value="YUL-LHR 18:40"' in body
    assert 'name="leg" value="YUL-LHR 21:15"' in body
    assert "origin_iata" not in body
    assert body.count("Operated as LH479") == 2
    assert written == {}


def test_choosing_a_leg_looks_the_number_up_again_and_adds_that_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The choice names a leg rather than carrying a flight, so what is saved is still
    what the schedule says at the moment of saving."""
    evening = replace(CANDIDATE, departure_local=datetime(2026, 9, 12, 21, 15))
    asked = looks_up(monkeypatch, [CANDIDATE, evening])
    written = records_creation(monkeypatch)

    response = client.post(
        "/f/new",
        data={"flight_number": "AC871", "departure_date": "2026-09-12", "leg": "YUL-LHR 21:15"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert asked == [("AC", "871", date(2026, 9, 12))]
    assert written["departure_local"] == datetime(2026, 9, 12, 21, 15)


def test_a_leg_the_schedule_no_longer_lists_is_offered_again_rather_than_guessed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    evening = replace(CANDIDATE, departure_local=datetime(2026, 9, 12, 21, 15))
    looks_up(monkeypatch, [CANDIDATE, evening])
    written = records_creation(monkeypatch)

    page = client.post(
        "/f/new",
        data={"flight_number": "AC871", "departure_date": "2026-09-12", "leg": "YUL-LHR 23:59"},
    )

    assert page.status_code == 400
    assert "no longer on the schedule" in page.text
    assert "flies more than once" in page.text
    assert written == {}


def test_a_flight_number_nobody_publishes_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    looks_up(monkeypatch, [])

    page = client.post("/f/new", data={"flight_number": "AC871", "departure_date": "2026-09-12"})

    assert page.status_code == 400
    assert "No AC871 is scheduled to leave that day." in page.text
    # There is no long way round: a flight the airline has not published is not added.
    assert "By hand" not in page.text
    # What was typed comes back with it, so the date does not have to be picked again.
    assert 'value="2026-09-12"' in page.text


@pytest.mark.parametrize(
    ("typed", "said"),
    [
        ({"flight_number": "YUL to LHR", "departure_date": "2026-09-12"}, "looks like AC871"),
        ({"flight_number": "AC871", "departure_date": "the 12th"}, "Pick the day"),
    ],
)
def test_what_cannot_be_looked_up_is_refused_before_anything_is_spent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, typed: dict[str, str], said: str
) -> None:
    asked = looks_up(monkeypatch, [CANDIDATE])

    page = client.post("/f/new", data=typed)

    assert page.status_code == 400
    assert said in page.text
    assert asked == []


def test_a_lookup_that_cannot_be_made_says_why(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    failures = [
        (BudgetExceeded(SPENT_BUDGET, just_tripped=False), "budget is spent"),
        (httpx.ConnectError("no route to host"), "did not answer"),
        (web.lookup.OutOfRange(date(2027, 12, 25)), "published a schedule that far off"),
    ]
    for raised, said in failures:

        async def broken(*_args: Any, _raised: Exception = raised, **_kwargs: Any) -> Any:
            raise _raised

        monkeypatch.setattr(web.lookup, "find_flights", broken)
        page = client.post(
            "/f/new", data={"flight_number": "AC871", "departure_date": "2026-09-12"}
        )

        assert page.status_code == 400
        assert said in page.text
        assert 'name="flight_number"' in page.text


def test_a_flight_already_on_the_list_is_said_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    looks_up(monkeypatch, [CANDIDATE])

    async def taken(*_args: Any, **_kwargs: Any) -> Booking:
        raise IntegrityError("INSERT INTO bookings", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(web.booking_repo, "create_booking", taken)

    page = client.post("/f/new", data={"flight_number": "AC871", "departure_date": "2026-09-12"})

    assert page.status_code == 400
    assert "already on the list for that day" in page.text


def test_without_a_flightaware_key_the_page_says_why_and_points_at_settings(
    unconfigured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with build_client(unconfigured, monkeypatch) as fresh:
        body = fresh.get("/f/new").text

    assert "FlightAware connection not configured" in body
    assert 'href="/settings"' in body
    assert 'name="flight_number"' not in body
    assert "By hand" not in body


# The dialog asks over fetch, so what comes back is what it needs to answer with: a
# flight to open, a refusal to draw beside the button, or the legs to choose between.
ASKED_BY_THE_DIALOG = {"Accept": "application/json"}


def test_the_flight_that_was_added_is_where_the_dialog_is_told_to_go(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fetch cannot be redirected onto a page, so the address is the answer."""
    looks_up(monkeypatch, [CANDIDATE])
    records_creation(monkeypatch)

    answer = client.post(
        "/f/new",
        data={"flight_number": "AC871", "departure_date": "2026-09-12"},
        headers=ASKED_BY_THE_DIALOG,
    )

    assert answer.status_code == 200
    assert answer.json() == {"location": "/f/1"}


def test_a_refusal_comes_back_as_the_line_that_goes_beside_the_button(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is added, so the dialog stays open on what was typed with the reason in it
    rather than being replaced by a page carrying the reason at the top."""
    looks_up(monkeypatch, [])

    answer = client.post(
        "/f/new",
        data={"flight_number": "AC871", "departure_date": "2026-09-12"},
        headers=ASKED_BY_THE_DIALOG,
    )

    assert answer.status_code == 400
    assert answer.json() == {"error": "No AC871 is scheduled to leave that day."}


def test_a_number_that_flies_twice_comes_back_as_the_legs_to_choose_between(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drawn by the server from the macro the page draws them with, so the choice inside
    the dialog is the same rows as the choice on a page."""
    evening = replace(CANDIDATE, departure_local=datetime(2026, 9, 12, 21, 15))
    looks_up(monkeypatch, [CANDIDATE, evening])
    written = records_creation(monkeypatch)

    answer = client.post(
        "/f/new",
        data={"flight_number": "AC871", "departure_date": "2026-09-12"},
        headers=ASKED_BY_THE_DIALOG,
    )

    assert answer.status_code == 200
    assert answer.json()["error"] is None
    choices = answer.json()["choices"]
    assert "flies more than once" in choices
    assert 'name="leg" value="YUL-LHR 18:40"' in choices
    assert 'name="leg" value="YUL-LHR 21:15"' in choices
    # The same two boxes go back with the leg named, and nothing about the flight does.
    assert 'name="flight_number" value="AC871"' in choices
    assert "origin_iata" not in choices
    assert written == {}


def test_a_flight_on_the_calendar_offers_a_way_into_the_calendar_app(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The push about an import goes on pointing here; this is the link the other way."""
    show(monkeypatch, booking(calendar_event_uid="flighter-1@flighter.invalid"), None)
    assert "calshow:" in client.get("/f/1").text


def test_a_flight_on_the_calendar_uses_the_scheme_macos_registers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(calendar_event_uid="flighter-1@flighter.invalid"), None)

    body = client.get(
        "/f/1",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 Version/18.6 Safari/605.1.15"
            )
        },
    ).text

    departure_day = to_local(DEPARTURE, "America/Toronto").strftime("%Y-%m-%d")
    assert f'href="ical://date/{departure_day}_12-00-00?method=show"' in body
    assert "calshow:" not in body


def test_an_ipad_in_desktop_mode_keeps_the_date_targeted_calendar_link(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(calendar_event_uid="flighter-1@flighter.invalid"), None)

    body = client.get(
        "/f/1",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) "
                "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
            )
        },
    ).text

    assert "calshow:" in body
    assert "ical://date/" not in body


def test_a_flight_that_is_not_on_the_calendar_offers_no_link(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), None)
    assert "calshow:" not in client.get("/f/1").text


def test_the_ticket_and_owner_are_what_the_page_lets_you_change(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(
        monkeypatch,
        booking(confirmations=[Confirmation("X7QW2P")], seat="14A"),
        empty_snapshot(),
    )

    body = client.get("/f/1").text

    assert 'action="/f/1/ticket"' in body
    assert 'action="/f/1/owner"' not in body
    for box in ("confirmation_code", "confirmation_name", "seat", "notes", "friend_name"):
        assert f'name="{box}"' in body
    assert re.search(r'name="owner" value="me"[^>]+ checked', body)
    assert 'name="owner" value="friend"' in body
    assert 'id="friend-name-field" role="group" class="field" hidden' in body
    assert re.search(r'name="friend_name"[^>]+ disabled', body)
    assert ">Me</aside>" in body
    assert 'value="X7QW2P"' in body and 'value="14A"' in body
    # The flight itself is the airline's statement, so nothing on the page edits it.
    for never in ("/f/1/edit", 'name="origin_iata"', 'type="datetime-local"', "Looks right"):
        assert never not in body


def test_every_confirmation_gets_a_row_of_its_own_under_the_name_it_is_filed_under(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two codes are two facts about the flight, and what tells them apart is whose
    booking each one is rather than the codes themselves."""
    show(
        monkeypatch,
        booking(
            confirmations=[
                Confirmation("X7QW2P", "Air Canada"),
                Confirmation("QQ4R5T"),
            ]
        ),
        empty_snapshot(),
    )

    body = client.get("/f/1").text

    assert "Confirmation (Air Canada)" in body
    assert "X7QW2P" in body and "QQ4R5T" in body
    # Each row copies its own code, so no two of them can share a target.
    assert 'id="confirmation-code-1"' in body and 'id="confirmation-code-2"' in body
    # Both are in the dialog too, each beside the name it is saved under.
    assert 'value="X7QW2P"' in body and 'value="Air Canada"' in body
    assert 'value="QQ4R5T"' in body


def test_a_flight_with_no_confirmation_still_has_a_row_and_a_box(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), empty_snapshot())

    body = client.get("/f/1").text

    assert "Confirmation" in body
    # Nothing to copy, and one empty pair of boxes to type the first code into.
    assert "confirmation-code-" not in body
    assert body.count('name="confirmation_code"') == 1


def test_the_friend_owner_is_selected_in_the_ticket_editor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(friend_name="Sam"), empty_snapshot())
    body = client.get("/f/1").text
    assert "Owner" in body and "Sam" in body
    assert 'class="friend-avatar flex size-4 shrink-0' in body
    assert 'name="friend_name"' in body and 'value="Sam"' in body
    assert re.search(r'name="owner" value="friend"[^>]+ checked', body)
    assert 'id="friend-name-field" role="group" class="field" hidden' not in body
    assert not re.search(r'name="friend_name"[^>]+ disabled', body)
    assert "Friend's name" not in body and "My flight" not in body


def test_the_service_worker_version_tracks_the_application_code(client: TestClient) -> None:
    body = client.get("/").text
    assert re.search(r'serviceWorker\.register\("/sw\.js\?v=[a-f0-9]{12}"\)', body)


def test_saving_the_ticket_hands_the_booking_layer_what_was_written(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = booking()
    show(monkeypatch, row, empty_snapshot())
    written: dict[str, Any] = {}

    async def update_ticket(_session: Any, booking_id: int, **fields: Any) -> Booking:
        written.update({"id": booking_id} | fields)
        return row

    monkeypatch.setattr(web.booking_repo, "update_ticket", update_ticket)

    response = client.post(
        "/f/1/ticket",
        data={
            "confirmation_code": [" X7QW2P ", "", "QQ4R5T"],
            "confirmation_name": ["Air Canada", "", ""],
            "seat": "14A",
            "notes": "",
            "friend_name": "  Sam  ",
            "return_tab": "friends",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/f/1?from=friends"
    # A blank box is nothing on the ticket, not an empty string in the database.
    assert written == {
        "id": 1,
        "confirmations": [
            Confirmation("X7QW2P", "Air Canada"),
            Confirmation("", None),
            Confirmation("QQ4R5T", None),
        ],
        "seat": "14A",
        "notes": None,
        "friend_name": "Sam",
    }


def test_saving_a_ticket_for_a_flight_that_is_not_there_is_a_404(client: TestClient) -> None:
    assert client.post("/f/9/ticket", data={"seat": "14A"}).status_code == 404


def test_deleting_a_flight_redirects_home(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = booking()
    show(monkeypatch, row, empty_snapshot())
    deleted: list[int] = []

    async def delete_booking(_session: Any, booking_id: int) -> Booking:
        deleted.append(booking_id)
        return row

    monkeypatch.setattr(web.booking_repo, "delete_booking", delete_booking)

    page = client.post("/f/1/delete", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/"
    assert deleted == [1]


def test_healthz_is_liveness_only(client: TestClient) -> None:
    page = client.get("/healthz")
    assert page.status_code == 200
    assert page.json() == {"status": "ok"}
    assert prefs.last_seen_origin() is None


def test_the_address_a_page_was_opened_on_is_kept_for_the_work_that_has_no_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefs, "_current", prefs.Prefs())
    merged: list[Any] = []
    remember = client.session.merge  # type: ignore[attr-defined]

    async def counted(instance: Any) -> Any:
        merged.append(instance)
        return await remember(instance)

    monkeypatch.setattr(client.session, "merge", counted)  # type: ignore[attr-defined]

    client.get("/")
    client.get("/f/new")

    assert prefs.public_base_url() == "http://testserver"
    assert [(row.key, row.value) for row in merged] == [
        (prefs.LAST_SEEN_ORIGIN_KEY, {"origin": "http://testserver"})
    ]


def test_clearing_the_address_hands_it_back_to_the_one_in_use(client: TestClient) -> None:
    """An empty override is how "work it out" is stored, so an emptied box has to reach
    the row: a field somebody cleared arrives looking exactly like one nobody posted."""
    client.post("/settings", data=SETTINGS_FORM | {"tab": "preferences"})
    assert prefs.current().public_base_url == "https://flights.example.com"

    client.post("/settings", data=SETTINGS_FORM | {"tab": "preferences", "public_base_url": ""})

    assert prefs.current().public_base_url == ""
    assert prefs.public_base_url("https://phone.example.com") == "https://phone.example.com"


def test_the_schema_is_not_published(client: TestClient) -> None:
    """Nothing writes against these routes, so a map of them is only ever a gift."""
    for path in ("/docs", "/openapi.json"):
        assert client.get(path).status_code == 404


# --- Settings ------------------------------------------------------------------------


def test_a_connection_that_is_not_made_yet_opens_on_the_way_in(
    unconfigured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows worth doing something about are the ones that are not done."""
    with build_client(unconfigured, monkeypatch) as fresh:
        rows = re.findall(r'<details class="setting"([^>]*)>', fresh.get("/settings").text)

    assert rows and all(" open" in row for row in rows)


def test_a_connection_already_made_stays_folded_away(client: TestClient) -> None:
    body = client.get("/settings").text
    rows = re.findall(r'<details class="setting"([^>]*)>', body)

    # Anthropic and Watchtower are the ones this deployment has not connected.
    assert [" open" in row for row in rows] == [False, False, False, True, True]


def test_a_connection_not_made_yet_cannot_be_saved_without_its_credentials(
    unconfigured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty box means "leave it alone" only once there is something to leave: on a
    connection nobody has made, saving one that is still empty connects nothing."""
    with build_client(unconfigured, monkeypatch) as fresh:
        body = fresh.get("/settings").text

    for name in ("aeroapi_key", "icloud_app_password", "pushover_token"):
        assert re.search(rf'<input[^>]*id="{name}"[^>]*required', body)
    # The limit is not a credential, and empty is a setting it can be left on.
    limit = re.search(r'<input[^>]*id="aeroapi_monthly_cap_usd"[^>]*>', body)
    assert limit is not None
    assert "required" not in limit.group()


def test_a_credential_on_file_says_what_leaving_its_box_empty_means(
    client: TestClient,
) -> None:
    body = client.get("/settings").text

    # The four secrets on file: the AeroAPI key, the app-specific password, and
    # Pushover's two. The Apple ID is shown back, and Anthropic is not connected.
    assert body.count("(leave empty to keep saved value)") == 4
    # Nothing is asked for again on a connection that is already made.
    assert not re.search(r'<input[^>]*id="icloud_app_password"[^>]*required', body)


def test_the_settings_page_shows_what_is_connected(client: TestClient) -> None:
    body = client.get("/settings").text
    # The Apple ID names the account rather than proving anything, so it is shown back.
    assert "someone@icloud.com" in body
    assert "Flights" in body
    # The token is handed to your own phone, so the page carries it: in the Connect link,
    # and in the clear for trying the endpoint by hand.
    assert (
        'href="scriptable:///run/Flighter?api=https%3A%2F%2Fflights.example.com&amp;token=test-token"'
        in body
    )
    assert ">test-token</pre>" in body


def test_the_widget_tab_says_whether_a_phone_has_fetched(client: TestClient) -> None:
    body = client.get("/settings").text
    assert "No widget has fetched flights yet" in body
    assert "Not connected" in body

    recent = datetime.now(UTC) - timedelta(minutes=4)
    client.session.rows["KV"] = [  # type: ignore[attr-defined]
        KV(key=LAST_SEEN_KEY, value={"at": recent.strftime("%Y-%m-%dT%H:%M:%SZ")})
    ]
    body = client.get("/settings").text
    assert "Last fetched by a widget <strong>4m ago</strong>" in body


def test_a_phone_not_heard_from_in_a_day_is_not_connected(client: TestClient) -> None:
    """iOS skips reloads for hours at a time; only a whole day of silence means broken."""
    long_ago = datetime.now(UTC) - timedelta(days=3, hours=2)
    client.session.rows["KV"] = [  # type: ignore[attr-defined]
        KV(key=LAST_SEEN_KEY, value={"at": long_ago.strftime("%Y-%m-%dT%H:%M:%SZ")})
    ]
    body = client.get("/settings").text
    assert "Last fetched by a widget <strong>3d ago</strong>" in body
    assert "Not connected" in body


def test_regenerating_the_token_lands_back_on_the_widget_tab(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    minted: list[bool] = []
    monkeypatch.setattr(web, "mint_widget_token", lambda: minted.append(True))

    response = client.post("/settings/widget/token", follow_redirects=False)

    assert minted == [True]
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=widget"
    body = client.get("/settings?tab=widget").text
    assert re.search(
        r'id="settings-tabs-tab-3"\s+aria-controls="[^"]+"\s+aria-selected="true"', body
    )
    assert re.search(
        r'id="settings-tabs-tab-1"\s+aria-controls="[^"]+"\s+aria-selected="false"', body
    )


def test_the_install_copy_fallback_carries_the_whole_script(client: TestClient) -> None:
    """Plain http has no clipboard API, so the script is on the page to be selected."""
    body = client.get("/settings").text
    assert 'id="widget-script"' in body
    assert "Keychain.set(TOKEN_KEY" in body


def test_the_limit_is_set_on_the_account_it_is_a_limit_on(client: TestClient) -> None:
    """It is what FlightAware is allowed to cost, so it is drawn with the FlightAware
    account rather than among the preferences."""
    accounts = client.get("/settings").text.split('id="settings-tabs-panel-2"')[0]
    assert "Monthly spend limit" in accounts
    assert 'name="aeroapi_monthly_cap_usd"' in accounts


def test_no_stored_credential_is_ever_rendered_back(client: TestClient) -> None:
    body = client.get("/settings").text
    for secret in ("abcd-efgh-ijkl-mnop", "test-key", "app-token", "user-key"):
        assert secret not in body
    # What is shown instead is that they are there, and a box to replace them.
    assert body.count("Connected") >= 3
    assert 'name="icloud_app_password"' in body


def test_a_fresh_deployment_is_told_what_to_do_in_order(
    unconfigured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefs, "_current", prefs.Prefs())
    with build_client(unconfigured, monkeypatch) as fresh:
        body = fresh.get("/settings").text
    # The badges are the signposting: every account says it is not connected, and they
    # are drawn in the order they have to be done in.
    at = [body.index(name) for name in ("FlightAware", "iCloud", "Pushover")]
    assert at == sorted(at)
    assert body.count("Not connected") >= 3
    # The feed is the one there is no board without, so it asks louder than the rest.
    assert "Required" in body


def test_a_deployment_nobody_has_told_its_address_is_offered_this_one(
    unconfigured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is read on the phone, so the address the page was opened on is the likely one."""
    monkeypatch.setattr(prefs, "_current", prefs.Prefs())
    with build_client(unconfigured, monkeypatch) as fresh:
        body = fresh.get("/settings").text
    # The box is the override and stays empty; what was worked out is offered in it.
    assert 'placeholder="http://testserver"' in body
    # The Connect link too: a phone handed the default would be told to ask itself.
    assert "api=http%3A%2F%2Ftestserver" in body
    assert "localhost:8000" not in body
    assert "localhost%3A8000" not in body


def test_an_address_that_was_set_is_left_alone(client: TestClient) -> None:
    assert 'value="https://flights.example.com"' in client.get("/settings").text


def test_an_account_that_is_set_up_stops_asking_to_be_connected(
    client: TestClient,
) -> None:
    """The same badges, the other way round: nothing is left saying it needs doing."""
    accounts = client.get("/settings").text.split('id="settings-tabs-panel-2"')[0]
    assert accounts.count("Connected") >= 3
    # The one the app cannot do without is no longer being asked for.
    assert "Required" not in accounts


def test_only_what_was_typed_in_is_written(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = record_secrets(monkeypatch)

    response = client.post(
        "/settings/credentials",
        data={"service": "icloud", "icloud_email": " me@icloud.com ", "icloud_app_password": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    # A blank box means "leave it alone", never "clear it": nothing was shown to leave in.
    assert written == [{"icloud_email": "me@icloud.com"}]


def test_a_form_with_nothing_typed_in_writes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = record_secrets(monkeypatch)
    client.post("/settings/credentials", data={"service": "pushover"})
    assert written == []


def test_forgetting_a_connection_clears_every_credential_it_needs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = record_secrets(monkeypatch)
    client.post("/settings/credentials", data={"service": "pushover", "forget": "1"})
    assert written == [{"pushover_token": "", "pushover_user_key": ""}]


# Written through the route rather than set on the live copy, so the row a removal merges
# into is a deployment that really had these on.
RUNNING = {
    "calendar_sync_enabled": "true",
    "email_import_enabled": "true",
    "notifications_enabled": "true",
}


def test_forgetting_icloud_puts_down_the_jobs_that_ran_on_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A switch left on for an account that is gone is one nothing can honour."""
    record_secrets(monkeypatch)
    client.post("/settings", data=RUNNING)

    client.post("/settings/credentials", data={"service": "icloud", "forget": "1"})

    assert not prefs.current().calendar_sync_enabled
    assert not prefs.current().email_import_enabled
    # The account that was not removed keeps what runs on it.
    assert prefs.current().notifications_enabled


def test_forgetting_pushover_puts_down_the_notifications(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_secrets(monkeypatch)
    client.post("/settings", data=RUNNING)

    client.post("/settings/credentials", data={"service": "pushover", "forget": "1"})

    assert not prefs.current().notifications_enabled
    assert prefs.current().calendar_sync_enabled
    assert prefs.current().email_import_enabled


def test_forgetting_an_account_nothing_runs_on_puts_down_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_secrets(monkeypatch)
    client.post("/settings", data=RUNNING)

    client.post("/settings/credentials", data={"service": "anthropic", "forget": "1"})

    assert prefs.current().calendar_sync_enabled
    assert prefs.current().email_import_enabled
    assert prefs.current().notifications_enabled


def test_details_the_service_will_not_take_are_not_kept(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Written to be tried, then put back: the check can only read what is on file."""
    written = record_secrets(monkeypatch)

    async def refuses(_settings: Any, _service: str) -> CheckResult:
        return CheckResult("pushover", False, "token rejected")

    monkeypatch.setattr(web, "check_service", refuses)

    response = client.post(
        "/settings/credentials",
        data={"service": "pushover", "pushover_token": "wrong", "pushover_user_key": "u"},
    )

    assert response.status_code == 400
    assert "Pushover: token rejected" in response.text
    # What it tried, and then what was there before it tried.
    assert written == [
        {"pushover_token": "wrong", "pushover_user_key": "u"},
        {"pushover_token": "app-token", "pushover_user_key": "user-key"},
    ]


def test_a_refusal_comes_back_as_a_reason_the_page_can_place(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asked for over fetch, so the answer lands beside the button that was pressed."""
    record_secrets(monkeypatch)

    async def refuses(_settings: Any, _service: str) -> CheckResult:
        return CheckResult("aeroapi", False, "key rejected")

    monkeypatch.setattr(web, "check_service", refuses)

    response = client.post(
        "/settings/credentials",
        data={"service": "flightaware", "aeroapi_key": "nope"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "key rejected"}


def test_a_save_that_works_says_so_without_a_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_secrets(monkeypatch)

    response = client.post(
        "/settings/credentials",
        data={"service": "flightaware", "aeroapi_key": "k"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_details_the_service_takes_are_kept(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = record_secrets(monkeypatch)

    response = client.post(
        "/settings/credentials",
        data={"service": "flightaware", "aeroapi_key": "k"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert written == [{"aeroapi_key": "k"}]


def test_forgetting_is_never_refused(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential being thrown away does not have to work first."""
    written = record_secrets(monkeypatch)

    async def refuses(_settings: Any, _service: str) -> CheckResult:
        raise AssertionError("a removal must not be checked")

    monkeypatch.setattr(web, "check_service", refuses)

    response = client.post(
        "/settings/credentials",
        data={"service": "pushover", "forget": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert written == [{"pushover_token": "", "pushover_user_key": ""}]


def test_a_calendar_that_does_not_answer_is_not_saved(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proved before it is stored, so nothing has to be undone when it fails."""

    async def refuses(_settings: Any, _url: str | None = None) -> CheckResult:
        return CheckResult("calendar", False, "no longer on this account")

    monkeypatch.setattr(web, "check_calendar", refuses)
    before = prefs.current().icloud_calendar_url

    response = client.post(
        "/settings", data={"icloud_calendar_url": CALENDARS[1].url, "tab": "connections"}
    )

    assert response.status_code == 400
    assert "Calendar: no longer on this account" in response.text
    assert prefs.current().icloud_calendar_url == before


def test_writing_to_no_calendar_is_a_choice_rather_than_a_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuses(_settings: Any, _url: str | None = None) -> CheckResult:
        raise AssertionError("nothing to prove when nothing was picked")

    monkeypatch.setattr(web, "check_calendar", refuses)

    response = client.post("/settings", data={"icloud_calendar_url": "", "tab": "preferences"})

    assert response.status_code == 200
    assert prefs.current().icloud_calendar_url == ""


def test_a_connection_that_does_not_exist_is_a_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_secrets(monkeypatch)
    assert client.post("/settings/credentials", data={"service": "gmail"}).status_code == 404


def test_saving_a_preference_redirects_and_takes_effect(client: TestClient) -> None:
    response = client.post("/settings", data=SETTINGS_FORM | {"log_level": "DEBUG"})
    assert response.status_code == 200
    assert response.url.path == "/settings"
    assert prefs.current().log_level == "DEBUG"


def test_friend_integration_settings_are_visible_and_saved(client: TestClient) -> None:
    body = client.get("/settings").text
    names = (
        "sync_friend_flights_to_calendar",
        "notify_for_friend_flights",
        "show_friend_flights_in_widget",
    )
    assert all(f'name="{name}"' in body for name in names)

    client.post(
        "/settings",
        data={name: "true" for name in names} | {"tab": "preferences"},
    )
    current = prefs.current()
    assert current.sync_friend_flights_to_calendar
    assert current.notify_for_friend_flights
    assert current.show_friend_flights_in_widget


def test_a_friend_setting_left_unticked_is_turned_off(client: TestClient) -> None:
    """A cleared tickbox posts nothing, so the form it came from is what makes it mean off."""
    client.post(
        "/settings",
        data={
            "notify_for_friend_flights": "true",
            "show_friend_flights_in_widget": "true",
            "tab": "preferences",
        },
    )
    assert prefs.current().notify_for_friend_flights

    client.post("/settings", data={"tab": "preferences"})

    assert not prefs.current().notify_for_friend_flights
    assert not prefs.current().show_friend_flights_in_widget


def test_a_form_without_the_tickboxes_on_it_leaves_them_alone(client: TestClient) -> None:
    """Only the preferences form carries them, so only it may read one as unticked."""
    client.post("/settings", data={"notify_for_friend_flights": "true", "tab": "preferences"})

    client.post("/settings", data={"icloud_calendar_url": FLIGHTS_CALENDAR, "tab": "connections"})

    assert prefs.current().notify_for_friend_flights


def test_disabling_friend_calendar_sync_removes_existing_events(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    friend = booking(friend_name="Sam", calendar_event_uid="flighter-1@flighter.invalid")
    client.session.rows["Booking"] = [friend]  # type: ignore[attr-defined]
    monkeypatch.setattr(
        prefs,
        "_current",
        prefs.current().model_copy(update={"sync_friend_flights_to_calendar": True}),
    )
    deleted: list[int] = []

    async def delete(_calendar: Any, row: Booking) -> bool:
        deleted.append(row.id)
        return True

    monkeypatch.setattr(web.CalendarClient, "delete", delete)

    response = client.post(
        "/settings",
        data={
            "sync_friend_flights_to_calendar": "false",
            "calendar_sync_enabled": "true",
            "tab": "preferences",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert deleted == [1]
    assert friend.calendar_event_uid is None
    assert not prefs.current().sync_friend_flights_to_calendar


def test_the_notification_switches_are_drawn_and_saved(client: TestClient) -> None:
    body = client.get("/settings").text
    assert all(f'name="{field}"' in body for field, _ in web.NOTIFICATION_CHOICES)
    assert 'name="notifications_enabled"' in body

    client.post(
        "/settings",
        data={"notifications_enabled": "true", "notify_gate_changes": "true", "tab": "preferences"},
    )

    current = prefs.current()
    assert current.notifications_enabled
    assert current.notify_gate_changes
    # Everything else on the card was posted empty, which on this form means off.
    assert not current.notify_disruptions
    assert not current.notify_imports


def test_the_two_card_switches_start_on_and_can_be_turned_off(client: TestClient) -> None:
    """A deployment nobody has configured still reads its mail and writes its calendar."""
    assert prefs.current().email_import_enabled
    assert prefs.current().calendar_sync_enabled

    client.post("/settings", data={"tab": "preferences"})

    assert not prefs.current().email_import_enabled
    assert not prefs.current().calendar_sync_enabled


def test_turning_calendar_sync_off_removes_every_flights_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only the friends' ones: a calendar left holding flights nothing will ever
    update again is worse than one holding none."""
    mine = booking(calendar_event_uid="flighter-1@flighter.invalid")
    client.session.rows["Booking"] = [mine]  # type: ignore[attr-defined]
    deleted: list[int] = []

    async def delete(_calendar: Any, row: Booking) -> bool:
        deleted.append(row.id)
        return True

    monkeypatch.setattr(web.CalendarClient, "delete", delete)

    client.post("/settings", data={"tab": "preferences"}, follow_redirects=False)

    assert deleted == [mine.id]
    assert mine.calendar_event_uid is None
    assert not prefs.current().calendar_sync_enabled


def test_a_save_comes_back_to_the_tab_it_was_made_on(client: TestClient) -> None:
    """The form says which tab it is on, so a save never moves the page out from under."""
    response = client.post(
        "/settings", data=SETTINGS_FORM | {"tab": "preferences"}, follow_redirects=False
    )

    assert response.headers["location"] == "/settings?tab=preferences"
    body = client.get("/settings?tab=preferences").text
    assert re.search(
        r'id="settings-tabs-tab-2"\s+aria-controls="[^"]+"\s+aria-selected="true"', body
    )


def test_a_tab_nobody_drew_is_the_first_one(client: TestClient) -> None:
    """The name goes straight into a Location header, so only ours may travel in it."""
    response = client.post(
        "/settings", data=SETTINGS_FORM | {"tab": "../evil"}, follow_redirects=False
    )

    assert response.headers["location"] == "/settings?tab=connections"


def test_a_credential_comes_back_to_the_connections_tab(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_secrets(monkeypatch)

    response = client.post(
        "/settings/credentials",
        data={"service": "flightaware", "aeroapi_key": "k"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/settings?tab=connections"


def test_a_refused_preference_stays_on_the_tab_it_was_typed_on(client: TestClient) -> None:
    """An error is worth nothing on a tab that is not showing the box it is about."""
    response = client.post(
        "/settings",
        data=SETTINGS_FORM | {"tab": "preferences", "imap_flag_colour": "chartreuse"},
    )

    assert response.status_code == 400
    assert re.search(
        r'id="settings-tabs-tab-2"\s+aria-controls="[^"]+"\s+aria-selected="true"',
        response.text,
    )


def test_a_card_that_posts_one_field_leaves_the_others_alone(client: TestClient) -> None:
    """Each card is its own form, so what arrives is a slice rather than the lot."""
    client.post("/settings", data=SETTINGS_FORM | {"log_level": "WARNING"})

    client.post("/settings", data={"icloud_calendar_url": CALENDARS[1].url})

    assert prefs.current().icloud_calendar_url == CALENDARS[1].url
    assert prefs.current().log_level == "WARNING"


def test_a_typo_in_the_limit_is_refused_beside_the_button_that_was_pressed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_secrets(monkeypatch)

    response = client.post(
        "/settings/credentials",
        data={"service": "flightaware", "aeroapi_monthly_cap_usd": "four dollars"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400
    assert "Monthly spend limit" in response.json()["error"]
    assert prefs.current().aeroapi_monthly_cap_usd == Decimal("4.00")


def test_the_limit_is_saved_by_the_button_that_saves_the_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is about this account and nothing else, so it is set where the key is."""
    record_secrets(monkeypatch)

    client.post(
        "/settings/credentials",
        data={"service": "flightaware", "aeroapi_monthly_cap_usd": "2.50"},
    )

    assert prefs.current().aeroapi_monthly_cap_usd == Decimal("2.50")


def test_an_empty_limit_is_no_limit_and_lets_polling_start_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty is a choice here rather than a box left alone, and a deployment that has
    just said "no limit" cannot be left stopped by a breaker latched against one."""
    record_secrets(monkeypatch)
    latch = KV(key=BREAKER_KEY, value={"month": "2026-08"})
    client.session.rows["KV"] = [latch]  # type: ignore[attr-defined]

    client.post(
        "/settings/credentials",
        data={"service": "flightaware", "aeroapi_monthly_cap_usd": ""},
    )

    assert prefs.current().aeroapi_monthly_cap_usd is None
    assert client.session.deleted == [latch]  # type: ignore[attr-defined]


def test_another_account_does_not_carry_the_limit_away(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A form that does not draw the box cannot be read as having emptied it."""
    record_secrets(monkeypatch)

    client.post("/settings/credentials", data={"service": "pushover", "pushover_token": "t"})

    assert prefs.current().aeroapi_monthly_cap_usd == Decimal("4.00")


def test_the_settings_page_offers_every_usable_flag_colour(client: TestClient) -> None:
    """Red is the one Apple sends unmarked, so it is not on the list to be chosen."""
    body = client.get("/settings").text
    for colour in ("orange", "yellow", "green", "blue", "purple", "grey"):
        assert f'value="{colour}"' in body
        assert f'data-colour="{colour}"' in body
    assert 'value="red"' not in body


def test_a_flag_colour_the_app_cannot_watch_for_is_refused(client: TestClient) -> None:
    response = client.post("/settings", data=SETTINGS_FORM | {"imap_flag_colour": "red"})
    assert response.status_code == 400
    assert "imap flag colour" in response.text


def test_picking_a_calendar_turns_the_sync_on(client: TestClient) -> None:
    client.post("/settings", data={"icloud_calendar_url": ""})
    assert not prefs.current().calendar_configured

    client.post("/settings", data={"icloud_calendar_url": CALENDARS[1].url})
    assert prefs.current().icloud_calendar_url == CALENDARS[1].url
    assert prefs.current().calendar_configured


def test_the_picker_offers_the_calendars_the_account_has(client: TestClient) -> None:
    """Fetched into the select after the page, so the page never waits on iCloud."""
    body = client.get("/settings/calendars").text
    for calendar in CALENDARS:
        assert f'value="{calendar.url}"' in body
        assert f">{calendar.name}<" in body
    # And the page itself carries the control but none of its options.
    assert 'id="icloud_calendar_url"' in client.get("/settings").text


def test_the_calendars_are_not_asked_for_until_there_is_an_account(
    unconfigured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery is a network call on a page render, so it waits for a reason to make it."""
    asked = False

    async def counted(self: Any) -> list[Collection]:
        nonlocal asked
        asked = True
        return list(CALENDARS)

    monkeypatch.setattr(web.CalendarClient, "calendars", counted)
    with build_client(unconfigured, monkeypatch) as fresh:
        body = fresh.get("/settings").text
        options = fresh.get("/settings/calendars").text

    assert asked is False
    assert "Connect iCloud in" in body
    assert CALENDARS[0].url not in options


def test_neither_job_can_be_switched_on_without_the_account_it_runs_on(
    unconfigured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every job runs on an account, so with none connected no switch can be flicked."""
    with build_client(unconfigured, monkeypatch) as fresh:
        body = fresh.get("/settings").text

    for field in ("calendar_sync_enabled", "email_import_enabled", "notifications_enabled"):
        switch = re.search(rf'<input[^>]*id="{field}"[^>]*>', body)
        assert switch is not None
        assert "disabled" in switch.group()
        assert "checked" not in switch.group()
    assert body.count("Connect iCloud in") == 2
    assert body.count("Connect Pushover in") == 1
    # Three ways to Connections from the cards that are waiting on one, and a fourth in
    # the add dialog every page carries, which has no feed to look a flight up in either.
    assert body.count('href="/settings?tab=connections"') == 4


def test_a_picker_with_nothing_stored_opens_on_the_first_calendar(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A calendar showing in the picker is one the page then stores, so it is never a
    name on screen that nothing behind it agrees with."""
    monkeypatch.setattr(prefs, "_current", prefs.Prefs(icloud_calendar_url=""))

    options = client.get("/settings/calendars").text

    first, second = CALENDARS[0], CALENDARS[1]
    assert re.search(rf'value="{re.escape(first.url)}"\s+selected\s+data-default', options)
    assert "data-default" not in options.split(second.url)[1]


def test_the_settings_page_still_opens_when_icloud_cannot_be_reached(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other preference on the page is still editable, and the reason is in the picker."""

    async def unreachable(self: Any) -> list[Collection]:
        raise CalendarUnavailable("cannot reach caldav.icloud.com")

    monkeypatch.setattr(web.CalendarClient, "calendars", unreachable)

    page = client.get("/settings")
    assert page.status_code == 200
    assert 'name="imap_flag_colour"' in page.text

    options = client.get("/settings/calendars")
    assert options.status_code == 200
    # A sentence about what to do, not the repr of whatever was raised.
    assert "Check the iCloud connection under Connections." in options.text
    assert "CalendarUnavailable" not in options.text
    # Marked so the picker stays shut rather than offering the failure as a choice.
    assert "data-error" in options.text


def test_a_diverted_flight_names_where_it_is_going_instead(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent_elsewhere = replace_snapshot(
        diverted=True,
        destination_iata="MAN",
        actual_out=DEPARTURE,
        actual_off=DEPARTURE + timedelta(minutes=12),
        estimated_in=ARRIVAL + timedelta(minutes=50),
    )
    show(monkeypatch, booking(), sent_elsewhere)
    page = client.get("/f/1")
    body = page.text
    assert "<title>AC871 YUL to MAN</title>" in body
    card = top_card(body)
    assert "Manchester" in card
    # The new airport stands where the code goes, and the booked one is behind a tap on
    # it rather than struck beside it.
    assert '<div class="replaced text-stop-soft"' in card
    assert 'class="replaced-now font-mono text-2xl font-bold tracking-tight">MAN</div>' in card
    assert struck("LHR") in card
    assert card.count("LHR") == 1
    assert "Diverted to" not in card
    assert "Lands in" in body
    # The board row follows the aircraft too.
    show_board(monkeypatch, [(booking(), sent_elsewhere)])
    assert "MAN" in client.get("/").text


def test_the_footer_carries_the_words_for_when_its_time_has_passed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    landed = replace_snapshot(
        actual_out=DEPARTURE,
        actual_off=DEPARTURE + timedelta(minutes=12),
        actual_on=ARRIVAL - timedelta(minutes=10),
        estimated_in=ARRIVAL,
    )
    show(monkeypatch, booking(), landed)
    body = client.get("/f/1").text
    assert ">At the gate in</span>" in body
    assert 'data-due="Due at the gate"' in body
    # The belt waits for the gate: until then the footer is the countdown to it.
    assert "Baggage claim" not in body


def belt(value: str) -> str:
    """The footer once parked: the words Baggage claim, then the carousel where the time was."""
    shown = "" if value != "-" else " text-muted-foreground"
    return (
        '<span class="text-sm text-muted-foreground">Baggage claim</span>\n'
        f'    <span class="ml-auto font-mono text-lg font-bold{shown}">{value}</span>'
    )


def test_a_landed_flight_whose_gate_time_has_passed_shows_the_belt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On-blocks is the one word the feed often never sends, so the clock stands in."""
    left = NOW - timedelta(hours=7)
    overdue = replace_snapshot(
        actual_out=left,
        actual_off=left + timedelta(minutes=12),
        actual_on=NOW - timedelta(minutes=25),
        estimated_in=NOW - timedelta(minutes=8),
        baggage_claim="7",
    )
    show(
        monkeypatch,
        booking(scheduled_departure_utc=left, scheduled_arrival_utc=NOW - timedelta(minutes=8)),
        overdue,
    )
    body = client.get("/f/1").text
    # The pill says only what the feed has said; the footer says where to walk.
    assert ">Landed</span>" in body and "Arrived" not in body
    assert belt("7") in body and "<time" not in body


def test_a_parked_flight_with_no_belt_on_file_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = NOW - timedelta(hours=7)
    parked = replace_snapshot(
        actual_out=left,
        actual_off=left + timedelta(minutes=12),
        actual_on=NOW - timedelta(minutes=40),
        actual_in=NOW - timedelta(minutes=30),
    )
    show(
        monkeypatch,
        booking(scheduled_departure_utc=left, scheduled_arrival_utc=NOW - timedelta(minutes=30)),
        parked,
    )
    body = client.get("/f/1").text
    assert "Arrived" in body and belt("-") in body


def test_a_flight_the_feed_lost_has_no_footer_to_grow(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closed by the poller with the last snapshot still saying airborne."""
    left = NOW - timedelta(days=3)
    lost = replace_snapshot(
        actual_out=left,
        actual_off=left + timedelta(minutes=12),
        estimated_on=left + timedelta(hours=6),
        progress_percent=40,
    )
    show(
        monkeypatch,
        booking(
            scheduled_departure_utc=left,
            scheduled_arrival_utc=left + timedelta(hours=6),
            status=BookingStatus.COMPLETED,
        ),
        lost,
    )
    body = client.get("/f/1").text
    card = body[body.index('<div class="card gap-5">') : body.index('<div class="card mt-3"')]
    assert "Flown" in card and "In the air" not in card
    assert "<footer" not in card and "Due to land" not in card
    assert "--progress: 100%" in card and "data-off" not in card


def test_a_flight_nobody_has_heard_about_for_days_has_no_footer_either(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Still active, because nothing polled it: no key, or a tripped budget."""
    left = NOW - timedelta(days=3)
    show(
        monkeypatch,
        booking(scheduled_departure_utc=left, scheduled_arrival_utc=left + timedelta(hours=6)),
        None,
    )
    body = client.get("/f/1").text
    card = body[body.index('<div class="card gap-5">') : body.index('<div class="card mt-3"')]
    assert "<footer" not in card and "Due to depart" not in card


def test_a_flight_just_at_the_gate_stays_on_the_board_a_while(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = NOW - timedelta(hours=7)
    parked = replace_snapshot(
        actual_out=left,
        actual_off=left + timedelta(minutes=12),
        actual_on=NOW - timedelta(minutes=40),
        actual_in=NOW - timedelta(minutes=30),
        baggage_claim="7",
    )
    show(
        monkeypatch,
        booking(scheduled_departure_utc=left, scheduled_arrival_utc=NOW - timedelta(minutes=30)),
        parked,
    )
    body = client.get("/").text
    assert 'class="card' in body
    assert rows(body) == []
    assert "Arrived" in body and belt("7") in body


def test_a_flight_long_at_the_gate_is_filed_under_flown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = NOW - timedelta(hours=10)
    parked = replace_snapshot(
        actual_out=left, actual_off=left, actual_on=left, actual_in=NOW - timedelta(hours=3)
    )
    show(
        monkeypatch,
        booking(scheduled_departure_utc=left, scheduled_arrival_utc=NOW - timedelta(hours=3)),
        parked,
    )
    body = client.get("/").text
    assert "Flown" in body and 'class="card' not in body
    assert rows(body) == ["1"]
    assert 'id="flight-tabs-panel-3"' in body


def test_the_updates_row_stands_without_watchtower(client: TestClient) -> None:
    """Checking only takes the registry, so the row is there before any connection -
    and it says so, so the page's script holds the Update button back."""
    body = client.get("/settings").text
    assert "Watchtower" in body
    assert "Check for updates" in body
    assert 'id="update-form"' in body
    assert 'data-watchtower=""' in body


def test_a_connected_watchtower_is_marked_on_the_row(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.watchtower_url = "http://watchtower:8080"
    settings.watchtower_token = "wt-api-token-value"
    with build_client(settings, monkeypatch) as client:
        body = client.get("/settings").text
    assert 'data-watchtower="1"' in body
    # The address is shown back the way the Apple ID is; the token never is.
    assert "http://watchtower:8080" in body
    assert "wt-api-token-value" not in body


def test_the_update_check_says_what_runs_and_what_is_published(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def checked(
        *, refresh: bool = True, force: bool = False, transport: Any = None
    ) -> updates.UpdateStatus:
        assert refresh and not force
        return updates.UpdateStatus(running="a" * 40, latest="b" * 40, checked=1.0)

    monkeypatch.setattr(updates, "status", checked)
    answer = client.get("/settings/update/check").json()
    assert answer["available"] is True
    assert answer["running"] == "a" * 40 and answer["latest"] == "b" * 40
    # What the poll after an update actually watches: any new image changes it.
    assert answer["build"]


def test_the_check_button_goes_past_the_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def checked(
        *, refresh: bool = True, force: bool = False, transport: Any = None
    ) -> updates.UpdateStatus:
        assert refresh and force
        return updates.UpdateStatus(running="a" * 40, latest="a" * 40, checked=1.0)

    monkeypatch.setattr(updates, "status", checked)
    answer = client.get("/settings/update/check?force=1").json()
    assert answer["available"] is False


def test_the_poll_stays_off_the_registry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unchecked(
        *, refresh: bool = True, force: bool = False, transport: Any = None
    ) -> updates.UpdateStatus:
        assert not refresh
        return updates.UpdateStatus(running="")

    monkeypatch.setattr(updates, "status", unchecked)
    answer = client.get("/settings/update/check?poll=1").json()
    assert answer["available"] is None


def test_an_update_without_watchtower_is_refused(client: TestClient) -> None:
    response = client.post("/settings/update", headers={"Accept": "application/json"})
    assert response.status_code == 400
    assert "Connect Watchtower" in response.json()["detail"]


def test_an_update_is_handed_to_watchtower(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.watchtower_url = "http://watchtower:8080"
    settings.watchtower_token = "secret"

    async def restarting(handed: Settings) -> updates.Outcome:
        assert handed is settings
        return updates.Outcome(True, "Watchtower is updating.", restarting=True)

    monkeypatch.setattr(updates, "trigger", restarting)
    with build_client(settings, monkeypatch) as client:
        answer = client.post("/settings/update", headers={"Accept": "application/json"})
    assert answer.status_code == 200
    assert answer.json() == {"ok": True, "restarting": True, "detail": "Watchtower is updating."}


def test_a_watchtower_refusal_lands_on_the_page_for_a_browser_with_no_script(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.watchtower_url = "http://watchtower:8080"
    settings.watchtower_token = "secret"

    async def refused(handed: Settings) -> updates.Outcome:
        return updates.Outcome(False, "Watchtower rejected the token.")

    monkeypatch.setattr(updates, "trigger", refused)
    with build_client(settings, monkeypatch) as client:
        response = client.post("/settings/update")
    assert response.status_code == 502
    assert "Watchtower rejected the token." in response.text
