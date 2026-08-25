"""The widget contract. Anything asserted here is something the phone renders literally."""

from __future__ import annotations

import asyncio
import base64
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


def detail(flight: dict[str, Any]) -> list[tuple[Any, str]]:
    """The line under the heading, in the runs it is drawn in: a mark and what it heads.

    The phone draws these in order - the glyph, then the figures behind it - so a run is
    the pair, and a line with nothing on it is no runs at all.
    """
    return [(run["icon"], run["text"]) for run in flight["detail"]]


def target(flight: dict[str, Any]) -> tuple[Any, Any]:
    """The end of the row: what the flight is next due to do, and when it is due."""
    return flight["target_label"], flight["target_value"]


# --- payload shaping ------------------------------------------------------------------


def test_upcoming_flight(settings: Settings) -> None:
    far = booking(scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings)["flights"][0]
    # Days out the day and time it leaves is the whole story, and it is told once: there
    # is no gate to find yet, no rung anybody is waiting on, and the pill says only that
    # it is booked. The card carries no footer for one of these either.
    assert flight == {
        "detail_url": "https://flights.example.com/f/42",
        "phase": "upcoming",
        "friend_initial": None,
        "friend_hue": None,
        "logo_url": "https://www.gstatic.com/flights/airline_logos/70px/DL.png",
        "number": "DL1234",
        "route": "JFK → LAX",
        "status_label": "Scheduled",
        "status_tone": "quiet",
        "detail": [{"icon": None, "text": "Fri 18 Sep 18:00 UTC"}],
        "target_label": None,
        "target_value": None,
    }


def test_a_friends_flight_is_drawn_in_their_own_colour(settings: Settings) -> None:
    """The board gives a friend a disc with their initial in it, tinted by a hue taken
    from their name. The widget draws the same disc, so one person is one colour on the
    phone and on the page both - which means the hue is worked out in one place."""
    theirs = booking(friend_name="beatrice")
    flight = payload([(theirs, None)], settings)["flights"][0]
    assert flight["friend_initial"] == "B"
    assert flight["friend_hue"] == views.friend_hue("beatrice")


def test_the_script_draws_the_disc_the_way_the_page_mixes_it(settings: Settings) -> None:
    """A hue is not a colour until something reads it at a saturation and a lightness,
    and the page reads it at four of them. The script carries the same four."""
    source = script_source()
    assert "{ s: 55, l: 50, a: 0.12 }" in source
    assert "{ s: 35, l: 38, a: 1 }" in source


def test_the_day_it_leaves_is_read_at_the_origin(settings: Settings) -> None:
    far = booking(origin_iata="HND", scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings, airports=AIRPORTS)["flights"][0]
    assert detail(flight) == [(None, "Sat 19 Sep 03:00 JST")]


def test_a_departure_carries_its_date(settings: Settings) -> None:
    """NOW is 18:00 UTC on the 12th: 14:00 in New York, 03:00 the next day in Tokyo.

    A time on its own reads as today's, so a flight days out would look hours overdue
    every day until it went. The date is always in front of it, because a flight far
    enough out that nobody is watching it yet is far enough out that "Tomorrow" is not
    what anyone needs to be told.
    """
    late = booking(scheduled_departure_utc=NOW + timedelta(hours=30))
    flight = payload([(late, None)], settings, airports=AIRPORTS)["flights"][0]
    assert detail(flight) == [(None, "Sun 13 Sep 20:00 EDT")]
    assert flight["status_label"] == "Scheduled"
    assert flight["status_tone"] == "quiet"

    # With no airport on file the day is read off UTC rather than left blank.
    assert detail(payload([(late, None)], settings)["flights"][0]) == [
        (None, "Mon 14 Sep 00:00 UTC")
    ]


def test_a_flight_inside_its_day_turns_its_line_over_to_the_building(
    settings: Settings,
) -> None:
    """The day it leaves stops being the news the moment there is somewhere to walk to.

    The time does not go anywhere: it is at the end of the row, where it was already,
    and the line under the heading turns over to the terminal and the gate.
    """
    tomorrow = NOW + timedelta(hours=12)
    flight = payload([(booking(), snapshot(scheduled_out=tomorrow))], settings, airports=AIRPORTS)[
        "flights"
    ][0]
    assert flight["status_label"] == "On time"
    # And the rung appears with it: days out there was nothing to be waiting on, and now
    # there is, which is the same line the card's footer comes and goes on.
    assert target(flight) == ("Departs", "Sun 02:00")
    # Nothing has been named yet, so there is nothing to name: the mark is what a row
    # waiting on a gate used to draw a dash for, and it arrives with the gate.
    assert detail(flight) == []


def test_day_of_shows_the_terminal_the_gate_and_the_seat(settings: Settings) -> None:
    """Everything a person on their way to the airport is walking towards, in the boxes
    the card draws them in. When it leaves is the count on the other side of the row."""
    gated = snapshot(scheduled_out=DEPARTURE, gate_origin="B22", terminal_origin="4")
    flight = payload([(booking(seat="14A"), gated)], settings, airports=AIRPORTS)["flights"][0]
    assert flight["phase"] == "day_of"
    # The end being walked to behind a plane climbing, then the seat behind a seat. The
    # terminal keeps the T a boarding pass prints in front of it, because a bare 4 beside
    # a bare B22 is two figures with nothing to tell them apart, and the dot between them
    # says which of the gaps on the line is the one that divides.
    assert detail(flight) == [("takeoff", "T4 • B22"), ("seat", "14A")]
    assert target(flight) == ("Departs", "14:40")
    assert flight["status_label"] == "On time"
    assert flight["status_tone"] == "ok"


def test_day_of_with_nothing_assigned_yet_draws_no_line(settings: Settings) -> None:
    """A place the airport has not named is left out rather than dashed.

    The dashes were there to hold the line still while gates are published. But a dash is
    an empty box, and a row that has three boxes at most and two of them empty says
    nothing in the space where it says everything. What holds the line still now is the
    mark in front of it, which is drawn as soon as one figure lands behind it.
    """
    flight = payload([(booking(), snapshot())], settings)["flights"][0]
    assert detail(flight) == []


