"""Command line: ``fbcollector run | login | pin-home | status | uplink | pause | resume | groups
confirm | replay | report | dialogs | selftest | upgrade-chrome | check-config``.

Phase-0 local mode has no brain, so the owner steers the collector from a local terminal:
``pause``, ``resume <block_id>`` and ``groups confirm`` require a TTY (DESIGN, Phase-0 local
mode), which keeps them out of scripts and remote shells without a terminal. ``pause`` and
``resume`` write to the same database the running collector reads: every gate check reads the
control row again, so a ``pause`` stops a read under way before its next scroll or final look,
and Chrome is closed. ``resume`` goes through exactly the rules of a remote ``/resume`` (the open
block's id, a fresh nonce, its time passed, then one clean probe).
``groups confirm <id|url>...`` writes the owner's word that the account is a member into
``monitored.toml`` (``fbcollector.confirm``), which the running collector reads again when the
file changes.

``main`` returns an exit code instead of exiting, so every command is testable in-process.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TextIO
from urllib.parse import urlsplit

from fbcollector import paths
from fbcollector.alerts import quiet_http_logs
from fbcollector.config import ConfigError, Settings, describe_config_error
from fbcollector.confirm import GroupRefError, confirm_in_file, parse_group_ref
from fbcollector.control import ControlCenter, MonitoredFileError
from fbcollector.detectors import panel_heads_up_items, panel_problem
from fbcollector.gates import (
    KV_HOME_PIN,
    Activity,
    GateContext,
    HomeError,
    IpifyLookup,
    ProfileLock,
    ProfileLockedError,
    capture_home_pin,
    host_probes,
)
from fbcollector.ladder import Ladder
from fbcollector.raw import STEP_SUFFIX, load_step
from fbcollector.reader import KV_PARSER_BROKEN, KV_PARSER_HISTORY, ReaderConfig
from fbcollector.replay import replay
from fbcollector.report import generate
from fbcollector.runner import (
    KV_HALT,
    Ports,
    login,
    parse_since,
    require_tty,
    run_collector,
    selftest,
    selftest_gates,
    selftest_key,
    status_lines,
)
from fbcollector.scheduler import KV_BACKSTOP_ONLY
from fbcollector.sleephook import platform_sleep_hook
from fbcollector.store import DatabaseCorruptError, ProcessKind, SchemaTooNewError, Store
from fbcollector.uplink import uplink_lines

log = logging.getLogger("fbcollector")

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_USAGE: Final = 2
EXIT_NO_TTY: Final = 3
EXIT_LOCKED: Final = 4
EXIT_DB: Final = 5
EXIT_CONFIG: Final = 6


@dataclass(frozen=True, slots=True)
class Invocation:
    """One command's inputs: settings, the machine's ports, the parsed arguments, the output."""

    settings: Settings
    ports: Ports
    args: argparse.Namespace
    out: TextIO


class JsonFormatter(logging.Formatter):
    """One JSON object per line (ddiver's format), for a log shipper."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str, fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=getattr(logging, level, logging.INFO), handlers=[handler], force=True)
    quiet_http_logs()


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="fbcollector", description="ddiver-fb home collector")
    top.add_argument("--env-file", type=Path, default=None, help="extra FBC_* settings")
    sub = top.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="the supervised collector")
    run.add_argument("--observe", action="store_true", help="phase-0 observe mode")
    login_cmd = sub.add_parser("login", help="log in by hand in the collector's Chrome")
    login_cmd.add_argument(
        "--record-login", action="store_true", help="record login_at even without a new session"
    )
    sub.add_parser("pin-home", help="record the home router, ASNs and timezone (once, at home)")
    sub.add_parser("status", help="what the collector is doing")
    sub.add_parser(
        "uplink", help="the link to the brain: mode, lease, last cycle, queues (no ids, no token)"
    )
    pause = sub.add_parser("pause", help="stop reading until `pause --off` (TTY)")
    pause.add_argument("--off", action="store_true", help="resume reading")
    pause.add_argument(
        "--feed-check", action="store_true", help="with --off: re-check the Groups feed order"
    )
    resume = sub.add_parser("resume", help="lift a block after its time, via a probe (TTY)")
    resume.add_argument("block_id", type=int)
    resume.add_argument("--lock-lifted", action="store_true", help="advanced_protection lifted")
    resume.add_argument("--replanned", action="store_true", help="lift the last rung")
    groups = sub.add_parser("groups", help="phase 0: the owner's word on group membership (TTY)")
    groups_sub = groups.add_subparsers(dest="groups_command", required=True)
    confirm = groups_sub.add_parser(
        "confirm", help="the account is a member of these groups (checked by hand)"
    )
    confirm.add_argument("refs", nargs="+", metavar="ID_OR_URL", help="group id or group URL")
    replay_cmd = sub.add_parser("replay", help="re-parse saved payloads after a parser fix")
    replay_cmd.add_argument("--since", required=True, help="6h, 2d or an ISO date")
    report = sub.add_parser("report", help="the phase-0 report (Markdown)")
    report.add_argument("--out", type=Path, default=None)
    report.add_argument("--since", default=None)
    report.add_argument(
        "--diagnostics-only",
        action="store_true",
        help="only the parser diagnostics, without the sections that print group ids",
    )
    dialogs = sub.add_parser(
        "dialogs", help="the dialog texts of the last saved steps (an unrecognised page's why)"
    )
    dialogs.add_argument("--last", type=int, default=4, choices=range(1, 21), metavar="1-20")
    sub.add_parser("selftest", help="the driver against a local page (never Facebook)")
    sub.add_parser("upgrade-chrome", help="Linux: upgrade the held Chrome in quiet hours")
    sub.add_parser("check-config", help="validate settings without starting anything")
    return top


