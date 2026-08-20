"""Shared fixtures.

Tests here are deliberately database-free: everything worth testing in this project is a
pure function over data (timezone normalisation, poll cadence, snapshot diffing, payload
shaping), and a suite that needs a live Postgres is a suite that stops being run.
"""

from __future__ import annotations

import pytest

from flighter import prefs
from flighter.config import Settings
from flighter.prefs import Prefs


@pytest.fixture
def settings() -> Settings:
    return Settings(
        aeroapi_key="test-key",
        widget_token="test-token",
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_refresh_token="refresh-token",
    )


@pytest.fixture(autouse=True)
def preferences(monkeypatch: pytest.MonkeyPatch) -> Prefs:
    """A fully configured deployment, installed as the live preferences.

    Autouse because the modules under test read the live row rather than being handed
    one, and a developer's own database must never be what a test asserts against.
    """
    configured = Prefs(
        public_base_url="https://flights.example.com",
        ntfy_topic="flights",
        gcal_calendar_id="cal-id",
    )
    monkeypatch.setattr(prefs, "_current", configured)
    return configured
