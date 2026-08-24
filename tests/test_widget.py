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

from flighter import prefs, views, widget
from flighter.aeroapi import BREAKER_KEY, month_key
from flighter.config import Settings, get_settings
from flighter.db import get_session
from flighter.models import KV, Airport, Booking, BookingStatus, FlightSnapshot
from flighter.phase import compute_phase
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


def detail(flight: dict[str, Any]) -> Any:
    """The one line under the pill: where to be, or the day it leaves while nothing counts."""
    return flight["detail"]


def counting(flight: dict[str, Any]) -> tuple[Any, Any]:
    """The right-hand side of the row: what it counts to, and the instant it counts to."""
    return flight["milestone_label"], flight["milestone_at"]


# --- payload shaping ------------------------------------------------------------------


def test_upcoming_flight(settings: Settings) -> None:
    far = booking(scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings)["flights"][0]
    # Days out the day and time it leaves is the whole story: there is no gate to find
    # yet, and the pill says only that it is booked.
    assert flight == {
        "detail_url": "https://flights.example.com/f/42",
        "phase": "upcoming",
        "logo_url": "https://www.gstatic.com/flights/airline_logos/70px/DL.png",
        "number": "DL1234",
        "route": "JFK → LAX",
        "status_label": "Scheduled",
        "status_tone": "quiet",
        "detail": "Fri 18 Sep 18:00 UTC",
        # Nobody counts the hours to a flight next week, and the board's card carries no
        # footer for one either.
        "milestone_label": None,
        "milestone_at": None,
    }


def test_the_day_it_leaves_is_read_at_the_origin(settings: Settings) -> None:
    far = booking(origin_iata="HND", scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings, airports=AIRPORTS)["flights"][0]
    assert detail(flight) == "Sat 19 Sep 03:00 JST"


def test_a_departure_not_today_carries_its_day(settings: Settings) -> None:
    """NOW is 18:00 UTC on the 12th: 14:00 in New York, 03:00 the next day in Tokyo.

    A time on its own reads as today's, so a flight leaving tomorrow evening would look
    hours overdue all day without the day in front of it. The board names the day in the
    pill when the feed has not picked the flight up; here the line does, and the pill
    says only that it is booked.
    """
    late = booking(scheduled_departure_utc=NOW + timedelta(hours=30))
    flight = payload([(late, None)], settings, airports=AIRPORTS)["flights"][0]
    assert detail(flight) == "Tomorrow 20:00 EDT"
    assert flight["status_label"] == "Scheduled"
    assert flight["status_tone"] == "quiet"

    # With no airport on file the day is read off UTC rather than left blank.
    assert detail(payload([(late, None)], settings)["flights"][0]) == "Mon 14 Sep 00:00 UTC"


def test_a_flight_inside_its_day_counts_rather_than_naming_a_time(settings: Settings) -> None:
    """The board stops naming the time and starts counting to it, and so does this.

    The count is the instant itself: the phone ticks it down between reloads, which is
    the one thing a widget cannot do with a figure worked out here.
    """
    tomorrow = NOW + timedelta(hours=12)
    flight = payload([(booking(), snapshot(scheduled_out=tomorrow))], settings, airports=AIRPORTS)[
        "flights"
    ][0]
    assert flight["status_label"] == "On time"
    assert counting(flight) == ("Departs in", "2026-09-13T06:00:00Z")
    assert "06:00" not in str(detail(flight))


def test_day_of_shows_the_terminal_the_gate_and_the_seat(settings: Settings) -> None:
    """Everything a person on their way to the airport is walking towards, in the boxes
    the card draws them in. When it leaves is the count on the other side of the row."""
    gated = snapshot(scheduled_out=DEPARTURE, gate_origin="B22", terminal_origin="4")
    flight = payload([(booking(seat="14A"), gated)], settings, airports=AIRPORTS)["flights"][0]
    assert flight["phase"] == "day_of"
    assert detail(flight) == "TERM 4 · GATE B22 · SEAT 14A"
    assert counting(flight) == ("Departs in", "2026-09-12T18:40:00Z")
    assert flight["status_label"] == "On time"
    assert flight["status_tone"] == "ok"


def test_day_of_with_nothing_assigned_yet_is_the_boxes_waiting(settings: Settings) -> None:
    """Dashed rather than dropped: an empty box is the airport not having said yet, and
    a line that comes and goes as gates are published is a row that moves under the eye."""
    flight = payload([(booking(), snapshot())], settings)["flights"][0]
    assert detail(flight) == "TERM - · GATE -"


