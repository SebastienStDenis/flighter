"""The pipeline: a Gmail message in, bookings and one ingest_log row out.

Every message ends with exactly one ingest_log row, whatever happened to it, because
that row is both the record of what we decided and the thing that stops a re-delivery
being processed twice. One bad email is never allowed to stop the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import unicodedata
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import prefs
from .airports import airport_tz
from .bookings import create_booking, find_duplicate
from .config import Settings, get_settings
from .db import session_scope
from .extract import Extraction, Segment, from_jsonld, from_model, looks_like_flight
from .gmail import Message, commit_history_id, poll_history
from .models import IngestLog, Passenger
from .timezones import to_utc

log = logging.getLogger(__name__)

# Below this two names are different people. Names come off tickets in every shape
# ("SEBASTIEN ST-DENIS", "St Denis/Sebastien Mr"), so the comparison is on a normalised
# first-and-last pair rather than the raw string, and this stays fairly strict.
NAME_MATCH_THRESHOLD = 0.87

# A message yields one outcome even when it carried several segments. Review wins over
# a success because it is the one that still needs a person.
_OUTCOME_PRECEDENCE = ("review", "created", "duplicate")

# Printed on tickets, never in the passenger list.
_TITLES = frozenset({"mr", "mrs", "ms", "miss", "mstr", "dr", "prof", "sir", "madam"})


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
    passenger, matched = await resolve_passenger(session, extraction.passenger_names)
    confident = extraction.confidence >= prefs.current().extraction_confidence_threshold
    # An unmatched name is as much a reason for a human to look as a shaky extraction:
    # the booking is attributed to the only self passenger so the row can exist at all,
    # and the review queue is where that guess gets confirmed.
    status = "active" if confident and matched else "pending_review"

    outcomes: list[str] = []
    for segment in extraction.segments:
        outcomes.append(
            await _book_segment(session, message, extraction, segment, passenger, status)
        )
    return next((o for o in _OUTCOME_PRECEDENCE if o in outcomes), "no_flight")


async def _book_segment(
    session: AsyncSession,
    message: Message,
    extraction: Extraction,
    segment: Segment,
    passenger: Passenger,
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
        passenger.id,
        segment.marketing_carrier,
        segment.marketing_number,
        to_utc(departure_local, origin_tz),
    )
    if twin is not None:
        log.info("%s is already booked as %d", flight, twin.id)
        return "duplicate"

    booking = await create_booking(
        session,
        passenger_id=passenger.id,
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
        "booked %s %s-%s for %s as %s (%d)",
        flight,
        segment.origin_iata,
        segment.dest_iata,
        passenger.display_name,
        status,
        booking.id,
    )
    return "review" if status == "pending_review" else "created"


# -- passengers ----------------------------------------------------------------------


def normalise_name(name: str) -> str:
    """Fold a name to lowercase ASCII letters and spaces, so only the letters compare."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalpha() else " " for c in stripped).lower().split())


def name_key(name: str) -> str:
    """First and last name only, in that order.

    Middle names, initials, and titles appear on a ticket about half the time and never
    in the passenger list, so comparing them only ever produces false misses. Airlines
    also print the surname first, separated by a slash: "ST-DENIS/SEBASTIEN MR".
    """
    surname, _, given = name.partition("/")
    parts = [
        p
        for p in normalise_name(f"{given} {surname}" if given else name).split()
        if p not in _TITLES
    ]
    if len(parts) <= 1:
        return " ".join(parts)
    return f"{parts[0]} {parts[-1]}"


def names_match(a: str, b: str) -> bool:
    """Whether two names plausibly belong to the same person.

    Some senders print the name reversed ("TREMBLAY MARIE") with no separator to
    give the order away, so a swap of the two ends counts as a match too.
    """
    left, right = name_key(a), name_key(b)
    if not left or not right:
        return False
    if left == right or left == " ".join(reversed(right.split())):
        return True
    return SequenceMatcher(None, left, right).ratio() >= NAME_MATCH_THRESHOLD


async def resolve_passenger(session: AsyncSession, names: list[str]) -> tuple[Passenger, bool]:
    """The passenger a booking belongs to, and whether we are actually sure of it.

    A booking row needs a passenger_id to exist at all, so an unrecognised name falls
    back to the single self passenger with `matched` false; the caller turns that into a
    pending_review booking for the UI's picker to correct.
    """
    result = await session.execute(select(Passenger))
    passengers = list(result.scalars().all())
    if not passengers:
        raise ValueError("no passengers are configured; add one before ingesting mail")

    for name in names:
        for passenger in passengers:
            if names_match(name, passenger.display_name):
                return passenger, True

    selves = [p for p in passengers if p.is_self]
    if len(selves) == 1:
        log.info(
            "no passenger matched %s; attributing to %s for review",
            names,
            selves[0].display_name,
        )
        return selves[0], False
    raise ValueError(f"no passenger matched {names} and there is no single self passenger")


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
            gmail_message_id=message.id,
            outcome=outcome,
            raw_extraction=extraction.model_dump(mode="json") if extraction else None,
            error=error,
        )
    )
    await session.flush()
    return outcome


# -- the loop ------------------------------------------------------------------------


async def run_ingest_loop(stopping: asyncio.Event, *, settings: Settings | None = None) -> None:
    """Poll Gmail until asked to stop.

    Each message is committed on its own so that a failure part way through a batch
    keeps everything already ingested, and the Gmail cursor only advances once the whole
    batch is in the log.
    """
    settings = settings or get_settings()

    while not stopping.is_set():
        try:
            # Checked every pass rather than once: Google is connected from the settings
            # page, and a loop that gave up at boot would need a restart to notice.
            if settings.google_connected:
                await ingest_once(settings)
        except Exception:
            log.exception("ingest poll failed; retrying at the next interval")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=prefs.current().gmail_poll_seconds)


async def ingest_once(settings: Settings) -> list[str]:
    """One poll: fetch, process, then advance the cursor. Returns the outcomes."""
    async with session_scope() as session:
        messages = await poll_history(session, settings=settings)

    outcomes: list[str] = []
    for message in messages:
        async with session_scope() as session:
            outcomes.append(await process_message(session, message, settings=settings))

    if messages:
        log.info("ingested %d message(s): %s", len(messages), _tally(outcomes))
    async with session_scope() as session:
        await commit_history_id(session)
    return outcomes


def _tally(outcomes: list[str]) -> str:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    return ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
