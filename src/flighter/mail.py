"""iCloud over IMAP: one connection, one folder, and raw messages out.

It knows nothing about flights. The folder *is* the queue: an email you have moved into
it is work waiting to be done, and moving it back out is what says the work is finished.
Nothing is scanned, ranked or guessed at, so there is no cursor to keep and no window to
re-scan - what is in the folder is what is pending, and an empty folder means there is
nothing to do.

Bodies are read with `BODY.PEEK[]`, so an email is never silently marked as read. The
mark itself is a write, and the only one: a message that came out the far end of the
pipeline is moved to the archive folder, and a message that failed is left exactly where
it stands so the next sweep tries it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from typing import Any, NamedTuple

from aioimaplib import IMAP4_SSL
from pydantic import BaseModel

from . import prefs
from .config import Settings

log = logging.getLogger(__name__)

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993

# Commands answer in a fraction of this. It is here so that a connection Apple has
# quietly stopped answering on surfaces as an error the watch loop can reconnect from,
# rather than as a mail loop that is alive and has not fetched anything for a week.
COMMAND_TIMEOUT_SECONDS = 30.0

# iCloud allows about five simultaneous connections per account, and the phone and the
# desktop are already holding some of them. A refused connection is answered by waiting
# rather than by asking again immediately, which is how an account gets locked out.
RECONNECT_MIN_SECONDS = 15.0
RECONNECT_MAX_SECONDS = 900.0

# Where a finished message goes. `\Archive` is what the account itself calls its archive
# whatever the display language is; the last resort is the inbox, which is somewhere the
# user will certainly look.
ARCHIVE_ATTRIBUTE = "\\archive"
ARCHIVE_FALLBACKS = ("Archive", "INBOX")

_UIDVALIDITY_RE = re.compile(rb"\[UIDVALIDITY (\d+)\]")
_EXISTS_RE = re.compile(rb"^(\d+) EXISTS")
_UID_RE = re.compile(rb"UID (\d+)")
# `(\HasNoChildren \Archive) "/" "Archive"`: attributes, the hierarchy delimiter, a name
# that is quoted only when the server feels like quoting it.
_LIST_RE = re.compile(r'^\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?:"((?:[^"\\]|\\.)*)"|(\S+))\s*$')


class Message(BaseModel):
    """One email, flattened to the parts an extractor can read."""

    id: str
    subject: str
    from_addr: str
    date: datetime | None
    text_plain: str = ""
    text_html: str = ""


class Marked(NamedTuple):
    """One message waiting in the folder, with the UID that clears its mark."""

    uid: int
    message: Message


class Listed(NamedTuple):
    """One line of a LIST reply."""

    attributes: frozenset[str]
    name: str


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
    """One IMAP connection to the folder you move flight emails into.

    Held open and idling rather than reopened every few minutes: a login costs Apple
    more than an IDLE does, and the connection budget is shared with every other client
    signed in to the account.
    """

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client
        self.folder = prefs.current().imap_import_folder
        self.archive = ARCHIVE_FALLBACKS[-1]
        self.waiting = 0
        self._uidvalidity = 0

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

        listed = _listed(_ok(await self._client.list('""', '"*"'), "list").lines)
        if self.folder not in {entry.name for entry in listed}:
            raise RuntimeError(
                f"there is no mailbox called {self.folder}. Make one in Mail and move the "
                f"flight emails you want imported into it."
            )
        self.archive = _archive(listed)

        response = _ok(await self._client.select(_quote(self.folder)), f"select {self.folder}")
        uidvalidity = _first(_UIDVALIDITY_RE, response.lines)
        if uidvalidity is None:
            raise RuntimeError(f"{self.folder} was selected without a UIDVALIDITY")
        self._uidvalidity = uidvalidity
        self.waiting = _first(_EXISTS_RE, response.lines) or 0
        log.info(
            "watching %s: %d message(s) marked, finished mail goes to %s",
            self.folder,
            self.waiting,
            self.archive,
        )

    async def close(self) -> None:
        """Hang up. Never raises: this runs while something else is already going wrong."""
        client, self._client = self._client, None
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.logout()

    async def poll(self) -> list[Marked]:
        """Everything sitting in the folder right now, oldest first.

        No de-duplication and no cursor: the folder holds only what is still to be done,
        and anything already dealt with was moved out of it by `clear_mark`.
        """
        response = _ok(await self._require_client().uid_search("ALL", charset=None), "search")
        uids = _uids(response.lines)
        self.waiting = len(uids)

        marked = []
        for uid in uids:
            for _, raw in await self._fetch(uid, "(BODY.PEEK[])"):
                message_id = _message_id(raw, self._uidvalidity, uid)
                marked.append(Marked(uid, parse_message(raw, message_id)))
        return marked

    async def clear_mark(self, uid: int) -> None:
        """Move a finished message to the archive, which is what unmarks it.

        Only ever called once the pipeline has written the message's ingest_log row, so a
        crash between the two leaves the message marked and the next sweep replays it.
        """
        _ok(
            await self._require_client().uid("move", str(uid), _quote(self.archive)),
            f"move {uid} to {self.archive}",
        )

    async def wait_for_mail(self, seconds: float) -> None:
        """Idle until the server has something to say, or the cycle is up.

        Marking a message *is* moving it into the selected folder, so IDLE reports it as
        an arrival and the next sweep runs within a second or two. The timeout is the
        floor under that: a move made while the connection was being re-established is
        never announced to anybody, and only the sweep finds it.

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

    async def _fetch(self, uid: int, parts: str) -> list[tuple[int, bytes]]:
        response = _ok(await self._require_client().uid("fetch", str(uid), parts), f"fetch {uid}")
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


def _listed(lines: Sequence[Any]) -> list[Listed]:
    """The mailboxes out of a LIST reply, one per line, plus a completion line to ignore."""
    entries = []
    for line in lines:
        if isinstance(line, bytearray):
            continue
        match = _LIST_RE.match(bytes(line).decode(errors="replace").strip())
        if match:
            attributes = frozenset(flag.lower() for flag in match.group(1).split())
            entries.append(Listed(attributes, _unquote(match.group(2) or match.group(3))))
    return entries


def _archive(listed: Sequence[Listed]) -> str:
    for entry in listed:
        if ARCHIVE_ATTRIBUTE in entry.attributes:
            return entry.name
    names = {entry.name for entry in listed}
    return next((name for name in ARCHIVE_FALLBACKS if name in names), ARCHIVE_FALLBACKS[-1])


def _message_id(raw: bytes, uidvalidity: int, uid: int) -> str:
    """The RFC822 Message-ID, which is what survives a message being moved or re-filed.

    A UID does not: it belongs to one folder and one UIDVALIDITY, and clearing a mark
    moves the message to another folder, so keying the ingest log on it would make every
    finished message look new again.
    """
    header = message_from_bytes(raw, policy=policy.default).get("Message-ID")
    value = str(header).strip() if header else ""
    return value or f"<uid-{uidvalidity}-{uid}@icloud.invalid>"


def _quote(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote(name: str) -> str:
    return name.replace('\\"', '"').replace("\\\\", "\\")
