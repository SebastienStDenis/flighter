"""The IMAP layer, against a fake server: no network, no iCloud, no database.

The mailbox is somebody else's, so the properties worth pinning down are the ones that
decide whether an email is imported twice or never at all: which mailboxes are searched
and which are deliberately not, that only the configured colour counts, that a body is
never fetched without peeking, and that taking the flag off is the one write we ever make.

The fake answers on the wire rather than at the library's API, so a reply here is shaped
the way a server shapes one - untagged lines, literals, and a completion line of prose at
the end of every command. That last part is the one that matters: a search that matched
nothing is a completion line and nothing else, and the numbers in it are not UIDs.
"""

from __future__ import annotations

import contextlib
import imaplib
import os
from pathlib import Path
from typing import Any

import pytest
from imap_tools import MailBox

from flighter import mail, prefs
from flighter.config import Settings
from flighter.mail import FLAG_COLOURS, FLAG_KEYWORDS, Mailbox, parse_message

FIXTURES = Path(__file__).parent / "fixtures"

FLAGGED = "\\Flagged"

# Every mailbox iCloud ships with, plus one the user made, so a test can assert on which
# of them the sweep goes near.
_DEFAULT_MAILBOXES = [
    ("\\HasNoChildren", "INBOX"),
    ("\\HasNoChildren \\Archive", "Archive"),
    ("\\HasNoChildren \\Sent", "Sent Messages"),
    ("\\HasNoChildren \\Drafts", "Drafts"),
    ("\\HasNoChildren \\Junk", "Junk"),
    ("\\HasNoChildren \\Trash", "Deleted Messages"),
    ("\\HasNoChildren", "Travel"),
]
SKIPPED = {"Sent Messages", "Drafts", "Junk", "Deleted Messages"}

GREETING = b"* OK [CAPABILITY IMAP4rev1 IDLE] iCloud ready\r\n"


def raw(message_id: str, subject: str = "Your itinerary") -> bytes:
    return (
        "From: notifications@airline.example\r\n"
        "To: someone@icloud.com\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {message_id}\r\n"
        "Date: Tue, 18 Aug 2026 09:12:00 -0400\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Seat 12A.\r\n"
    ).encode()


def flags(colour: str) -> set[str]:
    """What Apple Mail leaves on a message flagged this colour. Red sets no keyword."""
    index = FLAG_COLOURS.get(colour, 0)
    return {FLAGGED, *(word for bit, word in enumerate(FLAG_KEYWORDS) if index >> bit & 1)}


