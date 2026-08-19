"""Initial schema.

Revision ID: 74ca7df96720
Revises:
Create Date: 2026-08-19 19:41:12.998279

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "74ca7df96720"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "airports",
        sa.Column("iata", sa.String(length=3), nullable=False),
        sa.Column("icao", sa.String(length=4), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("tz", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("iata"),
    )
    op.create_table(
        "api_usage",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "called_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("result_sets", sa.Integer(), nullable=False),
        sa.Column("est_cost_usd", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ingest_log",
        sa.Column("gmail_message_id", sa.Text(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("raw_extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("gmail_message_id"),
    )
    op.create_table(
        "kv",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "passengers",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("is_self", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "bookings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("passenger_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.Text(), nullable=True),
        sa.Column("marketing_carrier", sa.Text(), nullable=False),
        sa.Column("marketing_number", sa.Text(), nullable=False),
        sa.Column("operating_carrier", sa.Text(), nullable=True),
        sa.Column("operating_number", sa.Text(), nullable=True),
        sa.Column("aeroapi_ident", sa.Text(), nullable=True),
        sa.Column("aeroapi_fa_flight_id", sa.Text(), nullable=True),
        sa.Column("origin_iata", sa.String(length=3), nullable=False),
        sa.Column("dest_iata", sa.String(length=3), nullable=False),
        sa.Column("scheduled_departure_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_arrival_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_code", sa.Text(), nullable=True),
        sa.Column("seat", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("extraction_confidence", sa.Float(), nullable=True),
        sa.Column("gcal_event_id", sa.Text(), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('email', 'manual')", name="bookings_source_check"),
        sa.CheckConstraint(
            "status IN ('pending_review', 'active', 'completed', 'cancelled', 'archived')",
            name="bookings_status_check",
        ),
        sa.ForeignKeyConstraint(
            ["dest_iata"],
            ["airports.iata"],
        ),
        sa.ForeignKeyConstraint(
            ["origin_iata"],
            ["airports.iata"],
        ),
        sa.ForeignKeyConstraint(
            ["passenger_id"],
            ["passengers.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # The whole expression needs its own parentheses: without them Postgres reads the
    # cast as closing the index element and rejects the statement.
    op.create_index(
        "bookings_dedupe",
        "bookings",
        [
            "passenger_id",
            "marketing_carrier",
            "marketing_number",
            sa.literal_column("((scheduled_departure_utc AT TIME ZONE 'UTC')::date)"),
        ],
        unique=True,
        postgresql_where=sa.text("status != 'archived'"),
    )
    op.create_index(
        "bookings_poll",
        "bookings",
        ["next_poll_at"],
        unique=False,
        postgresql_where=sa.text("status = 'active' AND next_poll_at IS NOT NULL"),
    )
    op.create_table(
        "flight_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("booking_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("calendar_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "flight_snapshots",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("booking_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status_text", sa.Text(), nullable=True),
        sa.Column("cancelled", sa.Boolean(), nullable=True),
        sa.Column("diverted", sa.Boolean(), nullable=True),
        sa.Column("gate_origin", sa.Text(), nullable=True),
        sa.Column("gate_destination", sa.Text(), nullable=True),
        sa.Column("terminal_origin", sa.Text(), nullable=True),
        sa.Column("terminal_destination", sa.Text(), nullable=True),
        sa.Column("baggage_claim", sa.Text(), nullable=True),
        sa.Column("scheduled_out", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_out", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_out", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_off", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_in", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_in", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_in", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=True),
        sa.Column("aircraft_type", sa.Text(), nullable=True),
        sa.Column("registration", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "flight_snapshots_latest",
        "flight_snapshots",
        ["booking_id", sa.literal_column("observed_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("flight_snapshots_latest", table_name="flight_snapshots")
    op.drop_table("flight_snapshots")
    op.drop_table("flight_events")
    op.drop_index(
        "bookings_poll",
        table_name="bookings",
        postgresql_where=sa.text("status = 'active' AND next_poll_at IS NOT NULL"),
    )
    op.drop_index(
        "bookings_dedupe", table_name="bookings", postgresql_where=sa.text("status != 'archived'")
    )
    op.drop_table("bookings")
    op.drop_table("passengers")
    op.drop_table("kv")
    op.drop_table("ingest_log")
    op.drop_table("api_usage")
    op.drop_table("airports")
