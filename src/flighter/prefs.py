"""The knobs a person turns, and the one place they live.

Every preference has a working default, so a fresh deployment runs before anyone opens
the settings page. They are stored as a single row of `preferences`, edited at
`/settings`, and never read from the environment: a value with two homes is a value that
eventually disagrees with itself.

The row holds one JSONB blob whose shape is `Prefs`. Adding a knob is a field here and
a form control, never a migration, and an older row missing the field falls back to the
default in the same breath.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Preferences

log = logging.getLogger(__name__)

SINGLETON_ID = 1


class Prefs(BaseModel):
    """Frozen so a caller that grabbed it cannot quietly edit the deployment."""

    model_config = ConfigDict(frozen=True)

    # Absolute and reachable from the phone: it ends up in calendar entries, push
    # notifications and the widget, none of which are read on the machine that serves
    # them. The settings page offers the address you opened it on.
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    # Polling stops dead above this month-to-date estimate. The tier above Personal
    # carries a $100/month minimum, so the default leaves room under the $5 allowance.
    aeroapi_monthly_cap_usd: Decimal = Decimal("4.00")
    # Documented limit is 10 result sets/minute; leave headroom for retries.
    aeroapi_rate_limit_per_minute: int = 8

    anthropic_model: str = "claude-sonnet-5"
    # Below this an extraction lands in the review queue instead of the tracked list.
    extraction_confidence_threshold: float = 0.85

    # The folder the IMAP watcher idles on, and how often that IDLE is re-issued. A
    # silent connection is what an impatient server and a NAT table both drop, and five
    # minutes is well inside what either tolerates.
    imap_folder: str = "INBOX"
    imap_idle_seconds: int = 300
    # A calendar of its own, so a bad sync is undone by deleting one calendar. Created
    # by the consent flow rather than looked up by hand.
    gcal_calendar_id: str = ""

    ntfy_url: str = "http://ntfy:80"
    # The topic name is the secret in front of the push stream, so it is generated
    # rather than invented.
    ntfy_topic: str = ""

    @field_validator("public_base_url", "ntfy_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def ntfy_configured(self) -> bool:
        return bool(self.ntfy_topic)

    @property
    def calendar_configured(self) -> bool:
        return bool(self.gcal_calendar_id)


_current = Prefs()


def current() -> Prefs:
    """The live preferences.

    Defaults until `load` has run, which is what keeps every pure function in here
    testable without a database behind it.
    """
    return _current


async def load(session: AsyncSession) -> Prefs:
    """Read the row, creating it with the defaults the first time."""
    global _current
    row = await session.get(Preferences, SINGLETON_ID)
    if row is None:
        row = Preferences(id=SINGLETON_ID, values={})
        session.add(row)
        await session.flush()
    _current = Prefs.model_validate(row.values)
    return _current


async def save(session: AsyncSession, values: Mapping[str, Any]) -> Prefs:
    """Validate a partial update against the whole model and store the result."""
    global _current
    row = await session.get(Preferences, SINGLETON_ID)
    if row is None:
        row = Preferences(id=SINGLETON_ID, values={})
        session.add(row)
    merged = {**row.values, **values}
    updated = Prefs.model_validate(merged)
    row.values = updated.model_dump(mode="json")
    await session.flush()
    _current = updated
    return updated


async def ensure_defaults(session: AsyncSession) -> Prefs:
    """Fill in what can be generated rather than asked for.

    Only ever writes what is still empty: a topic regenerated on a restart is a phone
    that quietly stops receiving pushes.
    """
    prefs = await load(session)
    if not prefs.ntfy_topic:
        prefs = await save(session, {"ntfy_topic": f"flights-{secrets.token_hex(8)}"})
        log.info("generated an ntfy topic")
    return prefs
