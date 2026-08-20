"""The pipeline: an email in, bookings and one ingest_log row out.

Every message ends with exactly one ingest_log row, whatever happened to it, because
that row is both the record of what we decided and the thing that stops a re-delivery
being processed twice. One bad email is never allowed to stop the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from . import prefs
from .airports import airport_tz
from .bookings import create_booking, find_duplicate
from .config import Settings, get_settings
from .db import session_scope
from .extract import Extraction, Segment, from_jsonld, from_model, looks_like_flight
from .mail import RECONNECT_MAX_SECONDS, RECONNECT_MIN_SECONDS, Mailbox, Message
from .models import IngestLog
from .timezones import to_utc

log = logging.getLogger(__name__)

# A message yields one outcome even when it carried several segments. Review wins over
# a success because it is the one that still needs a person.
_OUTCOME_PRECEDENCE = ("review", "created", "duplicate")


async def process_message(
    session: AsyncSession, message: Message, *, settings: Settings | None = None
) -> str:
    """Handle one email and return the ingest_log outcome it was recorded under."""
    settings = settings or get_settings()

    existing = await session.get(IngestLog, message.id)
    if existing is not None:
        log.debug("%s is already in the ingest log (%s)", message.id, existing.outcome)
        return existing.outcome

    extraction: Extraction | None = None
    try:
        if not looks_like_flight(message):
            return await _record(session, message, "no_flight")

        extraction = from_jsonld(message.text_html) or await from_model(message, settings=settings)
        if extraction is None or not extraction.is_flight_confirmation or not extraction.segments:
            return await _record(session, message, "no_flight", extraction)

        outcome = await _book(session, message, extraction, settings)
        return await _record(session, message, outcome, extraction)
    except Exception as exc:
        log.exception("failed to ingest %s", message.id)
        # Discard whatever this message had already written: a half-booked itinerary is
        # worse than none, and after a database error the session cannot write the log
        # row at all until it is rolled back.
        await session.rollback()
        return await _record(
            session, message, "error", extraction, error=f"{type(exc).__name__}: {exc}"
        )


async def _book(
    session: AsyncSession, message: Message, extraction: Extraction, settings: Settings
) -> str:
    confident = extraction.confidence >= prefs.current().extraction_confidence_threshold
    status = "active" if confident else "pending_review"

    outcomes = [
        await _book_segment(session, message, extraction, segment, status)
        for segment in extraction.segments
    ]
    return next((o for o in _OUTCOME_PRECEDENCE if o in outcomes), "no_flight")


async def _book_segment(
    session: AsyncSession,
    message: Message,
    extraction: Extraction,
    segment: Segment,
    status: str,
) -> str:
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
        return "duplicate"

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
    return "review" if status == "pending_review" else "created"


# -- the log -------------------------------------------------------------------------


async def _record(
    session: AsyncSession,
    message: Message,
    outcome: str,
    extraction: Extraction | None = None,
    *,
    error: str | None = None,
) -> str:
    """Write the ingest_log row. Every path through the pipeline ends here."""
    session.add(
        IngestLog(
            message_id=message.id,
            outcome=outcome,
            raw_extraction=extraction.model_dump(mode="json") if extraction else None,
            error=error,
        )
    )
    await session.flush()
    return outcome


# -- the loop ------------------------------------------------------------------------


async def run_ingest_loop(stopping: asyncio.Event, *, settings: Settings | None = None) -> None:
    """Hold one IMAP connection open and ingest what arrives on it, until asked to stop.

    A connection that fails is reopened after a wait that doubles each time: iCloud
    allows only a handful of connections per account, and a client that reconnects in a
    tight loop is a client it stops answering.
    """
    settings = settings or get_settings()
    backoff = RECONNECT_MIN_SECONDS

    while not stopping.is_set():
        if not settings.mail_configured:
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
            while not stopping.is_set() and mailbox.folder == prefs.current().imap_folder:
                await ingest_once(mailbox, settings)
                await mailbox.wait_for_mail(prefs.current().imap_idle_seconds)
        except Exception:
            log.exception("the mail connection failed; reconnecting in %.0fs", backoff)
            await mailbox.close()
            await _pause(stopping, backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
        else:
            await mailbox.close()


async def ingest_once(mailbox: Mailbox, settings: Settings) -> list[str]:
    """One pass: fetch, process, then advance the cursor. Returns the outcomes.

    Each message is committed on its own so that a failure part way through a batch
    keeps everything already ingested, and the cursor only advances once the whole batch
    is in the log.
    """
    async with session_scope() as session:
        messages = await mailbox.poll(session)

    outcomes = await _process(messages, settings)
    async with session_scope() as session:
        await mailbox.commit_cursor(session)
    return outcomes


async def backfill(days: int = 30, *, settings: Settings | None = None) -> list[str]:
    """A one-off sweep over recent mail, for the CLI.

    Opens a connection of its own and leaves the cursor where it was, so running this
    while the watcher is up neither moves its position nor costs it its connection for
    longer than the sweep takes.
    """
    settings = settings or get_settings()
    if not settings.mail_configured:
        log.warning("iCloud is not configured; nothing to backfill")
        return []

    mailbox = Mailbox(settings)
    try:
        await mailbox.connect()
        async with session_scope() as session:
            messages = await mailbox.backfill(session, days)
    finally:
        await mailbox.close()
    return await _process(messages, settings)


async def _process(messages: list[Message], settings: Settings) -> list[str]:
    outcomes: list[str] = []
    for message in messages:
        async with session_scope() as session:
            outcomes.append(await process_message(session, message, settings=settings))
    if messages:
        log.info("ingested %d message(s): %s", len(messages), _tally(outcomes))
    return outcomes


async def _pause(stopping: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stopping.wait(), timeout=seconds)


def _tally(outcomes: list[str]) -> str:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    return ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
