"""A flight number and a date, turned into the flight the airline publishes.

The interesting risk here is a date: the day a person means is the day at the airport
they are standing in, and a schedule row states its departure in UTC. Everything else is
a row we cannot use, which must never take the rest of the answer down with it.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from flighter import lookup
from flighter.airports import UnknownAirport
from flighter.lookup import Candidate, OutOfRange, find_flights, parse_flight_number

DAY = date(2026, 9, 12)

TIMEZONES = {
    "YUL": "America/Toronto",
    "LHR": "Europe/London",
    "HNL": "Pacific/Honolulu",
    "NRT": "Asia/Tokyo",
}


def row(**overrides: Any) -> dict[str, Any]:
    """One row of the `scheduled` array, shaped after the documented schema."""
    published: dict[str, Any] = {
        "ident": "ACA871",
        "ident_icao": "ACA871",
        "ident_iata": "AC871",
        "actual_ident": None,
        "actual_ident_icao": None,
        "actual_ident_iata": None,
        "aircraft_type": "B789",
        "scheduled_out": "2026-09-12T22:40:00Z",
        "scheduled_in": "2026-09-13T09:25:00Z",
        "origin": "CYUL",
        "origin_icao": "CYUL",
        "origin_iata": "YUL",
        "origin_lid": None,
        "destination": "EGLL",
        "destination_icao": "EGLL",
        "destination_iata": "LHR",
        "destination_lid": None,
        "fa_flight_id": None,
        "meal_service": "Economy: Meal",
        "seats_cabin_business": 30,
        "seats_cabin_coach": 247,
        "seats_cabin_first": 0,
    }
    return published | overrides


class FakeClient:
    """Answers with the rows it was given, and records what it was asked."""

    def __init__(self, *rows: dict[str, Any]) -> None:
        self.rows = list(rows)
        self.asked: list[dict[str, Any]] = []

    async def schedules(
        self, start: date, end: date, *, airline: str, flight_number: str
    ) -> dict[str, Any]:
        self.asked.append(
            {"start": start, "end": end, "airline": airline, "flight_number": flight_number}
        )
        return {"links": None, "num_pages": 1, "scheduled": self.rows}


@pytest.fixture(autouse=True)
def airports(monkeypatch: pytest.MonkeyPatch) -> None:
    """The airports table, without a database. An unknown code raises, as it does live."""

    async def airport_tz(_session: Any, iata: str) -> str:
        tz = TIMEZONES.get(iata)
        if tz is None:
            raise UnknownAirport(iata)
        return tz

    monkeypatch.setattr(lookup, "airport_tz", airport_tz)


@contextlib.asynccontextmanager
async def no_session() -> AsyncIterator[Any]:
    """The airports are faked above, so the session they would be read on is nothing."""
    yield None


async def find(
    client: FakeClient, day: date = DAY, number: str = "871", carrier: str = "AC"
) -> list[Candidate]:
    """Look up a flight as though the day being asked about were today."""
    lookup_client: Any = client
    return await find_flights(carrier, number, day, lookup_client, sessions=no_session, today=DAY)


# --- What somebody types -------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("AC871", ("AC", "871")),
        ("ac 871", ("AC", "871")),
        ("AC-0871", ("AC", "871")),
        ("  ba112  ", ("BA", "112")),
        # Airline codes with a digit in them are ordinary, and so is the ICAO spelling.
        ("6E123", ("6E", "123")),
        ("U21234", ("U2", "1234")),
        ("ACA871", ("ACA", "871")),
        ("AA1", ("AA", "1")),
    ],
)
def test_a_flight_number_is_read_the_way_it_is_written(
    typed: str, expected: tuple[str, str]
) -> None:
    assert parse_flight_number(typed) == expected


@pytest.mark.parametrize("typed", ["", "871", "AC", "YUL to LHR", "AC871A", "12345"])
def test_what_is_not_a_flight_number_is_refused(typed: str) -> None:
    assert parse_flight_number(typed) is None


# --- What the schedule says ----------------------------------------------------------


async def test_a_published_leg_comes_back_as_a_filled_in_form() -> None:
    client = FakeClient(row())

    (found,) = await find(client)

    assert found.marketing_carrier == "AC"
    assert found.marketing_number == "871"
    assert found.origin_iata == "YUL" and found.dest_iata == "LHR"
    # 22:40Z is 18:40 in Montreal, and the arrival is read at Heathrow, not at Montreal.
    assert found.departure_local == datetime(2026, 9, 12, 18, 40)
    assert found.arrival_local == datetime(2026, 9, 13, 10, 25)
    assert found.as_form() == {
        "marketing_carrier": "AC",
        "marketing_number": "871",
        "origin_iata": "YUL",
        "dest_iata": "LHR",
        "departure_local": "2026-09-12T18:40",
        "arrival_local": "2026-09-13T10:25",
    }


async def test_the_window_asked_about_is_the_day_either_side() -> None:
    """A departure's UTC date is not its local date, so the query cannot be one day."""
    client = FakeClient(row())

    await find(client)

    assert client.asked == [
        {
            "start": date(2026, 9, 11),
            "end": date(2026, 9, 14),
            "airline": "AC",
            "flight_number": "871",
        }
    ]


