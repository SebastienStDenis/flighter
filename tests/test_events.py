"""Snapshot diffing: the rules that decide whether a change is worth anyone's attention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flight_tracker.events import (
    ARRIVAL_TIME_CHANGED,
    BAGGAGE_CLAIM_ASSIGNED,
    CANCELLED,
    DEPARTED,
    DEPARTURE_DELAYED,
    DEPARTURE_MOVED_EARLIER,
    DIVERTED,
    GATE_ASSIGNED,
    GATE_CHANGED,
    LANDED,
    TERMINAL_CHANGED,
    DetectedChange,
    diff_snapshots,
)
from flight_tracker.models import FlightSnapshot

DEPARTS = datetime(2026, 9, 12, 19, 0, tzinfo=UTC)
ARRIVES = datetime(2026, 9, 12, 22, 30, tzinfo=UTC)


def snapshot(**fields: object) -> FlightSnapshot:
    base: dict[str, object] = {
        "raw": {},
        "cancelled": False,
        "diverted": False,
        "scheduled_out": DEPARTS,
        "estimated_out": DEPARTS,
        "scheduled_in": ARRIVES,
        "estimated_in": ARRIVES,
    }
    base.update(fields)
    return FlightSnapshot(**base)


def kinds(changes: list[DetectedChange]) -> list[str]:
    return [change.kind for change in changes]


def only(changes: list[DetectedChange], kind: str) -> DetectedChange:
    matching = [change for change in changes if change.kind == kind]
    assert len(matching) == 1, kinds(changes)
    return matching[0]


def test_gate_assigned_then_changed() -> None:
    assigned = diff_snapshots(snapshot(), snapshot(gate_origin="B22"))
    assert kinds(assigned) == [GATE_ASSIGNED]
    assert only(assigned, GATE_ASSIGNED).new_value == "B22"

    changed = diff_snapshots(snapshot(gate_origin="B22"), snapshot(gate_origin="C14"))
    assert kinds(changed) == [GATE_CHANGED]
    assert (only(changed, GATE_CHANGED).old_value, changed[0].new_value) == ("B22", "C14")


def test_terminal_change_fires() -> None:
    changes = diff_snapshots(snapshot(terminal_origin="4"), snapshot(terminal_origin="2"))
    assert kinds(changes) == [TERMINAL_CHANGED]


def test_departure_delay_and_recovery_fire() -> None:
    delayed = diff_snapshots(snapshot(), snapshot(estimated_out=DEPARTS + timedelta(minutes=35)))
    assert kinds(delayed) == [DEPARTURE_DELAYED]

    earlier = diff_snapshots(snapshot(), snapshot(estimated_out=DEPARTS - timedelta(minutes=20)))
    assert kinds(earlier) == [DEPARTURE_MOVED_EARLIER]


def test_arrival_change_fires_past_its_wider_band() -> None:
    within = diff_snapshots(snapshot(), snapshot(estimated_in=ARRIVES + timedelta(minutes=12)))
    assert kinds(within) == []

    beyond = diff_snapshots(snapshot(), snapshot(estimated_in=ARRIVES + timedelta(minutes=25)))
    assert kinds(beyond) == [ARRIVAL_TIME_CHANGED]


def test_departed_landed_and_baggage_fire() -> None:
    off = DEPARTS + timedelta(minutes=22)
    assert kinds(diff_snapshots(snapshot(), snapshot(actual_off=off))) == [DEPARTED]

    on = ARRIVES - timedelta(minutes=10)
    assert kinds(diff_snapshots(snapshot(), snapshot(actual_on=on))) == [LANDED]

    bags = diff_snapshots(snapshot(), snapshot(baggage_claim="3"))
    assert kinds(bags) == [BAGGAGE_CLAIM_ASSIGNED]
    assert only(bags, BAGGAGE_CLAIM_ASSIGNED).new_value == "3"


def test_cancelled_and_diverted_fire() -> None:
    assert kinds(diff_snapshots(snapshot(), snapshot(cancelled=True))) == [CANCELLED]
    assert kinds(diff_snapshots(snapshot(), snapshot(diverted=True))) == [DIVERTED]


def test_dead_band_suppresses_a_six_minute_slip() -> None:
    slipped = snapshot(estimated_out=DEPARTS + timedelta(minutes=6))
    assert diff_snapshots(snapshot(), slipped) == []


def test_repeated_small_slips_fire_once_cumulatively() -> None:
    """Three 8-minute slips each duck the 10-minute band, but 24 minutes of delay must
    not go unreported: the band is measured from the last value we told the user."""
    baselines: dict[str, str] = {}
    fired = []
    previous = snapshot()
    for step in (8, 16, 24):
        current = snapshot(estimated_out=DEPARTS + timedelta(minutes=step))
        for change in diff_snapshots(previous, current, baselines=baselines):
            fired.append(change)
            assert change.new_value is not None
            baselines["estimated_out"] = change.new_value
        previous = current

    assert kinds(fired) == [DEPARTURE_DELAYED]
    assert fired[0].new_value == (DEPARTS + timedelta(minutes=16)).isoformat()


def test_first_observation_is_silent() -> None:
    assert diff_snapshots(None, snapshot(gate_origin="B22", actual_off=DEPARTS)) == []


def test_first_observation_still_reports_cancellation_and_diversion() -> None:
    changes = diff_snapshots(None, snapshot(cancelled=True, diverted=True))
    assert kinds(changes) == [CANCELLED, DIVERTED]


def test_null_to_null_is_not_a_change() -> None:
    assert diff_snapshots(snapshot(), snapshot()) == []


def test_a_dropped_field_is_not_a_change() -> None:
    """AeroAPI blanks a gate now and then; that is a gap in the feed, not a reassignment."""
    assert diff_snapshots(snapshot(gate_origin="B22"), snapshot(gate_origin=None)) == []
