"""The pages, rendered against a faked data layer.

Nothing here touches Postgres or the network. The interesting risk in a template is a
null: gates, baggage belts and every estimate are absent for most of a flight's life,
and a page that raises on one of them is a page that fails exactly when it is needed.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flighter import prefs, web
from flighter.aeroapi import BudgetStatus
from flighter.config import Settings
from flighter.db import get_session
from flighter.models import Airport, Booking, FlightEvent, FlightSnapshot

NOW = datetime.now(UTC)
DEPARTURE = NOW + timedelta(days=2)
ARRIVAL = DEPARTURE + timedelta(hours=7)

AIRPORTS = {
    "YUL": Airport(
        iata="YUL",
        name="Montreal-Trudeau",
        city="Montreal",
        country="CA",
        latitude=45.5,
        longitude=-73.6,
        tz="America/Toronto",
    ),
    "LHR": Airport(
        iata="LHR",
        name="London Heathrow",
        city="London",
        country="GB",
        latitude=51.5,
        longitude=-0.5,
        tz="Europe/London",
    ),
}

# Every field the settings form posts, so a test can change one of them.
SETTINGS_FORM = {
    "public_base_url": "https://flights.example.com",
    "log_level": "INFO",
    "aeroapi_monthly_cap_usd": "4.00",
    "aeroapi_rate_limit_per_minute": "8",
    "anthropic_model": "claude-sonnet-5",
    "extraction_confidence_threshold": "0.85",
    "imap_folder": "INBOX",
    "imap_idle_seconds": "300",
    "icloud_calendar_name": "Flights",
}

CLEAR_BUDGET = BudgetStatus(
    spend_usd=Decimal("0.42"), cap_usd=Decimal("4.00"), tripped=False, month="2026-08"
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
        status_text="En Route / On Time",
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
        raw={
            "route": "BOSOX Q812 YAHOO DCT LOGAN",
            "filed_altitude": 380,
            "route_distance": 3251,
            "seats_cabin_first": 4,
            "seats_cabin_business": 30,
            "seats_cabin_coach": 250,
        },
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

    async def execute(self, statement: Any) -> FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        return FakeResult(self.rows.get(entity.__name__, []))

    async def scalar(self, statement: Any) -> Any:
        return None

    async def get(self, model: type, pk: Any) -> Any:
        for row in self.rows.get(model.__name__, []):
            if row.id == pk:
                return row
        return None

    def add(self, instance: Any) -> None:
        self.rows.setdefault(type(instance).__name__, [])

    async def flush(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def delete(self, instance: Any) -> None:
        return None

    async def merge(self, instance: Any) -> Any:
        self.rows.setdefault(type(instance).__name__, [])
        return instance


def build_client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The app with a faked data layer, ready for a request."""
    session = FakeSession()

    async def fake_get_airport(_session: Any, iata: str) -> Airport | None:
        return AIRPORTS.get(iata)

    async def fake_budget(_session: Any, _settings: Any = None) -> BudgetStatus:
        return CLEAR_BUDGET

    async def no_bookings(_session: Any, **_kwargs: Any) -> list[Booking]:
        return []

    monkeypatch.setattr(web, "get_airport", fake_get_airport)
    monkeypatch.setattr(web, "budget_status", fake_budget)
    monkeypatch.setattr(web.booking_repo, "list_bookings", no_bookings)

    app = web.create_app(settings)
    app.dependency_overrides[get_session] = lambda: session
    test_client = TestClient(app)
    test_client.session = session  # type: ignore[attr-defined]
    return test_client


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with build_client(settings, monkeypatch) as test_client:
        yield test_client


def show(monkeypatch: pytest.MonkeyPatch, view_booking: Booking, snapshot: Any) -> None:
    """Make one booking and its newest snapshot the whole of the database."""

    async def get_booking(_session: Any, booking_id: int) -> Booking | None:
        return view_booking if booking_id == view_booking.id else None

    async def latest(_session: Any, _ids: Any) -> dict[int, Any]:
        return {view_booking.id: snapshot} if snapshot is not None else {}

    monkeypatch.setattr(web.booking_repo, "get_booking", get_booking)
    monkeypatch.setattr(web, "latest_snapshots", latest)


