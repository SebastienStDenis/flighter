"""`to_booking_times` - the single conversion every booking passes through."""

from __future__ import annotations

from datetime import UTC, datetime

from flighter.bookings import _normalised, to_booking_times

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


def test_edits_are_normalised_the_same_way_inserts_are() -> None:
    # An edit writing a lower-case carrier or a zero-padded number would look like a
    # different flight to the dedupe index than the same flight inserted fresh.
    assert _normalised("marketing_carrier", " ac ") == "AC"
    assert _normalised("marketing_number", "0871") == "871"
    assert _normalised("origin_iata", "yul") == "YUL"
    assert _normalised("seat", "14c") == "14c"
    assert _normalised("marketing_number", None) is None
