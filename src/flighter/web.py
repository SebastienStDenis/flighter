"""The pages a person actually looks at: the flight list, the flight, the queue.

Everything is rendered on the server into one HTML response. The reader is standing in
a terminal on hotel wifi wanting to know a gate number, so there is no framework to
boot and nothing fetched after paint; htmx covers the handful of in-place mutations.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NamedTuple

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import bookings as booking_repo
from . import gcal, gmail, google_auth, prefs
from .aeroapi import budget_status
from .airports import airport_tz, get_airport
from .checks import run_checks
from .config import Settings
from .db import get_session
from .gcal import CalendarClient
from .models import KV, Airport, Booking, FlightEvent, FlightSnapshot, Passenger
from .phase import arrival_estimate, landing_estimate
from .timezones import format_local, to_local
from .widget import router as widget_router

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

# What a value looks like when we simply do not have it. Gates and baggage belts stay
# null until close to the event, so this is one of the most common things on the page.
MISSING = "-"

# What the list shows. Archived bookings are gone as far as the UI is concerned, and
# pending_review has a queue of its own.
LISTED_STATUSES = ("active", "completed", "cancelled")

# The industry calls anything under a quarter hour on time, and so does its own on-time
# statistic. Below this, a delay is noise nobody needs shouted at.
DELAY_THRESHOLD = timedelta(minutes=15)

# Flights this far apart are two journeys, not two legs of one. A same-day connection
# and a red-eye that lands tomorrow both fall inside a day; a return a week later does
# not, which is the split a person means by "trip".
TRIP_GAP = timedelta(hours=24)

# Where the consent flow parks its one-time state between the redirect out and back.
OAUTH_STATE_KEY = "google_oauth_state"

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _first_validation_message(exc: ValidationError) -> str:
    """One field, one sentence. A wall of pydantic is not an error message."""
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error["loc"]) or "value"
    return f"{field.replace('_', ' ')}: {error['msg']}"


class Status(NamedTuple):
    """A status is always a word plus a colour, never a colour on its own."""

    label: str
    tone: str


@dataclass(frozen=True)
class FlightView:
    """A booking, its newest snapshot, both airports and the passenger, for a template.

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
    def operating_flight(self) -> str | None:
        booking = self.booking
        if not booking.operating_carrier:
            return None
        return f"{booking.operating_carrier}{booking.operating_number or ''}"

    @property
    def passenger_name(self) -> str:
        return self.passenger.display_name if self.passenger else MISSING

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
    def departs(self) -> Timeline:
        """The gate departure, resolved: what it is now and what it was booked as."""
        snap = self.snapshot
        return Timeline(
            scheduled=self.scheduled_departure,
            best=self.departure,
            actual=snap.actual_out if snap else None,
        )

    @property
    def arrives(self) -> Timeline:
        """Gate arrival: the end of the trip, and what the calendar entry runs to."""
        snap = self.snapshot
        best = arrival_estimate(self.booking, snap)
        return Timeline(
            scheduled=self.scheduled_arrival,
            best=best,
            actual=snap.actual_in if snap else None,
        )

    @property
    def lands(self) -> Timeline:
        """Wheels down, which is the question once the doors are shut."""
        snap = self.snapshot
        return Timeline(
            scheduled=snap.scheduled_on if snap else None,
            best=landing_estimate(self.booking, snap) if snap else None,
            actual=snap.actual_on if snap else None,
        )

    @property
    def cancelled(self) -> bool:
        snap = self.snapshot
        return self.booking.status == "cancelled" or bool(snap is not None and snap.cancelled)

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
        """When this flight stopped being something to wait for.

        The same rule `list_bookings(upcoming_only=True)` applies: a flight in the air
        has departed but is still very much upcoming to the person meeting it.
        """
        arrival = self.arrival
        if arrival is not None:
            return arrival
        # Nothing anywhere says when it lands, so assume the longest plausible hop
        # rather than pinning a departed flight to the top of the list forever.
        return self.departure + timedelta(hours=3)

    def raw(self, *path: str) -> Any:
        """A field out of the stored AeroAPI object, or None if it is not there.

        Everything AeroAPI returns that is not worth its own column lives in `raw`, and
        every one of those fields is optional in practice.
        """
        value: Any = self.snapshot.raw if self.snapshot else None
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value


