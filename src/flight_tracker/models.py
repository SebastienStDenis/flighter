"""The database schema.

Two ideas shape it. Bookings are what the user (or an email) asserts about a trip and
are edited freely. Snapshots are what AeroAPI observed, are append-only, and are never
corrected — change detection is a diff of the newest two rows, so rewriting history
would silently erase events.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

BOOKING_STATUSES = ("pending_review", "active", "completed", "cancelled", "archived")
BOOKING_SOURCES = ("email", "manual")
INGEST_OUTCOMES = ("created", "duplicate", "no_flight", "review", "error")


class Base(DeclarativeBase):
    pass


def _created_at() -> Any:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Passenger(Base):
    __tablename__ = "passengers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_self: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created_at()

    bookings: Mapped[list[Booking]] = relationship(back_populates="passenger")


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
        CheckConstraint(
            "source IN ('email', 'manual')", name="bookings_source_check"
        ),
        CheckConstraint(
            "status IN ('pending_review', 'active', 'completed', 'cancelled', 'archived')",
            name="bookings_status_check",
        ),
        # A codeshare of the same physical flight must not become a second booking for
        # the same passenger. Archived rows are excluded so a deleted-and-re-added
        # booking is allowed.
        # The departure *date* rather than the instant: the same booking re-sent with a
        # slightly different time must collide, and a genuine second flight on the same
        # route the same day is not a thing one passenger does.
        Index(
            "bookings_dedupe",
            "passenger_id",
            "marketing_carrier",
            "marketing_number",
            text("(scheduled_departure_utc AT TIME ZONE 'UTC')::date"),
            unique=True,
            postgresql_where=text("status != 'archived'"),
        ),
        Index(
            "bookings_poll",
            "next_poll_at",
            postgresql_where=text("status = 'active' AND next_poll_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    passenger_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("passengers.id"), nullable=False
    )
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
    scheduled_departure_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scheduled_arrival_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    confirmation_code: Mapped[str | None] = mapped_column(Text)
    seat: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    gcal_event_id: Mapped[str | None] = mapped_column(Text)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    passenger: Mapped[Passenger] = relationship(back_populates="bookings")
    snapshots: Mapped[list[FlightSnapshot]] = relationship(
        back_populates="booking", cascade="all, delete-orphan"
    )
    events: Mapped[list[FlightEvent]] = relationship(
        back_populates="booking", cascade="all, delete-orphan"
    )


class FlightSnapshot(Base):
    """One AeroAPI observation. Append-only: never UPDATE a row in this table."""

    __tablename__ = "flight_snapshots"
    __table_args__ = (
        Index("flight_snapshots_latest", "booking_id", text("observed_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[datetime] = _created_at()
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Denormalised out of `raw` so diffing and list queries never parse JSON.
    status_text: Mapped[str | None] = mapped_column(Text)
    cancelled: Mapped[bool | None] = mapped_column(Boolean)
    diverted: Mapped[bool | None] = mapped_column(Boolean)
    gate_origin: Mapped[str | None] = mapped_column(Text)
    gate_destination: Mapped[str | None] = mapped_column(Text)
    terminal_origin: Mapped[str | None] = mapped_column(Text)
    terminal_destination: Mapped[str | None] = mapped_column(Text)
    baggage_claim: Mapped[str | None] = mapped_column(Text)
    scheduled_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_off: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress_percent: Mapped[int | None] = mapped_column(Integer)
    aircraft_type: Mapped[str | None] = mapped_column(Text)
    registration: Mapped[str | None] = mapped_column(Text)

    booking: Mapped[Booking] = relationship(back_populates="snapshots")


class FlightEvent(Base):
    """A material change, fanned out to ntfy and Google Calendar independently."""

    __tablename__ = "flight_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = _created_at()
    # Null means "not yet delivered"; each consumer claims its own column, so a failing
    # calendar never blocks a push and neither is ever delivered twice.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    calendar_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    booking: Mapped[Booking] = relationship(back_populates="events")


class IngestLog(Base):
    """Every Gmail message we have looked at, so a replay never re-processes one."""

    __tablename__ = "ingest_log"

    gmail_message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    processed_at: Mapped[datetime] = _created_at()
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    raw_extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class ApiUsage(Base):
    """Estimated AeroAPI spend. The circuit breaker reads month-to-date sums off this."""

    __tablename__ = "api_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    called_at: Mapped[datetime] = _created_at()
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    result_sets: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    est_cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)


class KV(Base):
    """Small singleton state: the Gmail history cursor, breaker latches, and the like."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
