"""The widget contract. Anything asserted here is something the phone renders literally."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flighter import prefs, widget
from flighter.aeroapi import BREAKER_KEY, month_key
from flighter.config import Settings, get_settings
from flighter.db import get_session
from flighter.models import KV, Airport, Booking, BookingStatus, FlightSnapshot
from flighter.widget import (
    FlightRow,
    authorize,
    build_payload,
    connect_url,
    last_seen,
    read_degraded,
    script_body,
    script_source,
)

NOW = datetime(2026, 9, 12, 18, 0, tzinfo=UTC)
DEPARTURE = datetime(2026, 9, 12, 18, 40, tzinfo=UTC)
ARRIVAL = datetime(2026, 9, 12, 22, 15, tzinfo=UTC)

# Anything a phone could read as an instant.
ISO_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def booking(**kwargs: Any) -> Booking:
    defaults: dict[str, Any] = {
        "id": 42,
        "marketing_carrier": "DL",
        "marketing_number": "1234",
        "origin_iata": "JFK",
        "dest_iata": "LAX",
        "scheduled_departure_utc": DEPARTURE,
        "scheduled_arrival_utc": ARRIVAL,
        "status": "active",
        "source": "manual",
    }
    return Booking(**(defaults | kwargs))


def snapshot(**kwargs: Any) -> FlightSnapshot:
    defaults: dict[str, Any] = {"booking_id": 42, "raw": {}}
    return FlightSnapshot(**(defaults | kwargs))


def payload(rows: Sequence[FlightRow], settings: Settings, **kwargs: Any) -> dict[str, Any]:
    built = build_payload(
        rows, settings=settings, now=NOW, base_url="https://flights.example.com", **kwargs
    )
    return built.model_dump(mode="json")


def _id(flight: dict[str, Any]) -> int:
    """Which booking a row is about. The script follows the link rather than an id."""
    return int(flight["detail_url"].rsplit("/", 1)[1])


def airport(iata: str, tz: str, city: str | None = None) -> Airport:
    return Airport(iata=iata, name=iata, city=city, latitude=0.0, longitude=0.0, tz=tz)


AIRPORTS = {
    "JFK": airport("JFK", "America/New_York", "New York"),
    "LAX": airport("LAX", "America/Los_Angeles", "Los Angeles"),
    "HND": airport("HND", "Asia/Tokyo", "Tokyo"),
    "YOW": airport("YOW", "America/Toronto", "Ottawa"),
}


def column(flight: dict[str, Any]) -> tuple[str | None, str | None]:
    """The right-hand column: the word and the time under it."""
    return flight["milestone_label"], flight["milestone_text"]


# --- payload shaping ------------------------------------------------------------------


def test_upcoming_flight(settings: Settings) -> None:
    far = booking(scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings)["flights"][0]
    # Days out the time it leaves is the whole story, with the day over it; there is
    # no gate to find yet, so no detail line.
    assert flight == {
        "detail_url": "https://flights.example.com/f/42",
        "phase": "upcoming",
        "logo_url": "https://www.gstatic.com/flights/airline_logos/70px/DL.png",
        "number": "DL1234",
        "route": "JFK → LAX",
        "status_label": "Scheduled",
        "status_tone": "quiet",
        "detail": None,
        "milestone_label": "Fri 18 Sep",
        "milestone_text": "18:00",
    }


def test_the_day_it_leaves_is_read_at_the_origin(settings: Settings) -> None:
    far = booking(origin_iata="HND", scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings, airports=AIRPORTS)["flights"][0]
    assert column(flight) == ("Sat 19 Sep", "03:00")


def test_a_departure_not_today_carries_its_day(settings: Settings) -> None:
    """NOW is 18:00 UTC on the 12th: 14:00 in New York, 03:00 the next day in Tokyo.

    A time on its own reads as today's, so a flight leaving tomorrow morning would look
    overdue all evening without the day under it. The board names the day in the pill
    when the feed has not picked the flight up; here the column does, and the status
    says only that it is booked.
    """
    early = booking(origin_iata="HND", scheduled_departure_utc=NOW + timedelta(hours=8))
    flight = payload([(early, None)], settings, airports=AIRPORTS)["flights"][0]
    assert column(flight) == ("Departs", "11:00")
    assert flight["status_label"] == "Scheduled"
    assert flight["status_tone"] == "quiet"

    late = booking(scheduled_departure_utc=NOW + timedelta(hours=12))
    flight = payload([(late, None)], settings, airports=AIRPORTS)["flights"][0]
    assert column(flight) == ("Tomorrow", "02:00")
    assert flight["status_label"] == "Scheduled"

    # With no airport on file the day is read off UTC rather than left blank.
    assert column(payload([(late, None)], settings)["flights"][0]) == ("Tomorrow", "06:00")
    assert column(payload([(early, None)], settings)["flights"][0]) == ("Tomorrow", "02:00")


def test_a_feed_that_says_on_time_keeps_the_day_under_the_time(settings: Settings) -> None:
    tomorrow = NOW + timedelta(hours=12)
    flight = payload([(booking(), snapshot(scheduled_out=tomorrow))], settings, airports=AIRPORTS)[
        "flights"
    ][0]
    assert flight["status_label"] == "On time"
    assert column(flight) == ("Tomorrow", "02:00")


def test_day_of_shows_the_gate_and_the_seat(settings: Settings) -> None:
    """The time it leaves is in the column, so the line does not repeat it."""
    gated = snapshot(scheduled_out=DEPARTURE, gate_origin="B22", terminal_origin="4")
    flight = payload([(booking(seat="14A"), gated)], settings)["flights"][0]
    assert flight["phase"] == "day_of"
    assert flight["detail"] == "Gate B22 · Seat 14A"
    assert flight["status_label"] == "On time"
    assert flight["status_tone"] == "ok"
    assert column(flight) == ("Departs", "18:40")


def test_day_of_with_nothing_assigned_yet_has_no_detail(settings: Settings) -> None:
    flight = payload([(booking(), snapshot())], settings)["flights"][0]
    assert column(flight) == ("Departs", "18:40")
    assert flight["detail"] is None


def test_a_delayed_departure_names_the_time_it_now_leaves(settings: Settings) -> None:
    held = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(minutes=30))
    flight = payload([(booking(seat="14A"), held)], settings)["flights"][0]
    assert flight["status_label"] == "Departure delayed"
    assert flight["status_tone"] == "warn"
    assert column(flight) == ("Departs", "19:10")
    assert flight["detail"] == "Seat 14A"


def test_the_run_up_to_departure_keeps_the_time(settings: Settings) -> None:
    """The half hour before departure is when the time matters most, so it must not be
    traded for a word about boarding that no feed reports."""
    imminent = snapshot(scheduled_out=NOW + timedelta(minutes=20), gate_origin="B22")
    flight = payload([(booking(), imminent)], settings)["flights"][0]
    assert flight["phase"] == "day_of"
    assert column(flight) == ("Departs", "18:20")


def test_taxiing_says_departed_and_names_the_landing_at_the_other_end(
    settings: Settings,
) -> None:
    """Taxiing is ten minutes the widget is as likely as not to miss, and Departed stays
    true until the gate. Nothing upstream estimates wheels up, so the next rung with a
    time is the landing, read on the clock where it lands."""
    taxiing = snapshot(
        scheduled_out=NOW - timedelta(minutes=5),
        actual_out=NOW - timedelta(minutes=2),
        gate_origin="B22",
        gate_destination="12",
        terminal_destination="B",
    )
    flight = payload([(booking(), taxiing)], settings, airports=AIRPORTS)["flights"][0]
    assert flight["phase"] == "taxiing"
    assert flight["status_label"] == "Departed"
    assert flight["status_tone"] == "live"
    assert column(flight) == ("Lands", "15:15")
    assert flight["detail"] is None


def test_airborne_names_the_landing(settings: Settings) -> None:
    flying = snapshot(
        scheduled_out=DEPARTURE - timedelta(hours=2),
        actual_out=DEPARTURE - timedelta(hours=2),
        actual_off=DEPARTURE - timedelta(hours=2),
        scheduled_in=ARRIVAL,
        estimated_in=ARRIVAL + timedelta(minutes=25),
        gate_destination="12",
        terminal_destination="B",
        progress_percent=64,
    )
    flight = payload([(booking(), flying)], settings)["flights"][0]
    assert flight["phase"] == "airborne"
    assert column(flight) == ("Lands", "22:40")
    # The gate at the other end waits until the flight is on the ground.
    assert flight["detail"] is None
    assert flight["status_label"] == "Arriving late"
    assert flight["status_tone"] == "warn"


def test_landed_names_the_time_at_the_gate(settings: Settings) -> None:
    landed = snapshot(
        actual_off=DEPARTURE,
        actual_on=ARRIVAL - timedelta(minutes=10),
        estimated_in=ARRIVAL,
        baggage_claim="7",
        gate_destination="12",
        terminal_destination="B",
        gate_origin="B22",
        progress_percent=100,
    )
    flight = payload([(booking(), landed)], settings)["flights"][0]
    assert flight["phase"] == "landed"
    assert flight["status_label"] == "Landed"
    assert flight["status_tone"] == "ok"
    assert flight["detail"] is None
    assert column(flight) == ("At the gate", "22:15")


def test_at_the_gate_the_belt_takes_the_column(settings: Settings) -> None:
    done = snapshot(
        actual_off=DEPARTURE,
        actual_on=ARRIVAL,
        actual_in=ARRIVAL,
        baggage_claim="7",
        gate_destination="12",
        terminal_destination="B",
    )
    flight = payload([(booking(), done)], settings)["flights"][0]
    assert flight["status_label"] == "Arrived"
    assert flight["detail"] is None
    assert column(flight) == ("Baggage claim", "7")


def test_a_belt_nobody_has_named_is_the_dash_the_card_shows(settings: Settings) -> None:
    done = snapshot(actual_off=DEPARTURE, actual_on=ARRIVAL, actual_in=ARRIVAL)
    flight = payload([(booking(), done)], settings)["flights"][0]
    assert column(flight) == ("Baggage claim", "-")
    assert flight["detail"] is None


def test_a_landed_flight_past_its_gate_time_is_sent_to_the_belt(settings: Settings) -> None:
    """On-blocks often never comes through the feed; the clock decides instead."""
    overdue = snapshot(
        actual_off=DEPARTURE,
        actual_on=ARRIVAL - timedelta(minutes=20),
        estimated_in=ARRIVAL - timedelta(minutes=8),
        baggage_claim="7",
        gate_destination="12",
    )
    late = build_payload(
        [(booking(), overdue)],
        settings=settings,
        now=ARRIVAL,
        base_url="https://flights.example.com",
    )
    flight = late.model_dump(mode="json")["flights"][0]
    assert flight["status_label"] == "Landed"
    assert column(flight) == ("Baggage claim", "7")
    assert flight["detail"] is None


def test_a_time_that_has_passed_stays_the_time(settings: Settings) -> None:
    """Wheels down is published minutes after the fact. The board says "due" meanwhile;
    here the time stands, and the phone's own clock says it is overdue."""
    overdue = snapshot(
        actual_off=DEPARTURE, estimated_on=ARRIVAL - timedelta(minutes=10), estimated_in=ARRIVAL
    )
    later = build_payload(
        [(booking(), overdue)],
        settings=settings,
        now=ARRIVAL - timedelta(minutes=4),
        base_url="https://flights.example.com",
    )
    flight = later.model_dump(mode="json")["flights"][0]
    assert column(flight) == ("Lands", "22:05")