class FakeServer:
    """Enough of an iCloud IMAP server to drive a Mailbox, and a record of what it was asked."""

    def __init__(
        self,
        messages: dict[str, dict[int, tuple[bytes, set[str]]]] | None = None,
        mailboxes: list[tuple[str, str]] | None = None,
        *,
        announces_empty_search: bool = True,
    ) -> None:
        self.messages = messages if messages is not None else {}
        # (attributes, name), as a LIST reply spells them.
        self.mailboxes = mailboxes if mailboxes is not None else _DEFAULT_MAILBOXES
        # Servers differ on whether a search that matched nothing is announced at all or
        # left to be inferred from the completion line. Both are in the wild.
        self.announces_empty_search = announces_empty_search
        self.selected: list[str] = []
        self.searches: list[tuple[str, str]] = []
        self.fetches: list[tuple[str, str]] = []
        self.stored: list[tuple[str, str, str]] = []
        self.logged_out = False
        self.rejects_login = False
        self.rejects_store = False
        # Lines the server volunteers the next time the client idles.
        self.pushes: list[bytes] = []
        self._selected = ""
        self._idle_tag = ""

    def command(self, line: str, connection: FakeSocket) -> None:
        """One command line in, whatever the server would say back out."""
        if line.upper() == "DONE":
            connection.reply(f"{self._idle_tag} OK IDLE terminated.\r\n".encode())
            return

        tag, _, body = line.partition(" ")
        name, _, rest = body.partition(" ")
        name = name.upper()
        if name == "UID":
            sub_command, _, rest = rest.partition(" ")
            name = f"UID {sub_command.upper()}"

        if name == "IDLE":
            self._idle_tag = tag
            connection.reply(b"+ idling\r\n")
            for push in self.pushes:
                connection.announce(push)
            self.pushes.clear()
            return
        connection.reply(self._reply(tag, name, rest))

    def _reply(self, tag: str, name: str, rest: str) -> bytes:
        if name == "CAPABILITY":
            return f"* CAPABILITY IMAP4rev1 IDLE\r\n{tag} OK Capability completed.\r\n".encode()
        if name == "LOGIN":
            if self.rejects_login:
                return f"{tag} NO [AUTHENTICATIONFAILED] Authentication failed.\r\n".encode()
            return f"{tag} OK Login completed.\r\n".encode()
        if name == "LOGOUT":
            self.logged_out = True
            return f"* BYE Logging out\r\n{tag} OK Logout completed.\r\n".encode()
        if name == "LIST":
            listed = "".join(
                f'* LIST ({attributes}) "/" "{mailbox}"\r\n'
                for attributes, mailbox in self.mailboxes
            )
            return f"{listed}{tag} OK List completed.\r\n".encode()
        if name == "SELECT":
            return self._select(tag, _unquote(rest))
        if name == "UID SEARCH":
            return self._search(tag, rest)
        if name == "UID FETCH":
            return self._fetch(tag, rest)
        if name == "UID STORE":
            return self._store(tag, rest)
        raise AssertionError(f"the fake server was asked for {name}, which it does not offer")

    def _select(self, tag: str, mailbox: str) -> bytes:
        self.selected.append(mailbox)
        self._selected = mailbox
        return (
            f"* {len(self._held())} EXISTS\r\n"
            f"* OK [UIDVALIDITY 4242] UIDs valid\r\n"
            f"{tag} OK [READ-WRITE] Select completed.\r\n"
        ).encode()

    def _search(self, tag: str, rest: str) -> bytes:
        criteria = rest.split(" ", 2)[2] if rest.upper().startswith("CHARSET ") else rest
        self.searches.append((self._selected, criteria))
        hits = [uid for uid, (_, held) in sorted(self._held().items()) if _matches(held, criteria)]
        announced = ""
        if hits or self.announces_empty_search:
            announced = "* SEARCH" + "".join(f" {uid}" for uid in hits) + "\r\n"
        # The prose is the server's, numbers and all, and it is the only line a search
        # that matched nothing is guaranteed to produce.
        return f"{announced}{tag} OK Search completed ({len(hits)} msgs in 2 secs).\r\n".encode()

    def _fetch(self, tag: str, rest: str) -> bytes:
        uid_set, _, parts = rest.partition(" ")
        self.fetches.append((uid_set, parts))
        assert "BODY.PEEK" in parts, "a body was fetched without peeking"
        reply = b""
        for sequence, uid in enumerate(int(value) for value in uid_set.split(",")):
            payload, held = self._held()[uid]
            reply += (
                f"* {sequence + 1} FETCH (UID {uid} RFC822.SIZE {len(payload)} "
                f"FLAGS ({' '.join(sorted(held))}) BODY[] {{{len(payload)}}}\r\n"
            ).encode()
            reply += payload + b")\r\n"
        return reply + f"{tag} OK Fetch completed.\r\n".encode()

    def _store(self, tag: str, rest: str) -> bytes:
        uid_set, instruction, flag_list = rest.split(" ", 2)
        self.stored.append((self._selected, uid_set, f"{instruction} {flag_list}"))
        if self.rejects_store:
            return f"{tag} NO Over quota\r\n".encode()
        removed = set(flag_list.strip("()").split())
        for value in uid_set.split(","):
            self._held()[int(value)][1].difference_update(removed)
        return f"{tag} OK Store completed.\r\n".encode()

    def _held(self) -> dict[int, tuple[bytes, set[str]]]:
        return self.messages.setdefault(self._selected, {})


