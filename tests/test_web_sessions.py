"""The web layer on the real session plumbing.

Every transaction takes the database's one write lock the moment it begins, so the one
thing a request must never do is hold a transaction open while something it calls opens
another: the second waits on the first until it times out, and the page is a 500. The
pages are tested against a stand-in session elsewhere; here a request runs on
`get_session` itself, over the in-memory engine, where a transaction left open across
another is an immediate error rather than a five-second wait.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flighter import lookup, web
from flighter.aeroapi import AeroAPIClient, TokenBucket
from flighter.config import Settings
from flighter.db import session_scope
from flighter.models import Airport, Booking

# Far enough out to be inside the published window whenever this runs.
DAY = (datetime.now(UTC) + timedelta(days=30)).date()


def schedule(request: httpx.Request) -> httpx.Response:
    """One leg of AC871 out of Montreal on DAY, as the schedules endpoint spells it."""
    row: dict[str, Any] = {
        "ident_iata": "AC871",
        "actual_ident_iata": None,
        "scheduled_out": f"{DAY.isoformat()}T22:40:00Z",
        "scheduled_in": f"{(DAY + timedelta(days=1)).isoformat()}T09:25:00Z",
        "origin_iata": "YUL",
        "destination_iata": "LHR",
    }
    return httpx.Response(200, json={"links": None, "num_pages": 1, "scheduled": [row]})


async def seed_airports(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        session.add_all(
            [
                Airport(
                    iata="YUL",
                    icao="CYUL",
                    name="Montreal-Trudeau",
                    city="Montreal",
                    country="CA",
                    latitude=45.47,
                    longitude=-73.74,
                    tz="America/Toronto",
                ),
                Airport(
                    iata="LHR",
                    icao="EGLL",
                    name="London Heathrow",
                    city="London",
                    country="GB",
                    latitude=51.47,
                    longitude=-0.46,
                    tz="Europe/London",
                ),
            ]
        )
        await session.commit()


async def test_looking_a_flight_up_holds_no_transaction_across_flightaware(
    settings: Settings,
    database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nav's Problems count, the client's budget check and the airports each take
    the lock in turn; a count left open across the lookup would deadlock the check."""
    await seed_airports(database)
    client = AeroAPIClient(
        settings,
        transport=httpx.MockTransport(schedule),
        limiter=TokenBucket(600),
        sessions=session_scope,
    )
    monkeypatch.setattr(lookup, "shared_client", lambda: client)

    app = web.create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://flighter.test"
    ) as http:
        response = await http.post(
            "/f/new",
            data={"flight_number": "AC871", "departure_date": DAY.isoformat()},
            follow_redirects=False,
        )

    assert response.status_code == 303
    async with database() as session:
        booking = await session.scalar(select(Booking))
    assert booking is not None
    assert response.headers["location"] == f"/f/{booking.id}"
    assert (booking.origin_iata, booking.dest_iata) == ("YUL", "LHR")
