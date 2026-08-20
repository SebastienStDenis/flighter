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

`imap_tools` speaks the protocol. Nothing here reads a server reply by hand: the mailbox
list, the search result and the message all arrive parsed, which is the only honest way
to treat a server we cannot test against. It is synchronous, so every command runs on a
worker thread and the surface this module offers stays async.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any, NamedTuple

from imap_tools import FolderInfo, MailBox
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

# A wait is served as a run of short IDLEs rather than one long one. The thread running
# it cannot be interrupted, so this is how long a shutdown can be left waiting on it, and
# it keeps the wait inside the 29 minutes RFC 2177 allows however long a cycle is set to.
IDLE_CHUNK_SECONDS = 30.0

# Never searched. A draft or a sent copy of a forwarded confirmation holds the same
# flight and would import it a second time under a Message-ID of its own, and mail in
# Trash was thrown away on purpose. Matched on the RFC 6154 attributes so that a
# non-English account resolves them, with the names iCloud gives them as a backstop for
# a LIST reply that carries no attributes at all.
SKIPPED_ATTRIBUTES = frozenset({"\\trash", "\\junk", "\\drafts", "\\sent"})
SKIPPED_NAMES = frozenset({"trash", "deleted messages", "junk", "drafts", "sent", "sent messages"})
UNSELECTABLE_ATTRIBUTE = "\\noselect"


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

    Every method here is a single hop onto a worker thread around the blocking half of
    the same name below, so the connection is only ever touched by one thread at a time.
    """

    def __init__(self, settings: Settings, box: MailBox | None = None) -> None:
        self._settings = settings
        self._box = box
        self.colour = prefs.current().imap_flag_colour
        self._generation = credentials_generation()
        self.mailboxes: tuple[str, ...] = ()
        self.waiting = 0
        self._criteria = ""
        self._selected: str | None = None

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
        await asyncio.to_thread(self._connect)
        log.info(
            "watching for %s flags across %d mailbox(es): %s",
            self.colour,
            len(self.mailboxes),
            ", ".join(self.mailboxes),
        )

    async def close(self) -> None:
        """Hang up. Never raises: this runs while something else is already going wrong."""
        await asyncio.to_thread(self._close)

    async def poll(self) -> list[Marked]:
        """Every message carrying the flag right now, mailbox by mailbox, oldest first.

        No de-duplication and no cursor: a flag is only ever set by the user and only ever
        cleared by `clear_mark`, so what is flagged is exactly what is still to be done.
        """
        marked = await asyncio.to_thread(self._poll)
        self.waiting = len(marked)
        return marked

    async def count_flagged(self) -> int:
        """How many messages carry the flag, without fetching any of them."""
        self.waiting = await asyncio.to_thread(self._count_flagged)
        return self.waiting

    async def clear_mark(self, marked: Marked) -> None:
        """Take the flag off a finished message and leave it exactly where it is.

        Only ever called once the pipeline has written the message's ingest_log row, so a
        crash between the two leaves the message flagged and the next sweep replays it.
        """
        await asyncio.to_thread(self._clear_mark, marked)

    async def wait_for_mail(self, seconds: float) -> None:
        """Idle until the server has something to say, or the cycle is up.

        IDLE reports changes in the selected mailbox and nowhere else, so it catches the
        common case - a confirmation flagged where it landed, in the inbox - and the next
        sweep runs within a second or two. A flag set on a message filed somewhere else is
        never announced to anybody, and the sweep this timeout paces is what finds it.
        """
        deadline = monotonic() + seconds
        while (remaining := deadline - monotonic()) > 0:
            if await asyncio.to_thread(self._idle, min(remaining, IDLE_CHUNK_SECONDS)):
                return

    # -- the blocking half -----------------------------------------------------------

    def _require_box(self) -> MailBox:
        if self._box is None:
            raise RuntimeError("the mailbox is not connected")
        return self._box

    def _connect(self) -> None:
        if self._box is None:
            self._box = MailBox(IMAP_HOST, port=IMAP_PORT, timeout=COMMAND_TIMEOUT_SECONDS)
        box = self._box
        box.login(self._settings.icloud_email, self._settings.icloud_app_password)
        # Signing in selects the inbox, which is where the wait between sweeps idles.
        self._selected = IDLE_MAILBOX
        self.mailboxes = _searchable(box.folder.list())

    def _close(self) -> None:
        box, self._box = self._box, None
        self._selected = None
        if box is None:
            return
        with contextlib.suppress(Exception):
            box.logout()

    def _poll(self) -> list[Marked]:
        marked = []
        for mailbox in self.mailboxes:
            self._select(mailbox)
            # Everything is read out before any of it is processed: the pipeline takes a
            # model call and a handful of network writes per message, and iCloud drops a
            # connection left holding a half-consumed fetch across all of that.
            for message in self._require_box().fetch(self._criteria, mark_seen=False):
                uid = int(message.uid or 0)
                raw = message.obj.as_bytes()
                marked.append(
                    Marked(mailbox, uid, parse_message(raw, _message_id(raw, mailbox, uid)))
                )
        return marked

    def _count_flagged(self) -> int:
        waiting = 0
        for mailbox in self.mailboxes:
            self._select(mailbox)
            waiting += len(self._require_box().uids(self._criteria))
        return waiting

    def _clear_mark(self, marked: Marked) -> None:
        self._select(marked.mailbox)
        # Issued rather than left to `MailBox.flag`, which follows every store with an
        # EXPUNGE: this service takes a flag off, and it does not delete anybody's mail.
        # SILENT because the reply is the flags we just cleared and nothing reads them.
        _ok(
            self._require_box().client.uid(
                "STORE", str(marked.uid), "-FLAGS.SILENT", f"({_UNFLAG})"
            ),
            f"unflag {marked.uid} in {marked.mailbox}",
        )

    def _idle(self, seconds: float) -> bool:
        """One IDLE, and whether the server said anything before it was up."""
        self._select(IDLE_MAILBOX)
        return bool(self._require_box().idle.wait(timeout=seconds))

    def _select(self, mailbox: str) -> None:
        """SELECT, unless this mailbox is already the selected one."""
        if self._selected == mailbox:
            return
        self._require_box().folder.set(mailbox)
        self._selected = mailbox


# -- responses -----------------------------------------------------------------------


def _ok(result: tuple[str, Any], what: str) -> None:
    """Every command is checked: a refused store answers NO rather than raising."""
    status, detail = result
    if status != "OK":
        joined = b" ".join(line for line in detail if isinstance(line, bytes))
        raise RuntimeError(
            f"IMAP {what} failed: {status} {joined.decode(errors='replace')}".strip()
        )


def _searchable(folders: Iterable[FolderInfo]) -> tuple[str, ...]:
    """The mailboxes a flagged message is worth looking for in, in LIST order."""
    searchable = []
    for folder in folders:
        attributes = {attribute.lower() for attribute in folder.flags}
        if UNSELECTABLE_ATTRIBUTE in attributes or attributes & SKIPPED_ATTRIBUTES:
            continue
        if folder.name.lower() in SKIPPED_NAMES:
            continue
        searchable.append(folder.name)
    return tuple(searchable)


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


def _message_id(raw: bytes, mailbox: str, uid: int) -> str:
    """The RFC822 Message-ID, which is what survives a message being moved or re-filed.

    A UID does not: it belongs to the one mailbox it was handed out in, and the same
    confirmation filed by hand in two places would look like two different messages, so
    keying the ingest log on it would import it twice. Where a message carries no id at
    all - legal, if rare - where it is sitting is the only handle there is.
    """
    header = message_from_bytes(raw, policy=policy.default).get("Message-ID")
    value = str(header).strip() if header else ""
    return value or f"<uid-{mailbox}-{uid}@icloud.invalid>"