def test_a_delayed_departure_states_the_time_it_now_leaves(settings: Settings) -> None:
    held = snapshot(scheduled_out=DEPARTURE, estimated_out=DEPARTURE + timedelta(minutes=30))
    flight = payload([(booking(seat="14A"), held)], settings)["flights"][0]
    assert flight["status_label"] == "Departure delayed"
    assert flight["status_tone"] == "warn"
    # Delayed to when is the whole question the pill leaves open, and the time answers
    # it from the estimate rather than from the schedule.
    assert target(flight) == ("Departs", "19:10")
    # No terminal and no gate yet, so no plane and nothing behind it. The seat is the
    # one thing that was known when the flight was booked, and it stands on its own.
    assert detail(flight) == [("seat", "14A")]


def test_the_run_up_to_departure_keeps_the_gate(settings: Settings) -> None:
    """The half hour before departure is when the gate matters most, so it must not be
    traded for a word about boarding that no feed reports."""
    imminent = snapshot(scheduled_out=NOW + timedelta(minutes=20), gate_origin="B22")
    flight = payload([(booking(), imminent)], settings)["flights"][0]
    assert flight["phase"] == "day_of"
    assert detail(flight) == [("takeoff", "B22")]
    assert target(flight) == ("Departs", "18:20")


def test_pushback_turns_the_line_round_to_the_far_end(settings: Settings) -> None:
    """The gate it left is behind the person reading this, so the line stops naming it
    and names the one at the other end instead - terminal then gate, as on the way out;
    nothing upstream estimates wheels up, so the time at the end of the row is the
    landing."""
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
    assert detail(flight) == [("landing", "TB • 12")]
    assert target(flight) == ("Lands", "18:15")


def test_airborne_states_the_landing_and_reads_the_line_backwards(settings: Settings) -> None:
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
    # Where they are, then where they are going: the seat first because that is where
    # the line is being read from, and the terminal and the gate at the far end after it -
    # the same pair in the same order the line out drew them in.
    assert detail(flight) == [("seat", "32A"), ("landing", "TB • 12")]
    assert target(flight) == ("Lands", "22:40")
    assert flight["status_label"] == "Arriving late"
    assert flight["status_tone"] == "warn"


def test_landed_states_when_it_is_at_the_gate(settings: Settings) -> None:
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
    assert detail(flight) == [("landing", "TB • 12")]
    assert target(flight) == ("At the gate", "22:15")


def test_at_the_gate_the_belt_takes_the_end_of_the_row(settings: Settings) -> None:
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
    # Where the card puts it: the last thing the flight points at, at the end of the row
    # every other time is read off. Nothing is left ahead of it to be due.
    assert target(flight) == ("Baggage claim", "7")
    # The card goes on drawing where the flight came in while it draws the belt, and so
    # does this: the terminal on that line is the one the belt is in.
    assert detail(flight) == [("landing", "TB • 12")]


def test_wheels_down_takes_the_seat_off_the_line(settings: Settings) -> None:
    """A seat is where the reader is only for as long as they are in it.

    Down, the row has one thing left to say - the way out - so the seat gives up its half
    of the line to the terminal and the gate the aircraft came in at, and the belt takes
    the end of the row from there.
    """
    landed = snapshot(
        actual_off=DEPARTURE,
        actual_on=ARRIVAL - timedelta(minutes=10),
        estimated_in=ARRIVAL,
        gate_destination="12",
        terminal_destination="B",
    )
    flight = payload([(booking(seat="32A"), landed)], settings)["flights"][0]
    assert flight["phase"] == "landed"
    assert flight["status_label"] == "Landed"
    assert detail(flight) == [("landing", "TB • 12")]


def test_a_parked_flight_keeps_the_seat_off_the_line(settings: Settings) -> None:
    """Still true at the gate, where the terminal on the line is the belt's own."""
    done = snapshot(
        actual_off=DEPARTURE,
        actual_on=ARRIVAL,
        actual_in=ARRIVAL,
        baggage_claim="7",
        gate_destination="12",
        terminal_destination="B",
    )
    flight = payload([(booking(seat="32A"), done)], settings)["flights"][0]
    assert flight["status_label"] == "Arrived"
    assert detail(flight) == [("landing", "TB • 12")]
    assert target(flight) == ("Baggage claim", "7")


def test_a_landed_flight_with_nothing_at_the_far_end_draws_no_line(settings: Settings) -> None:
    """The seat was the only thing on it, and the seat is behind them.

    A row with nothing left to name draws nothing rather than the one figure it still
    holds: an empty line reads as an airport that has not said yet, which is the truth of
    every other figure on it.
    """
    landed = snapshot(actual_off=DEPARTURE, actual_on=ARRIVAL - timedelta(minutes=10))
    flight = payload([(booking(seat="32A"), landed)], settings)["flights"][0]
    assert flight["phase"] == "landed"
    assert detail(flight) == []


def test_a_diversion_on_the_ground_gives_up_the_seat_too(settings: Settings) -> None:
    """Filed under the diversion for as long as it exists, and as done with its seat as
    a flight that came down where it was booked to."""
    down = snapshot(
        diverted=True,
        destination_iata="YOW",
        actual_off=DEPARTURE,
        actual_on=NOW - timedelta(minutes=5),
        estimated_in=NOW + timedelta(minutes=5),
        gate_destination="12",
        terminal_destination="B",
    )
    flight = payload([(booking(seat="32A"), down)], settings, airports=AIRPORTS)["flights"][0]
    assert flight["phase"] == "diverted"
    assert detail(flight) == [("landing", "TB • 12")]


def test_a_flight_still_in_the_air_keeps_its_seat(settings: Settings) -> None:
    """The line only turns round at wheels down; on the way there the reader is in 32A
    and the row says so, ahead of where they are going."""
    flying = snapshot(
        actual_off=DEPARTURE - timedelta(hours=2),
        estimated_on=ARRIVAL - timedelta(minutes=10),
        estimated_in=ARRIVAL,
        gate_destination="12",
        terminal_destination="B",
    )
    flight = payload([(booking(seat="32A"), flying)], settings)["flights"][0]
    assert flight["phase"] == "airborne"
    assert detail(flight) == [("seat", "32A"), ("landing", "TB • 12")]


