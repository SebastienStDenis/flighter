"""The one file the app writes for itself."""

from __future__ import annotations

import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from flighter.config import get_settings, write_secret


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A working directory of its own, since the secrets file is found relative to it."""
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_a_minted_secret_is_live_without_a_restart(deployment: Path) -> None:
    held = get_settings()
    updated = write_secret("WIDGET_TOKEN", "deadbeef")
    assert updated.widget_token == "deadbeef"
    # The poller and the mail loop are holding the object from before the write.
    assert held.widget_token == "deadbeef"


def test_minting_a_secret_twice_leaves_one_line(deployment: Path) -> None:
    write_secret("WIDGET_TOKEN", "first")
    write_secret("WIDGET_TOKEN", "second")
    written = (deployment / "data" / "secrets.env").read_text()
    assert written.count("WIDGET_TOKEN") == 1
    assert get_settings().widget_token == "second"


def test_the_file_is_not_world_readable(deployment: Path) -> None:
    write_secret("WIDGET_TOKEN", "deadbeef")
    mode = (deployment / "data" / "secrets.env").stat().st_mode
    assert not mode & (stat.S_IRGRP | stat.S_IROTH)