def test_a_delayed_departure_counts_to_the_time_it_now_leaves(settings: Settings) -> None:
    held = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(minutes=30))
    flight = payload([(booking(seat="14A"), held)], settings)["flights"][0]
    assert flight["status_label"] == "Departure delayed"
    assert flight["status_tone"] == "warn"
    # Delayed to when is the whole question the pill leaves open, and the count answers
    # it from the estimate rather than from the schedule.
    assert counting(flight) == ("Departs in", "2026-09-12T19:10:00Z")
    assert detail(flight) == "TERM - · GATE - · SEAT 14A"


def test_the_run_up_to_departure_keeps_the_gate(settings: Settings) -> None:
    """The half hour before departure is when the gate matters most, so it must not be
    traded for a word about boarding that no feed reports."""
    imminent = snapshot(scheduled_out=NOW + timedelta(minutes=20), gate_origin="B22")
    flight = payload([(booking(), imminent)], settings)["flights"][0]
    assert flight["phase"] == "day_of"
    assert detail(flight) == "TERM - · GATE B22"
    assert counting(flight) == ("Departs in", "2026-09-12T18:20:00Z")


def test_pushback_clears_the_gate_off_the_line(settings: Settings) -> None:
    """The gate it left is behind the person reading this, so the line empties; nothing
    upstream estimates wheels up, so the count is to the landing."""
    taxiing = snapshot(
        scheduled_out=NOW - timedelta(minutes=5),
        actual_out=NOW - timedelta(minutes=2),
        gate_origin="B22",
        terminal_origin="4",
        gate_destination="12",
        terminal_destination="B",
    )
    flight = payload([(booking(), taxiing)], settings, airports=AIRPORTS)["flights"][0]
    assert flight["phase"] == "taxiing"
    assert flight["status_label"] == "Taxiing"
    assert flight["status_tone"] == "live"
    assert detail(flight) is None
    assert counting(flight) == ("Lands in", "2026-09-12T22:15:00Z")


def test_airborne_counts_to_the_landing_and_keeps_only_the_seat(settings: Settings) -> None:
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
    flight = payload([(booking(seat="32A"), flying)], settings)["flights"][0]
    assert flight["phase"] == "airborne"
    # The gate at the other end is not worth the width from seat 32A; when it lands is,
    # and the seat is the last thing on the ticket still worth carrying.
    assert detail(flight) == "SEAT 32A"
    assert counting(flight) == ("Lands in", "2026-09-12T22:40:00Z")
    assert flight["status_label"] == "Arriving late"
    assert flight["status_tone"] == "warn"


def test_landed_counts_to_the_gate(settings: Settings) -> None:
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
    assert detail(flight) is None
    assert counting(flight) == ("At the gate in", "2026-09-12T22:15:00Z")


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
    assert detail(flight) == "Baggage claim 7"
    # Parked, there is nothing left ahead to count to, exactly as the card has nothing
    # left in its footer but the belt.
    assert counting(flight) == (None, None)


def test_a_belt_nobody_has_named_leaves_the_line_empty(settings: Settings) -> None:
    """The card has a labelled cell to put a dash in; a line has nothing to say."""
    done = snapshot(actual_off=DEPARTURE, actual_on=ARRIVAL, actual_in=ARRIVAL)
    flight = payload([(booking(), done)], settings)["flights"][0]
    assert flight["status_label"] == "Arrived"
    assert detail(flight) is None


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
    assert detail(flight) == "Baggage claim 7"


def test_a_time_that_has_passed_says_it_is_due(settings: Settings) -> None:
    """Wheels down is published minutes after the fact, and the board says "due" until it
    is. The same word here, over a count the phone lets run past zero: waiting is what
    both of them are describing."""
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
    assert counting(flight) == ("Due to land", "2026-09-12T22:05:00Z")


def test_cancelled_has_no_time_to_give(settings: Settings) -> None:
    flight = payload([(booking(), snapshot(cancelled=True))], settings)["flights"][0]
    assert flight["phase"] == "cancelled"
    assert flight["status_label"] == "Cancelled"
    assert flight["status_tone"] == "stop"
    assert detail(flight) is None


