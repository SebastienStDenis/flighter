"""AeroAPI v4: the only thing in this system that costs money, so it is the only thing
that rate-limits itself, meters its own spend, and refuses to run once a cap is hit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .models import ApiUsage, Booking, FlightSnapshot, KV

log = logging.getLogger(__name__)

FLIGHT_INFO_ENDPOINT = "/flights/{ident}"

# Billing is per page returned, one page being up to 15 records, so a `max_pages=1` call
# is one result set at the price below. Source: the per-query fee table at
# https://www.flightaware.com/commercial/aeroapi/ (checked 2026-08).
ENDPOINT_PRICE_USD: dict[str, Decimal] = {
    FLIGHT_INFO_ENDPOINT: Decimal("0.0050"),
}
# An endpoint we have not priced is assumed to cost as much as the one we call most, so a
# new call site over-reports rather than spending invisibly.
DEFAULT_PRICE_USD = Decimal("0.0050")

# Latch key in `kv`. The web app and the health page read this without touching AeroAPI.
BREAKER_KEY = "aeroapi_budget_breaker"

# How far a returned flight's scheduled departure may sit from the booking's before we
# stop believing it is the same flight. Wide enough for a day-of retiming, narrow enough
# that yesterday's or tomorrow's leg of a daily route can never win.
MATCH_WINDOW = timedelta(hours=6)

HTTP_TIMEOUT_SECONDS = 20.0


class BudgetExceeded(RuntimeError):
    """Raised instead of spending when month-to-date usage has reached the cap."""


class BudgetStatus(BaseModel):
    spend_usd: Decimal
    cap_usd: Decimal
    tripped: bool
    month: str


class FlightMatch(BaseModel):
    fa_flight_id: str
    flight: dict[str, Any]


# --- Rate limiting ------------------------------------------------------------------


class TokenBucket:
    """Result sets per minute, awaited rather than raised.

    The clock and sleep are injectable purely so the tests can prove the timing without
    spending real seconds on it.
    """

    def __init__(
        self,
        rate_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._capacity = float(max(rate_per_minute, 1))
        self._per_second = self._capacity / 60.0
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> float:
        return self._tokens

    async def acquire(self) -> None:
        # The lock serialises waiters, so a burst drains in arrival order rather than
        # every waiter waking together and racing for the same token.
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._per_second
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._per_second)


_limiter: TokenBucket | None = None


def shared_limiter(rate_per_minute: int | None = None) -> TokenBucket:
    """One bucket for the process: the quota is per account, not per caller."""
    global _limiter
    if _limiter is None:
        rate = rate_per_minute or get_settings().aeroapi_rate_limit_per_minute
        _limiter = TokenBucket(rate)
    return _limiter


# --- Spend metering and the circuit breaker -----------------------------------------


def month_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


def _month_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def estimate_cost(endpoint: str, result_sets: int) -> Decimal:
    price = ENDPOINT_PRICE_USD.get(endpoint, DEFAULT_PRICE_USD)
    return (price * Decimal(max(result_sets, 1))).quantize(Decimal("0.000001"))


async def spend_month_to_date(session: AsyncSession) -> Decimal:
    """Estimated USD spent since the start of the current UTC month."""
    total = await session.scalar(
        select(func.coalesce(func.sum(ApiUsage.est_cost_usd), 0)).where(
            ApiUsage.called_at >= _month_start(datetime.now(UTC))
        )
    )
    return Decimal(str(total or 0))


async def budget_status(session: AsyncSession, settings: Settings | None = None) -> BudgetStatus:
    """What the health page, the UI banner and the widget all read.

    The latch is scoped to the month it was written in, which is the whole of the reset
    logic: on the 1st the stored month stops matching and the breaker is simply not
    tripped any more.
    """
    settings = settings or get_settings()
    month = month_key(datetime.now(UTC))
    spend = await spend_month_to_date(session)
    latched = await _latched(session, month)
    return BudgetStatus(
        spend_usd=spend,
        cap_usd=settings.aeroapi_monthly_cap_usd,
        tripped=latched or spend >= settings.aeroapi_monthly_cap_usd,
        month=month,
    )


async def _latched(session: AsyncSession, month: str) -> bool:
    row = await session.get(KV, BREAKER_KEY)
    return bool(row is not None and row.value.get("month") == month)


async def _trip(session: AsyncSession, status: BudgetStatus) -> None:
    log.error(
        "AeroAPI budget breaker tripped: $%s spent against a $%s cap for %s; polling stops",
        status.spend_usd,
        status.cap_usd,
        status.month,
    )
    await session.merge(
        KV(
            key=BREAKER_KEY,
            value={
                "month": status.month,
                "spend_usd": str(status.spend_usd),
                "cap_usd": str(status.cap_usd),
                "tripped_at": datetime.now(UTC).isoformat(),
            },
        )
    )


async def ensure_budget(session: AsyncSession, settings: Settings | None = None) -> None:
    """Gate every call path. Latches on the way past so a restart stays stopped."""
    status = await budget_status(session, settings)
    if not status.tripped:
        return
    if not await _latched(session, status.month):
        await _trip(session, status)
    raise BudgetExceeded(
        f"AeroAPI spend ${status.spend_usd} has reached the ${status.cap_usd} cap for {status.month}"
    )


# --- Client --------------------------------------------------------------------------


class AeroAPIClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: TokenBucket | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._http = httpx.AsyncClient(
            base_url=self._settings.aeroapi_base_url,
            headers={"x-apikey": self._settings.aeroapi_key},
            timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS),
            transport=transport,
        )
        self._limiter = limiter or shared_limiter(self._settings.aeroapi_rate_limit_per_minute)

    @property
    def settings(self) -> Settings:
        return self._settings

    async def aclose(self) -> None:
        await self._http.aclose()

    async def flight_info(
        self, session: AsyncSession, ident: str, *, ident_type: str | None = None
    ) -> dict[str, Any]:
        """`GET /flights/{ident}`, one page, metered.

        `max_pages=1` is never optional: without it a bare ident returns roughly 14 days
        of a route's flights, and every extra page of 15 is billed again.
        """
        params: dict[str, str | int] = {"max_pages": 1}
        if ident_type is not None:
            params["ident_type"] = ident_type

        await ensure_budget(session, self._settings)
        await self._limiter.acquire()
        response = await self._http.get(f"/flights/{quote(ident, safe='')}", params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        await self._record_usage(session, FLIGHT_INFO_ENDPOINT, payload)
        return payload

    async def _record_usage(
        self, session: AsyncSession, endpoint: str, payload: dict[str, Any]
    ) -> None:
        result_sets = payload.get("num_pages") or 1
        if not isinstance(result_sets, int) or result_sets < 1:
            result_sets = 1
        session.add(
            ApiUsage(
                endpoint=endpoint,
                result_sets=result_sets,
                est_cost_usd=float(estimate_cost(endpoint, result_sets)),
            )
        )


_client: AeroAPIClient | None = None


def shared_client() -> AeroAPIClient:
    global _client
    if _client is None:
        _client = AeroAPIClient()
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


# --- Ident resolution ----------------------------------------------------------------


def flight_ident(booking: Booking) -> str:
    """The ident to ask AeroAPI about.

    The operating carrier is the one FlightAware tracks: a codeshare sold as AA8710 is
    flown, and reported, as BAW112. Marketing is only the fallback for when the booking
    never told us who actually flies it.
    """
    if booking.aeroapi_ident:
        return booking.aeroapi_ident
    carrier = (booking.operating_carrier or booking.marketing_carrier).strip().upper()
    number = (booking.operating_number or booking.marketing_number).strip()
    return f"{carrier}{number.lstrip('0') or number}"


def _airport_matches(ref: Any, iata: str) -> bool:
    if not isinstance(ref, dict):
        return False
    wanted = iata.strip().upper()
    for key in ("code_iata", "code", "code_icao", "code_lid"):
        value = ref.get(key)
        if isinstance(value, str) and value.strip().upper() == wanted:
            return True
    return False


def select_match(flights: Iterable[Any], booking: Booking) -> dict[str, Any] | None:
    """The returned leg nearest the booked departure, on the booked route, within ±6h."""
    booked = booking.scheduled_departure_utc
    if booked.tzinfo is None:
        booked = booked.replace(tzinfo=UTC)

    best: tuple[timedelta, dict[str, Any]] | None = None
    for flight in flights:
        if not isinstance(flight, dict):
            continue
        # A cancelled leg has no runway time, so gate-scheduled is the only field that is
        # reliably present on every flight we might be looking at.
        scheduled = _parse_timestamp(flight.get("scheduled_out")) or _parse_timestamp(
            flight.get("scheduled_off")
        )
        if scheduled is None:
            continue
        distance = abs(scheduled - booked)
        if distance > MATCH_WINDOW:
            continue
        if not _airport_matches(flight.get("origin"), booking.origin_iata):
            continue
        if not _airport_matches(flight.get("destination"), booking.dest_iata):
            continue
        if best is None or distance < best[0]:
            best = (distance, flight)
    return None if best is None else best[1]


async def resolve_flight(
    session: AsyncSession, booking: Booking, client: AeroAPIClient | None = None
) -> FlightMatch | None:
    """Turn a booking into a pinnable `fa_flight_id`, or nothing at all.

    A booking we cannot resolve is normal - schedules appear in AeroAPI a couple of days
    out - so this is never an error, just an absence.
    """
    client = client or shared_client()
    ident = flight_ident(booking)
    # Without ident_type an ident is read as a registration "if possible", which is the
    # wrong guess for every commercial flight number.
    payload = await client.flight_info(session, ident, ident_type="designator")
    flights = payload.get("flights") or []
    flight = select_match(flights, booking)
    if flight is None:
        log.info(
            "no AeroAPI match for booking %s (%s %s->%s): %d candidate(s)",
            booking.id,
            ident,
            booking.origin_iata,
            booking.dest_iata,
            len(flights) if isinstance(flights, list) else 0,
        )
        return None
    fa_flight_id = flight.get("fa_flight_id")
    if not isinstance(fa_flight_id, str) or not fa_flight_id:
        log.warning("AeroAPI match for booking %s carries no fa_flight_id", booking.id)
        return None
    return FlightMatch(fa_flight_id=fa_flight_id, flight=flight)


async def fetch_flight(
    session: AsyncSession, booking: Booking, client: AeroAPIClient | None = None
) -> dict[str, Any] | None:
    """One observation of a booking's flight, pinning the id on first success."""
    client = client or shared_client()

    if booking.aeroapi_fa_flight_id:
        payload = await client.flight_info(
            session, booking.aeroapi_fa_flight_id, ident_type="fa_flight_id"
        )
        legs = [
            flight
            for flight in payload.get("flights") or []
            if isinstance(flight, dict)
            and flight.get("fa_flight_id") == booking.aeroapi_fa_flight_id
        ]
        if not legs:
            log.warning(
                "pinned fa_flight_id %s returned nothing for booking %s",
                booking.aeroapi_fa_flight_id,
                booking.id,
            )
            return None
        # A diversion comes back as two legs sharing one id, original first. The later
        # departure is the leg still in the air.
        return max(legs, key=_leg_ordering)

    match = await resolve_flight(session, booking, client)
    if match is None:
        return None
    booking.aeroapi_fa_flight_id = match.fa_flight_id
    ident = match.flight.get("ident_icao") or match.flight.get("ident")
    if isinstance(ident, str) and ident:
        booking.aeroapi_ident = ident
    log.info("pinned booking %s to %s", booking.id, match.fa_flight_id)
    return match.flight


