"""The IMAP layer, against a fake server: no network, no iCloud, no database.

The mailbox is somebody else's, so the properties worth pinning down are the ones that
decide whether an email is looked at twice or never at all: where the cursor is, when it
moves, and what happens when Apple renumbers the folder underneath us.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aioimaplib import Response

from flighter import mail
from flighter.config import Settings
from flighter.mail import Mailbox, parse_message
from flighter.models import KV

FIXTURES = Path(__file__).parent / "fixtures"

UIDVALIDITY = 4242


def _days_ago(days: int) -> str:
    """The same date the mailbox spells out in a SINCE search."""
    day = datetime.now(UTC).date() - timedelta(days=days)
    return f"{day.day:02d}-{day.strftime('%b')}-{day.year}"


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

    def __init__(self, messages: dict[int, bytes], uidvalidity: int = UIDVALIDITY) -> None:
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.searches: list[str] = []
        self.fetches: list[tuple[str, str]] = []
        self.logged_out = False

    async def login(self, user: str, password: str) -> Response:
        return Response("OK", [b"LOGIN completed"])

    async def logout(self) -> Response:
        self.logged_out = True
        return Response("OK", [b"LOGOUT completed"])

    async def select(self, folder: str) -> Response:
        return Response(
            "OK",
            [
                f"{len(self.messages)} EXISTS".encode(),
                f"OK [UIDVALIDITY {self.uidvalidity}] UIDs valid".encode(),
                f"OK [UIDNEXT {self._uidnext()}] Predicted next UID".encode(),
            ],
        )

    async def uid_search(self, criteria: str, charset: str | None = None) -> Response:
        self.searches.append(criteria)
        return Response("OK", [" ".join(str(uid) for uid in self._match(criteria)).encode()])

    async def uid(self, command: str, uid_set: str, parts: str) -> Response:
        assert command == "fetch"
        self.fetches.append((uid_set, parts))
        lines: list[Any] = []
        for sequence, uid in enumerate(int(value) for value in uid_set.split(",")):
            payload = self._payload(uid, parts)
            lines.append(f"{sequence + 1} FETCH (UID {uid} BODY[] {{{len(payload)}}}".encode())
            lines.append(bytearray(payload))
            lines.append(b")")
        lines.append(b"FETCH completed.")
        return Response("OK", lines)

    def _uidnext(self) -> int:
        return max(self.messages, default=0) + 1

    def _match(self, criteria: str) -> list[int]:
        if criteria.startswith("SINCE"):
            return sorted(self.messages)
        # `n:*` is the range between n and the highest UID in the folder, so a server
        # answers it with the newest message even when that message is older than n.
        low = int(criteria.removeprefix("UID ").partition(":")[0])
        matched = {uid for uid in self.messages if uid >= low}
        if self.messages:
            matched.add(max(self.messages))
        return sorted(matched)

    def _payload(self, uid: int, parts: str) -> bytes:
        body = self.messages[uid]
        if "HEADER.FIELDS" not in parts:
            return body
        header = [line for line in body.split(b"\r\n") if line.startswith(b"Message-ID:")]
        return b"\r\n".join([*header, b"", b""])


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Enough AsyncSession for the cursor row and the ingest log lookup."""

    def __init__(self, cursor: dict[str, int] | None = None, ingested: list[str] | None = None):
        self.cursor = cursor
        self.ingested = ingested or []

    async def get(self, model: type, key: str) -> Any:
        assert model is KV
        return None if self.cursor is None else KV(key=key, value=self.cursor)

    async def merge(self, row: Any) -> Any:
        assert isinstance(row, KV)
        self.cursor = row.value
        return row

    async def execute(self, statement: Any) -> FakeResult:
        return FakeResult(list(self.ingested))


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

    (message,) = await mailbox.poll(FakeSession())  # type: ignore[arg-type]

    assert message.id == f"<uid-{UIDVALIDITY}-7@icloud.invalid>"


# -- the cursor ----------------------------------------------------------------------


async def test_a_first_run_seeds_from_recent_mail(settings: Settings) -> None:
    server = FakeIMAP({4: raw("<one@airline.example>"), 5: raw("<two@airline.example>")})
    mailbox = await connected(settings, server)
    session = FakeSession()

    messages = await mailbox.poll(session)  # type: ignore[arg-type]

    assert [message.id for message in messages] == [
        "<one@airline.example>",
        "<two@airline.example>",
    ]
    assert server.searches == [f"SINCE {_days_ago(mail.RESCAN_DAYS)}"]