def test_a_booking_closed_in_the_air_gives_up_the_seat(settings: Settings) -> None:
    """The poller closed the book without ever seeing it come down. Whatever the feed
    last said, that flight is over and nobody is in that seat."""
    lost = snapshot(
        actual_off=NOW - timedelta(hours=9),
        estimated_in=NOW - timedelta(hours=3),
        gate_destination="12",
        terminal_destination="B",
    )
    closed = booking(status=BookingStatus.COMPLETED, seat="32A")
    flight = payload([(closed, lost)], settings)["flights"][0]
    assert flight["status_label"] == "Flown"
    assert detail(flight) == [("landing", "TB • 12")]


def test_a_belt_nobody_has_named_yet_leaves_the_row_empty(settings: Settings) -> None:
    """A row has one line for the figure, so words with nothing beside them are the news
    that there is no news. The line arrives with the belt; the card draws a dash there
    instead, having the width to hold both."""
    done = snapshot(actual_off=DEPARTURE, actual_on=ARRIVAL, actual_in=ARRIVAL)
    flight = payload([(booking(), done)], settings)["flights"][0]
    assert flight["status_label"] == "Arrived"
    assert target(flight) == (None, None)


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
    assert target(flight) == ("Baggage claim", "7")


def test_a_time_that_has_passed_says_it_is_due(settings: Settings) -> None:
    """Wheels down is published minutes after the fact, and the board says "due" until it
    is. The same word here, in front of the same time: a flight due to land at 22:05 was
    due to land at 22:05, and the words are what say it has not."""
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
    assert target(flight) == ("Due to land", "22:05")


def test_cancelled_has_no_time_to_give(settings: Settings) -> None:
    flight = payload([(booking(), snapshot(cancelled=True))], settings)["flights"][0]
    assert flight["phase"] == "cancelled"
    assert flight["status_label"] == "Cancelled"
    assert flight["status_tone"] == "stop"
    assert detail(flight) == []
    assert target(flight) == (None, None)


def test_a_booking_the_poller_closed_in_the_air_has_no_time_to_give(
    settings: Settings,
) -> None:
    """The feed lost it. A landing hours in the past is no time to name."""
    lost = snapshot(actual_off=NOW - timedelta(hours=9), estimated_in=NOW - timedelta(hours=3))
    closed = booking(status=BookingStatus.COMPLETED)
    flight = payload([(closed, lost)], settings)["flights"][0]
    assert flight["status_label"] == "Flown"
    assert target(flight) == (None, None)
    # Nothing was ever named at the far end, so there is nothing under the heading. The
    # pill says the feed lost it, which is the whole of what is known.
    assert detail(flight) == []


def test_a_diversion_renames_the_destination_and_reads_its_clock(settings: Settings) -> None:
    diverted = snapshot(
        diverted=True, destination_iata="YOW", actual_off=DEPARTURE, estimated_in=ARRIVAL
    )
    flight = payload([(booking(), diverted)], settings, airports=AIRPORTS)["flights"][0]
    assert flight["phase"] == "diverted"
    assert flight["route"] == "JFK → YOW"
    assert flight["status_label"] == "Diverted"
    # The route names where it is bound and the end of the row says when it is due there.
    assert target(flight) == ("Lands", "18:15")


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
    assert target(flight) == ("Lands", "22:04")


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


def test_the_row_is_the_cards_two_conditions_and_nothing_else(settings: Settings) -> None:
    """The card draws where to be for as long as it is watching the flight, and a footer
    for the belt or for a rung on a flight it is watching. Nothing else.

    A widget that names a rung the card leaves out is a second answer to a question the
    card has already answered - the same fault as a pill that rephrases the board's word
    - so both halves of the row are read off the card's own two conditions rather than
    off a rule of the widget's own.
    """
    rows: list[FlightRow] = [
        (booking(id=1, scheduled_departure_utc=NOW + timedelta(days=6)), None),
        (booking(id=2), snapshot(booking_id=2, scheduled_out=DEPARTURE, gate_origin="B22")),
        (
            booking(id=3),
            snapshot(
                booking_id=3,
                actual_off=DEPARTURE,
                estimated_in=ARRIVAL,
                gate_destination="12",
            ),
        ),
        (
            booking(id=4),
            snapshot(
                booking_id=4,
                actual_off=DEPARTURE,
                actual_on=ARRIVAL,
                actual_in=ARRIVAL,
                baggage_claim="7",
                gate_destination="12",
            ),
        ),
        (booking(id=5), snapshot(booking_id=5, cancelled=True)),
    ]
    for this, snap in rows:
        flight = payload([(this, snap)], settings, airports=AIRPORTS)["flights"][0]
        phase = compute_phase(this, snap, NOW)
        watched = views.watched(phase)
        footer = views.at_the_gate(phase, this, snap, NOW) or (
            watched and views.milestone(phase, this, snap, now=NOW) is not None
        )
        assert (flight["target_label"] is not None) is footer, phase
        # The card's places, compressed to the end being walked to, appear on exactly the
        # rows the card draws them on. Each of these has a gate at that end, so what is
        # being read is the condition rather than whether the airport has said yet.
        assert bool(flight["detail"] and flight["detail"][-1]["icon"]) is watched, phase


# --- the phone's own clock --------------------------------------------------------------


HOME = "America/Toronto"


# One flight, read from Ottawa: it leaves Tokyo at 03:40 on the 13th JST, which is 14:40
# on the 12th where it is being read, and it is inside its day either way.
def tokyo_row() -> FlightRow:
    return (
        booking(origin_iata="HND", scheduled_departure_utc=DEPARTURE),
        snapshot(scheduled_out=DEPARTURE),
    )


