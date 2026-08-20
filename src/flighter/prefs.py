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

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Preferences

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

    # The Apple Mail flag colour that means "import this", and how often the IDLE that
    # watches for it is re-issued. A silent connection is what an impatient server and a
    # NAT table both drop, and five minutes is well inside what either tolerates.
    imap_flag_colour: str = "grey"
    imap_idle_seconds: int = 300
    # The display name of the iCloud calendar flights are written to, found by name
    # because iCloud will not let a client create one. A calendar of its own, so a bad
    # sync is undone by deleting one calendar rather than hunting among appointments.
    icloud_calendar_name: str = ""

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("imap_flag_colour")
    @classmethod
    def _known_colour(cls, value: str) -> str:
        # Imported here rather than at the top because mail.py reads these preferences.
        from .mail import FLAG_COLOURS

        if value not in FLAG_COLOURS:
            raise ValueError(f"pick one of {', '.join(FLAG_COLOURS)}")
        return value

    @property
    def calendar_configured(self) -> bool:
        return bool(self.icloud_calendar_name)


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
