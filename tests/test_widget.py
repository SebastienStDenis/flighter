"""The widget contract. Anything asserted here is something the phone renders literally."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
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
from flighter.models import KV, Airport, Booking, FlightSnapshot
from flighter.views import until
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


# --- payload shaping ------------------------------------------------------------------


def test_upcoming_flight(settings: Settings) -> None:
    far = booking(scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings)["flights"][0]
    assert flight == {
        "detail_url": "https://flights.example.com/f/42",
        "phase": "upcoming",
        "title": "DL1234  JFK → LAX",
        "subtitle": None,
        "status_label": "Scheduled",
        "status_tone": "quiet",
        "milestone_label": "Scheduled",
        "milestone_to": "2026-09-18T18:00:00Z",
        "progress_percent": None,
    }


def test_a_flight_without_a_feed_is_named_by_the_day_at_its_origin(settings: Settings) -> None:
    """NOW is 18:00 UTC on the 12th: 14:00 in New York, 03:00 the next day in Tokyo."""
    early = booking(origin_iata="HND", scheduled_departure_utc=NOW + timedelta(hours=8))
    zones = {"HND": "Asia/Tokyo", "JFK": "America/New_York"}

    assert payload([(early, None)], settings, zones=zones)["flights"][0]["status_label"] == "Today"
    late = booking(scheduled_departure_utc=NOW + timedelta(hours=12))
    assert payload([(late, None)], settings, zones=zones)["flights"][0]["status_label"] == (
        "Tomorrow"
    )
    # With no airport on file the day is read off UTC rather than left blank.
    assert payload([(late, None)], settings)["flights"][0]["status_label"] == "Tomorrow"
    assert payload([(early, None)], settings)["flights"][0]["status_label"] == "Tomorrow"


def test_day_of_shows_gate_and_terminal(settings: Settings) -> None:
    gated = snapshot(scheduled_out=DEPARTURE, gate_origin="B22", terminal_origin="4")
    flight = payload([(booking(), gated)], settings)["flights"][0]
    assert flight["phase"] == "day_of"
    assert flight["subtitle"] == "Gate B22 · Terminal 4"
    assert flight["status_label"] == "On time"
    assert flight["status_tone"] == "ok"
    assert flight["milestone_label"] == "Departs in"
    assert flight["milestone_to"] == "2026-09-12T18:40:00Z"


def test_upcoming_falls_back_to_the_seat(settings: Settings) -> None:
    flight = payload([(booking(seat="14A"), snapshot())], settings)["flights"][0]
    assert flight["subtitle"] == "Seat 14A"


def test_the_run_up_to_departure_keeps_counting(settings: Settings) -> None:
    """The half hour before departure is when the number matters most, so it must not be
    traded for a word about boarding that no feed reports."""
    imminent = snapshot(scheduled_out=NOW + timedelta(minutes=20), gate_origin="B22")
    flight = payload([(booking(), imminent)], settings)["flights"][0]
    assert flight["phase"] == "day_of"
    assert flight["milestone_label"] == "Departs in"
    assert flight["milestone_to"] == "2026-09-12T18:20:00Z"


def test_taxiing_counts_to_the_landing_and_does_not_name_the_gate_it_left(
    settings: Settings,
) -> None:
    """Nothing upstream estimates wheels up, so the next rung with a time is the landing."""
    taxiing = snapshot(
        scheduled_out=NOW - timedelta(minutes=5),
        actual_out=NOW - timedelta(minutes=2),
        gate_origin="B22",
    )
    flight = payload([(booking(), taxiing)], settings)["flights"][0]
    assert flight["phase"] == "taxiing"
    assert flight["status_label"] == "Taxiing"
    assert flight["status_tone"] == "live"
    assert flight["milestone_label"] == "Lands in"
    assert flight["milestone_to"] == "2026-09-12T22:15:00Z"
    assert flight["subtitle"] is None


def test_airborne_counts_down_to_landing_and_shows_progress(settings: Settings) -> None:
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
    assert flight["milestone_label"] == "Lands in"
    assert flight["milestone_to"] == "2026-09-12T22:40:00Z"
    # Eighty minutes into a six-hour span by the clock, whatever the last poll said.
    assert flight["progress_percent"] == 22
    assert flight["subtitle"] == "Gate 12 · Terminal B"
    assert flight["status_label"] == "Arriving late"
    assert flight["status_tone"] == "warn"


def test_landed_keeps_the_gate_while_it_counts_to_it(settings: Settings) -> None:
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
    assert flight["subtitle"] == "Gate 12 · Terminal B"
    assert flight["milestone_label"] == "At the gate in"
    assert flight["milestone_to"] == "2026-09-12T22:15:00Z"
    # A landed flight must never show the departure gate it left hours ago.
    assert flight["progress_percent"] is None


def test_at_the_gate_there_is_nothing_left_to_count(settings: Settings) -> None:
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
    assert flight["subtitle"] == "Bag claim 7 · Terminal B"
    assert flight["milestone_label"] is None
    assert flight["milestone_to"] is None


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
    assert flight["subtitle"] == "Bag claim 7"


def test_cancelled_has_nothing_to_count_to(settings: Settings) -> None:
    flight = payload([(booking(), snapshot(cancelled=True))], settings)["flights"][0]
    assert flight["phase"] == "cancelled"
    assert flight["status_label"] == "Cancelled"
    assert flight["status_tone"] == "stop"
    assert flight["subtitle"] is None
    assert flight["milestone_label"] is None
    assert flight["milestone_to"] is None


def test_diverted_still_lands_somewhere(settings: Settings) -> None:
    diverted = snapshot(diverted=True, actual_off=DEPARTURE, estimated_in=ARRIVAL)
    flight = payload([(booking(), diverted)], settings)["flights"][0]
    assert flight["phase"] == "diverted"
    assert flight["status_label"] == "Diverted"
    assert flight["status_tone"] == "stop"
    assert flight["subtitle"] == "Diverted"
    assert flight["milestone_label"] == "Lands in"
    assert flight["milestone_to"] == "2026-09-12T22:15:00Z"


def test_a_late_departure_is_the_pill_the_board_shows(settings: Settings) -> None:
    waiting = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(minutes=30))
    flight = payload([(booking(), waiting)], settings)["flights"][0]
    assert flight["status_label"] == "Departure delayed"
    assert flight["status_tone"] == "warn"
    assert flight["milestone_label"] == "Departs in"
    assert flight["milestone_to"] == "2026-09-12T19:10:00Z"


def test_a_minute_late_is_still_on_time(settings: Settings) -> None:
    jitter = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(minutes=1))
    flight = payload([(booking(), jitter)], settings)["flights"][0]
    assert flight["status_label"] == "On time"
    assert flight["status_tone"] == "ok"


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


def test_every_instant_is_utc_with_a_z(settings: Settings) -> None:
    """The phone renders these relative to its own clock; a missing zone shifts it."""
    rows: list[FlightRow] = [
        (booking(id=1), snapshot(scheduled_out=NOW + timedelta(minutes=20))),
        (booking(id=2), snapshot(actual_off=DEPARTURE, estimated_in=ARRIVAL)),
        (booking(id=3, scheduled_departure_utc=NOW + timedelta(days=4)), None),
    ]
    body = payload(rows, settings)
    found = list(_instants(body))
    assert len(found) == 3  # one milestone per flight
    assert all(instant.endswith("Z") for instant in found)


def test_instants_survive_a_non_utc_input(settings: Settings) -> None:
    """AeroAPI states offsets; whatever arrives must leave as Z."""
    tokyo = datetime(2026, 9, 12, 18, 40, tzinfo=UTC).astimezone()
    flight = payload([(booking(scheduled_departure_utc=tokyo), None)], settings)["flights"][0]
    assert flight["milestone_to"] == "2026-09-12T18:40:00Z"


# --- ordering and cadence -------------------------------------------------------------


def test_in_progress_first_then_soonest_capped_at_three(settings: Settings) -> None:
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
        (booking(id=5, scheduled_departure_utc=NOW + timedelta(minutes=10)), None),
    ]
    body = payload(rows, settings)
    # Airborne, then departing in ten minutes, then tomorrow. The landed flight and the
    # one three days out lose their seats.
    assert [_id(flight) for flight in body["flights"]] == [3, 5, 1]


def test_landed_sinks_below_what_is_still_coming(settings: Settings) -> None:
    rows: list[FlightRow] = [
        (
            booking(id=4, scheduled_departure_utc=NOW - timedelta(hours=8)),
            snapshot(actual_on=NOW - timedelta(hours=1)),
        ),
        (booking(id=2, scheduled_departure_utc=NOW + timedelta(days=3)), None),
    ]
    assert [_id(flight) for flight in payload(rows, settings)["flights"]] == [2, 4]


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
    assert body["flights"][0]["title"] == "DL1234  JFK → LAX"


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


def test_airborne_milestone_is_touchdown_not_the_gate(settings: Settings) -> None:
    """The number someone reads from seat 32A is time to wheels down.

    Taxiing to a stand is ten minutes nobody counts, so a countdown aimed at the gate
    is wrong for the whole stretch of the flight anyone is watching it.
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
    assert flight["milestone_label"] == "Lands in"
    assert flight["milestone_to"] == "2026-09-12T22:04:00Z"


