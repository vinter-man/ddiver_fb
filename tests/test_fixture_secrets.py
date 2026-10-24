"""The secret scan over ``tests/fixtures`` and the system captures (DESIGN, Phase 0: fixtures).

Fixtures are built by allow-list (``tools/make_fixture.py``), and this is the second line: it
fails CI on anything that looks like a session secret or tracking token, on any non-zero
account id under the keys Facebook uses for the viewer (``USER_ID``, ``ACCOUNT_ID``,
``actorID``, ``__user``, ``av``), and on the listener account's real ids and names, which are
listed one per line in ``tests/fixtures/.forbidden`` — a local file that is git-ignored and
never committed (it would be the very leak it guards against). Lines starting with ``#`` are
comments; matching ignores case. Without that file the identity check is *skipped with a
visible reason* (``FBC_NO_FORBIDDEN=1`` says the skip is deliberate, e.g. in CI), never passed
silently.

Text is scanned as written and once more normalised — JSON escapes (``\\uXXXX``, nested
``\\"``) undone and URL-quoting removed, gzip'd fixtures unpacked — so an id stored inside a
JSON string inside a JSON string is still found. The system captures under
``tests/collector/system_fixtures`` (``arp``, ``netstat``, ``route``: a re-capture would carry the
home router's MAC, which is the home pin) may hold only documentation or private addresses and
locally administered MACs.
"""

from __future__ import annotations

import gzip
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SYSTEM_FIXTURES = Path(__file__).parent / "collector" / "system_fixtures"
FORBIDDEN_FILE = FIXTURES / ".forbidden"
PUBLIC_PROBES = frozenset({"1.1.1.1", "2606:4700:4700::1111"})
"""The public resolvers whose *route* the home guard looks up (``gates.PROBE_V4/V6``)."""

# JSON inside JSON (a GraphQL body stored as a string) escapes its quotes, hence the \\?".
PATTERNS: dict[str, re.Pattern[str]] = {
    "DTSGInitialData": re.compile(r"DTSGInitialData"),
    '"LSD"': re.compile(r'\\?"LSD\\?"'),
    "fb_dtsg": re.compile(r"fb_dtsg", re.IGNORECASE),
    "c_user": re.compile(r"c_user", re.IGNORECASE),
    "xs=": re.compile(r"(?<![A-Za-z0-9_])xs="),
    "oh=": re.compile(r"(?<![A-Za-z0-9_])oh="),
    "encrypted_tracking": re.compile(r"encrypted_tracking"),
}
_ACCOUNT_ID = re.compile(
    r'(?<![A-Za-z0-9_])(USER_ID|ACCOUNT_ID|actorID|actor_id|__user|av)(?:\\*")?\s*[:=]\s*'
    r'(?:\\*")?\s*([0-9]+)'
)
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})")
_MAC = re.compile(r"(?<![0-9A-Fa-f:])([0-9A-Fa-f]{1,2}(?::[0-9A-Fa-f]{1,2}){5})(?![0-9A-Fa-f:])")
_SYNTHETIC_MACS = ("02:00:5e:", "01:00:5e:", "33:33:")
"""Locally administered (the repository's synthetic prefix) and multicast-derived MACs."""


def normalise(text: str) -> str:
    """Undo JSON escaping (nested too) and URL quoting, a few rounds deep."""
    for _ in range(4):
        before = text
        text = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
        text = text.replace('\\"', '"').replace("\\/", "/")
        text = unquote(text)
        if text == before:
            break
    return text


def load_forbidden(path: Path = FORBIDDEN_FILE) -> list[str]:
    if not path.is_file():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def scan(text: str, forbidden: list[str] | None = None) -> list[str]:
    """Names of everything forbidden that ``text`` contains, as written or normalised."""
    found: list[str] = []
    for variant in dict.fromkeys((text, normalise(text))):
        found += [name for name, pattern in PATTERNS.items() if pattern.search(variant)]
        found += [
            f"{m.group(1)} {m.group(2)}"
            for m in _ACCOUNT_ID.finditer(variant)
            if int(m.group(2)) != 0
        ]
        lowered = variant.lower()
        found += [
            f"forbidden entry #{i + 1}"
            for i, term in enumerate(forbidden or [])
            if term.lower() in lowered
        ]
    return list(dict.fromkeys(found))


def network_leaks(text: str) -> list[str]:
    """Public addresses (other than the probed resolvers) and real MACs in a system capture."""
    found: list[str] = []
    for token in [*_IPV4.findall(text), *_IPV6.findall(text)]:
        try:
            address = ipaddress.ip_address(token)
        except ValueError:
            continue
        if address.is_global and not address.is_multicast and str(address) not in PUBLIC_PROBES:
            found.append(f"public address {address}")
    for token in _MAC.findall(text):
        mac = ":".join(part.zfill(2) for part in token.lower().split(":"))
        if mac in {"ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"} or mac.startswith(_SYNTHETIC_MACS):
            continue
        found.append(f"MAC {mac}")
    return found


def read_text(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file() and p.name != FORBIDDEN_FILE.name)


def system_files() -> list[Path]:
    return sorted(p for p in SYSTEM_FIXTURES.rglob("*") if p.is_file())


