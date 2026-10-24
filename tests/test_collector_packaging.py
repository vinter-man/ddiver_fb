"""The collector's units and tunnel configs say what DESIGN (Operations) says they must."""

from __future__ import annotations

import configparser
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def systemd(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # type: ignore[assignment, method-assign]
    parser.read_string(path.read_text())
    return parser


def directives(path: Path) -> dict[str, str]:
    """``Key value`` lines (sshd / ssh_config), keys lower-cased; the Match line included."""
    found: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        assert key.lower() not in found, f"{key} set twice"
        found[key.lower()] = value.strip()
    return found


def test_launch_agent() -> None:
    path = DEPLOY / "macos" / "com.ddiver.fbcollector.plist"
    plist = plistlib.loads(path.read_bytes())
    assert plist["Label"] == "com.ddiver.fbcollector"
    assert path.name == f"{plist['Label']}.plist"
    assert plist["LimitLoadToSessionType"] == "Aqua"
    assert plist["ProcessType"] == "Interactive"
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] >= 60
    assert plist["Umask"] == 0o077
    args = plist["ProgramArguments"]
    assert args[:3] == ["/usr/bin/caffeinate", "-i", "-s"]
    assert args[3].endswith("/.venv/bin/fbcollector")
    assert args[4:] == ["run"]
    assert "Program" not in plist
    assert plist["StandardOutPath"].endswith("/Library/Logs/fbcollector.log")


