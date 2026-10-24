"""One full read cycle of the driver on the local page, for the protocol-trace test.

Run as ``python -m tests.collector.browser.trace_probe <profile dir>`` with
``DEBUG=pw:protocol``: Patchright's driver process then logs every CDP message it sends to
stderr, and this script prints what the guarded session sent (JSON, one line) to stdout.
Chrome is headless unless ``FBC_BROWSER_HEADED=1`` (the variable is inherited from the test).
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

from fbcollector.clock import SystemClock
from fbcollector.driver._patchright import PatchrightDriver, Timings
from fbcollector.driver.launch import SessionMode, launch_spec
from fbcollector.driver.policy import LocalOnly
from fbcollector.driver.selftest import LocalSite
from tests.collector.browser.headless import playwright_factory

FAST = Timings(
    dwell=(0.2, 0.3),
    story_poll_max=1.0,
    poll_interval=0.2,
    pagination_wait=3.0,
    pagination_finish=5.0,
    scroll_settle=0.2,
    park_wait=6.0,
    close_timeout=10.0,
)


async def main(profile: Path) -> None:
    with LocalSite() as site:
        driver = PatchrightDriver(
            launch_spec(profile),
            policy=LocalOnly(site.port),
            clock=SystemClock(),
            rng=random.Random(5),
            timings=FAST,
            factory=playwright_factory(),
        )
        await driver.launch(mode=SessionMode.READ)
        try:
            await driver.navigate(site.url("/"))
            await driver.scroll_step()
            await driver.dwell(0.1)
            await driver.cookie_names(site.url("/"))
            await driver.screenshot(profile.parent / "trace-shot.jpg.gz")
            await driver.park()
            sent = list(driver.cdp_sent)
        finally:
            await driver.close()
    sys.stdout.write(json.dumps({"cdp_sent": sent}) + "\n")


if __name__ == "__main__":
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    asyncio.run(main(target))
