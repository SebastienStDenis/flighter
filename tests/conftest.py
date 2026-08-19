"""Shared fixtures.

Tests here are deliberately database-free: everything worth testing in this project is a
pure function over data (timezone normalisation, poll cadence, snapshot diffing, payload
shaping), and a suite that needs a live Postgres is a suite that stops being run.
"""

from __future__ import annotations

import pytest

from flight_tracker.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        public_base_url="https://flights.example.com",
        aeroapi_key="test-key",
        widget_token="test-token",
        ntfy_topic="flights",
        gcal_calendar_id="cal-id",
    )
