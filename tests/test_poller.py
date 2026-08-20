"""The poll cadence, which is the whole of the project's spend control."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from flighter.poller import (
    ABANDON_AFTER,
    CLOSE_INTERVAL,
    DAILY_INTERVAL,
    HOURLY_INTERVAL,
    next_poll_at,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@dataclass
class FakeSnapshot:
    scheduled_out: datetime | None = None
    estimated_out: datetime | None = None
    actual_off: datetime | None = None
    actual_on: datetime | None = None
    cancelled: bool | None = False
    diverted: bool | None = False
    observed_at: datetime | None = NOW


def scheduled(delta: timedelta, **kwargs: object) -> FakeSnapshot:
    return FakeSnapshot(scheduled_out=NOW + delta, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("label", "snapshot", "expected"),
    [
        # Far out: a wake-up at departure minus 7 days, never None.
        ("10 days out", scheduled(timedelta(days=10)), NOW + timedelta(days=3)),
        ("7 days + 1s", scheduled(timedelta(days=7, seconds=1)), NOW + timedelta(seconds=1)),
        # Exactly 7 days is already inside the daily band.
        ("exactly 7 days", scheduled(timedelta(days=7)), NOW + DAILY_INTERVAL),
        ("5 days out", scheduled(timedelta(days=5)), NOW + DAILY_INTERVAL),
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


def test_cancelled_polls_hard_for_two_hours_from_the_observation() -> None:
    snapshot = FakeSnapshot(
        scheduled_out=NOW + timedelta(days=4),
        cancelled=True,
        observed_at=NOW - timedelta(minutes=119),
    )
    assert next_poll_at(NOW, snapshot) == NOW + CLOSE_INTERVAL


def test_cancelled_falls_back_to_the_table_after_two_hours() -> None:
    snapshot = FakeSnapshot(
        scheduled_out=NOW + timedelta(days=4),
        cancelled=True,
        observed_at=NOW - timedelta(hours=2, minutes=1),
    )
    assert next_poll_at(NOW, snapshot) == NOW + DAILY_INTERVAL


def test_diverted_override_outranks_the_landed_tail() -> None:
    """A diversion that has already landed is exactly when the gate and bags change."""
    snapshot = FakeSnapshot(
        actual_off=NOW - timedelta(hours=5),
        actual_on=NOW - timedelta(hours=4),
        diverted=True,
        observed_at=NOW - timedelta(minutes=5),
    )
    assert next_poll_at(NOW, snapshot) == NOW + CLOSE_INTERVAL


def test_diverted_and_long_finished_still_completes() -> None:
    snapshot = FakeSnapshot(
        actual_on=NOW - timedelta(hours=6),
        diverted=True,
        observed_at=NOW - timedelta(hours=3),
    )
    assert next_poll_at(NOW, snapshot) is None


def test_departure_long_past_without_a_takeoff_is_abandoned() -> None:
    snapshot = FakeSnapshot(scheduled_out=NOW - ABANDON_AFTER - timedelta(minutes=1))
    assert next_poll_at(NOW, snapshot) is None


def test_no_departure_estimate_keeps_a_moderate_cadence() -> None:
    assert next_poll_at(NOW, FakeSnapshot()) == NOW + HOURLY_INTERVAL


def test_naive_timestamps_are_read_as_utc() -> None:
    snapshot = FakeSnapshot(scheduled_out=(NOW + timedelta(hours=1)).replace(tzinfo=None))
    assert next_poll_at(NOW.replace(tzinfo=None), snapshot) == NOW + CLOSE_INTERVAL
