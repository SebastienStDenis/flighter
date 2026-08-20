"""The supervisor that keeps the background loops alive for the life of the process."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from flighter import app


async def test_a_loop_that_dies_is_started_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A poller that fell over must not leave the process alive and blind."""
    monkeypatch.setattr(app, "RESTART_MIN_SECONDS", 0.001)
    starts = 0

    async def flaky() -> None:
        nonlocal starts
        starts += 1
        if starts == 1:
            raise RuntimeError("the poller fell over")

    await app._supervise("poller", flaky, asyncio.Event())

    assert starts == 2


async def test_a_loop_that_returns_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returning is how a loop says it is finished; restarting it would be a busy loop."""
    starts = 0

    async def finishes() -> None:
        nonlocal starts
        starts += 1

    await app._supervise("ingest", finishes, asyncio.Event())

    assert starts == 1


async def test_a_stopping_process_is_not_restarted_into(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "RESTART_MIN_SECONDS", 0.001)
    stopping = asyncio.Event()
    starts = 0

    async def fails_on_the_way_down() -> None:
        nonlocal starts
        starts += 1
        stopping.set()
        raise RuntimeError("the connection went with the process")

    await app._supervise("ingest", fails_on_the_way_down, stopping)

    assert starts == 1
    assert stopping.is_set()


async def test_the_pause_between_restarts_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug that kills a loop on every pass must not become a busy loop."""
    waited: list[float] = []
    wait_for = asyncio.wait_for

    async def record(awaitable: Any, timeout: float) -> Any:
        waited.append(timeout)
        return await wait_for(awaitable, 0)

    monkeypatch.setattr(asyncio, "wait_for", record)
    starts = 0

    async def fails_three_times() -> None:
        nonlocal starts
        starts += 1
        if starts <= 3:
            raise RuntimeError("still broken")

    await app._supervise("dispatch", fails_three_times, asyncio.Event())

    assert waited == [
        app.RESTART_MIN_SECONDS,
        app.RESTART_MIN_SECONDS * 2,
        app.RESTART_MIN_SECONDS * 4,
    ]


async def test_a_cancelled_loop_is_not_restarted() -> None:
    """Shutdown cancels the task; treating that as a crash would restart it mid-teardown."""

    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await app._supervise("poller", cancelled, asyncio.Event())