class FakeSocket:
    """What imaplib reads and writes through: the socket and the file it makes of it.

    Bytes are handed over in memory, but IDLE waits on the descriptor itself rather than
    on a read, so readiness is a real pipe that only an announced line ever writes to.
    """

    def __init__(self, server: FakeServer) -> None:
        self._server = server
        self._out = bytearray(GREETING)
        self._sent = bytearray()
        self._ready_out, self._ready_in = os.pipe()
        os.set_blocking(self._ready_out, False)
        self._timeout: float | None = None

    def reply(self, data: bytes) -> None:
        self._out += data

    def announce(self, data: bytes) -> None:
        """Say something nobody asked for, and wake whoever is polling."""
        self._out += data
        os.write(self._ready_in, b"!")

    def sendall(self, data: bytes) -> None:
        self._sent += data
        while b"\r\n" in self._sent:
            line, _, rest = bytes(self._sent).partition(b"\r\n")
            self._sent = bytearray(rest)
            self._server.command(line.decode(), self)

    def makefile(self, mode: str) -> FakeSocket:
        return self

    def read(self, size: int) -> bytes:
        return self._take(size)

    def readline(self, limit: int = -1) -> bytes:
        end = self._out.find(b"\n")
        size = len(self._out) if end < 0 else end + 1
        return self._take(size if limit < 0 else min(size, limit))

    def _take(self, size: int) -> bytes:
        if not self._out:
            # imap_tools drains an IDLE with the socket non-blocking and stops at the
            # timeout; anywhere else an empty buffer is the server having hung up.
            if self._timeout == 0:
                raise TimeoutError
            return b""
        taken, self._out = bytes(self._out[:size]), self._out[size:]
        if not self._out:
            with contextlib.suppress(BlockingIOError):
                os.read(self._ready_out, 64)
        return taken

    def fileno(self) -> int:
        return self._ready_out

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def gettimeout(self) -> float | None:
        return self._timeout

    def close(self) -> None:
        os.close(self._ready_out)
        os.close(self._ready_in)


class FakeIMAP4(imaplib.IMAP4):
    """The real imaplib, on a connection that goes to the fake server instead of Apple."""

    def __init__(self, server: FakeServer) -> None:
        self._server = server
        super().__init__("fake.invalid", 993)

    def open(self, host: str = "", port: int = 993, timeout: float | None = None) -> None:
        self.host, self.port = host, port
        self.sock = FakeSocket(self._server)  # type: ignore[assignment]
        self.file = self.sock.makefile("rb")

    def shutdown(self) -> None:
        self.sock.close()


class FakeMailBox(MailBox):
    """imap_tools in full, from the login down, over a connection that goes nowhere."""

    def __init__(self, server: FakeServer) -> None:
        self._server = server
        super().__init__(mail.IMAP_HOST)

    def _get_mailbox_client(self) -> imaplib.IMAP4:
        return FakeIMAP4(self._server)


def _unquote(value: str) -> str:
    return value[1:-1].replace('\\"', '"').replace("\\\\", "\\") if value.startswith('"') else value


def _matches(held: set[str], criteria: str) -> bool:
    """The subset of SEARCH this layer ever issues: FLAGGED, KEYWORD and UNKEYWORD."""
    tokens = criteria.split()
    index = 0
    while index < len(tokens):
        key = tokens[index]
        if key == "FLAGGED":
            if FLAGGED not in held:
                return False
            index += 1
        elif key in ("KEYWORD", "UNKEYWORD"):
            if (tokens[index + 1] in held) is not (key == "KEYWORD"):
                return False
            index += 2
        else:
            raise AssertionError(f"unexpected search key {key}")
    return True


def inbox(*messages: tuple[int, bytes, set[str]]) -> dict[str, dict[int, tuple[bytes, set[str]]]]:
    return {"INBOX": {uid: (payload, held) for uid, payload, held in messages}}


