"""Hypothesis profiles for the collector's property tests, and a stand-in Chrome process.

``ci`` (``CI`` set, or ``HYPOTHESIS_PROFILE=ci``) is derandomized: a property that fails there
fails on every run of that commit, and a run that passes is not luck. Locally the default
``dev`` profile keeps exploring new examples (and replays saved failures from ``.hypothesis``).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator

import pytest
from hypothesis import settings

settings.register_profile("ci", derandomize=True, print_blob=True, deadline=None)
settings.register_profile("dev")
settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
)


@pytest.fixture
def chrome_pid() -> Iterator[subprocess.Popen[bytes]]:
    """A harmless child process standing in for Chrome, so a SIGKILL has a real target."""
    child = subprocess.Popen(["/bin/sleep", "60"])
    yield child
    child.kill()
    child.wait()
