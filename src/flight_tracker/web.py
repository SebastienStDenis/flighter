"""The pages a person actually looks at: the flight list, the flight, the queue.

Everything is rendered on the server into one HTML response. The reader is standing in
a terminal on hotel wifi wanting to know a gate number, so there is no framework to
boot and nothing fetched after paint; htmx covers the handful of in-place mutations.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NamedTuple

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import bookings as booking_repo
from .aeroapi import budget_status
from .airports import get_airport
from .config import Settings
from .db import dispose_engine, get_session, init_engine
from .gcal import CalendarClient
from .models import KV, Airport, Booking, FlightEvent, FlightSnapshot, Passenger
from .timezones import format_local, to_local
from .widget import router as widget_router

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

# What a value looks like when we simply do not have it. Half of a flight's fields are
# null until an hour before departure, so this is the most common thing on the page.
MISSING = "-"

# Statuses the list renders. Archived rows are gone as far as the UI is concerned, and
# pending_review lives in its own queue.
LISTED_STATUSES = ("active", "completed", "cancelled")

# Airlines call anything under a quarter hour "on time", and so does the industry's own
# on-time statistic. Below this a delay is noise the reader does not need shouted at.
DELAY_THRESHOLD = timedelta(minutes=15)

# Flights this far apart are two journeys, not two legs of one. A same-day connection
# and a red-eye that lands tomorrow both fall inside a day; a return a week later does
# not, which is exactly the split a person means by "trip".
TRIP_GAP = timedelta(hours=24)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class Status(NamedTuple):
    """A status is always a word plus a colour, never a colour on its own."""

    label: str
    tone: str


@dataclass(frozen=True)
class FlightView:
    """A booking, its newest snapshot and both airports, shaped for a template.

    Templates ask this for values rather than reaching into a snapshot, so "estimated
    beats scheduled, actual beats estimated" is decided once instead of per page.
    """

    booking: Booking
    snapshot: FlightSnapshot | None
    origin: Airport | None
    dest: Airport | None
    passenger: Passenger | None

    @property
    def flight_number(self) -> str:
        return f"{self.booking.marketing_carrier}{self.booking.marketing_number}"

    @property
    def passenger_name(self) -> str:
        return self.passenger.display_name if self.passenger else MISSING

    @property
    def operating_flight(self) -> str | None:
        booking = self.booking
        if not booking.operating_carrier:
            return None
        return f"{booking.operating_carrier}{booking.operating_number or ''}"

    @property
    def origin_tz(self) -> str:
        return self.origin.tz if self.origin else "UTC"

    @property
    def dest_tz(self) -> str:
        return self.dest.tz if self.dest else "UTC"

    @property
    def scheduled_departure(self) -> datetime:
        snap = self.snapshot
        if snap is not None and snap.scheduled_out is not None:
            return snap.scheduled_out
        return self.booking.scheduled_departure_utc

    @property
    def scheduled_arrival(self) -> datetime | None:
        snap = self.snapshot
        if snap is not None and snap.scheduled_in is not None:
            return snap.scheduled_in
        return self.booking.scheduled_arrival_utc

    @property
    def departure(self) -> datetime:
        snap = self.snapshot
        if snap is not None:
            return snap.actual_out or snap.estimated_out or self.scheduled_departure
        return self.scheduled_departure

    @property
    def arrival(self) -> datetime | None:
        snap = self.snapshot
        if snap is not None:
            return snap.actual_in or snap.estimated_in or self.scheduled_arrival
        return self.scheduled_arrival

    @property
    def delay(self) -> timedelta:
        return self.departure - self.scheduled_departure

    @property
    def cancelled(self) -> bool:
        return self.booking.status == "cancelled" or bool(
            self.snapshot is not None and self.snapshot.cancelled
        )

    @property
    def progress_percent(self) -> int | None:
        return self.snapshot.progress_percent if self.snapshot else None

    @property
    def status(self) -> Status:
        snap = self.snapshot
        if self.cancelled:
            return Status("Cancelled", "stop")
        if snap is not None and snap.diverted:
            return Status("Diverted", "stop")
        if self.booking.status == "pending_review":
            return Status("Needs review", "signal")
        if self.delay >= DELAY_THRESHOLD:
            return Status(f"Delayed {duration(self.delay)}", "signal")
        if snap is not None and snap.actual_in is not None:
            return Status("Landed", "clear")
        if snap is not None and snap.actual_out is not None:
            return Status("In the air", "clear")
        if self.booking.status == "completed":
            return Status("Flown", "quiet")
        if snap is not None and snap.status_text:
            return Status(snap.status_text, "quiet")
        return Status("Scheduled", "quiet")

    @property
    def ended(self) -> datetime:
        """When this flight stopped being something to worry about."""
        arrival = self.arrival
        if arrival is not None:
            return arrival
        # No arrival time anywhere: assume the longest plausible short-haul hop rather
        # than leaving a departed flight pinned to the top of the list forever.
        return self.departure + timedelta(hours=3)

    def raw(self, *path: str) -> Any:
        """A field out of the stored AeroAPI object, or None if it is not there."""
        value: Any = self.snapshot.raw if self.snapshot else None
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value


def duration(delta: timedelta) -> str:
    """`45m`, `1h 20m`. Used for delays, so the sign is carried by the caller."""
    minutes = int(abs(delta).total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def day(instant: datetime, tz: str) -> str:
    """`Sat 12 Sep`, the heading a trip is filed under."""
    return to_local(instant, tz).strftime("%a %-d %b")


def same_day(a: FlightView, b: FlightView) -> bool:
    """Whether two flights depart on the same calendar day, each read at its own airport."""
    return (
        to_local(a.scheduled_departure, a.origin_tz).date()
        == to_local(b.scheduled_departure, b.origin_tz).date()
    )


def at(instant: datetime | None, tz: str, *, with_date: bool = False) -> str:
    """A time at an airport, or the missing marker. Every time on every page uses it."""
    if instant is None:
        return MISSING
    return format_local(instant, tz, with_date=with_date)


def dash(value: Any) -> str:
    if value is None or value == "":
        return MISSING
    return str(value)


def altitude(value: Any) -> str:
    """AeroAPI files altitude in hundreds of feet, which is a flight level."""
    if not isinstance(value, int):
        return MISSING
    return f"FL{value:03d}" if value < 600 else f"{value:,} ft"


def distance(value: Any) -> str:
    """AeroAPI reports route distance in statute miles."""
    if not isinstance(value, int | float):
        return MISSING
    return f"{int(value):,} mi"


def group_into_trips(views: Sequence[FlightView]) -> list[list[FlightView]]:
    """Split departure-ordered flights into runs that belong to the same journey."""
    trips: list[list[FlightView]] = []
    for view in views:
        if trips and view.departure - trips[-1][-1].ended <= TRIP_GAP:
            trips[-1].append(view)
        else:
            trips.append([view])
    return trips


async def latest_snapshots(
    session: AsyncSession, booking_ids: Sequence[int]
) -> dict[int, FlightSnapshot]:
    """The newest snapshot per booking, in one query.

    Snapshots are append-only, so "newest row wins" is the whole of the read model.
    """
    if not booking_ids:
        return {}
    rows = await session.execute(
        select(FlightSnapshot)
        .where(FlightSnapshot.booking_id.in_(booking_ids))
        .order_by(FlightSnapshot.booking_id, FlightSnapshot.observed_at.desc())
    )
    newest: dict[int, FlightSnapshot] = {}
    for snapshot in rows.scalars():
        newest.setdefault(snapshot.booking_id, snapshot)
    return newest


async def build_views(session: AsyncSession, rows: Iterable[Booking]) -> list[FlightView]:
    rows = list(rows)
    snapshots = await latest_snapshots(session, [row.id for row in rows])
    airports: dict[str, Airport | None] = {}
    for row in rows:
        for iata in (row.origin_iata, row.dest_iata):
            if iata not in airports:
                airports[iata] = await get_airport(session, iata)
    # Loaded up front rather than through booking.passenger: a lazy relationship on an
    # async session raises rather than emitting the query the template expects.
    people = await session.execute(
        select(Passenger).where(Passenger.id.in_({row.passenger_id for row in rows}))
    )
    by_id = {person.id: person for person in people.scalars()}
    views = [
        FlightView(
            booking=row,
            snapshot=snapshots.get(row.id),
            origin=airports.get(row.origin_iata),
            dest=airports.get(row.dest_iata),
            passenger=by_id.get(row.passenger_id),
        )
        for row in rows
    ]
    views.sort(key=lambda view: view.scheduled_departure)
    return views


async def list_passengers(session: AsyncSession) -> list[Passenger]:
    rows = await session.execute(
        select(Passenger).order_by(Passenger.is_self.desc(), Passenger.display_name)
    )
    return list(rows.scalars())


async def pending_count(session: AsyncSession) -> int:
    rows = await booking_repo.list_bookings(session, statuses=("pending_review",))
    return len(list(rows))


def local_input(instant: datetime | None, tz: str) -> str:
    """A UTC instant as the wall clock the airport reads, for datetime-local.

    The form never sees UTC. What the user typed is what they read off their ticket.
    """
    if instant is None:
        return ""
    return to_local(instant, tz).strftime("%Y-%m-%dT%H:%M")


def parse_local(value: str) -> datetime | None:
    """A datetime-local field as the naive wall clock it is. No zone is applied here."""
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_engine(settings)
        yield
        await dispose_engine()

    app = FastAPI(title="flight-tracker", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(widget_router)

    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.globals.update(
        at=at,
        dash=dash,
        day=day,
        same_day=same_day,
        altitude=altitude,
        distance=distance,
        duration=duration,
        local_input=local_input,
        missing=MISSING,
        delay_threshold=DELAY_THRESHOLD,
    )

    def page(request: Request, name: str, context: dict[str, Any], **kwargs: Any) -> Response:
        return templates.TemplateResponse(request, name, context, **kwargs)

    def htmx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    @app.exception_handler(HTTPException)
    async def on_http_error(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith(("/api/", "/healthz")):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return page(
            request,
            "error.html",
            {"code": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code,
        )

    async def load(session: AsyncSession, booking_id: int) -> FlightView:
        booking = await booking_repo.get_booking(session, booking_id)
        if booking is None:
            raise HTTPException(status_code=404, detail="No such flight.")
        views = await build_views(session, [booking])
        return views[0]

    @app.get("/")
    async def index(request: Request, session: SessionDep) -> Response:
        rows = await booking_repo.list_bookings(session, statuses=LISTED_STATUSES)
        views = await build_views(session, rows)
        now = datetime.now(UTC)
        upcoming = [view for view in views if view.ended >= now]
        past = [view for view in views if view.ended < now]
        past.reverse()
        budget = await budget_status(session)
        return page(
            request,
            "index.html",
            {
                "trips": group_into_trips(upcoming),
                "past": past,
                "pending": await pending_count(session),
                "budget": budget,
            },
        )

    # Declared before /f/{booking_id} so "new" is never parsed as an id.
    @app.get("/f/new")
    async def new_flight(request: Request, session: SessionDep) -> Response:
        return page(
            request,
            "form.html",
            {
                "view": None,
                "passengers": await list_passengers(session),
                "error": None,
                "form": {},
            },
        )

    @app.post("/f")
    async def create_flight(
        request: Request,
        session: SessionDep,
        passenger_id: Annotated[int, Form()],
        marketing_carrier: Annotated[str, Form()],
        marketing_number: Annotated[str, Form()],
        origin_iata: Annotated[str, Form()],
        dest_iata: Annotated[str, Form()],
        departure_local: Annotated[str, Form()],
        arrival_local: Annotated[str, Form()] = "",
        confirmation_code: Annotated[str, Form()] = "",
        seat: Annotated[str, Form()] = "",
        notes: Annotated[str, Form()] = "",
    ) -> Response:
        departure = parse_local(departure_local)
        if departure is None:
            return page(
                request,
                "form.html",
                {
                    "view": None,
                    "passengers": await list_passengers(session),
                    "error": "Departure needs a date and a time.",
                    "form": dict(await request.form()),
                },
                status_code=400,
            )
        booking = await booking_repo.create_booking(
            session,
            passenger_id=passenger_id,
            marketing_carrier=marketing_carrier.strip().upper(),
            marketing_number=marketing_number.strip(),
            origin_iata=origin_iata.strip().upper(),
            dest_iata=dest_iata.strip().upper(),
            departure_local=departure,
            arrival_local=parse_local(arrival_local),
            confirmation_code=confirmation_code.strip() or None,
            seat=seat.strip() or None,
            notes=notes.strip() or None,
            source="manual",
        )
        return RedirectResponse(f"/f/{booking.id}", status_code=303)

    @app.get("/f/{booking_id}")
    async def detail(request: Request, session: SessionDep, booking_id: int) -> Response:
        view = await load(session, booking_id)
        events = await session.execute(
            select(FlightEvent)
            .where(FlightEvent.booking_id == booking_id)
            .order_by(FlightEvent.occurred_at.desc())
        )
        return page(request, "detail.html", {"v": view, "events": list(events.scalars())})

    @app.get("/f/{booking_id}/edit")
    async def edit_flight(request: Request, session: SessionDep, booking_id: int) -> Response:
        view = await load(session, booking_id)
        return page(
            request,
            "form.html",
            {
                "view": view,
                "passengers": await list_passengers(session),
                "error": None,
                "form": {},
            },
        )

    @app.post("/f/{booking_id}")
    async def update_flight(
        request: Request,
        session: SessionDep,
        booking_id: int,
        passenger_id: Annotated[int, Form()],
        marketing_carrier: Annotated[str, Form()],
        marketing_number: Annotated[str, Form()],
        origin_iata: Annotated[str, Form()],
        dest_iata: Annotated[str, Form()],
        departure_local: Annotated[str, Form()],
        arrival_local: Annotated[str, Form()] = "",
        confirmation_code: Annotated[str, Form()] = "",
        seat: Annotated[str, Form()] = "",
        notes: Annotated[str, Form()] = "",
    ) -> Response:
        departure = parse_local(departure_local)
        if departure is None:
            view = await load(session, booking_id)
            return page(
                request,
                "form.html",
                {
                    "view": view,
                    "passengers": await list_passengers(session),
                    "error": "Departure needs a date and a time.",
                    "form": dict(await request.form()),
                },
                status_code=400,
            )
        await booking_repo.update_booking(
            session,
            booking_id,
            passenger_id=passenger_id,
            marketing_carrier=marketing_carrier.strip().upper(),
            marketing_number=marketing_number.strip(),
            origin_iata=origin_iata.strip().upper(),
            dest_iata=dest_iata.strip().upper(),
            departure_local=departure,
            arrival_local=parse_local(arrival_local),
            confirmation_code=confirmation_code.strip() or None,
            seat=seat.strip() or None,
            notes=notes.strip() or None,
        )
        return RedirectResponse(f"/f/{booking_id}", status_code=303)

    # An HTML form can only ever send GET or POST, so deletion is a POST to its own
    # path rather than DELETE /f/{id}.
    @app.post("/f/{booking_id}/delete")
    async def delete_flight(request: Request, session: SessionDep, booking_id: int) -> Response:
        booking = await booking_repo.get_booking(session, booking_id)
        if booking is None:
            raise HTTPException(status_code=404, detail="No such flight.")
        if booking.gcal_event_id and settings.gcal_configured:
            try:
                await CalendarClient(settings).delete(booking)
            except Exception:
                # The booking still goes. A calendar event nobody can delete is worth
                # less than a list that refuses to let go of a cancelled trip.
                log.warning("could not delete calendar event for booking %s", booking_id)
        await booking_repo.delete_booking(session, booking_id)
        if htmx(request):
            return Response(status_code=200)
        return RedirectResponse("/", status_code=303)

    @app.get("/review")
    async def review(request: Request, session: SessionDep) -> Response:
        rows = await booking_repo.list_bookings(session, statuses=("pending_review",))
        views = await build_views(session, rows)
        return page(request, "review.html", {"views": views})

    @app.post("/review/{booking_id}/accept")
    async def accept(request: Request, session: SessionDep, booking_id: int) -> Response:
        await booking_repo.update_booking(session, booking_id, status="active")
        if htmx(request):
            return Response(status_code=200)
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/{booking_id}/reject")
    async def reject(request: Request, session: SessionDep, booking_id: int) -> Response:
        # Archived rather than deleted: the dedupe index skips archived rows, so the
        # same email arriving again is free to be extracted afresh.
        await booking_repo.update_booking(session, booking_id, status="archived")
        if htmx(request):
            return Response(status_code=200)
        return RedirectResponse("/review", status_code=303)

    @app.get("/passengers")
    async def passengers(request: Request, session: SessionDep) -> Response:
        return page(request, "passengers.html", {"passengers": await list_passengers(session)})

    @app.post("/passengers")
    async def add_passenger(
        request: Request,
        session: SessionDep,
        display_name: Annotated[str, Form()],
        is_self: Annotated[bool, Form()] = False,
    ) -> Response:
        name = display_name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="A passenger needs a name.")
        passenger = Passenger(display_name=name, is_self=is_self)
        session.add(passenger)
        await session.flush()
        if htmx(request):
            # Added from inside the flight form: hand back the option it should select.
            return page(request, "option.html", {"passenger": passenger})
        return RedirectResponse("/passengers", status_code=303)

    @app.post("/passengers/{passenger_id}/delete")
    async def delete_passenger(
        request: Request, session: SessionDep, passenger_id: int
    ) -> Response:
        passenger = await session.get(Passenger, passenger_id)
        if passenger is None:
            raise HTTPException(status_code=404, detail="No such passenger.")
        held = await session.execute(
            select(func.count()).select_from(Booking).where(Booking.passenger_id == passenger_id)
        )
        if held.scalar_one():
            raise HTTPException(
                status_code=400, detail="That passenger still has flights on the list."
            )
        await session.delete(passenger)
        if htmx(request):
            return Response(status_code=200)
        return RedirectResponse("/passengers", status_code=303)

    @app.get("/health")
    async def health(request: Request, session: SessionDep) -> Response:
        counts = await session.execute(
            select(Booking.status, func.count()).group_by(Booking.status)
        )
        last_snapshot = await session.execute(select(func.max(FlightSnapshot.observed_at)))
        state = await session.execute(select(KV).order_by(KV.key))
        return page(
            request,
            "health.html",
            {
                "counts": dict(counts.all()),
                "last_snapshot": last_snapshot.scalar_one_or_none(),
                "budget": await budget_status(session),
                # The poller and the Gmail sync each own their own KV keys; this page
                # reports whatever is in the table rather than asserting a shape.
                "state": [
                    (row.key, json.dumps(row.value, indent=2, default=str))
                    for row in state.scalars()
                ],
                "settings": settings,
            },
        )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness only, for the container health check. It must not touch the DB."""
        return JSONResponse({"status": "ok"})

    return app
