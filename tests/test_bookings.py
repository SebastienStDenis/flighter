"""The booking repository: the one conversion every booking passes through, the dedupe
rule, and the newest-snapshot query the rest of the app reads through."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flighter.airports import UnknownAirport
from flighter.bookings import (
    create_booking,
    delete_booking,
    find_duplicate,
    flight_label,
    latest_snapshot,
    latest_snapshots,
    operated_note,
    to_booking_times,
    update_ticket,
)
from flighter.cadence import FEED_HORIZON
from flighter.db import session_scope
from flighter.models import Airport, Booking, EventKind, FlightEvent, FlightSnapshot

JFK = "America/New_York"
LHR = "Europe/London"
LAX = "America/Los_Angeles"
NRT = "Asia/Tokyo"


def test_each_end_is_read_in_its_own_zone() -> None:
    departure, arrival = to_booking_times(
        datetime(2026, 6, 10, 8, 0), LAX, datetime(2026, 6, 10, 16, 30), JFK
    )
    assert departure == datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    assert arrival == datetime(2026, 6, 10, 20, 30, tzinfo=UTC)


def test_overnight_is_stored_as_stated() -> None:
    departure, arrival = to_booking_times(
        datetime(2026, 9, 12, 23, 30), JFK, datetime(2026, 9, 13, 11, 45), LHR
    )
    assert departure == datetime(2026, 9, 13, 3, 30, tzinfo=UTC)
    assert arrival == datetime(2026, 9, 13, 10, 45, tzinfo=UTC)


def test_date_line_arrival_is_not_pushed_to_the_next_day() -> None:
    """NRT 17:00 -> LAX 10:30 the same local date is correct as given."""
    departure, arrival = to_booking_times(
        datetime(2026, 3, 15, 17, 0), NRT, datetime(2026, 3, 15, 10, 30), LAX
    )
    assert departure == datetime(2026, 3, 15, 8, 0, tzinfo=UTC)
    assert arrival == datetime(2026, 3, 15, 17, 30, tzinfo=UTC)
    assert arrival > departure


def test_missing_arrival_stays_missing() -> None:
    departure, arrival = to_booking_times(datetime(2026, 6, 10, 8, 0), LAX, None, JFK)
    assert departure == datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    assert arrival is None


def test_backwards_pair_is_never_corrected() -> None:
    """A mistyped arrival is stored verbatim; guessing at a fix would hide the error."""
    departure, arrival = to_booking_times(
        datetime(2026, 6, 10, 16, 0), JFK, datetime(2026, 6, 10, 8, 0), JFK
    )
    assert departure == datetime(2026, 6, 10, 20, 0, tzinfo=UTC)
    assert arrival == datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    assert arrival < departure


def test_dst_boundary_departure() -> None:
    departure, _ = to_booking_times(datetime(2026, 3, 8, 9, 0), "America/Chicago", None, JFK)
    assert departure == datetime(2026, 3, 8, 14, 0, tzinfo=UTC)


def test_the_label_is_the_ticket_not_the_operator() -> None:
    booking = Booking(
        marketing_carrier="AA",
        marketing_number="6141",
        operating_carrier="BA",
        operating_number="112",
        origin_iata="LHR",
        dest_iata="JFK",
    )
    assert flight_label(booking) == "AA6141 LHR -> JFK"


def test_the_note_names_the_operator_only_when_there_is_one() -> None:
    assert operated_note("BA", "112") == "Operated as BA112"
    assert operated_note("BA", None) == "Operated by BA"
    assert operated_note(None, None) is None


# --- Against the database ------------------------------------------------------------


def airports() -> list[Airport]:
    return [
        Airport(iata="JFK", name="JFK", latitude=0.0, longitude=0.0, tz=JFK),
        Airport(iata="LAX", name="LAX", latitude=0.0, longitude=0.0, tz=LAX),
        Airport(iata="LHR", name="LHR", latitude=0.0, longitude=0.0, tz=LHR),
    ]


@pytest.fixture
async def seeded(database: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope() as session:
        session.add_all(airports())


async def book(session: AsyncSession, departure_local: datetime, **fields: object) -> Booking:
    defaults: dict[str, object] = {
        "marketing_carrier": "DL",
        "marketing_number": "1234",
        "origin_iata": "JFK",
        "dest_iata": "LAX",
        "source": "manual",
    }
    return await create_booking(session, departure_local=departure_local, **(defaults | fields))  # type: ignore[arg-type]


async def test_a_booking_carries_its_local_date_and_a_first_poll(seeded: None) -> None:
    async with session_scope() as session:
        # 23:30 in New York is already the 13th in UTC; the dedupe day is the 12th.
        booking = await book(session, datetime(2026, 9, 12, 23, 30))
        assert booking.scheduled_departure_utc == datetime(2026, 9, 13, 3, 30, tzinfo=UTC)
        assert booking.departure_local_date == date(2026, 9, 12)
        assert booking.next_poll_at == booking.scheduled_departure_utc - FEED_HORIZON


async def test_an_unknown_airport_is_refused_not_guessed(seeded: None) -> None:
    async with session_scope() as session:
        with pytest.raises(UnknownAirport) as raised:
            await book(session, datetime(2026, 9, 12, 9, 0), origin_iata="ZZZ")
    assert raised.value.iata == "ZZZ"


async def test_the_ticket_is_the_only_thing_an_edit_touches(seeded: None) -> None:
    async with session_scope() as session:
        booking = await book(session, datetime(2026, 9, 12, 9, 0))
        changed = await update_ticket(
            session, booking.id, confirmation_code="X7QW2P", seat="14A", notes=None
        )
        assert changed is not None
        assert (changed.confirmation_code, changed.seat, changed.notes) == ("X7QW2P", "14A", None)
        assert changed.scheduled_departure_utc == booking.scheduled_departure_utc
        assert changed.departure_local_date == date(2026, 9, 12)


async def events_for(session: AsyncSession, booking_id: int) -> list[str]:
    stmt = (
        select(FlightEvent.kind)
        .where(FlightEvent.booking_id == booking_id)
        .order_by(FlightEvent.id)
    )
    return list(await session.scalars(stmt))


async def test_adding_a_flight_queues_it_for_the_calendar(seeded: None) -> None:
    async with session_scope() as session:
        booking = await book(session, datetime(2026, 9, 12, 9, 0))
        assert await events_for(session, booking.id) == [EventKind.BOOKING_ADDED]


async def test_a_finished_flight_is_not_queued(seeded: None) -> None:
    async with session_scope() as session:
        booking = await book(session, datetime(2026, 9, 12, 9, 0), status="completed")
        assert await events_for(session, booking.id) == []


async def test_a_ticket_edit_restates_the_flight_only_when_the_calendar_shows_it(
    seeded: None,
) -> None:
    async with session_scope() as session:
        booking = await book(session, datetime(2026, 9, 12, 9, 0))
        await update_ticket(session, booking.id, confirmation_code=None, seat="12A", notes=None)
        await update_ticket(session, booking.id, confirmation_code=None, seat="12A", notes=None)
        await update_ticket(session, booking.id, confirmation_code=None, seat="12A", notes="aisle")
        assert await events_for(session, booking.id) == [
            EventKind.BOOKING_ADDED,
            EventKind.BOOKING_EDITED,
        ]


async def test_a_ticket_for_a_flight_that_is_not_there_is_none(seeded: None) -> None:
    async with session_scope() as session:
        missing = await update_ticket(session, 99, confirmation_code=None, seat=None, notes=None)
        assert missing is None


async def test_the_same_flight_resent_with_a_corrected_time_is_a_duplicate(seeded: None) -> None:
    async with session_scope() as session:
        original = await book(session, datetime(2026, 9, 12, 19, 30))
        # 20:30 in New York is the next UTC day; it is still the same flight.
        twin = await find_duplicate(session, "DL", "1234", datetime(2026, 9, 13, 0, 30, tzinfo=UTC))
        assert twin is not None and twin.id == original.id


async def test_the_next_days_flight_is_not_a_duplicate(seeded: None) -> None:
    async with session_scope() as session:
        await book(session, datetime(2026, 9, 12, 23, 30))
        # 00:30 on the 13th in New York, an hour later on the clock and a day later on
        # the calendar.
        assert (
            await find_duplicate(session, "DL", "1234", datetime(2026, 9, 13, 4, 30, tzinfo=UTC))
            is None
        )


async def test_the_operating_flight_number_is_the_same_aeroplane(seeded: None) -> None:
    async with session_scope() as session:
        original = await book(
            session,
            datetime(2026, 9, 12, 9, 0),
            marketing_carrier="AA",
            marketing_number="6141",
            operating_carrier="BA",
            operating_number="0112",
        )
        twin = await find_duplicate(session, "ba", "112", datetime(2026, 9, 12, 13, 0, tzinfo=UTC))
        assert twin is not None and twin.id == original.id


async def test_an_archived_booking_does_not_count(seeded: None) -> None:
    async with session_scope() as session:
        booking = await book(session, datetime(2026, 9, 12, 9, 0))
        await delete_booking(session, booking.id)
        assert await find_duplicate(session, "DL", "1234", booking.scheduled_departure_utc) is None
        # And the index lets the same flight be added again.
        again = await book(session, datetime(2026, 9, 12, 9, 0))
        assert again.id != booking.id


async def test_the_index_backs_the_rule_up(seeded: None) -> None:
    from sqlalchemy.exc import IntegrityError

    async with session_scope() as session:
        await book(session, datetime(2026, 9, 12, 9, 0))
        with pytest.raises(IntegrityError):
            await book(session, datetime(2026, 9, 12, 11, 0))
        await session.rollback()


async def test_latest_snapshots_picks_the_newest_row_per_booking(seeded: None) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        first = await book(session, datetime(2026, 9, 12, 9, 0))
        second = await book(session, datetime(2026, 9, 13, 9, 0))
        unpolled = await book(session, datetime(2026, 9, 14, 9, 0))
        session.add_all(
            [
                FlightSnapshot(
                    booking_id=first.id,
                    raw={},
                    gate_origin="old",
                    observed_at=now - timedelta(hours=2),
                ),
                FlightSnapshot(booking_id=first.id, raw={}, gate_origin="new", observed_at=now),
                # Two rows in the same second: the later insert wins.
                FlightSnapshot(
                    booking_id=second.id, raw={}, gate_origin="earlier", observed_at=now
                ),
                FlightSnapshot(booking_id=second.id, raw={}, gate_origin="later", observed_at=now),
            ]
        )
        await session.flush()

        newest = await latest_snapshots(session, [first.id, second.id, unpolled.id])
        assert {key: value.gate_origin for key, value in newest.items()} == {
            first.id: "new",
            second.id: "later",
        }
        one = await latest_snapshot(session, first.id)
        assert one is not None and one.gate_origin == "new"
        assert await latest_snapshot(session, unpolled.id) is None
        assert await latest_snapshots(session, []) == {}
