"""Credentials, and nothing else.

Configuration here splits in two, and no value lives in both halves. A *credential* is
set once by hand, is never handed back out by the UI, and is read from the environment.
Everything else is a *preference*: it has a working default, it is edited on the
settings page, and the database is the only place it lives - see `prefs`.

The app writes exactly one file: `data/secrets.env`, holding the credentials it mints
itself rather than asking a person for - the Google refresh token the consent flow
returns, and the widget token generated on first boot. Those keys never appear in
`.env`, so there is still one home per value and never a precedence question.
"""

from __future__ import annotations

import os
import secrets
import stat
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Relative on purpose: the image's WORKDIR is /app and the volume is mounted at
# /app/data, so the same default is the volume in Docker and ./data in a checkout.
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
SECRETS_FILE = DATA_DIR / "secrets.env"

# The app-written file is listed last because pydantic-settings lets the last file win,
# and a token the app minted is newer than anything a hand-edited file knows.
ENV_FILES = (".env", SECRETS_FILE)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILES, env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://flights:flights@db:5432/flights"

    # --- FlightAware -----------------------------------------------------------------
    aeroapi_key: str = Field(default="", repr=False)

    # --- Anthropic, the extraction fallback ------------------------------------------
    anthropic_api_key: str = Field(default="", repr=False)

    # --- Google, one client for Gmail and Calendar alike -----------------------------
    # Both APIs sit in one Cloud project behind one consent screen, so asking for two
    # clients only ever bought two ways to paste the wrong string.
    google_client_id: str = ""
    google_client_secret: str = Field(default="", repr=False)
    # Minted by the consent flow at /settings/google/connect, not typed by anyone.
    google_refresh_token: str = Field(default="", repr=False)

    # --- ntfy -------------------------------------------------------------------------
    ntfy_token: str = Field(default="", repr=False)

    # --- Widget -----------------------------------------------------------------------
    # Generated on first boot. The only authentication in front of the flight data.
    widget_token: str = Field(default="", repr=False)

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def google_connected(self) -> bool:
        return bool(self.google_configured and self.google_refresh_token)

    @property
    def aeroapi_configured(self) -> bool:
        return bool(self.aeroapi_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def write_secret(key: str, value: str) -> Settings:
    """Persist one app-minted credential and make it live in this process.

    Written whole rather than appended so re-authorising replaces the dead token instead
    of leaving two lines and letting the parser pick.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lines = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            name, _, existing = line.partition("=")
            if name.strip():
                lines[name.strip()] = existing
    lines[key.upper()] = value
    SECRETS_FILE.write_text("".join(f"{name}={val}\n" for name, val in sorted(lines.items())))
    SECRETS_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return reload_settings()


def ensure_widget_token() -> Settings:
    """Mint the widget token on first boot so nobody has to run `openssl rand`."""
    settings = get_settings()
    if settings.widget_token:
        return settings
    return write_secret("WIDGET_TOKEN", secrets.token_hex(32))


def reload_settings() -> Settings:
    """Re-read the environment into the object every caller is already holding.

    The alternative, returning a fresh instance, leaves the poller and the mail loop
    talking to Google with the token that was live when they started.
    """
    current = get_settings()
    fresh = Settings()
    for name in Settings.model_fields:
        setattr(current, name, getattr(fresh, name))
    return current