def test_the_time_a_flight_is_due_is_on_the_phones_clock(settings: Settings) -> None:
    """A time four zones away is arithmetic, not information.

    Watching from Ottawa, a flight leaving Tokyo at 03:40 is due at 14:40 on the watch of
    the person reading it, and that is the figure. The airport's own reading of the same
    instant is not set beside it - a line with two times on it is a line that has to be
    worked out rather than read - and the zone is not named either, because the widget's
    footer says once that every time on it is on the clock in the same hand.
    """
    flight = payload([tokyo_row()], settings, airports=AIRPORTS, viewer_tz=HOME)["flights"][0]
    assert target(flight) == ("Departs", "14:40")
    assert "JST" not in str(target(flight)) and "EDT" not in str(target(flight))


def test_the_day_it_leaves_is_the_airports_and_names_its_zone(settings: Settings) -> None:
    """The one time on the widget that is not the reader's own, and the one that says so.

    Days out there is no rung anybody is waiting on, so the row has one time on it rather
    than two, and it is the airport's: the day a flight leaves is the day where it leaves
    from. A bare reading would be taken for the reader's own, so this one keeps its zone.
    """
    far = booking(origin_iata="HND", scheduled_departure_utc=NOW + timedelta(days=6))
    flight = payload([(far, None)], settings, airports=AIRPORTS, viewer_tz=HOME)["flights"][0]
    assert detail(flight) == [(None, "Sat 19 Sep 03:00 JST")]
    assert target(flight) == (None, None)


def test_the_day_in_front_of_the_time_is_the_phones_day(settings: Settings) -> None:
    """The day belongs to the clock the figure is read on, or it contradicts it.

    NOW is the 12th in Toronto, and a flight leaving Tokyo at 15:00 on the 13th JST is
    due at 02:00 on the 13th where it is being read: not today to the person holding the
    phone, however plainly it is the afternoon at the airport. A day is named at all
    because a bare 02:00 read on a Saturday afternoon is a time that looks like it has
    gone rather than one fourteen hours out.
    """
    tokyo = booking(origin_iata="HND", scheduled_departure_utc=NOW + timedelta(hours=12))
    rows: list[FlightRow] = [(tokyo, snapshot(scheduled_out=NOW + timedelta(hours=12)))]
    flight = payload(rows, settings, airports=AIRPORTS, viewer_tz=HOME)["flights"][0]
    assert target(flight) == ("Departs", "Sun 02:00")


def test_a_time_today_is_the_time_and_nothing_else(settings: Settings) -> None:
    """Most flights on the widget go today, and a day in front of every one of them is a
    word the reader has to step over to reach the figure."""
    soon = payload([(booking(), snapshot(scheduled_out=DEPARTURE))], settings, viewer_tz=HOME)
    assert target(soon["flights"][0]) == ("Departs", "14:40")


def test_a_phone_that_says_nothing_gets_the_airports_clock(settings: Settings) -> None:
    """What every copy drew before the zone was sent, and still right, just harder work.

    It is the one reading on the widget that is not the reader's own and does not say so,
    and there is nothing better to draw: the alternative is a row with no time on it.
    """
    rows = [tokyo_row()]
    silent = payload(rows, settings, airports=AIRPORTS)["flights"][0]
    assert target(silent) == ("Departs", "03:40")
    blank = payload(rows, settings, airports=AIRPORTS, viewer_tz="")["flights"][0]
    assert target(blank) == ("Departs", "03:40")


def test_a_zone_the_phone_made_up_does_not_break_the_widget(settings: Settings) -> None:
    """`zone()` falls back to UTC on a name it does not know, and a widget that draws
    the wrong clock is still better than one that draws an error."""
    rows = [tokyo_row()]
    flight = payload(rows, settings, airports=AIRPORTS, viewer_tz="Mars/Olympus_Mons")["flights"][0]
    assert target(flight) == ("Departs", "18:40")


