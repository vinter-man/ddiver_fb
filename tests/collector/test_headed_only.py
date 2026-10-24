"""The production launch can never be headless (DESIGN, non-goals: no headless reading).

The browser tests run Chrome headless so they never open a window on the owner's screen. They
do it by wrapping the driver's ``factory`` (``tests/collector/browser/headless.py``); nothing in
the production path can do the same. These checks hold that from four sides:

- **The spec** has no ``headless`` field, and no platform's launch spec carries a
  ``--headless`` switch.
- **The driver's launch call** passes ``headless=False`` as a literal, spreads no ``**options``,
  and hands Chrome exactly the spec's switches (checked in the source, and at run time against
  a recording fake of Playwright).
- **The composition root** builds its drivers — the reading one and the self-test's — with the
  real ``async_playwright`` entry point, not a wrapper.
- **Nowhere in ``packages/`` or ``scripts/``** is ``headless`` passed as anything but the
  literal ``False``, or a ``--headless`` switch spelled out.

Each source rule is also run against small bad snippets, so the checker cannot pass by being
blind.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Any

import pytest
from patchright.async_api import async_playwright

from fbcollector.clock import FakeClock
from fbcollector.driver._patchright import PatchrightDriver
from fbcollector.driver.launch import LaunchSpec, SessionMode, launch_spec
from fbcollector.driver.policy import FacebookSurfaces, LocalOnly
from fbcollector.paths import Layout
from fbcollector.runner import Ports, default_driver_factory, default_local_factory

REPO = Path(__file__).resolve().parents[2]
DRIVER = REPO / "packages/fbcollector/fbcollector/driver/_patchright.py"
SCANNED = (REPO / "packages", REPO / "scripts")
PLATFORMS = ("darwin", "linux")


# -- the spec ---------------------------------------------------------------------------------


def test_the_launch_spec_has_no_headless_field(tmp_path: Path) -> None:
    assert "headless" not in {f.name for f in dataclasses.fields(LaunchSpec)}
    with pytest.raises(TypeError):
        LaunchSpec(tmp_path, (), (), headless=True)  # type: ignore[call-arg]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_no_platform_spec_asks_for_headless(tmp_path: Path, platform: str) -> None:
    spec = launch_spec(tmp_path, platform=platform)
    assert not [a for a in spec.args if a.lower().lstrip("-").startswith("headless")]


# -- the driver's launch call (source) --------------------------------------------------------


def launch_call_problems(source: str) -> list[str]:
    """Why the ``launch_persistent_context`` call(s) in ``source`` could launch headless."""
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "launch_persistent_context"
    ]
    if len(calls) != 1:
        return [f"{len(calls)} launch_persistent_context calls (expected exactly one)"]
    problems = []
    keywords = {k.arg: k.value for k in calls[0].keywords}
    if None in keywords:
        problems.append("a ** spread in the launch call")
    headless = keywords.get("headless")
    if not (isinstance(headless, ast.Constant) and headless.value is False):
        problems.append("headless is not the literal False")
    args = keywords.get("args")
    if args is None or ast.unparse(args) != "list(spec.args)":
        problems.append("args are not exactly the spec's")
    return problems


def test_the_driver_launches_headed_as_a_literal() -> None:
    assert launch_call_problems(DRIVER.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "c.launch_persistent_context(p, headless=spec.headless, args=list(spec.args))",
        "c.launch_persistent_context(p, headless=True, args=list(spec.args))",
        "c.launch_persistent_context(p, args=list(spec.args))",
        "c.launch_persistent_context(p, headless=False, args=list(spec.args), **extra)",
        "c.launch_persistent_context(p, headless=False, args=[*spec.args, '--headless'])",
        "c.launch_persistent_context(p, headless=False)",
        (
            "c.launch_persistent_context(p, headless=False, args=list(spec.args))\n"
            "c.launch_persistent_context(p, headless=True, args=list(spec.args))"
        ),
        "pass",
    ],
)
def test_the_launch_call_check_is_not_blind(snippet: str) -> None:
    assert launch_call_problems(snippet)


# -- the driver's launch call (run time) ------------------------------------------------------


class RefusedError(Exception):
    """The recording fake's way to stop a launch once it has seen the options."""


class RecordingChromium:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def launch_persistent_context(self, user_data_dir: str, **options: Any) -> Any:
        self.calls.append({"user_data_dir": user_data_dir, **options})
        raise RefusedError


class RecordingPlaywright:
    def __init__(self) -> None:
        self.chromium = RecordingChromium()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class RecordingManager:
    last: RecordingPlaywright | None = None

    async def start(self) -> RecordingPlaywright:
        RecordingManager.last = RecordingPlaywright()
        return RecordingManager.last


@pytest.mark.parametrize("mode", list(SessionMode))
@pytest.mark.parametrize("platform", PLATFORMS)
async def test_the_driver_asks_playwright_for_a_headed_chrome(
    tmp_path: Path, platform: str, mode: SessionMode
) -> None:
    """Whether the collector reads or the owner logs in, the same headed launch."""
    spec = launch_spec(tmp_path / "profile", platform=platform)
    driver = PatchrightDriver(
        spec, policy=LocalOnly(8765), clock=FakeClock(), factory=RecordingManager
    )
    with pytest.raises(RefusedError):
        await driver.launch(mode=mode)
    playwright = RecordingManager.last
    assert playwright is not None
    (options,) = playwright.chromium.calls
    assert options["headless"] is False
    assert options["args"] == list(spec.args)
    assert options["channel"] == "chrome"
    assert options["user_data_dir"] == str(spec.profile)
    assert playwright.stopped  # a failed launch leaves nothing behind
    assert not driver.running


# -- the composition root ---------------------------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_production_drivers_use_the_real_playwright_entry_point(
    tmp_path: Path, platform: str
) -> None:
    layout = Layout(tmp_path)
    ports = Ports(clock=FakeClock(), platform=platform)
    reading = default_driver_factory(layout, ports)(FacebookSurfaces())
    selftest = default_local_factory(layout, ports)(8765)
    for driver in (reading, selftest):
        assert isinstance(driver, PatchrightDriver)
        assert driver._factory is async_playwright
        assert driver._spec == launch_spec(layout.profile, platform=platform)


# -- nowhere else -----------------------------------------------------------------------------


def headless_problems(source: str) -> list[str]:
    """``headless=<anything but False>`` or a spelled-out ``--headless`` switch."""
    problems = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "headless":
            if not (isinstance(node.value, ast.Constant) and node.value.value is False):
                problems.append(f"headless={ast.unparse(node.value)}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.lower().lstrip().startswith("--headless")
        ):
            problems.append(f"switch {node.value!r}")
        elif isinstance(node, ast.Dict):
            problems += [
                "a 'headless' key"
                for key in node.keys
                if isinstance(key, ast.Constant) and key.value == "headless"
            ]
    return problems


def test_nothing_in_the_packages_or_scripts_launches_headless() -> None:
    found = {
        str(path.relative_to(REPO)): problems
        for root in SCANNED
        for path in sorted(root.rglob("*.py"))
        if (problems := headless_problems(path.read_text(encoding="utf-8")))
    }
    assert found == {}


@pytest.mark.parametrize(
    "snippet",
    [
        "launch(headless=True)",
        "launch(headless=settings.headless)",
        "args = ['--headless=new']",
        "args = ('--no-proxy-server', '--headless')",
        "options = {'headless': True}",
    ],
)
def test_the_package_scan_is_not_blind(snippet: str) -> None:
    assert headless_problems(snippet)
