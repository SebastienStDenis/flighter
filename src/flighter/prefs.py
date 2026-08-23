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
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from .models import KV, Preferences

SINGLETON_ID = 1

LAST_SEEN_ORIGIN_KEY: Final = "last_seen_origin"


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

    # The two jobs the app runs against the mailbox and the calendar. Either can be put
    # down without taking the account off the app: off, the mailbox is not watched and
    # nothing is written to the calendar.
    email_import_enabled: bool = True
    calendar_sync_enabled: bool = True

    # The Apple Mail flag colour that means "import this".
    imap_flag_colour: str = "grey"
    # The collection URL of the iCloud calendar flights are written to, picked from the
    # ones the account offers on the settings page. The URL rather than the display name
    # because a calendar renamed in the Calendar app is still the same collection, and
    # because a stored URL is a sync that costs one request instead of four.
    icloud_calendar_url: str = ""

    # What the phone is told about. The master switch stops every push about a flight or
    # an email; the rest choose between classes of news under it. All on by default,
    # because a deployment nobody has configured should still say when a gate moves.
    #
    # The budget alarm is deliberately not among these: it is the one push that is about
    # the service rather than about a trip, and it is how somebody finds out the board
    # has stopped updating.
    notifications_enabled: bool = True
    notify_gate_changes: bool = True
    notify_time_changes: bool = True
    notify_departure_and_landing: bool = True
    notify_baggage_claim: bool = True
    notify_disruptions: bool = True
    notify_imports: bool = True
    notify_import_failures: bool = True

    sync_friend_flights_to_calendar: bool = False
    notify_for_friend_flights: bool = False
    show_friend_flights_in_widget: bool = False

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
        return bool(self.icloud_calendar_url)


_current = Prefs()

# The scheme and host the app was last reached on. The calendar and the push
# notifications are written from the poller and the mail import, with no request in
# hand, and this is the address they fall back to until one is saved.
_last_seen_origin: str | None = None


def current() -> Prefs:
    """The live preferences.

    Defaults until `load` has run, which is what keeps every pure function in here
    testable without a database behind it.
    """
    return _current


def last_seen_origin() -> str | None:
    return _last_seen_origin


DEFAULT_BASE_URL: Final = Prefs.model_fields["public_base_url"].default

# The addresses that only resolve on the machine serving the page. A request that came
# in on one of these proves nothing about what a phone can reach.
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def is_loopback(origin: str) -> bool:
    return urlsplit(origin).hostname in LOOPBACK_HOSTS


def automatic_base_url(origin: str | None = None) -> str:
    """What the address would be with nobody having set one.

    Kept apart from `public_base_url` because the settings page has to offer it while an
    override is still in force: it is both what the box says it would fall back to and
    what clearing the box actually gets you.
    """
    return origin or _last_seen_origin or DEFAULT_BASE_URL


def public_base_url(origin: str | None = None) -> str:
    """The address somebody set, or the best evidence of one when nobody has.

    Nothing that carries this address is read on the machine serving the page, so the
    default is never the right answer for long. A request that reached this server came
    in on an address that demonstrably works from wherever it was typed, which is the
    better guess: the request in hand when there is one, otherwise the last one the app
    was reached on.

    An empty setting and the default both mean "work it out", so a deployment nobody has
    told its address follows the one it is actually being opened on.
    """
    saved = _current.public_base_url
    if saved and saved != DEFAULT_BASE_URL:
        return saved
    return automatic_base_url(origin)


async def load(session: AsyncSession) -> Prefs:
    """Read the row, creating it with the defaults the first time."""
    global _current, _last_seen_origin
    row = await session.get(Preferences, SINGLETON_ID)
    if row is None:
        row = Preferences(id=SINGLETON_ID, values={})
        session.add(row)
        await session.flush()
    _current = Prefs.model_validate(row.values)
    seen = await session.get(KV, LAST_SEEN_ORIGIN_KEY)
    _last_seen_origin = seen.value["origin"] if seen is not None else None
    return _current


async def remember_origin(session: AsyncSession, origin: str) -> None:
    """Keep the address a request came in on, for the paths that have no request.

    Opening the board on the machine that serves it is not evidence of an address a
    phone can reach, so a loopback address never displaces one that is not. Without
    that, one visit to localhost would point every calendar entry, push and widget at
    a host only the server itself can resolve.
    """
    global _last_seen_origin
    if is_loopback(origin) and _last_seen_origin and not is_loopback(_last_seen_origin):
        return
    await session.merge(KV(key=LAST_SEEN_ORIGIN_KEY, value={"origin": origin}))
    await session.flush()
    _last_seen_origin = origin


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
