"""iCloud over IMAP: one connection, one flag colour, and raw messages out.

It knows nothing about flights. The flag *is* the queue: an email you have flagged the
colour named on the settings page is work waiting to be done, and taking the flag off is
what says the work is finished. Nothing is scanned, ranked or guessed at, so there is no
cursor to keep and no window to re-scan - what is flagged is what is pending, and nothing
flagged means there is nothing to do.

A flag rides with the message wherever it already lives, so the sweep looks in every
mailbox the account has rather than in one, all of it down the same connection.

Bodies are read with `BODY.PEEK[]`, so an email is never silently marked as read. The
unflag is a write, and the only one: a message that came out the far end of the pipeline
loses its flag where it stands, and a message that failed keeps it so the next sweep
tries it again.
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
from .config import Settings, credentials_generation

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

# Apple encodes a flag's colour as a three-bit index carried alongside \Flagged, low bit
# first, in the order the colours appear in Mail's own flag menu. Red is index 0 and so
# sets no keyword at all, which makes it indistinguishable from a plain flag set by a
# client that knows nothing about colours; it is not offered. docs/api-research.md §6.1.
FLAG_KEYWORDS = ("$MailFlagBit0", "$MailFlagBit1", "$MailFlagBit2")
FLAG_COLOURS = {
    "orange": 1,
    "yellow": 2,
    "green": 3,
    "blue": 4,
    "purple": 5,
    "grey": 6,
}

# Everything an unflag takes off, whatever colour was configured: removing a flag a
# message does not carry is not an error, and clearing the colour bits without \Flagged
# would leave the message flagged red rather than unflagged.
_UNFLAG = " ".join(("\\Flagged", *FLAG_KEYWORDS))

# IDLE announces changes in the selected mailbox only, and the inbox is where a booking
# confirmation is flagged nine times in ten.
IDLE_MAILBOX = "INBOX"

# Never searched. A draft or a sent copy of a forwarded confirmation holds the same
# flight and would import it a second time under a Message-ID of its own, and mail in
# Trash was thrown away on purpose. Matched on the RFC 6154 attributes so that a
# non-English account resolves them, with the names iCloud gives them as a backstop for
# a LIST reply that carries no attributes at all.
SKIPPED_ATTRIBUTES = frozenset({"\\trash", "\\junk", "\\drafts", "\\sent"})
SKIPPED_NAMES = frozenset({"trash", "deleted messages", "junk", "drafts", "sent", "sent messages"})
UNSELECTABLE_ATTRIBUTE = "\\noselect"

_UIDVALIDITY_RE = re.compile(rb"\[UIDVALIDITY (\d+)\]")
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
    """One flagged message, with the mailbox and UID that together clear its flag."""

    mailbox: str
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
    """One IMAP connection, and every mailbox on the account behind it.

    Held open and idling rather than reopened every few minutes: a login costs Apple more
    than an IDLE does, and the connection budget is shared with every other client signed
    in to the account. The sweep never opens a second one either - it selects each
    mailbox in turn down this one.
    """

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client
        self.colour = prefs.current().imap_flag_colour
        self._generation = credentials_generation()
        self.mailboxes: tuple[str, ...] = ()
        self.waiting = 0
        self._criteria = ""
        self._selected: str | None = None
        self._uidvalidity = 0

    @property
    def current(self) -> bool:
        """Whether this connection still matches what the settings page says.

        Neither a new flag colour nor a new app-specific password reaches the server
        without opening a connection again, and this one may sit idling for minutes.
        """
        return (
            self.colour == prefs.current().imap_flag_colour
            and self._generation == credentials_generation()
        )

    async def connect(self) -> None:
        """Open the connection, log in, and work out which mailboxes to sweep."""
        self._criteria = _criteria(self.colour)
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
        self.mailboxes = _searchable(listed)
        await self._select(IDLE_MAILBOX)
        log.info(
            "watching for %s flags across %d mailbox(es): %s",
            self.colour,
            len(self.mailboxes),
            ", ".join(self.mailboxes),
        )

    async def close(self) -> None:
        """Hang up. Never raises: this runs while something else is already going wrong."""
        client, self._client = self._client, None
        self._selected = None
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.logout()

    async def poll(self) -> list[Marked]:
        """Every message carrying the flag right now, mailbox by mailbox, oldest first.

        No de-duplication and no cursor: a flag is only ever set by the user and only ever
        cleared by `clear_mark`, so what is flagged is exactly what is still to be done.
        """
        marked = []
        for mailbox in self.mailboxes:
            for uid in await self._flagged(mailbox):
                for _, raw in await self._fetch(uid, "(BODY.PEEK[])"):
                    message_id = _message_id(raw, self._uidvalidity, uid)
                    marked.append(Marked(mailbox, uid, parse_message(raw, message_id)))
        self.waiting = len(marked)
        return marked

    async def count_flagged(self) -> int:
        """How many messages carry the flag, without fetching any of them."""
        waiting = 0
        for mailbox in self.mailboxes:
            waiting += len(await self._flagged(mailbox))
        self.waiting = waiting
        return waiting

    async def clear_mark(self, marked: Marked) -> None:
        """Take the flag off a finished message and leave it exactly where it is.

        Only ever called once the pipeline has written the message's ingest_log row, so a
        crash between the two leaves the message flagged and the next sweep replays it.
        """
        await self._select(marked.mailbox)
        _ok(
            await self._require_client().uid(
                "store", str(marked.uid), f"-FLAGS.SILENT ({_UNFLAG})"
            ),
            f"unflag {marked.uid} in {marked.mailbox}",
        )

    async def wait_for_mail(self, seconds: float) -> None:
        """Idle until the server has something to say, or the cycle is up.

        IDLE reports changes in the selected mailbox and nowhere else, so it catches the
        common case - a confirmation flagged where it landed, in the inbox - and the next
        sweep runs within a second or two. A flag set on a message filed somewhere else is
        never announced to anybody, and the sweep this timeout paces is what finds it.

        Cycled rather than left open indefinitely because a silent IDLE is what a NAT
        table and an impatient server both drop, and neither tells the client.
        """
        await self._select(IDLE_MAILBOX)
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

    async def _select(self, mailbox: str) -> None:
        """SELECT, unless this mailbox is already the selected one."""
        if self._selected == mailbox:
            return
        response = _ok(await self._require_client().select(_quote(mailbox)), f"select {mailbox}")
        uidvalidity = _first(_UIDVALIDITY_RE, response.lines)
        if uidvalidity is None:
            raise RuntimeError(f"{mailbox} was selected without a UIDVALIDITY")
        self._selected = mailbox
        self._uidvalidity = uidvalidity

    async def _flagged(self, mailbox: str) -> list[int]:
        await self._select(mailbox)
        response = _ok(
            await self._require_client().uid_search(self._criteria, charset=None),
            f"search {mailbox}",
        )
        return _uids(response.lines)

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


def _searchable(listed: Sequence[Listed]) -> tuple[str, ...]:
    """The mailboxes a flagged message is worth looking for in, in LIST order."""
    return tuple(
        entry.name
        for entry in listed
        if UNSELECTABLE_ATTRIBUTE not in entry.attributes
        and not entry.attributes & SKIPPED_ATTRIBUTES
        and entry.name.lower() not in SKIPPED_NAMES
    )


def _criteria(colour: str) -> str:
    """The SEARCH keys that match exactly one flag colour and no other.

    Every bit is pinned, not only the set ones: purple and grey both carry
    `$MailFlagBit2`, so a search that only asked for what is set would import mail marked
    for something else entirely.
    """
    index = FLAG_COLOURS.get(colour)
    if index is None:
        raise RuntimeError(
            f"{colour!r} is not a flag colour this can watch for. "
            f"Pick one of {', '.join(FLAG_COLOURS)} on the settings page."
        )
    keys = ["FLAGGED"]
    for bit, keyword in enumerate(FLAG_KEYWORDS):
        keys.append(f"{'KEYWORD' if index >> bit & 1 else 'UNKEYWORD'} {keyword}")
    return " ".join(keys)


def _message_id(raw: bytes, uidvalidity: int, uid: int) -> str:
    """The RFC822 Message-ID, which is what survives a message being moved or re-filed.

    A UID does not: it belongs to one mailbox under one UIDVALIDITY, and the same
    confirmation filed by hand in two places would look like two different messages, so
    keying the ingest log on it would import it twice.
    """
    header = message_from_bytes(raw, policy=policy.default).get("Message-ID")
    value = str(header).strip() if header else ""
    return value or f"<uid-{uidvalidity}-{uid}@icloud.invalid>"


def _quote(mailbox: str) -> str:
    escaped = mailbox.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote(name: str) -> str:
    return name.replace('\\"', '"').replace("\\\\", "\\")
