"""Gates: every one must pass before Chrome launches, before each navigation and each scroll.

Invariant 2 is the reason this module exists: no Facebook traffic unless the machine is at home,
awake, in its pinned timezone, on AC power, outside quiet hours and breaks, inside the budget,
not blocked, not crash-looping and holding the profile lock. Each gate answers with a
``GateResult`` that says *why* it is closed and which collector state that means, so the
status the owner sees names the actual cause.

The home guard has two tiers. Cheap local checks run every time: the default gateway's MAC is
the pinned router's, no VPN-looking interface carries the default route (or the split halves
``0/1``+``128.0/1`` a full-tunnel VPN installs next to it), and no system proxy is set. The
public verdict — IPv4 and IPv6 egress addresses mapped to ASNs, compared with the pinned ones —
costs two HTTPS requests to non-Facebook services, so it is cached for at most 10 minutes and
dropped the moment any local fact changes or the machine slept. The lookups use
``trust_env=False`` (no proxy from the environment) and a transport bound to one address family,
so "the IPv6 egress" really is IPv6.

Interface-scoped routes (macOS flag ``I``) are ignored on purpose: Tailscale and the system's
own ``utun`` interfaces install scoped ``default`` routes that carry no ordinary traffic, and a
guard that counted them would never open on a Mac running Tailscale.

Every system command goes through an injected runner and every parser is a pure function over
its text, tested against sanitised outputs captured on the owner's Mac.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import fcntl
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import sys
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import httpx

from fbcollector.clock import SUSPEND_THRESHOLD, Clock, Mark
from fbcollector.model import CollectorState
from fbcollector.paths import CommandResult, Runner, run_command
from fbcollector.rhythm import Jitter

if TYPE_CHECKING:
    from fbcollector.store import ProcessKind

log = logging.getLogger(__name__)

HOUR: Final = 3600.0
DAY: Final = 86400.0
PUBLIC_VERDICT_TTL: Final = 600.0
PUBLIC_FAILURE_TTL: Final = 60.0
PROBE_V4: Final = "1.1.1.1"
PROBE_V6: Final = "2606:4700:4700::1111"
"""Public addresses whose *route* is looked up (``route get`` sends nothing to them)."""
VPN_INTERFACE: Final = re.compile(
    r"^(utun|tun|tap|wg|ppp|ipsec|gpd|zt|tailscale)\d*", re.IGNORECASE
)
SPLIT_DEFAULTS: Final = frozenset(
    {"default", "0/1", "128.0/1", "0.0.0.0/1", "128.0.0.0/1", "::/1", "8000::/1", "::/0"}
)
PROXY_KEYS: Final = (
    "HTTPEnable",
    "HTTPSEnable",
    "SOCKSEnable",
    "FTPEnable",
    "GopherEnable",
    "RTSPEnable",
    "ProxyAutoConfigEnable",
    "ProxyAutoDiscoveryEnable",
)
PROXY_ENV: Final = ("http_proxy", "https_proxy", "all_proxy", "socks_proxy")
KV_HOME_PIN: Final = "home_pin"
KV_LOGIN_AT: Final = "login_at"
SYSTEM_PATH: Final = "/usr/sbin:/usr/bin:/sbin:/bin"


class Activity(enum.StrEnum):
    LAUNCH = "launch"  # about to start Chrome
    NAVIGATE = "navigate"
    SCROLL = "scroll"


@dataclass(frozen=True, slots=True)
class GateContext:
    activity: Activity
    probe: bool = False
    """The step is the resume probe: the block gate lets exactly this through."""
    launches: int = 1
    """Chrome launches a ``LAUNCH`` starts: 2 when the local self-test runs before the read's
    own launch. The crash-loop gate counts all of them as pending, so none can pass the cap."""
    planned: bool = False
    """The launches are planned: Chrome ended by the collector's own close (a hold, a sleep, a
    stop), or this is the first launch of a planned process start, or a human's command. The
    crash-loop gate leaves them out of its unplanned count, never out of its cap on all."""


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: str
    ok: bool
    reason: str = ""
    state: CollectorState | None = None
    """The collector state while this gate is closed (``None``: transient, no state of its own)."""
    retry_at: float | None = None
    """When re-checking could help; ``None`` means it needs something outside the collector."""


STATE_PRIORITY: Final = (
    CollectorState.BLOCKED,
    CollectorState.LOGGED_OUT,
    CollectorState.CRASH_LOOP,
    CollectorState.NEEDS_LOGIN,
    CollectorState.PAUSED,
    CollectorState.NOT_HOME,
    CollectorState.UNKNOWN,
    CollectorState.WARMUP,
    CollectorState.QUIET,
    CollectorState.BREAK,
)


@dataclass(frozen=True, slots=True)
class GateReport:
    results: tuple[GateResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def closed(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if not r.ok)

    @property
    def state(self) -> CollectorState:
        states = {r.state for r in self.closed if r.state is not None}
        for state in STATE_PRIORITY:
            if state in states:
                return state
        return CollectorState.OK if self.ok else CollectorState.UNKNOWN

    @property
    def retry_at(self) -> float | None:
        """The latest retry time of the closed gates (all must reopen); ``None``: open-ended."""
        closed = self.closed
        if not closed or any(r.retry_at is None for r in closed):
            return None
        return max(r.retry_at for r in closed if r.retry_at is not None)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f"{r.gate}: {r.reason}" for r in self.closed)

    def as_status(self) -> dict[str, bool]:
        """The ``gates`` field of ``/v1/status``."""
        return {r.gate: r.ok for r in self.results}


class Gate(Protocol):
    name: str

    async def check(self, context: GateContext) -> GateResult: ...


class FuncGate:
    """A gate from a coroutine function (ladder, control and scheduler provide theirs so)."""

    def __init__(self, name: str, check: Callable[[GateContext], Awaitable[GateResult]]) -> None:
        self.name = name
        self._check = check

    async def check(self, context: GateContext) -> GateResult:
        return await self._check(context)


class GateSet:
    """Gates in order: cheap and state-changing first (sleep invalidates the home verdict)."""

    def __init__(self, gates: Sequence[Gate]) -> None:
        self._gates = tuple(gates)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(gate.name for gate in self._gates)

    async def evaluate(self, context: GateContext, *, stop_early: bool = True) -> GateReport:
        """Check every gate; with ``stop_early`` the first closed gate ends the check.

        A gate that raises is a closed gate: a probe that cannot run cannot say "home".
        """
        results = []
        for gate in self._gates:
            try:
                result = await gate.check(context)
            except Exception as exc:  # noqa: BLE001 - fail closed on any probe failure
                log.warning("gate %s failed: %s", gate.name, type(exc).__name__)
                result = GateResult(gate.name, ok=False, reason=f"check failed ({exc})")
            results.append(result)
            if stop_early and not result.ok:
                break
        return GateReport(tuple(results))


# -- parsers ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Route:
    gateway: str | None
    interface: str | None


@dataclass(frozen=True, slots=True)
class RouteEntry:
    destination: str
    gateway: str
    flags: str
    interface: str
    family: int  # 4 or 6


def normalize_mac(value: str | None) -> str | None:
    """``a4:2b:b0:1:2:3`` (macOS drops leading zeros) → ``a4:2b:b0:01:02:03``; else ``None``."""
    if not value:
        return None
    parts = re.split(r"[:-]", value.strip().lower())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9a-f]{1,2}", p) for p in parts):
        return None
    mac = ":".join(p.zfill(2) for p in parts)
    return None if mac in {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"} else mac


def parse_route_get(text: str) -> Route | None:
    """macOS ``route -n get <dest>``: the gateway and interface, or ``None`` if not routed."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in {"gateway", "interface"}:
            fields[key.strip()] = value.strip()
    if "interface" not in fields:
        return None
    return Route(gateway=fields.get("gateway"), interface=fields["interface"])