def test_the_phones_zone_reaches_the_payload_from_the_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one thing the server cannot work out for itself."""
    leaves = datetime.now(UTC) + timedelta(hours=6)

    async def close_in(_session: Any, _now: datetime) -> list[FlightRow]:
        return [(booking(scheduled_departure_utc=leaves), snapshot(scheduled_out=leaves))]

    monkeypatch.setattr(widget, "load_flight_rows", close_in)
    response = client.get(
        "/api/widget?tz=Asia/Tokyo", headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 200
    # The time it is due, read in Tokyo, because Tokyo is where the phone says it is.
    assert response.json()["flights"][0]["target_value"].endswith(views.clock(leaves, "Asia/Tokyo"))


def test_the_script_tells_the_server_where_the_phone_is() -> None:
    source = script_source()
    assert "tz=${encodeURIComponent(timeZone())}" in source
    assert "resolvedOptions().timeZone" in source


def test_the_script_tells_the_server_which_size_is_asking() -> None:
    """The other thing the server cannot work out for itself.

    How many rows fit is the widget's own business, and a list cut to the smallest size
    that might be asking is a large widget with its bottom half empty.
    """
    source = script_source()
    assert "family=${encodeURIComponent(family)}" in source
    assert 'const family = config.widgetFamily || "medium";' in source


def test_the_script_says_once_which_clock_the_times_are_on() -> None:
    """No time on the widget carries a zone, so the footer carries it for all of them.

    It cannot come from the payload: the server does not know whether the phone will be
    drawing a widget with a footer or a lock screen without one.
    """
    source = script_source()
    assert "Times on your phone's clock" in source


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
    """Every string the phone draws is a clock face, already read on the right clock.

    An instant would mean the phone had something to work out from it, and the only
    figure it could work out - how long is left - is the one figure that would be a
    quarter of an hour wrong by the time anybody read it.
    """
    rows: list[FlightRow] = [
        (booking(id=1), snapshot(scheduled_out=NOW + timedelta(minutes=20))),
        (booking(id=2), snapshot(actual_off=DEPARTURE, estimated_in=ARRIVAL)),
        (booking(id=3, scheduled_departure_utc=NOW + timedelta(days=4)), None),
    ]
    assert list(_instants(payload(rows, settings))) == []


def test_a_non_utc_input_is_read_at_the_origins_clock(settings: Settings) -> None:
    """AeroAPI states offsets; whatever arrives is a clock at the airport on the way out."""
    tokyo = datetime(2026, 9, 18, 18, 40, tzinfo=UTC).astimezone()
    flight = payload([(booking(scheduled_departure_utc=tokyo), None)], settings)["flights"][0]
    assert detail(flight) == [(None, "Fri 18 Sep 18:40 UTC")]


# --- ordering and cadence -------------------------------------------------------------


def test_in_the_order_they_now_leave_cut_to_what_the_size_holds(settings: Settings) -> None:
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
    # Three of them, because a request that did not say which widget it is for is a
    # script that has not replaced itself yet, and the medium widget is the middle size.
    assert [_id(flight) for flight in body["flights"]] == [4, 3, 1]

    # A large widget is twice the medium's height and holds twice its rows, so the two
    # that did not fit are on it - in the same order, cut later.
    large = payload(rows, settings, family="large")
    assert [_id(flight) for flight in large["flights"]] == [4, 3, 1, 5, 2]
    assert [_id(flight) for flight in payload(rows, settings, family="medium")["flights"]] == [
        4,
        3,
        1,
    ]
    assert [_id(flight) for flight in payload(rows, settings, family="small")["flights"]] == [4, 3]
    lock = payload(rows, settings, family="accessoryRectangular")
    assert [_id(flight) for flight in lock["flights"]] == [4]


def test_the_large_widget_holds_seven_and_cuts_the_eighth(settings: Settings) -> None:
    """Twice the medium's height, and one row more than twice its rows.

    What a size holds is what its height holds once the gap between two flights is the
    gap a break between two flights needs. The large widget's used to be half as wide
    again as the medium's, which was air the size happened to have rather than air the
    column asked for, and three points off each of six of them is a seventh flight. Past
    seven it is a trip itinerary, which is what the web UI is for.
    """
    rows: list[FlightRow] = [
        (booking(id=n, scheduled_departure_utc=NOW + timedelta(hours=n)), None) for n in range(1, 9)
    ]
    large = payload(rows, settings, family="large")
    assert [_id(flight) for flight in large["flights"]] == [1, 2, 3, 4, 5, 6, 7]


def test_the_payload_names_the_board(settings: Settings) -> None:
    """Where a tap goes when there is no row to give it to: the list both flights are on,
    on the address the widget links to everything else by."""
    body = payload([(booking(), None)], settings)
    assert body["board_url"] == "https://flights.example.com"
    assert body["flights"][0]["detail_url"].startswith(f"{body['board_url']}/f/")


def test_a_size_nobody_has_heard_of_gets_the_middle_one(settings: Settings) -> None:
    """A family the server does not know is a widget iOS grew after this was written.

    The medium widget's share is the answer to that and to a script old enough not to
    say: too few rows leaves a widget half empty, and too many are cut by the phone.
    """
    rows: list[FlightRow] = [
        (booking(id=n, scheduled_departure_utc=NOW + timedelta(hours=n)), None) for n in range(1, 6)
    ]
    for asked in ("systemExtraLarge", "", None):
        body = payload(rows, settings, family=asked)
        assert [_id(flight) for flight in body["flights"]] == [1, 2, 3], asked


def test_refresh_slows_down_when_nothing_is_close(settings: Settings) -> None:
    far = booking(scheduled_departure_utc=NOW + timedelta(days=4))
    assert payload([(far, None)], settings)["refresh_seconds"] == 900


def test_refresh_speeds_up_on_the_day(settings: Settings) -> None:
    assert payload([(booking(), None)], settings)["refresh_seconds"] == 600


def test_the_reload_is_asked_for_when_the_rung_falls_due(settings: Settings) -> None:
    """The time stays true on its own; the word in front of it does not.

    "Departs 18:40" is drawn once and stays drawn, so at 18:44 it is still saying the
    flight departs - when what the reader needs to see is that it is due and has not.
    Nothing on the phone can fix that between reloads, so the reload is asked for at the
    instant the wording changes rather than at the usual ten minutes.
    """
    close = snapshot(scheduled_out=NOW + timedelta(minutes=4))
    assert payload([(booking(), close)], settings)["refresh_seconds"] == 240


def test_a_rung_further_out_than_the_cadence_does_not_slow_it_down(
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


def test_a_rung_already_past_is_not_asked_about_again(settings: Settings) -> None:
    """It is drawn as "Due to depart" already; there is no later instant to wake for."""
    overdue = snapshot(scheduled_out=NOW - timedelta(minutes=3))
    flight = payload([(booking(), overdue)], settings)
    assert target(flight["flights"][0])[0] == "Due to depart"
    assert flight["refresh_seconds"] == 600


# --- a row that stands still ------------------------------------------------------------


def test_the_row_is_words_and_clock_faces_and_nothing_else(settings: Settings) -> None:
    """The phone is handed strings and told to draw them, and that is the whole contract.

    Nothing in it is worked out on the phone, so nothing in it can drift out of step with
    anything else in it: the word in front of a time and the time itself were decided in
    the same breath, on the same clock, by the same function.
    """
    ahead = snapshot(scheduled_out=NOW + timedelta(minutes=4))
    flight = payload([(booking(), ahead)], settings)["flights"][0]
    assert target(flight) == ("Departs", "18:04")
    assert set(flight) == {
        "detail_url",
        "phase",
        "friend_initial",
        "friend_hue",
        "logo_url",
        "number",
        "route",
        "status_label",
        "status_tone",
        "detail",
        "target_label",
        "target_value",
    }


def test_a_time_that_has_gone_by_is_still_the_time(settings: Settings) -> None:
    """A flight due to leave at 17:57 was due to leave at 17:57 whatever happened next.

    The figure never has to be taken back, which is the point of stating it: only the
    words in front of it change, and they change here rather than on the phone.
    """
    overdue = snapshot(scheduled_out=NOW - timedelta(minutes=3))
    flight = payload([(booking(), overdue)], settings)["flights"][0]
    assert target(flight) == ("Due to depart", "17:57")


def test_the_script_draws_nothing_that_moves() -> None:
    """A date drawn in the timer style is the one thing WidgetKit keeps ticking between
    reloads, and the one thing Scriptable gives no way to stand down: `applyTimerStyle`
    takes no end date, no pause and no direction. Every figure on the widget is a string
    the server chose, so there is no call to it anywhere."""
    source = script_source()
    assert "applyTimerStyle" not in source
    assert "addDate" not in source


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
    """Every word, every figure and every colour is the server's, and the phase is for
    the server's own cadence rather than for anything drawn.

    Nothing in the payload is a date, so there is nothing for the script to measure or
    to count: the times were read on the right clock before they were sent, and what
    reaches the phone are the glyphs.
    """
    source = script_source()
    assert ".phase" not in source
    assert "new Date(flight" not in source
    assert "applyTimerStyle" not in source


def test_the_row_holds_the_pill_and_the_time_against_the_same_edge() -> None:
    """The wide sizes draw a row as two full-width lines rather than as two columns.

    The heading leads the first and the pill ends it; where to be leads the second and
    the time the flight is due ends that. What holds the two right-hand halves against
    the same edge is the spacer in the middle of each line - which is the one thing on a
    widget that costs nothing to be exactly as wide as it has to be. A column would have
    to be measured, and the two things standing in it, a pill and a run of words, are the
    two things a script cannot measure before WidgetKit draws them.
    """
    source = script_source()
    wide = source[source.index("function renderList(") : source.index("function titleRow(")]
    assert "row.layoutVertically();" in wide
    assert "titleRow(head, flight, logos);" in wide
    assert "pill(head, flight);" in wide
    assert wide.count("line.addSpacer();") == 1
    assert "detailText(line, flight);" in wide
    assert "targetLabel(line, flight);" in wide
    assert "targetValue(line, flight);" in wide


def test_every_row_keeps_the_same_line_under_its_heading() -> None:
    """A line is as tall as whatever happens to land on it, unless it is told otherwise.

    The time is the largest type on a row and where to be is among the smallest, so a
    flight with a time to show stood taller than a flight with only a date under its
    heading - and a flight with neither, days out or called off, drew no second line at
    all. The gap between two rows is a fixed one, so what moved was the rows themselves:
    the column came out unevenly spaced, and the spacing said nothing about the flights
    beyond which of them happened to have a time.

    So the line is drawn on every row and given the height of the tallest thing it can
    ever carry, whether or not this flight has anything that tall to put on it.
    """
    source = script_source()
    assert "const UNDER_HEADING = Math.ceil(TYPE.time * LINE_HEIGHT);" in source
    wide = source[source.index("function renderList(") : source.index("function titleRow(")]
    assert "line.size = new Size(0, UNDER_HEADING);" in wide
    # Nothing decides whether the line is there, only what goes on it.
    assert "hasDetail" not in wide
    small = source[source.index("function renderSmall(") : source.index("function renderList(")]
    assert "line.size = new Size(0, UNDER_HEADING);" in small
    assert "hasDetail" not in small


def test_the_large_widget_spends_its_air_on_a_seventh_row() -> None:
    """The one distance the two wide sizes do not share, and the tightest figure here.

    A row is a shade under 36 points - the heading, the line under it, and the three
    between them - so seven of them, the gap between each pair, the footer and the
    widget's own inset come to within a point or two of what a 6.1in phone's large
    widget holds. Which is why the gap is a name rather than a bare number in the line
    that draws it: it is the first figure to put back if a row is ever drawn clipped.
    """
    source = script_source()
    assert "const LARGE_GAP = 9;" in source
    wide = source[source.index("function renderList(") : source.index("function titleRow(")]
    assert 'widget.addSpacer(family === "large" ? LARGE_GAP : 8);' in wide


def test_the_time_is_the_weight_the_row_is_read_for() -> None:
    """Semibold and bold are the same weight to look at when a Mac draws an iPhone's
    widget, so a figure set in semibold reads as heavier than its row on a mirrored
    screen and no different from it on the phone. The phone is the screen this is for.

    Monospaced with it, so a column of times down the widget lines up digit under digit.
    """
    source = script_source()
    drawn = source[source.index("function targetValue(") : source.index("function pill(")]
    assert "Font.boldMonospacedSystemFont(TYPE.time)" in drawn


def test_a_small_widget_holds_two_flights_of_three_lines() -> None:
    """The number and the route, the pill with the rung beside it, and where to be with
    the time beside that.

    There is no width on a 155pt square for a heading and a pill on one line, so the two
    halves of a wide row take a line each - and the words and the time they name still
    end up one above the other against the right-hand edge, which is the whole of what
    the wide row's own layout is for.
    """
    source = script_source()
    assert "const SMALL_FLIGHTS = 2;" in source
    assert "flights.slice(0, SMALL_FLIGHTS)" in source
    drawn = source[source.index("function renderSmall(") : source.index("function renderList(")]
    assert "titleRow(widget, flight, logos);" in drawn
    assert "pill(state, flight);" in drawn
    assert "targetLabel(state, flight);" in drawn
    assert "detailText(line, flight);" in drawn
    assert "targetValue(line, flight);" in drawn
    # Every line of a flight is the same distance under the one above it, and the only
    # wider gap is the one that separates two flights - which is no longer a distance at
    # all, but whatever the square has left after the six lines and the footer.
    assert drawn.count("widget.addSpacer(SMALL_GAP)") == 2
    assert "widget.addSpacer();" in drawn
    # And nothing on those lines carries air of its own inside that distance: a pill with
    # its own padding is a line held further from its neighbours than any other.
    assert 'const pad = family === "small" ? 0 : 2;' in source


def test_the_small_widget_fills_its_square() -> None:
    """Two blocks of three lines, and the room left over handed to the gap between them.

    The room used to be left where it fell, in a heap between the second flight and the
    footer, and the inset was cut to eleven points on the reading that a 155pt square
    holding six lines has nothing to spare. It has: the lines are the size they are, and
    what the square has left over is enough to stand the two blocks apart and still keep
    the words the distance from the rounded corner that every other size gives them.
    """
    source = script_source()
    assert "const INSET = 14;" in source
    assert "widget.setPadding(INSET, INSET, INSET, INSET);" in source
    # No size drawn tighter than the rest.
    assert 'family === "small" ? 11' not in source
    drawn = source[source.index("function renderSmall(") : source.index("function renderList(")]
    assert "widget.addSpacer();" in drawn
    assert "SMALL_GAP * 2" not in drawn


def test_the_small_widget_draws_every_route_at_one_size() -> None:
    """The airports under one flight, set the size of the airports under the other.

    The route was the half of the heading that gave way, and what it gave way to was
    whatever else landed on that row: a longer number or a friend's disc left it less to
    fit into, so the same seven characters came out smaller on the second flight than on
    the first. Nothing about the flight was different, only the row.

    It is set now: two points under the number, with an arrow the server sends without
    the spaces around it and the air between the two runs handed back to the figures.
    """
    source = script_source()
    drawn = source[source.index("function titleRow(") : source.index("// Whose flight it is")]
    assert "minimumScaleFactor" not in drawn
    # The air between the number and the route is the wide sizes' alone.
    assert 'if (family !== "small") {' in drawn
    assert "row.addSpacer(3);" in drawn


def test_the_square_gets_the_arrow_without_the_spaces_around_it(settings: Settings) -> None:
    """Seven characters rather than nine, on the one size where the heading is holding
    the number as well and those two spaces are most of an airport code's worth of it."""
    small = payload([(booking(), None)], settings, family="small")
    assert small["flights"][0]["route"] == "JFK→LAX"
    # Every other size sets the route the way a board sets it.
    for asked in ("medium", "large", "accessoryRectangular"):
        wide = payload([(booking(), None)], settings, family=asked)
        assert wide["flights"][0]["route"] == "JFK → LAX", asked


