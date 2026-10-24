"""Watch posts flow from a collector to your bot on this machine, without Facebook.

Operator tool, never CI. It starts the brain (``BrainApp`` with the bot token from the
environment and a database in a fresh temporary directory) and a collector's store and uplink on
127.0.0.1. Instead of reading Facebook, the collector queues a made-up post in one made-up group
every ``--every`` seconds, exactly as the reader queues a post it parsed. Posts that match
``--rule`` reach you in Telegram as real notifications would; ``/status`` and the other owner
commands work in the bot meanwhile.

Nothing here starts Chrome or contacts Facebook: there is no driver at all. It does talk to
Telegram with your bot (that is its purpose), and it sets the bot's command list the way the
real brain does, so it refuses to run without ``--i-know``. The token is read from the process
environment only, never from a file:

    BOT_TOKEN=... API_ID=... API_HASH=... OWNER_ID=... \\
        uv run python scripts/local_e2e.py --i-know --rule flat --every 20 --posts 3
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import secrets
import socket
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import SecretStr

from ddiver_core.rules import RuleKind
from ddiver_fb.config import FbSettings
from ddiver_fb.runtime import BrainApp
from fbcollector.alerts import NullAlerts
from fbcollector.clock import SystemClock
from fbcollector.control import ControlCenter
from fbcollector.ladder import Ladder
from fbcollector.model import GroupMeta, Sighting, Surface
from fbcollector.store import Store
from fbcollector.sync import SyncOutcome
from fbcollector.uplink import IngestClient, Uplink
from fbwire.models import CollectorState, Item, Profile, Status, Via

REQUIRED_ENV = ("BOT_TOKEN", "API_ID", "API_HASH", "OWNER_ID")
GROUP = "900000000000001"
COLLECTOR = "local-e2e"


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    top = argparse.ArgumentParser(prog="local_e2e", description=__doc__.splitlines()[0])
    top.add_argument("--i-know", action="store_true", help="it messages you through your bot")
    top.add_argument("--rule", default="flat", help="the owner's rule for the fake group")
    top.add_argument("--every", type=float, default=30.0, help="seconds between fake posts")
    top.add_argument("--posts", type=int, default=3, help="how many fake posts to send")
    return top.parse_args(argv)


def refusal(args: argparse.Namespace) -> str | None:
    if not args.i_know:
        return "refusing: this sends real Telegram messages through your bot; add --i-know"
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        return "refusing: set " + ", ".join(missing) + " in the environment (never a file)"
    if args.every < 1 or not 1 <= args.posts <= 50:
        return "refusing: --every must be >= 1 s and --posts between 1 and 50"
    return None


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def run(args: argparse.Namespace, root: Path) -> int:
    token, port = secrets.token_urlsafe(32), free_port()
    settings = FbSettings(  # BOT_TOKEN, API_ID, API_HASH, OWNER_ID from the environment
        _env_file=None,  # type: ignore[call-arg]
        data_dir=root / "brain",
        ingest_host="127.0.0.1",
        ingest_port=port,
        ingest_token=SecretStr(token),
    )
    brain = BrainApp(settings)
    await brain.setup()
    serving = asyncio.create_task(brain.run())
    clock = SystemClock()
    store = Store(root / "collector.sqlite", clock=clock)
    await store.open()

    class Idle:
        async def build(self, *, seq: int | None = None, incarnation: str | None = None) -> Status:
            now = int(clock.time())
            return Status(
                collector_id=COLLECTOR,
                sent_at=now,
                state=CollectorState.OK,
                profile=Profile.CAUTIOUS,
                detail="local end-to-end run (no Facebook)",
                seq=seq,
                incarnation=incarnation,
            )

    control = ControlCenter(
        store, Ladder(store, clock), clock, collector_id=COLLECTOR, local_mode=False
    )
    uplink = Uplink(
        client=IngestClient(f"http://127.0.0.1:{port}", token),
        store=store,
        control=control,
        status=Idle(),
        alerts=NullAlerts(),
        clock=clock,
        collector_id=COLLECTOR,
    )
    try:
        while not brain.ingest.serving:
            if serving.done():
                return await serving
            await asyncio.sleep(0.2)
        await uplink.poll_control()
        await store.upsert_group(GroupMeta(id=GROUP, name="Local e2e group"), synced=True)
        await uplink.queue_snapshot(SyncOutcome(True, 1, 1, False, listed_ids=(GROUP,)))
        await uplink.cycle()
        owner = settings.owner_id
        await brain.storage.subscribe(owner, int(GROUP))
        await brain.storage.add_rule(owner, int(GROUP), RuleKind.INCLUDE, args.rule)
        await brain.invalidate(int(GROUP))
        await asyncio.sleep(2.5)  # a post must be newer than the subscription (D10)
        await uplink.poll_control()
        for n in range(1, args.posts + 1):
            post_id = f"{int(time.time())}{n:03d}"
            now = int(clock.time())
            item = Item(
                collector_id=COLLECTOR,
                seq=0,
                group_id=GROUP,
                post_id=post_id,
                url=f"https://www.facebook.com/groups/{GROUP}/posts/{post_id}/",
                group_name="Local e2e group",
                group_url=f"https://www.facebook.com/groups/{GROUP}/",
                created_at=now,
                first_seen_at=now,
                via=Via.FEED,
                text=f"Local end-to-end post {n} of {args.posts}: a {args.rule} (not on Facebook).",
            )
            sighting = Sighting(post_id=post_id, group_id=GROUP, surface=Surface.FEED)
            await store.mark_seen(sighting, via=Via.FEED, item=item, outcome="queued")
            await uplink.cycle()
            print(f"post {n}/{args.posts} sent: {uplink.stats.items_sent} delivered to the brain")
            await asyncio.sleep(args.every)
        return 0
    finally:
        brain.request_stop()
        with contextlib.suppress(Exception):
            await serving
        await brain.close()
        await store.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse(argv)
    problem = refusal(args)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with tempfile.TemporaryDirectory(prefix="ddiver-fb-e2e-") as tmp:
        try:
            return asyncio.run(run(args, Path(tmp)))
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    sys.exit(main())