def test_cancelled_has_no_time_to_give(settings: Settings) -> None:
    flight = payload([(booking(), snapshot(cancelled=True))], settings)["flights"][0]
    assert flight["phase"] == "cancelled"
    assert flight["status_label"] == "Cancelled"
    assert flight["status_tone"] == "stop"
    assert flight["detail"] is None
    assert column(flight) == (None, None)


def test_a_booking_the_poller_closed_in_the_air_has_no_time_to_give(
    settings: Settings,
) -> None:
    """The feed lost it. A landing hours in the past is no time to name."""
    lost = snapshot(actual_off=NOW - timedelta(hours=9), estimated_in=NOW - timedelta(hours=3))
    closed = booking(status=BookingStatus.COMPLETED)
    flight = payload([(closed, lost)], settings)["flights"][0]
    assert flight["status_label"] == "Flown"
    assert column(flight) == (None, None)


def test_a_diversion_renames_the_destination_and_reads_its_clock(settings: Settings) -> None:
    diverted = snapshot(
        diverted=True, destination_iata="YOW", actual_off=DEPARTURE, estimated_in=ARRIVAL
    )
    flight = payload([(booking(), diverted)], settings, airports=AIRPORTS)["flights"][0]
    assert flight["phase"] == "diverted"
    assert flight["route"] == "JFK → YOW"
    assert flight["status_label"] == "Diverted"
    # The status says Diverted and the route names where to; nothing else repeats it.
    assert flight["detail"] is None
    assert column(flight) == ("Lands", "18:15")


