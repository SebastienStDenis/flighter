"""iCloud over IMAP: one connection, the UID cursor, and raw messages out.

It knows nothing about flights, and it never writes to the mailbox. Everything is read
with `BODY.PEEK[]`, so nothing is marked read, flagged or moved: how far we have got is
our own state rather than a flag on somebody else's server, and the mailbox looks the
same afterwards as a phone left it.

The cursor is the whole difficulty. A UID only means anything inside the UIDVALIDITY it
was issued under, so the two are stored together, and a UIDVALIDITY that has changed
throws the position away and scans the recent window again rather than trusting a number
that now points somewhere else. Advancing it is a promise that everything before it has
been dealt with, so it is written by the caller once a batch has been processed rather
than when the batch is handed over.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from itertools import batched
from typing import Any, NamedTuple

from aioimaplib import IMAP4_SSL
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import prefs
from .config import Settings
from .models import KV, IngestLog

log = logging.getLogger(__name__)

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993

# Commands answer in a fraction of this. It is here so that a connection Apple has
# quietly stopped answering on surfaces as an error the watch loop can reconnect from,
# rather than as a mail loop that is alive and has not fetched anything for a week.
COMMAND_TIMEOUT_SECONDS = 30.0

CURSOR_KEY = "imap_cursor"

# How far back a first run, or a mailbox whose UIDVALIDITY has changed, looks. Wide
# enough to cover a long outage, and the ingest log is what keeps the overlap from being
# processed a second time.
RESCAN_DAYS = 14

# How many Message-ID headers are asked for in one command. A UID set is spelled out in
# full, and a server that truncates an over-long command line does it silently.
HEADER_BATCH = 200

# iCloud allows about five simultaneous connections per account, and the phone and the
# desktop are already holding some of them. A refused connection is answered by waiting
# rather than by asking again immediately, which is how an account gets locked out.
RECONNECT_MIN_SECONDS = 15.0
RECONNECT_MAX_SECONDS = 900.0

_UIDVALIDITY_RE = re.compile(rb"\[UIDVALIDITY (\d+)\]")
_UIDNEXT_RE = re.compile(rb"\[UIDNEXT (\d+)\]")
_EXISTS_RE = re.compile(rb"^(\d+) EXISTS")
_UID_RE = re.compile(rb"UID (\d+)")

# IMAP dates are spelled in English whatever the machine's locale says.
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


class Message(BaseModel):
    """One email, flattened to the parts an extractor can read."""

    id: str
    subject: str
    from_addr: str
    date: datetime | None
    text_plain: str = ""
    text_html: str = ""


class Cursor(NamedTuple):
    """Where we have got to. The UID is meaningless without the UIDVALIDITY beside it."""

    uidvalidity: int
    last_uid: int


# -- message parsing -----------------------------------------------------------------


def parse_message(raw: bytes, message_id: str) -> Message:
    """Flatten raw RFC822 bytes into a Message.

    The whole message is fetched and handed to the standard library rather than asking
    the server for one MIME part at a time: it already knows how to walk multipart,
    decode transfer encodings, and pick charsets, and the raw form is what the .eml
    fixtures exercise.
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