def test_there_are_fixtures_to_scan() -> None:
    assert len(fixture_files()) >= 10
    assert len(system_files()) >= 10


@pytest.mark.parametrize("path", fixture_files() + system_files(), ids=lambda p: p.name)
def test_fixture_carries_no_secrets(path: Path) -> None:
    # Entries are reported by line number, never echoed: the output of a failing CI job is
    # itself somewhere a real id should not appear.
    assert scan(read_text(path), load_forbidden()) == []


@pytest.mark.parametrize("path", system_files(), ids=lambda p: p.name)
def test_system_captures_carry_no_real_network(path: Path) -> None:
    assert network_leaks(read_text(path)) == []


def test_the_listeners_identity_is_checked() -> None:
    """The scan above knows the listener's real id and name only from the local file."""
    if FORBIDDEN_FILE.is_file():
        assert load_forbidden(), f"{FORBIDDEN_FILE.name} lists nothing"
        return
    if os.environ.get("FBC_NO_FORBIDDEN") == "1":
        return  # deliberately without it (CI): the generic patterns still ran
    pytest.skip(
        "tests/fixtures/.forbidden is missing: fixtures are NOT checked for the listener's real "
        "id and name (create it, see docs/RUNBOOK.md, or set FBC_NO_FORBIDDEN=1)"
    )


@pytest.mark.parametrize(
    ("sample", "hit"),
    [
        ('["DTSGInitialData",[],{"token":"x"}]', "DTSGInitialData"),
        ('["LSD",[],{"token":"x"}]', '"LSD"'),
        ('{\\"LSD\\":1}', '"LSD"'),
        ("fb_dtsg=AQH", "fb_dtsg"),
        ("cookie: c_user=100", "c_user"),
        ("xs=12%3Aabc", "xs="),
        ("https://scontent.x/v.jpg?_nc_ht=x&oh=00_AQ", "oh="),
        ('"encrypted_tracking":"AZ"', "encrypted_tracking"),
        ('"USER_ID":"100012345678901"', "USER_ID 100012345678901"),
        ('{\\"USER_ID\\":\\"42\\"}', "USER_ID 42"),
        ('"USER_ID": 7', "USER_ID 7"),
        ('{"actorID":"100012345678901"}', "actorID 100012345678901"),
        ('{"ACCOUNT_ID":"100012345678901"}', "ACCOUNT_ID 100012345678901"),
        ("__user=100012345678901&a=1", "__user 100012345678901"),
        ("&av=100012345678901", "av 100012345678901"),
        ('{"c_user":1}', "c_user"),
        ('"fb_dtsg"', "fb_dtsg"),
        ('\\\\\\"USER_ID\\\\\\":\\\\\\"100012345678901\\\\\\"', "USER_ID 100012345678901"),
        ("\\u0022USER_ID\\u0022:\\u0022100012345678901\\u0022", "USER_ID 100012345678901"),
        ("USER_ID%22%3A%22100012345678901", "USER_ID 100012345678901"),
    ],
)
def test_scanner_catches(sample: str, hit: str) -> None:
    assert hit in scan(sample)


@pytest.mark.parametrize(
    ("sample", "hit"),
    [
        ("? (192.168.1.1) at 3c:22:fb:12:34:56 on en0", "MAC 3c:22:fb:12:34:56"),
        ("default 81.2.69.160 UGScg en0", "public address 81.2.69.160"),
        ("default 2a00:1450:4001::1 UGcg en0", "public address 2a00:1450:4001::1"),
    ],
)
def test_network_check_catches(sample: str, hit: str) -> None:
    assert hit in network_leaks(sample)


def test_network_check_passes_synthetic_captures() -> None:
    sample = (
        "? (192.0.2.1) at 2:0:5e:10:0:1 on en0\n1.1.1.1 via 10.8.0.1\nfe80::1%utun0 "
        "fd7a:115c:a1e0::a 224.0.0.251 01:00:5e:00:00:fb ff:ff:ff:ff:ff:ff 127.0.0.1"
    )
    assert network_leaks(sample) == []


def test_gzip_fixtures_are_unpacked(tmp_path: Path) -> None:
    path = tmp_path / "x.json.gz"
    path.write_bytes(gzip.compress(b'{"USER_ID":"100012345678901"}'))
    assert scan(read_text(path)) == ["USER_ID 100012345678901"]


@pytest.mark.parametrize(
    "sample",
    ['"USER_ID":"0"', '{\\"USER_ID\\":\\"0\\"}', "boxs=1 photo=2", '"LSDX"', "no secrets here"],
)
def test_scanner_passes(sample: str) -> None:
    assert scan(sample) == []


def test_forbidden_file(tmp_path: Path) -> None:
    path = tmp_path / ".forbidden"
    path.write_text("# the listener account\n\n100098765432101\nJane Listener\n", encoding="utf-8")
    terms = load_forbidden(path)
    assert terms == ["100098765432101", "Jane Listener"]
    assert scan("posted by JANE LISTENER", terms) == ["forbidden entry #2"]
    assert scan("id 100098765432101", terms) == ["forbidden entry #1"]
    assert load_forbidden(tmp_path / "missing") == []


def test_forbidden_file_is_ignored_by_git() -> None:
    gitignore = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "tests/fixtures/.forbidden" in gitignore.splitlines()
