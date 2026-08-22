"""The pipeline: a flagged email in, bookings, a push, and one ingest_log row out.

Every message ends with exactly one ingest_log row, whatever happened to it, because
that row is both the record of what we decided and the thing that stops a re-delivery
being processed twice. One bad email is never allowed to stop the loop.

The flag is the queue, so the row is also the retry state. A message that failed keeps
its flag and is swept again a couple of times, minutes apart; if it still fails it is set
aside, and only a person asking for it on the email page brings it back. An email that
held no flight is set aside at once, because reading it again reads it the same way. A
message that reached the board is unflagged where it stands, and comes back only by being
flagged again: that is the person overruling a decision that it held no flight, or asking
for flights of its own that have since been deleted, and the email is read again; while
what it booked is still on the board the flag simply comes off again. Either way the
phone is told once, when there is nothing left to try, which is why the state already on
file is read before the new one is written.

No transaction is open while the model is reading an email. Every transaction here takes
the database's one write lock the moment it begins, and a model call can take most of a
minute, so a session held across it would hold every page of the web UI for as long.
The row is read in one short transaction and written in another, with nothing locked
between them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import notices
from .airports import UnknownAirport, airport_tz
from .bookings import create_booking, find_duplicate, on_board_from_message
from .config import Settings, get_settings
from .db import session_scope
from .extract import Extraction, Segment, from_jsonld, from_model, looks_like_flight
from .mail import (
    IDLE_CYCLE_SECONDS,
    RECONNECT_MAX_SECONDS,
    RECONNECT_MIN_SECONDS,
    Mailbox,
    Marked,
    Message,
)
from .models import Booking, BookingSource, IngestLog, IngestOutcome
from .notify import Notifier
from .timezones import to_utc

log = logging.getLogger(__name__)

# A message yields one outcome even when it carried several segments. A flight added
# wins over one already there because it is the one the push should name.
_OUTCOME_PRECEDENCE = (IngestOutcome.CREATED, IngestOutcome.DUPLICATE)

# The only outcome that keeps its flag, and so the only one that is ever tried again.
# Everything else reached the board and is finished with. An email that held no flight is
# recorded as one of these too: there is nothing to try again, but the flag stays on so
# that the email does not quietly lose it with nothing to show for it.
ERROR = IngestOutcome.ERROR

# How long to wait before each retry of a message that failed. One failure more than
# there are delays here and the message is set aside: whatever is wrong with it is not
# the kind of wrong that fixes itself, and a sweep that keeps re-running a model against
# the same broken email costs money and says nothing new. The flag stays on so the email
# is still where the person left it, and the email page offers it back.
RETRY_DELAYS = (timedelta(minutes=2), timedelta(minutes=10))

# How far back the email page reads. A deployment that has been running for years has
# a mailbox behind it, and nobody scrolls a list of every confirmation they have ever
# been sent; what is set aside is added to this however old it is.
ACTIVITY_LIMIT = 50

# How often the watcher looks again while there is nothing to sign in with. Seconds
# rather than minutes because no connection is open to cost anybody anything, and because
# somebody who has just typed an Apple ID into the settings page is watching for it.
UNCONFIGURED_PAUSE_SECONDS = 5.0


class Ingested(NamedTuple):
    """What one email came to: the ingest_log outcome, and the flights it points at.

    `settled` is whether the service is finished with it. Every decision is settled; a
    failure is settled only once its last retry has been used up, or at once when it is
    the kind of failure another attempt cannot answer differently.
    """

    outcome: IngestOutcome
    booking_ids: tuple[int, ...] = ()
    error: str | None = None
    settled: bool = True
    retryable: bool = True


class Standing(NamedTuple):
    """What the ingest log already says about a message, and whether its flights are there.

    A copy rather than the row: the transaction it was read in has ended by the time it
    is consulted, and the pipeline is about to write a new row in its place.
    """

    outcome: str
    error: str | None
    retry_at: datetime | None
    on_board: bool

    @property
    def due(self) -> bool:
        """Whether a message that failed before is ready for another go."""
        return self.retry_at is not None and self.retry_at <= datetime.now(UTC)


async def _on_file(message_id: str) -> Standing | None:
    async with session_scope() as session:
        row = await session.get(IngestLog, message_id)
        if row is None:
            return None
        on_board = await on_board_from_message(session, message_id)
        return Standing(row.outcome, row.error, row.retry_at, on_board)


async def process_message(message: Message, *, settings: Settings | None = None) -> Ingested:
    """Handle one email and return what it was recorded as.

    Reads the email with nothing locked, then books and records in one transaction. Does
    not consult the log first: whether the message is wanted at all is the sweep's call.
    """
    settings = settings or get_settings()
    try:
        extraction = await _extract(message, settings)
    except Exception as exc:
        log.exception("failed to extract %s", message.id)
        async with session_scope() as session:
            return await _record(session, message, _failed(exc))

    async with session_scope() as session:
        try:
            if extraction is None or not extraction.is_flight_confirmation:
                return await _record(session, message, _no_flight(), extraction)
            if not extraction.segments:
                return await _record(session, message, _unreadable(), extraction)
            return await _record(
                session, message, await _book(session, message, extraction), extraction
            )
        except UnknownAirport as exc:
            log.warning("%s names an airport we have no row for: %s", message.id, exc.iata)
            await session.rollback()
            return await _record(session, message, _unknown_airport(exc), extraction)
        except Exception as exc:
            log.exception("failed to book %s", message.id)
            # Discard whatever this message had already written: a half-booked itinerary
            # is worse than none, and after a database error the session cannot write
            # the log row at all until it is rolled back.
            await session.rollback()
            return await _record(session, message, _failed(exc), extraction)


async def _extract(message: Message, settings: Settings) -> Extraction | None:
    """Cheapest tier first. None means the email holds no flight as far as we can tell."""
    if not looks_like_flight(message):
        return None
    return from_jsonld(message.text_html) or await from_model(message, settings=settings)


def _failed(exc: Exception) -> Ingested:
    """The exception's own words, which a person reads on a push and on the email page.

    The class name is in the traceback the caller has already logged; on a phone it is
    noise in front of the sentence that matters.
    """
    return Ingested(ERROR, error=str(exc) or type(exc).__name__)


def _no_flight() -> Ingested:
    """An answer, but not one that takes the flag off.

    Reading the email again reads it the same way, so there is nothing to retry. The flag
    stays on all the same: the email is still where the person left it, and the email
    page asks them whether it really held nothing rather than deciding that on its own.
    """
    return Ingested(ERROR, error=notices.NO_FLIGHT, retryable=False)


def _unreadable() -> Ingested:
    """The model agreed it was a flight email and still came back with no segments.

    Set aside at once like `_no_flight`, since the body it was shown is the body it would
    be shown again, but said differently: the person was right to flag this one, and what
    wants looking at is how the email was read, not whether it held a flight.
    """
    return Ingested(ERROR, error=notices.UNREADABLE, retryable=False)


def _unknown_airport(exc: UnknownAirport) -> Ingested:
    """A failure that is decided the moment it happens, rather than swept again.

    A code that is not an airport was mis-read, and reading the same email again reads it
    the same way, so the retries would spend model calls to end up here anyway. The email
    is set aside at once instead, and the push names the code that has to be corrected.
    """
    return Ingested(ERROR, error=notices.unknown_airport(exc.iata), retryable=False)


async def _book(session: AsyncSession, message: Message, extraction: Extraction) -> Ingested:
    booked = [
        await _book_segment(session, message, extraction, segment)
        for segment in extraction.segments
    ]
    outcomes = [outcome for outcome, _ in booked]
    return Ingested(
        next((o for o in _OUTCOME_PRECEDENCE if o in outcomes), IngestOutcome.NO_FLIGHT),
        tuple(booking_id for _, booking_id in booked),
    )


async def _book_segment(
    session: AsyncSession,
    message: Message,
    extraction: Extraction,
    segment: Segment,
) -> tuple[IngestOutcome, int]:
    flight = f"{segment.marketing_carrier}{segment.marketing_number}"
    departure_local = segment.departure_at
    if departure_local is None:
        raise ValueError(f"segment {flight} has no readable departure time")

    # The zone comes from the airports table, which is also what create_booking will use
    # a moment later. Whatever zone the email stated is never consulted: airlines get it
    # wrong often enough that trusting it moves real flights by hours, and the origin
    # airport's IANA zone is always right.
    origin_tz = await airport_tz(session, segment.origin_iata)
    twin = await find_duplicate(
        session,
        segment.marketing_carrier,
        segment.marketing_number,
        to_utc(departure_local, origin_tz),
    )
    if twin is not None:
        log.info("%s is already booked as %d", flight, twin.id)
        return IngestOutcome.DUPLICATE, twin.id

    booking = await create_booking(
        session,
        marketing_carrier=segment.marketing_carrier,
        marketing_number=segment.marketing_number,
        operating_carrier=segment.operating_carrier,
        operating_number=segment.operating_number,
        origin_iata=segment.origin_iata,
        dest_iata=segment.dest_iata,
        departure_local=departure_local,
        arrival_local=segment.arrival_at,
        source=BookingSource.EMAIL,
        source_message_id=message.id,
        confirmation_code=segment.confirmation_code,
        seat=segment.seat,
        extraction_confidence=extraction.confidence,
    )
    log.info("booked %s %s-%s (%d)", flight, segment.origin_iata, segment.dest_iata, booking.id)
    return IngestOutcome.CREATED, booking.id


# -- the log -------------------------------------------------------------------------


async def _record(
    session: AsyncSession,
    message: Message,
    result: Ingested,
    extraction: Extraction | None = None,
) -> Ingested:
    """Write the ingest_log row. Every path through the pipeline ends here.

    A retried message overwrites the row it failed under instead of adding a second one:
    there is one row per email, and it says how that email stands now - including when it
    is due to be tried next, and whether there is any next at all.
    """
    row = await session.get(IngestLog, message.id)
    if row is None:
        row = IngestLog(message_id=message.id)
        session.add(row)
    row.outcome = result.outcome
    row.subject = message.subject
    row.raw_extraction = extraction.model_dump(mode="json") if extraction else None
    row.error = result.error
    failed = result.outcome == ERROR
    row.attempts = (row.attempts or 0) + 1 if failed else 0
    row.retry_at = _next_attempt(row.attempts) if failed and result.retryable else None
    await session.flush()
    return result._replace(settled=row.retry_at is None)


def _next_attempt(attempts: int) -> datetime | None:
    """When a message that has failed this many times may be tried again, if ever."""
    if attempts > len(RETRY_DELAYS):
        return None
    return datetime.now(UTC) + RETRY_DELAYS[attempts - 1]


def set_aside(row: IngestLog) -> bool:
    """Whether this message has been given up on and is waiting to be asked for again."""
    return row.outcome == ERROR and row.retry_at is None


async def list_set_aside(session: AsyncSession) -> list[IngestLog]:
    """Every message the service has stopped trying, newest first."""
    rows = await session.execute(
        select(IngestLog)
        .where(IngestLog.outcome == ERROR, IngestLog.retry_at.is_(None))
        .order_by(IngestLog.processed_at.desc())
    )
    return list(rows.scalars())


async def list_activity(session: AsyncSession, limit: int = ACTIVITY_LIMIT) -> list[IngestLog]:
    """What the service has made of the mailbox lately, newest first.

    Anything set aside is on the list wherever it falls, because the nav counts those
    rows whatever their age and a marked tab over a page that does not show them would
    be a tab that lies. They are older than everything in the recent run by definition,
    so they go on the end and the order still reads newest first.
    """
    rows = await session.execute(
        select(IngestLog).order_by(IngestLog.processed_at.desc()).limit(limit)
    )
    recent = list(rows.scalars())
    seen = {row.message_id for row in recent}
    return recent + [row for row in await list_set_aside(session) if row.message_id not in seen]


async def retry(session: AsyncSession, message_id: str) -> IngestLog | None:
    """Put a set-aside message back at the front of the queue.

    The flag never came off, so the next sweep finds it exactly as it did the first time;
    all this clears is the record of having given up.
    """
    row = await session.get(IngestLog, message_id)
    if row is None or not set_aside(row):
        return None
    row.attempts = 0
    row.retry_at = datetime.now(UTC)
    await session.flush()
    return row


async def dismiss(session: AsyncSession, message_id: str) -> IngestLog | None:
    """Give up on a set-aside message for good, from the page rather than from Mail.

    Recording it as ignored is what makes the next sweep take the flag off without
    reading the email again. Only once the flag is off does the row say no flight: a
    flag found on a no-flight message is the person asking for it to be read again, and
    that has to be told apart from the flag this decision has not yet taken off.
    """
    row = await session.get(IngestLog, message_id)
    if row is None or not set_aside(row):
        return None
    row.outcome = IngestOutcome.IGNORED
    row.error = None
    row.attempts = 0
    row.retry_at = None
    await session.flush()
    return row


async def _settle_ignored(message_id: str) -> None:
    """The flag is off an ignored message, so from here on it is simply one with no flight."""
    async with session_scope() as session:
        row = await session.get(IngestLog, message_id)
        if row is not None and row.outcome == IngestOutcome.IGNORED:
            row.outcome = IngestOutcome.NO_FLIGHT


# -- the loop ------------------------------------------------------------------------


# Set from the email page when a person hands an email back, so the watcher sweeps at
# the end of its current idle rather than at the end of its cycle. Only ever set, read
# and cleared, never awaited, so it belongs to no particular event loop.
_wake = asyncio.Event()


def wake() -> None:
    """Ask the watcher to sweep as soon as it can rather than when its cycle is up."""
    _wake.set()


async def run_ingest_loop(stopping: asyncio.Event, *, settings: Settings | None = None) -> None:
    """Hold one IMAP connection open and import what is flagged on it, until asked to stop.

    A connection that fails is reopened after a wait that doubles each time: iCloud
    allows only a handful of connections per account, and a client that reconnects in a
    tight loop is a client it stops answering.
    """
    settings = settings or get_settings()
    notifier = Notifier(settings)
    backoff = RECONNECT_MIN_SECONDS

    while not stopping.is_set():
        if not settings.icloud_configured:
            log.debug("iCloud is not configured; not watching for flagged mail")
            await _pause(stopping, UNCONFIGURED_PAUSE_SECONDS)
            continue

        mailbox = Mailbox(settings)
        try:
            await mailbox.connect()
            backoff = RECONNECT_MIN_SECONDS
            # The flag colour and the sign-in were both read when the connection opened.
            # Changing either on the settings page drops out of here and connects again on
            # the new one rather than waiting for a restart.
            while not stopping.is_set() and mailbox.current:
                await ingest_once(mailbox, settings, notifier)
                await mailbox.wait_for_mail(IDLE_CYCLE_SECONDS, wake=_wake)
        except Exception:
            log.exception("the mail connection failed; reconnecting in %.0fs", backoff)
            await mailbox.close()
            await _pause(stopping, backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
        else:
            await mailbox.close()


async def ingest_once(mailbox: Mailbox, settings: Settings, notifier: Notifier) -> list[str]:
    """One sweep: import everything that is flagged, and unflag whatever is finished.

    Each message is committed on its own so that a failure part way through a sweep keeps
    everything already imported, and the flag is cleared only after its row is written.
    """
    outcomes = []
    async for marked_in_mailbox in mailbox.poll():
        for marked in marked_in_mailbox:
            outcome = await _import(mailbox, marked, settings, notifier)
            if outcome is not None:
                outcomes.append(outcome)
    if outcomes:
        log.info("imported %d message(s): %s", len(outcomes), _tally(outcomes))
    return outcomes


async def import_flagged(*, settings: Settings | None = None) -> list[str]:
    """One sweep of every mailbox, for the CLI.

    Opens a connection of its own, so running this while the watcher is up costs it a
    connection for no longer than the sweep takes.
    """
    settings = settings or get_settings()
    if not settings.icloud_configured:
        log.warning("iCloud is not configured; there is no mail to sweep")
        return []

    mailbox = Mailbox(settings)
    try:
        await mailbox.connect()
        return await ingest_once(mailbox, settings, Notifier(settings))
    finally:
        await mailbox.close()


async def _import(
    mailbox: Mailbox, marked: Marked, settings: Settings, notifier: Notifier
) -> str | None:
    """One flagged message: through the pipeline, onto the phone, and unflagged.

    Returns None for a message this sweep deliberately left where it was, which is not
    the same as one that was looked at and came to nothing.
    """
    message = marked.message
    before = await _on_file(message.id)

    if before is not None and before.outcome == IngestOutcome.IGNORED:
        # Decided on the email page; taking the flag off is what was left to do.
        log.debug("%s was ignored; taking its flag off", message.id)
        await mailbox.clear_mark(marked)
        await _settle_ignored(message.id)
        return before.outcome
    if before is not None and before.outcome not in (ERROR, IngestOutcome.NO_FLIGHT):
        if before.on_board:
            # On the board from an earlier sweep. A crash between writing that row and
            # clearing the flag is the one way back here, so all that is left is the flag.
            log.debug("%s is already in the ingest log (%s)", message.id, before.outcome)
            await mailbox.clear_mark(marked)
            return before.outcome
        log.info("%s is flagged again with nothing it booked left on the board", message.id)
    if before is not None and before.outcome == ERROR and not before.due:
        log.debug("%s is not due to be tried again yet", message.id)
        return None

    # A message on file as holding no flight lost its flag with that decision, so a flag
    # on it now was put there since, by somebody who disagrees: it is read again. So is
    # one whose every flight has since been deleted: the flag is the person asking for
    # them back, and the dedupe rule skips archived rows, so it books them afresh.
    result = await process_message(message, settings=settings)
    # The phone hears once, and only when there is nothing left for the service to do: a
    # failure that is going to be retried in two minutes is not news. Nothing that was
    # already settled gets this far, so a settled result is always the first word.
    if result.settled:
        await _announce(notifier, message, result)
    if result.outcome != ERROR:
        await mailbox.clear_mark(marked)
    return result.outcome


async def _announce(notifier: Notifier, message: Message, result: Ingested) -> None:
    """Tell the phone what became of a flagged email, whichever way it went.

    A push that cannot be sent is logged and let go. The row is already committed, so
    retrying would mean re-reading an email whose answer is on file, and whatever became
    of it is on the board either way.
    """
    try:
        if result.outcome == ERROR:
            # Nothing settled reaches here with no reason on it: every failure is built by
            # one of the three helpers above, and each of them says what went wrong.
            await notifier.mail_failed(
                message_id=message.id,
                subject=message.subject,
                reason=result.error,
            )
            return

        async with session_scope() as session:
            found = [await session.get(Booking, booking_id) for booking_id in result.booking_ids]
        bookings = [booking for booking in found if booking is not None]
        await notifier.mail_imported(bookings, outcome=result.outcome)
    except Exception:
        log.warning("could not tell the phone what became of %r", message.subject, exc_info=True)


async def _pause(stopping: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stopping.wait(), timeout=seconds)


def _tally(outcomes: list[str]) -> str:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    return ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