def test_tunnel_launch_agent() -> None:
    """The macOS tunnel: /usr/bin/ssh alone under launchd, its -F path absolute once installed,
    restarted at once after a long session and at most every 200 s after a refused one."""
    path = DEPLOY / "macos" / "com.ddiver.fbtunnel.plist"
    plist = plistlib.loads(path.read_bytes())
    assert plist["Label"] == "com.ddiver.fbtunnel"
    assert path.name == f"{plist['Label']}.plist"
    assert plist["ProgramArguments"] == [
        "/usr/bin/ssh",
        "-N",
        "-F",
        "__HOME__/.ssh/fbtunnel_client.config",
        "fbtunnel",
    ]
    assert "Program" not in plist
    assert plist["LimitLoadToSessionType"] == "Aqua"
    assert plist["ProcessType"] == "Standard"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    # fail2ban's default sshd jail bans at 5 failures in 10 minutes: 600 / 200 + 1 = 4
    # attempts, the window counting both ends.
    assert plist["ThrottleInterval"] == 200
    assert plist["Umask"] == 0o077
    assert plist["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}
    assert plist["StandardOutPath"] == "__HOME__/Library/Logs/fbtunnel.log"
    assert plist["StandardErrorPath"] == plist["StandardOutPath"]


@pytest.mark.skipif(shutil.which("plutil") is None, reason="macOS plutil not available")
@pytest.mark.parametrize("name", ["com.ddiver.fbcollector.plist", "com.ddiver.fbtunnel.plist"])
def test_launch_agent_lints_with_plutil(name: str) -> None:
    done = subprocess.run(  # noqa: S603 - fixed tool, our own file
        ["plutil", "-lint", str(DEPLOY / "macos" / name)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_collector_unit() -> None:
    unit = systemd(DEPLOY / "linux" / "fbcollector.service")
    assert unit["Unit"]["PartOf"] == "graphical-session.target"
    assert unit["Unit"]["After"] == "graphical-session.target"
    assert unit["Unit"]["StartLimitIntervalSec"] == "0"
    assert unit["Install"]["WantedBy"] == "graphical-session.target"
    service = unit["Service"]
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "30"
    assert service["ExecStart"].endswith("/.venv/bin/fbcollector run")
    assert service["UMask"] == "0077"
    assert int(service["TimeoutStopSec"]) >= 60  # room to park and close Chrome


def test_tunnel_unit() -> None:
    unit = systemd(DEPLOY / "linux" / "autossh.service")
    service = unit["Service"]
    assert service["Environment"] == "AUTOSSH_GATETIME=0"
    assert int(service["RestartSec"]) >= 200  # 4 restarts in fail2ban's default 10 minutes
    assert service["Restart"] == "always"
    exec_start = service["ExecStart"].split()
    assert exec_start[:3] == ["/usr/bin/autossh", "-M", "0"]
    assert "-N" in exec_start
    assert exec_start[exec_start.index("-F") + 1].endswith("fbtunnel_client.config")
    assert unit["Unit"]["StartLimitIntervalSec"] == "0"


def test_tunnel_client_config() -> None:
    config = directives(DEPLOY / "ssh" / "fbtunnel_client.config")
    expected = {
        "host": "fbtunnel",
        "user": "fbtunnel",
        "exitonforwardfailure": "yes",
        "stricthostkeychecking": "yes",
        "batchmode": "yes",
        "identitiesonly": "yes",
        "serveraliveinterval": "15",
        "serveralivecountmax": "3",
        "forwardagent": "no",
        "forwardx11": "no",
        # The local end is a socket in the collector's own 0700 ~/.ssh (%d: the home), not a
        # loopback TCP port, which any local account could bind while ssh is down and so be
        # handed the bearer token. Owner-only, and a stale one is replaced at reconnect.
        "localforward": "%d/.ssh/fbtunnel.sock 127.0.0.1:8781",
        "streamlocalbindmask": "0177",
        "streamlocalbindunlink": "yes",
        # Placeholders the Mac's setup fills in from the pinned host key line.
        "hostname": "VPS_HOST",
        "port": "22",
        # launchd's SSH_AUTH_SOCK, the system's known hosts and other host key types are not
        # used; nothing learns a new host key; a dead network fails in 20 s.
        "identityagent": "none",
        "globalknownhostsfile": "/dev/null",
        "hostkeyalgorithms": "ssh-ed25519",
        "updatehostkeys": "no",
        "checkhostip": "no",
        "connecttimeout": "20",
        "identityfile": "~/.ssh/fbtunnel_ed25519",
        "userknownhostsfile": "~/.ssh/fbtunnel_known_hosts",
    }
    for key, value in expected.items():
        assert config[key] == value, key
    assert "remoteforward" not in config
    assert "dynamicforward" not in config


def test_sshd_snippet() -> None:
    config = directives(DEPLOY / "vps" / "20-fbtunnel.conf")
    assert config["match"] == "User fbtunnel"
    expected = {
        "allowtcpforwarding": "local",
        "permitopen": "127.0.0.1:8781",
        "permitlisten": "none",
        "allowstreamlocalforwarding": "no",
        "x11forwarding": "no",
        "allowagentforwarding": "no",
        "permittty": "no",
        "forcecommand": "/bin/false",
        "authorizedkeysfile": "/etc/ssh/fbtunnel_keys",
        "passwordauthentication": "no",
        # No shell, exec or subsystem session at all; the forward still works (sshd_config(5)).
        "maxsessions": "0",
        "clientaliveinterval": "30",
        "clientalivecountmax": "4",
    }
    for key, value in expected.items():
        assert config[key] == value, key
    # Everything after the Match line belongs to it: nothing global slipped in above it.
    first = next(
        line.strip()
        for line in (DEPLOY / "vps" / "20-fbtunnel.conf").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )
    assert re.fullmatch(r"Match User fbtunnel", first)


def test_the_runbook_says_how_to_reach_the_brain_and_move_the_lease() -> None:
    """Round 2 F4: the migration relied on the lease, and the runbook never named the id it is
    granted to, nor the settings that turn the uplink on."""
    runbook = (DEPLOY.parent / "docs" / "RUNBOOK.md").read_text()
    migration = runbook.split("## 8.", 1)[1].split("## 9.", 1)[0]
    assert "FBC_COLLECTOR_ID=laptop-1" in migration and "/lease laptop-1" in migration
    uplink = runbook.split("## 9.", 1)[1]
    for setting in (
        "FBC_INGEST_URL=http://127.0.0.1:8781",
        "FBC_INGEST_SOCKET=/Users/fbc/.ssh/fbtunnel.sock",
        "FBC_INGEST_TOKEN",
        "FBC_COLLECTOR_ID",
    ):
        assert setting in uplink


def uv_runs(text: str) -> list[str]:
    """Every ``uv run …`` command in ``text``, with its backslash-continued lines, up to the
    end of its code span."""
    runs = []
    for match in re.finditer(r"uv run\b", text):
        parts = []
        for line in text[match.start() :].split("\n"):
            head, tick, _ = line.partition("`")
            parts.append(head.rstrip().removesuffix("\\"))
            if tick or not line.rstrip().endswith("\\"):
                break
        runs.append(" ".join(" ".join(parts).split()))
    return runs


def test_the_runbook_runs_the_collector_alone() -> None:
    """A plain ``uv run`` syncs the whole workspace with its dev group (the brain, ddiver-core
    from the private ddiver repository, Telethon, pytest) into the ``.venv`` the collector's
    LaunchAgent runs; every ``uv run`` the runbook shows for the collector is the
    collector-only one."""
    runbook = (DEPLOY.parent / "docs" / "RUNBOOK.md").read_text()
    runs = uv_runs(runbook)
    assert "uv run --frozen --package fbcollector --no-dev fbcollector …" in runs
    for command in runs:
        if "fbcollector" in command:
            assert "uv run --frozen --package fbcollector --no-dev" in command, command
    assert "uv run fbcollector" not in " ".join(runbook.split())
    assert uv_runs("`uv run a \\\n  b` and `uv run c`") == ["uv run a b", "uv run c"]
