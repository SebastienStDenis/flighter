"""The pages, rendered against a faked data layer.

Nothing here touches the database or the network. The interesting risk in a template is a
null: gates, baggage belts and every estimate are absent for most of a flight's life,
and a page that raises on one of them is a page that fails exactly when it is needed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from flighter import prefs, web
from flighter.aeroapi import BREAKER_KEY, BudgetExceeded, BudgetStatus
from flighter.airports import UnknownAirport
from flighter.caldav import CalendarUnavailable, Collection
from flighter.config import Settings
from flighter.db import get_session
from flighter.lookup import Candidate
from flighter.models import KV, Airport, Booking, FlightEvent, FlightSnapshot, IngestLog
from flighter.widget import LAST_SEEN_KEY

NOW = datetime.now(UTC)
DEPARTURE = NOW + timedelta(days=2)
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
}

# Every field the preferences form posts, so a test can change one of them.
SETTINGS_FORM = {
    "public_base_url": "https://flights.example.com",
    "log_level": "INFO",
    "aeroapi_monthly_cap_usd": "4.00",
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

    async def fake_airport_tz(_session: Any, iata: str) -> str:
        airport = AIRPORTS.get(iata)
        if airport is None:
            raise UnknownAirport(iata)
        return airport.tz

    async def fake_budget(_session: Any, _settings: Any = None) -> BudgetStatus:
        return CLEAR_BUDGET

    async def no_bookings(_session: Any, **_kwargs: Any) -> list[Booking]:
        return []

    async def fake_calendars(self: Any) -> list[Collection]:
        return list(CALENDARS)

    monkeypatch.setattr(web.views, "get_airport", fake_get_airport)
    monkeypatch.setattr(web.views, "airport_tz", fake_airport_tz)
    monkeypatch.setattr(web, "budget_status", fake_budget)
    monkeypatch.setattr(web.booking_repo, "list_bookings", no_bookings)
    monkeypatch.setattr(web.CalendarClient, "calendars", fake_calendars)

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

    async def latest(_session: Any, _ids: Any) -> dict[int, Any]:
        return {view_booking.id: snapshot} if snapshot is not None else {}

    async def list_bookings(_session: Any, **_kwargs: Any) -> list[Booking]:
        return [view_booking]

    monkeypatch.setattr(web.booking_repo, "get_booking", get_booking)
    monkeypatch.setattr(web.booking_repo, "list_bookings", list_bookings)
    monkeypatch.setattr(web.views.booking_repo, "latest_snapshots", latest)


def looks_up(
    monkeypatch: pytest.MonkeyPatch, candidates: Sequence[Candidate]
) -> list[tuple[str, str, date]]:
    """Answer every lookup with these, and record what it was asked about."""
    asked: list[tuple[str, str, date]] = []

    async def find_flights(_session: Any, carrier: str, number: str, day: date) -> list[Candidate]:
        asked.append((carrier, number, day))
        return list(candidates)

    monkeypatch.setattr(web.lookup, "find_flights", find_flights)
    return asked


# --- The board -----------------------------------------------------------------------


def test_the_board_says_what_to_do_when_there_are_no_flights(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "Nothing on the board" in page.text
    assert "/f/new" in page.text


def test_a_booking_nobody_has_checked_sits_on_the_board_with_a_badge(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is a flight like any other; the badge is the whole of the difference."""
    show(monkeypatch, booking(status="pending_review"), None)

    body = client.get("/").text
    assert "AC871" in body
    assert "Check this" in body
    assert 'href="/f/1"' in body


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


def test_the_problems_page_says_which_email_was_set_aside_and_why(client: TestClient) -> None:
    client.session.rows["IngestLog"] = [set_aside_row()]  # type: ignore[attr-defined]

    body = client.get("/problems").text

    assert "<strong>Your booking is confirmed</strong>" in body
    assert "the model timed out" in body
    assert "Try again" in body
    assert "Ignore" in body
    # The email itself, in Mail, is where the other half of the decision is made.
    assert 'href="message://%3Cabc@icloud.invalid%3E"' in body


