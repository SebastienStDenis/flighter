"""The IMAP layer, against a fake server: no network, no iCloud, no database.

The mailbox is somebody else's, so the properties worth pinning down are the ones that
decide whether an email is imported twice or never at all: what the folder is taken to
hold, that a body is never fetched without peeking, and that clearing a mark is the one
write we ever make.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aioimaplib import Response

from flighter.config import Settings
from flighter.mail import Mailbox, parse_message

FIXTURES = Path(__file__).parent / "fixtures"

UIDVALIDITY = 4242
FOLDER = "flighter"


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


class FakeIMAP:
    """Enough of an IMAP server to drive a Mailbox, and a record of what it was asked."""

    def __init__(
        self,
        messages: dict[int, bytes],
        uidvalidity: int = UIDVALIDITY,
        mailboxes: list[tuple[str, str]] | None = None,
    ) -> None:
        self.messages = messages
        self.uidvalidity = uidvalidity
        # (attributes, name), as a LIST reply spells them.
        self.mailboxes = mailboxes if mailboxes is not None else _DEFAULT_MAILBOXES
        self.selected: list[str] = []
        self.searches: list[str] = []
        self.fetches: list[tuple[str, str]] = []
        self.moved: list[tuple[str, str]] = []
        self.logged_out = False

    async def login(self, user: str, password: str) -> Response:
        return Response("OK", [b"LOGIN completed"])

    async def logout(self) -> Response:
        self.logged_out = True
        return Response("OK", [b"LOGOUT completed"])

    async def list(self, reference_name: str, mailbox_pattern: str) -> Response:
        lines: list[Any] = [
            f'({attributes}) "/" "{name}"'.encode() for attributes, name in self.mailboxes
        ]
        lines.append(b"LIST completed.")
        return Response("OK", lines)

    async def select(self, folder: str) -> Response:
        self.selected.append(folder)
        return Response(
            "OK",
            [
                f"{len(self.messages)} EXISTS".encode(),
                f"OK [UIDVALIDITY {self.uidvalidity}] UIDs valid".encode(),
            ],
        )

    async def uid_search(self, criteria: str, charset: str | None = None) -> Response:
        self.searches.append(criteria)
        return Response("OK", [" ".join(str(uid) for uid in sorted(self.messages)).encode()])

    async def uid(self, command: str, *args: str) -> Response:
        if command == "move":
            uid_set, mailbox = args
            self.moved.append((uid_set, mailbox))
            for value in uid_set.split(","):
                self.messages.pop(int(value), None)
            return Response("OK", [b"MOVE completed."])

        assert command == "fetch"
        uid_set, parts = args
        self.fetches.append((uid_set, parts))
        lines: list[Any] = []
        for sequence, uid in enumerate(int(value) for value in uid_set.split(",")):
            payload = self.messages[uid]
            lines.append(f"{sequence + 1} FETCH (UID {uid} BODY[] {{{len(payload)}}}".encode())
            lines.append(bytearray(payload))
            lines.append(b")")
        lines.append(b"FETCH completed.")
        return Response("OK", lines)


_DEFAULT_MAILBOXES = [
    ("\\HasNoChildren", "INBOX"),
    ("\\HasNoChildren \\Archive", "Archive"),
    ("\\HasNoChildren \\Trash", "Deleted Messages"),
    ("\\HasNoChildren", FOLDER),
]


async def connected(settings: Settings, server: FakeIMAP) -> Mailbox:
    mailbox = Mailbox(settings, client=server)
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
    server = FakeIMAP({7: b"Subject: Your itinerary\r\nFrom: a@b.example\r\n\r\nSeat 12A.\r\n"})
    mailbox = await connected(settings, server)

    (marked,) = await mailbox.poll()

    assert marked.message.id == f"<uid-{UIDVALIDITY}-7@icloud.invalid>"


# -- the folder ----------------------------------------------------------------------


async def test_the_import_folder_is_the_only_one_selected(settings: Settings) -> None:
    """Nothing else is looked at, which is the whole point of marking a message."""
    server = FakeIMAP({4: raw("<one@airline.example>")})
    await connected(settings, server)

    assert server.selected == [f'"{FOLDER}"']


async def test_a_missing_folder_says_what_to_do_about_it(settings: Settings) -> None:
    server = FakeIMAP({}, mailboxes=[("\\HasNoChildren", "INBOX")])

    with pytest.raises(RuntimeError, match="no mailbox called flighter"):
        await connected(settings, server)


async def test_everything_in_the_folder_is_pending(settings: Settings) -> None:
    server = FakeIMAP({4: raw("<one@airline.example>"), 5: raw("<two@airline.example>")})
    mailbox = await connected(settings, server)

    marked = await mailbox.poll()

    assert [item.message.id for item in marked] == [
        "<one@airline.example>",
        "<two@airline.example>",
    ]
    assert server.searches == ["ALL"]
    assert mailbox.waiting == 2


async def test_an_empty_folder_is_no_work(settings: Settings) -> None:
    server = FakeIMAP({})
    mailbox = await connected(settings, server)

    assert await mailbox.poll() == []
    assert server.fetches == []


async def test_nothing_is_ever_fetched_without_peeking(settings: Settings) -> None:
    """A read flag set on the phone's behalf is a change nobody asked for."""
    server = FakeIMAP({4: raw("<one@airline.example>")})
    mailbox = await connected(settings, server)

    await mailbox.poll()

    assert server.fetches == [("4", "(BODY.PEEK[])")]


