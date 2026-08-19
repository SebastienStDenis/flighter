"""Airport reference data: the seed, and the timezone lookup the rest of the app trusts.

Every booking time in the database was converted using the zone stored here, so this
table is the one place an airport's IANA zone comes from. The data ships offline in the
`airportsdata` package, which already carries a per-airport `tz`, so there is nothing to
fetch at runtime and no coordinate-to-zone guessing to do.
"""

from __future__ import annotations

import logging
from typing import Any

import airportsdata
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Airport
from .timezones import FALLBACK_TZ

log = logging.getLogger(__name__)

# Postgres caps a statement at 65535 bind parameters and each row spends eight of them.
_SEED_CHUNK_ROWS = 500

_tz_cache: dict[str, str] = {}


def _dataset_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code, entry in airportsdata.load("IATA").items():
        iata = (code or "").strip().upper()
        tz = (entry.get("tz") or "").strip()
        # An airport we cannot place in time is worse than no row at all: a booking
        # would silently be converted against UTC and land hours out.
        if len(iata) != 3 or not tz:
            continue
        rows.append(
            {
                "iata": iata,
                "icao": (entry.get("icao") or "").strip() or None,
                "name": entry.get("name") or iata,
                "city": (entry.get("city") or "").strip() or None,
                "country": (entry.get("country") or "").strip() or None,
                "latitude": float(entry["lat"]),
                "longitude": float(entry["lon"]),
                "tz": tz,
            }
        )
    return rows


async def seed_airports(session: AsyncSession) -> int:
    """Upsert the whole dataset and return the number of rows written.

    Safe to run on every boot: an airport that has been renamed or re-zoned is updated
    in place, and one the dataset has dropped is left alone rather than deleted, since a
    booking may still reference it.
    """
    rows = _dataset_rows()
    written = 0
    for start in range(0, len(rows), _SEED_CHUNK_ROWS):
        chunk = rows[start : start + _SEED_CHUNK_ROWS]
        stmt = insert(Airport).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Airport.iata],
            set_={
                "icao": stmt.excluded.icao,
                "name": stmt.excluded.name,
                "city": stmt.excluded.city,
                "country": stmt.excluded.country,
                "latitude": stmt.excluded.latitude,
                "longitude": stmt.excluded.longitude,
                "tz": stmt.excluded.tz,
            },
        )
        result = await session.execute(stmt)
        written += result.rowcount or 0

    _tz_cache.clear()
    log.info("seeded %d airports", written)
    return written


async def get_airport(session: AsyncSession, iata: str) -> Airport | None:
    code = (iata or "").strip().upper()
    if len(code) != 3:
        return None
    return await session.get(Airport, code)


async def airport_tz(session: AsyncSession, iata: str) -> str:
    """The IANA zone for an airport, falling back to UTC for one we do not know.

    UTC is the wrong answer, but it is a stable wrong answer that keeps a booking
    visible instead of failing the whole ingestion; the warning is the signal to seed.
    """
    code = (iata or "").strip().upper()
    cached = _tz_cache.get(code)
    if cached is not None:
        return cached

    airport = await get_airport(session, code)
    if airport is None:
        log.warning("no airport row for %r; falling back to %s", iata, FALLBACK_TZ)
        return FALLBACK_TZ

    # Only real hits are cached, so an airport seeded after a miss is picked up.
    _tz_cache[code] = airport.tz
    return airport.tz


def clear_tz_cache() -> None:
    _tz_cache.clear()
