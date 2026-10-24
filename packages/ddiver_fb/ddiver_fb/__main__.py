"""Command line: ``ddiver-fb run | health | check-config``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from ddiver_core.app import EXIT_DB_CORRUPT, EXIT_ERROR, EXIT_LOCKED, EXIT_OK
from ddiver_core.process import InstanceLock, JsonFormatter, describe_config_error, harden_data_dir
from ddiver_core.storage import DatabaseCorruptError, MigrationError
from ddiver_fb.config import FbSettings
from ddiver_fb.health import check as health_check

log = logging.getLogger("ddiver_fb")

LOCK_NAME = "ddiver-fb.lock"
CONTAINER_UID = 10002
"""The user the Dockerfile runs the brain as (``useradd --uid``); a test holds the two equal."""


def configure_logging(level: str, fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=getattr(logging, level, logging.INFO), handlers=[handler], force=True)
    logging.getLogger("telethon").setLevel(logging.WARNING)


async def cmd_run(settings: FbSettings) -> int:
    from ddiver_fb.runtime import BrainApp

    lock = InstanceLock(settings.data_dir / LOCK_NAME)
    try:
        harden_data_dir(settings.data_dir)
        acquired = lock.acquire()
    except OSError as exc:
        # In the container the brain runs as uid 10002, and Docker creates a missing bind
        # mount as root: without this the process crash-loops on a traceback. RUNBOOK, "The
        # brain on the VPS", creates the directory first.
        log.error(
            "cannot write the data directory %s (%s); it must belong to the brain's user "
            "(uid %d in the container): install -d -o %d -g %d -m 700 <host data dir>",
            settings.data_dir,
            exc.strerror or type(exc).__name__,
            CONTAINER_UID,
            CONTAINER_UID,
            CONTAINER_UID,
        )
        return EXIT_ERROR
    if not acquired:
        log.error("another ddiver-fb process is already running for %s", settings.data_dir)
        return EXIT_LOCKED
    app = BrainApp(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, app.request_stop)
    try:
        try:
            await app.setup()
        except DatabaseCorruptError as exc:
            log.error("database is corrupt (%s); restore %s from a backup", exc, settings.db_path)
            return EXIT_DB_CORRUPT
        except MigrationError:
            log.exception("a migration of the fb track failed; the database is unchanged")
            return EXIT_ERROR
        except Exception:
            log.exception("startup failed")
            return EXIT_ERROR
        return await app.run()
    finally:
        with contextlib.suppress(Exception):
            await app.close()
        lock.release()


async def cmd_check_config(settings: FbSettings) -> int:
    from ddiver_core.botclient import make_bot_client

    problems: list[str] = []
    print(f"Data directory: {settings.data_dir}")
    print(f"Owner id: {settings.owner_id}")
    print(f"Bot id: {settings.bot_id}")
    print(f"Ingest: {settings.ingest_host}:{settings.ingest_port}")
    client = make_bot_client(settings)
    try:
        await client.start(bot_token=settings.bot_token.get_secret_value())
        me = await client.get_me()
        if me.id != settings.bot_id:
            problems.append(f"bot.session belongs to bot {me.id}; delete {settings.bot_session}")
        else:
            print(f"Bot: @{me.username}")
        try:
            await client.get_input_entity(settings.owner_id)
        except Exception:
            problems.append(
                f"the owner has not started the bot yet — open t.me/{me.username} and press Start"
            )
    except Exception as exc:  # this command reports problems, it never raises
        problems.append(f"cannot use BOT_TOKEN: {type(exc).__name__}")
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  ✖ {problem}")
        return EXIT_ERROR
    print("\nEverything looks fine.")
    return EXIT_OK


def cmd_health(settings: FbSettings) -> int:
    report = health_check(settings.health_path)
    print(report.reason)
    return EXIT_OK if report.healthy else EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ddiver-fb", description=__doc__)
    parser.add_argument("--env-file", default=".env", help="path to the .env file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the bot and the ingest server")
    sub.add_parser("health", help="check the heartbeat (for Docker HEALTHCHECK)")
    sub.add_parser("check-config", help="validate the configuration and the bot token")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # before anything creates a database, a session or a heartbeat
    args = build_parser().parse_args(argv)
    try:
        settings = FbSettings.load(args.env_file)
    except Exception as exc:  # reported without the offending value (it may be a token)
        print(f"Configuration error: {describe_config_error(exc)}", file=sys.stderr)
        return EXIT_ERROR
    configure_logging(settings.log_level, settings.log_format)
    if args.command == "health":
        return cmd_health(settings)
    if args.command == "check-config":
        return asyncio.run(cmd_check_config(settings))
    return asyncio.run(cmd_run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