def _watching(colour: str) -> prefs.Prefs:
    """The live preferences with one other colour named as the import flag."""
    return prefs.current().model_copy(update={"imap_flag_colour": colour})


async def connected(settings: Settings, server: FakeServer) -> Mailbox:
    mailbox = Mailbox(settings, box=FakeMailBox(server))
    await mailbox.connect()
    return mailbox


# -- parsing -------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.eml")), ids=lambda p: p.name)
def test_every_fixture_still_parses(fixture: Path) -> None:
    message = parse_message(fixture.read_bytes(), fixture.name)
    assert message.id == fixture.name
    assert message.subject
    assert message.from_addr
    assert message.text_plain or message.text_html


async def test_a_message_without_a_message_id_is_still_identified(settings: Settings) -> None:
    """Legal, if rare. Without an id of its own there is nothing to key the log on."""
    body = b"Subject: Your itinerary\r\nFrom: a@b.example\r\n\r\nSeat 12A.\r\n"
    server = FakeServer(inbox((7, body, flags("grey"))))
    mailbox = await connected(settings, server)

    (marked,) = await mailbox.poll()

    assert marked.message.id == "<uid-INBOX-7@icloud.invalid>"


# -- what a search reply says --------------------------------------------------------


@pytest.mark.parametrize("announces_empty_search", [True, False])
async def test_a_search_that_matches_nothing_imports_nothing(
    settings: Settings, announces_empty_search: bool
) -> None:
    """The completion line carries numbers - "0 msgs in 2 secs" - and none of them is a UID.

    A server need not announce a search that matched nothing, in which case that line of
    prose is the entire reply. Reading it as a result set is how mail nobody flagged gets
    fetched, run through extraction, and pushed back as "nothing imported".
    """
    server = FakeServer(
        inbox(
            (2, raw("<unflagged@airline.example>"), set()),
            (3, raw("<read@airline.example>"), {"\\Seen"}),
        ),
        announces_empty_search=announces_empty_search,
    )
    mailbox = await connected(settings, server)

    assert await mailbox.poll() == []
    assert await mailbox.count_flagged() == 0
    assert server.fetches == []


async def test_a_search_that_matches_several_returns_exactly_those(settings: Settings) -> None:
    server = FakeServer(
        inbox(
            (2, raw("<first@airline.example>"), flags("grey")),
            (3, raw("<unflagged@airline.example>"), set()),
            (30, raw("<second@airline.example>"), flags("grey")),
        )
    )
    mailbox = await connected(settings, server)

    marked = await mailbox.poll()

    assert [(item.uid, item.message.id) for item in marked] == [
        (2, "<first@airline.example>"),
        (30, "<second@airline.example>"),
    ]
    assert [uid_set for uid_set, _ in server.fetches] == ["2", "30"]


async def test_a_mailbox_with_nothing_flagged_is_never_fetched_from(settings: Settings) -> None:
    """Every mailbox is searched on every sweep, and an empty one costs one search."""
    server = FakeServer({"Travel": {1: (raw("<one@airline.example>"), flags("grey"))}})
    mailbox = await connected(settings, server)

    await mailbox.poll()

    assert [name for name, _ in server.searches] == ["INBOX", "Archive", "Travel"]
    assert server.fetches == [("1", "(BODY.PEEK[] UID FLAGS RFC822.SIZE)")]


# -- which mailboxes are swept -------------------------------------------------------


async def test_a_flag_is_found_wherever_the_message_lives(settings: Settings) -> None:
    """The whole point of a flag over a mailbox: the email never has to be moved."""
    server = FakeServer(
        {
            "INBOX": {4: (raw("<inbox@airline.example>"), flags("grey"))},
            "Archive": {9: (raw("<archived@airline.example>"), flags("grey"))},
            "Travel": {2: (raw("<filed@airline.example>"), flags("grey"))},
        }
    )
    mailbox = await connected(settings, server)

    marked = await mailbox.poll()

    assert {(item.mailbox, item.message.id) for item in marked} == {
        ("INBOX", "<inbox@airline.example>"),
        ("Archive", "<archived@airline.example>"),
        ("Travel", "<filed@airline.example>"),
    }
    assert mailbox.waiting == 3