def test_a_booking_the_poller_closed_in_the_air_has_no_time_to_give(
    settings: Settings,
) -> None:
    """The feed lost it. A landing hours in the past is no time to name."""
    lost = snapshot(actual_off=NOW - timedelta(hours=9), estimated_in=NOW - timedelta(hours=3))
    closed = booking(status=BookingStatus.COMPLETED)
    flight = payload([(closed, lost)], settings)["flights"][0]
    assert flight["status_label"] == "Flown"
    assert detail(flight) is None


def test_a_diversion_renames_the_destination_and_reads_its_clock(settings: Settings) -> None:
    diverted = snapshot(
        diverted=True, destination_iata="YOW", actual_off=DEPARTURE, estimated_in=ARRIVAL
    )
    flight = payload([(booking(), diverted)], settings, airports=AIRPORTS)["flights"][0]
    assert flight["phase"] == "diverted"
    assert flight["route"] == "JFK → YOW"
    assert flight["status_label"] == "Diverted"
    # The route names where it is bound and the count says how long until it is there.
    assert counting(flight) == ("Lands in", "2026-09-12T22:15:00Z")


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
    assert counting(flight) == ("Lands in", "2026-09-12T22:04:00Z")


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


def test_the_pill_is_the_boards_pill_and_nothing_else(settings: Settings) -> None:
    """One flight, one word for it. A widget that says Departed while the page says
    Taxiing is two answers to one question, and whoever reads both has no way to tell
    which of them is the stale one - so the widget rephrases nothing on the way out.
    """
    rows: list[FlightRow] = [
        # Nothing from the feed yet, inside the day: the board names the day.
        (booking(id=1), None),
        # Pushed back and rolling, which is the word the board had its own name for.
        (
            booking(id=2),
            snapshot(
                booking_id=2,
                scheduled_out=NOW - timedelta(minutes=5),
                actual_out=NOW - timedelta(minutes=2),
            ),
        ),
        (
            booking(id=3),
            snapshot(booking_id=3, actual_off=DEPARTURE, estimated_in=ARRIVAL),
        ),
    ]
    # The board's order, not the order they were written down in.
    drawn = {
        _id(flight): flight for flight in payload(rows, settings, airports=AIRPORTS)["flights"]
    }
    for this, snap in rows:
        board = views.status(
            compute_phase(this, snap, NOW),
            this,
            snap,
            now=NOW,
            origin_tz=AIRPORTS[this.origin_iata].tz,
        )
        flight = drawn[this.id]
        assert (flight["status_label"], flight["status_tone"]) == (board.label, board.tone)
    assert [drawn[n]["status_label"] for n in (1, 2, 3)] == ["Today", "Taxiing", "In the air"]


# --- the phone's own clock --------------------------------------------------------------


HOME = "America/Toronto"


def test_the_one_time_still_drawn_is_on_the_phones_clock(settings: Settings) -> None:
    """A time four zones away is arithmetic, not information.

    Watching from Ottawa, a flight leaving Tokyo at 03:00 on the 19th leaves at 14:00 on
    the 18th on the watch of the person reading it, and that is the figure - the only
    figure. The airport's own reading of the same instant is not set beside it: a line
    with two times on it is a line that has to be worked out rather than read.
    """
    far = booking(origin_iata="HND", scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings, airports=AIRPORTS, viewer_tz=HOME)["flights"][0]
    assert detail(flight) == "Fri 18 Sep 14:00"
    assert "JST" not in detail(flight) and "EDT" not in detail(flight)


def test_the_day_in_front_is_the_phones_day(settings: Settings) -> None:
    """The day belongs to the clock the line is read on, or it contradicts it.

    NOW is the 12th in Toronto, and a flight leaving Tokyo at 10:00 on the 14th JST
    leaves at 21:00 on the 13th where it is being read: tomorrow to the person holding
    the phone, and the day after tomorrow at the airport.
    """
    tokyo = booking(origin_iata="HND", scheduled_departure_utc=NOW + timedelta(hours=31))
    flight = payload([(tokyo, None)], settings, airports=AIRPORTS, viewer_tz=HOME)["flights"][0]
    assert detail(flight) == "Tomorrow 21:00"


