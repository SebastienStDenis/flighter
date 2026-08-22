"""Snapshot diffing: the rules that decide whether a change is worth anyone's attention.
And delivery: the promise that nothing is stamped as sent until it has been."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flighter.bookings import create_booking, delete_booking
from flighter.db import session_scope
from flighter.events import (
    CALENDAR_WINDOW,
    NOTIFY_WINDOW,
    DetectedChange,
    diff_snapshots,
    dispatch_pending,
)
from flighter.models import Airport, Booking, EventKind, FlightEvent, FlightSnapshot

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
    assert kinds(assigned) == [EventKind.GATE_ASSIGNED]
    assert only(assigned, EventKind.GATE_ASSIGNED).new_value == "B22"

    changed = diff_snapshots(snapshot(gate_origin="B22"), snapshot(gate_origin="C14"))
    assert kinds(changed) == [EventKind.GATE_CHANGED]
    assert (only(changed, EventKind.GATE_CHANGED).old_value, changed[0].new_value) == (
        "B22",
        "C14",
    )


def test_terminal_change_fires() -> None:
    changes = diff_snapshots(snapshot(terminal_origin="4"), snapshot(terminal_origin="2"))
    assert kinds(changes) == [EventKind.TERMINAL_CHANGED]


def test_departure_delay_and_recovery_fire() -> None:
    delayed = diff_snapshots(snapshot(), snapshot(estimated_out=DEPARTS + timedelta(minutes=35)))
    assert kinds(delayed) == [EventKind.DEPARTURE_DELAYED]

    earlier = diff_snapshots(snapshot(), snapshot(estimated_out=DEPARTS - timedelta(minutes=20)))
    assert kinds(earlier) == [EventKind.DEPARTURE_MOVED_EARLIER]


def test_arrival_change_fires_past_its_band() -> None:
    within = diff_snapshots(snapshot(), snapshot(estimated_in=ARRIVES + timedelta(minutes=12)))
    assert kinds(within) == []

    beyond = diff_snapshots(snapshot(), snapshot(estimated_in=ARRIVES + timedelta(minutes=25)))
    assert kinds(beyond) == [EventKind.ARRIVAL_TIME_CHANGED]


def test_departed_landed_and_baggage_fire() -> None:
    out = DEPARTS + timedelta(minutes=2)
    assert kinds(diff_snapshots(snapshot(), snapshot(actual_out=out))) == [EventKind.DEPARTED]

    # Wheels up moves the phase to airborne but raises no event of its own, so it does
    # not arrive as a second "departed" twenty minutes after the first.
    off = DEPARTS + timedelta(minutes=22)
    assert kinds(diff_snapshots(snapshot(), snapshot(actual_off=off))) == []

    on = ARRIVES - timedelta(minutes=10)
    assert kinds(diff_snapshots(snapshot(), snapshot(actual_on=on))) == [EventKind.LANDED]

    bags = diff_snapshots(snapshot(), snapshot(baggage_claim="3"))
    assert kinds(bags) == [EventKind.BAGGAGE_CLAIM_ASSIGNED]
    assert only(bags, EventKind.BAGGAGE_CLAIM_ASSIGNED).new_value == "3"


def test_cancelled_and_diverted_fire() -> None:
    assert kinds(diff_snapshots(snapshot(), snapshot(cancelled=True))) == [EventKind.CANCELLED]
    assert kinds(diff_snapshots(snapshot(), snapshot(diverted=True))) == [EventKind.DIVERTED]


def test_dead_band_suppresses_a_quarter_hour_slip() -> None:
    """Under fifteen minutes is on time, to the industry and to the page, so it is not
    worth a push either."""
    slipped = snapshot(estimated_out=DEPARTS + timedelta(minutes=14))
    assert diff_snapshots(snapshot(), slipped) == []


def test_repeated_small_slips_fire_once_cumulatively() -> None:
    """Three 8-minute slips each duck the band, but 24 minutes of delay must not go
    unreported: the band is measured from the last value we told the user."""
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

    assert kinds(fired) == [EventKind.DEPARTURE_DELAYED]
    assert fired[0].new_value == (DEPARTS + timedelta(minutes=16)).isoformat()


def test_first_observation_is_silent() -> None:
    assert diff_snapshots(None, snapshot(gate_origin="B22", actual_off=DEPARTS)) == []


def test_first_observation_still_reports_cancellation_and_diversion() -> None:
    changes = diff_snapshots(None, snapshot(cancelled=True, diverted=True))
    assert kinds(changes) == [EventKind.CANCELLED, EventKind.DIVERTED]


def test_null_to_null_is_not_a_change() -> None:
    assert diff_snapshots(snapshot(), snapshot()) == []


def test_a_dropped_field_is_not_a_change() -> None:
    """AeroAPI blanks a gate now and then; that is a gap in the feed, not a reassignment."""
    assert diff_snapshots(snapshot(gate_origin="B22"), snapshot(gate_origin=None)) == []


def test_event_kinds_compare_equal_to_their_stored_strings() -> None:
    assert EventKind.GATE_CHANGED == "GateChanged"
    assert "GateChanged" in {EventKind.GATE_CHANGED}


# --- Delivery ------------------------------------------------------------------------


def airports() -> list[Airport]:
    return [
        Airport(iata="JFK", name="JFK", latitude=0.0, longitude=0.0, tz="America/New_York"),
        Airport(iata="LAX", name="LAX", latitude=0.0, longitude=0.0, tz="America/Los_Angeles"),
    ]


class FakeNotifier:
    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.sent: list[tuple[int, str, str]] = []

    async def flight_event(
        self, booking: Booking, event: FlightEvent, *, origin_tz: str, dest_tz: str
    ) -> None:
        if self.failing:
            raise RuntimeError("Pushover is down")
        self.sent.append((booking.id, event.kind, origin_tz))


class FakeCalendar:
    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.upserts: list[tuple[int, str | None]] = []
        self.deleted: list[int] = []

    async def upsert(self, booking: Booking, snapshot: FlightSnapshot | None) -> str | None:
        if self.failing:
            raise RuntimeError("iCloud is down")
        self.upserts.append((booking.id, snapshot.gate_origin if snapshot else None))
        return f"flighter-{booking.id}@flighter.invalid"

    async def delete(self, booking: Booking) -> bool:
        if self.failing:
            raise RuntimeError("iCloud is down")
        self.deleted.append(booking.id)
        return booking.calendar_event_uid is not None


async def tracked(database: async_sessionmaker[AsyncSession], *events: FlightEvent) -> int:
    async with session_scope() as session:
        session.add_all(airports())
        await session.flush()
        booking = await create_booking(
            session,
            marketing_carrier="DL",
            marketing_number="1234",
            origin_iata="JFK",
            dest_iata="LAX",
            departure_local=datetime(2026, 9, 12, 15, 0),
            source="manual",
        )
        # Already on the calendar: what is under test is what the poller finds later.
        now = datetime.now(UTC)
        await session.execute(
            update(FlightEvent)
            .where(FlightEvent.booking_id == booking.id)
            .values(notified_at=now, calendar_synced_at=now)
        )
        for event in events:
            event.booking_id = booking.id
        session.add_all(events)
        return booking.id


async def fresh(database: async_sessionmaker[AsyncSession]) -> int:
    async with session_scope() as session:
        session.add_all(airports())
        await session.flush()
        booking = await create_booking(
            session,
            marketing_carrier="DL",
            marketing_number="1234",
            origin_iata="JFK",
            dest_iata="LAX",
            departure_local=datetime(2026, 9, 12, 15, 0),
            source="manual",
        )
        return booking.id


async def pending() -> list[tuple[str, bool, bool]]:
    async with session_scope() as session:
        rows = (await session.scalars(select(FlightEvent).order_by(FlightEvent.id))).all()
        return [
            (row.kind, row.notified_at is not None, row.calendar_synced_at is not None)
            for row in rows
        ]


async def test_a_new_flight_reaches_the_calendar_without_a_push(
    database: async_sessionmaker[AsyncSession],
) -> None:
    """Adding a flight is news to the calendar months before any snapshot exists, and
    is not news to the person who just typed it in."""
    booking_id = await fresh(database)

    notifier, calendar = FakeNotifier(), FakeCalendar()
    await dispatch_pending(notifier, calendar)
    assert notifier.sent == []
    assert calendar.upserts == [(booking_id, None)]
    assert await pending() == [(EventKind.BOOKING_ADDED, True, True)]
    async with session_scope() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.calendar_event_uid == f"flighter-{booking_id}@flighter.invalid"


async def test_a_delivered_event_is_stamped_on_both_sides(
    database: async_sessionmaker[AsyncSession],
) -> None:
    booking_id = await tracked(
        database,
        FlightEvent(kind=EventKind.GATE_CHANGED, old_value="B22", new_value="C14"),
        FlightEvent(kind=EventKind.ARRIVAL_TIME_CHANGED),
    )
    async with session_scope() as session:
        session.add(FlightSnapshot(booking_id=booking_id, raw={}, gate_origin="C14"))

    notifier, calendar = FakeNotifier(), FakeCalendar()
    await dispatch_pending(notifier, calendar)

    # The arrival wobble is stamped without a push; the calendar gets one upsert for both.
    assert notifier.sent == [(booking_id, EventKind.GATE_CHANGED, "America/New_York")]
    assert calendar.upserts == [(booking_id, "C14")]
    assert await pending() == [
        (EventKind.BOOKING_ADDED, True, True),
        (EventKind.GATE_CHANGED, True, True),
        (EventKind.ARRIVAL_TIME_CHANGED, True, True),
    ]
    async with session_scope() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.calendar_event_uid == f"flighter-{booking_id}@flighter.invalid"


async def test_disabled_friend_outputs_are_stamped_and_calendar_entries_are_removed(
    database: async_sessionmaker[AsyncSession],
) -> None:
    booking_id = await tracked(
        database, FlightEvent(kind=EventKind.GATE_CHANGED, old_value="B22", new_value="C14")
    )
    async with session_scope() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        booking.friend_name = "Sam"
        booking.calendar_event_uid = f"flighter-{booking_id}@flighter.invalid"

    notifier, calendar = FakeNotifier(), FakeCalendar()
    await dispatch_pending(notifier, calendar)

    assert notifier.sent == []
    assert calendar.upserts == []
    assert calendar.deleted == [booking_id]
    assert (await pending())[-1] == (EventKind.GATE_CHANGED, True, True)
    async with session_scope() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None and booking.calendar_event_uid is None


async def test_a_failed_push_is_not_stamped_and_is_retried(
    database: async_sessionmaker[AsyncSession],
) -> None:
    await tracked(database, FlightEvent(kind=EventKind.GATE_CHANGED, old_value="B", new_value="C"))

    await dispatch_pending(FakeNotifier(failing=True), FakeCalendar(failing=True))
    assert await pending() == [
        (EventKind.BOOKING_ADDED, True, True),
        (EventKind.GATE_CHANGED, False, False),
    ]

    notifier, calendar = FakeNotifier(), FakeCalendar()
    await dispatch_pending(notifier, calendar)
    assert len(notifier.sent) == 1
    assert len(calendar.upserts) == 1
    assert await pending() == [
        (EventKind.BOOKING_ADDED, True, True),
        (EventKind.GATE_CHANGED, True, True),
    ]


async def test_one_failure_does_not_hold_up_the_events_behind_it(
    database: async_sessionmaker[AsyncSession],
) -> None:
    """Each stamp is its own transaction, so a batch that dies half-way keeps what it
    delivered. A notifier that fails on the first event and not on the second proves the
    second was stamped regardless."""
    await tracked(
        database,
        FlightEvent(kind=EventKind.GATE_CHANGED, old_value="B", new_value="C"),
        FlightEvent(kind=EventKind.LANDED),
    )

    class FlakyNotifier(FakeNotifier):
        async def flight_event(
            self, booking: Booking, event: FlightEvent, *, origin_tz: str, dest_tz: str
        ) -> None:
            if event.kind == EventKind.GATE_CHANGED:
                raise RuntimeError("dropped")
            await super().flight_event(booking, event, origin_tz=origin_tz, dest_tz=dest_tz)

    await dispatch_pending(FlakyNotifier(), FakeCalendar())
    assert await pending() == [
        (EventKind.BOOKING_ADDED, True, True),
        (EventKind.GATE_CHANGED, False, True),
        (EventKind.LANDED, True, True),
    ]


async def test_a_deleted_flight_is_neither_pushed_nor_put_back_on_the_calendar(
    database: async_sessionmaker[AsyncSession],
) -> None:
    booking_id = await tracked(database, FlightEvent(kind=EventKind.GATE_CHANGED))
    async with session_scope() as session:
        await delete_booking(session, booking_id)

    notifier, calendar = FakeNotifier(), FakeCalendar()
    await dispatch_pending(notifier, calendar)
    assert notifier.sent == []
    assert calendar.upserts == []
    assert await pending() == [
        (EventKind.BOOKING_ADDED, True, True),
        (EventKind.GATE_CHANGED, False, False),
    ]


async def test_delivery_gives_up_on_stale_events(
    database: async_sessionmaker[AsyncSession],
) -> None:
    """A push about a gate that changed yesterday is noise, and a calendar a day behind
    is corrected by the next event; neither is worth retrying forever."""
    now = datetime.now(UTC)
    await tracked(
        database,
        FlightEvent(
            kind=EventKind.GATE_CHANGED, occurred_at=now - NOTIFY_WINDOW - timedelta(minutes=1)
        ),
        FlightEvent(
            kind=EventKind.LANDED, occurred_at=now - CALENDAR_WINDOW - timedelta(minutes=1)
        ),
    )

    notifier, calendar = FakeNotifier(), FakeCalendar()
    await dispatch_pending(notifier, calendar)
    assert notifier.sent == []
    # The gate change is still young enough for the calendar; the landing is too old
    # for either.
    assert len(calendar.upserts) == 1
    assert await pending() == [
        (EventKind.BOOKING_ADDED, True, True),
        (EventKind.GATE_CHANGED, False, True),
        (EventKind.LANDED, False, False),
    ]


def test_a_diversion_names_where_the_flight_is_now_going() -> None:
    change = only(
        diff_snapshots(snapshot(), snapshot(diverted=True, destination_iata="YOW")),
        EventKind.DIVERTED,
    )
    assert (change.old_value, change.new_value) == (None, "YOW")
    first = only(
        diff_snapshots(None, snapshot(diverted=True, destination_iata="YOW")), EventKind.DIVERTED
    )
    assert first.new_value == "YOW"
    # The feed can flag a diversion before it says where to.
    unknown = only(diff_snapshots(snapshot(), snapshot(diverted=True)), EventKind.DIVERTED)
    assert unknown.new_value == "true"
