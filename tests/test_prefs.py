"""Preferences: the merge, the validation, and the defaults a fresh deployment runs on."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from flighter import prefs
from flighter.mail import FLAG_COLOURS
from flighter.models import Preferences
from flighter.prefs import Prefs


class FakeSession:
    """Holds the one row, which is all `prefs` ever touches."""

    def __init__(self, row: Preferences | None = None) -> None:
        self.row = row

    async def get(self, _model: type, _pk: Any) -> Preferences | None:
        return self.row

    def add(self, instance: Preferences) -> None:
        self.row = instance

    async def flush(self) -> None:
        return None


async def test_a_missing_row_is_created_with_the_defaults() -> None:
    session = FakeSession()
    loaded = await prefs.load(session)  # type: ignore[arg-type]
    assert loaded == Prefs()
    assert session.row is not None


async def test_saving_one_field_leaves_the_others_alone() -> None:
    session = FakeSession(Preferences(id=1, values={"imap_flag_colour": "blue"}))
    saved = await prefs.save(session, {"log_level": "DEBUG"})  # type: ignore[arg-type]
    assert saved.log_level == "DEBUG"
    assert saved.imap_flag_colour == "blue"
    assert session.row is not None
    assert session.row.values["imap_flag_colour"] == "blue"


async def test_a_saved_value_becomes_the_live_one() -> None:
    session = FakeSession()
    await prefs.save(session, {"aeroapi_monthly_cap_usd": "2.50"})  # type: ignore[arg-type]
    assert prefs.current().aeroapi_monthly_cap_usd == Decimal("2.50")


async def test_a_bad_value_is_refused_and_changes_nothing() -> None:
    """The form posts strings, so the model is the only thing standing between a typo and
    a polling budget of nonsense."""
    before = prefs.current()
    session = FakeSession()
    with pytest.raises(ValidationError):
        await prefs.save(session, {"aeroapi_rate_limit_per_minute": "lots"})  # type: ignore[arg-type]
    assert prefs.current() == before


async def test_a_trailing_slash_never_reaches_a_generated_link() -> None:
    session = FakeSession()
    saved = await prefs.save(  # type: ignore[arg-type]
        session, {"public_base_url": "https://flighter.tailnet.ts.net/"}
    )
    assert saved.public_base_url == "https://flighter.tailnet.ts.net"


async def test_the_default_flag_colour_is_one_the_app_can_tell_apart() -> None:
    """Red sets no keyword at all, so it would match every ordinary flag on the account."""
    assert Prefs().imap_flag_colour in FLAG_COLOURS
    assert "red" not in FLAG_COLOURS