def main(
    argv: Sequence[str] | None = None,
    *,
    ports: Ports | None = None,
    stdin: Any = None,
    out: TextIO | None = None,
) -> int:
    args = parser().parse_args(argv)
    stream = out or sys.stdout
    try:
        settings = Settings.load(args.env_file)
    except (ConfigError, ValueError) as exc:
        stream.write(f"configuration error: {describe_config_error(exc)}\n")
        return EXIT_CONFIG
    configure_logging(settings.log_level, settings.log_format)
    runtime_ports = ports or Ports()
    tty = stdin if stdin is not None else sys.stdin
    command = args.command
    try:
        if command == "run":
            missing = settings.missing_for_run()
            if missing:
                stream.write(f"missing settings: {', '.join(missing)}\n")
                return EXIT_CONFIG
            if ports is None:  # the real process: listen for the OS's sleep announcements
                runtime_ports.sleep_hook = platform_sleep_hook(runtime_ports.platform)
            return asyncio.run(run_collector(settings, runtime_ports, observe=args.observe))
        if command == "login":
            return asyncio.run(
                login(settings, runtime_ports, out=stream, record_login=args.record_login)
            )
        if command in {"pause", "resume", "groups"} and not require_tty(tty):
            stream.write(f"`fbcollector {command}` needs a local terminal (TTY).\n")
            return EXIT_NO_TTY
        handler = COMMANDS.get(command)
        if handler is None:
            return EXIT_USAGE  # pragma: no cover - argparse rejects unknown commands
        return asyncio.run(handler(Invocation(settings, runtime_ports, args, stream)))
    except ProfileLockedError as exc:
        stream.write(f"{exc}\n")
        return EXIT_LOCKED
    except (DatabaseCorruptError, SchemaTooNewError) as exc:
        stream.write(f"state database problem: {exc}\n")
        return EXIT_DB
    except paths.UnsafeLayoutError as exc:
        stream.write(
            "the collector root is not safe:\n" + "".join(f"  - {p}\n" for p in exc.problems)
        )
        return EXIT_CONFIG


async def _open(settings: Settings, ports: Ports) -> Store:
    layout = paths.prepare(settings.root, platform=ports.platform, runner=ports.runner)
    store = Store(layout.db, clock=ports.clock)
    await store.open()
    return store


async def cmd_status(inv: Invocation) -> int:
    settings, ports, out = inv.settings, inv.ports, inv.out
    store = await _open(settings, ports)
    try:
        for line in await status_lines(settings, store, ports.clock):
            out.write(line + "\n")
    finally:
        await store.close()
    return EXIT_OK


