"""Shared fixtures.

Tests here are deliberately database-free: everything worth testing in this project is a
pure function over data (timezone normalisation, poll cadence, snapshot diffing, payload
shaping), and a suite that needs a database on disk is a suite that stops being run.
"""

from __future__ import annotations

import pytest

from flighter import prefs
from flighter.config import CREDENTIALS, Settings
from flighter.prefs import Prefs

# Every credential named explicitly, so that a developer's own .env or data/secrets.env
# can never be what a test is really asserting against.
BLANK = dict.fromkeys((*CREDENTIALS, "widget_token"), "")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        **BLANK
        | {
            "aeroapi_key": "test-key",
            "widget_token": "test-token",
            "icloud_email": "someone@icloud.com",
            "icloud_app_password": "abcd-efgh-ijkl-mnop",
            "pushover_token": "app-token",
            "pushover_user_key": "user-key",
        }
    )


@pytest.fixture
def unconfigured() -> Settings:
    """A deployment on its first boot, with nothing entered anywhere."""
    return Settings(**BLANK)


@pytest.fixture(autouse=True)
def preferences(monkeypatch: pytest.MonkeyPatch) -> Prefs:
    """A fully configured deployment, installed as the live preferences.

    Autouse because the modules under test read the live row rather than being handed
    one, and a developer's own database must never be what a test asserts against.
    """
    configured = Prefs(
        public_base_url="https://flights.example.com",
        icloud_calendar_url="https://p34-caldav.icloud.com/12345/calendars/6c1f4f0e-flights/",
    )
    monkeypatch.setattr(prefs, "_current", configured)
    return configured