def test_a_late_pushback_is_history_once_the_flight_is_off_the_ground(
    settings: Settings,
) -> None:
    """A flight that left the gate late but is landing on time is not delayed, and saying
    so for the rest of the cruise makes the pill mean nothing."""
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


# --- the script -----------------------------------------------------------------------


def test_the_script_draws_what_it_is_told() -> None:
    """Every word and colour is the server's. The phase is for the server's own ranking,
    and a timer element would count seconds, which nothing on any screen shows."""
    source = script_source()
    assert ".phase" not in source
    assert "applyTimerStyle" not in source
    assert "applyRelativeStyle" not in source


def test_a_widget_reload_takes_the_servers_newer_script_quietly() -> None:
    """The phone follows the server without anyone opening the app, and a widget has
    nobody to tell when it does."""
    source = script_source()
    widget_run = source[source.index("if (config.runsInWidget)") : source.index("} else {")]
    assert "updateScript(" in widget_run
    assert "notify(" not in widget_run


FIGURES = [
    timedelta(days=3, hours=5),
    timedelta(hours=24, minutes=1),
    timedelta(hours=23, minutes=59, seconds=30),
    timedelta(hours=1, minutes=5, seconds=30),
    timedelta(minutes=12, seconds=30),
    timedelta(seconds=40),
    timedelta(minutes=-20, seconds=-30),
    timedelta(seconds=-20),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the widget script")
def test_the_script_builds_the_same_figure_as_the_page() -> None:
    """The board and the lock screen must never disagree about how far off a flight is."""
    source = script_source()
    start = source.index("function figure(ms)")
    end = source.index("function staleNote(", start)
    offsets = [int(ahead.total_seconds() * 1000) for ahead in FIGURES]
    program = (
        f"{source[start:end]} console.log(JSON.stringify({json.dumps(offsets)}.map("
        "function (ms) { return until(new Date(Date.now() + ms)); })));"
    )
    rendered = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    ).stdout
    now = datetime.now(UTC)
    assert json.loads(rendered) == [until(now + ahead) for ahead in FIGURES]


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