def test_the_small_widget_sends_its_one_tap_to_the_board() -> None:
    """iOS gives a small widget a single tap target for the whole square - Link is a
    medium and large size thing - so whichever of the two flights the thumb lands on, the
    square opens what it was pointed at. It was pointed at the top flight, which made a
    tap on the second one open the first: a row that is a thing to press on every other
    size, answering with the wrong flight here.

    The board is the one page that is not the wrong answer to either tap. The Lock Screen
    keeps its flight, because the one it draws and the one it opens are the same flight.
    """
    source = script_source()
    drawn = source[source.index("function renderSmall(") : source.index("function renderList(")]
    assert "widget.url = board;" in drawn
    assert "flights[0].detail_url" not in drawn
    assert "renderSmall(widget, flights.slice(0, SMALL_FLIGHTS), logos, data.board_url);" in source
    lock = source[source.index("function renderAccessory(") : source.index("function renderSmall(")]
    assert "widget.url = flight.detail_url;" in lock


def test_a_friends_disc_has_the_letter_drawn_on_it_rather_than_set() -> None:
    """The disc is squared to the number beside it, and the letter is drawn into it.

    Centring a line of type in a stack takes a spacer either side of it, and a spacer
    holds a length of its own - the stack's default spacing - before it gives any room
    away, whatever the stack's own `spacing` is set to afterwards. Two of them ask for
    more than a disc the height of a flight number has, so what was left for the letter
    was nothing, and a letter with nowhere to be drawn is dropped rather than drawn
    small. Which is why zeroing the padding did not put it back.

    Drawn into an image the size of the disc, the letter is centred by the context, and
    the two spacers that could not fit are not needed at all.
    """
    source = script_source()
    drawn = source[source.index("function friendMark(") : source.index("function hasDetail(")]
    assert "const side = TYPE.heading;" in drawn
    assert "disc.size = new Size(side, side);" in drawn
    assert "disc.cornerRadius = side / 2;" in drawn
    assert "disc.setPadding(0, 0, 0, 0);" in drawn
    # Nothing on the disc is laid out, so nothing on it can be squeezed out.
    assert "disc.spacing" not in drawn
    assert "addSpacer" not in drawn
    assert "disc.addText" not in drawn
    assert "disc.addImage(initialImage(flight.friend_initial, side))" in drawn
    assert "letter.imageSize = new Size(side, side);" in drawn
    # White into the context and tinted on the way onto the row, which is the deal the
    # marks below make: one image is the light scheme's colour and the dark one's both.
    assert "context.setTextColor(Color.white());" in drawn
    assert "letter.tintColor = friendColor(flight.friend_hue, FRIEND_INITIAL);" in drawn
    # And centred: across by the context's own alignment, down by a rect one line tall
    # set half of what is left over down the square, because the text hangs from the top
    # of the rect it is given.
    assert "context.setTextAlignedCenter();" in drawn
    assert "new Rect(0, (side - line) / 2, side, line)" in drawn
    assert "const line = size * LINE_HEIGHT;" in drawn