async def cmd_uplink(inv: Invocation) -> int:
    """``uplink``: the link to the brain, read from the settings and the database only (the
    operator door's ``uplink``). Prints no group or post id, no text and nothing of the token."""
    settings, ports, out = inv.settings, inv.ports, inv.out
    store = await _open(settings, ports)
    try:
        for line in await uplink_lines(settings, store, ports.clock.time()):
            out.write(line + "\n")
    finally:
        await store.close()
    return EXIT_OK


async def cmd_pause(inv: Invocation) -> int:
    settings, ports, args, out = inv.settings, inv.ports, inv.args, inv.out
    store = await _open(settings, ports)
    try:
        ladder = Ladder(store, ports.clock)
        control = ControlCenter(
            store,
            ladder,
            ports.clock,
            collector_id=settings.collector_id,
            local_mode=settings.local_mode,
        )
        await control.load()
        if not args.off:
            await control.pause()
            out.write("Paused: no reads until `fbcollector pause --off`.\n")
            return EXIT_OK
        await control.unpause()
        await store.kv_delete(KV_PARSER_BROKEN)
        await store.kv_delete(KV_PARSER_HISTORY)  # the fixed parser starts a fresh window
        halt = await store.kv_get_json(KV_HALT)
        if isinstance(halt, dict) and halt.get("kind") == "selftest":
            await store.kv_delete(KV_HALT)
        if args.feed_check:
            await store.kv_delete(KV_BACKSTOP_ONLY)
        out.write("Reading resumes at the next scheduled moment (gates permitting).\n")
    finally:
        await store.close()
    return EXIT_OK


async def cmd_resume(inv: Invocation) -> int:
    settings, ports, args, out = inv.settings, inv.ports, inv.args, inv.out
    store = await _open(settings, ports)
    try:
        ladder = Ladder(
            store,
            ports.clock,
            cautious_days_after_warning=settings.cautious_days_after_warning,
            cautious_days_after_logout=settings.cautious_days_after_logout,
            strike_decay_days=settings.strike_decay_days,
        )
        control = ControlCenter(
            store,
            ladder,
            ports.clock,
            collector_id=settings.collector_id,
            local_mode=settings.local_mode,
            rng=ports.rng,
        )
        await control.load()
        result = await control.resume(
            args.block_id, lock_lifted=args.lock_lifted, replanned=args.replanned
        )
        out.write(f"resume block {args.block_id}: {result.value}\n")
        if result.value == "accepted":
            out.write("A probe of the Groups feed runs within 30-300 s; it must come back clean.\n")
            return EXIT_OK
        return EXIT_ERROR
    finally:
        await store.close()


async def cmd_groups(inv: Invocation) -> int:
    """``groups confirm <id|url>...``: the owner's word, dated, into ``monitored.toml``."""
    settings, ports, args, out = inv.settings, inv.ports, inv.args, inv.out
    if not settings.local_mode:
        out.write(
            "With the brain, monitored.toml is not read: the brain's groups snapshot decides.\n"
        )
        return EXIT_USAGE
    try:
        refs = [parse_group_ref(ref) for ref in args.refs]
    except GroupRefError as exc:
        out.write(f"{exc}\n")
        return EXIT_USAGE
    slugs = [ref.slug for ref in refs if ref.group_id is None and ref.slug]
    known: dict[str, str] = {}
    if slugs:
        store = await _open(settings, ports)
        try:
            known = {g.slug: g.id for g in await store.list_groups() if g.slug}
        finally:
            await store.close()
    ids: list[str] = []
    for ref in refs:
        group_id = ref.group_id or known.get(ref.slug or "")
        if group_id is None:
            out.write(
                f"{ref.slug}: a group name the collector has not seen yet; use the numeric id "
                "(the group's About page, or its URL in `fbcollector login`).\n"
            )
            return EXIT_USAGE
        ids.append(group_id)
    paths.prepare(settings.root, platform=ports.platform, runner=ports.runner)
    path = settings.monitored_file
    at = dt.datetime.fromtimestamp(ports.clock.time(), dt.UTC)
    try:
        added = confirm_in_file(path, ids, at)
    except MonitoredFileError as exc:
        out.write(f"{exc}\n")
        return EXIT_CONFIG
    for group_id in dict.fromkeys(ids):
        new = " (added to the monitored groups)" if group_id in added else ""
        out.write(f"confirmed {group_id}{new}\n")
    out.write(
        f"Written to {path}. The collector reads each confirmed group once its backstop is due "
        "and checks the page: a page that does not say MEMBER pauses the group and withdraws "
        "the confirmation.\n"
    )
    return EXIT_OK


