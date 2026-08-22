"""Preferences: the merge, the validation, and the defaults a fresh deployment runs on."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from flighter import prefs
from flighter.mail import FLAG_COLOURS
from flighter.models import KV, Preferences
from flighter.prefs import Prefs

LAN = "http://192.168.1.20:8586"


class FakeSession:
    """Holds the one row and the KV entries, which is all `prefs` ever touches."""

    def __init__(self, row: Preferences | None = None) -> None:
        self.row = row
        self.kv: dict[str, KV] = {}

    async def get(self, model: type, pk: Any) -> Any:
        if model is KV:
            return self.kv.get(pk)
        return self.row

    def add(self, instance: Preferences) -> None:
        self.row = instance

    async def merge(self, instance: KV) -> KV:
        self.kv[instance.key] = instance
        return instance

    async def flush(self) -> None:
        return None


async def test_a_missing_row_is_created_with_the_defaults() -> None:
    session = FakeSession()
    loaded = await prefs.load(session)  # type: ignore[arg-type]
    assert loaded == Prefs()
    assert session.row is not None


def test_friend_integrations_are_opt_in() -> None:
    defaults = Prefs()
    assert not defaults.sync_friend_flights_to_calendar
    assert not defaults.notify_for_friend_flights
    assert not defaults.show_friend_flights_in_widget


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
        await prefs.save(session, {"aeroapi_monthly_cap_usd": "lots"})  # type: ignore[arg-type]
    assert prefs.current() == before


async def test_a_trailing_slash_never_reaches_a_generated_link() -> None:
    session = FakeSession()
    saved = await prefs.save(  # type: ignore[arg-type]
        session, {"public_base_url": "https://flighter.tailnet.ts.net/"}
    )
    assert saved.public_base_url == "https://flighter.tailnet.ts.net"


def test_an_unsaved_address_gives_way_to_the_one_the_request_came_in_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prefs, "_current", Prefs())
    assert prefs.public_base_url("https://flighter.tailnet.ts.net") == (
        "https://flighter.tailnet.ts.net"
    )


def test_a_saved_address_is_kept_whatever_the_request_came_in_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prefs, "_current", Prefs(public_base_url="https://flights.example.com"))
    monkeypatch.setattr(prefs, "_last_seen_origin", LAN)
    assert prefs.public_base_url("http://192.168.1.20:8000") == "https://flights.example.com"
    assert prefs.public_base_url() == "https://flights.example.com"


def test_with_no_request_in_hand_the_last_address_seen_stands_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poller and the mail import write links without a request to borrow from."""
    monkeypatch.setattr(prefs, "_current", Prefs())
    monkeypatch.setattr(prefs, "_last_seen_origin", LAN)
    assert prefs.public_base_url() == LAN
    assert prefs.public_base_url("https://flighter.tailnet.ts.net") == (
        "https://flighter.tailnet.ts.net"
    )


def test_the_default_is_the_answer_only_before_any_request_has_arrived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prefs, "_current", Prefs())
    assert prefs.public_base_url() == "http://localhost:8000"


async def test_the_last_address_seen_survives_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    await prefs.remember_origin(session, LAN)  # type: ignore[arg-type]
    assert prefs.last_seen_origin() == LAN

    monkeypatch.setattr(prefs, "_last_seen_origin", None)
    await prefs.load(session)  # type: ignore[arg-type]
    assert prefs.last_seen_origin() == LAN


async def test_the_default_flag_colour_is_one_the_app_can_tell_apart() -> None:
    """Red sets no keyword at all, so it would match every ordinary flag on the account."""
    assert Prefs().imap_flag_colour in FLAG_COLOURS
    assert "red" not in FLAG_COLOURS