async def test_trash_junk_drafts_and_sent_are_never_even_selected(settings: Settings) -> None:
    """A sent copy would import the same flight twice; Trash was emptied on purpose."""
    server = FakeServer(
        {name: {1: (raw(f"<{name}@airline.example>"), flags("grey"))} for name in SKIPPED}
    )
    mailbox = await connected(settings, server)

    assert await mailbox.poll() == []
    assert SKIPPED.isdisjoint(mailbox.mailboxes)
    assert SKIPPED.isdisjoint(server.selected)
    assert SKIPPED.isdisjoint(name for name, _ in server.searches)


async def test_a_mailbox_that_cannot_hold_mail_is_left_alone(settings: Settings) -> None:
    server = FakeServer(
        mailboxes=[("\\HasChildren \\Noselect", "Folders"), ("\\HasNoChildren", "INBOX")]
    )
    mailbox = await connected(settings, server)

    assert mailbox.mailboxes == ("INBOX",)


async def test_every_mailbox_is_swept_down_the_one_connection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """iCloud counts connections, not commands, so the sweep selects rather than dials."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the sweep opened a second connection")

    monkeypatch.setattr(mail, "MailBox", explode)
    server = FakeServer()
    mailbox = await connected(settings, server)
    server.selected.clear()

    await mailbox.poll()
    await mailbox.poll()

    # INBOX is already selected from signing in, so the first sweep skips re-selecting it.
    assert server.selected == ["Archive", "Travel", "INBOX", "Archive", "Travel"]


# -- waiting for the next one --------------------------------------------------------


async def test_idling_goes_back_to_the_inbox(settings: Settings) -> None:
    """IDLE announces the selected mailbox and no other, and a sweep leaves it elsewhere."""
    server = FakeServer({"Travel": {1: (raw("<one@airline.example>"), flags("grey"))}})
    mailbox = await connected(settings, server)
    await mailbox.poll()
    server.selected.clear()

    await mailbox.wait_for_mail(0.05)

    assert server.selected == ["INBOX"]


async def test_a_flagged_message_ends_the_wait_rather_than_waiting_it_out(
    settings: Settings,
) -> None:
    """Flagging a confirmation where it landed is meant to import within a second or two."""
    server = FakeServer()
    server.pushes = [b"* 3 FETCH (FLAGS (\\Flagged $MailFlagBit1 $MailFlagBit2))\r\n"]
    mailbox = await connected(settings, server)

    # Far longer than the test would tolerate if the push were not what ended it.
    await mailbox.wait_for_mail(300)


# -- which flag counts ---------------------------------------------------------------


async def test_only_the_configured_colour_is_imported(settings: Settings) -> None:
    """Every other colour is somebody else's filing system and none of our business."""
    server = FakeServer(
        inbox(
            (1, raw("<orange@airline.example>"), flags("orange")),
            (2, raw("<purple@airline.example>"), flags("purple")),
            (3, raw("<grey@airline.example>"), flags("grey")),
            (4, raw("<green@airline.example>"), flags("green")),
        )
    )
    mailbox = await connected(settings, server)

    marked = await mailbox.poll()

    assert [item.message.id for item in marked] == ["<grey@airline.example>"]


async def test_a_plain_flag_is_not_the_mark(settings: Settings) -> None:
    """Red carries no keyword, so an ordinary flag looks exactly like one. Neither counts."""
    server = FakeServer(
        inbox(
            (1, raw("<plain@airline.example>"), {FLAGGED}),
            (2, raw("<red@airline.example>"), flags("red")),
        )
    )
    mailbox = await connected(settings, server)

    assert await mailbox.poll() == []