async def test_the_cursor_only_moves_once_the_batch_is_committed(settings: Settings) -> None:
    server = FakeIMAP({4: raw("<one@airline.example>")})
    mailbox = await connected(settings, server)
    session = FakeSession()

    await mailbox.poll(session)  # type: ignore[arg-type]
    assert session.cursor is None

    await mailbox.commit_cursor(session)  # type: ignore[arg-type]
    assert session.cursor == {"uidvalidity": UIDVALIDITY, "last_uid": 4}


async def test_an_empty_folder_still_anchors_the_cursor(settings: Settings) -> None:
    """Otherwise the next poll reads `1:*` and hands the whole mailbox to the pipeline."""
    server = FakeIMAP({})
    mailbox = await connected(settings, server)
    session = FakeSession()

    assert await mailbox.poll(session) == []  # type: ignore[arg-type]
    await mailbox.commit_cursor(session)  # type: ignore[arg-type]
    assert session.cursor == {"uidvalidity": UIDVALIDITY, "last_uid": 0}


async def test_only_uids_above_the_cursor_are_ingested(settings: Settings) -> None:
    """A server answers `n:*` with the newest message whatever n is, so it is filtered."""
    server = FakeIMAP({4: raw("<one@airline.example>")})
    mailbox = await connected(settings, server)
    session = FakeSession(cursor={"uidvalidity": UIDVALIDITY, "last_uid": 4})

    assert await mailbox.poll(session) == []  # type: ignore[arg-type]
    assert server.searches == ["UID 5:*"]
    assert server.fetches == []


async def test_a_changed_uidvalidity_forces_a_rescan(settings: Settings) -> None:
    """The stored UID means nothing under a new UIDVALIDITY, so it must not be trusted."""
    server = FakeIMAP({1: raw("<one@airline.example>"), 2: raw("<two@airline.example>")})
    mailbox = await connected(settings, server)
    session = FakeSession(cursor={"uidvalidity": UIDVALIDITY - 1, "last_uid": 9999})

    messages = await mailbox.poll(session)  # type: ignore[arg-type]

    assert [message.id for message in messages] == [
        "<one@airline.example>",
        "<two@airline.example>",
    ]
    assert server.searches[0].startswith("SINCE ")
    await mailbox.commit_cursor(session)  # type: ignore[arg-type]
    assert session.cursor == {"uidvalidity": UIDVALIDITY, "last_uid": 2}


# -- fetching ------------------------------------------------------------------------


async def test_mail_already_in_the_ingest_log_is_not_downloaded_again(settings: Settings) -> None:
    server = FakeIMAP({4: raw("<one@airline.example>"), 5: raw("<two@airline.example>")})
    mailbox = await connected(settings, server)
    session = FakeSession(ingested=["<one@airline.example>"])

    messages = await mailbox.poll(session)  # type: ignore[arg-type]

    assert [message.id for message in messages] == ["<two@airline.example>"]
    # The headers of both, then the body of the one that is new.
    assert server.fetches == [
        ("4,5", "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"),
        ("5", "(BODY.PEEK[])"),
    ]


async def test_nothing_is_ever_fetched_without_peeking(settings: Settings) -> None:
    """A read flag set on the phone's behalf is a change to somebody else's mailbox."""
    server = FakeIMAP({4: raw("<one@airline.example>")})
    mailbox = await connected(settings, server)

    await mailbox.poll(FakeSession())  # type: ignore[arg-type]

    assert server.fetches
    assert all("BODY.PEEK" in parts for _, parts in server.fetches)


async def test_a_backfill_leaves_the_cursor_alone(settings: Settings) -> None:
    server = FakeIMAP({4: raw("<one@airline.example>")})
    mailbox = await connected(settings, server)
    session = FakeSession(cursor={"uidvalidity": UIDVALIDITY, "last_uid": 99})

    messages = await mailbox.backfill(session, days=30)  # type: ignore[arg-type]

    assert [message.id for message in messages] == ["<one@airline.example>"]
    await mailbox.commit_cursor(session)  # type: ignore[arg-type]
    assert session.cursor == {"uidvalidity": UIDVALIDITY, "last_uid": 99}


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
