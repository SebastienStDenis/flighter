"""The pipeline, against a stand-in session: no database, no mailbox, no Anthropic."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from flighter import ingest, notices
from flighter.airports import UnknownAirport
from flighter.config import Settings
from flighter.extract import Extraction, Segment
from flighter.mail import Marked, Message, parse_message
from flighter.models import IngestLog

FIXTURES = Path(__file__).parent / "fixtures"


def message(name: str) -> Message:
    return parse_message((FIXTURES / name).read_bytes(), name)


def extraction(*, confidence: float = 0.99) -> Extraction:
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
        # How many scopes are open on it right now; the pipeline promises zero during a
        # model call.
        self.in_use = 0

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


@pytest.fixture
def one_session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    """One session behind every scope the pipeline opens, so the log survives between them."""
    session = FakeSession()

    @contextlib.asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        session.in_use += 1
        try:
            yield session
        finally:
            session.in_use -= 1

    monkeypatch.setattr(ingest, "session_scope", scope)
    return session


def use_model(monkeypatch: pytest.MonkeyPatch, result: Extraction | None) -> None:
    async def fake(message: Message, **kwargs: Any) -> Extraction | None:
        return result

    monkeypatch.setattr(ingest, "from_model", fake)


# -- the pipeline --------------------------------------------------------------------


async def test_structured_confirmation_becomes_an_active_booking(
    settings: Settings, recorder: Recorder, one_session: FakeSession
) -> None:
    result = await ingest.process_message(message("flight_jsonld.eml"), settings=settings)

    assert result.outcome == "created"
    assert result.settled
    (created,) = recorder.created
    assert created["marketing_carrier"] == "DL"
    assert created["departure_local"] == datetime(2026, 9, 12, 18, 40)
    assert created["source"] == "email"
    assert created["source_message_id"] == "flight_jsonld.eml"

    logged = one_session.log["flight_jsonld.eml"]
    assert logged.outcome == "created"
    assert logged.raw_extraction is not None
    assert logged.raw_extraction["segments"][0]["confirmation_code"] == "K7QX2M"


async def test_multi_segment_itinerary_books_every_leg(
    settings: Settings, recorder: Recorder, one_session: FakeSession
) -> None:
    result = await ingest.process_message(message("flight_package_jsonld.eml"), settings=settings)

    assert result.outcome == "created"
    assert [(c["marketing_number"], c["origin_iata"]) for c in recorder.created] == [
        ("8830", "YUL"),
        ("856", "YYZ"),
    ]
    assert one_session.log["flight_package_jsonld.eml"].outcome == "created"


async def test_marketing_email_never_reaches_an_extractor(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    def explode(_: Message, **kwargs: Any) -> None:
        raise AssertionError("the prefilter should have stopped this")

    monkeypatch.setattr(ingest, "from_model", explode)

    result = await ingest.process_message(message("airline_promo.eml"), settings=settings)

    assert result.outcome == "error"
    assert one_session.log["airline_promo.eml"].raw_extraction is None
    assert recorder.created == []


async def test_the_model_path_runs_when_there_is_no_structured_data(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    use_model(monkeypatch, extraction())

    result = await ingest.process_message(message("flight_plain.eml"), settings=settings)

    assert result.outcome == "created"
    assert recorder.created[0]["marketing_carrier"] == "WS"


async def test_no_transaction_is_open_while_the_model_reads(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """Every transaction takes the write lock, and a model call can take most of a minute."""

    async def slow_model(message: Message, **kwargs: Any) -> Extraction:
        assert one_session.in_use == 0, "a session was held across the model call"
        return extraction()

    monkeypatch.setattr(ingest, "from_model", slow_model)

    result = await ingest.process_message(message("flight_plain.eml"), settings=settings)

    assert result.outcome == "created"
    assert one_session.in_use == 0


async def test_the_departure_zone_comes_from_the_airport_not_the_email(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """Whatever zone the airline printed, the wall clock is read in the origin's own zone."""
    use_model(monkeypatch, extraction())

    await ingest.process_message(message("flight_plain.eml"), settings=settings)

    (created,) = recorder.created
    assert created["departure_local"] == datetime(2026, 11, 17, 6, 30)
    assert created["departure_local"].tzinfo is None
    # The only zone anybody asked about came from the airports table.
    assert recorder.zones_asked == ["YYC"]