async def test_an_unflagged_message_is_not_the_mark(settings: Settings) -> None:
    """The colour keywords mean nothing without \\Flagged, so the search asks for both."""
    server = FakeServer(inbox((1, raw("<stray@airline.example>"), flags("grey") - {FLAGGED})))
    mailbox = await connected(settings, server)

    assert await mailbox.poll() == []


@pytest.mark.parametrize("colour", sorted(FLAG_COLOURS))
async def test_every_offered_colour_matches_itself_and_nothing_else(
    settings: Settings, colour: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefs, "_current", _watching(colour))
    server = FakeServer(
        inbox(
            *(
                (uid, raw(f"<{name}@x.example>"), flags(name))
                for uid, name in enumerate(FLAG_COLOURS)
            )
        )
    )
    mailbox = await connected(settings, server)

    marked = await mailbox.poll()

    assert [item.message.id for item in marked] == [f"<{colour}@x.example>"]


async def test_a_colour_the_app_cannot_watch_for_says_so(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefs, "_current", _watching("red"))

    with pytest.raises(RuntimeError, match="not a flag colour"):
        await connected(settings, FakeServer())


async def test_nothing_is_ever_fetched_without_peeking(settings: Settings) -> None:
    """A read flag set on the phone's behalf is a change nobody asked for."""
    server = FakeServer(inbox((4, raw("<one@airline.example>"), flags("grey"))))
    mailbox = await connected(settings, server)

    await mailbox.poll()

    assert server.fetches == [("4", "(BODY.PEEK[] UID FLAGS RFC822.SIZE)")]


async def test_counting_the_flag_fetches_nothing(settings: Settings) -> None:
    """What the checks page asks: is the mark visible, and is anything waiting under it."""
    server = FakeServer(
        {
            "INBOX": {4: (raw("<one@airline.example>"), flags("grey"))},
            "Travel": {1: (raw("<two@airline.example>"), flags("grey"))},
        }
    )
    mailbox = await connected(settings, server)

    assert await mailbox.count_flagged() == 2
    assert server.fetches == []


# -- clearing the mark ---------------------------------------------------------------


async def test_clearing_a_mark_unflags_the_message_where_it_stands(settings: Settings) -> None:
    server = FakeServer({"Travel": {4: (raw("<one@airline.example>"), flags("grey"))}})
    mailbox = await connected(settings, server)
    (marked,) = await mailbox.poll()

    await mailbox.clear_mark(marked)

    assert server.stored == [
        ("Travel", "4", "-FLAGS.SILENT (\\Flagged $MailFlagBit0 $MailFlagBit1 $MailFlagBit2)")
    ]
    # Still in Travel, still the same UID, and no longer flagged at all.
    assert set(server.messages["Travel"][4][1]) == set()
    assert await mailbox.poll() == []


async def test_a_refused_store_is_raised_rather_than_swallowed(settings: Settings) -> None:
    """The mark has to stay set when it cannot be cleared, and silence would hide that."""
    server = FakeServer(inbox((4, raw("<one@airline.example>"), flags("grey"))))
    mailbox = await connected(settings, server)
    (marked,) = await mailbox.poll()
    server.rejects_store = True

    with pytest.raises(RuntimeError, match="Over quota"):
        await mailbox.clear_mark(marked)


# -- the connection ------------------------------------------------------------------


async def test_a_refused_login_is_raised_rather_than_logged(settings: Settings) -> None:
    server = FakeServer()
    server.rejects_login = True

    with pytest.raises(Exception, match="AUTHENTICATIONFAILED"):
        await connected(settings, server)


async def test_hanging_up_never_raises(settings: Settings) -> None:
    class Broken(FakeServer):
        def _reply(self, tag: str, name: str, rest: str) -> bytes:
            if name == "LOGOUT":
                raise ConnectionResetError("the server went away")
            return super()._reply(tag, name, rest)

    mailbox = await connected(settings, Broken())
    await mailbox.close()
