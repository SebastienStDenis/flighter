"""The timezone rules the whole schedule rests on, pinned to real flights.

Every case here is a flight that a naive implementation gets wrong: one that lands the
next day, one that lands "before" it left, one that departs the morning the clocks
change, and one whose email states an offset that is not to be believed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flighter.timezones import format_local, same_local_date, to_local, to_utc

JFK = "America/New_York"
LHR = "Europe/London"
LAX = "America/Los_Angeles"
NRT = "Asia/Tokyo"
ORD = "America/Chicago"


def test_same_day_domestic() -> None:
    """LAX 08:00 -> JFK 16:30, an ordinary transcon."""
    departure = to_utc(datetime(2026, 6, 10, 8, 0), LAX)
    arrival = to_utc(datetime(2026, 6, 10, 16, 30), JFK)

    assert departure == datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    assert arrival == datetime(2026, 6, 10, 20, 30, tzinfo=UTC)
    assert (arrival - departure).total_seconds() == 5.5 * 3600
    assert same_local_date(departure, arrival, JFK)


def test_overnight_crosses_into_the_next_local_day() -> None:
    """JFK 23:30 -> LHR 11:45 the next morning."""
    departure = to_utc(datetime(2026, 9, 12, 23, 30), JFK)
    arrival = to_utc(datetime(2026, 9, 13, 11, 45), LHR)

    assert departure == datetime(2026, 9, 13, 3, 30, tzinfo=UTC)
    assert arrival == datetime(2026, 9, 13, 10, 45, tzinfo=UTC)
    assert arrival > departure

    # The departure's own local date is still the 12th even though it is already the
    # 13th in UTC, and the arrival lands a day later at the destination.
    assert to_local(departure, JFK).date() == datetime(2026, 9, 12).date()
    assert to_local(arrival, LHR).date() == datetime(2026, 9, 13).date()
    assert not same_local_date(departure, arrival, JFK)


def test_date_line_arrival_reads_earlier_but_is_later() -> None:
    """NRT 17:00 -> LAX 10:30 the *same* local date, seven hours after take-off."""
    departure = to_utc(datetime(2026, 3, 15, 17, 0), NRT)
    arrival = to_utc(datetime(2026, 3, 15, 10, 30), LAX)

    assert departure == datetime(2026, 3, 15, 8, 0, tzinfo=UTC)
    assert arrival == datetime(2026, 3, 15, 17, 30, tzinfo=UTC)
    assert arrival > departure
    assert (arrival - departure).total_seconds() == 9.5 * 3600

    # The clocks read backwards, which is exactly the trap.
    assert to_local(arrival, LAX).time() < to_local(departure, NRT).time()


def test_dst_boundary_uses_the_offset_in_force_that_day() -> None:
    """A 09:00 departure from ORD the morning US clocks spring forward (8 Mar 2026)."""
    on_the_day = to_utc(datetime(2026, 3, 8, 9, 0), ORD)
    a_week_earlier = to_utc(datetime(2026, 3, 1, 9, 0), ORD)

    # Same wall clock, an hour apart in UTC: CDT (-05:00) against CST (-06:00).
    assert on_the_day == datetime(2026, 3, 8, 14, 0, tzinfo=UTC)
    assert a_week_earlier == datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
    assert to_local(on_the_day, ORD).utcoffset() != to_local(a_week_earlier, ORD).utcoffset()

    # A 01:30 departure is before the 02:00 jump and is still on standard time.
    before_the_jump = to_utc(datetime(2026, 3, 8, 1, 30), ORD)
    assert before_the_jump == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)


def test_offset_stated_in_an_email_is_ignored() -> None:
    """A naive wall clock is read at the airport's zone, whatever the email claimed."""
    stated = datetime(2026, 9, 12, 23, 30)

    assert to_utc(stated, JFK) == datetime(2026, 9, 13, 3, 30, tzinfo=UTC)
    # Not what taking the mail's "+00:00" at face value would have given.
    assert to_utc(stated, JFK) != stated.replace(tzinfo=UTC)
    # Same reading, different airport, different instant: the zone is the only input.
    assert to_utc(stated, LAX) == datetime(2026, 9, 13, 6, 30, tzinfo=UTC)


def test_aware_input_is_converted_not_reinterpreted() -> None:
    """Passing an already-UTC instant back through to_utc must be a no-op."""
    instant = datetime(2026, 9, 13, 3, 30, tzinfo=UTC)
    assert to_utc(instant, JFK) == instant
    assert to_utc(to_utc(instant, JFK), LAX) == instant


def test_local_times_are_always_rendered_with_a_zone() -> None:
    """Two airports on one page means a bare 18:40 is a missed flight."""
    departure = to_utc(datetime(2026, 9, 12, 23, 30), JFK)

    assert format_local(departure, JFK) == "23:30 EDT"
    assert format_local(departure, LHR) == "04:30 BST"
    assert format_local(departure, JFK, with_date=True).startswith("Sat 12 Sep 23:30")


def test_unknown_zone_falls_back_rather_than_raising() -> None:
    naive = datetime(2026, 6, 10, 8, 0)
    assert to_utc(naive, "Mars/Olympus_Mons") == naive.replace(tzinfo=UTC)