async def cmd_pin_home(inv: Invocation) -> int:
    settings, ports, out = inv.settings, inv.ports, inv.out
    probes = ports.probes or host_probes(ports.platform, ports.runner)
    lookup = ports.lookup or IpifyLookup()
    try:
        pin = await capture_home_pin(
            probes, lookup, timezone=settings.timezone, now=ports.clock.time()
        )
    except HomeError as exc:
        out.write(f"cannot pin home: {exc}\n")
        return EXIT_ERROR
    store = await _open(settings, ports)
    try:
        await store.kv_set(KV_HOME_PIN, pin.to_json())
    finally:
        await store.close()
    v6 = f", AS{pin.asn_v6} (IPv6)" if pin.asn_v6 else ""
    out.write(f"Home pinned: router {pin.router_mac}, AS{pin.asn_v4}{v6}, {pin.timezone}.\n")
    return EXIT_OK


async def cmd_replay(inv: Invocation) -> int:
    settings, ports, args, out = inv.settings, inv.ports, inv.args, inv.out
    now = ports.clock.time()
    try:
        since = parse_since(args.since, now)
    except ValueError:
        out.write("--since takes 30m, 6h, 2d or an ISO date\n")
        return EXIT_USAGE
    store = await _open(settings, ports)
    try:
        layout = paths.Layout(settings.root)
        config = ReaderConfig(
            collector_id=settings.collector_id,
            raw_dir=layout.raw,
            shots_dir=layout.shots,
            novelty_days=settings.novelty_days,
            late_after_hours=settings.late_after_hours,
            outbox_cap=settings.outbox_cap,
        )
        result = await replay(store, layout.raw, since=since, config=config, now=now)
    finally:
        await store.close()
    out.write(
        f"replayed {result.steps} steps, {result.stories} stories: {result.queued} queued, "
        f"{result.still_deferred} still deferred, {result.still_invalid} still invalid\n"
    )
    return EXIT_OK


async def cmd_report(inv: Invocation) -> int:
    settings, ports, args, out = inv.settings, inv.ports, inv.args, inv.out
    layout = paths.check(settings.root, platform=ports.platform, runner=ports.runner)
    now = ports.clock.time()
    since = parse_since(args.since, now) if args.since else None
    text = await asyncio.to_thread(
        generate,
        layout.db,
        zone=settings.zone,
        now=now,
        since=since,
        raw_dir=layout.raw,
        diagnostics_only=args.diagnostics_only,
    )
    if args.out is None:
        out.write(text)
    else:
        args.out.write_text(text, encoding="utf-8")
        out.write(f"wrote {args.out}\n")
    return EXIT_OK


async def cmd_dialogs(inv: Invocation) -> int:
    """The texts of the ``[role=dialog]`` elements on the last saved steps, newest last: what
    made the detectors call a page unrecognised (``dialogs_clear``). Read from ``<root>/raw``
    only; the operator door's ``dialogs`` (the owner's consent of 2026-09-30)."""
    settings, ports, args, out = inv.settings, inv.ports, inv.args, inv.out
    layout = paths.check(settings.root, platform=ports.platform, runner=ports.runner)
    names = sorted(layout.raw.glob(f"*{STEP_SUFFIX}"))[-args.last :]
    if not names:
        out.write("No saved steps.\n")
    for path in names:
        try:
            saved = await asyncio.to_thread(load_step, path)
        except (OSError, ValueError, KeyError, EOFError):
            out.write(f"{path.name}: unreadable\n")
            continue
        when = dt.datetime.fromtimestamp(saved.at, settings.zone).strftime("%Y-%m-%d %H:%M:%S")
        dom = saved.step.dom
        texts = dom.dialog_texts
        counted = "" if dom.dialog_count is None else f" of {dom.dialog_count}"
        out.write(
            f"{when} read {saved.read_id} step {saved.index} {saved.surface.value}"
            f" {urlsplit(saved.step.final_url).path}: {len(texts)}{counted} dialog(s)\n"
        )
        for n, text in enumerate(texts, 1):
            out.write(f"  [{n}] ({_dialog_label(text)}) {text or '(empty)'}\n")
    return EXIT_OK