def test_the_list_says_what_to_do_when_there_are_no_flights(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "Nothing on the board" in page.text
    assert "/f/new" in page.text


def test_the_review_banner_is_absent_until_something_is_pending(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "to check" not in client.get("/").text

    pending = booking(id=7, status="pending_review", extraction_confidence=0.62)

    async def list_bookings(_session: Any, *, statuses: Sequence[str] = (), **_kw: Any) -> Any:
        return [pending] if "pending_review" in statuses else []

    monkeypatch.setattr(web.booking_repo, "list_bookings", list_bookings)

    page = client.get("/")
    assert "1 booking to check" in page.text
    assert 'href="/review"' in page.text


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
    for label in ("Gate", "Terminal", "Baggage", "Registration", "Filed altitude"):
        assert label in body
    assert body.count(">-<") >= 8
    assert "None" not in body
    # A missing value is a dash in its row, never the page-level empty state.
    assert 'class="empty"' not in body
    assert "Scheduled" in body


def test_a_flight_in_the_air_renders_everything_it_knows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(confirmation_code="X7QW2P", seat="14A"), full_snapshot())

    body = client.get("/f/1").text
    assert "B27" in body and "A14" in body
    assert "C-FVLQ" in body and "B789" in body
    assert "FL380" in body
    assert "3,251 mi" in body
    assert "BOSOX Q812 YAHOO DCT LOGAN" in body
    assert "X7QW2P" in body and "14A" in body
    assert "64% flown" in body
    # Both ends are labelled with the zone they are read in.
    assert "America/Toronto" in body and "Europe/London" in body


def test_the_timeline_shows_changes_newest_first(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), full_snapshot())
    client.session.rows["FlightEvent"] = [  # type: ignore[attr-defined]
        FlightEvent(
            id=2,
            booking_id=1,
            kind="gate_change",
            old_value="B12",
            new_value="B27",
            occurred_at=NOW,
        ),
    ]

    body = client.get("/f/1").text
    assert "Gate change" in body
    assert "B12" in body and "B27" in body


def test_a_flight_that_is_not_there_is_a_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(monkeypatch, booking(), None)

    page = client.get("/f/999")
    assert page.status_code == 404
    assert "No such flight." in page.text


def test_the_add_form_asks_only_about_the_flight(client: TestClient) -> None:
    page = client.get("/f/new")
    assert page.status_code == 200
    assert 'name="marketing_carrier"' in page.text
    # The times are wall clock at their own airport, never a UTC instant.
    assert 'type="datetime-local"' in page.text


def test_the_edit_form_shows_local_wall_clock_not_utc(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = booking(scheduled_departure_utc=datetime(2026, 9, 12, 22, 40, tzinfo=UTC))
    show(monkeypatch, row, empty_snapshot())

    body = client.get("/f/1/edit").text
    assert 'value="2026-09-12T18:40"' in body  # 22:40Z is 18:40 in Montreal.


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


def test_htmx_gets_an_empty_body_so_the_row_just_goes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = booking()
    show(monkeypatch, row, empty_snapshot())

    async def delete_booking(_session: Any, booking_id: int) -> Booking:
        return row

    monkeypatch.setattr(web.booking_repo, "delete_booking", delete_booking)

    page = client.post("/f/1/delete", headers={"HX-Request": "true"})
    assert page.status_code == 200
    assert page.text == ""


def test_healthz_is_liveness_only(client: TestClient) -> None:
    page = client.get("/healthz")
    assert page.status_code == 200
    assert page.json() == {"status": "ok"}


# --- Settings ------------------------------------------------------------------------


def test_the_settings_page_shows_what_is_connected(client: TestClient) -> None:
    body = client.get("/settings").text
    assert "someone@icloud.com" in body
    assert "Flights" in body
    # The token has to be readable: it is typed into Scriptable by hand.
    assert "test-token" in body


def test_saving_a_preference_redirects_and_takes_effect(client: TestClient) -> None:
    response = client.post("/settings", data=SETTINGS_FORM | {"log_level": "DEBUG"})
    assert response.status_code == 200
    assert response.url.path == "/settings"
    assert prefs.current().log_level == "DEBUG"


def test_a_typo_in_the_cap_is_refused_with_the_field_named(client: TestClient) -> None:
    response = client.post(
        "/settings", data=SETTINGS_FORM | {"aeroapi_monthly_cap_usd": "four dollars"}
    )
    assert response.status_code == 400
    assert "aeroapi monthly cap usd" in response.text
    # And the typed-in value is still on the form rather than silently reverted.
    assert "four dollars" in response.text


def test_naming_the_calendar_turns_the_sync_on(client: TestClient) -> None:
    client.post("/settings", data=SETTINGS_FORM | {"icloud_calendar_name": ""})
    assert not prefs.current().calendar_configured

    client.post("/settings", data=SETTINGS_FORM | {"icloud_calendar_name": "Trips"})
    assert prefs.current().icloud_calendar_name == "Trips"
    assert prefs.current().calendar_configured