def test_the_wide_sizes_draw_every_row_at_one_size() -> None:
    """A type size on a widget is chosen once, and a shrink factor un-chooses it.

    The sizes above are picked so that a flight is drawn the same on a medium widget as
    on a large one. A shrink factor on the pill or the rung does the same thing one row
    further down: it hands the size to whatever else landed on that particular line, so
    the flight at the foot of a large widget comes out larger than the flight above it
    because its status was a shorter word.

    The narrow size keeps them on the two that share a line with the pill, where the
    longest status and the longest rung do not fit beside each other on a 155pt square
    and a word cut in half is worse than a word read small. The route no longer gives:
    it is set two points under the number and its arrow comes without spaces, so one
    size draws it on every row.
    """
    source = script_source()
    for drawn in (
        source[source.index("function targetLabel(") : source.index("// The other half")],
        source[source.index("function pill(") : source.index("// The bottom of the widget")],
    ):
        assert "minimumScaleFactor" in drawn
        assert 'if (family === "small") {' in drawn
        assert drawn.index('if (family === "small") {') < drawn.index("minimumScaleFactor")


def test_the_line_under_the_heading_is_marks_in_front_of_figures() -> None:
    """The glyph, then what it is the terminal or the gate or the seat of.

    The words TERM, GATE and SEAT were most of a line with room for figures or for
    labels and not for both, and they spelled out the one thing on the widget nobody has
    to be told: which of three figures is the gate. The mark says the thing a reader can
    get wrong instead, which is which end of the flight the row is naming.
    """
    source = script_source()
    drawn = source[source.index("function detailText(") : source.index("// The board's own")]
    assert "const glyph = mark(run.icon);" in drawn
    assert "line.addImage(glyph)" in drawn
    assert "line.addText(run.text)" in drawn
    # The mark is drawn before the figures it heads, and holds its own air from them.
    assert drawn.index("addImage") < drawn.index("addText")
    assert "line.addSpacer(BESIDE_MARK)" in drawn
    assert "line.addSpacer(BETWEEN_RUNS)" in drawn
    # In the row's quieter colour: the mark says which figures these are, and the
    # figures are what the line is read for.
    assert "drawn.tintColor = MUTED;" in drawn
    assert "text.textColor = TEXT;" in drawn