def test_a_minute_late_is_still_on_time(settings: Settings) -> None:
    jitter = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(minutes=1))
    flight = payload([(booking(), jitter)], settings)["flights"][0]
    assert flight["status_label"] == "On time"
    assert flight["status_tone"] == "ok"


def test_airborne_time_is_touchdown_not_the_gate(settings: Settings) -> None:
    """The time someone reads from seat 32A is wheels down.

    Taxiing to a stand is ten minutes nobody counts, so a time aimed at the gate is
    wrong for the whole stretch of the flight anyone is watching it.
    """
    touchdown = ARRIVAL - timedelta(minutes=11)
    flying = snapshot(
        actual_out=DEPARTURE - timedelta(hours=2),
        actual_off=DEPARTURE - timedelta(hours=2),
        scheduled_on=touchdown,
        estimated_on=touchdown,
        scheduled_in=ARRIVAL,
        estimated_in=ARRIVAL,
        progress_percent=70,
    )
    flight = payload([(booking(), flying)], settings)["flights"][0]
    assert column(flight) == ("Lands", "22:04")


def test_a_late_pushback_is_history_once_the_flight_is_off_the_ground(
    settings: Settings,
) -> None:
    """A flight that left the gate late but is landing on time is not delayed, and saying
    so for the rest of the cruise makes the status mean nothing."""
    recovered = snapshot(
        scheduled_out=DEPARTURE,
        actual_out=DEPARTURE + timedelta(minutes=40),
        actual_off=DEPARTURE + timedelta(minutes=52),
        scheduled_in=ARRIVAL,
        estimated_in=ARRIVAL,
    )
    flight = payload([(booking(), recovered)], settings)["flights"][0]
    assert flight["status_label"] == "In the air"
    assert flight["status_tone"] == "live"


