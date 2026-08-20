"""Engine and session plumbing. One engine per process, sessions per unit of work."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import DATA_DIR, Settings

log = logging.getLogger(__name__)

# How long a statement waits for another task's write lock before giving up. The app is
# one process with a handful of tasks and every transaction is short, so anything that
# hits this is a bug rather than congestion - but a few seconds of patience is cheaper
# than an error propagating out of a poll tick.
BUSY_TIMEOUT_MS = 5000

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class DataDirectoryNotWritable(RuntimeError):
    """The directory mounted at /app/data cannot be written to by the app's user."""


def ensure_data_dir(path: Path = DATA_DIR) -> Path:
    """Fail early, and by name, when the data directory cannot be written to.

    A bind mount of a host directory owned by somebody else is the ordinary way this
    happens: the container runs as uid 10001 and simply cannot create the database
    there. The error that would otherwise surface names a temporary file inside a driver
    rather than the mount the operator has to fix.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".writable"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        message = (
            f"cannot write to the data directory {path.absolute()}: {exc}. The app runs "
            f"as uid 10001, so a bind-mounted host directory has to be owned by it "
            f"(chown 10001:10001); a named volume needs nothing."
        )
        log.error(message)
        raise DataDirectoryNotWritable(message) from exc
    return path


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
    """WAL, a busy timeout and foreign keys, on every connection.

    SQLite opens each connection with foreign keys *off* and journalling in rollback
    mode, neither of which suits a service: `ondelete="CASCADE"` would silently do
    nothing, and a reader would block behind the poller's write. WAL and a busy timeout
    together are what let the web app, the poller and the mail loop share one file.

    Clearing `isolation_level` stops the driver opening transactions of its own, which
    hands that job to `_on_begin` below.
    """
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
        # Safe under WAL: a power cut can lose the most recent commits but cannot corrupt
        # the database, and the alternative is an fsync per transaction on a machine
        # tracking a handful of flights.
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _on_begin(connection: Connection) -> None:
    """Take the write lock up front rather than upgrading to it mid-transaction.

    A transaction that reads and then writes has to promote its lock, and SQLite refuses
    to wait for that promotion: it returns SQLITE_BUSY immediately, busy timeout or not,
    because waiting would deadlock against another reader wanting the same thing. Almost
    every transaction here reads before it writes, so all of them take the write lock up
    front and queue on the busy timeout instead.

    That makes a read-only page load a writer too, which is the price. With one user and
    transactions measured in milliseconds it is not a price anyone can notice, and the
    alternative is an intermittent failure under exactly the concurrency the app has.
    """
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        # Caught here rather than left to the driver, which answers a URL for a server
        # this no longer talks to with a missing-module error several layers down,
        # naming a package instead of the line that has to go.
        if not settings.database_url.startswith("sqlite"):
            raise ValueError(
                f"DATABASE_URL is set to {settings.database_url.split('://')[0]}://, but "
                "the database is SQLite in the data directory. Unset DATABASE_URL."
            )
        ensure_data_dir()
        # Small pool on purpose: the web app, the poll worker and the mail loop share it,
        # and one SQLite file admits one writer at a time however many connections point
        # at it.
        _engine = create_async_engine(settings.database_url, pool_size=5, max_overflow=0)
        event.listen(_engine.sync_engine, "connect", _on_connect)
        event.listen(_engine.sync_engine, "begin", _on_begin)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("init_engine() has not been called")
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session that commits on success and rolls back on any exception."""
    async with sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session