async def test_a_shaky_extraction_is_booked_like_any_other(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """There is no review step: what was read goes on the board, and a flight that is
    not yours is stopped from its own page. The confidence is kept, for the log."""
    use_model(monkeypatch, extraction(confidence=0.4))

    result = await ingest.process_message(message("flight_plain.eml"), settings=settings)

    assert result.outcome == "created"
    assert "status" not in recorder.created[0]
    assert recorder.created[0]["extraction_confidence"] == pytest.approx(0.4)


async def test_a_flight_we_already_have_is_not_booked_twice(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, one_session: FakeSession
) -> None:
    rec = Recorder(duplicate=True)
    monkeypatch.setattr(ingest, "airport_tz", rec.airport_tz)
    monkeypatch.setattr(ingest, "find_duplicate", rec.find_duplicate)
    monkeypatch.setattr(ingest, "create_booking", rec.create_booking)

    result = await ingest.process_message(message("flight_jsonld.eml"), settings=settings)

    assert result.outcome == "duplicate"
    assert rec.created == []


async def test_a_failing_extraction_is_logged_and_swallowed(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    async def boom(message: Message, **kwargs: Any) -> Extraction:
        raise RuntimeError("model output did not match the extraction schema")

    monkeypatch.setattr(ingest, "from_model", boom)

    result = await ingest.process_message(message("flight_plain.eml"), settings=settings)

    assert result.outcome == "error"
    assert not result.settled
    logged = one_session.log["flight_plain.eml"]
    # The exception's words and not its class: this is what a push and the Problems page
    # show, and "RuntimeError:" in front of it helps nobody standing in a terminal.
    assert logged.error == "model output did not match the extraction schema"
    assert recorder.created == []


async def test_a_failing_booking_is_rolled_back_and_logged(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """Half an itinerary is worse than none, and the log row still has to be written."""

    async def refuse(session: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ingest, "create_booking", refuse)

    result = await ingest.process_message(message("flight_package_jsonld.eml"), settings=settings)

    assert result.outcome == "error"
    assert one_session.rolled_back
    logged = one_session.log["flight_package_jsonld.eml"]
    assert logged.error is not None and "database is locked" in logged.error
    assert logged.raw_extraction is not None


async def test_an_airport_we_do_not_know_is_never_retried(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """Reading the same email again reads the same code, so the retries would buy nothing."""

    async def unknown(session: Any, iata: str) -> str:
        raise UnknownAirport(iata)

    monkeypatch.setattr(ingest, "airport_tz", unknown)

    result = await ingest.process_message(message("flight_jsonld.eml"), settings=settings)

    assert result.outcome == "error"
    assert result.settled
    logged = one_session.log["flight_jsonld.eml"]
    assert ingest.set_aside(logged)
    assert logged.error is not None and "JFK is not an airport" in logged.error
    assert recorder.created == []


async def test_an_extraction_that_is_not_a_confirmation_is_set_aside(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    use_model(
        monkeypatch,
        Extraction(is_flight_confirmation=False, confidence=0.1, segments=[]),
    )

    result = await ingest.process_message(message("flight_plain.eml"), settings=settings)

    assert result.outcome == "error"
    # Set aside rather than queued for a retry: the same read gets the same answer.
    assert ingest.set_aside(one_session.log["flight_plain.eml"])
    # The raw answer is still kept: it is the evidence for why nothing was booked.
    assert one_session.log["flight_plain.eml"].raw_extraction is not None
    assert recorder.created == []


async def test_a_confirmation_the_model_could_not_read_is_not_called_no_flight(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """The person was right to flag it, and the push must not tell them otherwise."""
    use_model(
        monkeypatch,
        Extraction(is_flight_confirmation=True, confidence=0.3, segments=[]),
    )

    result = await ingest.process_message(message("flight_plain.eml"), settings=settings)

    assert result.outcome == "error"
    logged = one_session.log["flight_plain.eml"]
    assert ingest.set_aside(logged)
    assert logged.error == notices.UNREADABLE
    assert recorder.created == []


# -- the sweep -----------------------------------------------------------------------


class FakeMailbox:
    """Flagged messages across the account, and a record of which flags were cleared.

    Nothing here moves: an unflagged message is still in the mailbox it was found in, so
    `cleared` names both, and a message left flagged comes back on the next sweep.
    """

    def __init__(self, *messages: Message, mailbox: str = "INBOX") -> None:
        self.marked = [
            Marked(mailbox, index + 1, message) for index, message in enumerate(messages)
        ]
        self.cleared: list[tuple[str, int]] = []

    async def poll(self) -> AsyncIterator[list[Marked]]:
        yield list(self.marked)

    async def clear_mark(self, marked: Marked) -> None:
        self.cleared.append((marked.mailbox, marked.uid))
        self.marked = [item for item in self.marked if item.uid != marked.uid]


class FakeNotifier:
    """Every push the sweep asked for, in order."""

    def __init__(self) -> None:
        self.imported: list[tuple[str, list[Any]]] = []
        self.failed: list[tuple[str, str, str]] = []

    async def mail_imported(self, bookings: Any, *, outcome: str) -> None:
        self.imported.append((outcome, list(bookings)))

    async def mail_failed(self, *, message_id: str, subject: str, reason: str) -> None:
        self.failed.append((message_id, subject, reason))


async def sweep(mailbox: FakeMailbox, notifier: FakeNotifier, settings: Settings) -> list[str]:
    return await ingest.ingest_once(mailbox, settings, notifier)  # type: ignore[arg-type]


async def test_a_marked_message_is_imported_and_unmarked(
    settings: Settings, recorder: Recorder, one_session: FakeSession
) -> None:
    mailbox = FakeMailbox(message("flight_jsonld.eml"))
    notifier = FakeNotifier()

    assert await sweep(mailbox, notifier, settings) == ["created"]
    assert mailbox.cleared == [("INBOX", 1)]
    (outcome, bookings) = notifier.imported[0]
    assert (outcome, [booking.id for booking in bookings]) == ("created", [1])
    assert notifier.failed == []


async def test_a_message_is_unflagged_where_it_was_found(
    settings: Settings, recorder: Recorder, one_session: FakeSession
) -> None:
    """Nothing is filed anywhere: the email is left in whichever mailbox it already sat in."""
    mailbox = FakeMailbox(message("flight_jsonld.eml"), mailbox="Travel")
    notifier = FakeNotifier()

    assert await sweep(mailbox, notifier, settings) == ["created"]
    assert mailbox.cleared == [("Travel", 1)]


async def test_the_same_message_delivered_twice_is_booked_once(
    settings: Settings, recorder: Recorder, one_session: FakeSession
) -> None:
    """A crash between writing the row and clearing the flag replays the message, safely."""
    notifier = FakeNotifier()

    assert await sweep(FakeMailbox(message("flight_jsonld.eml")), notifier, settings) == ["created"]
    again = FakeMailbox(message("flight_jsonld.eml"))
    assert await sweep(again, notifier, settings) == ["created"]

    assert len(recorder.created) == 1
    assert again.cleared == [("INBOX", 1)]
    assert len(notifier.imported) == 1


def fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(message: Message, **kwargs: Any) -> Extraction:
        raise RuntimeError("the model timed out")

    monkeypatch.setattr(ingest, "from_model", boom)


def due_now(session: FakeSession, name: str = "flight_plain.eml") -> None:
    """Bring the next attempt forward, the way waiting out the delay would."""
    session.log[name].retry_at = datetime.now(UTC)


async def sweep_until_set_aside(
    mailbox: FakeMailbox, notifier: FakeNotifier, settings: Settings, session: FakeSession
) -> None:
    """Every attempt a failing message gets, with the waits between them skipped."""
    for attempt in range(len(ingest.RETRY_DELAYS) + 1):
        if attempt:
            due_now(session)
        await sweep(mailbox, notifier, settings)


async def test_a_failure_keeps_its_mark(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """The mark is the retry queue, so it must survive exactly the case that needs retrying."""
    fails(monkeypatch)
    mailbox = FakeMailbox(message("flight_plain.eml"))

    assert await sweep(mailbox, FakeNotifier(), settings) == ["error"]
    assert mailbox.cleared == []
    logged = one_session.log["flight_plain.eml"]
    assert logged.attempts == 1
    assert logged.retry_at is not None


async def test_a_failure_that_will_be_tried_again_is_not_pushed(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """A model that timed out once is usually a model that answers the next time."""
    fails(monkeypatch)
    mailbox, notifier = FakeMailbox(message("flight_plain.eml")), FakeNotifier()

    for _ in ingest.RETRY_DELAYS:
        await sweep(mailbox, notifier, settings)
        assert notifier.failed == []
        due_now(one_session)


async def test_a_message_waiting_for_its_retry_is_left_alone(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """Retrying every sweep would put the same email through the model every few minutes."""
    fails(monkeypatch)
    mailbox, notifier = FakeMailbox(message("flight_plain.eml")), FakeNotifier()
    await sweep(mailbox, notifier, settings)

    def explode(_: Message, **kwargs: Any) -> None:
        raise AssertionError("a message that is not due should not be looked at")

    monkeypatch.setattr(ingest, "from_model", explode)
    assert await sweep(mailbox, notifier, settings) == []
    assert one_session.log["flight_plain.eml"].attempts == 1


async def test_a_message_that_keeps_failing_is_set_aside_and_reported_once(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """Every sweep sees the same broken email; a push every sweep is worse than the bug."""
    fails(monkeypatch)
    mailbox, notifier = FakeMailbox(message("flight_plain.eml")), FakeNotifier()

    await sweep_until_set_aside(mailbox, notifier, settings, one_session)

    logged = one_session.log["flight_plain.eml"]
    assert ingest.set_aside(logged)
    assert logged.attempts == len(ingest.RETRY_DELAYS) + 1
    assert [subject for _, subject, _ in notifier.failed] == ["Your booking is confirmed - WS 1502"]
    assert "the model timed out" in notifier.failed[0][2]
    # And the flag is still on, so the email is where the person left it.
    assert mailbox.cleared == []


async def test_three_failed_extractions_make_exactly_one_push(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """Not one per attempt, and not one per sweep that finds the email still flagged."""
    fails(monkeypatch)
    mailbox, notifier = FakeMailbox(message("flight_plain.eml")), FakeNotifier()

    await sweep_until_set_aside(mailbox, notifier, settings, one_session)
    for _ in range(3):
        await sweep(mailbox, notifier, settings)

    assert len(notifier.failed) == 1
    assert notifier.imported == []


async def test_a_message_that_was_set_aside_is_not_looked_at_again(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    fails(monkeypatch)
    mailbox, notifier = FakeMailbox(message("flight_plain.eml")), FakeNotifier()
    await sweep_until_set_aside(mailbox, notifier, settings, one_session)

    use_model(monkeypatch, extraction())
    assert await sweep(mailbox, notifier, settings) == []
    assert recorder.created == []


async def test_asking_for_a_set_aside_message_puts_it_back_in_the_queue(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    fails(monkeypatch)
    mailbox, notifier = FakeMailbox(message("flight_plain.eml")), FakeNotifier()
    await sweep_until_set_aside(mailbox, notifier, settings, one_session)

    assert await ingest.retry(one_session, "flight_plain.eml") is not None  # type: ignore[arg-type]
    use_model(monkeypatch, extraction())

    assert await sweep(mailbox, notifier, settings) == ["created"]
    assert mailbox.cleared == [("INBOX", 1)]


async def test_a_retry_that_works_is_imported_and_reported(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    fails(monkeypatch)
    mailbox = FakeMailbox(message("flight_plain.eml"))
    notifier = FakeNotifier()
    await sweep(mailbox, notifier, settings)
    due_now(one_session)

    use_model(monkeypatch, extraction())
    assert await sweep(mailbox, notifier, settings) == ["created"]

    assert mailbox.cleared == [("INBOX", 1)]
    assert [outcome for outcome, _ in notifier.imported] == ["created"]
    logged = one_session.log["flight_plain.eml"]
    assert (logged.error, logged.attempts, logged.retry_at) == (None, 0, None)


async def test_a_marked_email_with_no_flight_is_reported_and_left_flagged(
    settings: Settings, recorder: Recorder, one_session: FakeSession
) -> None:
    """Asking again would get the same answer, but the flag is the person's to take off."""
    mailbox = FakeMailbox(message("airline_promo.eml"))
    notifier = FakeNotifier()

    assert await sweep(mailbox, notifier, settings) == ["error"]
    # Set aside on the first pass, so the second one does not read it again.
    assert await sweep(mailbox, notifier, settings) == []

    assert mailbox.cleared == []
    (message_id, _, reason) = notifier.failed[0]
    assert len(notifier.failed) == 1
    assert message_id == "airline_promo.eml"
    assert reason == notices.NO_FLIGHT
    assert notifier.imported == []


async def test_a_message_that_will_not_unmark_is_not_reported_twice(
    settings: Settings, recorder: Recorder, one_session: FakeSession
) -> None:
    """A mark that cannot be cleared must not turn one import into a push every sweep."""

    class Stuck(FakeMailbox):
        async def clear_mark(self, marked: Marked) -> None:
            raise RuntimeError("the flag will not come off")

    mailbox = Stuck(message("flight_jsonld.eml"))
    notifier = FakeNotifier()

    for _ in range(3):
        with contextlib.suppress(RuntimeError):
            await sweep(mailbox, notifier, settings)

    assert len(notifier.imported) == 1


async def test_a_push_that_fails_does_not_hold_the_email(
    settings: Settings,
    recorder: Recorder,
    one_session: FakeSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The row is committed and the flight is on the board; the flag comes off regardless."""

    class Unreachable(FakeNotifier):
        async def mail_imported(self, bookings: Any, *, outcome: str) -> None:
            raise RuntimeError("Pushover is down")

    mailbox = FakeMailbox(message("flight_jsonld.eml"))

    with caplog.at_level(logging.WARNING, logger="flighter.ingest"):
        assert await sweep(mailbox, Unreachable(), settings) == ["created"]

    assert mailbox.cleared == [("INBOX", 1)]
    assert one_session.log["flight_jsonld.eml"].outcome == "created"
    # A warning rather than an error, and it names the email a person would recognise.
    (record,) = [entry for entry in caplog.records if entry.name == "flighter.ingest"]
    assert record.levelno == logging.WARNING
    assert "Your trip is confirmed" in record.getMessage()


async def test_a_flagged_email_naming_an_unknown_airport_is_set_aside_at_once(
    settings: Settings,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    one_session: FakeSession,
) -> None:
    """One push naming the code, and no second look at an email that cannot come right."""

    async def unknown(session: Any, iata: str) -> str:
        raise UnknownAirport(iata)

    monkeypatch.setattr(ingest, "airport_tz", unknown)
    mailbox, notifier = FakeMailbox(message("flight_jsonld.eml")), FakeNotifier()

    assert await sweep(mailbox, notifier, settings) == ["error"]
    assert await sweep(mailbox, notifier, settings) == []

    (_, _, reason) = notifier.failed[0]
    assert len(notifier.failed) == 1
    assert reason == "JFK is not an airport we know."
    # The flag is still on, so the email is where the person left it.
    assert mailbox.cleared == []