def parse_arp(text: str) -> str | None:
    """macOS ``arp -n <ip>``: ``? (192.0.2.1) at a4:2b:… on en0 …`` → the MAC."""
    match = re.search(r"\bat\s+([0-9a-fA-F:]+)\s", text + " ")
    return normalize_mac(match.group(1)) if match else None


def parse_netstat(text: str) -> list[RouteEntry]:
    """macOS ``netstat -rn``: every route of the Internet and Internet6 tables."""
    entries: list[RouteEntry] = []
    family = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Internet6"):
            family = 6
            continue
        if stripped.startswith("Internet"):
            family = 4
            continue
        cols = stripped.split()
        if not family or len(cols) < 4 or cols[0] == "Destination":
            continue
        entries.append(RouteEntry(cols[0], cols[1], cols[2], cols[3], family))
    return entries


def vpn_routes(entries: Iterable[RouteEntry]) -> tuple[str, ...]:
    """Default or split-default routes (not interface-scoped) through a VPN-like interface."""
    found = []
    for entry in entries:
        if entry.destination not in SPLIT_DEFAULTS or "I" in entry.flags:
            continue
        if VPN_INTERFACE.match(entry.interface):
            found.append(f"{entry.destination} via {entry.interface}")
    return tuple(dict.fromkeys(found))


def parse_scutil_proxy(text: str) -> tuple[str, ...]:
    """macOS ``scutil --proxy``: the proxy kinds that are switched on."""
    return tuple(
        key for key in PROXY_KEYS if re.search(rf"^\s*{key}\s*:\s*1\s*$", text, re.MULTILINE)
    )


class PowerSource(enum.StrEnum):
    AC = "ac"
    BATTERY = "battery"
    UNKNOWN = "unknown"


def parse_pmset(text: str) -> PowerSource:
    """macOS ``pmset -g batt``: ``Now drawing from 'AC Power'`` or ``'Battery Power'``."""
    match = re.search(r"drawing from '([^']+)'", text)
    if not match:
        return PowerSource.UNKNOWN
    source = match.group(1).lower()
    if "ac" in source.split():
        return PowerSource.AC
    if "battery" in source:
        return PowerSource.BATTERY
    return PowerSource.UNKNOWN


