"""Command line entry points."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Settings, get_settings

log = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
    )
    return 0


def _cmd_migrate(settings: Settings, _args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    return 0


def _cmd_seed_airports(settings: Settings, _args: argparse.Namespace) -> int:
    from .airports import seed_airports
    from .db import dispose_engine, init_engine, session_scope

    async def run() -> int:
        init_engine(settings)
        try:
            async with session_scope() as session:
                count = await seed_airports(session)
        finally:
            await dispose_engine()
        print(f"seeded {count} airports")
        return 0

    return asyncio.run(run())


def _cmd_backfill(settings: Settings, args: argparse.Namespace) -> int:
    """One-off catch-up over recent mail, for a first run or after a long outage."""
    from .db import dispose_engine, init_engine, session_scope
    from .gmail import backfill

    async def run() -> int:
        init_engine(settings)
        try:
            async with session_scope() as session:
                processed = await backfill(session, days=args.days)
        finally:
            await dispose_engine()
        print(f"processed {processed} messages")
        return 0

    return asyncio.run(run())


def _cmd_poll(settings: Settings, _args: argparse.Namespace) -> int:
    """A single polling pass, for checking a change lands without waiting on the loop."""
    from .db import dispose_engine, init_engine
    from .poller import poll_once

    async def run() -> int:
        init_engine(settings)
        try:
            polled = await poll_once()
        finally:
            await dispose_engine()
        print(f"polled {polled} bookings")
        return 0

    return asyncio.run(run())


def _cmd_check(settings: Settings, _args: argparse.Namespace) -> int:
    """Exercise every external dependency and say which one is broken."""
    from .checks import run_checks
    from .db import dispose_engine, init_engine

    async def run() -> int:
        init_engine(settings)
        try:
            results = await run_checks(settings)
        finally:
            await dispose_engine()
        failed = 0
        for result in results:
            mark = "ok  " if result.ok else "FAIL"
            print(f"[{mark}] {result.name}: {result.detail}")
            failed += 0 if result.ok else 1
        return 1 if failed else 0

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flight-tracker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the API, poller and mail loop")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=_cmd_serve)

    subparsers.add_parser("migrate", help="apply database migrations").set_defaults(
        func=_cmd_migrate
    )
    subparsers.add_parser("seed-airports", help="load the airport table").set_defaults(
        func=_cmd_seed_airports
    )
    subparsers.add_parser("poll", help="run one polling pass and exit").set_defaults(func=_cmd_poll)
    subparsers.add_parser("check", help="exercise every external dependency").set_defaults(
        func=_cmd_check
    )

    backfill = subparsers.add_parser("backfill", help="ingest recent mail once")
    backfill.add_argument("--days", type=int, default=30)
    backfill.set_defaults(func=_cmd_backfill)

    args = parser.parse_args(argv)
    settings = get_settings()
    _configure_logging(settings.log_level)
    result: int = args.func(settings, args)
    return result


if __name__ == "__main__":
    sys.exit(main())
