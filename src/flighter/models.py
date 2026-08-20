"""The database schema.

Two ideas shape it. Bookings are what the user (or an email) asserts about a trip and
are edited freely. Snapshots are what AeroAPI observed, are append-only, and are never
corrected - change detection is a diff of the newest two rows, so rewriting history
would silently erase events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

BOOKING_STATUSES = ("pending_review", "active", "completed", "cancelled", "archived")
BOOKING_SOURCES = ("email", "manual")
INGEST_OUTCOMES = ("created", "duplicate", "no_flight", "review", "error")


class Base(DeclarativeBase):
    pass


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is UTC on both sides of the connection, or it does not go in.

    SQLite has no `timestamptz`. It stores whatever wall clock it is handed and returns
    it naive, so an aware value in another zone would be written with its offset silently
    discarded and a naive one would be read back as though it were local time. Every
    instant in this database is UTC; this is where that stops being a convention held by
    every call site and becomes a rule the column enforces.

    A naive datetime is refused rather than assumed to be UTC. In this codebase a naive
    datetime is a wall-clock reading at an airport, and guessing at which airport is the
    class of bug the whole timezone policy exists to prevent.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "refusing to store a naive datetime: it is a wall-clock reading, not an "
                "instant. Convert it with timezones.to_utc() first."
            )
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def _created_at() -> Any:
    # `func.now()` is CURRENT_TIMESTAMP, which SQLite computes in UTC, so the default
    # lands in the same frame as everything written through the type above.
    return mapped_column(UtcDateTime, nullable=False, server_default=func.now())


class Airport(Base):
    """Seeded once at startup; the only source of truth for an airport's timezone."""

    __tablename__ = "airports"

    iata: Mapped[str] = mapped_column(String(3), primary_key=True)
    icao: Mapped[str | None] = mapped_column(String(4))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    tz: Mapped[str] = mapped_column(Text, nullable=False)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("source IN ('email', 'manual')", name="bookings_source_check"),
        CheckConstraint(
            "status IN ('pending_review', 'active', 'completed', 'cancelled', 'archived')",
            name="bookings_status_check",
        ),
        # A codeshare of the same physical flight must not become a second booking.
        # Archived rows are excluded so a deleted-and-re-added booking is allowed.
        # The departure *date* rather than the instant: the same booking re-sent with a
        # slightly different time must collide, and a genuine second flight on the same
        # route the same day is not a thing anyone does. `date()` reads the stored text
        # directly, and every instant in the column is UTC, so the day it yields is the
        # UTC calendar day with no zone conversion to get wrong.
        Index(
            "bookings_dedupe",
            "marketing_carrier",
            "marketing_number",
            text("date(scheduled_departure_utc)"),
            unique=True,
            sqlite_where=text("status != 'archived'"),
        ),
        Index(
            "bookings_poll",
            "next_poll_at",
            sqlite_where=text("status = 'active' AND next_poll_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(Text)

    # Marketing is what the ticket says; operating is what AeroAPI actually tracks.
    marketing_carrier: Mapped[str] = mapped_column(Text, nullable=False)
    marketing_number: Mapped[str] = mapped_column(Text, nullable=False)
    operating_carrier: Mapped[str | None] = mapped_column(Text)
    operating_number: Mapped[str | None] = mapped_column(Text)
    aeroapi_ident: Mapped[str | None] = mapped_column(Text)
    # Pinned on first successful resolution; unambiguous, so later polls skip matching.
    aeroapi_fa_flight_id: Mapped[str | None] = mapped_column(Text)

    origin_iata: Mapped[str] = mapped_column(String(3), ForeignKey("airports.iata"), nullable=False)
    dest_iata: Mapped[str] = mapped_column(String(3), ForeignKey("airports.iata"), nullable=False)
    scheduled_departure_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    scheduled_arrival_utc: Mapped[datetime | None] = mapped_column(UtcDateTime)

    confirmation_code: Mapped[str | None] = mapped_column(Text)
    seat: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    calendar_event_uid: Mapped[str | None] = mapped_column(Text)
    next_poll_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    snapshots: Mapped[list[FlightSnapshot]] = relationship(
        back_populates="booking", cascade="all, delete-orphan"
    )
    events: Mapped[list[FlightEvent]] = relationship(
        back_populates="booking", cascade="all, delete-orphan"
    )


class FlightSnapshot(Base):
    """One AeroAPI observation. Append-only: never UPDATE a row in this table."""

    __tablename__ = "flight_snapshots"
    __table_args__ = (Index("flight_snapshots_latest", "booking_id", text("observed_at DESC")),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[datetime] = _created_at()
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Denormalised out of `raw` so diffing and list queries never parse JSON.
    status_text: Mapped[str | None] = mapped_column(Text)
    cancelled: Mapped[bool | None] = mapped_column(Boolean)
    diverted: Mapped[bool | None] = mapped_column(Boolean)
    gate_origin: Mapped[str | None] = mapped_column(Text)
    gate_destination: Mapped[str | None] = mapped_column(Text)
    terminal_origin: Mapped[str | None] = mapped_column(Text)
    terminal_destination: Mapped[str | None] = mapped_column(Text)
    baggage_claim: Mapped[str | None] = mapped_column(Text)
    scheduled_out: Mapped[datetime | None] = mapped_column(UtcDateTime)
    estimated_out: Mapped[datetime | None] = mapped_column(UtcDateTime)
    actual_out: Mapped[datetime | None] = mapped_column(UtcDateTime)
    actual_off: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Runway arrival, kept alongside the gate times because "when do we land" is the
    # question in the air, and it is not the same question as "when are we at the gate".
    scheduled_on: Mapped[datetime | None] = mapped_column(UtcDateTime)
    estimated_on: Mapped[datetime | None] = mapped_column(UtcDateTime)
    scheduled_in: Mapped[datetime | None] = mapped_column(UtcDateTime)
    estimated_in: Mapped[datetime | None] = mapped_column(UtcDateTime)
    actual_in: Mapped[datetime | None] = mapped_column(UtcDateTime)
    actual_on: Mapped[datetime | None] = mapped_column(UtcDateTime)
    progress_percent: Mapped[int | None] = mapped_column(Integer)
    aircraft_type: Mapped[str | None] = mapped_column(Text)
    registration: Mapped[str | None] = mapped_column(Text)

    booking: Mapped[Booking] = relationship(back_populates="snapshots")


class FlightEvent(Base):
    """A material change, fanned out to Pushover and iCloud Calendar independently."""

    __tablename__ = "flight_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = _created_at()
    # Null means "not yet delivered"; each consumer claims its own column, so a failing
    # calendar never blocks a push and neither is ever delivered twice.
    notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    calendar_synced_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    booking: Mapped[Booking] = relationship(back_populates="events")


class IngestLog(Base):
    """Every email we have looked at, so a replay never re-processes one.

    Keyed on the RFC822 Message-ID rather than anything the server hands out: an IMAP
    UID belongs to one mailbox under one UIDVALIDITY, so keying on a UID would make the
    same confirmation, filed by hand in two places, look like two different emails.

    It is also the retry state. An `error` row keeps its message flagged and is tried
    again once `retry_at` has passed; a null `retry_at` means the message has either been
    decided or been set aside, and nothing will pick it up again without being asked.
    Having an outcome already on file is what stops a second push going out about the
    same email.
    """

    __tablename__ = "ingest_log"

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    processed_at: Mapped[datetime] = _created_at()
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    # Kept because a Message-ID names nothing a person recognises, and the set-aside list
    # on the health page has to say which email it is talking about.
    subject: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    raw_extraction: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    retry_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class ApiUsage(Base):
    """Estimated AeroAPI spend. The circuit breaker reads month-to-date sums off this."""

    __tablename__ = "api_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    called_at: Mapped[datetime] = _created_at()
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    result_sets: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    est_cost_usd: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False), nullable=False)


class Preferences(Base):
    """The settings page, as one row.

    One deployment means one row, and the JSON blob means a new knob costs a field on
    `Prefs` rather than a migration. Credentials are deliberately absent: they live in
    the environment and the app never writes them here.
    """

    __tablename__ = "preferences"
    __table_args__ = (CheckConstraint("id = 1", name="preferences_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KV(Base):
    """Small singleton state: breaker latches, and the like."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