def test_a_diversion_renames_the_destination(settings: Settings) -> None:
    diverted = snapshot(
        diverted=True, destination_iata="YOW", actual_off=DEPARTURE, estimated_in=ARRIVAL
    )
    flight = payload([(booking(), diverted)], settings)["flights"][0]
    assert flight["title"].endswith("→ YOW")
    assert flight["subtitle"] == "Diverted to YOW"
    assert flight["status_label"] == "Diverted"


def test_at_the_gate_the_pill_says_arrived(settings: Settings) -> None:
    parked = snapshot(
        actual_off=DEPARTURE, actual_on=ARRIVAL - timedelta(minutes=10), actual_in=ARRIVAL
    )
    flight = payload([(booking(), parked)], settings)["flights"][0]
    assert flight["status_label"] == "Arrived"
    assert flight["milestone_label"] is None


def test_a_milestone_whose_time_has_passed_is_due_not_ago(settings: Settings) -> None:
    overdue = snapshot(
        actual_off=DEPARTURE, actual_on=ARRIVAL - timedelta(minutes=10), estimated_in=ARRIVAL
    )
    later = build_payload(
        [(booking(), overdue)],
        settings=settings,
        now=ARRIVAL + timedelta(minutes=18),
        base_url="https://flights.example.com",
    )
    flight = later.model_dump(mode="json")["flights"][0]
    assert flight["milestone_label"] == "Due at the gate"
    assert flight["milestone_to"] == "2026-09-12T22:15:00Z"