def _dialog_label(text: str) -> str:
    """How today's detectors take one saved dialog text (the owner of an unrecognised page
    learns whether a fix already covers it)."""
    if not text.strip():
        return "empty: recognised"
    problem = panel_problem(text)
    if problem is None:
        notices = panel_heads_up_items(text)
        if notices:
            return (
                f"the notifications panel: recognised; unread notices for the owner: {len(notices)}"
            )
        return "the notifications panel: recognised"
    if problem == "not the notifications panel":
        return "unrecognised"
    return f"{problem}: unrecognised"


async def cmd_selftest(inv: Invocation) -> int:
    settings, ports, out = inv.settings, inv.ports, inv.out
    from fbcollector.runner import default_local_factory  # noqa: PLC0415 - loads the driver

    layout = paths.prepare(settings.root, platform=ports.platform, runner=ports.runner)
    lock = ProfileLock(layout.lock)
    with lock:
        async with Store(layout.db, clock=ports.clock) as store:
            # The read loop's launch door, the crash-loop gate counting this launch, minus the
            # warm-up, rhythm and halt gates (runner.SELFTEST_EXEMPT says why). A human's
            # command is a planned launch: its cap is the one on all launches, planned or not.
            door = await selftest_gates(settings, store, ports, lock=lock)
            launch = GateContext(Activity.LAUNCH, planned=True)
            closed = await door.evaluate(launch, stop_early=False)
            if not closed.ok:
                out.write(
                    "Refusing to start the account's Chrome now:\n"
                    + "".join(f"  - {p}\n" for p in closed.reasons)
                )
                return EXIT_ERROR
            await store.record_process_event(ProcessKind.CHROME_LAUNCH, "selftest", planned=True)
            factory = ports.local_driver_factory or default_local_factory(layout, ports)
            report = await selftest(factory)
            for name, ok in report.checks:
                out.write(f"{'ok  ' if ok else 'FAIL'} {name}\n")
            if report.passed and report.chrome_major is not None:
                await store.kv_set(selftest_key(report.chrome_major), "passed")
    out.write("self-test passed\n" if report.passed else "self-test FAILED\n")
    return EXIT_OK if report.passed else EXIT_ERROR


async def cmd_upgrade_chrome(inv: Invocation) -> int:
    ports, out = inv.ports, inv.out
    if ports.platform == "darwin":
        out.write("Not on macOS: Chrome updates itself; the self-test runs on a new major.\n")
        return EXIT_USAGE
    out.write(
        "Not automated yet. In quiet hours, with the collector stopped:\n"
        "  sudo apt-mark unhold google-chrome-stable\n"
        "  sudo apt-get install --only-upgrade google-chrome-stable\n"
        "  sudo apt-mark hold google-chrome-stable\n"
        "  fbcollector selftest\n"
    )
    return EXIT_USAGE


async def cmd_check_config(inv: Invocation) -> int:
    settings, out = inv.settings, inv.out
    missing = settings.missing_for_run()
    mode = "local (phase 0)" if settings.local_mode else "uplink"
    out.write(f"root: {settings.root}\nzone: {settings.timezone}\nmode: {mode}\n")
    if missing:
        out.write(f"missing for run: {', '.join(missing)}\n")
        return EXIT_CONFIG
    out.write("ok\n")
    return EXIT_OK


COMMANDS: Final = {
    "status": cmd_status,
    "uplink": cmd_uplink,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "groups": cmd_groups,
    "pin-home": cmd_pin_home,
    "replay": cmd_replay,
    "report": cmd_report,
    "dialogs": cmd_dialogs,
    "selftest": cmd_selftest,
    "upgrade-chrome": cmd_upgrade_chrome,
    "check-config": cmd_check_config,
}


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
