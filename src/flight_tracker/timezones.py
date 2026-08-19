"""Timezone rules, enforced in one place.

The rule the rest of the codebase depends on: a time is either a timezone-aware UTC
instant, or it is a naive wall-clock reading that is meaningless without the airport it
was read at. Nothing else is allowed to exist. An offset stated in an email is never
trusted - airlines state the wrong one often enough, and the origin airport's IANA zone
is always right.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FALLBACK_TZ = "UTC"

# Gate and baggage claim are null far more often than not, so the placeholder is part of
# the design rather than an error state.
UNKNOWN = "-"


def to_utc(local: datetime, tz: str) -> datetime:
    """Read a naive wall-clock time as local to `tz` and return the UTC instant.

    An already-aware datetime is converted rather than reinterpreted, so passing one
    through twice is harmless.
    """
    if local.tzinfo is not None:
        return local.astimezone(UTC)
    return local.replace(tzinfo=zone(tz)).astimezone(UTC)


def to_local(instant: datetime, tz: str) -> datetime:
    """Render a UTC instant as wall-clock time at `tz`."""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(zone(tz))


def zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(FALLBACK_TZ)


def format_local(instant: datetime | None, tz: str, *, with_date: bool = False) -> str:
    """`18:40 EDT`, or `Fri 12 Sep 18:40 EDT` - always carrying the zone abbreviation.

    Every time shown to the user goes through here. A bare `18:40` on a page listing two
    airports in different zones is the single easiest way to miss a flight.
    """
    if instant is None:
        return UNKNOWN
    local = to_local(instant, tz)
    fmt = "%a %-d %b %H:%M %Z" if with_date else "%H:%M %Z"
    return local.strftime(fmt)


def same_local_date(a: datetime, b: datetime, tz: str) -> bool:
    """Whether two instants fall on the same calendar day at `tz`.

    Overnight and date-line-crossing flights make "arrives the same day" a question with
    a real answer rather than an assumption.
    """
    return to_local(a, tz).date() == to_local(b, tz).date()
