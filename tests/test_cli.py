"""The command line: what each subcommand parses to, and what it does with it.

Nothing here opens a database, a mailbox or a socket. Every command's real work is
stubbed at the module it imports from, so what is left under test is the parsing, the
printing and the exit code - which is the half a person actually types.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from flighter import airports, app, checks, cli, db, ingest, poller
from flighter.checks import CheckResult
from flighter.config import Settings


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """No engine, no logging of its own, and the test's credentials rather than a .env."""

    @contextlib.asynccontextmanager
    async def database(_settings: Settings) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(cli, "_database", database)
    monkeypatch.setattr(cli, "_configure_logging", lambda level: None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


@pytest.fixture
def no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session scope that yields nothing, for the commands that only pass one along."""

    @contextlib.asynccontextmanager
    async def scope() -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(db, "session_scope", scope)


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main([])
    assert refused.value.code == 2


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(["fly"])
    assert refused.value.code == 2


def test_serve_runs_uvicorn_on_the_loopback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    served: dict[str, Any] = {}

    monkeypatch.setattr(app, "create_app", lambda settings: "the app")
    monkeypatch.setattr("uvicorn.run", lambda application, **kwargs: served.update(kwargs))

    assert cli.main(["serve"]) == 0
    assert (served["host"], served["port"]) == ("127.0.0.1", 8000)


def test_serve_takes_a_host_and_a_port(monkeypatch: pytest.MonkeyPatch) -> None:
    served: dict[str, Any] = {}

    monkeypatch.setattr(app, "create_app", lambda settings: "the app")
    monkeypatch.setattr("uvicorn.run", lambda application, **kwargs: served.update(kwargs))

    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9000"]) == 0
    assert (served["host"], served["port"]) == ("0.0.0.0", 9000)


def test_migrate_brings_the_schema_up(monkeypatch: pytest.MonkeyPatch) -> None:
    upgraded = []
    monkeypatch.setattr(app, "migrate", lambda: upgraded.append(True))

    assert cli.main(["migrate"]) == 0
    assert upgraded == [True]


def test_seed_airports_reports_what_it_wrote(
    monkeypatch: pytest.MonkeyPatch, no_session: None, capsys: pytest.CaptureFixture[str]
) -> None:
    async def seed(session: Any) -> int:
        return 4

    monkeypatch.setattr(airports, "seed_airports", seed)

    assert cli.main(["seed-airports"]) == 0
    assert "seeded 4 airports" in capsys.readouterr().out


def test_import_sweeps_every_mailbox_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def sweep(*, settings: Settings) -> list[str]:
        return ["created", "no_flight"]

    monkeypatch.setattr(ingest, "import_flagged", sweep)

    assert cli.main(["import"]) == 0
    assert "imported 2 message(s)" in capsys.readouterr().out


def test_poll_runs_one_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def once() -> int:
        return 3

    monkeypatch.setattr(poller, "poll_once", once)

    assert cli.main(["poll"]) == 0
    assert "polled 3 bookings" in capsys.readouterr().out


def test_check_exits_zero_when_everything_answers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run(settings: Settings) -> list[CheckResult]:
        return [CheckResult("database", True, "4 airports seeded")]

    monkeypatch.setattr(checks, "run_checks", run)

    assert cli.main(["check"]) == 0
    assert "[ok  ] database: 4 airports seeded" in capsys.readouterr().out


def test_check_exits_nonzero_and_names_the_broken_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exit code is what a cron job or a person's `&&` reads."""

    async def run(settings: Settings) -> list[CheckResult]:
        return [
            CheckResult("database", True, "4 airports seeded"),
            CheckResult("aeroapi", False, "key rejected"),
        ]

    monkeypatch.setattr(checks, "run_checks", run)

    assert cli.main(["check"]) == 1
    assert "[FAIL] aeroapi: key rejected" in capsys.readouterr().out
