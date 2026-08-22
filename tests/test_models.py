"""The column that keeps every instant in the database UTC, and the enums that keep the
status strings honest.

SQLite has no `timestamptz`: it writes back whatever wall clock it is given and hands it
back naive. Without `UtcDateTime` an aware Montreal time would be stored with its offset
quietly dropped, and every time on screen would be four hours wrong in the direction
nobody notices until they are at the gate. These are the two conversions, in both
directions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import sqlite

from flighter.models import (
    Booking,
    BookingSource,
    BookingStatus,
    EventKind,
    IngestOutcome,
    UtcDateTime,
)
from flighter.timezones import to_utc

DIALECT = sqlite.dialect()
COLUMN = UtcDateTime()

EDT = timezone(timedelta(hours=-4))


def test_an_offset_becomes_utc_before_it_is_stored() -> None:
    """22:00 in Montreal is 02:00 the next morning in UTC, and that is what goes in."""
    stored = COLUMN.process_bind_param(datetime(2026, 9, 12, 22, 0, tzinfo=EDT), DIALECT)
    assert stored == datetime(2026, 9, 13, 2, 0)
    assert stored is not None and stored.tzinfo is None


def test_a_stored_value_comes_back_aware_and_utc() -> None:
    read = COLUMN.process_result_value(datetime(2026, 9, 13, 2, 0), DIALECT)
    assert read == datetime(2026, 9, 13, 2, 0, tzinfo=UTC)


def test_a_round_trip_is_the_same_instant() -> None:
    departure = to_utc(datetime(2026, 9, 12, 22, 0), "America/Montreal")
    read = COLUMN.process_result_value(COLUMN.process_bind_param(departure, DIALECT), DIALECT)
    assert read == departure


def test_a_naive_datetime_is_refused() -> None:
    """A naive datetime here is a wall-clock reading at some airport. Storing it as
    though it were UTC is the bug the whole timezone policy exists to prevent."""
    with pytest.raises(ValueError, match="naive"):
        COLUMN.process_bind_param(datetime(2026, 9, 12, 22, 0), DIALECT)


def test_nothing_is_still_nothing_in_both_directions() -> None:
    assert COLUMN.process_bind_param(None, DIALECT) is None
    assert COLUMN.process_result_value(None, DIALECT) is None


def test_status_enums_are_the_strings_the_database_holds() -> None:
    """Rows written before the enums existed hold plain strings, and the check
    constraints are spelled from the same values, so the two must compare equal."""
    assert BookingStatus.ACTIVE == "active"
    assert BookingSource.EMAIL == "email"
    assert IngestOutcome.NO_FLIGHT == "no_flight"
    assert EventKind.DEPARTURE_MOVED_EARLIER == "DepartureMovedEarlier"
    assert "archived" in {BookingStatus.ARCHIVED}


def test_the_status_constraint_is_spelled_from_the_enum() -> None:
    constraints = {constraint.name: constraint for constraint in Booking.__table__.constraints}
    status_check = str(constraints["bookings_status_check"].sqltext)
    for status in BookingStatus:
        assert f"'{status}'" in status_check
    assert "cancelled" not in status_check


def test_a_booking_is_mine_until_a_friend_is_named() -> None:
    column = Booking.__table__.columns["friend_name"]
    assert column.nullable
    assert Booking(friend_name=None).friend_name is None