def test_the_script_carries_every_mark_the_payload_can_name() -> None:
    """The glyphs are in the script rather than fetched, and they are real images.

    An airline's mark is decoration: the number beside it already names the carrier, so
    one that never arrives costs nothing. These are not. "T4 • B22" with no plane in front
    of it is a line that is read wrong rather than read short, so the script carries them
    and draws them on the first reload, network or no network.
    """
    source = script_source()
    marks = source[source.index("  const MARKS = {") : source.index("  const data = name ?")]
    for name in (widget.ICON_TAKEOFF, widget.ICON_LANDING, widget.ICON_SEAT):
        assert f"    {name}:" in marks
    blobs = re.findall(r'"([A-Za-z0-9+/=]{40,})"', marks)
    assert len(blobs) >= 3
    # Every run of them, concatenated the way the script concatenates them, is a PNG.
    for chunk in re.split(r"    \w+:", marks)[1:]:
        data = base64.b64decode("".join(re.findall(r'"([A-Za-z0-9+/=]+)"', chunk)))
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert "Image.fromData(Data.fromBase64String(data))" in source


def test_the_route_is_set_rather_than_left_to_fit() -> None:
    """One size for the route on both wide widgets, chosen rather than left over.

    Given the number's own size the route was the longest thing on the heading, so it
    was the half that shrank to fit - by however much that line had going spare, which
    is not the same on a medium widget as on a large one. The same flight came out
    larger on the bigger widget, for no reason a reader could see.
    """
    source = script_source()
    scale = source[source.index("const TYPE =") : source.index("const server = connect();")]
    assert "{ heading: 12, route: 10, detail: 10, pill: 10, label: 10, time: 13 }" in scale
    assert "{ heading: 14, route: 12, detail: 11, pill: 10, label: 11, time: 13 }" in scale
    # And the one branch in it is the small size's: medium and large are one scale, so
    # neither can drift from the other.
    assert scale.count("family ===") == 1
    assert "Font.regularMonospacedSystemFont(TYPE.route)" in source


def test_every_home_screen_size_says_when_it_was_last_updated() -> None:
    """Including the small one. Only the lock screen goes without, where three lines is
    the whole widget and the age is said in the message instead."""
    source = script_source()
    drawn = source[
        source.index("async function buildWidget(") : source.index("function newWidget(")
    ]
    assert "footer(widget, data, result, true);" in drawn
    assert 'family === "small" ? null' not in drawn
    assert "isAccessory ? staleNote(result) : null" in drawn


def test_the_footer_states_the_time_it_was_fetched_at() -> None:
    """A widget is redrawn a few times an hour, so "4 min ago" written at draw time is
    the one figure on screen guaranteed to be wrong by the time anybody reads it, and
    wrong in the flattering direction. The time it was fetched at is simply a fact, and
    stays one for as long as iOS leaves the widget alone.

    "Cached" rather than "Last updated" when the server could not be reached, because
    then the time is when a file on the phone was written rather than when a server
    last spoke.
    """
    source = script_source()
    line = source[source.index("function updatedLine(") : source.index("function footerSize(")]
    assert '"Last updated"' in line and '"Cached"' in line
    assert "timeOfDay(result.fetchedAt)" in line
    assert "applyTimerStyle" not in line
    # Drawn from when the data landed, which is the fetch when there was one and the
    # cache file's own date when the server could not be reached.
    assert "result.fetchedAt" in line
    assert (
        "new Date()"
        in source[source.index("async function load(") : source.index("async function request(")]
    )


def test_the_footer_holds_the_stamp_and_the_note_apart() -> None:
    """One phrase against each edge, the way the rows above it are held apart: when the
    data landed at the near end, and at the far one the thing every time above it has in
    common. On a 155pt square there is no width for two phrases held apart, so there they
    are one phrase with both halves said as shortly as they can be."""
    source = script_source()
    line = source[source.index("function updatedLine(") : source.index("function footerSize(")]
    assert "line.addSpacer();" in line
    assert "line.addText(CLOCK_NOTE)" in line
    assert "CLOCK_NOTE_SHORT" in line
    assert 'const CLOCK_NOTE_SHORT = "your clock";' in source
    # The reason above it is part of the same block and centred with it: a sentence
    # starting where the flights start reads as one more row of the list.
    footer = source[source.index("function footer(") : source.index("function updatedLine(")]
    assert "text.centerAlignText();" in footer


def test_a_widget_with_no_flights_on_it_claims_no_clock() -> None:
    """The note is about the times on the widget. With no flights there are none of them,
    and a line explaining which clock nothing is on is a line that has to be read."""
    source = script_source()
    drawn = source[
        source.index("async function buildWidget(") : source.index("function newWidget(")
    ]
    assert "footer(widget, data, result, false);" in drawn


def test_the_lock_screen_gives_the_rung_its_own_line() -> None:
    """Three lines is the whole widget: the flight, then the word for it with where to be
    across the row, then what it is next due to do and when. The rung keeps its own line
    because "Lands" and a bare 22:15 across a row from it is a line to be guessed at."""
    source = script_source()
    drawn = source[
        source.index("function renderAccessory(") : source.index("function renderSmall(")
    ]
    assert "if (flight.target_label) {" in drawn
    assert "line.addText(flight.target_label)" in drawn
    assert "line.addText(flight.target_value)" in drawn
    assert "detailText(state, flight" in drawn


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
