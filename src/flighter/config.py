"""Credentials, and nothing else.

Configuration here splits in two, and no value lives in both halves. A *credential* is
set once by hand, is never handed back out by the UI, and is read from the environment.
Everything else is a *preference*: it has a working default, it is edited on the
settings page, and the database is the only place it lives - see `prefs`.

The app writes one file of its own into `data/` beside the database:
`data/secrets.env`, holding the credentials it mints itself rather than asking a person
for - today just the widget token generated on first boot. That key never appears in
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
DATABASE_FILE = DATA_DIR / "flighter.db"

# The app-written file is listed last because pydantic-settings lets the last file win,
# and a token the app minted is newer than anything a hand-edited file knows.
ENV_FILES = (".env", SECRETS_FILE)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILES, env_file_encoding="utf-8", extra="ignore")

    # One file in the same directory as the minted secrets, so the whole of the
    # deployment's state is the one thing to back up and the one thing to mount.
    database_url: str = f"sqlite+aiosqlite:///{DATABASE_FILE}"

    # --- FlightAware -----------------------------------------------------------------
    aeroapi_key: str = Field(default="", repr=False)

    # --- Anthropic, the extraction fallback ------------------------------------------
    anthropic_api_key: str = Field(default="", repr=False)

    # --- iCloud, the mailbox we watch and the calendar we write -----------------------
    # One app-specific password from appleid.apple.com covers both IMAP and CalDAV, and
    # neither accepts the Apple ID password once two-factor authentication is on.
    icloud_email: str = ""
    icloud_app_password: str = Field(default="", repr=False)

    # --- Pushover, the phone ----------------------------------------------------------
    # The token belongs to the application registered at pushover.net; the user key
    # identifies the account every device of yours is signed in to.
    pushover_token: str = Field(default="", repr=False)
    pushover_user_key: str = Field(default="", repr=False)

    # --- Widget -----------------------------------------------------------------------
    # Generated on first boot. The only authentication in front of the flight data.
    widget_token: str = Field(default="", repr=False)

    @property
    def icloud_configured(self) -> bool:
        return bool(self.icloud_email and self.icloud_app_password)

    @property
    def aeroapi_configured(self) -> bool:
        return bool(self.aeroapi_key)

    @property
    def pushover_configured(self) -> bool:
        return bool(self.pushover_token and self.pushover_user_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def write_secret(key: str, value: str) -> Settings:
    """Persist one app-minted credential and make it live in this process.

    Written whole rather than appended so a re-minted value replaces the old one instead
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

    The alternative, returning a fresh instance, leaves the poller and the dispatcher
    holding the values that were live when they started.
    """
    current = get_settings()
    fresh = Settings()
    for name in Settings.model_fields:
        setattr(current, name, getattr(fresh, name))
    return current
