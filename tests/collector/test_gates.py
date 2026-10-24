"""Gates. ``system_fixtures/macos_*.txt`` are real outputs of the read-only commands captured on
the owner's Mac (2026-09-25) with MACs and addresses replaced; ``synthetic_*`` files are
hand-written in the same formats for states that Mac was not in (a VPN, a proxy, battery power,
Linux)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from fbcollector.clock import FakeClock
from fbcollector.gates import (
    Activity,
    CrashLoopGate,
    EgressLookupError,
    Family,
    FuncGate,
    GateContext,
    GateReport,
    GateResult,
    GateSet,
    HomeError,
    HomeGuard,
    HomePin,
    IpifyLookup,
    LinuxProbes,
    MacProbes,
    PowerGate,
    PowerSource,
    ProfileLock,
    ProfileLockedError,
    RouteEntry,
    SingletonState,
    SleepGate,
    TimezoneGate,
    WarmupGate,
    capture_home_pin,
    clear_stale_singleton,
    host_probes,
    linux_power,
    normalize_mac,
    parse_arp,
    parse_asn,
    parse_gsettings_mode,
    parse_ip_neigh,
    parse_ip_route,
    parse_netstat,
    parse_pmset,
    parse_route_get,
    parse_scutil_proxy,
    pid_alive,
    singleton_state,
    vpn_routes,
    zone_from_link,
)
from fbcollector.model import CollectorState
from fbcollector.paths import CommandResult
from fbcollector.rhythm import Jitter
from fbcollector.store import ProcessKind, Store

from .gate_fakes import (
    HOME_ASN,
    HOME_FACTS,
    HOME_PIN,
    HOME_V4,
    ROUTER_MAC,
    FakeLookup,
    FakeProbes,
)
from .pacing_support import ZONE

FIXTURES = Path(__file__).parent / "system_fixtures"
NAV = GateContext(Activity.NAVIGATE)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeRunner:
    """Answers commands from a table; anything unexpected is a test failure."""

    def __init__(self, table: dict[tuple[str, ...], CommandResult]) -> None:
        self.table = table
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> CommandResult:
        key = tuple(args)
        self.calls.append(key)
        if key not in self.table:
            raise AssertionError(f"unexpected command {key}")
        return self.table[key]


def ok(text: str) -> CommandResult:
    return CommandResult(0, text, "")


def mac_table(**over: CommandResult) -> dict[tuple[str, ...], CommandResult]:
    table = {
        ("/sbin/route", "-n", "get", "default"): ok(fixture("macos_route_get_default.txt")),
        ("/usr/sbin/arp", "-n", "192.0.2.1"): ok(fixture("macos_arp.txt")),
        ("/sbin/route", "-n", "get", "1.1.1.1"): ok(fixture("macos_route_get_public4.txt")),
        ("/sbin/route", "-n", "get", "-inet6", "2606:4700:4700::1111"): ok(
            fixture("macos_route_get_public6_none.txt")
        ),
        ("/usr/sbin/netstat", "-rn"): ok(fixture("macos_netstat_rn.txt")),
        ("/usr/sbin/scutil", "--proxy"): ok(fixture("macos_scutil_proxy_none.txt")),
        ("/usr/bin/pmset", "-g", "batt"): ok(fixture("macos_pmset_ac.txt")),
    }
    table.update({tuple(k.split("|")): v for k, v in over.items()})
    return table


# -- parsers on real and synthetic outputs ----------------------------------------------------


class TestMacParsers:
    def test_route_get(self) -> None:
        route = parse_route_get(fixture("macos_route_get_default.txt"))
        assert route is not None
        assert (route.gateway, route.interface) == ("192.0.2.1", "en0")
        assert parse_route_get(fixture("macos_route_get_public6_none.txt")) is None

    def test_arp_keeps_macos_short_octets(self) -> None:
        assert parse_arp(fixture("macos_arp.txt")) == ROUTER_MAC
        assert parse_arp("? (192.0.2.1) at (incomplete) on en0") is None

    def test_scoped_utun_defaults_are_not_a_vpn(self) -> None:
        entries = parse_netstat(fixture("macos_netstat_rn.txt"))
        assert any(e.interface == "utun13" and e.destination == "default" for e in entries)
        assert {e.family for e in entries} == {4, 6}
        assert vpn_routes(entries) == ()

    def test_a_full_tunnel_vpn_is(self) -> None:
        found = vpn_routes(parse_netstat(fixture("synthetic_macos_netstat_full_tunnel.txt")))
        assert found == ("0/1 via utun4", "128.0/1 via utun4", "::/1 via utun4")
        plain = [RouteEntry("default", "10.8.0.1", "UGSc", "wg0", 4)]
        assert vpn_routes(plain) == ("default via wg0",)

    def test_proxy(self) -> None:
        assert parse_scutil_proxy(fixture("macos_scutil_proxy_none.txt")) == ()
        assert parse_scutil_proxy(fixture("synthetic_macos_scutil_proxy_on.txt")) == (
            "HTTPEnable",
            "ProxyAutoConfigEnable",
        )

    def test_power(self) -> None:
        assert parse_pmset(fixture("macos_pmset_ac.txt")) is PowerSource.AC
        assert parse_pmset(fixture("synthetic_macos_pmset_battery.txt")) is PowerSource.BATTERY
        assert parse_pmset("Now drawing from 'UPS Power'") is PowerSource.UNKNOWN
        assert parse_pmset("") is PowerSource.UNKNOWN

    @pytest.mark.parametrize(
        ("raw", "mac"),
        [
            ("2:0:5e:10:0:1", "02:00:5e:10:00:01"),
            ("02-00-5E-10-00-01", "02:00:5e:10:00:01"),
            ("ff:ff:ff:ff:ff:ff", None),
            ("0:0:0:0:0:0", None),
            ("02:00:5e:10:00", None),
            ("zz:00:5e:10:00:01", None),
            ("", None),
            (None, None),
        ],
    )
    def test_normalize_mac(self, raw: str | None, mac: str | None) -> None:
        assert normalize_mac(raw) == mac

    @pytest.mark.parametrize(
        ("target", "zone"),
        [
            ("/var/db/timezone/zoneinfo/Europe/Sofia", "Europe/Sofia"),
            ("../usr/share/zoneinfo/Europe/Kyiv", "Europe/Kyiv"),
            ("/usr/share/zoneinfo/posix/Europe/Sofia", "Europe/Sofia"),
            ("/usr/share/zoneinfo/", None),
            ("/etc/somewhere", None),
            (None, None),
        ],
    )
    def test_zone_from_link(self, target: str | None, zone: str | None) -> None:
        assert zone_from_link(target) == zone


class TestLinuxParsers:
    def test_ip_route(self) -> None:
        got = parse_ip_route(fixture("synthetic_linux_ip_route_get.txt"))
        assert got is not None
        assert (got.gateway, got.interface) == ("192.0.2.1", "wlp2s0")
        wg = parse_ip_route(fixture("synthetic_linux_ip_route_get_wg.txt"))
        assert wg is not None
        assert (wg.gateway, wg.interface) == (None, "wg0")
        assert parse_ip_route("") is None

    def test_ip_neigh(self) -> None:
        assert parse_ip_neigh(fixture("synthetic_linux_ip_neigh.txt")) == ROUTER_MAC
        assert parse_ip_neigh("192.0.2.1 dev wlp2s0 lladdr 02:00:5e:10:00:01 FAILED") is None
        assert parse_ip_neigh("192.0.2.1 dev wlp2s0 INCOMPLETE") is None

    def test_gsettings(self) -> None:
        assert parse_gsettings_mode(fixture("synthetic_linux_gsettings_none.txt")) == "none"
        assert parse_gsettings_mode("") is None

    @pytest.mark.parametrize(
        ("supplies", "source"),
        [
            ({}, PowerSource.AC),
            ({"AC": {"type": "Mains", "online": "1"}}, PowerSource.AC),
            (
                {
                    "AC": {"type": "Mains", "online": "0"},
                    "BAT0": {"type": "Battery", "status": "Full"},
                },
                PowerSource.BATTERY,
            ),
            ({"BAT0": {"type": "Battery", "status": "Discharging"}}, PowerSource.BATTERY),
            ({"BAT0": {"type": "Battery", "status": "Charging"}}, PowerSource.AC),
            ({"BAT0": {"type": "Battery", "status": "Unknown"}}, PowerSource.UNKNOWN),
        ],
    )
    def test_power_supply(self, supplies: dict[str, dict[str, str]], source: PowerSource) -> None:
        assert linux_power(supplies) is source

    @pytest.mark.parametrize(
        ("data", "asn"),
        [
            ({"org": "AS8866 Vivacom"}, 8866),
            ({"asn": {"asn": "AS1234", "name": "x"}}, 1234),
            ({"asn": "as42"}, 42),
            ({"org": "Vivacom"}, None),
            ([], None),
        ],
    )
    def test_parse_asn(self, data: object, asn: int | None) -> None:
        assert parse_asn(data) == asn


# -- probes over a fake runner ----------------------------------------------------------------


class TestMacProbes:
    def test_this_macs_real_state_is_home_shaped(self) -> None:
        runner = FakeRunner(mac_table())
        probes = MacProbes(
            runner, environ={}, readlink=lambda _: "/var/db/timezone/zoneinfo/Europe/Sofia"
        )
        facts = probes.network()
        assert facts.gateway == "192.0.2.1"
        assert facts.interface == "en0"
        assert facts.gateway_mac == ROUTER_MAC
        assert facts.egress_interfaces == ("en0",)
        assert facts.vpn_routes == ()
        assert facts.proxies == ()
        assert facts.errors == ()
        assert probes.power() is PowerSource.AC
        assert probes.os_timezone() == "Europe/Sofia"

    def test_vpn_proxy_env_and_failures(self) -> None:
        runner = FakeRunner(
            mac_table(
                **{
                    "/usr/sbin/netstat|-rn": ok(fixture("synthetic_macos_netstat_full_tunnel.txt")),
                    "/usr/sbin/scutil|--proxy": CommandResult(1, "", "boom"),
                    "/usr/bin/pmset|-g|batt": CommandResult(1, "", ""),
                }
            )
        )
        probes = MacProbes(runner, environ={"HTTPS_PROXY": "http://192.0.2.9:3128"})
        facts = probes.network()
        assert "0/1 via utun4" in facts.vpn_routes
        assert facts.proxies == ("https_proxy",)
        assert facts.errors == ("scutil exited 1",)
        assert probes.power() is PowerSource.UNKNOWN

    def test_no_default_route(self) -> None:
        runner = FakeRunner(
            mac_table(**{"/sbin/route|-n|get|default": CommandResult(1, "not in table", "")})
        )
        facts = MacProbes(runner, environ={}).network()
        assert facts.gateway is None
        assert facts.gateway_mac is None
        assert ("/usr/sbin/arp", "-n", "192.0.2.1") not in runner.calls

    def test_timezone_env_wins_and_unreadable_link(self) -> None:
        def broken(_: str) -> str:
            raise OSError("no link")

        assert MacProbes(FakeRunner({}), environ={"TZ": ":UTC"}).os_timezone() == "UTC"
        assert MacProbes(FakeRunner({}), environ={}, readlink=broken).os_timezone() is None


def linux_table(
    route_get: str = "synthetic_linux_ip_route_get.txt",
    default: str | None = None,
    mode: str = "'none'",
) -> dict[tuple[str, ...], CommandResult]:
    return {
        ("/usr/sbin/ip", "route", "show", "default"): ok(
            default or fixture("synthetic_linux_ip_route_default.txt")
        ),
        ("/usr/sbin/ip", "neigh", "show", "192.0.2.1"): ok(fixture("synthetic_linux_ip_neigh.txt")),
        ("/usr/sbin/ip", "route", "get", "1.1.1.1"): ok(fixture(route_get)),
        ("/usr/sbin/ip", "-6", "route", "get", "2606:4700:4700::1111"): CommandResult(
            2, "", "RTNETLINK answers: Network is unreachable"
        ),
        ("/usr/bin/gsettings", "get", "org.gnome.system.proxy", "mode"): ok(mode),
    }


def which(name: str) -> str | None:
    return {"ip": "/usr/sbin/ip", "gsettings": "/usr/bin/gsettings"}.get(name)


class TestLinuxProbes:
    def test_home(self, tmp_path: Path) -> None:
        probes = LinuxProbes(
            FakeRunner(linux_table()), environ={}, which=which, power_dir=tmp_path / "none"
        )
        facts = probes.network()
        assert facts.gateway_mac == ROUTER_MAC
        assert facts.egress_interfaces == ("wlp2s0",)
        assert facts.vpn_routes == ()
        assert facts.proxies == ()
        assert probes.power() is PowerSource.AC  # no supply at all: a desktop

    def test_wireguard_policy_routing_is_seen(self) -> None:
        runner = FakeRunner(linux_table(route_get="synthetic_linux_ip_route_get_wg.txt"))
        facts = LinuxProbes(runner, environ={}, which=which).network()
        assert facts.vpn_routes == ("1.1.1.1 via wg0",)
        assert facts.egress_interfaces == ("wg0",)

    def test_a_vpn_default_route_and_a_gnome_proxy(self) -> None:
        runner = FakeRunner(linux_table(default="default via 10.8.0.1 dev tun0\n", mode="'manual'"))
        runner.table[("/usr/sbin/ip", "neigh", "show", "10.8.0.1")] = ok("")
        facts = LinuxProbes(runner, environ={}, which=which).network()
        assert "default via tun0" in facts.vpn_routes
        assert facts.proxies == ("gnome:manual",)

    def test_without_gsettings_only_the_environment_counts(self) -> None:
        runner = FakeRunner(linux_table())
        probes = LinuxProbes(
            runner,
            environ={"all_proxy": "socks5://x"},
            which=lambda n: None if n == "gsettings" else which(n),
        )
        assert probes.network().proxies == ("all_proxy",)

    def test_power_supply_tree(self, tmp_path: Path) -> None:
        (tmp_path / "AC").mkdir()
        (tmp_path / "AC" / "type").write_text("Mains\n")
        (tmp_path / "AC" / "online").write_text("0\n")
        (tmp_path / "BAT0").mkdir()
        (tmp_path / "BAT0" / "type").write_text("Battery\n")
        (tmp_path / "BAT0" / "status").write_text("Discharging\n")
        probes = LinuxProbes(FakeRunner({}), environ={}, which=which, power_dir=tmp_path)
        assert probes.power() is PowerSource.BATTERY

    def test_timezone_fallbacks(self) -> None:
        def no_link(_: str) -> str:
            raise OSError("regular file")

        def read(path: Path) -> str:
            assert path == Path("/etc/timezone")
            return "Europe/Sofia\n"

        probes = LinuxProbes(
            FakeRunner({}), environ={}, readlink=no_link, read_text=read, which=which
        )
        assert probes.os_timezone() == "Europe/Sofia"

        def unreadable(_: Path) -> str:
            raise OSError("missing")

        bare = LinuxProbes(
            FakeRunner({}), environ={}, readlink=no_link, read_text=unreadable, which=which
        )
        assert bare.os_timezone() is None
        assert (
            LinuxProbes(FakeRunner({}), environ={"TZ": "UTC"}, which=which).os_timezone() == "UTC"
        )


def test_host_probes_by_platform() -> None:
    assert isinstance(host_probes("darwin"), MacProbes)
    assert isinstance(host_probes("linux"), LinuxProbes)
    with pytest.raises(NotImplementedError):
        host_probes("win32")


# -- the home guard ---------------------------------------------------------------------------


def guard(
    clock: FakeClock,
    probes: FakeProbes | None = None,
    lookup: FakeLookup | None = None,
    pin: HomePin | None = HOME_PIN,
) -> tuple[HomeGuard, FakeProbes, FakeLookup]:
    p = probes or FakeProbes()
    lk = lookup or FakeLookup()

    async def load_pin() -> HomePin | None:
        return pin

    return HomeGuard(p, lk, load_pin, clock), p, lk


class TestHomeGuard:
    async def test_home_and_the_ten_minute_verdict(self) -> None:
        clock = FakeClock()
        g, probes, lookup = guard(clock)
        assert (await g.check(NAV)).ok
        calls = lookup.calls
        clock.advance(599)
        assert (await g.check(NAV)).ok
        assert lookup.calls == calls  # cached
        clock.advance(2)
        assert (await g.check(NAV)).ok
        assert lookup.calls > calls
        assert probes.network_calls == 3  # the cheap checks run every time

    async def test_any_local_change_or_invalidation_redoes_the_verdict(self) -> None:
        clock = FakeClock()
        g, probes, _lookup = guard(clock)
        await g.check(NAV)
        before = g.lookups
        probes.facts = replace(HOME_FACTS, gateway="192.0.2.254")  # same router, new address
        assert (await g.check(NAV)).ok
        assert g.lookups == before + 1
        g.invalidate()
        await g.check(NAV)
        assert g.lookups == before + 2
        clock.jump_wall(-3600)  # a verdict from "the future" is not fresh
        await g.check(NAV)
        assert g.lookups == before + 3

    @pytest.mark.parametrize(
        ("facts", "reason"),
        [
            (replace(HOME_FACTS, gateway_mac="02:00:5e:10:00:99"), "not the pinned home router"),
            (replace(HOME_FACTS, gateway_mac=None), "MAC is unknown"),
            (replace(HOME_FACTS, interface="utun4"), "goes through utun4"),
            (replace(HOME_FACTS, egress_interfaces=("wg0",)), "routed through wg0"),
            (replace(HOME_FACTS, vpn_routes=("0/1 via utun4",)), "VPN routes"),
            (replace(HOME_FACTS, proxies=("HTTPEnable",)), "system proxy"),
        ],
    )
    async def test_local_problems_close_it_without_a_lookup(
        self, facts: object, reason: str
    ) -> None:
        g, _, lookup = guard(FakeClock(), FakeProbes(facts=facts))  # type: ignore[arg-type]
        result = await g.check(NAV)
        assert not result.ok
        assert reason in result.reason
        assert result.state is CollectorState.NOT_HOME
        assert lookup.calls == 0

    async def test_not_pinned(self) -> None:
        g, _, _ = guard(FakeClock(), pin=None)
        result = await g.check(NAV)
        assert not result.ok
        assert "pin-home" in result.reason
        assert result.retry_at is None

    async def test_public_problems(self) -> None:
        clock = FakeClock()
        lookup = FakeLookup(asns={HOME_V4: 1})
        g, _, _ = guard(clock, lookup=lookup)
        result = await g.check(NAV)
        assert "AS1, not the pinned AS8866" in result.reason
        lookup.addresses[Family.V4] = None
        g.invalidate()
        assert "no IPv4 egress" in (await g.check(NAV)).reason

    async def test_ipv6_rules(self) -> None:
        v6 = "2001:db8::7"
        lookup = FakeLookup(
            addresses={Family.V4: HOME_V4, Family.V6: v6}, asns={HOME_V4: HOME_ASN, v6: HOME_ASN}
        )
        g, _, _ = guard(FakeClock(), lookup=lookup)
        assert "none was pinned" in (await g.check(NAV)).reason
        pinned = replace(HOME_PIN, asn_v6=HOME_ASN)
        g6, _, _ = guard(FakeClock(), lookup=lookup, pin=pinned)
        assert (await g6.check(NAV)).ok
        lookup.asns[v6] = 13335
        g6.invalidate()
        assert "IPv6 egress is in AS13335" in (await g6.check(NAV)).reason
        lookup.addresses[Family.V6] = None  # v6 gone: Chrome uses the v4 just checked
        g6.invalidate()
        assert (await g6.check(NAV)).ok

    async def test_a_failed_lookup_is_closed_and_retried_after_a_minute(self) -> None:
        clock = FakeClock()
        lookup = FakeLookup(fail=httpx.ConnectTimeout("slow"))
        g, _, _ = guard(clock, lookup=lookup)
        result = await g.check(NAV)
        assert not result.ok
        assert "ConnectTimeout" in result.reason
        assert result.retry_at == pytest.approx(clock.time() + 60)
        lookup.fail = None
        clock.advance(30)
        assert not (await g.check(NAV)).ok  # still the cached failure
        clock.advance(31)
        assert (await g.check(NAV)).ok


class TestPin:
    async def test_capture(self) -> None:
        pin = await capture_home_pin(FakeProbes(), FakeLookup(), timezone=ZONE, now=5.0)
        assert pin == HomePin(ROUTER_MAC, HOME_ASN, None, ZONE, 5.0)
        assert HomePin.from_json(pin.to_json()) == pin

    @pytest.mark.parametrize(
        ("probes", "lookup", "message"),
        [
            (FakeProbes(facts=replace(HOME_FACTS, gateway_mac=None)), FakeLookup(), "MAC"),
            (
                FakeProbes(facts=replace(HOME_FACTS, vpn_routes=("0/1 via utun4",))),
                FakeLookup(),
                "VPN",
            ),
            (FakeProbes(), FakeLookup(addresses={Family.V4: None}), "no IPv4"),
            (FakeProbes(), FakeLookup(asns={}), "no ASN"),
        ],
    )
    async def test_refusals(self, probes: FakeProbes, lookup: FakeLookup, message: str) -> None:
        with pytest.raises(HomeError, match=message):
            await capture_home_pin(probes, lookup, timezone=ZONE, now=1.0)

    async def test_capture_with_ipv6(self) -> None:
        lookup = FakeLookup(
            addresses={Family.V4: HOME_V4, Family.V6: "2001:db8::1"},
            asns={HOME_V4: HOME_ASN, "2001:db8::1": HOME_ASN},
        )
        pin = await capture_home_pin(FakeProbes(), lookup, timezone=ZONE, now=1.0)
        assert pin.asn_v6 == HOME_ASN

    def test_a_malformed_pin_is_refused(self) -> None:
        raw = HOME_PIN.to_json().replace(ROUTER_MAC, "nonsense")
        with pytest.raises(ValueError, match="MAC"):
            HomePin.from_json(raw)


class TestIpify:
    def lookup(self, handler: object) -> IpifyLookup:
        transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
        return IpifyLookup(transport=lambda _family: transport)

    async def test_addresses_and_asn(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.host == "api.ipify.org":
                return httpx.Response(200, text="198.51.100.7\n")
            if request.url.host == "api6.ipify.org":
                return httpx.Response(200, text="2001:db8::7")
            return httpx.Response(200, json={"ip": "198.51.100.7", "org": "AS8866 Vivacom"})

        lookup = self.lookup(handler)
        assert await lookup.address(Family.V4) == "198.51.100.7"
        assert await lookup.address(Family.V6) == "2001:db8::7"
        assert await lookup.asn("198.51.100.7") == 8866
        assert all(url.startswith("https://") for url in seen)
        assert seen[-1] == "https://ipinfo.io/198.51.100.7/json"

    async def test_bad_answers(self) -> None:
        answers = iter(
            [
                httpx.Response(200, text="2001:db8::7"),  # v6 from the v4 host
                httpx.Response(200, text="<html>"),
                httpx.Response(503, text="busy"),
                httpx.Response(429, json={}),
            ]
        )
        lookup = self.lookup(lambda _r: next(answers))
        with pytest.raises(EgressLookupError, match="IPv6"):
            await lookup.address(Family.V4)
        with pytest.raises(EgressLookupError, match="did not return"):
            await lookup.address(Family.V4)
        with pytest.raises(EgressLookupError, match="503"):
            await lookup.address(Family.V4)
        with pytest.raises(EgressLookupError, match="429"):
            await lookup.asn("198.51.100.7")

    async def test_no_route_in_a_family_is_an_answer(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable", request=request)

        assert await self.lookup(handler).address(Family.V6) is None

    async def test_the_client_ignores_proxy_environment(self) -> None:
        lookup = IpifyLookup()
        async with lookup._client(Family.V4) as client:
            assert client.trust_env is False
            assert client.follow_redirects is False
        async with lookup._client(None) as client:
            assert client.trust_env is False


# -- other gates ------------------------------------------------------------------------------


class TestSmallGates:
    async def test_sleep_closes_once_and_invalidates(self) -> None:
        clock = FakeClock()
        called: list[str] = []
        gate = SleepGate(clock, on_sleep=[lambda: called.append("x")])
        assert (await gate.check(NAV)).ok
        clock.advance(3600)
        assert (await gate.check(NAV)).ok
        clock.suspend(31)
        result = await gate.check(NAV)
        assert not result.ok
        assert "diverged by 31 s" in result.reason
        assert called == ["x"]
        assert (await gate.check(NAV)).ok

    async def test_a_sleep_before_the_first_check_counts(self) -> None:
        clock = FakeClock()
        gate = SleepGate(clock)  # built at process start
        clock.suspend(120)
        assert not (await gate.check(NAV)).ok

    async def test_timezone(self) -> None:
        assert (await TimezoneGate(lambda: ZONE, ZONE).check(NAV)).ok
        other = await TimezoneGate(lambda: "UTC", ZONE).check(NAV)
        assert not other.ok
        assert "UTC, not Europe/Sofia" in other.reason
        assert other.state is CollectorState.NOT_HOME
        assert "unknown" in (await TimezoneGate(lambda: None, ZONE).check(NAV)).reason
        pinned = await TimezoneGate(lambda: ZONE, ZONE, home_zone="Europe/Kyiv").check(NAV)
        assert "differs" in pinned.reason

    async def test_timezone_reads_the_pin_at_every_check(self) -> None:
        pins: list[str | None] = [None, ZONE, "Europe/Kyiv"]

        async def pin_zone() -> str | None:
            return pins.pop(0)

        gate = TimezoneGate(lambda: ZONE, ZONE, home_zone=pin_zone)
        assert (await gate.check(NAV)).ok  # no pin yet: the home gate refuses on its own
        assert (await gate.check(NAV)).ok
        moved = await gate.check(NAV)  # FBC_TIMEZONE and the OS agree, the pin does not
        assert not moved.ok
        assert "Europe/Kyiv differs" in moved.reason

    async def test_power(self) -> None:
        clock = FakeClock()
        assert (await PowerGate(lambda: PowerSource.AC, clock).check(NAV)).ok
        battery = await PowerGate(lambda: PowerSource.BATTERY, clock).check(NAV)
        assert (battery.ok, battery.reason) == (False, "on battery")
        unknown = await PowerGate(lambda: PowerSource.UNKNOWN, clock).check(NAV)
        assert unknown.reason == "power source unknown"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = Store(tmp_path / "state.sqlite", clock=FakeClock())
    await s.open()
    yield s
    await s.close()


class TestCrashLoop:
    async def test_process_starts(self, tmp_path: Path) -> None:
        clock = FakeClock()
        async with Store(tmp_path / "s.sqlite", clock=clock) as store:
            gate = CrashLoopGate(store, clock)
            for _ in range(3):
                await store.record_process_event(ProcessKind.PROCESS_START)
                clock.advance(60)
            assert (await gate.check(NAV)).ok
            await store.record_process_event(ProcessKind.PROCESS_START)
            result = await gate.check(NAV)
            assert not result.ok
            assert result.state is CollectorState.CRASH_LOOP
            assert result.reason == "4 unplanned process starts in the last hour"
            clock.advance(3600)
            assert (await gate.check(NAV)).ok

    async def test_the_launch_being_guarded_counts(self, tmp_path: Path) -> None:
        clock = FakeClock()
        async with Store(tmp_path / "s.sqlite", clock=clock) as store:
            gate = CrashLoopGate(store, clock)
            for _ in range(3):
                await store.record_process_event(ProcessKind.CHROME_LAUNCH)
            assert (await gate.check(NAV)).ok
            assert not (await gate.check(GateContext(Activity.LAUNCH))).ok

    async def test_eight_a_day(self, tmp_path: Path) -> None:
        clock = FakeClock()
        async with Store(tmp_path / "s.sqlite", clock=clock) as store:
            gate = CrashLoopGate(store, clock)
            for _ in range(9):
                await store.record_process_event(ProcessKind.CHROME_LAUNCH)
                clock.advance(2 * 3600)
            result = await gate.check(NAV)
            assert "in the last day" in result.reason

    async def test_planned_launches_and_starts_do_not_count(self, tmp_path: Path) -> None:
        clock = FakeClock()
        async with Store(tmp_path / "s.sqlite", clock=clock) as store:
            gate = CrashLoopGate(store, clock)
            # A day of the planned lifecycle: 30 relaunches after quiet hours, breaks, sleeps
            # and gates, and 9 restarts someone asked for — at most 10 of a kind in any hour.
            for n in range(30):
                await store.record_process_event(ProcessKind.CHROME_LAUNCH, planned=True)
                if n < 9:
                    await store.record_process_event(ProcessKind.PROCESS_START, planned=True)
                clock.advance(40 * 60)
            assert (await gate.check(NAV)).ok
            assert (await gate.check(GateContext(Activity.LAUNCH))).ok

    async def test_unplanned_ones_count_at_three_an_hour(self, tmp_path: Path) -> None:
        clock = FakeClock()
        async with Store(tmp_path / "s.sqlite", clock=clock) as store:
            gate = CrashLoopGate(store, clock)
            for _ in range(5):
                await store.record_process_event(ProcessKind.CHROME_LAUNCH, planned=True)
            for _ in range(3):
                await store.record_process_event(ProcessKind.CHROME_LAUNCH)
            assert (await gate.check(NAV)).ok
            await store.record_process_event(ProcessKind.CHROME_LAUNCH)
            result = await gate.check(NAV)
            assert result.state is CollectorState.CRASH_LOOP
            assert result.reason == "4 unplanned chrome launches in the last hour"
            assert result.retry_at == clock.time() + 900

    async def test_unplanned_ones_count_at_eight_a_day(self, tmp_path: Path) -> None:
        clock = FakeClock()
        async with Store(tmp_path / "s.sqlite", clock=clock) as store:
            gate = CrashLoopGate(store, clock)
            for _ in range(8):
                await store.record_process_event(ProcessKind.CHROME_LAUNCH)
                await store.record_process_event(ProcessKind.CHROME_LAUNCH, planned=True)
                clock.advance(2 * 3600)
            assert (await gate.check(NAV)).ok
            await store.record_process_event(ProcessKind.CHROME_LAUNCH)
            result = await gate.check(NAV)
            assert result.reason == "9 unplanned chrome launches in the last day"
            assert result.retry_at == clock.time() + 6 * 3600 / 4

    async def test_the_pending_launch_counts_only_when_unplanned(self, tmp_path: Path) -> None:
        clock = FakeClock()
        async with Store(tmp_path / "s.sqlite", clock=clock) as store:
            gate = CrashLoopGate(store, clock)
            for _ in range(3):
                await store.record_process_event(ProcessKind.CHROME_LAUNCH)
            unplanned = await gate.check(GateContext(Activity.LAUNCH))
            assert unplanned.reason == "4 unplanned chrome launches in the last hour"
            assert (await gate.check(GateContext(Activity.LAUNCH, planned=True))).ok
            pair = GateContext(Activity.LAUNCH, launches=2, planned=True)
            assert (await gate.check(pair)).ok  # a planned self-test with its read's launch
            await store.record_process_event(ProcessKind.CHROME_LAUNCH)
            planned = await gate.check(GateContext(Activity.LAUNCH, planned=True))
            assert planned.reason == "4 unplanned chrome launches in the last hour"

    async def test_all_launches_planned_or_not_are_capped_at_ten_an_hour(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock()
        async with Store(tmp_path / "s.sqlite", clock=clock) as store:
            gate = CrashLoopGate(store, clock)
            for _ in range(10):
                await store.record_process_event(ProcessKind.CHROME_LAUNCH, planned=True)
            assert (await gate.check(NAV)).ok
            refused = await gate.check(GateContext(Activity.LAUNCH, planned=True))
            assert refused.state is CollectorState.CRASH_LOOP
            assert refused.reason == "11 chrome launches (planned or not) in the last hour"
            await store.record_process_event(ProcessKind.CHROME_LAUNCH, planned=True)
            assert (await gate.check(NAV)).reason == (
                "11 chrome launches (planned or not) in the last hour"
            )
            assert (await CrashLoopGate(store, clock, all_per_hour=11).check(NAV)).ok
            for _ in range(11):
                await store.record_process_event(ProcessKind.PROCESS_START, planned=True)
            starts = await gate.check(NAV)
            assert starts.reason == "11 process starts (planned or not) in the last hour"
            clock.advance(3600 + 1)
            assert (await gate.check(NAV)).ok

    async def test_all_launches_planned_or_not_are_capped_at_forty_a_day(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock()
        async with Store(tmp_path / "s.sqlite", clock=clock) as store:
            gate = CrashLoopGate(store, clock)
            for _ in range(40):
                await store.record_process_event(ProcessKind.CHROME_LAUNCH, planned=True)
                await store.record_process_event(ProcessKind.PROCESS_START, planned=True)
                clock.advance(30 * 60)
            assert (await gate.check(NAV)).ok
            await store.record_process_event(ProcessKind.PROCESS_START, planned=True)
            result = await gate.check(NAV)
            assert result.reason == "41 process starts (planned or not) in the last day"
            assert result.retry_at == clock.time() + 6 * 3600 / 4
            wider = CrashLoopGate(store, clock, all_per_day=41)
            assert (await wider.check(NAV)).ok
            pair = GateContext(Activity.LAUNCH, launches=2, planned=True)
            assert (await wider.check(pair)).reason == (
                "42 chrome launches (planned or not) in the last day"
            )


class TestWarmup:
    async def test_phases(self, store: Store) -> None:
        clock = FakeClock()
        gate = WarmupGate(store, clock, Jitter("w"))
        first = await gate.check(NAV)
        assert first.state is CollectorState.NEEDS_LOGIN
        await store.kv_set("login_at", repr(clock.time()))
        ends = await gate.ends_at()
        assert ends is not None
        assert 24 * 3600 <= ends - clock.time() <= 72 * 3600
        warm = await gate.check(NAV)
        assert (warm.state, warm.retry_at) == (CollectorState.WARMUP, ends)
        clock.advance(ends - clock.time())
        assert (await gate.check(NAV)).ok
        assert await WarmupGate(store, clock, Jitter("w")).ends_at() == ends  # stable


class TestLocks:
    def test_profile_lock(self, tmp_path: Path) -> None:
        path = tmp_path / "collector.lock"
        with ProfileLock(path) as lock:
            assert bool(lock.held)  # (bool(): mypy would otherwise pin the property to True)
            lock.acquire()  # idempotent
            other = ProfileLock(path)
            with pytest.raises(ProfileLockedError):
                other.acquire()
            assert not other.held
            assert path.read_text().strip() == str(os.getpid())
        assert not lock.held
        lock.release()  # idempotent
        again = ProfileLock(path)
        again.acquire()
        again.release()

    async def test_lock_gate(self, tmp_path: Path) -> None:
        lock = ProfileLock(tmp_path / "l")
        assert not (await lock.check(NAV)).ok
        lock.acquire()
        assert (await lock.check(NAV)).ok
        lock.release()

    def test_singleton_states(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        assert singleton_state(profile).state is SingletonState.ABSENT
        (profile / "SingletonLock").symlink_to("myhost-4242")
        (profile / "SingletonSocket").symlink_to("nowhere")
        dead = singleton_state(profile, hostname="myhost", alive=lambda _: False)
        assert (dead.state, dead.pid) == (SingletonState.STALE, 4242)
        live = singleton_state(profile, hostname="myhost", alive=lambda _: True)
        assert live.state is SingletonState.LIVE
        assert singleton_state(profile, hostname="other").state is SingletonState.FOREIGN
        with pytest.raises(ProfileLockedError, match="live"):
            clear_stale_singleton(profile, hostname="myhost", alive=lambda _: True)
        cleared = clear_stale_singleton(profile, hostname="myhost", alive=lambda _: False)
        assert cleared.state is SingletonState.STALE
        assert not (profile / "SingletonLock").is_symlink()
        assert not (profile / "SingletonSocket").is_symlink()
        assert clear_stale_singleton(profile).state is SingletonState.ABSENT

    def test_unreadable_singletons(self, tmp_path: Path) -> None:
        profile = tmp_path / "p"
        profile.mkdir()
        (profile / "SingletonLock").symlink_to("no-pid-here")
        assert singleton_state(profile).state is SingletonState.UNREADABLE
        (profile / "SingletonLock").unlink()
        (profile / "SingletonLock").write_text("a regular file")
        assert singleton_state(profile).state is SingletonState.UNREADABLE
        with pytest.raises(ProfileLockedError):
            clear_stale_singleton(profile)

    def test_pid_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert pid_alive(os.getpid())

        def gone(pid: int, sig: int) -> None:
            raise ProcessLookupError

        def foreign(pid: int, sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(os, "kill", gone)
        assert not pid_alive(1)
        monkeypatch.setattr(os, "kill", foreign)
        assert pid_alive(1)


class TestGateSet:
    async def test_order_short_circuit_and_failures(self) -> None:
        calls: list[str] = []

        def make(name: str, result: GateResult | Exception) -> FuncGate:
            async def check(context: GateContext) -> GateResult:
                calls.append(name)
                if isinstance(result, Exception):
                    raise result
                return result

            return FuncGate(name, check)

        gates = GateSet(
            [
                make("a", GateResult("a", True)),
                make("b", RuntimeError("probe crashed")),
                make("c", GateResult("c", False, "x", CollectorState.QUIET, 50.0)),
            ]
        )
        report = await gates.evaluate(NAV)
        assert calls == ["a", "b"]
        assert not report.ok
        assert report.closed[0].reason == "check failed (probe crashed)"
        calls.clear()
        full = await gates.evaluate(NAV, stop_early=False)
        assert calls == ["a", "b", "c"]
        assert full.as_status() == {"a": True, "b": False, "c": False}
        assert full.reasons == ("b: check failed (probe crashed)", "c: x")
        assert full.state is CollectorState.QUIET
        assert full.retry_at is None  # b is open-ended

    def test_report_state_and_retry(self) -> None:
        report = GateReport(
            (
                GateResult("q", False, "", CollectorState.QUIET, 10.0),
                GateResult("b", False, "", CollectorState.BLOCKED, 20.0),
                GateResult("ok", True),
            )
        )
        assert report.state is CollectorState.BLOCKED
        assert report.retry_at == 20.0
        assert GateReport((GateResult("ok", True),)).state is CollectorState.OK
        assert GateReport((GateResult("x", False),)).state is CollectorState.UNKNOWN
        assert GateReport(()).retry_at is None