def parse_ip_route(text: str) -> Route | None:
    """Linux ``ip route get <dest>`` / ``ip route show default``: first route's via and dev."""
    for line in text.splitlines():
        dev = re.search(r"\bdev\s+(\S+)", line)
        if dev:
            via = re.search(r"\bvia\s+(\S+)", line)
            return Route(gateway=via.group(1) if via else None, interface=dev.group(1))
    return None


def parse_ip_neigh(text: str) -> str | None:
    """Linux ``ip neigh show <ip>``: ``192.0.2.1 dev wlan0 lladdr a4:2b:… REACHABLE``."""
    for line in text.splitlines():
        if "FAILED" in line or "INCOMPLETE" in line:
            continue
        match = re.search(r"\blladdr\s+([0-9a-fA-F:]+)", line)
        if match:
            return normalize_mac(match.group(1))
    return None


def parse_gsettings_mode(text: str) -> str | None:
    """``gsettings get org.gnome.system.proxy mode`` → ``none`` / ``manual`` / ``auto``."""
    value = text.strip().strip("'\"")
    return value or None


def linux_power(supplies: Mapping[str, Mapping[str, str]]) -> PowerSource:
    """``/sys/class/power_supply/*`` (``type``, ``online``, ``status``) → where power comes from.

    No supply at all is a desktop: mains. A mains adapter that is online wins; otherwise a
    discharging battery means battery power.
    """
    if not supplies:
        return PowerSource.AC
    mains = [s for s in supplies.values() if s.get("type") in {"Mains", "USB"}]
    if any(s.get("online") == "1" for s in mains):
        return PowerSource.AC
    batteries = [s for s in supplies.values() if s.get("type") == "Battery"]
    if any(s.get("status") == "Discharging" for s in batteries):
        return PowerSource.BATTERY
    if mains:
        return PowerSource.BATTERY
    if batteries and all(
        s.get("status") in {"Charging", "Full", "Not charging"} for s in batteries
    ):
        return PowerSource.AC
    return PowerSource.UNKNOWN


def zone_from_link(target: str | None) -> str | None:
    """``/var/db/timezone/zoneinfo/Europe/Sofia`` → ``Europe/Sofia``."""
    if not target:
        return None
    _, sep, rest = target.partition("zoneinfo/")
    if not sep:
        return None
    for prefix in ("posix/", "right/"):
        rest = rest.removeprefix(prefix)
    return rest or None


# -- host probes ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NetworkFacts:
    """The cheap, local view of the network, and the fingerprint the public verdict keys on."""

    gateway: str | None = None
    interface: str | None = None
    gateway_mac: str | None = None
    egress_interfaces: tuple[str, ...] = ()
    """Interfaces the routing table picks for a public IPv4 and IPv6 address."""
    vpn_routes: tuple[str, ...] = ()
    proxies: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.gateway,
            self.interface,
            self.gateway_mac,
            self.egress_interfaces,
            self.vpn_routes,
            self.proxies,
        )


class HostProbes(Protocol):
    def network(self) -> NetworkFacts: ...

    def power(self) -> PowerSource: ...

    def os_timezone(self) -> str | None: ...


