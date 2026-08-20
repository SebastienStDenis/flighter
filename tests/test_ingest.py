"""The pipeline, against a stand-in session: no database, no mailbox, no Anthropic."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from flighter import ingest
from flighter.config import Settings
from flighter.extract import Extraction, Segment
from flighter.mail import Marked, Message, parse_message
from flighter.models import IngestLog

FIXTURES = Path(__file__).parent / "fixtures"


def message(name: str) -> Message:
    return parse_message((FIXTURES / name).read_bytes(), name)


def extraction(*, confidence: float = 0.99, tz_hint: str | None = None) -> Extraction:
    return Extraction(
        is_flight_confirmation=True,
        confidence=confidence,
        segments=[
            Segment(
                marketing_carrier="WS",
                marketing_number="1502",
                operating_carrier=None,
                operating_number=None,
                origin_iata="YYC",
                dest_iata="YVR",
                departure_local="2026-11-17T06:30:00",
                departure_tz_hint=tz_hint,
                arrival_local="2026-11-17T07:12:00",
                confirmation_code="8HTGRX",
                seat="12A",
            )
        ],
    )


class FakeSession:
    """Enough AsyncSession to run the pipeline: the ingest log and the bookings it made."""

    def __init__(self) -> None:
        self.log: dict[str, IngestLog] = {}
        self.bookings: dict[int, Any] = {}
        self.rolled_back = False

    async def get(self, model: type, pk: Any) -> Any:
        return self.log.get(pk) if model is IngestLog else self.bookings.get(pk)

    def add(self, row: Any) -> None:
        assert isinstance(row, IngestLog)
        self.log[row.message_id] = row

    async def flush(self) -> None:
        return None

    async def rollback(self) -> None:
        self.rolled_back = True


class Recorder:
    """Captures what the pipeline asked bookings.py and airports.py to do."""

    def __init__(self, duplicate: bool = False) -> None:
        self.created: list[dict[str, Any]] = []
        self.zones_asked: list[str] = []
        self.duplicate = duplicate

    async def airport_tz(self, session: Any, iata: str) -> str:
        self.zones_asked.append(iata)
        return {"YYC": "America/Edmonton", "JFK": "America/New_York"}.get(iata, "UTC")

    async def find_duplicate(self, session: Any, *args: Any) -> Any:
        return _Booking(99) if self.duplicate else None

    async def create_booking(self, session: Any, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        booking = _Booking(len(self.created))
        session.bookings[booking.id] = booking
        return booking


class _Booking:
    def __init__(self, booking_id: int) -> None:
        self.id = booking_id


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(ingest, "airport_tz", rec.airport_tz)
    monkeypatch.setattr(ingest, "find_duplicate", rec.find_duplicate)
    monkeypatch.setattr(ingest, "create_booking", rec.create_booking)
    return rec


def use_model(monkeypatch: pytest.MonkeyPatch, result: Extraction | None) -> None:
    async def fake(message: Message, **kwargs: Any) -> Extraction | None:
        return result

    monkeypatch.setattr(ingest, "from_model", fake)


# -- the pipeline --------------------------------------------------------------------


async def test_structured_confirmation_becomes_an_active_booking(
    settings: Settings, recorder: Recorder
) -> None:
    session = FakeSession()
    result = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_jsonld.eml"),
        settings=settings,
    )

    assert result.outcome == "created"
    (created,) = recorder.created
    assert created["status"] == "active"
    assert created["marketing_carrier"] == "DL"
    assert created["departure_local"] == datetime(2026, 9, 12, 18, 40)
    assert created["source"] == "email"
    assert created["source_message_id"] == "flight_jsonld.eml"

    logged = session.log["flight_jsonld.eml"]
    assert logged.outcome == "created"
    assert logged.raw_extraction is not None
    assert logged.raw_extraction["segments"][0]["confirmation_code"] == "K7QX2M"


async def test_multi_segment_itinerary_books_every_leg(
    settings: Settings, recorder: Recorder
) -> None:
    session = FakeSession()
    result = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_package_jsonld.eml"),
        settings=settings,
    )

    assert result.outcome == "created"
    assert [(c["marketing_number"], c["origin_iata"]) for c in recorder.created] == [
        ("8830", "YUL"),
        ("856", "YYZ"),
    ]
    assert session.log["flight_package_jsonld.eml"].outcome == "created"


async def test_marketing_email_never_reaches_an_extractor(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_: Message, **kwargs: Any) -> None:
        raise AssertionError("the prefilter should have stopped this")

    monkeypatch.setattr(ingest, "from_model", explode)
    session = FakeSession()

    result = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("airline_promo.eml"),
        settings=settings,
    )

    assert result.outcome == "no_flight"
    assert session.log["airline_promo.eml"].raw_extraction is None
    assert recorder.created == []


async def test_the_model_path_runs_when_there_is_no_structured_data(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(monkeypatch, extraction())
    session = FakeSession()

    result = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    assert result.outcome == "created"
    assert recorder.created[0]["marketing_carrier"] == "WS"


async def test_the_stated_timezone_hint_is_discarded(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The airline said Asia/Tokyo for a flight out of Calgary. It is not consulted."""
    use_model(monkeypatch, extraction(tz_hint="Asia/Tokyo"))
    session = FakeSession()

    await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    (created,) = recorder.created
    assert created["departure_local"] == datetime(2026, 11, 17, 6, 30)
    assert created["departure_local"].tzinfo is None
    assert "departure_tz_hint" not in created
    # The only zone anybody asked about came from the airports table.
    assert recorder.zones_asked == ["YYC"]


