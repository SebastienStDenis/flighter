"""The pipeline: a marked email in, bookings, a push, and one ingest_log row out.

Every message ends with exactly one ingest_log row, whatever happened to it, because
that row is both the record of what we decided and the thing that stops a re-delivery
being processed twice. One bad email is never allowed to stop the loop.

The mark is the queue, so the row is also the retry state. A message that failed keeps
its mark and is swept again a few minutes later; a message that got as far as a decision
has its mark cleared and never comes back. Either way the phone is told once, and only
once, which is why the outcome already on file is read before the new one is written.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from . import prefs
from .airports import airport_tz
from .bookings import create_booking, find_duplicate
from .config import Settings, get_settings
from .db import session_scope
from .extract import Extraction, Segment, from_jsonld, from_model, looks_like_flight
from .mail import RECONNECT_MAX_SECONDS, RECONNECT_MIN_SECONDS, Mailbox, Marked, Message
from .models import Booking, IngestLog
from .notify import Notifier
from .timezones import to_utc

log = logging.getLogger(__name__)

# A message yields one outcome even when it carried several segments. Review wins over
# a success because it is the one that still needs a person.
_OUTCOME_PRECEDENCE = ("review", "created", "duplicate")

# The only outcome that keeps its mark, and so the only one that is ever tried again.
# Everything else has been decided, including an email that turned out to hold no flight:
# leaving that one marked would retry a message whose answer cannot change, every few
# minutes, for as long as the service runs.
ERROR = "error"

# Outcomes the user is told about as a failure rather than as a flight.
_FAILURES = frozenset({ERROR, "no_flight"})

_NO_FLIGHT_REASON = "There was no flight in it, so nothing was added."


class Ingested(NamedTuple):
    """What one email came to: the ingest_log outcome, and the flights it points at."""

    outcome: str
    booking_ids: tuple[int, ...] = ()
    error: str | None = None


async def process_message(
    session: AsyncSession, message: Message, *, settings: Settings | None = None
) -> Ingested:
    """Handle one email and return what it was recorded as."""
    settings = settings or get_settings()

    existing = await session.get(IngestLog, message.id)
    if existing is not None and existing.outcome != ERROR:
        log.debug("%s is already in the ingest log (%s)", message.id, existing.outcome)
        return Ingested(existing.outcome, error=existing.error)

    extraction: Extraction | None = None
    try:
        if not looks_like_flight(message):
            return await _record(session, message, Ingested("no_flight"))

        extraction = from_jsonld(message.text_html) or await from_model(message, settings=settings)
        if extraction is None or not extraction.is_flight_confirmation or not extraction.segments:
            return await _record(session, message, Ingested("no_flight"), extraction)

        return await _record(
            session, message, await _book(session, message, extraction), extraction
        )
    except Exception as exc:
        log.exception("failed to ingest %s", message.id)
        # Discard whatever this message had already written: a half-booked itinerary is
        # worse than none, and after a database error the session cannot write the log
        # row at all until it is rolled back.
        await session.rollback()
        return await _record(
            session, message, Ingested(ERROR, error=f"{type(exc).__name__}: {exc}"), extraction
        )


async def _book(session: AsyncSession, message: Message, extraction: Extraction) -> Ingested:
    confident = extraction.confidence >= prefs.current().extraction_confidence_threshold
    status = "active" if confident else "pending_review"

    booked = [
        await _book_segment(session, message, extraction, segment, status)
        for segment in extraction.segments
    ]
    outcomes = [outcome for outcome, _ in booked]
    return Ingested(
        next((o for o in _OUTCOME_PRECEDENCE if o in outcomes), "no_flight"),
        tuple(booking_id for _, booking_id in booked),
    )


async def _book_segment(
    session: AsyncSession,
    message: Message,
    extraction: Extraction,
    segment: Segment,
    status: str,
) -> tuple[str, int]:
    flight = f"{segment.marketing_carrier}{segment.marketing_number}"
    departure_local = segment.departure_at
    if departure_local is None:
        raise ValueError(f"segment {flight} has no readable departure time")

    # The zone comes from the airports table, which is also what create_booking will use
    # a moment later. departure_tz_hint is deliberately not consulted here or anywhere
    # else: airlines state it wrong often enough that trusting it moves real flights by
    # hours, and the origin airport's IANA zone is always right.
    origin_tz = await airport_tz(session, segment.origin_iata)
    twin = await find_duplicate(
        session,
        segment.marketing_carrier,
        segment.marketing_number,
        to_utc(departure_local, origin_tz),
    )
    if twin is not None:
        log.info("%s is already booked as %d", flight, twin.id)
        return "duplicate", twin.id

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
        source="email",
        source_message_id=message.id,
        confirmation_code=segment.confirmation_code,
        seat=segment.seat,
        status=status,
        extraction_confidence=extraction.confidence,
    )
    log.info(
        "booked %s %s-%s as %s (%d)",
        flight,
        segment.origin_iata,
        segment.dest_iata,
        status,
        booking.id,
    )
    return ("review" if status == "pending_review" else "created"), booking.id


# -- the log -------------------------------------------------------------------------


async def _record(
    session: AsyncSession,
    message: Message,
    result: Ingested,
    extraction: Extraction | None = None,
) -> Ingested:
    """Write the ingest_log row. Every path through the pipeline ends here.

    A retried message overwrites the row it failed under instead of adding a second one:
    there is one row per email, and it says how that email stands now.
    """
    row = await session.get(IngestLog, message.id)
    if row is None:
        row = IngestLog(message_id=message.id)
        session.add(row)
    row.outcome = result.outcome
    row.raw_extraction = extraction.model_dump(mode="json") if extraction else None
    row.error = result.error
    await session.flush()
    return result


# -- the loop ------------------------------------------------------------------------


async def run_ingest_loop(stopping: asyncio.Event, *, settings: Settings | None = None) -> None:
    """Hold one IMAP connection open and import what is marked on it, until asked to stop.

    A connection that fails is reopened after a wait that doubles each time: iCloud
    allows only a handful of connections per account, and a client that reconnects in a
    tight loop is a client it stops answering.
    """
    settings = settings or get_settings()
    notifier = Notifier(settings)
    backoff = RECONNECT_MIN_SECONDS

    while not stopping.is_set():
        if not settings.icloud_configured:
            log.debug("iCloud is not configured; not watching the mailbox")
            await _pause(stopping, prefs.current().imap_idle_seconds)
            continue

        mailbox = Mailbox(settings)
        try:
            await mailbox.connect()
            backoff = RECONNECT_MIN_SECONDS
            # The folder is a preference, and the connection is selected on the one that
            # was live when it opened. Changing it on the settings page drops out of here
            # and reconnects on the new one rather than waiting for a restart.
            while not stopping.is_set() and mailbox.folder == prefs.current().imap_import_folder:
                await ingest_once(mailbox, settings, notifier)
                await mailbox.wait_for_mail(prefs.current().imap_idle_seconds)
        except Exception:
            log.exception("the mail connection failed; reconnecting in %.0fs", backoff)
            await mailbox.close()
            await _pause(stopping, backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
        else:
            await mailbox.close()


async def ingest_once(mailbox: Mailbox, settings: Settings, notifier: Notifier) -> list[str]:
    """One sweep: import everything that is marked, and unmark whatever is finished.

    Each message is committed on its own so that a failure part way through a sweep keeps
    everything already imported, and the mark is cleared only after its row is written.
    """
    outcomes = []
    for marked in await mailbox.poll():
        outcomes.append(await _import(mailbox, marked, settings, notifier))
    if outcomes:
        log.info("imported %d message(s): %s", len(outcomes), _tally(outcomes))
    return outcomes


async def import_marked(*, settings: Settings | None = None) -> list[str]:
    """One sweep of the folder, for the CLI.

    Opens a connection of its own, so running this while the watcher is up costs it a
    connection for no longer than the sweep takes.
    """
    settings = settings or get_settings()
    if not settings.icloud_configured:
        log.warning("iCloud is not configured; there is no mailbox to sweep")
        return []

    mailbox = Mailbox(settings)
    try:
        await mailbox.connect()
        return await ingest_once(mailbox, settings, Notifier(settings))
    finally:
        await mailbox.close()


async def _import(mailbox: Mailbox, marked: Marked, settings: Settings, notifier: Notifier) -> str:
    """One marked message: through the pipeline, onto the phone, and out of the folder."""
    message = marked.message
    async with session_scope() as session:
        # Read as a string rather than kept as a row: the pipeline is about to rewrite
        # that very row, and the identity map would hand back the new outcome.
        logged = await session.get(IngestLog, message.id)
        reported = logged.outcome if logged is not None else None
        result = await process_message(session, message, settings=settings)
        # A message that fails keeps its mark and is swept again in a few minutes, so
        # repeating the push would mean a notification every cycle for as long as the
        # email stays broken. Only a decision that differs from the one already on file
        # is news, which also covers the case where the mark itself would not clear.
        if reported != result.outcome:
            await _announce(session, notifier, message, result)

    if result.outcome != ERROR:
        await mailbox.clear_mark(marked.uid)
    return result.outcome


async def _announce(
    session: AsyncSession, notifier: Notifier, message: Message, result: Ingested
) -> None:
    """Tell the phone what became of a marked email, whichever way it went."""
    if result.outcome in _FAILURES:
        await notifier.mail_failed(
            message_id=message.id,
            subject=message.subject,
            reason=result.error or _NO_FLIGHT_REASON,
        )
        return

    bookings = [await session.get(Booking, booking_id) for booking_id in result.booking_ids]
    await notifier.mail_imported(
        [booking for booking in bookings if booking is not None], outcome=result.outcome
    )


async def _pause(stopping: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stopping.wait(), timeout=seconds)


def _tally(outcomes: list[str]) -> str:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    return ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