@dataclass(frozen=True)
class Timeline:
    """One event's three-state answer, collapsed to what a reader needs to see.

    Scheduled, estimated and actual describe the same moment at three levels of
    certainty, so showing them as three rows makes a reader work out which is current.
    This carries the best answer, and the original only when it is worth striking out.
    """

    scheduled: datetime | None
    best: datetime | None
    actual: datetime | None

    @property
    def confirmed(self) -> bool:
        return self.actual is not None

    @property
    def moved(self) -> timedelta | None:
        """How far the best answer has drifted from the schedule, when it matters."""
        if self.scheduled is None or self.best is None:
            return None
        shift = self.best - self.scheduled
        return shift if abs(shift) >= DELAY_THRESHOLD else None


def duration(delta: timedelta) -> str:
    """`45m`, `1h 20m`. Used for delays, so the caller carries the sign."""
    minutes = int(abs(delta).total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def day(instant: datetime, tz: str) -> str:
    """`Sat 12 Sep`, the heading a trip is filed under."""
    return to_local(instant, tz).strftime("%a %-d %b")


def same_day(a: FlightView, b: FlightView) -> bool:
    """Whether two flights leave on the same day, each read at its own airport."""
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
    """AeroAPI files altitude in hundreds of feet, which is to say a flight level."""
    if not isinstance(value, int):
        return MISSING
    return f"FL{value:03d}" if value < 600 else f"{value:,} ft"


def distance(value: Any) -> str:
    """AeroAPI reports route distance in statute miles."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return MISSING
    return f"{int(value):,} mi"


def group_into_trips(views: Sequence[FlightView]) -> list[list[FlightView]]:
    """Split departure-ordered flights into the runs that belong to one journey."""
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
        .where(FlightSnapshot.booking_id.in_(list(booking_ids)))
        .order_by(FlightSnapshot.booking_id, FlightSnapshot.observed_at.desc())
    )
    newest: dict[int, FlightSnapshot] = {}
    for snapshot in rows.scalars():
        newest.setdefault(snapshot.booking_id, snapshot)
    return newest


async def build_views(session: AsyncSession, rows: Iterable[Booking]) -> list[FlightView]:
    bookings = list(rows)
    snapshots = await latest_snapshots(session, [booking.id for booking in bookings])

    airports: dict[str, Airport | None] = {}
    for booking in bookings:
        for iata in (booking.origin_iata, booking.dest_iata):
            if iata not in airports:
                airports[iata] = await get_airport(session, iata)

    # Fetched up front rather than through booking.passenger: a lazy relationship on an
    # async session raises instead of quietly emitting the query a template expects.
    people = await session.execute(
        select(Passenger).where(Passenger.id.in_({booking.passenger_id for booking in bookings}))
    )
    by_id = {person.id: person for person in people.scalars()}

    views = [
        FlightView(
            booking=booking,
            snapshot=snapshots.get(booking.id),
            origin=airports.get(booking.origin_iata),
            dest=airports.get(booking.dest_iata),
            passenger=by_id.get(booking.passenger_id),
        )
        for booking in bookings
    ]
    views.sort(key=lambda view: view.scheduled_departure)
    return views


async def list_passengers(session: AsyncSession) -> list[Passenger]:
    rows = await session.execute(
        select(Passenger).order_by(Passenger.is_self.desc(), Passenger.display_name)
    )
    return list(rows.scalars())


def local_input(instant: datetime | None, tz: str) -> str:
    """A UTC instant as the wall clock its airport reads, for a datetime-local input.

    The form never shows a UTC time. What the user typed is what the ticket says.
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


@dataclass
class FlightForm:
    """The add and edit forms, which post exactly the same fields."""

    passenger_id: Annotated[int, Form()]
    marketing_carrier: Annotated[str, Form()]
    marketing_number: Annotated[str, Form()]
    origin_iata: Annotated[str, Form()]
    dest_iata: Annotated[str, Form()]
    departure_local: Annotated[str, Form()]
    arrival_local: Annotated[str, Form()] = ""
    confirmation_code: Annotated[str, Form()] = ""
    seat: Annotated[str, Form()] = ""
    notes: Annotated[str, Form()] = ""

    @property
    def departure(self) -> datetime | None:
        return parse_local(self.departure_local)

    @property
    def arrival(self) -> datetime | None:
        return parse_local(self.arrival_local)

    def optional(self, name: str) -> str | None:
        value: str = getattr(self, name)
        return value.strip() or None

    def as_posted(self) -> dict[str, Any]:
        """What the user typed, so a rejected form comes back filled in."""
        return {field.name: getattr(self, field.name) for field in fields(self)}


FormDep = Annotated[FlightForm, Depends()]


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="flighter")
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

    def from_htmx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    async def flight_form_page(
        request: Request,
        session: AsyncSession,
        view: FlightView | None,
        error: str | None = None,
        posted: dict[str, Any] | None = None,
    ) -> Response:
        return page(
            request,
            "form.html",
            {
                "view": view,
                "passengers": await list_passengers(session),
                "error": error,
                "form": posted or {},
            },
            status_code=400 if error else 200,
        )

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
        return (await build_views(session, [booking]))[0]

    def base_url() -> str:
        """Where this deployment answers, for a link that leaves the browser.

        The stored value wins because a push notification is opened long after the
        request that would have implied one.
        """
        return prefs.current().public_base_url

    @app.get("/")
    async def index(request: Request, session: SessionDep) -> Response:
        views = await build_views(
            session, await booking_repo.list_bookings(session, statuses=LISTED_STATUSES)
        )
        now = datetime.now(UTC)
        upcoming = [view for view in views if view.ended >= now]
        past = [view for view in views if view.ended < now]
        past.reverse()
        pending = await booking_repo.list_bookings(session, statuses=("pending_review",))
        return page(
            request,
            "index.html",
            {
                "trips": group_into_trips(upcoming),
                "past": past,
                "pending": len(pending),
                "budget": await budget_status(session),
            },
        )

    # Declared before /f/{booking_id} so that "new" is never read as an id.
    @app.get("/f/new")
    async def new_flight(request: Request, session: SessionDep) -> Response:
        return await flight_form_page(request, session, view=None)

    @app.post("/f")
    async def create_flight(request: Request, session: SessionDep, form: FormDep) -> Response:
        if form.departure is None:
            return await flight_form_page(
                request, session, None, "Departure needs a date and a time.", form.as_posted()
            )
        try:
            booking = await booking_repo.create_booking(
                session,
                passenger_id=form.passenger_id,
                marketing_carrier=form.marketing_carrier,
                marketing_number=form.marketing_number,
                origin_iata=form.origin_iata,
                dest_iata=form.dest_iata,
                departure_local=form.departure,
                arrival_local=form.arrival,
                confirmation_code=form.optional("confirmation_code"),
                seat=form.optional("seat"),
                notes=form.optional("notes"),
                source="manual",
            )
        except IntegrityError:
            # The dedupe index caught a flight this passenger is already on. Roll back
            # or every query behind the re-rendered form fails too.
            await session.rollback()
            return await flight_form_page(
                request,
                session,
                None,
                "That passenger is already booked on this flight that day.",
                form.as_posted(),
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
        return await flight_form_page(request, session, await load(session, booking_id))

    @app.post("/f/{booking_id}")
    async def update_flight(
        request: Request, session: SessionDep, booking_id: int, form: FormDep
    ) -> Response:
        view = await load(session, booking_id)
        if form.departure is None:
            return await flight_form_page(
                request, session, view, "Departure needs a date and a time.", form.as_posted()
            )
        origin = form.origin_iata.strip().upper()
        dest = form.dest_iata.strip().upper()
        # update_booking takes column values verbatim, so the wall clock has to become
        # UTC here - through the booking layer's own conversion, never by hand.
        departure_utc, arrival_utc = booking_repo.to_booking_times(
            form.departure,
            await airport_tz(session, origin),
            form.arrival,
            await airport_tz(session, dest),
        )
        try:
            await booking_repo.update_booking(
                session,
                booking_id,
                passenger_id=form.passenger_id,
                # Upper-cased to match what create_booking stores: the dedupe index
                # compares these literally, and "aa" would read as another airline.
                marketing_carrier=form.marketing_carrier.strip().upper(),
                marketing_number=form.marketing_number.strip(),
                origin_iata=origin,
                dest_iata=dest,
                scheduled_departure_utc=departure_utc,
                scheduled_arrival_utc=arrival_utc,
                confirmation_code=form.optional("confirmation_code"),
                seat=form.optional("seat"),
                notes=form.optional("notes"),
            )
        except IntegrityError:
            await session.rollback()
            return await flight_form_page(
                request,
                session,
                view,
                "That passenger is already booked on this flight that day.",
                form.as_posted(),
            )
        return RedirectResponse(f"/f/{booking_id}", status_code=303)

    # An HTML form can only send GET or POST, so removal is a POST to its own path
    # rather than DELETE /f/{id}.
    @app.post("/f/{booking_id}/delete")
    async def delete_flight(request: Request, session: SessionDep, booking_id: int) -> Response:
        booking = await booking_repo.get_booking(session, booking_id)
        if booking is None:
            raise HTTPException(status_code=404, detail="No such flight.")
        if booking.gcal_event_id:
            try:
                await CalendarClient(settings).delete(booking)
            except Exception:
                # The booking still goes. An orphaned calendar event is worth less than
                # a list that refuses to let go of a trip that is not happening.
                log.warning("could not delete the calendar event for booking %s", booking_id)
        await booking_repo.delete_booking(session, booking_id)
        if from_htmx(request):
            return Response(status_code=200)
        return RedirectResponse("/", status_code=303)

    @app.get("/review")
    async def review(request: Request, session: SessionDep) -> Response:
        rows = await booking_repo.list_bookings(session, statuses=("pending_review",))
        return page(request, "review.html", {"views": await build_views(session, rows)})

    @app.post("/review/{booking_id}/accept")
    async def accept(request: Request, session: SessionDep, booking_id: int) -> Response:
        if await booking_repo.update_booking(session, booking_id, status="active") is None:
            raise HTTPException(status_code=404, detail="No such flight.")
        if from_htmx(request):
            return Response(status_code=200)
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/{booking_id}/reject")
    async def reject(request: Request, session: SessionDep, booking_id: int) -> Response:
        # Archived rather than deleted, the same as any other removal: the dedupe index
        # skips archived rows, so the same email may be extracted again later.
        if await booking_repo.delete_booking(session, booking_id) is None:
            raise HTTPException(status_code=404, detail="No such flight.")
        if from_htmx(request):
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
        if from_htmx(request):
            # Added from inside the flight form: hand back the option to select.
            return page(request, "option.html", {"passenger": passenger})
        return RedirectResponse("/passengers", status_code=303)

    @app.post("/passengers/{passenger_id}/delete")
    async def delete_passenger(
        request: Request, session: SessionDep, passenger_id: int
    ) -> Response:
        passenger = await session.get(Passenger, passenger_id)
        if passenger is None:
            raise HTTPException(status_code=404, detail="No such passenger.")
        booked = await session.scalar(
            select(func.count()).select_from(Booking).where(Booking.passenger_id == passenger_id)
        )
        if booked:
            raise HTTPException(
                status_code=400, detail="That passenger still has flights on the list."
            )
        await session.delete(passenger)
        if from_htmx(request):
            return Response(status_code=200)
        return RedirectResponse("/passengers", status_code=303)

    async def settings_context(request: Request) -> dict[str, Any]:
        current = prefs.current()
        return {
            "prefs": current,
            "posted": current.model_dump(mode="json"),
            "settings": settings,
            "log_levels": LOG_LEVELS,
            "callback_url": google_auth.callback_url(base_url()),
            # What the browser is talking to right now, offered as the public base URL
            # because on a first visit it is almost always the right answer.
            "this_origin": str(request.base_url).rstrip("/"),
            "saved": "saved" in request.query_params,
            "connected": "connected" in request.query_params,
            "error": None,
        }

    @app.get("/settings")
    async def settings_page(request: Request) -> Response:
        return page(request, "settings.html", await settings_context(request))

    @app.post("/settings")
    async def save_settings(
        request: Request,
        session: SessionDep,
        public_base_url: Annotated[str, Form()],
        log_level: Annotated[str, Form()],
        aeroapi_monthly_cap_usd: Annotated[str, Form()],
        aeroapi_rate_limit_per_minute: Annotated[str, Form()],
        anthropic_model: Annotated[str, Form()],
        extraction_confidence_threshold: Annotated[str, Form()],
        gmail_poll_seconds: Annotated[str, Form()],
        ntfy_url: Annotated[str, Form()],
        ntfy_topic: Annotated[str, Form()],
        gcal_calendar_id: Annotated[str, Form()] = "",
    ) -> Response:
        posted = {
            "public_base_url": public_base_url.strip(),
            "log_level": log_level.strip().upper(),
            "aeroapi_monthly_cap_usd": aeroapi_monthly_cap_usd.strip(),
            "aeroapi_rate_limit_per_minute": aeroapi_rate_limit_per_minute.strip(),
            "anthropic_model": anthropic_model.strip(),
            "extraction_confidence_threshold": extraction_confidence_threshold.strip(),
            "gmail_poll_seconds": gmail_poll_seconds.strip(),
            "ntfy_url": ntfy_url.strip(),
            "ntfy_topic": ntfy_topic.strip(),
            "gcal_calendar_id": gcal_calendar_id.strip(),
        }
        try:
            await prefs.save(session, posted)
        except ValidationError as exc:
            context = await settings_context(request)
            context["error"] = _first_validation_message(exc)
            context["posted"] = posted
            return page(request, "settings.html", context, status_code=400)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.get("/settings/google/connect")
    async def google_connect(session: SessionDep) -> Response:
        if not settings.google_configured:
            raise HTTPException(
                status_code=400,
                detail="Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first.",
            )
        url, state = google_auth.consent_url(settings, google_auth.callback_url(base_url()))
        await session.merge(KV(key=OAUTH_STATE_KEY, value={"state": state}))
        return RedirectResponse(url, status_code=303)

    @app.get("/settings/google/callback")
    async def google_callback(
        request: Request, session: SessionDep, state: str = "", code: str = "", error: str = ""
    ) -> Response:
        if error:
            raise HTTPException(status_code=400, detail=f"Google refused: {error}")
        stored = await session.get(KV, OAUTH_STATE_KEY)
        if stored is None or not secrets.compare_digest(state, str(stored.value.get("state", ""))):
            raise HTTPException(status_code=400, detail="That sign-in did not start here.")
        await session.delete(stored)
        authorised = await google_auth.exchange_code(
            settings, google_auth.callback_url(base_url()), state, code
        )
        # The mail loop holds a client built from the dead token; drop it so the next
        # pass builds one with the token that was just granted.
        gmail.reset_service()
        if not prefs.current().calendar_configured:
            await prefs.save(session, {"gcal_calendar_id": await gcal.create_calendar(authorised)})
        return RedirectResponse("/settings?connected=1", status_code=303)

    @app.post("/settings/checks")
    async def run_checks_now(request: Request) -> Response:
        return page(request, "checks.html", {"results": await run_checks(settings)})

    @app.get("/health")
    async def health(request: Request, session: SessionDep) -> Response:
        counts = await session.execute(
            select(Booking.status, func.count()).group_by(Booking.status)
        )
        last_snapshot = await session.scalar(select(func.max(FlightSnapshot.observed_at)))
        state = await session.execute(select(KV).order_by(KV.key))
        return page(
            request,
            "health.html",
            {
                "counts": {status: count for status, count in counts.all()},
                "last_snapshot": last_snapshot,
                "budget": await budget_status(session),
                # The poller and the Gmail sync each own their own keys in here, so the
                # page reports what is in the table rather than asserting a shape.
                "state": [
                    (row.key, json.dumps(row.value, indent=2, default=str))
                    for row in state.scalars()
                ],
                "settings": settings,
                "prefs": prefs.current(),
            },
        )

    # A service worker may only control paths below its own, so this one is served from
    # the root even though it lives with the rest of the static files.
    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        return FileResponse(STATIC / "sw.js", media_type="text/javascript")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness for the container health check, which must not touch the database."""
        return JSONResponse({"status": "ok"})

    return app