def _leg_ordering(flight: dict[str, Any]) -> datetime:
    for key in ("actual_out", "estimated_out", "scheduled_out"):
        parsed = _parse_timestamp(flight.get(key))
        if parsed is not None:
            return parsed
    return datetime.min.replace(tzinfo=UTC)


# --- Snapshot mapping ----------------------------------------------------------------

_TIMESTAMP_FIELDS = (
    "scheduled_out",
    "estimated_out",
    "actual_out",
    "actual_off",
    "scheduled_in",
    "estimated_in",
    "actual_in",
    "actual_on",
)
_TEXT_FIELDS = (
    "gate_origin",
    "gate_destination",
    "terminal_origin",
    "terminal_destination",
    "baggage_claim",
    "aircraft_type",
    "registration",
)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _as_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_snapshot_fields(flight: dict[str, Any]) -> dict[str, Any]:
    """Flatten one AeroAPI flight object onto the snapshot's denormalised columns.

    Total: a field AeroAPI omits, nulls, or returns in a shape we did not expect becomes
    None. Change detection diffs these, and a KeyError here would lose an event.
    """
    fields: dict[str, Any] = {
        "status_text": _as_text(flight.get("status")),
        "cancelled": _as_bool(flight.get("cancelled")),
        "diverted": _as_bool(flight.get("diverted")),
        "progress_percent": _as_int(flight.get("progress_percent")),
    }
    for key in _TEXT_FIELDS:
        fields[key] = _as_text(flight.get(key))
    for key in _TIMESTAMP_FIELDS:
        fields[key] = _parse_timestamp(flight.get(key))
    return fields


async def record_snapshot(
    session: AsyncSession, booking: Booking, flight: dict[str, Any]
) -> FlightSnapshot:
    snapshot = FlightSnapshot(booking_id=booking.id, raw=flight, **to_snapshot_fields(flight))
    session.add(snapshot)
    await session.flush()
    # `observed_at` is a server default, and everything downstream reads it. Loading it
    # lazily would mean an implicit IO on attribute access, which async will not do.
    await session.refresh(snapshot)
    return snapshot