async def test_the_day_is_the_day_at_the_airport_it_leaves_from() -> None:
    """23:00 in Honolulu is already tomorrow in UTC, and it is still tonight's flight."""
    tonight = row(origin_iata="HNL", destination_iata="NRT", scheduled_out="2026-09-13T09:00:00Z")
    tomorrow = row(origin_iata="HNL", destination_iata="NRT", scheduled_out="2026-09-14T09:00:00Z")

    (found,) = await find(FakeClient(tonight, tomorrow))

    assert found.departure_local == datetime(2026, 9, 12, 23, 0)


async def test_a_number_that_flies_twice_that_day_offers_both_in_order() -> None:
    afternoon = row(scheduled_out="2026-09-12T18:00:00Z", scheduled_in="2026-09-13T05:00:00Z")

    found = await find(FakeClient(row(), afternoon))

    assert [candidate.departure_local.hour for candidate in found] == [14, 18]


async def test_one_leg_published_twice_is_offered_once() -> None:
    """The operator's row and the codeshare sold on it are the same aeroplane."""
    codeshare = row(ident_iata="LH8811", actual_ident_iata="AC871")

    found = await find(FakeClient(row(), codeshare))

    assert len(found) == 1


async def test_the_airline_that_actually_flies_it_is_kept() -> None:
    """A codeshare is polled under the operator's number, so the lookup has to carry it."""
    (found,) = await find(
        FakeClient(row(ident_iata="AC871", actual_ident_iata="LH479")), number="871"
    )

    assert found.marketing_carrier == "AC" and found.marketing_number == "871"
    assert found.operating_carrier == "LH" and found.operating_number == "479"
    assert found.as_form()["operating_carrier"] == "LH"


async def test_the_number_typed_is_the_one_kept_when_the_operator_answers() -> None:
    """LH8811 is AC871 with a Lufthansa number on it; the board should say LH8811."""
    (found,) = await find(FakeClient(row()), carrier="LH", number="8811")

    assert found.marketing_carrier == "LH" and found.marketing_number == "8811"
    assert found.operating_carrier == "AC" and found.operating_number == "871"
    assert found.operated == "Operated as AC871"


async def test_the_answer_is_the_same_whichever_row_comes_back_first() -> None:
    codeshare = row(
        ident="DLH8811", ident_icao="DLH8811", ident_iata="LH8811", actual_ident_iata="AC871"
    )

    first = await find(FakeClient(row(), codeshare), carrier="LH", number="8811")
    second = await find(FakeClient(codeshare, row()), carrier="LH", number="8811")

    assert first == second
    assert first[0].flight_number == "LH8811" and first[0].operating_number == "871"


async def test_an_icao_spelling_is_offered_the_way_the_ticket_prints_it() -> None:
    (found,) = await find(FakeClient(row()), carrier="ACA")

    assert found.flight_number == "AC871"
    assert found.operating_carrier is None


async def test_a_row_we_cannot_place_is_dropped_rather_than_raised_on() -> None:
    """An airport with no IATA code, or none we know the zone of, is not an error."""
    usable = row(scheduled_out="2026-09-12T20:00:00Z")
    found = await find(
        FakeClient(
            row(origin_iata=None, origin="ZZZZ"),
            row(destination_iata="XYZ"),
            row(scheduled_out=None),
            usable,
        )
    )

    assert [candidate.departure_local.hour for candidate in found] == [16]


async def test_a_flight_on_another_day_is_not_an_answer() -> None:
    assert await find(FakeClient(row(scheduled_out="2026-09-14T22:40:00Z"))) == []


async def test_a_date_no_schedule_reaches_is_refused_without_spending_anything() -> None:
    client = FakeClient(row())

    for day in (DAY + timedelta(days=400), DAY - timedelta(days=100)):
        with pytest.raises(OutOfRange):
            await find(client, day)

    assert client.asked == []


def test_the_window_is_the_one_the_vendor_answers_about() -> None:
    """Three months back and a year on, less the day the query reaches for either side."""
    assert lookup.in_range(DAY, DAY)
    assert lookup.in_range(DAY + timedelta(days=363), DAY)
    assert not lookup.in_range(DAY + timedelta(days=365), DAY)
    assert lookup.in_range(DAY - timedelta(days=88), DAY)
    assert not lookup.in_range(DAY - timedelta(days=90), DAY)