# --- instants -------------------------------------------------------------------------


def _instants(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _instants(item)
    elif isinstance(value, list):
        for item in value:
            yield from _instants(item)
    elif isinstance(value, str) and ISO_LIKE.match(value):
        yield value


def test_nothing_in_the_payload_is_an_instant(settings: Settings) -> None:
    """The phone draws at reload and iOS reloads when it likes; anything it measured
    against its own clock would be a quarter of an hour wrong by the next one."""
    rows: list[FlightRow] = [
        (booking(id=1), snapshot(scheduled_out=NOW + timedelta(minutes=20))),
        (booking(id=2), snapshot(actual_off=DEPARTURE, estimated_in=ARRIVAL)),
        (booking(id=3, scheduled_departure_utc=NOW + timedelta(days=4)), None),
    ]
    assert list(_instants(payload(rows, settings))) == []


def test_a_non_utc_input_is_read_at_the_origins_clock(settings: Settings) -> None:
    """AeroAPI states offsets; whatever arrives is a clock at the airport on the way out."""
    tokyo = datetime(2026, 9, 12, 18, 40, tzinfo=UTC).astimezone()
    flight = payload([(booking(scheduled_departure_utc=tokyo), None)], settings)["flights"][0]
    assert column(flight) == ("Departs", "18:40")


# --- ordering and cadence -------------------------------------------------------------


def test_in_the_order_they_now_leave_capped_at_three(settings: Settings) -> None:
    """The board's order, so the widget leads with the card the board leads with."""
    rows: list[FlightRow] = [
        (booking(id=1, scheduled_departure_utc=NOW + timedelta(days=1)), None),
        (booking(id=2, scheduled_departure_utc=NOW + timedelta(days=3)), None),
        (
            booking(id=3, scheduled_departure_utc=NOW - timedelta(hours=3)),
            snapshot(actual_off=NOW - timedelta(hours=3)),
        ),
        (
            booking(id=4, scheduled_departure_utc=NOW - timedelta(hours=8)),
            snapshot(actual_off=NOW - timedelta(hours=8), actual_on=NOW - timedelta(hours=1)),
        ),
        (
            booking(id=5, scheduled_departure_utc=NOW + timedelta(minutes=10)),
            snapshot(estimated_out=NOW + timedelta(hours=30)),
        ),
    ]
    body = payload(rows, settings)
    # Landed, airborne, then tomorrow's. The one booked for ten minutes from now is
    # held until the day after, so it sorts by when it actually leaves and loses its seat.
    assert [_id(flight) for flight in body["flights"]] == [4, 3, 1]


def test_refresh_slows_down_when_nothing_is_close(settings: Settings) -> None:
    far = booking(scheduled_departure_utc=NOW + timedelta(days=4))
    assert payload([(far, None)], settings)["refresh_seconds"] == 900


def test_refresh_speeds_up_on_the_day(settings: Settings) -> None:
    assert payload([(booking(), None)], settings)["refresh_seconds"] == 600


def test_no_flights(settings: Settings) -> None:
    body = payload([], settings)
    assert body["flights"] == []
    assert body["refresh_seconds"] == 900
    assert body["degraded"] is False


# --- degraded -------------------------------------------------------------------------


class FakeBudgetSession:
    """Just enough of AsyncSession for aeroapi.budget_status; no database anywhere."""

    def __init__(self, *, spend: str = "0", latch: KV | None = None) -> None:
        self._spend = spend
        self._latch = latch

    async def scalar(self, statement: Any) -> str:
        return self._spend

    async def get(self, model: Any, key: Any) -> KV | None:
        return self._latch


def latch(month: datetime) -> KV:
    return KV(
        key=BREAKER_KEY,
        value={"month": month_key(month), "spend_usd": "4.01", "cap_usd": "4.00"},
    )


async def test_degraded_when_the_breaker_latch_is_present() -> None:
    session = FakeBudgetSession(latch=latch(datetime.now(UTC)))
    reason = await read_degraded(session)  # type: ignore[arg-type]
    assert reason is not None and "AeroAPI budget" in reason


async def test_a_latch_from_last_month_is_not_degraded() -> None:
    """The latch is month-scoped, so it unlatches on its own on the 1st."""
    session = FakeBudgetSession(latch=latch(datetime.now(UTC) - timedelta(days=40)))
    assert await read_degraded(session) is None  # type: ignore[arg-type]


async def test_no_latch_is_not_degraded() -> None:
    assert await read_degraded(FakeBudgetSession()) is None  # type: ignore[arg-type]


def test_a_stale_snapshot_on_a_close_flight_degrades(settings: Settings) -> None:
    stale = snapshot(scheduled_out=DEPARTURE, observed_at=NOW - timedelta(minutes=95))
    body = payload([(booking(), stale)], settings)
    assert body["degraded"] is True
    assert body["degraded_reason"] == "No status update in 95 min"


def test_a_recent_snapshot_does_not_degrade(settings: Settings) -> None:
    fresh = snapshot(scheduled_out=DEPARTURE, observed_at=NOW - timedelta(minutes=9))
    assert payload([(booking(), fresh)], settings)["degraded"] is False


def test_a_stale_snapshot_on_a_distant_flight_does_not_degrade(settings: Settings) -> None:
    """A flight days out is polled every few hours by design."""
    far = booking(scheduled_departure_utc=NOW + timedelta(days=6))
    old = snapshot(scheduled_out=NOW + timedelta(days=6), observed_at=NOW - timedelta(hours=5))
    assert payload([(far, old)], settings)["degraded"] is False


def test_a_never_polled_flight_does_not_degrade(settings: Settings) -> None:
    assert payload([(booking(), None)], settings)["degraded"] is False


def test_the_breaker_outranks_staleness(settings: Settings) -> None:
    stale = snapshot(scheduled_out=DEPARTURE, observed_at=NOW - timedelta(minutes=95))
    body = payload([(booking(), stale)], settings, degraded_reason="Cap hit")
    assert body["degraded"] is True
    assert body["degraded_reason"] == "Cap hit"


# --- auth -----------------------------------------------------------------------------


class FakeSession:
    """Enough of a session to prove the stamp: a KV table and nothing else."""

    def __init__(self) -> None:
        self.kv: dict[str, KV] = {}

    async def merge(self, row: KV) -> KV:
        self.kv[row.key] = row
        return row

    async def get(self, model: type, key: str) -> KV | None:
        return self.kv.get(key)


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def fake_rows(session: Any, now: datetime) -> list[FlightRow]:
        return [(booking(), snapshot(gate_origin="B22", terminal_origin="4"))]

    async def fake_degraded(session: Any) -> str | None:
        return None

    monkeypatch.setattr(widget, "load_flight_rows", fake_rows)
    monkeypatch.setattr(widget, "read_degraded", fake_degraded)

    session = FakeSession()

    async def fake_session() -> Any:
        yield session

    app = FastAPI()
    app.include_router(widget.router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = fake_session
    with TestClient(app) as test_client:
        test_client.session = session  # type: ignore[attr-defined]
        yield test_client


def test_bearer_header_is_accepted(client: TestClient) -> None:
    response = client.get("/api/widget", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["flights"][0]["number"] == "DL1234"
    assert body["flights"][0]["route"] == "JFK → LAX"


def test_query_token_is_accepted(client: TestClient) -> None:
    assert client.get("/api/widget?token=test-token").status_code == 200


def test_wrong_token_is_rejected(client: TestClient) -> None:
    assert client.get("/api/widget?token=nope").status_code == 401
    assert client.get("/api/widget", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_missing_token_is_rejected(client: TestClient) -> None:
    assert client.get("/api/widget").status_code == 401


def test_a_header_that_is_not_a_bearer_is_rejected(client: TestClient) -> None:
    response = client.get("/api/widget", headers={"Authorization": "test-token"})
    assert response.status_code == 401


def test_a_fetch_that_got_through_is_stamped(client: TestClient) -> None:
    """The settings page's only evidence that a phone is talking to this server."""
    session = client.session  # type: ignore[attr-defined]
    assert asyncio.run(last_seen(session)) is None

    client.get("/api/widget", headers={"Authorization": "Bearer test-token"})

    seen = asyncio.run(last_seen(session))
    assert seen is not None
    assert seen.tzinfo is UTC
    assert datetime.now(UTC) - seen < timedelta(seconds=5)


def test_a_rejected_fetch_leaves_no_stamp(client: TestClient) -> None:
    """A wrong token must look like silence, not like a phone that is connected."""
    client.get("/api/widget", headers={"Authorization": "Bearer nope"})
    assert asyncio.run(last_seen(client.session)) is None  # type: ignore[attr-defined]


def test_an_unset_token_refuses_everyone(settings: Settings) -> None:
    """A blank token must never mean "no authentication"."""
    unset = settings.model_copy(update={"widget_token": ""})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        authorize(unset, "Bearer test-token", None)
    assert caught.value.status_code == 503

    with pytest.raises(HTTPException):
        authorize(unset, None, "")


# --- install -------------------------------------------------------------------------


def test_the_connect_link_runs_the_script_with_the_address_and_the_token(
    settings: Settings,
) -> None:
    assert connect_url(settings, "https://flights.example.com") == (
        "scriptable:///run/Flighter?api=https%3A%2F%2Fflights.example.com&token=test-token"
    )


def test_links_point_where_the_phone_reached_until_an_address_is_saved(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default is the server's own name for itself, which no phone can follow."""
    monkeypatch.setattr(prefs, "_current", prefs.Prefs())
    response = client.get("/api/widget", headers={"Authorization": "Bearer test-token"})
    assert response.json()["flights"][0]["detail_url"] == "http://testserver/f/42"


def test_the_script_is_the_same_for_everyone(settings: Settings) -> None:
    """Address and token travel by the Connect link, so the file never has to change."""
    source = script_source()
    assert "flights.example.com" not in source
    assert settings.widget_token not in source
    assert "args.queryParameters" in source
    assert "Keychain.set" in source


def test_the_bundle_installs_the_script_under_the_name_the_connect_link_runs(
    client: TestClient,
) -> None:
    response = client.get("/widget/Flighter.scriptable")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="Flighter.scriptable"'
    bundle = response.json()
    assert bundle["name"] == "Flighter"
    assert bundle["icon"] == {"color": "deep-blue", "glyph": "plane-departure"}
    assert "Keychain.set" in bundle["script"]


def test_the_bundle_leaves_the_header_to_scriptable() -> None:
    """The app writes its own icon header on import; a second one would sit under it."""
    source = script_source()
    assert source.startswith("// Variables used by Scriptable.")
    body = script_body()
    assert not body.startswith("// Variables used by Scriptable.")
    assert body in source
    assert "icon-glyph" not in body


# --- the script -----------------------------------------------------------------------


def test_the_script_draws_what_it_is_told() -> None:
    """Every word and colour is the server's. The phase is for the server's own cadence,
    a timer element would count seconds, and nothing the server sends is a date for the
    phone to measure against its own clock."""
    source = script_source()
    assert ".phase" not in source
    assert "applyTimerStyle" not in source
    assert "applyRelativeStyle" not in source
    assert "new Date(flight" not in source
    assert "milestone_to" not in source


def test_a_widget_reload_takes_the_servers_newer_script_quietly() -> None:
    """The phone follows the server without anyone opening the app, and a widget has
    nobody to tell when it does."""
    source = script_source()
    widget_run = source[source.index("if (config.runsInWidget)") : source.index("} else {")]
    assert "updateScript(" in widget_run
    assert "notify(" not in widget_run


# --- the query ------------------------------------------------------------------------


async def test_the_widget_reads_the_newest_snapshot_of_each_flight(
    database: async_sessionmaker[AsyncSession],
) -> None:
    """Snapshots are append-only, and SQLite has no DISTINCT ON to lean on."""
    async with database() as session:
        session.add_all(
            [
                Airport(iata="JFK", name="JFK", latitude=0.0, longitude=0.0, tz="UTC"),
                Airport(iata="LAX", name="LAX", latitude=0.0, longitude=0.0, tz="UTC"),
            ]
        )
        session.add(booking(departure_local_date=DEPARTURE.date()))
        await session.flush()
        session.add_all(
            [
                snapshot(observed_at=NOW - timedelta(hours=3), gate_origin="B1"),
                snapshot(observed_at=NOW - timedelta(minutes=5), gate_origin="B22"),
            ]
        )
        await session.flush()

        rows = await widget.load_flight_rows(session, NOW)

    assert [(row.id, latest.gate_origin if latest else None) for row, latest in rows] == [
        (42, "B22")
    ]


async def _seed(session: AsyncSession, *rows: Booking) -> None:
    session.add_all(
        [
            Airport(iata="JFK", name="JFK", latitude=0.0, longitude=0.0, tz="UTC"),
            Airport(iata="LAX", name="LAX", latitude=0.0, longitude=0.0, tz="UTC"),
        ]
    )
    session.add_all(rows)
    await session.flush()


async def test_a_flight_stays_until_the_board_files_it_under_flown(
    database: async_sessionmaker[AsyncSession],
) -> None:
    """Two hours after the gate, on the ticket's times when the feed has said nothing."""
    async with database() as session:
        await _seed(
            session,
            booking(id=1, marketing_number="1", departure_local_date=DEPARTURE.date()),
            booking(
                id=2,
                marketing_number="2",
                departure_local_date=DEPARTURE.date(),
                scheduled_departure_utc=NOW - timedelta(hours=6),
                scheduled_arrival_utc=NOW - timedelta(hours=1),
            ),
            booking(
                id=3,
                marketing_number="3",
                departure_local_date=DEPARTURE.date(),
                scheduled_departure_utc=NOW - timedelta(hours=9),
                scheduled_arrival_utc=NOW - timedelta(hours=3),
            ),
        )
        rows = await widget.load_flight_rows(session, NOW)
    assert sorted(row.id for row, _ in rows) == [1, 2]


async def test_a_booking_the_poller_closed_stays_while_someone_is_walking_off_it(
    database: async_sessionmaker[AsyncSession],
) -> None:
    """The poller closes a flight ninety minutes after wheels down; the board does not."""
    async with database() as session:
        await _seed(
            session,
            booking(
                id=1,
                marketing_number="1",
                status="completed",
                departure_local_date=DEPARTURE.date(),
                scheduled_departure_utc=NOW - timedelta(hours=6),
                scheduled_arrival_utc=NOW - timedelta(hours=1),
            ),
            booking(
                id=2,
                marketing_number="2",
                status="completed",
                departure_local_date=DEPARTURE.date(),
                scheduled_departure_utc=NOW - timedelta(days=2),
                scheduled_arrival_utc=NOW - timedelta(days=2) + timedelta(hours=5),
            ),
        )
        rows = await widget.load_flight_rows(session, NOW)
    assert [row.id for row, _ in rows] == [1]


async def test_a_cancelled_flight_keeps_its_day_like_the_card_does(
    database: async_sessionmaker[AsyncSession],
) -> None:
    async with database() as session:
        await _seed(
            session,
            booking(
                id=1,
                marketing_number="1",
                status="completed",
                departure_local_date=DEPARTURE.date(),
                scheduled_departure_utc=NOW - timedelta(hours=1),
            ),
        )
        session.add(snapshot(booking_id=1, cancelled=True, observed_at=NOW - timedelta(hours=2)))
        await session.flush()
        rows = await widget.load_flight_rows(session, NOW)
    assert [(row.id, snap.cancelled if snap else None) for row, snap in rows] == [(1, True)]


def test_at_the_gate_the_status_says_arrived(settings: Settings) -> None:
    parked = snapshot(
        actual_off=DEPARTURE, actual_on=ARRIVAL - timedelta(minutes=10), actual_in=ARRIVAL
    )
    flight = payload([(booking(), parked)], settings)["flights"][0]
    assert flight["status_label"] == "Arrived"
    assert flight["milestone_label"] == "Baggage claim"
