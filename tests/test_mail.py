"""The IMAP layer, against a fake server: no network, no iCloud, no database.

The mailbox is somebody else's, so the properties worth pinning down are the ones that
decide whether an email is imported twice or never at all: which mailboxes are searched
and which are deliberately not, that only the configured colour counts, that a body is
never fetched without peeking, and that taking the flag off is the one write we ever make.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aioimaplib import Response

from flighter import mail, prefs
from flighter.config import Settings
from flighter.mail import FLAG_COLOURS, FLAG_KEYWORDS, Mailbox, parse_message

FIXTURES = Path(__file__).parent / "fixtures"

UIDVALIDITY = 4242
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


class FakeIMAP:
    """Enough of an IMAP server to drive a Mailbox, and a record of what it was asked."""

    def __init__(
        self,
        messages: dict[str, dict[int, tuple[bytes, set[str]]]] | None = None,
        mailboxes: list[tuple[str, str]] | None = None,
    ) -> None:
        self.messages = messages if messages is not None else {}
        # (attributes, name), as a LIST reply spells them.
        self.mailboxes = mailboxes if mailboxes is not None else _DEFAULT_MAILBOXES
        self.selected: list[str] = []
        self.searches: list[tuple[str, str]] = []
        self.fetches: list[tuple[str, str]] = []
        self.stored: list[tuple[str, str, str]] = []
        self.logged_out = False
        self._selected = ""

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

    async def select(self, mailbox: str) -> Response:
        self.selected.append(mailbox)
        self._selected = mailbox.strip('"')
        return Response(
            "OK",
            [
                f"{len(self._held())} EXISTS".encode(),
                f"OK [UIDVALIDITY {UIDVALIDITY}] UIDs valid".encode(),
            ],
        )

    async def uid_search(self, criteria: str, charset: str | None = None) -> Response:
        self.searches.append((self._selected, criteria))
        hits = [uid for uid, (_, held) in sorted(self._held().items()) if _matches(held, criteria)]
        return Response("OK", [" ".join(str(uid) for uid in hits).encode()])

    async def uid(self, command: str, *args: str) -> Response:
        if command == "store":
            uid_set, instruction = args
            self.stored.append((self._selected, uid_set, instruction))
            removed = set(instruction[instruction.index("(") + 1 : instruction.index(")")].split())
            for value in uid_set.split(","):
                self._held()[int(value)][1].difference_update(removed)
            return Response("OK", [b"STORE completed."])

        assert command == "fetch", f"nothing should ever issue UID {command.upper()}"
        uid_set, parts = args
        self.fetches.append((uid_set, parts))
        lines: list[Any] = []
        for sequence, uid in enumerate(int(value) for value in uid_set.split(",")):
            payload = self._held()[uid][0]
            lines.append(f"{sequence + 1} FETCH (UID {uid} BODY[] {{{len(payload)}}}".encode())
            lines.append(bytearray(payload))
            lines.append(b")")
        lines.append(b"FETCH completed.")
        return Response("OK", lines)

    async def idle_start(self, timeout: float) -> Any:
        return asyncio.sleep(0)

    async def wait_server_push(self, timeout: float) -> Any:
        return []

    def idle_done(self) -> None:
        return None

    def _held(self) -> dict[int, tuple[bytes, set[str]]]:
        return self.messages.setdefault(self._selected, {})


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
    body = b"Subject: Your itinerary\r\nFrom: a@b.example\r\n\r\nSeat 12A.\r\n"
    server = FakeIMAP(inbox((7, body, flags("grey"))))
    mailbox = await connected(settings, server)

    (marked,) = await mailbox.poll()

    assert marked.message.id == f"<uid-{UIDVALIDITY}-7@icloud.invalid>"


# -- which mailboxes are swept -------------------------------------------------------


async def test_a_flag_is_found_wherever_the_message_lives(settings: Settings) -> None:
    """The whole point of a flag over a mailbox: the email never has to be moved."""
    server = FakeIMAP(
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
    server = FakeIMAP(
        {name: {1: (raw(f"<{name}@airline.example>"), flags("grey"))} for name in SKIPPED}
    )
    mailbox = await connected(settings, server)

    assert await mailbox.poll() == []
    assert SKIPPED.isdisjoint(mailbox.mailboxes)
    assert SKIPPED.isdisjoint(name.strip('"') for name in server.selected)


async def test_a_mailbox_that_cannot_hold_mail_is_left_alone(settings: Settings) -> None:
    server = FakeIMAP(
        mailboxes=[("\\HasChildren \\Noselect", "Folders"), ("\\HasNoChildren", "INBOX")]
    )
    mailbox = await connected(settings, server)

    assert mailbox.mailboxes == ("INBOX",)


async def test_every_mailbox_is_swept_down_the_one_connection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """iCloud counts connections, not commands, so the sweep selects rather than dials."""

    def explode(**kwargs: Any) -> None:
        raise AssertionError("the sweep opened a second connection")

    monkeypatch.setattr(mail, "IMAP4_SSL", explode)
    server = FakeIMAP()
    mailbox = await connected(settings, server)
    server.selected.clear()

    await mailbox.poll()
    await mailbox.poll()

    # INBOX is already selected from connecting, so the first sweep skips re-selecting it.
    assert [name.strip('"') for name in server.selected] == [
        "Archive",
        "Travel",
        "INBOX",
        "Archive",
        "Travel",
    ]


async def test_idling_goes_back_to_the_inbox(settings: Settings) -> None:
    """IDLE announces the selected mailbox and no other, and a sweep leaves it elsewhere."""
    server = FakeIMAP({"Travel": {1: (raw("<one@airline.example>"), flags("grey"))}})
    mailbox = await connected(settings, server)
    await mailbox.poll()
    server.selected.clear()

    await mailbox.wait_for_mail(0.01)

    assert [name.strip('"') for name in server.selected] == ["INBOX"]


# -- which flag counts ---------------------------------------------------------------


async def test_only_the_configured_colour_is_imported(settings: Settings) -> None:
    """Every other colour is somebody else's filing system and none of our business."""
    server = FakeIMAP(
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
    server = FakeIMAP(
        inbox(
            (1, raw("<plain@airline.example>"), {FLAGGED}),
            (2, raw("<red@airline.example>"), flags("red")),
        )
    )
    mailbox = await connected(settings, server)

    assert await mailbox.poll() == []


async def test_an_unflagged_message_is_not_the_mark(settings: Settings) -> None:
    """The colour keywords mean nothing without \\Flagged, so the search asks for both."""
    server = FakeIMAP(inbox((1, raw("<stray@airline.example>"), flags("grey") - {FLAGGED})))
    mailbox = await connected(settings, server)

    assert await mailbox.poll() == []


@pytest.mark.parametrize("colour", sorted(FLAG_COLOURS))
async def test_every_offered_colour_matches_itself_and_nothing_else(
    settings: Settings, colour: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefs, "_current", _watching(colour))
    server = FakeIMAP(
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
        await connected(settings, FakeIMAP())


async def test_nothing_is_ever_fetched_without_peeking(settings: Settings) -> None:
    """A read flag set on the phone's behalf is a change nobody asked for."""
    server = FakeIMAP(inbox((4, raw("<one@airline.example>"), flags("grey"))))
    mailbox = await connected(settings, server)

    await mailbox.poll()

    assert server.fetches == [("4", "(BODY.PEEK[])")]


async def test_counting_the_flag_fetches_nothing(settings: Settings) -> None:
    """What the checks page asks: is the mark visible, and is anything waiting under it."""
    server = FakeIMAP(
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
    server = FakeIMAP({"Travel": {4: (raw("<one@airline.example>"), flags("grey"))}})
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

    class Refusing(FakeIMAP):
        async def uid(self, command: str, *args: str) -> Response:
            if command == "store":
                return Response("NO", [b"Over quota"])
            return await super().uid(command, *args)

    server = Refusing(inbox((4, raw("<one@airline.example>"), flags("grey"))))
    mailbox = await connected(settings, server)
    (marked,) = await mailbox.poll()

    with pytest.raises(RuntimeError, match="Over quota"):
        await mailbox.clear_mark(marked)


# -- the connection ------------------------------------------------------------------


async def test_a_refused_login_is_raised_rather_than_logged(settings: Settings) -> None:
    class Refusing(FakeIMAP):
        async def login(self, user: str, password: str) -> Response:
            return Response("NO", [b"[AUTHENTICATIONFAILED] Authentication failed"])

    with pytest.raises(RuntimeError, match="AUTHENTICATIONFAILED"):
        await connected(settings, Refusing())


async def test_hanging_up_never_raises(settings: Settings) -> None:
    class Broken(FakeIMAP):
        async def logout(self) -> Response:
            raise ConnectionResetError("the server went away")

    mailbox = await connected(settings, Broken())
    await mailbox.close()
