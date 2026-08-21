"""The poll cadence, which is the whole of the project's spend control, and the tick
that spends it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flighter import poller
from flighter.bookings import create_booking, latest_snapshot
from flighter.cadence import (
    ABANDON_AFTER,
    CLOSE_INTERVAL,
    DAILY_INTERVAL,
    FEED_HORIZON,
    HOURLY_INTERVAL,
    RETRY_INTERVAL,
    first_poll_at,
    next_poll_at,
    retry_poll_at,
)
from flighter.db import session_scope
from flighter.models import (
    Airport,
    ApiUsage,
    Booking,
    BookingStatus,
    EventKind,
    FlightEvent,
    FlightSnapshot,
)
from flighter.poller import HISTORY_RETENTION, USAGE_RETENTION, poll_once, prune_history

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@dataclass
class FakeSnapshot:
    scheduled_out: datetime | None = None
    estimated_out: datetime | None = None
    actual_off: datetime | None = None
    actual_on: datetime | None = None
    cancelled: bool | None = False


def scheduled(delta: timedelta, **kwargs: object) -> FakeSnapshot:
    return FakeSnapshot(scheduled_out=NOW + delta, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("label", "snapshot", "expected"),
    [
        # Beyond the feed's horizon there is nothing to see: one wake-up, at the horizon.
        ("10 days out", scheduled(timedelta(days=10)), NOW + timedelta(days=8)),
        ("2 days + 1s", scheduled(FEED_HORIZON + timedelta(seconds=1)), NOW + timedelta(seconds=1)),
        # Exactly at the horizon is already inside the daily band.
        ("exactly 2 days", scheduled(FEED_HORIZON), NOW + DAILY_INTERVAL),
        # Daily, but never past the boundary of the next, tighter band.
        ("25h out", scheduled(timedelta(hours=25)), NOW + timedelta(hours=1)),
        ("exactly 24h", scheduled(timedelta(hours=24)), NOW + HOURLY_INTERVAL),
        ("6h out", scheduled(timedelta(hours=6)), NOW + HOURLY_INTERVAL),
        ("3h20m out", scheduled(timedelta(hours=3, minutes=20)), NOW + timedelta(minutes=20)),
        ("exactly 3h", scheduled(timedelta(hours=3)), NOW + CLOSE_INTERVAL),
        ("40m out", scheduled(timedelta(minutes=40)), NOW + CLOSE_INTERVAL),
        # Departure passed but still on the ground: keep watching.
        ("30m late, not off", scheduled(timedelta(minutes=-30)), NOW + CLOSE_INTERVAL),
    ],
)
def test_cadence_windows(label: str, snapshot: FakeSnapshot, expected: datetime) -> None:
    assert next_poll_at(NOW, snapshot) == expected, label


def test_estimated_out_beats_scheduled_out() -> None:
    """A 6h delay must move the flight back into the daily band, not stay at 10 min."""
    snapshot = FakeSnapshot(
        scheduled_out=NOW + timedelta(hours=2),
        estimated_out=NOW + timedelta(hours=30),
    )
    assert next_poll_at(NOW, snapshot) == NOW + timedelta(hours=6)


def test_airborne_polls_every_ten_minutes() -> None:
    snapshot = FakeSnapshot(
        scheduled_out=NOW - timedelta(hours=2),
        actual_off=NOW - timedelta(hours=2),
    )
    assert next_poll_at(NOW, snapshot) == NOW + CLOSE_INTERVAL


def test_landed_keeps_polling_through_the_baggage_tail() -> None:
    snapshot = FakeSnapshot(
        scheduled_out=NOW - timedelta(hours=5),
        actual_off=NOW - timedelta(hours=5),
        actual_on=NOW - timedelta(minutes=30),
    )
    assert next_poll_at(NOW, snapshot) == NOW + CLOSE_INTERVAL


def test_landed_exactly_ninety_minutes_ago_still_polls() -> None:
    snapshot = FakeSnapshot(actual_on=NOW - timedelta(minutes=90))
    assert next_poll_at(NOW, snapshot) == NOW + CLOSE_INTERVAL


def test_landed_ninety_one_minutes_ago_is_done() -> None:
    snapshot = FakeSnapshot(actual_on=NOW - timedelta(minutes=91))
    assert next_poll_at(NOW, snapshot) is None


def test_cancelled_gets_one_confirming_poll_then_stops() -> None:
    """A flight FlightAware has dropped is looked at once more and then left alone,
    however far off its departure: there is nothing further to learn, and ten-minute
    polls until the abandon point would be a dollar a flight."""
    first = FakeSnapshot(scheduled_out=NOW + timedelta(days=4), cancelled=True)
    assert next_poll_at(NOW, first) == NOW + CLOSE_INTERVAL
    assert next_poll_at(NOW, first, previous=FakeSnapshot(cancelled=False)) == NOW + CLOSE_INTERVAL

    confirmed = FakeSnapshot(scheduled_out=NOW + timedelta(days=4), cancelled=True)
    assert next_poll_at(NOW, confirmed, previous=first) is None


def test_a_cancellation_that_clears_returns_to_the_table() -> None:
    previous = FakeSnapshot(scheduled_out=NOW + timedelta(days=4), cancelled=True)
    restored = FakeSnapshot(scheduled_out=NOW + timedelta(days=4), cancelled=False)
    assert next_poll_at(NOW, restored, previous=previous) == NOW + timedelta(days=2)


def test_a_diversion_follows_the_flight_like_any_other() -> None:
    """The diverted leg is in the air and then on the ground: the airborne and landed
    rules already watch exactly those minutes."""
    in_the_air = FakeSnapshot(actual_off=NOW - timedelta(hours=2))
    assert next_poll_at(NOW, in_the_air) == NOW + CLOSE_INTERVAL
    long_down = FakeSnapshot(
        actual_off=NOW - timedelta(hours=6), actual_on=NOW - timedelta(hours=4)
    )
    assert next_poll_at(NOW, long_down) is None


def test_departure_long_past_without_a_takeoff_is_abandoned() -> None:
    snapshot = FakeSnapshot(scheduled_out=NOW - ABANDON_AFTER - timedelta(minutes=1))
    assert next_poll_at(NOW, snapshot) is None


def test_no_departure_estimate_keeps_a_moderate_cadence() -> None:
    assert next_poll_at(NOW, FakeSnapshot()) == NOW + HOURLY_INTERVAL


def test_naive_timestamps_are_read_as_utc() -> None:
    snapshot = FakeSnapshot(scheduled_out=(NOW + timedelta(hours=1)).replace(tzinfo=None))
    assert next_poll_at(NOW.replace(tzinfo=None), snapshot) == NOW + CLOSE_INTERVAL


# --- Nothing came back ---------------------------------------------------------------


def test_an_unresolved_booking_waits_for_the_feed_horizon() -> None:
    """A flight AeroAPI cannot see yet is not asked about again until it can be: the
    alternative is forty-eight empty result sets a day for as long as the flight is out."""
    departure = NOW + timedelta(days=30)
    assert retry_poll_at(NOW, departure) == departure - FEED_HORIZON


def test_a_retry_is_never_faster_than_the_retry_interval() -> None:
    assert retry_poll_at(NOW, NOW + timedelta(hours=1)) == NOW + RETRY_INTERVAL
    assert retry_poll_at(NOW, NOW + timedelta(hours=20)) == NOW + RETRY_INTERVAL


def test_a_retry_gives_up_where_a_poll_would() -> None:
    assert retry_poll_at(NOW, NOW - ABANDON_AFTER - timedelta(minutes=1)) is None
    assert retry_poll_at(NOW, NOW - ABANDON_AFTER + timedelta(minutes=1)) == NOW + RETRY_INTERVAL


def test_the_first_poll_is_at_the_horizon_or_now_whichever_is_later() -> None:
    far_out = NOW + timedelta(days=90)
    assert first_poll_at(NOW, far_out) == far_out - FEED_HORIZON
    assert first_poll_at(NOW, NOW + timedelta(hours=3)) == NOW
    # A flight that has already flown is polled once, which is what closes it.
    assert first_poll_at(NOW, NOW - timedelta(days=3)) == NOW


# --- The tick ------------------------------------------------------------------------


def airports() -> list[Airport]:
    return [
        Airport(iata="JFK", name="JFK", latitude=0.0, longitude=0.0, tz="America/New_York"),
        Airport(iata="LAX", name="LAX", latitude=0.0, longitude=0.0, tz="America/Los_Angeles"),
    ]


FLIGHT: dict[str, Any] = {
    "fa_flight_id": "DAL1234-1",
    "ident": "DAL1234",
    "cancelled": False,
    "diverted": False,
    "gate_origin": "B22",
    "scheduled_out": "2026-09-12T14:00:00Z",
}


async def booked(sessions: async_sessionmaker[AsyncSession], departure_local: datetime) -> int:
    async with session_scope() as session:
        session.add_all(airports())
        await session.flush()
        booking = await create_booking(
            session,
            marketing_carrier="DL",
            marketing_number="1234",
            origin_iata="JFK",
            dest_iata="LAX",
            departure_local=departure_local,
            source="manual",
        )
        booking.next_poll_at = datetime.now(UTC) - timedelta(minutes=1)
        return booking.id


async def test_a_tick_records_the_observation_and_reschedules(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    booking_id = await booked(database, datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2))

    async def fetch(booking: Booking, client: Any = None) -> dict[str, Any]:
        # No session may be open while FlightAware is being asked: the shared in-memory
        # connection would refuse a second transaction, and so would production's lock.
        async with session_scope() as session:
            assert await session.get(Booking, booking.id) is not None
        booking.aeroapi_fa_flight_id = FLIGHT["fa_flight_id"]
        return FLIGHT

    monkeypatch.setattr(poller, "fetch_flight", fetch)
    assert await poll_once() == 1

    async with session_scope() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.aeroapi_fa_flight_id == FLIGHT["fa_flight_id"]
        assert booking.next_poll_at is not None
        assert booking.next_poll_at > datetime.now(UTC) + CLOSE_INTERVAL - timedelta(seconds=5)
        snapshot = await latest_snapshot(session, booking_id)
        assert snapshot is not None and snapshot.gate_origin == "B22"


async def test_a_booking_the_feed_cannot_see_backs_off_to_the_horizon(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    departure_local = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=40)
    booking_id = await booked(database, departure_local)

    async def nothing(booking: Booking, client: Any = None) -> None:
        return None

    monkeypatch.setattr(poller, "fetch_flight", nothing)
    assert await poll_once() == 1

    async with session_scope() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.next_poll_at == booking.scheduled_departure_utc - FEED_HORIZON
        assert await latest_snapshot(session, booking_id) is None


async def test_a_flight_long_gone_that_never_resolves_is_completed(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    booking_id = await booked(database, datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3))

    async def nothing(booking: Booking, client: Any = None) -> None:
        return None

    monkeypatch.setattr(poller, "fetch_flight", nothing)
    await poll_once()

    async with session_scope() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.status == BookingStatus.COMPLETED
        assert booking.next_poll_at is None


async def test_a_failed_poll_leaves_the_lease_standing(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    booking_id = await booked(database, datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2))

    async def explode(booking: Booking, client: Any = None) -> None:
        raise RuntimeError("FlightAware is down")

    monkeypatch.setattr(poller, "fetch_flight", explode)
    before = datetime.now(UTC)
    assert await poll_once() == 1

    async with session_scope() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.next_poll_at is not None
        assert booking.next_poll_at >= before + RETRY_INTERVAL - timedelta(seconds=1)
        assert booking.status == BookingStatus.ACTIVE


# --- Retention -----------------------------------------------------------------------


async def test_prune_drops_only_what_is_finished_and_old(
    database: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    old = now - HISTORY_RETENTION - timedelta(days=1)
    async with session_scope() as session:
        session.add_all(airports())
        await session.flush()
        done = await create_booking(
            session,
            marketing_carrier="DL",
            marketing_number="1",
            origin_iata="JFK",
            dest_iata="LAX",
            departure_local=datetime(2026, 1, 1, 9, 0),
            source="manual",
            status=BookingStatus.COMPLETED,
        )
        live = await create_booking(
            session,
            marketing_carrier="DL",
            marketing_number="2",
            origin_iata="JFK",
            dest_iata="LAX",
            departure_local=datetime(2026, 1, 1, 9, 0),
            source="manual",
        )
        session.add_all(
            [
                FlightSnapshot(booking_id=done.id, raw={}, observed_at=old),
                FlightSnapshot(booking_id=done.id, raw={}, observed_at=now),
                # An active booking's newest row is its current state however old it is.
                FlightSnapshot(booking_id=live.id, raw={}, observed_at=old),
                FlightEvent(booking_id=done.id, kind="Landed", occurred_at=old),
                FlightEvent(booking_id=done.id, kind="Landed", occurred_at=now),
                ApiUsage(endpoint="/flights/{ident}", est_cost_usd=0.005, called_at=old),
                ApiUsage(
                    endpoint="/flights/{ident}",
                    est_cost_usd=0.005,
                    called_at=now - USAGE_RETENTION - timedelta(days=1),
                ),
            ]
        )

    await prune_history(now)

    async with session_scope() as session:
        snapshots = (await session.scalars(select(FlightSnapshot.booking_id))).all()
        assert sorted(snapshots) == sorted([done.id, live.id])
        kept = (await session.scalars(select(FlightEvent.kind).order_by(FlightEvent.id))).all()
        assert kept == [EventKind.BOOKING_ADDED, EventKind.LANDED]
        assert await session.scalar(select(func.count()).select_from(ApiUsage)) == 1
