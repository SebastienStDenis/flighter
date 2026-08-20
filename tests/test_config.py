"""The one file the app writes, and the precedence that makes it the source of truth."""

from __future__ import annotations

import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from flighter.config import credentials_generation, get_settings, write_secrets


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A working directory of its own, since the secrets file is found relative to it."""
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_a_credential_is_live_without_a_restart(deployment: Path) -> None:
    held = get_settings()
    updated = write_secrets({"aeroapi_key": "fresh"})
    assert updated.aeroapi_key == "fresh"
    # The poller and the mail loop are holding the object from before the write.
    assert held.aeroapi_key == "fresh"


def test_writing_a_credential_twice_leaves_one_line(deployment: Path) -> None:
    write_secrets({"widget_token": "first"})
    write_secrets({"widget_token": "second"})
    written = (deployment / "data" / "secrets.env").read_text()
    assert written.count("WIDGET_TOKEN") == 1
    assert get_settings().widget_token == "second"


def test_the_file_is_not_world_readable(deployment: Path) -> None:
    write_secrets({"widget_token": "deadbeef"})
    mode = (deployment / "data" / "secrets.env").stat().st_mode
    assert not mode & (stat.S_IRGRP | stat.S_IROTH)


def test_what_was_typed_in_beats_the_environment(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container started with the old key must not undo the new one on the next boot."""
    monkeypatch.setenv("AEROAPI_KEY", "from-the-environment")
    (deployment / ".env").write_text("AEROAPI_KEY=from-dotenv\n")
    assert get_settings().aeroapi_key == "from-the-environment"

    write_secrets({"aeroapi_key": "typed-in"})
    assert get_settings().aeroapi_key == "typed-in"


def test_the_environment_still_seeds_a_credential_nobody_has_typed(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "from-the-environment")
    write_secrets({"aeroapi_key": "typed-in"})
    assert get_settings().pushover_token == "from-the-environment"


def test_clearing_a_credential_sticks(deployment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEROAPI_KEY", "from-the-environment")
    write_secrets({"aeroapi_key": ""})
    assert get_settings().aeroapi_key == ""


def test_a_changed_credential_moves_the_generation(deployment: Path) -> None:
    """What tells a client holding a login that it has to open a new one."""
    before = credentials_generation()
    write_secrets({"icloud_app_password": "xxxx-xxxx-xxxx-xxxx"})
    assert credentials_generation() > before