def test_a_set_aside_email_is_kept_off_the_board(client: TestClient) -> None:
    """The board is read in a hurry for a gate; an email that would not parse is not
    news about any flight on it."""
    client.session.rows["IngestLog"] = [set_aside_row()]  # type: ignore[attr-defined]
    assert "Your booking is confirmed" not in client.get("/").text


def test_the_problems_tab_is_marked_only_while_something_is_waiting(client: TestClient) -> None:
    quiet = client.get("/").text
    assert 'href="/problems"' in quiet
    assert "waiting:" not in quiet

    client.session.rows["IngestLog"] = [set_aside_row()]  # type: ignore[attr-defined]
    # On every page, not only the board: the mark is how you learn there is anything.
    for path in ("/", "/settings", "/problems"):
        assert "1 waiting:" in client.get(path).text


def test_the_problems_page_says_when_there_is_nothing(client: TestClient) -> None:
    assert "Nothing needs attention" in client.get("/problems").text


def test_trying_a_set_aside_message_again_puts_it_back_in_the_queue(
    client: TestClient,
) -> None:
    row = set_aside_row()
    client.session.rows["IngestLog"] = [row]  # type: ignore[attr-defined]

    response = client.post(
        "/mail/retry", data={"message_id": row.message_id}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/problems"
    # The email never lost its flag, so clearing the give-up is all it takes.
    assert row.attempts == 0
    assert row.retry_at is not None


def test_ignoring_a_set_aside_message_lets_the_next_sweep_unflag_it(
    client: TestClient,
) -> None:
    row = set_aside_row()
    client.session.rows["IngestLog"] = [row]  # type: ignore[attr-defined]

    response = client.post(
        "/mail/ignore", data={"message_id": row.message_id}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/problems"
    assert row.outcome == "no_flight"
    assert row.retry_at is None


def test_a_message_that_is_not_set_aside_is_a_404(client: TestClient) -> None:
    for path in ("/mail/retry", "/mail/ignore"):
        assert client.post(path, data={"message_id": "<nope@icloud.invalid>"}).status_code == 404


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


def test_a_flight_typed_in_by_hand_has_no_email_to_open(
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
    # Every fact keeps its row and reads as a plain dash rather than disappearing.
    for label in ("Gate", "Terminal", "Baggage", "Wheels up"):
        assert label in body
    assert body.count(">-<") >= 6
    assert "None" not in body
    # A missing value is a dash in its row, never the page-level empty state.
    assert 'class="empty"' not in body
    assert "Scheduled" in body


def test_a_flight_in_the_air_renders_what_is_worth_knowing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(confirmation_code="X7QW2P", seat="14A"), full_snapshot())

    body = client.get("/f/1").text
    assert "B27" in body and "A14" in body
    assert "B789" in body
    assert "X7QW2P" in body and "14A" in body
    # What is on the ticket, not what is in the flight plan.
    for gone in ("Filed route", "Distance", "Registration", "Timezone", "Last checked"):
        assert gone not in body


def test_a_cancelled_flight_says_who_said_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AeroAPI's flag means "no longer tracked", which is not the same as being told."""
    show(monkeypatch, booking(), FlightSnapshot(id=3, booking_id=1, cancelled=True, raw={}))

    body = client.get("/f/1").text
    assert "Maybe cancelled" in body
    assert "confirm with the airline" in body


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


def test_a_flight_number_that_names_one_flight_comes_back_as_a_filled_in_form(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = looks_up(monkeypatch, [CANDIDATE])

    response = client.post(
        "/f/new",
        data={"flight_number": "ac 871", "departure_date": "2026-09-12"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert asked == [("AC", "871", date(2026, 9, 12))]
    body = client.get(response.headers["location"]).text
    assert 'value="YUL"' in body and 'value="LHR"' in body
    assert 'value="2026-09-12T18:40"' in body
    assert 'value="2026-09-13T10:25"' in body
    # Who actually flies it is carried through without being asked about.
    assert 'name="operating_carrier"' in body
    assert 'value="LH"' in body and 'value="479"' in body


def test_a_number_that_flies_twice_that_day_is_a_choice_rather_than_a_guess(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    evening = replace(CANDIDATE, departure_local=datetime(2026, 9, 12, 21, 15))
    looks_up(monkeypatch, [CANDIDATE, evening])

    body = client.post(
        "/f/new", data={"flight_number": "AC871", "departure_date": "2026-09-12"}
    ).text

    assert "flies more than once" in body
    assert "18:40" in body and "21:15" in body
    assert body.count("/f/new/details?") == 2


def test_a_flight_number_nobody_publishes_says_so_and_offers_the_long_way(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    looks_up(monkeypatch, [])

    page = client.post("/f/new", data={"flight_number": "AC871", "departure_date": "2026-09-12"})

    assert page.status_code == 400
    assert "No AC871 is scheduled to leave that day." in page.text
    assert 'href="/f/new/details"' in page.text
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


def test_a_lookup_that_cannot_be_made_still_leaves_a_way_to_add_the_flight(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A convenience that is down must not take adding a flight down with it."""
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
        assert 'href="/f/new/details"' in page.text


def test_without_a_flightaware_key_the_page_says_why_and_offers_the_form(
    unconfigured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with build_client(unconfigured, monkeypatch) as fresh:
        body = fresh.get("/f/new").text

    assert "No FlightAware key" in body
    assert 'name="flight_number"' not in body
    assert 'href="/f/new/details"' in body


def test_the_form_behind_the_lookup_still_asks_about_the_whole_flight(
    client: TestClient,
) -> None:
    page = client.get("/f/new/details")
    assert page.status_code == 200
    assert 'name="marketing_carrier"' in page.text
    # The times are wall clock at their own airport, never a UTC instant.
    assert 'type="datetime-local"' in page.text
    assert 'action="/f"' in page.text


def test_a_flight_added_by_hand_is_not_credited_to_an_operator_nobody_named(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hidden fields are empty on the by-hand path, and empty is not a carrier."""
    written: dict[str, Any] = {}

    async def create_booking(_session: Any, **fields: Any) -> Booking:
        written.update(fields)
        return booking()

    monkeypatch.setattr(web.booking_repo, "create_booking", create_booking)

    client.post(
        "/f",
        data={
            "marketing_carrier": "AC",
            "marketing_number": "871",
            "origin_iata": "YUL",
            "dest_iata": "LHR",
            "departure_local": "2026-09-12T18:40",
            "operating_carrier": "",
            "operating_number": "",
        },
        follow_redirects=False,
    )

    assert written["operating_carrier"] is None
    assert written["operating_number"] is None


def test_an_airport_nobody_has_heard_of_comes_back_as_a_sentence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unknown(*_args: Any, **_kwargs: Any) -> Booking:
        raise UnknownAirport("XYZ")

    monkeypatch.setattr(web.booking_repo, "create_booking", unknown)

    page = client.post(
        "/f",
        data={
            "marketing_carrier": "AC",
            "marketing_number": "871",
            "origin_iata": "YUL",
            "dest_iata": "XYZ",
            "departure_local": "2026-09-12T18:40",
        },
    )

    assert page.status_code == 400
    assert "XYZ is not an airport we know." in page.text


def test_a_flight_on_the_calendar_offers_a_way_into_the_calendar_app(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The push about an import goes on pointing here; this is the link the other way."""
    show(monkeypatch, booking(calendar_event_uid="flighter-1@flighter.invalid"), None)
    assert "calshow:" in client.get("/f/1").text


def test_a_flight_that_is_not_on_the_calendar_offers_no_link(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), None)
    assert "calshow:" not in client.get("/f/1").text


def test_the_edit_form_shows_local_wall_clock_not_utc(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = booking(scheduled_departure_utc=datetime(2026, 9, 12, 22, 40, tzinfo=UTC))
    show(monkeypatch, row, empty_snapshot())

    body = client.get("/f/1/edit").text
    assert 'value="2026-09-12T18:40"' in body  # 22:40Z is 18:40 in Montreal.


def test_an_edit_hands_the_booking_layer_what_the_user_typed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normalising a carrier is the booking layer's job, and it is the dedupe key."""
    show(monkeypatch, booking(), empty_snapshot())
    written: dict[str, Any] = {}

    async def update_booking(_session: Any, booking_id: int, **fields: Any) -> Booking:
        written.update(fields)
        return booking()

    monkeypatch.setattr(web.booking_repo, "update_booking", update_booking)

    response = client.post(
        "/f/1",
        data={
            "marketing_carrier": "ac",
            "marketing_number": "871",
            "origin_iata": "YUL",
            "dest_iata": "LHR",
            "departure_local": "2026-09-12T18:40",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert written["marketing_carrier"] == "ac"
    # 18:40 in Montreal is 22:40Z, read at the origin airport rather than at the server.
    assert written["scheduled_departure_utc"] == datetime(2026, 9, 12, 22, 40, tzinfo=UTC)


def test_confirming_a_booking_starts_it_being_tracked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = booking(status="pending_review")
    show(monkeypatch, row, None)
    kept: dict[str, Any] = {}

    async def update_booking(_session: Any, booking_id: int, **fields: Any) -> Booking:
        kept.update({"id": booking_id} | fields)
        return row

    monkeypatch.setattr(web.booking_repo, "update_booking", update_booking)

    response = client.post("/f/1/keep", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/f/1"
    assert kept == {"id": 1, "status": "active"}


def test_confirming_a_flight_that_is_not_there_is_a_404(client: TestClient) -> None:
    assert client.post("/f/9/keep").status_code == 404


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


def test_the_schema_is_not_published(client: TestClient) -> None:
    """Nothing writes against these routes, so a map of them is only ever a gift."""
    for path in ("/docs", "/openapi.json"):
        assert client.get(path).status_code == 404


# --- Settings ------------------------------------------------------------------------


def test_the_settings_page_shows_what_is_connected(client: TestClient) -> None:
    body = client.get("/settings").text
    # The Apple ID names the account rather than proving anything, so it is shown back.
    assert "someone@icloud.com" in body
    assert "Flights" in body
    # The token is handed to your own phone, so the page carries it: in the Connect link,
    # and in the clear for trying the endpoint by hand.
    assert (
        'href="scriptable:///run/Flights?api=https%3A%2F%2Fflights.example.com&amp;token=test-token"'
        in body
    )
    assert ">test-token</pre>" in body


def test_the_widget_tab_says_whether_a_phone_has_fetched(client: TestClient) -> None:
    body = client.get("/settings").text
    assert "No phone has fetched flights yet" in body
    assert "Not connected" in body

    recent = datetime.now(UTC) - timedelta(minutes=4)
    client.session.rows["KV"] = [  # type: ignore[attr-defined]
        KV(key=LAST_SEEN_KEY, value={"at": recent.strftime("%Y-%m-%dT%H:%M:%SZ")})
    ]
    body = client.get("/settings").text
    assert "Last fetched by a phone <strong>4m ago</strong>" in body


def test_a_phone_not_heard_from_in_a_day_is_not_connected(client: TestClient) -> None:
    """iOS skips reloads for hours at a time; only a whole day of silence means broken."""
    long_ago = datetime.now(UTC) - timedelta(days=3, hours=2)
    client.session.rows["KV"] = [  # type: ignore[attr-defined]
        KV(key=LAST_SEEN_KEY, value={"at": long_ago.strftime("%Y-%m-%dT%H:%M:%SZ")})
    ]
    body = client.get("/settings").text
    assert "Last fetched by a phone <strong>3d ago</strong>" in body
    assert "Not connected" in body


def test_regenerating_the_token_lands_back_on_the_widget_tab(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    minted: list[bool] = []
    monkeypatch.setattr(web, "mint_widget_token", lambda: minted.append(True))

    response = client.post("/settings/widget/token", follow_redirects=False)

    assert minted == [True]
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1&tab=widget"
    body = client.get("/settings?saved=1&tab=widget").text
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


def test_the_settings_page_says_what_the_month_has_cost(client: TestClient) -> None:
    assert "$0.42 of $4.00 this" in client.get("/settings").text


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
    assert "Start here" in body
    for step, name in enumerate(("Apple ID", "FlightAware", "Pushover", "Calendar"), start=1):
        assert name in body
        assert f">{step}<" in body
    # And the board says where to go rather than sitting there empty.
    assert "Nothing is connected yet" in fresh.get("/").text


def test_a_deployment_nobody_has_told_its_address_is_offered_this_one(
    unconfigured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is read on the phone, so the address the page was opened on is the likely one."""
    monkeypatch.setattr(prefs, "_current", prefs.Prefs())
    with build_client(unconfigured, monkeypatch) as fresh:
        body = fresh.get("/settings").text
    assert 'value="http://testserver"' in body
    # The Connect link too: a phone handed the default would be told to ask itself.
    assert "api=http%3A%2F%2Ftestserver" in body
    assert "localhost:8000" not in body
    assert "localhost%3A8000" not in body


def test_an_address_that_was_set_is_left_alone(client: TestClient) -> None:
    assert 'value="https://flights.example.com"' in client.get("/settings").text


def test_the_first_run_signpost_goes_away_once_everything_is_set_up(
    client: TestClient,
) -> None:
    assert "Start here" not in client.get("/settings").text


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


def test_a_card_that_posts_one_field_leaves_the_others_alone(client: TestClient) -> None:
    """Each card is its own form, so what arrives is a slice rather than the lot."""
    client.post("/settings", data=SETTINGS_FORM | {"log_level": "WARNING"})

    client.post("/settings", data={"icloud_calendar_url": CALENDARS[1].url})

    assert prefs.current().icloud_calendar_url == CALENDARS[1].url
    assert prefs.current().log_level == "WARNING"


def test_a_typo_in_the_cap_is_refused_with_the_field_named(client: TestClient) -> None:
    response = client.post(
        "/settings", data=SETTINGS_FORM | {"aeroapi_monthly_cap_usd": "four dollars"}
    )
    assert response.status_code == 400
    assert "aeroapi monthly cap usd" in response.text
    # And the typed-in value is still on the form rather than silently reverted.
    assert "four dollars" in response.text


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


def test_the_settings_page_offers_the_calendars_the_account_has(client: TestClient) -> None:
    body = client.get("/settings").text
    for calendar in CALENDARS:
        assert f'value="{calendar.url}"' in body
        assert f">{calendar.name}<" in body


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

    assert asked is False
    assert "Connect the Apple ID above to pick a calendar." in body


def test_the_settings_page_still_opens_when_icloud_cannot_be_reached(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other preference on the page is still editable, and the reason is on it."""

    async def unreachable(self: Any) -> list[Collection]:
        raise CalendarUnavailable("cannot reach caldav.icloud.com")

    monkeypatch.setattr(web.CalendarClient, "calendars", unreachable)

    page = client.get("/settings")
    assert page.status_code == 200
    # A sentence about what to do, not the repr of whatever was raised.
    assert "iCloud did not answer" in page.text
    assert "CalendarUnavailable" not in page.text
    assert 'name="imap_flag_colour"' in page.text
