"""Gmail: OAuth, the history cursor, and raw messages out. It knows nothing about flights.

The cursor is the whole difficulty. Gmail's `historyId` is only valid for about a week,
and advancing it is a promise that everything before it has been dealt with, so it is
written by the caller after a batch has been processed rather than when the batch is
handed over.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .google_auth import credentials
from .models import KV, IngestLog

log = logging.getLogger(__name__)

HISTORY_KEY = "gmail_history_id"
# Gmail keeps roughly a week of history. Past that `startHistoryId` is refused outright
# and the only way back is a date-bounded list, so the window is wide enough to cover a
# long outage and the ingest log stops the overlap from being processed twice.
FALLBACK_QUERY_DAYS = 14

# Set by poll_history, written to the database by commit_history_id once the batch has
# been processed. One task polls Gmail, so this is deliberately process-wide state
# rather than something threaded through every caller.
_pending_history_id: str | None = None


class Message(BaseModel):
    """One Gmail message, flattened to the parts an extractor can read."""

    id: str
    subject: str
    from_addr: str
    date: datetime | None
    text_plain: str = ""
    text_html: str = ""


# -- credentials ---------------------------------------------------------------------


_service: Any = None


def service(settings: Settings) -> Any:
    """The Gmail API handle, built once. Blocking, like everything on this client."""
    global _service
    if _service is None:
        _service = build("gmail", "v1", credentials=credentials(settings), cache_discovery=False)
    return _service


def reset_service() -> None:
    """Drop the cached handle so a freshly authorised token is picked up in place."""
    global _service
    _service = None


# -- message parsing -----------------------------------------------------------------


def parse_message(raw: bytes, message_id: str) -> Message:
    """Flatten raw RFC822 bytes into a Message.

    Gmail's `format=raw` is used in preference to the parsed payload tree: the standard
    library already knows how to walk multipart, decode transfer encodings, and pick
    charsets, and the raw form is what the .eml fixtures exercise.
    """
    parsed = message_from_bytes(raw, policy=policy.default)

    plain: list[str] = []
    html: list[str] = []
    for part in parsed.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(str(part.get_content()))
        elif content_type == "text/html":
            html.append(str(part.get_content()))

    return Message(
        id=message_id,
        subject=str(parsed.get("Subject") or ""),
        from_addr=str(parsed.get("From") or ""),
        date=_parse_date(parsed.get("Date")),
        text_plain="\n\n".join(p.strip() for p in plain).strip(),
        text_html="\n\n".join(html),
    )


def _parse_date(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    # A sender that omits the offset is rare but legal; UTC is the only defensible
    # reading, and the header only ever anchors relative dates in the extraction prompt.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# -- the mailbox ---------------------------------------------------------------------


async def fetch_message(message_id: str, *, settings: Settings | None = None) -> Message:
    settings = settings or get_settings()
    api = service(settings)
    payload = await asyncio.to_thread(
        lambda: api.users().messages().get(userId="me", id=message_id, format="raw").execute()
    )
    raw = base64.urlsafe_b64decode(payload["raw"])
    return parse_message(raw, message_id)


async def profile(*, settings: Settings | None = None) -> str:
    """The address of the mailbox we are watching, for the `check` command.

    The only call here that raises rather than logging and carrying on: proving the
    credentials work is the whole point, so the failure text has to reach the caller.
    """
    settings = settings or get_settings()
    api = service(settings)
    payload = await asyncio.to_thread(lambda: api.users().getProfile(userId="me").execute())
    return str(payload["emailAddress"])


async def poll_history(session: AsyncSession, *, settings: Settings | None = None) -> list[Message]:
    """Everything that has arrived since the stored cursor, already de-duplicated.

    The new cursor is held back until commit_history_id is called, so a crash part way
    through a batch replays the batch instead of skipping the rest of it.
    """
    global _pending_history_id
    settings = settings or get_settings()
    if not settings.google_connected:
        log.debug("Gmail is not configured; skipping poll")
        return []

    api = service(settings)
    start = await _read_history_id(session)

    if start is None:
        log.info("no Gmail cursor yet; seeding from the last %d days", FALLBACK_QUERY_DAYS)
        ids, cursor = await asyncio.to_thread(_scan_recent, api, FALLBACK_QUERY_DAYS)
    else:
        try:
            ids, cursor = await asyncio.to_thread(_scan_history, api, start)
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            # HistoryIdInvalid: the cursor fell out of Gmail's window, which takes about
            # a week of downtime. The ingest log is what keeps the overlap from being
            # processed a second time.
            log.warning("Gmail history id %s is no longer valid; falling back to a scan", start)
            ids, cursor = await asyncio.to_thread(_scan_recent, api, FALLBACK_QUERY_DAYS)

    _pending_history_id = cursor
    return await _fetch_unseen(session, ids, settings)


async def backfill(
    session: AsyncSession, days: int = 30, *, settings: Settings | None = None
) -> list[Message]:
    """A one-off sweep of recent mail, for the CLI.

    Leaves the cursor alone: a backfill is a catch-up over old mail and must not move a
    live poller's position, forwards or backwards.
    """
    settings = settings or get_settings()
    if not settings.google_connected:
        log.warning("Gmail is not configured; nothing to backfill")
        return []
    ids, _ = await asyncio.to_thread(_scan_recent, service(settings), days)
    return await _fetch_unseen(session, ids, settings)


async def commit_history_id(session: AsyncSession) -> None:
    """Advance the stored cursor. Only ever called once a batch is fully processed."""
    global _pending_history_id
    if _pending_history_id is None:
        return
    await session.execute(
        insert(KV)
        .values(key=HISTORY_KEY, value={"history_id": _pending_history_id})
        .on_conflict_do_update(
            index_elements=[KV.key], set_={"value": {"history_id": _pending_history_id}}
        )
    )
    log.debug("Gmail cursor advanced to %s", _pending_history_id)
    _pending_history_id = None


# -- internals -----------------------------------------------------------------------


def _scan_history(api: Any, start_history_id: str) -> tuple[list[str], str]:
    """Message ids added since the cursor, plus the cursor to store next."""
    ids: list[str] = []
    cursor = start_history_id
    page_token: str | None = None
    while True:
        response = (
            api.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                pageToken=page_token,
            )
            .execute()
        )
        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                ids.append(str(added["message"]["id"]))
        cursor = str(response.get("historyId", cursor))
        page_token = response.get("nextPageToken")
        if not page_token:
            return _dedupe(ids), cursor


def _scan_recent(api: Any, days: int) -> tuple[list[str], str]:
    """Every message from the last `days`, plus a cursor that predates the listing.

    The profile is read first on purpose: anything that arrives while the pages are
    being walked lands after the cursor and is picked up by the next poll.
    """
    cursor = str(api.users().getProfile(userId="me").execute()["historyId"])
    ids: list[str] = []
    page_token: str | None = None
    while True:
        response = (
            api.users()
            .messages()
            .list(userId="me", q=f"newer_than:{days}d", pageToken=page_token)
            .execute()
        )
        ids.extend(str(m["id"]) for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return _dedupe(ids), cursor


def _dedupe(ids: Sequence[str]) -> list[str]:
    """Order-preserving: one history page can report the same message more than once."""
    return list(dict.fromkeys(ids))


async def _fetch_unseen(
    session: AsyncSession, ids: Sequence[str], settings: Settings
) -> list[Message]:
    fresh = await _reconcile(session, ids)
    if len(fresh) != len(ids):
        log.info("skipping %d message(s) already in the ingest log", len(ids) - len(fresh))
    return [await fetch_message(message_id, settings=settings) for message_id in fresh]


async def _reconcile(session: AsyncSession, ids: Sequence[str]) -> list[str]:
    """Drop anything the ingest log has already seen."""
    if not ids:
        return []
    rows = await session.execute(
        select(IngestLog.gmail_message_id).where(IngestLog.gmail_message_id.in_(list(ids)))
    )
    seen = set(rows.scalars().all())
    return [message_id for message_id in ids if message_id not in seen]


async def _read_history_id(session: AsyncSession) -> str | None:
    row = await session.get(KV, HISTORY_KEY)
    if row is None:
        return None
    value = row.value.get("history_id")
    return str(value) if value else None
