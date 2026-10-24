"""The VPS's side of the tunnel (``deploy/vps/setup-fbtunnel.sh``), run for real in a temporary
root.

Nothing here touches the VPS or this machine's own sshd. The script runs with ``--root`` on a
tree of its own and meets stubs for every command that would change the system: ``id`` (it
believes it is root), ``useradd``, ``usermod``, ``chown``, ``systemctl`` and ``fail2ban-client``.
``sshd`` is the system's own (macOS has one), only ever with ``-f`` and ``-h`` pointing into the
temporary tree, run as ``sshd -t`` and ``sshd -T``: it parses the configuration and prints what
it makes of it, and neither listens nor connects. Without a system sshd those tests are skipped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "vps" / "setup-fbtunnel.sh"
SNIPPET = REPO / "deploy" / "vps" / "20-fbtunnel.conf"
SSHD = Path("/usr/sbin/sshd")
SSH_KEYGEN = shutil.which("ssh-keygen")
PERMIT = 'restrict,port-forwarding,permitopen="127.0.0.1:8781"'

needs_sshd = pytest.mark.skipif(
    not SSHD.is_file() or SSH_KEYGEN is None, reason="no system sshd or ssh-keygen to parse with"
)

STUB_ID = """#!/bin/bash
echo "id $*" >>"$STUB_STATE/calls"
case "$*" in
  -u) cat "$STUB_STATE/uid" ;;
  "-u fbtunnel") [ -e "$STUB_STATE/user" ] && echo 998 || exit 1 ;;
  "-Gn fbtunnel") echo "fbtunnel" ;;
  *) exit 1 ;;
esac
"""
STUB_LOG = """#!/bin/bash
echo "$(basename "$0") $*" >>"$STUB_STATE/calls"
case "$(basename "$0") $1" in
  "useradd "*) touch "$STUB_STATE/user" ;;
  "systemctl cat") [ "$2" = "$(cat "$STUB_STATE/unit")" ] || exit 1 ;;
  "systemctl is-active") [ -e "$STUB_STATE/active" ] || exit 3 ;;
  "fail2ban-client get") [ -e "$STUB_STATE/$3" ] || exit 255; cat "$STUB_STATE/$3" ;;