def _env_proxies(environ: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(name for name in PROXY_ENV if environ.get(name) or environ.get(name.upper()))


def _zone_from_env_or_link(
    environ: Mapping[str, str], readlink: Callable[[str], str]
) -> tuple[str | None, bool]:
    """``(zone, from_env)``: ``TZ`` wins (Chrome inherits it); else ``/etc/localtime``."""
    tz = environ.get("TZ")
    if tz:
        return tz.removeprefix(":").strip() or None, True
    try:
        return zone_from_link(readlink("/etc/localtime")), False
    except OSError:
        return None, False


class MacProbes:
    ROUTE: Final = "/sbin/route"
    ARP: Final = "/usr/sbin/arp"
    NETSTAT: Final = "/usr/sbin/netstat"
    SCUTIL: Final = "/usr/sbin/scutil"
    PMSET: Final = "/usr/bin/pmset"

    def __init__(
        self,
        runner: Runner = run_command,
        *,
        environ: Mapping[str, str] | None = None,
        readlink: Callable[[str], str] = os.readlink,
    ) -> None:
        self._run = runner
        self._environ = environ if environ is not None else os.environ
        self._readlink = readlink

    def _text(self, args: Sequence[str], errors: list[str]) -> str:
        result: CommandResult = self._run(args)
        if result.returncode != 0:
            errors.append(f"{Path(args[0]).name} exited {result.returncode}")
        return result.stdout

    def network(self) -> NetworkFacts:
        errors: list[str] = []
        default = parse_route_get(self._text([self.ROUTE, "-n", "get", "default"], errors))
        gateway = default.gateway if default else None
        mac = None
        if gateway:
            mac = parse_arp(self._text([self.ARP, "-n", gateway], errors))
        egress = []
        for args in (
            [self.ROUTE, "-n", "get", PROBE_V4],
            [self.ROUTE, "-n", "get", "-inet6", PROBE_V6],
        ):
            # "not in table" is a normal answer for IPv6 on a v4-only network: no error.
            route = parse_route_get(self._run(args).stdout)
            if route and route.interface:
                egress.append(route.interface)
        routes = parse_netstat(self._text([self.NETSTAT, "-rn"], errors))
        proxies = parse_scutil_proxy(self._text([self.SCUTIL, "--proxy"], errors))
        return NetworkFacts(
            gateway=gateway,
            interface=default.interface if default else None,
            gateway_mac=mac,
            egress_interfaces=tuple(egress),
            vpn_routes=vpn_routes(routes),
            proxies=proxies + _env_proxies(self._environ),
            errors=tuple(errors),
        )

    def power(self) -> PowerSource:
        result = self._run([self.PMSET, "-g", "batt"])
        return parse_pmset(result.stdout) if result.returncode == 0 else PowerSource.UNKNOWN

    def os_timezone(self) -> str | None:
        zone, _ = _zone_from_env_or_link(self._environ, self._readlink)
        return zone


class LinuxProbes:
    def __init__(
        self,
        runner: Runner = run_command,
        *,
        environ: Mapping[str, str] | None = None,
        readlink: Callable[[str], str] = os.readlink,
        read_text: Callable[[Path], str] | None = None,
        power_dir: Path = Path("/sys/class/power_supply"),
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self._run = runner
        self._environ = environ if environ is not None else os.environ
        self._readlink = readlink
        self._read = read_text or (lambda p: p.read_text(encoding="utf-8"))
        self._power_dir = power_dir
        find = which or (lambda name: shutil.which(name, path=SYSTEM_PATH))
        self._ip = find("ip") or "/usr/sbin/ip"
        self._gsettings = find("gsettings")

    def _text(self, args: Sequence[str], errors: list[str]) -> str:
        result = self._run(args)
        if result.returncode != 0:
            errors.append(f"{Path(args[0]).name} exited {result.returncode}")
        return result.stdout

    def network(self) -> NetworkFacts:
        errors: list[str] = []
        default = parse_ip_route(self._text([self._ip, "route", "show", "default"], errors))
        gateway = default.gateway if default else None
        mac = None
        if gateway:
            mac = parse_ip_neigh(self._text([self._ip, "neigh", "show", gateway], errors))
        egress = []
        vpn = []
        # "ip route get" follows policy routing, so wg-quick's fwmark table is seen here even
        # though the main table's default still points at the physical interface.
        for args in (
            [self._ip, "route", "get", PROBE_V4],
            [self._ip, "-6", "route", "get", PROBE_V6],
        ):
            route = parse_ip_route(self._run(args).stdout)
            if route and route.interface:
                egress.append(route.interface)
                if VPN_INTERFACE.match(route.interface):
                    vpn.append(f"{args[-1]} via {route.interface}")
        if default and default.interface and VPN_INTERFACE.match(default.interface):
            vpn.append(f"default via {default.interface}")
        return NetworkFacts(
            gateway=gateway,
            interface=default.interface if default else None,
            gateway_mac=mac,
            egress_interfaces=tuple(egress),
            vpn_routes=tuple(dict.fromkeys(vpn)),
            proxies=self._proxies(),
            errors=tuple(errors),
        )

    def _proxies(self) -> tuple[str, ...]:
        found = list(_env_proxies(self._environ))
        if self._gsettings:
            result = self._run([self._gsettings, "get", "org.gnome.system.proxy", "mode"])
            mode = parse_gsettings_mode(result.stdout) if result.returncode == 0 else None
            if mode not in {None, "none"}:
                found.append(f"gnome:{mode}")
        return tuple(found)

    def power(self) -> PowerSource:
        supplies: dict[str, dict[str, str]] = {}
        try:
            entries = sorted(self._power_dir.iterdir()) if self._power_dir.is_dir() else []
        except OSError:
            return PowerSource.UNKNOWN
        for entry in entries:
            values = {}
            for name in ("type", "online", "status"):
                with contextlib.suppress(OSError):
                    values[name] = self._read(entry / name).strip()
            supplies[entry.name] = values
        return linux_power(supplies)

    def os_timezone(self) -> str | None:
        zone, from_env = _zone_from_env_or_link(self._environ, self._readlink)
        if zone or from_env:
            return zone
        with contextlib.suppress(OSError):
            return self._read(Path("/etc/timezone")).strip() or None
        return None


def host_probes(platform: str | None = None, runner: Runner = run_command) -> HostProbes:
    system = platform or sys.platform
    if system == "darwin":
        return MacProbes(runner)
    if system.startswith("linux"):
        return LinuxProbes(runner)
    msg = f"platform {system!r} is not supported"
    raise NotImplementedError(msg)


# -- public egress ----------------------------------------------------------------------------


class Family(enum.IntEnum):
    V4 = 4
    V6 = 6


class EgressLookup(Protocol):
    """Where this machine's traffic leaves for the internet, and whose network that is."""

    async def address(self, family: Family) -> str | None:
        """The public address in ``family``; ``None`` when that family has no connectivity."""
        ...

    async def asn(self, address: str) -> int | None: ...


class EgressLookupError(Exception):
    """A lookup service answered something unusable (distinct from "no connectivity")."""


class IpifyLookup:
    """ipify for the address (one host per family) and ipinfo for the ASN; HTTPS only.

    Never used in tests: they inject a fake ``EgressLookup`` or a mock transport.
    """

    V4_URL: Final = "https://api.ipify.org"
    V6_URL: Final = "https://api6.ipify.org"
    ASN_URL: Final = "https://ipinfo.io/{address}/json"

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        transport: Callable[[Family | None], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport or _bound_transport

    def _client(self, family: Family | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport(family),
            timeout=self._timeout,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept": "application/json, text/plain"},
        )

    async def address(self, family: Family) -> str | None:
        url = self.V4_URL if family is Family.V4 else self.V6_URL
        try:
            async with self._client(family) as client:
                response = await client.get(url)
        except httpx.ConnectError:
            return None  # no route in this family: that is an answer, not a failure
        if response.status_code != 200:
            raise EgressLookupError(f"{url} answered {response.status_code}")
        try:
            address = ipaddress.ip_address(response.text.strip())
        except ValueError as exc:
            raise EgressLookupError(f"{url} did not return an address") from exc
        if address.version != family.value:
            raise EgressLookupError(f"{url} returned an IPv{address.version} address")
        return str(address)

    async def asn(self, address: str) -> int | None:
        ip = ipaddress.ip_address(address)
        async with self._client(None) as client:
            response = await client.get(self.ASN_URL.format(address=ip))
        if response.status_code != 200:
            raise EgressLookupError(f"ASN lookup answered {response.status_code}")
        return parse_asn(response.json())


def parse_asn(data: Any) -> int | None:
    """``{"org": "AS8866 Vivacom"}`` or ``{"asn": {"asn": "AS8866"}}`` → 8866."""
    if not isinstance(data, Mapping):
        return None
    candidates = [data.get("org")]
    nested = data.get("asn")
    candidates.append(nested.get("asn") if isinstance(nested, Mapping) else nested)
    for value in candidates:
        if isinstance(value, str):
            match = re.match(r"^AS(\d+)\b", value.strip(), re.IGNORECASE)
            if match:
                return int(match.group(1))
    return None


def _bound_transport(family: Family | None) -> httpx.AsyncBaseTransport:
    if family is None:
        return httpx.AsyncHTTPTransport(retries=0)
    local = "0.0.0.0" if family is Family.V4 else "::"  # noqa: S104 - binds the client side only
    return httpx.AsyncHTTPTransport(local_address=local, retries=0)


# -- the home pin and guard -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HomePin:
    """What ``fbcollector pin-home`` recorded once, at home."""

    router_mac: str
    asn_v4: int
    asn_v6: int | None
    timezone: str
    pinned_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> HomePin:
        data = json.loads(raw)
        mac = normalize_mac(data["router_mac"])
        if mac is None:
            msg = "the pinned router MAC is malformed"
            raise ValueError(msg)
        return cls(
            router_mac=mac,
            asn_v4=int(data["asn_v4"]),
            asn_v6=None if data.get("asn_v6") is None else int(data["asn_v6"]),
            timezone=str(data["timezone"]),
            pinned_at=float(data["pinned_at"]),
        )


class HomeError(Exception):
    """The home network cannot be verified (or pinned)."""


def local_problems(facts: NetworkFacts, pin: HomePin) -> list[str]:
    """Everything the cheap checks object to (empty: looks like home)."""
    problems = []
    if facts.gateway_mac is None:
        problems.append("the default gateway's MAC is unknown")
    elif facts.gateway_mac != pin.router_mac:
        problems.append("the default gateway is not the pinned home router")
    return problems + route_problems(facts)


def route_problems(facts: NetworkFacts) -> list[str]:
    """VPN routes and proxies: wrong wherever the machine is."""
    problems = []
    if facts.interface and VPN_INTERFACE.match(facts.interface):
        problems.append(f"the default route goes through {facts.interface}")
    vpn_egress = [i for i in facts.egress_interfaces if VPN_INTERFACE.match(i)]
    if vpn_egress:
        problems.append(f"public traffic is routed through {', '.join(vpn_egress)}")
    if facts.vpn_routes:
        problems.append(f"VPN routes: {', '.join(facts.vpn_routes)}")
    if facts.proxies:
        problems.append(f"a system proxy is set ({', '.join(facts.proxies)})")
    return problems


@dataclass(slots=True)
class _Verdict:
    ok: bool
    reason: str
    at: float
    fingerprint: tuple[object, ...]


async def capture_home_pin(
    probes: HostProbes, lookup: EgressLookup, *, timezone: str, now: float
) -> HomePin:
    """Read the facts ``pin-home`` records. Refuses anything that already looks not-home."""
    facts = await asyncio.to_thread(probes.network)
    if facts.gateway_mac is None:
        raise HomeError("cannot read the default gateway's MAC")
    problems = route_problems(facts)
    if problems:
        raise HomeError("; ".join(problems))
    v4 = await lookup.address(Family.V4)
    if v4 is None:
        raise HomeError("no IPv4 egress")
    asn_v4 = await lookup.asn(v4)
    if asn_v4 is None:
        raise HomeError("the IPv4 egress has no ASN")
    v6 = await lookup.address(Family.V6)
    asn_v6 = await lookup.asn(v6) if v6 else None
    return HomePin(facts.gateway_mac, asn_v4, asn_v6, timezone, now)


class HomeGuard:
    """The home gate: cheap checks every time, the public verdict at most every 10 minutes."""

    name = "home"

    def __init__(
        self,
        probes: HostProbes,
        lookup: EgressLookup,
        pin: Callable[[], Awaitable[HomePin | None]],
        clock: Clock,
        *,
        ttl: float = PUBLIC_VERDICT_TTL,
        failure_ttl: float = PUBLIC_FAILURE_TTL,
    ) -> None:
        self._probes = probes
        self._lookup = lookup
        self._pin = pin
        self._clock = clock
        self._ttl = ttl
        self._failure_ttl = failure_ttl
        self._verdict: _Verdict | None = None
        self.lookups = 0

    def invalidate(self) -> None:
        """Forget the public verdict (sleep, or anything else that may have moved us)."""
        self._verdict = None

    async def check(self, context: GateContext) -> GateResult:  # noqa: ARG002 - one rule for all
        pin = await self._pin()
        if pin is None:
            return self._closed("home is not pinned (run fbcollector pin-home at home)", None)
        facts = await asyncio.to_thread(self._probes.network)
        problems = local_problems(facts, pin)
        if problems:
            self.invalidate()
            return self._closed("; ".join(problems), self._clock.time() + 60)
        now = self._clock.time()
        verdict = self._verdict
        fresh = (
            verdict is not None
            and verdict.fingerprint == facts.fingerprint
            and 0 <= now - verdict.at < (self._ttl if verdict.ok else self._failure_ttl)
        )
        if not fresh:
            verdict = await self._public(pin, facts, now)
            self._verdict = verdict
        assert verdict is not None  # noqa: S101 - set just above
        if verdict.ok:
            return GateResult(self.name, ok=True)
        return self._closed(verdict.reason, now + self._failure_ttl)

    def _closed(self, reason: str, retry_at: float | None) -> GateResult:
        return GateResult(
            self.name, ok=False, reason=reason, state=CollectorState.NOT_HOME, retry_at=retry_at
        )

    async def _public(self, pin: HomePin, facts: NetworkFacts, now: float) -> _Verdict:
        self.lookups += 1
        try:
            reason = await self._public_problem(pin)
        except (httpx.HTTPError, EgressLookupError, ValueError) as exc:
            reason = f"public network lookup failed ({type(exc).__name__})"
        return _Verdict(
            ok=reason is None, reason=reason or "", at=now, fingerprint=facts.fingerprint
        )

    async def _public_problem(self, pin: HomePin) -> str | None:
        v4 = await self._lookup.address(Family.V4)
        if v4 is None:
            return "no IPv4 egress"
        asn_v4 = await self._lookup.asn(v4)
        if asn_v4 != pin.asn_v4:
            return f"IPv4 egress is in AS{asn_v4}, not the pinned AS{pin.asn_v4}"
        v6 = await self._lookup.address(Family.V6)
        if v6 is None:
            return None  # without IPv6, Chrome's traffic is the IPv4 just checked
        asn_v6 = await self._lookup.asn(v6)
        if pin.asn_v6 is None:
            return f"IPv6 egress (AS{asn_v6}) exists but none was pinned"
        if asn_v6 != pin.asn_v6:
            return f"IPv6 egress is in AS{asn_v6}, not the pinned AS{pin.asn_v6}"
        return None


# -- other gates ------------------------------------------------------------------------------


class SleepNotices(Protocol):
    """What the OS said about sleep (``fbcollector.sleephook.SleepNotice``)."""

    @property
    def pending(self) -> bool: ...

    def take_wake(self) -> bool: ...

    def settle(self) -> None: ...


class SleepGate:
    """Wall vs monotonic divergence > 30 s between two checks: the machine slept.

    Closes exactly once per detected sleep, so the caller aborts the read and closes Chrome;
    the listeners (the home guard) forget whatever they had verified before it. The first mark
    is taken when the gate is built, so a sleep before its first check counts too.

    With an OS hook (``notices``): closed from the moment the OS announces a sleep until it
    reports the wake, and once more at the wake itself, listeners called — so the pass after
    that one re-verifies the network before anything opens. The divergence stays the backstop
    for a hook that is missing, late or silent.
    """

    name = "sleep"

    def __init__(
        self,
        clock: Clock,
        *,
        threshold: float = SUSPEND_THRESHOLD,
        on_sleep: Sequence[Callable[[], None]] = (),
        notices: SleepNotices | None = None,
    ) -> None:
        self._clock = clock
        self._threshold = threshold
        self._on_sleep = tuple(on_sleep)
        self._notices = notices
        self._last: Mark | None = Mark.now(clock)

    async def check(self, context: GateContext) -> GateResult:  # noqa: ARG002 - one rule for all
        now = Mark.now(self._clock)
        last, self._last = self._last, now
        notices = self._notices
        if last is not None and last.slept_before(now, self._threshold):
            if notices is not None:
                notices.settle()  # the wake message, if it still comes, is owed nothing
            return self._woke(
                now, f"clocks diverged by {last.divergence(now):.0f} s (sleep or clock step)"
            )
        if notices is None:
            return GateResult(self.name, ok=True)
        if notices.pending:
            self._forget()
            return GateResult(self.name, ok=False, reason="the OS announced a sleep")
        if notices.take_wake():
            return self._woke(now, "woke from sleep (the OS said so)")
        return GateResult(self.name, ok=True)

    def _forget(self) -> None:
        for listener in self._on_sleep:
            listener()

    def _woke(self, now: Mark, reason: str) -> GateResult:
        self._forget()
        return GateResult(self.name, ok=False, reason=reason, retry_at=now.wall)


class TimezoneGate:
    """The OS zone, the configured zone and the zone ``pin-home`` recorded must all agree.

    ``home_zone`` is the pin's zone, or a loader for it (read at every check, so a later
    ``pin-home`` counts): a changed ``FBC_TIMEZONE`` must not move quiet hours to another zone
    behind the pin's back.
    """

    name = "timezone"

    def __init__(
        self,
        os_zone: Callable[[], str | None],
        pinned: str,
        *,
        home_zone: str | Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self._os_zone = os_zone
        self._pinned = pinned
        self._home_zone = home_zone

    async def check(self, context: GateContext) -> GateResult:  # noqa: ARG002 - one rule for all
        zone = await asyncio.to_thread(self._os_zone)
        home = self._home_zone
        if callable(home):
            home = await home()
        wanted = {self._pinned} | ({home} if home else set())
        if len(wanted) > 1:
            return self._closed(f"the pinned zone {home} differs from {self._pinned}")
        if zone != self._pinned:
            return self._closed(f"the OS timezone is {zone or 'unknown'}, not {self._pinned}")
        return GateResult(self.name, ok=True)

    def _closed(self, reason: str) -> GateResult:
        return GateResult(self.name, ok=False, reason=reason, state=CollectorState.NOT_HOME)


class PowerGate:
    name = "power"

    def __init__(self, probe: Callable[[], PowerSource], clock: Clock) -> None:
        self._probe = probe
        self._clock = clock

    async def check(self, context: GateContext) -> GateResult:  # noqa: ARG002 - one rule for all
        source = await asyncio.to_thread(self._probe)
        if source is PowerSource.AC:
            return GateResult(self.name, ok=True)
        return GateResult(
            self.name,
            ok=False,
            reason="on battery" if source is PowerSource.BATTERY else "power source unknown",
            state=CollectorState.NOT_HOME,
            retry_at=self._clock.time() + 60,
        )


class ProcessLedger(Protocol):
    async def process_events_since(
        self,
        window: float,
        *,
        kinds: Collection[ProcessKind] | None = None,
        now: float | None = None,
        planned: bool | None = False,
    ) -> int: ...


PROCESS_NOUNS: Final = {"process_start": "process starts", "chrome_launch": "chrome launches"}
"""What a crash-loop reason calls each ``ProcessKind`` (by value), in the plural."""


class CrashLoopGate:
    """More than 3 unplanned process starts or Chrome launches in an hour, or 8 in a day: stop.

    Planned ones do not count there: a restart someone asked for (SIGTERM from ``launchctl
    kickstart -k``, an update) and a relaunch after the collector closed Chrome itself (quiet
    hours, a break, an announced sleep, a closed gate). The planned lifecycle alone passes 8 a
    day. More than 10 in an hour or 40 in a day of one kind, planned or not, still stop it: a
    gate that flaps would otherwise close and relaunch Chrome for ever.
    """

    name = "crash_loop"

    def __init__(
        self,
        ledger: ProcessLedger,
        clock: Clock,
        *,
        per_hour: int = 3,
        per_day: int = 8,
        all_per_hour: int = 10,
        all_per_day: int = 40,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._per_hour = per_hour
        self._per_day = per_day
        self._all_per_hour = all_per_hour
        self._all_per_day = all_per_day

    async def check(self, context: GateContext) -> GateResult:
        from fbcollector.store import ProcessKind  # noqa: PLC0415 - store imports aiosqlite

        now = self._clock.time()
        launching = context.activity is Activity.LAUNCH
        for kind in ProcessKind:
            # The launches this check guards count too: the 4th unplanned launch in an hour is
            # refused, and so is a self-test with its read's launch when only one is left.
            pending = context.launches if launching and kind is ProcessKind.CHROME_LAUNCH else 0
            noun = PROCESS_NOUNS[kind.value]
            for window, label, limit, all_limit in (
                (HOUR, "hour", self._per_hour, self._all_per_hour),
                (DAY, "day", self._per_day, self._all_per_day),
            ):
                unplanned = await self._ledger.process_events_since(window, kinds=[kind], now=now)
                unplanned += 0 if context.planned else pending
                if unplanned > limit:
                    reason = f"{unplanned} unplanned {noun} in the last {label}"
                    return self._closed(reason, now, window)
                every = await self._ledger.process_events_since(
                    window, kinds=[kind], now=now, planned=None
                )
                if every + pending > all_limit:
                    reason = f"{every + pending} {noun} (planned or not) in the last {label}"
                    return self._closed(reason, now, window)
        return GateResult(self.name, ok=True)

    def _closed(self, reason: str, now: float, window: float) -> GateResult:
        return GateResult(
            self.name,
            ok=False,
            reason=reason,
            state=CollectorState.CRASH_LOOP,
            retry_at=now + (HOUR if window == HOUR else 6 * HOUR) / 4,
        )


class LoginLedger(Protocol):
    async def kv_get(self, key: str) -> str | None: ...


class WarmupGate:
    """No automated reads for 24-72 h after a login (drawn once per login, from the seed)."""

    name = "warmup"

    def __init__(
        self,
        store: LoginLedger,
        clock: Clock,
        jitter: Jitter,
        *,
        min_hours: float = 24,
        max_hours: float = 72,
    ) -> None:
        self._store = store
        self._clock = clock
        self._jitter = jitter
        self._min = min_hours
        self._max = max_hours

    async def ends_at(self) -> float | None:
        raw = await self._store.kv_get(KV_LOGIN_AT)
        if raw is None:
            return None
        login_at = float(raw)
        return login_at + HOUR * self._jitter.uniform(self._min, self._max, "warmup", login_at)

    async def check(self, context: GateContext) -> GateResult:  # noqa: ARG002 - one rule for all
        ends = await self.ends_at()
        if ends is None:
            return GateResult(
                self.name,
                ok=False,
                reason="no login recorded (run fbcollector login)",
                state=CollectorState.NEEDS_LOGIN,
            )
        if self._clock.time() < ends:
            return GateResult(
                self.name,
                ok=False,
                reason="warming up after the login",
                state=CollectorState.WARMUP,
                retry_at=ends,
            )
        return GateResult(self.name, ok=True)


# -- the profile lock -------------------------------------------------------------------------


class ProfileLockedError(Exception):
    """Another process holds the collector's profile lock."""


class ProfileLock:
    """An exclusive ``flock`` on ``<root>/collector.lock`` for the life of the process.

    ``flock`` is released by the kernel when the process dies, so a crash never leaves a stale
    lock behind (unlike a pid file); the pid written inside is for humans only.
    """

    name = "lock"

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            msg = f"{self._path} is held by another collector process"
            raise ProfileLockedError(msg) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> ProfileLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    async def check(self, context: GateContext) -> GateResult:  # noqa: ARG002 - one rule for all
        if self.held:
            return GateResult(self.name, ok=True)
        return GateResult(self.name, ok=False, reason="the profile lock is not held")


class SingletonState(enum.StrEnum):
    ABSENT = "absent"
    STALE = "stale"  # this host, dead pid: safe to remove
    LIVE = "live"  # another Chrome owns the profile
    FOREIGN = "foreign"  # written on another host: never touch
    UNREADABLE = "unreadable"


SINGLETON_FILES: Final = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


@dataclass(frozen=True, slots=True)
class SingletonCheck:
    state: SingletonState
    host: str | None = None
    pid: int | None = None


def singleton_state(
    profile: Path,
    *,
    hostname: str | None = None,
    alive: Callable[[int], bool] = pid_alive,
) -> SingletonCheck:
    """Chrome's ``SingletonLock`` is a symlink to ``<hostname>-<pid>``; read who owns it."""
    lock = profile / "SingletonLock"
    try:
        target = str(lock.readlink())
    except FileNotFoundError:
        return SingletonCheck(SingletonState.ABSENT)
    except OSError:
        return SingletonCheck(SingletonState.UNREADABLE)
    host, sep, pid_text = target.rpartition("-")
    if not sep or not pid_text.isdigit():
        return SingletonCheck(SingletonState.UNREADABLE)
    pid = int(pid_text)
    if host != (hostname or socket.gethostname()):
        return SingletonCheck(SingletonState.FOREIGN, host, pid)
    state = SingletonState.LIVE if alive(pid) else SingletonState.STALE
    return SingletonCheck(state, host, pid)


def clear_stale_singleton(
    profile: Path,
    *,
    hostname: str | None = None,
    alive: Callable[[int], bool] = pid_alive,
) -> SingletonCheck:
    """Remove Chrome's singleton links if their owner is dead on this host; else leave them.

    Raises ``ProfileLockedError`` for a live or foreign owner: another Chrome has the profile.
    """
    check = singleton_state(profile, hostname=hostname, alive=alive)
    if check.state in {SingletonState.LIVE, SingletonState.FOREIGN, SingletonState.UNREADABLE}:
        msg = f"Chrome's profile lock is {check.state.value}; another Chrome may own the profile"
        raise ProfileLockedError(msg)
    if check.state is SingletonState.STALE:
        for name in SINGLETON_FILES:
            with contextlib.suppress(FileNotFoundError):
                (profile / name).unlink()
        log.info("removed a stale Chrome SingletonLock (pid %s)", check.pid)
    return check