class Mailbox:
    """One IMAP connection to the watched folder.

    Held open and idling rather than reopened every few minutes: a login costs Apple
    more than an IDLE does, and the connection budget is shared with every other client
    signed in to the account.
    """

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client
        self.folder = prefs.current().imap_folder
        self.message_count = 0
        self._uidvalidity = 0
        self._uidnext = 0
        self._pending: Cursor | None = None

    async def connect(self) -> None:
        """Open the connection, log in, and select the folder."""
        if self._client is None:
            client = IMAP4_SSL(host=IMAP_HOST, port=IMAP_PORT, timeout=COMMAND_TIMEOUT_SECONDS)
            await client.wait_hello_from_server()
            self._client = client

        _ok(
            await self._client.login(
                self._settings.icloud_email, self._settings.icloud_app_password
            ),
            "login",
        )
        response = _ok(await self._client.select(_quote(self.folder)), f"select {self.folder}")

        uidvalidity = _first(_UIDVALIDITY_RE, response.lines)
        if uidvalidity is None:
            raise RuntimeError(f"{self.folder} was selected without a UIDVALIDITY")
        self._uidvalidity = uidvalidity
        self._uidnext = _first(_UIDNEXT_RE, response.lines) or 0
        self.message_count = _first(_EXISTS_RE, response.lines) or 0
        log.info(
            "watching %s: %d message(s), UIDVALIDITY %d",
            self.folder,
            self.message_count,
            self._uidvalidity,
        )

    async def close(self) -> None:
        """Hang up. Never raises: this runs while something else is already going wrong."""
        client, self._client = self._client, None
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.logout()

    async def poll(self, session: AsyncSession) -> list[Message]:
        """Everything that has arrived since the stored cursor, already de-duplicated.

        The new position is held back until commit_cursor is called, so a crash part way
        through a batch replays the batch instead of skipping the rest of it.
        """
        stored = await _read_cursor(session)
        if stored is not None and stored.uidvalidity == self._uidvalidity:
            found = await self._search_since(stored.last_uid)
            uids = [uid for uid in found if uid > stored.last_uid]
            floor = stored.last_uid
        else:
            if stored is not None:
                log.warning(
                    "UIDVALIDITY changed from %d to %d; rescanning the last %d days",
                    stored.uidvalidity,
                    self._uidvalidity,
                    RESCAN_DAYS,
                )
            else:
                log.info("no cursor yet; seeding from the last %d days", RESCAN_DAYS)
            uids = await self._search_recent(RESCAN_DAYS)
            floor = 0

        # Nothing in the window still moves the cursor forward, to the last UID the
        # folder holds: leaving it where it was would hand the whole mailbox to the next
        # poll, which reads `n:*` as "at least the newest message" whatever n is.
        position = max(uids) if uids else max(self._uidnext - 1, floor)
        self._pending = Cursor(self._uidvalidity, position)
        return await self._collect(session, uids)

    async def backfill(self, session: AsyncSession, days: int = 30) -> list[Message]:
        """A one-off sweep of recent mail, for the CLI.

        Leaves the cursor alone: a backfill is a catch-up over old mail and must not
        move a live watcher's position, forwards or backwards.
        """
        return await self._collect(session, await self._search_recent(days))

    async def commit_cursor(self, session: AsyncSession) -> None:
        """Advance the stored position. Only ever called once a batch is fully processed."""
        if self._pending is None:
            return
        await session.merge(
            KV(
                key=CURSOR_KEY,
                value={
                    "uidvalidity": self._pending.uidvalidity,
                    "last_uid": self._pending.last_uid,
                },
            )
        )
        log.debug("cursor advanced to %s", self._pending)
        self._pending = None

    async def wait_for_mail(self, seconds: float) -> None:
        """Idle until the server has something to say, or the cycle is up.

        Cycled rather than left open indefinitely because a silent IDLE is what a NAT
        table and an impatient server both drop, and neither tells the client.
        """
        client = self._require_client()
        idle = await client.idle_start(timeout=seconds)
        # idle_start's own timer ends the wait, so the timeout here only matters when
        # the connection has died without saying so.
        await client.wait_server_push(timeout=seconds + COMMAND_TIMEOUT_SECONDS)
        client.idle_done()
        await asyncio.wait_for(idle, COMMAND_TIMEOUT_SECONDS)

    # -- internals -------------------------------------------------------------------

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("the mailbox is not connected")
        return self._client

    async def _search_since(self, last_uid: int) -> list[int]:
        response = _ok(
            await self._require_client().uid_search(f"UID {last_uid + 1}:*", charset=None),
            "search",
        )
        return _uids(response.lines)

    async def _search_recent(self, days: int) -> list[int]:
        since = _imap_date(datetime.now(UTC).date() - timedelta(days=days))
        response = _ok(
            await self._require_client().uid_search(f"SINCE {since}", charset=None), "search"
        )
        return _uids(response.lines)

    async def _collect(self, session: AsyncSession, uids: Sequence[int]) -> list[Message]:
        """Fetch the bodies of everything the ingest log has not already recorded.

        The Message-ID headers are read first, in one command: they are what the log is
        keyed on, and reading a few hundred bytes each is what stops a re-scan from
        pulling down a fortnight of mail it has already decided about.
        """
        if not uids:
            return []

        identified: list[tuple[int, str]] = []
        for batch in batched(uids, HEADER_BATCH):
            identified.extend(
                (uid, _message_id(raw, self._uidvalidity, uid))
                for uid, raw in await self._fetch(batch, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            )
        seen = await _already_ingested(session, [message_id for _, message_id in identified])
        fresh = [(uid, message_id) for uid, message_id in identified if message_id not in seen]
        if len(fresh) != len(identified):
            log.info(
                "skipping %d message(s) already in the ingest log", len(identified) - len(fresh)
            )

        messages = []
        for uid, message_id in fresh:
            for _, raw in await self._fetch([uid], "(BODY.PEEK[])"):
                messages.append(parse_message(raw, message_id))
        return messages

    async def _fetch(self, uids: Sequence[int], parts: str) -> list[tuple[int, bytes]]:
        uid_set = ",".join(str(uid) for uid in uids)
        response = _ok(
            await self._require_client().uid("fetch", uid_set, parts), f"fetch {uid_set}"
        )
        return _fetched(response.lines)


# -- responses -----------------------------------------------------------------------


def _ok(response: Any, what: str) -> Any:
    """Every command is checked: a rejected login answers NO rather than raising."""
    if response.result != "OK":
        detail = b" ".join(bytes(line) for line in response.lines).decode(errors="replace")
        raise RuntimeError(f"IMAP {what} failed: {response.result} {detail}".strip())
    return response


def _first(pattern: re.Pattern[bytes], lines: Sequence[Any]) -> int | None:
    for line in lines:
        if isinstance(line, bytearray):
            continue
        match = pattern.search(bytes(line))
        if match:
            return int(match.group(1))
    return None


def _uids(lines: Sequence[Any]) -> list[int]:
    """The UIDs out of a SEARCH reply, which arrives as one space-separated line."""
    if not lines:
        return []
    return sorted(int(uid) for uid in bytes(lines[0]).split() if uid.isdigit())


def _fetched(lines: Sequence[Any]) -> list[tuple[int, bytes]]:
    """Pair each fetched literal with the UID from the line that introduced it.

    A FETCH reply is a header line naming the UID and the size, then the bytes as a
    bytearray, then a closing parenthesis, repeated once per message.
    """
    fetched: list[tuple[int, bytes]] = []
    uid: int | None = None
    for line in lines:
        if isinstance(line, bytearray):
            if uid is not None:
                fetched.append((uid, bytes(line)))
                uid = None
            continue
        match = _UID_RE.search(bytes(line))
        if match:
            uid = int(match.group(1))
    return fetched


def _message_id(raw: bytes, uidvalidity: int, uid: int) -> str:
    """The RFC822 Message-ID, which is what survives a message being moved or re-filed.

    A UID does not: it belongs to one folder and one UIDVALIDITY, so keying the ingest
    log on it would re-process every message the day either of those changed.
    """
    header = message_from_bytes(raw, policy=policy.default).get("Message-ID")
    value = str(header).strip() if header else ""
    return value or f"<uid-{uidvalidity}-{uid}@icloud.invalid>"


def _quote(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _imap_date(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


# -- stored state --------------------------------------------------------------------


async def _already_ingested(session: AsyncSession, message_ids: Sequence[str]) -> set[str]:
    if not message_ids:
        return set()
    rows = await session.execute(
        select(IngestLog.message_id).where(IngestLog.message_id.in_(list(message_ids)))
    )
    return set(rows.scalars().all())


async def _read_cursor(session: AsyncSession) -> Cursor | None:
    row = await session.get(KV, CURSOR_KEY)
    if row is None:
        return None
    uidvalidity = row.value.get("uidvalidity")
    last_uid = row.value.get("last_uid")
    if uidvalidity is None or last_uid is None:
        return None
    return Cursor(int(uidvalidity), int(last_uid))