esac
exit 0
"""


@dataclass
class Vps:
    root: Path
    state: Path
    env: dict[str, str]
    host_blob: str

    @property
    def ssh(self) -> Path:
        return self.root / "etc" / "ssh"

    @property
    def keys(self) -> Path:
        return self.ssh / "fbtunnel_keys"

    def calls(self) -> list[str]:
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def run(self, *args: str, sudo_user: str | None = "admin") -> tuple[int, str]:
        env = dict(self.env)
        if sudo_user is not None:
            env["SUDO_USER"] = sudo_user
        done = subprocess.run(  # noqa: S603 - our own script
            [
                "/bin/bash",
                str(self.root / "checkout" / "setup-fbtunnel.sh"),
                "--root",
                str(self.root),
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        return done.returncode, done.stdout + done.stderr


def keygen(path: Path, comment: str = "") -> str:
    """A throwaway ed25519 key at ``path``; returns its public line."""
    assert SSH_KEYGEN is not None
    subprocess.run(  # noqa: S603 - the system's ssh-keygen, a key in a temporary directory
        [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return path.with_suffix(".pub").read_text().strip()


@pytest.fixture
def vps(tmp_path: Path) -> Vps:
    if not SSHD.is_file() or SSH_KEYGEN is None:
        pytest.skip("no system sshd or ssh-keygen to parse with")
    root = tmp_path / "root"
    ssh = root / "etc" / "ssh"
    (ssh / "sshd_config.d").mkdir(parents=True)
    host_line = keygen(ssh / "ssh_host_ed25519_key", "root@vps")
    # The checkout's two files, as /srv/ddiver-fb/deploy/vps holds them.
    checkout = root / "checkout"
    checkout.mkdir()
    shutil.copy2(SCRIPT, checkout / "setup-fbtunnel.sh")
    shutil.copy2(SNIPPET, checkout / "20-fbtunnel.conf")
    (ssh / "sshd_config").write_text(
        f"Include {ssh}/sshd_config.d/*.conf\n"
        "PasswordAuthentication yes\n"
        "X11Forwarding yes\n"
        "AcceptEnv LANG LC_*\n"
    )
    state = tmp_path / "state"
    stubs = tmp_path / "stubs"
    state.mkdir()
    stubs.mkdir()
    (state / "uid").write_text("0\n")
    (state / "unit").write_text("ssh.service\n")
    (state / "active").write_text("")
    (state / "maxretry").write_text("5\n")
    (state / "findtime").write_text("600\n")
    (stubs / "id").write_text(STUB_ID)
    for name in ("useradd", "usermod", "chown", "systemctl", "fail2ban-client"):
        (stubs / name).write_text(STUB_LOG)
    # sshd parses the temporary tree's configuration, never the system's.
    (stubs / "sshd").write_text(
        "#!/bin/bash\n"
        'echo "sshd $*" >>"$STUB_STATE/calls"\n'
        f'exec {SSHD} -f "{ssh}/sshd_config" -h "{ssh}/ssh_host_ed25519_key" "$@"\n'
    )
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
        "STUB_STATE": str(state),
        "LANG": "C",
    }
    return Vps(root, state, env, host_line.split()[1])


@pytest.fixture
def mac_key(tmp_path: Path) -> Path:
    """The Mac's key as the door's tunnel-key prints it, copied to the VPS."""
    line = keygen(tmp_path / "mac", "fbtunnel-mac-1")
    out = tmp_path / "fbtunnel-mac-1.pub"
    out.write_text(line + "\n")
    return out


@needs_sshd
def test_a_first_run_makes_the_user_and_sshd_ready(vps: Vps) -> None:
    code, out = vps.run()
    assert code == 0, out
    calls = vps.calls()
    assert (
        "useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin"
        " fbtunnel"
    ) in calls
    assert "usermod --shell /usr/sbin/nologin --password * fbtunnel" in calls
    installed = vps.ssh / "sshd_config.d" / "20-fbtunnel.conf"
    assert installed.read_bytes() == SNIPPET.read_bytes()
    assert f"chown root:root {vps.keys}" in calls
    assert oct(installed.stat().st_mode & 0o777) == "0o644"
    assert vps.keys.read_text() == ""
    assert oct(vps.keys.stat().st_mode & 0o777) == "0o644"
    assert "sshd -t" in calls
    assert "sshd -T -C user=fbtunnel,host=fbtunnel-check,addr=192.0.2.1" in calls
    assert "sshd -T -C user=admin,host=fbtunnel-check,addr=192.0.2.1" in calls
    reload = calls.index("systemctl reload ssh.service")
    assert calls[reload - 1] == "systemctl is-active --quiet ssh.service"
    assert calls.index("sshd -t") < reload
    assert f"ssh-ed25519 {vps.host_blob}" in out
    assert "fingerprint: 256 SHA256:" in out
    assert "maxretry 5 within findtime 600 s" in out
    assert "4 attempts within findtime, under maxretry" in out
    assert [p.name for p in (vps.ssh / "sshd_config.d").iterdir()] == ["20-fbtunnel.conf"]

    # Again: nothing made twice, nothing rewritten.
    code, out = vps.run()
    assert code == 0, out
    assert sum(c.startswith("useradd") for c in vps.calls()) == 1
    assert "unchanged" in out


@needs_sshd
def test_the_macs_key_gets_one_forward_and_replaces_its_old_line(
    vps: Vps, mac_key: Path, tmp_path: Path
) -> None:
    blob = mac_key.read_text().split()[1]
    code, out = vps.run("--key", str(mac_key))
    assert code == 0, out
    assert vps.keys.read_text() == f"{PERMIT} ssh-ed25519 {blob} fbtunnel-mac-1\n"
    assert vps.run("--key", str(mac_key))[0] == 0  # the same key again: one line
    assert vps.keys.read_text() == f"{PERMIT} ssh-ed25519 {blob} fbtunnel-mac-1\n"
    # A new key of the same collector replaces the old one; another collector's is added.
    new = tmp_path / "new.pub"
    new.write_text(keygen(tmp_path / "new", "fbtunnel-mac-1") + "\n")
    laptop = tmp_path / "laptop.pub"
    laptop.write_text(keygen(tmp_path / "laptop", "fbtunnel-laptop-1") + "\n")
    assert vps.run("--key", str(new))[0] == 0
    assert vps.run("--key", str(laptop))[0] == 0
    new_blob, laptop_blob = new.read_text().split()[1], laptop.read_text().split()[1]
    assert vps.keys.read_text() == (
        f"{PERMIT} ssh-ed25519 {new_blob} fbtunnel-mac-1\n"
        f"{PERMIT} ssh-ed25519 {laptop_blob} fbtunnel-laptop-1\n"
    )
    # The same key under another name replaces its line too: one key, one line.
    renamed = tmp_path / "renamed.pub"
    renamed.write_text(f"ssh-ed25519 {laptop_blob} fbtunnel-laptop-2\n")
    assert vps.run("--key", str(renamed))[0] == 0
    assert vps.keys.read_text() == (
        f"{PERMIT} ssh-ed25519 {new_blob} fbtunnel-mac-1\n"
        f"{PERMIT} ssh-ed25519 {laptop_blob} fbtunnel-laptop-2\n"
    )


@needs_sshd
@pytest.mark.parametrize(
    "text",
    [
        "{opts} ssh-ed25519 {blob} fbtunnel-mac-1\n",
        'command="/bin/sh" ssh-ed25519 {blob} fbtunnel-mac-1\n',
        "ssh-ed25519 {blob}\n",
        "ssh-ed25519 {blob} owner@laptop\n",
        "ssh-ed25519 {blob} fbtunnel-mac-1 extra\n",
        "ssh-rsa {blob} fbtunnel-mac-1\n",
        "ssh-ed25519 {blob} fbtunnel-mac-1\nssh-ed25519 {blob} fbtunnel-mac-2\n",
        "",
    ],
)
def test_a_key_file_of_another_shape_is_refused(
    vps: Vps, mac_key: Path, tmp_path: Path, text: str
) -> None:
    blob = mac_key.read_text().split()[1]
    bad = tmp_path / "bad.pub"
    bad.write_text(text.format(blob=blob, opts='restrict,permitopen="127.0.0.1:22"'))
    code, out = vps.run("--key", str(bad))
    assert code != 0
    assert "not one 'ssh-ed25519 AAAA... fbtunnel-<id>' line" in out
    assert not vps.keys.exists() or vps.keys.read_text() == ""
    assert not any(c.startswith("systemctl reload") for c in vps.calls())


@needs_sshd
def test_a_match_that_leaks_to_other_users_is_refused(vps: Vps) -> None:
    config = vps.ssh / "sshd_config"
    config.write_text(config.read_text() + "MaxSessions 0\n")
    code, out = vps.run()
    assert code != 0
    assert "the Match block leaks: sshd -T for admin shows 'maxsessions 0'" in out
    assert not any(c.startswith("systemctl reload") for c in vps.calls())


@needs_sshd
def test_a_setting_another_file_made_first_is_found(vps: Vps) -> None:
    # sshd takes the first value it reads: a file sorted before ours can undo the Match block.
    (vps.ssh / "sshd_config.d" / "10-early.conf").write_text(
        "Match User fbtunnel\n    AllowTcpForwarding yes\n"
    )
    code, out = vps.run()
    assert code != 0
    assert "does not show 'allowtcpforwarding local'" in out


@needs_sshd
@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("AllowUsers admin deploy", "AllowUsers leaves fbtunnel out"),
        ("AllowGroups sudo", "AllowGroups leaves fbtunnel"),
        ("DenyUsers fb*", "DenyUsers names fbtunnel"),
        ("DenyGroups fbtunnel", "DenyGroups names fbtunnel"),
        ("AllowUsers admin fbtun*", None),
        ("AllowUsers admin fbtunnel@*", None),
        ("AllowGroups sudo fbtunnel", None),
    ],
)
def test_access_lists_must_let_the_tunnel_user_in(vps: Vps, line: str, why: str | None) -> None:
    config = vps.ssh / "sshd_config"
    config.write_text(config.read_text() + line + "\n")
    code, out = vps.run()
    if why is None:
        assert code == 0, out
        return
    assert code != 0
    assert why in out


@needs_sshd
def test_what_sshd_must_hold_before_anything_is_reloaded(vps: Vps) -> None:
    config = vps.ssh / "sshd_config"
    text = config.read_text()
    config.write_text(text.split("\n", 1)[1])  # no Include of sshd_config.d
    code, out = vps.run()
    assert code != 0
    assert "does not include sshd_config.d/*.conf" in out
    config.write_text(text + "NoSuchKeyword yes\n")
    code, out = vps.run()
    assert code != 0
    assert "sshd -t refuses the configuration" in out
    config.write_text(text)
    (vps.state / "uid").write_text("1000\n")
    code, out = vps.run()
    assert code != 0
    assert "run it as root" in out
    assert not any(c.startswith("systemctl reload") for c in vps.calls())


@needs_sshd
def test_sshd_and_fail2ban_as_the_vps_has_them(vps: Vps) -> None:
    (vps.state / "unit").write_text("sshd.service\n")
    (vps.state / "active").unlink()
    (vps.state / "maxretry").write_text("4\n")
    code, out = vps.run(sudo_user=None)  # straight as root: root is the other user checked
    assert code == 0, out
    assert "sshd -T -C user=root,host=fbtunnel-check,addr=192.0.2.1" in vps.calls()
    assert not any(c.startswith("systemctl reload") for c in vps.calls())
    assert "sshd.service is not running" in out
    assert "4 attempts within findtime: NOT under maxretry 4" in out
    (vps.state / "maxretry").unlink()
    code, out = vps.run()
    assert code == 0, out
    assert "fail2ban: no sshd jail here" in out
    (vps.state / "unit").write_text("none\n")
    code, out = vps.run()
    assert code != 0
    assert "neither ssh.service nor sshd.service" in out


@pytest.mark.skipif(not SSHD.is_file(), reason="no system sshd to parse with")
def test_the_snippet_gives_the_tunnel_user_one_forward_and_nobody_else_anything(
    tmp_path: Path,
) -> None:
    """The shipped 20-fbtunnel.conf, included the way Debian's sshd_config includes the
    directory, as sshd itself reads it for fbtunnel and for another user."""
    assert SSH_KEYGEN is not None
    (tmp_path / "sshd_config.d").mkdir()
    shutil.copy2(SNIPPET, tmp_path / "sshd_config.d" / "20-fbtunnel.conf")
    keygen(tmp_path / "host")
    config = tmp_path / "sshd_config"
    config.write_text(
        f"Include {tmp_path}/sshd_config.d/*.conf\nPasswordAuthentication yes\nX11Forwarding yes\n"
    )

    def effective(user: str) -> dict[str, str]:
        done = subprocess.run(  # noqa: S603 - the system's sshd, parsing a temporary config
            [
                str(SSHD),
                "-T",
                "-f",
                str(config),
                "-h",
                str(tmp_path / "host"),
                "-C",
                f"user={user},host=x,addr=192.0.2.1",
            ],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        return dict(line.split(" ", 1) for line in done.stdout.splitlines() if " " in line)

    tunnel = effective("fbtunnel")
    assert {
        k: tunnel[k]
        for k in (
            "allowtcpforwarding",
            "permitopen",
            "permitlisten",
            "forcecommand",
            "maxsessions",
            "permittty",
            "authorizedkeysfile",
            "passwordauthentication",
            "x11forwarding",
            "clientaliveinterval",
            "clientalivecountmax",
            "allowagentforwarding",
            "allowstreamlocalforwarding",
        )
    } == {
        "allowtcpforwarding": "local",
        "permitopen": "127.0.0.1:8781",
        "permitlisten": "none",
        "forcecommand": "/bin/false",
        "maxsessions": "0",
        "permittty": "no",
        "authorizedkeysfile": "/etc/ssh/fbtunnel_keys",
        "passwordauthentication": "no",
        "x11forwarding": "no",
        "clientaliveinterval": "30",
        "clientalivecountmax": "4",
        "allowagentforwarding": "no",
        "allowstreamlocalforwarding": "no",
    }
    other = effective("admin")
    assert other["forcecommand"] != "/bin/false"
    assert other["maxsessions"] != "0"
    assert other["permitopen"] == "any"
    assert other["passwordauthentication"] == "yes"  # the main file still decides for others