# -- clearing the mark ---------------------------------------------------------------


async def test_clearing_a_mark_moves_the_message_to_the_archive(settings: Settings) -> None:
    server = FakeIMAP({4: raw("<one@airline.example>")})
    mailbox = await connected(settings, server)

    await mailbox.clear_mark(4)

    assert server.moved == [("4", '"Archive"')]
    assert await mailbox.poll() == []


async def test_the_archive_is_found_by_its_attribute_not_its_name(settings: Settings) -> None:
    """A French account calls it Archive too, but a renamed one does not."""
    server = FakeIMAP(
        {4: raw("<one@airline.example>")},
        mailboxes=[
            ("\\HasNoChildren", "INBOX"),
            ("\\HasNoChildren \\Archive", "Archives"),
            ("\\HasNoChildren", FOLDER),
        ],
    )
    mailbox = await connected(settings, server)

    await mailbox.clear_mark(4)

    assert server.moved == [("4", '"Archives"')]


async def test_an_account_with_no_archive_falls_back_to_the_inbox(settings: Settings) -> None:
    server = FakeIMAP(
        {4: raw("<one@airline.example>")},
        mailboxes=[("\\HasNoChildren", "INBOX"), ("\\HasNoChildren", FOLDER)],
    )
    mailbox = await connected(settings, server)

    await mailbox.clear_mark(4)

    assert server.moved == [("4", '"INBOX"')]


async def test_a_refused_move_is_raised_rather_than_swallowed(settings: Settings) -> None:
    """The mark has to stay set when it cannot be cleared, and silence would hide that."""

    class Refusing(FakeIMAP):
        async def uid(self, command: str, *args: str) -> Response:
            if command == "move":
                return Response("NO", [b"Over quota"])
            return await super().uid(command, *args)

    mailbox = await connected(settings, Refusing({4: raw("<one@airline.example>")}))

    with pytest.raises(RuntimeError, match="Over quota"):
        await mailbox.clear_mark(4)


# -- the connection ------------------------------------------------------------------


async def test_a_refused_login_is_raised_rather_than_logged(settings: Settings) -> None:
    class Refusing(FakeIMAP):
        async def login(self, user: str, password: str) -> Response:
            return Response("NO", [b"[AUTHENTICATIONFAILED] Authentication failed"])

    with pytest.raises(RuntimeError, match="AUTHENTICATIONFAILED"):
        await connected(settings, Refusing({}))


async def test_hanging_up_never_raises(settings: Settings) -> None:
    class Broken(FakeIMAP):
        async def logout(self) -> Response:
            raise ConnectionResetError("the server went away")

    mailbox = await connected(settings, Broken({}))
    await mailbox.close()