async def test_low_confidence_goes_to_review(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(monkeypatch, extraction(confidence=0.4))
    session = FakeSession()

    result = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    assert result.outcome == "review"
    assert recorder.created[0]["status"] == "pending_review"
    assert recorder.created[0]["extraction_confidence"] == pytest.approx(0.4)


async def test_a_flight_we_already_have_is_not_booked_twice(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(duplicate=True)
    monkeypatch.setattr(ingest, "airport_tz", rec.airport_tz)
    monkeypatch.setattr(ingest, "find_duplicate", rec.find_duplicate)
    monkeypatch.setattr(ingest, "create_booking", rec.create_booking)
    session = FakeSession()

    result = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_jsonld.eml"),
        settings=settings,
    )

    assert result.outcome == "duplicate"
    assert rec.created == []


async def test_the_same_message_delivered_twice_is_a_no_op(
    settings: Settings, recorder: Recorder
) -> None:
    session = FakeSession()
    first = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_jsonld.eml"),
        settings=settings,
    )
    second = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_jsonld.eml"),
        settings=settings,
    )

    assert (first.outcome, second.outcome) == ("created", "created")
    assert len(recorder.created) == 1


async def test_a_failing_extraction_is_logged_and_swallowed(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(message: Message, **kwargs: Any) -> Extraction:
        raise RuntimeError("model output did not match the extraction schema")

    monkeypatch.setattr(ingest, "from_model", boom)
    session = FakeSession()

    result = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    assert result.outcome == "error"
    logged = session.log["flight_plain.eml"]
    assert logged.error is not None and "RuntimeError" in logged.error
    assert session.rolled_back


async def test_an_extraction_that_is_not_a_confirmation_is_no_flight(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(
        monkeypatch,
        Extraction(is_flight_confirmation=False, confidence=0.1, segments=[]),
    )
    session = FakeSession()

    result = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    assert result.outcome == "no_flight"
    # The raw answer is still kept: it is the evidence for why nothing was booked.
    assert session.log["flight_plain.eml"].raw_extraction is not None
    assert recorder.created == []


# -- the sweep -----------------------------------------------------------------------


class FakeMailbox:
    """A folder holding marked messages, and a record of which marks were cleared."""

    def __init__(self, *messages: Message) -> None:
        self.marked = [Marked(index + 1, message) for index, message in enumerate(messages)]
        self.cleared: list[int] = []

    async def poll(self) -> list[Marked]:
        return list(self.marked)

    async def clear_mark(self, uid: int) -> None:
        self.cleared.append(uid)
        self.marked = [item for item in self.marked if item.uid != uid]


class FakeNotifier:
    """Every push the sweep asked for, in order."""

    def __init__(self) -> None:
        self.imported: list[tuple[str, list[Any]]] = []
        self.failed: list[tuple[str, str, str]] = []

    async def mail_imported(self, bookings: Any, *, outcome: str) -> None:
        self.imported.append((outcome, list(bookings)))

    async def mail_failed(self, *, message_id: str, subject: str, reason: str) -> None:
        self.failed.append((message_id, subject, reason))


@pytest.fixture
def one_session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    """One session behind every scope the sweep opens, so the log survives between passes."""
    session = FakeSession()

    @contextlib.asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        yield session

    monkeypatch.setattr(ingest, "session_scope", scope)
    return session


async def sweep(mailbox: FakeMailbox, notifier: FakeNotifier, settings: Settings) -> list[str]:
    return await ingest.ingest_once(mailbox, settings, notifier)  # type: ignore[arg-type]


async def test_a_marked_message_is_imported_and_unmarked(
    settings: Settings, recorder: Recorder, one_session: FakeSession
) -> None:
    mailbox = FakeMailbox(message("flight_jsonld.eml"))
    notifier = FakeNotifier()

    assert await sweep(mailbox, notifier, settings) == ["created"]
    assert mailbox.cleared == [1]
    (outcome, bookings) = notifier.imported[0]
    assert (outcome, [booking.id for booking in bookings]) == ("created", [1])
    assert notifier.failed == []


async def test_a_failure_keeps_its_mark(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """The mark is the retry queue, so it must survive exactly the case that needs retrying."""

    async def boom(message: Message, **kwargs: Any) -> Extraction:
        raise RuntimeError("the model timed out")

    monkeypatch.setattr(ingest, "from_model", boom)
    mailbox = FakeMailbox(message("flight_plain.eml"))
    notifier = FakeNotifier()

    assert await sweep(mailbox, notifier, settings) == ["error"]
    assert mailbox.cleared == []
    assert [subject for _, subject, _ in notifier.failed] == ["Your booking is confirmed - WS 1502"]
    assert "the model timed out" in notifier.failed[0][2]


async def test_the_same_failure_is_only_reported_once(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """Every sweep sees the same broken email; a push every sweep is worse than the bug."""

    async def boom(message: Message, **kwargs: Any) -> Extraction:
        raise RuntimeError("the model timed out")

    monkeypatch.setattr(ingest, "from_model", boom)
    mailbox = FakeMailbox(message("flight_plain.eml"))
    notifier = FakeNotifier()

    await sweep(mailbox, notifier, settings)
    await sweep(mailbox, notifier, settings)
    await sweep(mailbox, notifier, settings)

    assert len(notifier.failed) == 1
    assert mailbox.cleared == []


async def test_a_retry_that_works_is_imported_and_reported(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    async def boom(message: Message, **kwargs: Any) -> Extraction:
        raise RuntimeError("the model timed out")

    monkeypatch.setattr(ingest, "from_model", boom)
    mailbox = FakeMailbox(message("flight_plain.eml"))
    notifier = FakeNotifier()
    await sweep(mailbox, notifier, settings)

    use_model(monkeypatch, extraction())
    assert await sweep(mailbox, notifier, settings) == ["created"]

    assert mailbox.cleared == [1]
    assert [outcome for outcome, _ in notifier.imported] == ["created"]
    assert one_session.log["flight_plain.eml"].error is None


async def test_a_marked_email_with_no_flight_is_reported_and_unmarked(
    settings: Settings, recorder: Recorder, one_session: FakeSession
) -> None:
    """Retrying it forever would never find a flight, so it is answered and let go."""
    mailbox = FakeMailbox(message("airline_promo.eml"))
    notifier = FakeNotifier()

    assert await sweep(mailbox, notifier, settings) == ["no_flight"]
    assert mailbox.cleared == [1]
    (message_id, _, reason) = notifier.failed[0]
    assert message_id == "airline_promo.eml"
    assert "no flight" in reason
    assert notifier.imported == []


async def test_a_message_that_will_not_unmark_is_not_reported_twice(
    settings: Settings, recorder: Recorder, one_session: FakeSession
) -> None:
    """A mark that cannot be cleared must not turn one import into a push every sweep."""

    class Stuck(FakeMailbox):
        async def clear_mark(self, uid: int) -> None:
            raise RuntimeError("the archive is over quota")

    mailbox = Stuck(message("flight_jsonld.eml"))
    notifier = FakeNotifier()

    for _ in range(3):
        with contextlib.suppress(RuntimeError):
            await sweep(mailbox, notifier, settings)

    assert len(notifier.imported) == 1
