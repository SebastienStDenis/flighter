"""Credentials, and the one file they live in.

Configuration splits in two, and no value lives in both halves. A *credential* is a
secret handed to another service: it is entered on the settings page, is never handed
back out by it, and is stored in `data/secrets.env` beside the database. Everything else
is a *preference*: it has a working default, it is edited on the same page, and the
database is the only place it lives - see `prefs`.

`data/secrets.env` outranks both the process environment and `.env`, which stay as an
optional way to seed a deployment. A fresh container needs neither, and a credential
typed into the settings page survives a restart of a container whose environment still
carries the old one.

`DATABASE_URL` is the exception and stays an environment value: the app cannot read a
setting out of a database it has not opened yet.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Relative on purpose: the image's WORKDIR is /app and the volume is mounted at
# /app/data, so the same default is the volume in Docker and ./data in a checkout.
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
SECRETS_FILE = DATA_DIR / "secrets.env"
DATABASE_FILE = DATA_DIR / "flighter.db"


class Service(NamedTuple):
    """One account the settings page connects to, and the credentials it needs."""

    key: str
    name: str
    fields: tuple[str, ...]


# What the settings page offers, in the order it offers it. The widget token is not here
# because the app mints that one rather than asking anybody for it.
SERVICES = (
    Service("icloud", "iCloud", ("icloud_email", "icloud_app_password")),
    Service("flightaware", "FlightAware", ("aeroapi_key",)),
    Service("pushover", "Pushover", ("pushover_token", "pushover_user_key")),
    Service("anthropic", "Anthropic", ("anthropic_api_key",)),
)

CREDENTIALS = tuple(name for service in SERVICES for name in service.fields)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The settings page writes data/secrets.env, so it outranks the environment and
        # .env rather than being overridden by them: what somebody typed into the UI is
        # newer than anything the container was started with, and an empty value there
        # is how a credential is cleared for good.
        return (
            init_settings,
            DotEnvSettingsSource(settings_cls, env_file=SECRETS_FILE, env_file_encoding="utf-8"),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    # One file in the same directory as the credentials, so the whole of the
    # deployment's state is the one thing to back up and the one thing to mount.
    database_url: str = f"sqlite+aiosqlite:///{DATABASE_FILE}"

    # --- FlightAware -----------------------------------------------------------------
    aeroapi_key: str = Field(default="", repr=False)

    # --- Anthropic, the extraction fallback ------------------------------------------
    anthropic_api_key: str = Field(default="", repr=False)

    # --- iCloud, the mailbox we watch and the calendar we write -----------------------
    # One app-specific password from appleid.apple.com covers both IMAP and CalDAV, and
    # neither accepts the Apple ID password once two-factor authentication is on. The
    # address is the account's name rather than a secret, so it is the one credential
    # the settings page shows back.
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


_generation = 0


def credentials_generation() -> int:
    """How many times a credential has changed since this process started.

    A client that had to sign in holds the number it saw and reconnects when it moves,
    which is what makes a credential typed on the settings page take effect without a
    restart. Clients that read the credential afresh on every call need nothing.
    """
    return _generation


def write_secrets(values: Mapping[str, str]) -> Settings:
    """Persist credentials to `data/secrets.env` and make them live in this process.

    The file is rewritten whole rather than appended to, so a replaced value leaves one
    line rather than two and a parser choosing between them. An empty value is kept as an
    empty line rather than dropped: that is what clears a credential the environment or
    `.env` would otherwise go on supplying.
    """
    global _generation
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lines = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            name, _, existing = line.partition("=")
            if name.strip():
                lines[name.strip()] = existing
    lines.update({name.upper(): value for name, value in values.items()})
    SECRETS_FILE.write_text("".join(f"{name}={value}\n" for name, value in sorted(lines.items())))
    SECRETS_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    _generation += 1
    return reload_settings()


def ensure_widget_token() -> Settings:
    """Mint the widget token on first boot so nobody has to run `openssl rand`."""
    settings = get_settings()
    if settings.widget_token:
        return settings
    return mint_widget_token()


def mint_widget_token() -> Settings:
    """A fresh token. Every phone has to connect again; that is one tap, so rotating is cheap."""
    return write_secrets({"widget_token": secrets.token_hex(32)})


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
