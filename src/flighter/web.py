"""The pages a person actually looks at: the board, the flight, the settings.

Everything is rendered on the server into one HTML response. The reader is standing in
a terminal on hotel wifi wanting to know a gate number, so there is no framework to boot
and nothing fetched after paint. What a template is handed is built in `views`; this
module is the routes and nothing else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import bookings as booking_repo
from . import ingest, prefs, views
from .aeroapi import budget_status, clear_breaker
from .airports import UnknownAirport
from .caldav import CalendarClient, CalendarUnavailable, Collection
from .checks import run_checks
from .config import CREDENTIALS, SERVICES, Settings, mint_widget_token, write_secrets
from .db import get_session
from .mail import FLAG_COLOURS
from .models import Booking, BookingSource, BookingStatus, FlightEvent
from .phase import CANCELLED_NOTICE
from .views import FlightView, build_views
from .widget import connect_url, last_seen, script_source
from .widget import router as widget_router

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

# What the board shows above the fold. Archived bookings are gone as far as the UI is
# concerned, and a completed one belongs under Flown.
BOARD_STATUSES = (BookingStatus.ACTIVE, BookingStatus.PENDING_REVIEW)

# Flown flights are history, and history is not what this screen is for. Five is enough
# to recognise the trip you just took and few enough to keep the query bounded.
FLOWN_LIMIT = 5

# What the board's one-tap button adds to the monthly limit. Big enough to buy a few
# hundred more polls, small enough that pressing it twice is not a surprise bill.
LIMIT_STEP = Decimal("2.00")

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# iOS reloads a widget on its own schedule, and a phone left face down for an afternoon
# is not a broken one. A day without a fetch is.
WIDGET_QUIET_AFTER = timedelta(days=1)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def ago(then: datetime, now: datetime) -> str:
    """`4m ago`, `3d ago`: how long since a phone was last heard from."""
    elapsed = now - then
    if elapsed >= timedelta(days=1):
        return f"{elapsed.days}d ago"
    return f"{views.duration(elapsed)} ago"


def _first_validation_message(exc: ValidationError) -> str:
    """One field, one sentence. A wall of pydantic is not an error message."""
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error["loc"]) or "value"
    return f"{field.replace('_', ' ')}: {error['msg']}"


@dataclass
class FlightForm:
    """The add and edit forms, which post exactly the same fields."""

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
        return views.parse_local(self.departure_local)

    @property
    def arrival(self) -> datetime | None:
        return views.parse_local(self.arrival_local)

    def optional(self, name: str) -> str | None:
        value: str = getattr(self, name)
        return value.strip() or None

    def as_posted(self) -> dict[str, Any]:
        """What the user typed, so a rejected form comes back filled in."""
        return {field.name: getattr(self, field.name) for field in fields(self)}


FormDep = Annotated[FlightForm, Depends()]


async def recently_flown(session: AsyncSession, limit: int) -> list[Booking]:
    """The last few flights that have been taken, newest first and never more."""
    rows = await session.execute(
        select(Booking)
        .where(Booking.status == BookingStatus.COMPLETED)
        .order_by(Booking.scheduled_departure_utc.desc())
        .limit(limit)
    )
    return list(rows.scalars())


def create_app(settings: Settings) -> FastAPI:
    # Nothing here is an API anybody writes against, and a schema of every route is a
    # map of the house for whatever reaches the port.
    app = FastAPI(title="flighter", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(widget_router)

    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.globals.update(
        at=views.at,
        change_title=views.change_title,
        change_value=views.change_value,
        dash=views.dash,
        day=views.day,
        duration=views.duration,
        local_input=views.local_input,
        missing=views.MISSING,
        same_day=views.same_day,
        until=views.until,
    )

    def page(request: Request, name: str, context: dict[str, Any], **kwargs: Any) -> Response:
        return templates.TemplateResponse(request, name, context, **kwargs)

    def error_page(request: Request, code: int, detail: str) -> Response:
        if request.url.path.startswith(("/api/", "/healthz")):
            return JSONResponse({"detail": detail}, status_code=code)
        return page(request, "error.html", {"code": code, "detail": detail}, status_code=code)

    def flight_form_page(
        request: Request,
        view: FlightView | None,
        error: str | None = None,
        posted: dict[str, Any] | None = None,
    ) -> Response:
        return page(
            request,
            "form.html",
            {"view": view, "error": error, "form": posted or {}},
            status_code=400 if error else 200,
        )

    @app.exception_handler(HTTPException)
    async def on_http_error(request: Request, exc: HTTPException) -> Response:
        return error_page(request, exc.status_code, exc.detail)

    @app.exception_handler(Exception)
    async def on_unhandled_error(request: Request, exc: Exception) -> Response:
        """A page rather than Starlette's plain text, and the traceback in the log.

        Whatever broke is a bug here, so the reader gets a way back to the board and the
        detail goes where it can be read: into the logs, not onto a screen in an airport.
        """
        log.exception("unhandled error serving %s", request.url.path)
        return error_page(request, 500, "Something went wrong.")

    async def load(session: AsyncSession, booking_id: int) -> FlightView:
        booking = await booking_repo.get_booking(session, booking_id)
        if booking is None:
            raise HTTPException(status_code=404, detail="No such flight.")
        return (await build_views(session, [booking]))[0]

    @app.get("/")
    async def board(request: Request, session: SessionDep) -> Response:
        tracked = await build_views(
            session, await booking_repo.list_bookings(session, statuses=BOARD_STATUSES)
        )
        now = datetime.now(UTC)
        upcoming = [view for view in tracked if view.ended >= now]
        landed = [view for view in tracked if view.ended < now]
        flown = await build_views(session, await recently_flown(session, FLOWN_LIMIT))
        past = sorted(landed + flown, key=lambda view: view.scheduled_departure, reverse=True)
        budget = await budget_status(session)
        return page(
            request,
            "index.html",
            {
                "trips": views.group_into_trips(upcoming),
                "past": past[:FLOWN_LIMIT],
                "urgent_id": views.most_urgent(upcoming),
                "budget": budget,
                "raised_cap": budget.cap_usd + LIMIT_STEP,
                "set_aside": await ingest.list_set_aside(session),
                # An empty board on a fresh deployment is not the same thing as an empty
                # board on a working one, and only one of them is worth a signpost.
                "set_up": settings.icloud_configured or settings.aeroapi_configured,
            },
        )

    @app.post("/limit")
    async def raise_limit(session: SessionDep) -> Response:
        """Raise the monthly limit and start polling again, from the board.

        The breaker latches so that a restart stays stopped, which means raising the cap
        has to unlatch it too or nothing visibly happens.
        """
        cap = prefs.current().aeroapi_monthly_cap_usd + LIMIT_STEP
        await prefs.save(session, {"aeroapi_monthly_cap_usd": str(cap)})
        await clear_breaker(session)
        return RedirectResponse("/", status_code=303)

    @app.post("/mail/retry")
    async def retry_message(session: SessionDep, message_id: Annotated[str, Form()]) -> Response:
        """Hand one set-aside email back to the mail watcher.

        Nothing is reprocessed here: the message is still flagged in Mail, so all this
        does is clear the record of having given up and let the next sweep find it.
        """
        if await ingest.retry(session, message_id) is None:
            raise HTTPException(status_code=404, detail="That email is not set aside.")
        return RedirectResponse("/", status_code=303)

    @app.post("/mail/ignore")
    async def ignore_message(session: SessionDep, message_id: Annotated[str, Form()]) -> Response:
        """Decide the email holds no flight, which is what takes its flag off in Mail."""
        if await ingest.dismiss(session, message_id) is None:
            raise HTTPException(status_code=404, detail="That email is not set aside.")
        return RedirectResponse("/", status_code=303)

    # Declared before /f/{booking_id} so that "new" is never read as an id.
    @app.get("/f/new")
    async def new_flight(request: Request) -> Response:
        return flight_form_page(request, view=None)

    @app.post("/f")
    async def create_flight(request: Request, session: SessionDep, form: FormDep) -> Response:
        if form.departure is None:
            return flight_form_page(
                request, None, "Departure needs a date and a time.", form.as_posted()
            )
        try:
            booking = await booking_repo.create_booking(
                session,
                marketing_carrier=form.marketing_carrier,
                marketing_number=form.marketing_number,
                origin_iata=form.origin_iata,
                dest_iata=form.dest_iata,
                departure_local=form.departure,
                arrival_local=form.arrival,
                confirmation_code=form.optional("confirmation_code"),
                seat=form.optional("seat"),
                notes=form.optional("notes"),
                source=BookingSource.MANUAL,
            )
        except UnknownAirport as exc:
            return flight_form_page(request, None, _unknown_airport(exc), form.as_posted())
        except IntegrityError:
            # The dedupe index caught a flight already on the list. Roll back or every
            # query behind the re-rendered form fails too.
            await session.rollback()
            return flight_form_page(
                request, None, "That flight is already on the list for that day.", form.as_posted()
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
        return page(
            request,
            "detail.html",
            {"v": view, "events": list(events.scalars()), "cancelled_notice": CANCELLED_NOTICE},
        )

    @app.get("/f/{booking_id}/edit")
    async def edit_flight(request: Request, session: SessionDep, booking_id: int) -> Response:
        return flight_form_page(request, await load(session, booking_id))

    @app.post("/f/{booking_id}")
    async def update_flight(
        request: Request, session: SessionDep, booking_id: int, form: FormDep
    ) -> Response:
        view = await load(session, booking_id)
        if form.departure is None:
            return flight_form_page(
                request, view, "Departure needs a date and a time.", form.as_posted()
            )
        try:
            departure_utc, arrival_utc = await views.utc_times(
                session, form.origin_iata, form.dest_iata, form.departure, form.arrival
            )
            await booking_repo.update_booking(
                session,
                booking_id,
                marketing_carrier=form.marketing_carrier,
                marketing_number=form.marketing_number,
                origin_iata=form.origin_iata,
                dest_iata=form.dest_iata,
                scheduled_departure_utc=departure_utc,
                scheduled_arrival_utc=arrival_utc,
                confirmation_code=form.optional("confirmation_code"),
                seat=form.optional("seat"),
                notes=form.optional("notes"),
            )
        except UnknownAirport as exc:
            return flight_form_page(request, view, _unknown_airport(exc), form.as_posted())
        except IntegrityError:
            await session.rollback()
            return flight_form_page(
                request, view, "That flight is already on the list for that day.", form.as_posted()
            )
        return RedirectResponse(f"/f/{booking_id}", status_code=303)

    @app.post("/f/{booking_id}/keep")
    async def keep_flight(session: SessionDep, booking_id: int) -> Response:
        """Confirm a booking we were unsure about, which is what starts it being polled."""
        booking = await booking_repo.update_booking(
            session, booking_id, status=BookingStatus.ACTIVE
        )
        if booking is None:
            raise HTTPException(status_code=404, detail="No such flight.")
        return RedirectResponse(f"/f/{booking_id}", status_code=303)

    # An HTML form can only send GET or POST, so removal is a POST to its own path
    # rather than DELETE /f/{id}.
    @app.post("/f/{booking_id}/delete")
    async def delete_flight(session: SessionDep, booking_id: int) -> Response:
        booking = await booking_repo.get_booking(session, booking_id)
        if booking is None:
            raise HTTPException(status_code=404, detail="No such flight.")
        if booking.calendar_event_uid:
            try:
                await CalendarClient(settings).delete(booking)
            except Exception:
                # The booking still goes. An orphaned calendar event is worth less than
                # a board that refuses to let go of a trip that is not happening.
                log.warning("could not delete the calendar event for booking %s", booking_id)
        await booking_repo.delete_booking(session, booking_id)
        return RedirectResponse("/", status_code=303)

    async def settings_context(request: Request, session: AsyncSession) -> dict[str, Any]:
        current = prefs.current()
        this_origin = str(request.base_url).rstrip("/")
        posted = current.model_dump(mode="json")
        # Whatever the page was opened on is almost always the right answer, so a
        # deployment nobody has told yet is shown that rather than the default.
        if current.public_base_url == prefs.Prefs.model_fields["public_base_url"].default:
            posted["public_base_url"] = this_origin
        calendars, calendar_error = await offered_calendars(settings.icloud_configured)
        now = datetime.now(UTC)
        seen = await last_seen(session)
        return {
            "prefs": current,
            "posted": posted,
            # Booleans, never values: a stored credential is never rendered back into the
            # page. The Apple ID is the exception, because it names the account rather
            # than proving anything about it.
            "connected": {name: bool(getattr(settings, name)) for name in CREDENTIALS},
            "icloud_email": settings.icloud_email,
            # The widget token is the other exception: it is handed to your own phone,
            # through the Connect link, and this page is where the phone gets it from.
            "widget_token": settings.widget_token,
            "widget_connect_url": connect_url(settings),
            "widget_script": script_source(),
            "widget_last_seen": ago(seen, now) if seen else None,
            "widget_connected": seen is not None and now - seen < WIDGET_QUIET_AFTER,
            "log_levels": LOG_LEVELS,
            "flag_colours": tuple(FLAG_COLOURS),
            "calendars": calendars,
            "calendar_error": calendar_error,
            "budget": await budget_status(session),
            "saved": "saved" in request.query_params,
            "tab": request.query_params.get("tab"),
            "error": None,
        }

    async def offered_calendars(configured: bool) -> tuple[list[Collection], str | None]:
        """The account's calendars for the picker, or why there are none to offer.

        Discovery is a network call on a page render, so it is only made once there are
        credentials for it to use, and it is allowed to fail: a settings page that says
        iCloud cannot be reached is worth far more than one that will not open.
        """
        if not configured:
            return [], None
        try:
            return await CalendarClient(settings).calendars(), None
        except CalendarUnavailable:
            log.warning("could not list the iCloud calendars", exc_info=True)
            return [], "iCloud did not answer, so its calendars cannot be listed right now."
        except Exception:
            log.warning("could not list the iCloud calendars", exc_info=True)
            return [], "Something went wrong listing the calendars on this account."

    @app.get("/settings")
    async def settings_page(request: Request, session: SessionDep) -> Response:
        return page(request, "settings.html", await settings_context(request, session))

    @app.post("/settings")
    async def save_settings(
        request: Request,
        session: SessionDep,
        public_base_url: Annotated[str | None, Form()] = None,
        log_level: Annotated[str | None, Form()] = None,
        aeroapi_monthly_cap_usd: Annotated[str | None, Form()] = None,
        imap_flag_colour: Annotated[str | None, Form()] = None,
        icloud_calendar_url: Annotated[str | None, Form()] = None,
    ) -> Response:
        """Save whichever preferences were posted.

        Each card is its own form, so what arrives is a slice rather than the lot, and
        a field nobody sent is one nobody touched.
        """
        entered = {
            "public_base_url": public_base_url,
            "log_level": log_level.upper() if log_level is not None else None,
            "aeroapi_monthly_cap_usd": aeroapi_monthly_cap_usd,
            "imap_flag_colour": imap_flag_colour,
            "icloud_calendar_url": icloud_calendar_url,
        }
        posted = {name: value.strip() for name, value in entered.items() if value is not None}
        try:
            updated = await prefs.save(session, posted)
        except ValidationError as exc:
            context = await settings_context(request, session)
            context["error"] = _first_validation_message(exc)
            context["posted"] = context["posted"] | posted
            return page(request, "settings.html", context, status_code=400)
        # Applied here rather than only at boot, so turning the logs up to find out what
        # is going wrong does not need the restart that would clear the evidence.
        logging.getLogger().setLevel(updated.log_level.upper())
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/credentials")
    async def save_credentials(
        service: Annotated[str, Form()],
        forget: Annotated[str, Form()] = "",
        icloud_email: Annotated[str, Form()] = "",
        icloud_app_password: Annotated[str, Form()] = "",
        aeroapi_key: Annotated[str, Form()] = "",
        anthropic_api_key: Annotated[str, Form()] = "",
        pushover_token: Annotated[str, Form()] = "",
        pushover_user_key: Annotated[str, Form()] = "",
    ) -> Response:
        """Store one service's credentials, or forget them.

        Saved one service at a time so that the boxes on the page and the values on file
        can never disagree: nothing is shown back, so a form covering all of them would
        have no way to say which blank boxes were meant.
        """
        found = next((candidate for candidate in SERVICES if candidate.key == service), None)
        if found is None:
            raise HTTPException(status_code=404, detail="No such connection.")
        entered = {
            "icloud_email": icloud_email,
            "icloud_app_password": icloud_app_password,
            "aeroapi_key": aeroapi_key,
            "anthropic_api_key": anthropic_api_key,
            "pushover_token": pushover_token,
            "pushover_user_key": pushover_user_key,
        }
        changed = _merged(found.fields, entered, forget=bool(forget))
        if changed:
            write_secrets(changed)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/widget/token")
    async def rotate_widget_token() -> Response:
        mint_widget_token()
        return RedirectResponse("/settings?saved=1&tab=widget", status_code=303)

    @app.post("/settings/checks")
    async def run_checks_now(request: Request) -> Response:
        return page(request, "checks.html", {"results": await run_checks(settings)})

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


def _unknown_airport(exc: UnknownAirport) -> str:
    return f"{exc.iata} is not an airport we know."


def _merged(names: tuple[str, ...], entered: dict[str, str], *, forget: bool) -> dict[str, str]:
    """What to write for one service: what was typed, or blanks to clear it.

    An empty box means "leave this one alone", because the page never shows a stored
    credential back for it to have been left in. Forget is what clears.
    """
    if forget:
        return dict.fromkeys(names, "")
    return {name: entered[name].strip() for name in names if entered[name].strip()}