def test_a_phone_that_says_nothing_gets_the_airports_clock_and_its_zone(
    settings: Settings,
) -> None:
    """What every copy drew before the zone was sent, and still right, just harder work.

    This is the one case a zone is named: the time is not on the clock in the reader's
    hand, and a bare figure would be read as though it were.
    """
    far = booking(origin_iata="HND", scheduled_departure_utc=NOW + timedelta(days=6))
    rows: list[FlightRow] = [(far, None)]
    silent = payload(rows, settings, airports=AIRPORTS)["flights"][0]
    assert detail(silent) == "Sat 19 Sep 03:00 JST"
    blank = payload(rows, settings, airports=AIRPORTS, viewer_tz="")["flights"][0]
    assert detail(blank) == "Sat 19 Sep 03:00 JST"


def test_a_zone_the_phone_made_up_does_not_break_the_widget(settings: Settings) -> None:
    """`zone()` falls back to UTC on a name it does not know, and a widget that draws
    the wrong clock is still better than one that draws an error."""
    far = booking(scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings, airports=AIRPORTS, viewer_tz="Mars/Olympus_Mons")[
        "flights"
    ][0]
    assert detail(flight) == "Fri 18 Sep 18:00"


def test_the_phones_zone_reaches_the_payload_from_the_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one thing the server cannot work out for itself."""
    leaves = (datetime.now(UTC) + timedelta(days=6)).replace(
        hour=18, minute=40, second=0, microsecond=0
    )

    async def far_out(_session: Any, _now: datetime) -> list[FlightRow]:
        return [(booking(scheduled_departure_utc=leaves), None)]

    monkeypatch.setattr(widget, "load_flight_rows", far_out)
    response = client.get(
        "/api/widget?tz=Asia/Tokyo", headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 200
    # The flight leaves JFK at 18:40 UTC, which is 03:40 the next morning in Tokyo, and
    # Tokyo is where the phone says it is.
    assert response.json()["flights"][0]["detail"].endswith(" 03:40")


def test_the_script_tells_the_server_where_the_phone_is() -> None:
    source = script_source()
    assert "tz=${encodeURIComponent(timeZone())}" in source
    assert "resolvedOptions().timeZone" in source


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


def test_the_only_instants_are_the_ones_the_phone_counts_down(settings: Settings) -> None:
    """The phone draws at reload and iOS reloads when it likes, so a figure worked out
    here is a quarter of an hour wrong by the next one. Every string in the payload is
    therefore a clock face - except the one the phone does not read at all: WidgetKit
    counts a date down itself, ticking between reloads, so what it is counting to goes
    over as the instant."""
    rows: list[FlightRow] = [
        (booking(id=1), snapshot(scheduled_out=NOW + timedelta(minutes=20))),
        (booking(id=2), snapshot(actual_off=DEPARTURE, estimated_in=ARRIVAL)),
        (booking(id=3, scheduled_departure_utc=NOW + timedelta(days=4)), None),
    ]
    built = payload(rows, settings)
    assert list(_instants(built)) == [
        flight["milestone_at"] for flight in built["flights"] if flight["milestone_at"]
    ]
    assert list(_instants(built)) == ["2026-09-12T18:20:00Z", "2026-09-12T22:15:00Z"]


def test_a_non_utc_input_is_read_at_the_origins_clock(settings: Settings) -> None:
    """AeroAPI states offsets; whatever arrives is a clock at the airport on the way out."""
    tokyo = datetime(2026, 9, 18, 18, 40, tzinfo=UTC).astimezone()
    flight = payload([(booking(scheduled_departure_utc=tokyo), None)], settings)["flights"][0]
    assert detail(flight) == "Fri 18 Sep 18:40 UTC"


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


def test_the_reload_is_asked_for_when_the_count_reaches_zero(settings: Settings) -> None:
    """A word does not tick and a date does, which is the whole of the problem.

    "Departs in" is drawn once and stays drawn, so a count that runs past zero under it
    reads as four minutes to go when the flight is four minutes overdue. Nothing on the
    phone can fix that between reloads, so the reload is asked for at the instant the
    wording changes rather than at the usual ten minutes.
    """
    close = snapshot(scheduled_out=NOW + timedelta(minutes=4))
    assert payload([(booking(), close)], settings)["refresh_seconds"] == 240


def test_a_count_further_out_than_the_cadence_does_not_slow_it_down(
    settings: Settings,
) -> None:
    """The poller's cadence is still the ceiling: a rung two hours off is not a reason
    to stop asking about the gate for two hours."""
    later = snapshot(scheduled_out=NOW + timedelta(hours=2))
    assert payload([(booking(), later)], settings)["refresh_seconds"] == 600


def test_a_rung_seconds_away_is_not_worth_a_reload_of_its_own(settings: Settings) -> None:
    """iOS budgets reloads across every widget on the phone, and the one a minute later
    says exactly what the one ten seconds from now would have said."""
    imminent = snapshot(scheduled_out=NOW + timedelta(seconds=10))
    assert payload([(booking(), imminent)], settings)["refresh_seconds"] == 60


def test_a_count_already_past_zero_is_not_asked_about_again(settings: Settings) -> None:
    """It is drawn as "Due to depart" already; there is no later instant to wake for."""
    overdue = snapshot(scheduled_out=NOW - timedelta(minutes=3))
    flight = payload([(booking(), overdue)], settings)
    assert counting(flight["flights"][0])[0] == "Due to depart"
    assert flight["refresh_seconds"] == 600


# --- what the count does when the label cannot follow it --------------------------------


def test_the_count_is_the_instant_and_nothing_but_the_instant(settings: Settings) -> None:
    """The phone is handed a date and told to tick it, and that is the whole contract.

    The label beside it is a word, and words do not tick: whatever "Departs in" said when
    the payload was built is what it says until something reloads the widget, while the
    figure goes on climbing past zero underneath it. Nothing on the phone can repair that
    - swapping a drawn glyph for another needs a drawing, and WidgetKit hands out one per
    reload - so the payload does not try, and the reload asked for at the instant is the
    only thing standing between the row and a wrong reading.
    """
    ahead = snapshot(scheduled_out=NOW + timedelta(minutes=4))
    flight = payload([(booking(), ahead)], settings)["flights"][0]
    assert counting(flight) == ("Departs in", "2026-09-12T18:04:00Z")
    assert set(flight) == {
        "detail_url",
        "phase",
        "logo_url",
        "number",
        "route",
        "status_label",
        "status_tone",
        "detail",
        "milestone_label",
        "milestone_at",
    }


def test_a_count_whose_label_has_caught_up_runs_upwards(settings: Settings) -> None:
    """Once the label itself says "Due to depart" there is nothing left to disagree with,
    and how far past due a flight is is worth knowing."""
    overdue = snapshot(scheduled_out=NOW - timedelta(minutes=3))
    flight = payload([(booking(), overdue)], settings)["flights"][0]
    assert counting(flight)[0] == "Due to depart"


def test_the_script_draws_the_count_and_never_stands_it_down() -> None:
    """A timer is the one thing on the widget WidgetKit keeps ticking between reloads,
    and the one thing Scriptable gives no way to stop: `applyTimerStyle` takes no end
    date, no pause and no direction, so a count past its instant climbs and there is no
    branch anywhere that could put a word in its place."""
    source = script_source()
    timer = source[source.index("function countdown(") : source.index("function pill(")]
    assert "addText" not in timer
    assert "milestone_due" not in source


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
    """Every word and colour is the server's, and the phase is for the server's own
    cadence rather than for anything drawn.

    The one figure the script is not handed is the count: it is given the instant and
    lets WidgetKit tick it, which is the only way a countdown is right on a widget iOS
    reloads a few times an hour. Nothing else in the payload is a date to measure.
    """
    source = script_source()
    assert ".phase" not in source
    assert "applyTimerStyle()" in source
    assert source.count("new Date(flight") == 1
    assert "new Date(flight.milestone_at)" in source


def test_the_count_is_held_against_the_end_of_the_row() -> None:
    """A timer is the one element WidgetKit cannot measure before drawing it, so it is
    given the whole of what the spacer left and draws inside that. Unaligned it sits at
    the right in Scriptable's preview, which measures a snapshot, and part-way along the
    row on the home screen - the same widget disagreeing with itself."""
    source = script_source()
    timer = source[source.index("function countdown(") : source.index("function pill(")]
    assert "rightAlignText()" in timer


def test_the_count_is_the_weight_the_row_is_read_for() -> None:
    """Semibold and bold are the same weight to look at when a Mac draws an iPhone's
    widget, so a count set in semibold reads as heavier than its row on a mirrored
    screen and no different from it on the phone. The phone is the screen this is for."""
    source = script_source()
    timer = source[source.index("function countdown(") : source.index("function pill(")]
    assert "Font.boldMonospacedSystemFont(size)" in timer


def test_the_footer_ages_itself_rather_than_stating_a_figure() -> None:
    """How old what is on screen is has the same problem as the countdown and the same
    answer: a figure worked out at draw time is wrong within the minute and flatteringly
    so, and WidgetKit will count a past date upwards for nothing."""
    source = script_source()
    line = source[source.index("function updatedLine(") : source.index("function footerSize(")]
    assert "applyTimerStyle()" in line
    assert '"Last updated"' in line and '"Cached"' in line
    # Drawn from when the data landed, which is the fetch when there was one and the
    # cache file's own date when the server could not be reached.
    assert "result.fetchedAt" in line
    assert (
        "new Date()"
        in source[source.index("async function load(") : source.index("async function request(")]
    )


def test_a_count_is_boxed_to_the_reading_it_is_about_to_show() -> None:
    """A timer is the one element WidgetKit cannot measure before it draws it, so left to
    itself it is handed every point the spacer left and keeps what it does not use. That
    room comes off the line beside it: the gate and the seat are cut short to make space
    a count never fills. Boxed to its own reading, the count takes what it needs and the
    line keeps the rest - and it is the same line in Scriptable, which measures a
    snapshot, as on the home screen, which cannot."""
    source = script_source()
    for name, ends in (("countdown(", "function pill("), ("updatedLine(", "function timerWidth(")):
        drawn = source[source.index(f"function {name}") : source.index(ends)]
        assert "box.size = new Size(timerWidth(" in drawn, name
        assert "applyTimerStyle()" in drawn, name
        # And never shrinks into that box: a figure drawn at four fifths of the size it
        # was asked for is the bug the box is there to prevent, not the fallback.
        assert "minimumScaleFactor = 0.95" in drawn, name


def test_the_age_ends_its_line_with_nothing_drawn_after_it() -> None:
    """The word leads and the figure ends the phrase. A timer is the one element in it
    whose width is not known before it is drawn, so with nothing after it whatever its
    box has over falls past the end of the phrase rather than inside it - which is what
    reading "Updated 04:12" rather than "Updated 04:12 ago" is worth."""
    source = script_source()
    line = source[source.index("function updatedLine(") : source.index("function timerWidth(")]
    assert "leftAlignText()" in line
    assert "rightAlignText()" not in line
    assert "addText" not in line[line.index("box.size") :]


def test_the_age_sits_in_the_middle_of_the_widget() -> None:
    """The one line on a widget that belongs to the whole of it rather than to a flight,
    and the only one not starting where the flights start.

    A spacer at each end rather than one at the right: the two share what the phrase
    leaves and hold it between them. The word alone is centred the same way, which is why
    the cache with no date on it cannot take an early return out of the middle of this.
    And no glyph of slack in the timer's box here, the way there is on a count held
    against the end of a row: a centred phrase is measured from both ends, so room the box
    has over pushes the words off-centre rather than falling off where the line stops.
    """
    source = script_source()
    line = source[source.index("function updatedLine(") : source.index("function timerWidth(")]
    assert line.count("line.addSpacer();") == 2
    assert "return;" not in line
    assert "box.size = new Size(timerWidth(result.fetchedAt, size), 0);" in line
    # The reason above it is part of the same block and centred with it: a sentence
    # starting where the flights start reads as one more row of the list.
    footer = source[source.index("function footer(") : source.index("function updatedLine(")]
    assert "text.centerAlignText();" in footer


def test_a_small_widget_holds_two_flights_of_four_lines() -> None:
    """The number and the route, the pill, where to be, and what it is counting to. The
    route used to take a line to itself, which is a line out of eight on a widget that
    has room for eight; it shrinks against the number instead - `titleRow`'s own scale
    factor is what lets it - and the height that frees is the second flight."""
    source = script_source()
    assert "const SMALL_FLIGHTS = 2;" in source
    assert "flights.slice(0, SMALL_FLIGHTS)" in source
    drawn = source[source.index("function renderSmall(") : source.index("function renderList(")]
    assert "titleRow(widget, flight, logos, 11, true)" in drawn
    assert "pill(state, flight)" in drawn
    assert "milestoneWord(line, flight, 9)" in drawn
    # Every line of a flight is the same distance under the one above it, and the only
    # wider gap is the one that separates two flights.
    assert drawn.count("widget.addSpacer(SMALL_GAP)") == 3
    assert "widget.addSpacer(SMALL_GAP * 2)" in drawn
    # And nothing on those lines carries air of its own inside that distance: a pill with
    # its own padding is a line held further from its neighbours than any other.
    assert 'const pad = family === "small" ? 0 : 2;' in source


def test_every_home_screen_size_says_how_old_what_it_is_showing_is() -> None:
    """Including the small one, holding two flights of four lines. Only the lock screen
    goes without, where three lines is the whole widget and the age is said in the
    message instead."""
    source = script_source()
    drawn = source[
        source.index("async function buildWidget(") : source.index("function newWidget(")
    ]
    assert "footer(widget, data, result);" in drawn
    assert 'family === "small" ? null' not in drawn
    assert "isAccessory ? staleNote(result) : null" in drawn


def test_the_count_is_drawn_bigger_than_the_row_it_is_read_off() -> None:
    """It is the figure the widget is looked at for, and it is drawn at the size it is
    given: the box around it is measured wide enough that WidgetKit never shrinks the
    digits to fit, which is a count drawn smaller than the size it was asked for. The
    height is there on every size but a medium widget holding three flights, where it
    belongs to the rows."""
    source = script_source()
    sizes = {}
    for name, ends in (
        ("renderSmall(", "function renderList("),
        ("renderList(", "function titleRow("),
    ):
        drawn = source[source.index(f"function {name}") : source.index(ends)]
        found = re.search(r"countdown\([a-z]+, flight, (\w+)[,)]", drawn)
        assert found, name
        sizes[name] = found.group(1)
    # One size on every home screen size. Sized against the room each widget had going
    # spare, the figure came out smallest on the widget with the most room on it, and the
    # same number was drawn three different ways across one home screen.
    assert sizes["renderSmall("] == "COUNT_SIZE"
    assert sizes["renderList("] == "COUNT_SIZE"
    assert "const COUNT_SIZE = 18;" in source
    assert "flights.length < 3" not in source


def test_the_count_is_pulled_up_under_the_words_naming_it() -> None:
    """Only on the sizes that draw those words on the row above the figure.

    A line of type carries air over its glyphs, and the count is set half again the size
    of every other word on the widget, so it carries half again as much. Left where it
    falls, that air lands between "Departs in" and the figure it belongs to and reads as
    most of a blank line. WidgetKit gives a line of text no say in its own height, so the
    only way to take it back is to pull the box the count is drawn in up by it.

    The narrow sizes are not touched: there the words and the figure share a line, so
    there is no gap over the count to close.
    """
    source = script_source()
    assert "const COUNT_LIFT = 6;" in source
    wide = source[source.index("function renderList(") : source.index("function titleRow(")]
    assert "countdown(line, flight, COUNT_SIZE, 1, COUNT_LIFT)" in wide
    for name, ends in (
        ("renderAccessory(", "function renderSmall("),
        ("renderSmall(", "function renderList("),
    ):
        assert "COUNT_LIFT" not in source[source.index(f"function {name}") : source.index(ends)]
    # Applied to the box the count is measured into rather than to the figure itself: a
    # line of text has no say in its own height, and the box is the only handle there is.
    timer = source[source.index("function countdown(") : source.index("function pill(")]
    assert "box.setPadding(-lift, 0, 0, 0);" in timer


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


async def test_friend_flights_follow_the_widget_preference(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with database() as session:
        await _seed(
            session,
            booking(id=1, marketing_number="1", departure_local_date=DEPARTURE.date()),
            booking(
                id=2,
                marketing_number="2",
                departure_local_date=DEPARTURE.date(),
                friend_name="Sam",
            ),
        )
        mine_only = await widget.load_flight_rows(session, NOW)
        monkeypatch.setattr(
            prefs,
            "_current",
            prefs.current().model_copy(update={"show_friend_flights_in_widget": True}),
        )
        with_friends = await widget.load_flight_rows(session, NOW)

    assert [row.id for row, _ in mine_only] == [1]
    assert [row.id for row, _ in with_friends] == [1, 2]


def test_at_the_gate_the_status_says_arrived(settings: Settings) -> None:
    parked = snapshot(
        actual_off=DEPARTURE, actual_on=ARRIVAL - timedelta(minutes=10), actual_in=ARRIVAL
    )
    flight = payload([(booking(), parked)], settings)["flights"][0]
    assert flight["status_label"] == "Arrived"
    assert detail(flight) is None
