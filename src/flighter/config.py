"""Runtime configuration, entirely from the environment.

Single user, single deployment: there is no settings UI and nothing to configure at
runtime, so every knob is an environment variable read once at startup. Secrets come
from the compose file's env or a Docker secret; nothing sensitive is ever written to
the database.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Deployment -----------------------------------------------------------------
    # Absolute, publicly reachable base for links we hand to other systems: the widget's
    # tap target, the Click header on a push, the URL in a calendar description. Those
    # are read on a phone that is not on the home network, so a LAN address is useless.
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://flights:flights@db:5432/flights"

    # --- AeroAPI --------------------------------------------------------------------
    aeroapi_key: str = ""
    aeroapi_base_url: str = "https://aeroapi.flightaware.com/aeroapi"
    # The Personal tier has no monthly minimum but the next tier up costs $100/month, so
    # the breaker stops polling well short of anything that could trigger an upgrade.
    aeroapi_monthly_cap_usd: Decimal = Decimal("4.00")
    # Documented limit is 10 result sets/minute; leave headroom for retries.
    aeroapi_rate_limit_per_minute: int = 8

    # --- Anthropic (extraction fallback) --------------------------------------------
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    # Below this an extraction lands in the review queue instead of the tracked list.
    extraction_confidence_threshold: float = 0.85

    # --- Gmail ----------------------------------------------------------------------
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_poll_seconds: int = 180

    # --- Google Calendar ------------------------------------------------------------
    gcal_client_id: str = ""
    gcal_client_secret: str = ""
    gcal_refresh_token: str = ""
    # A calendar of its own, so a bad sync can be cleared by deleting one calendar.
    gcal_calendar_id: str = ""

    # --- ntfy -------------------------------------------------------------------------
    ntfy_url: str = "http://ntfy:80"
    ntfy_topic: str = ""
    ntfy_token: str = ""

    # --- Widget -----------------------------------------------------------------------
    # Shared bearer token; the only authentication in the system.
    widget_token: str = Field(default="", repr=False)

    @field_validator("public_base_url", "aeroapi_base_url", "ntfy_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def gmail_configured(self) -> bool:
        return bool(self.gmail_client_id and self.gmail_client_secret and self.gmail_refresh_token)

    @property
    def gcal_configured(self) -> bool:
        return bool(
            self.gcal_client_id
            and self.gcal_client_secret
            and self.gcal_refresh_token
            and self.gcal_calendar_id
        )

    @property
    def ntfy_configured(self) -> bool:
        return bool(self.ntfy_topic)

    @property
    def aeroapi_configured(self) -> bool:
        return bool(self.aeroapi_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
