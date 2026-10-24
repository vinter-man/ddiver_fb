"""A home network, a power source, a timezone and an egress lookup the test controls."""

from __future__ import annotations

from dataclasses import dataclass, field

from fbcollector.gates import Family, HomePin, NetworkFacts, PowerSource

from .pacing_support import ZONE

ROUTER_MAC = "02:00:5e:10:00:01"
HOME_ASN = 8866
HOME_V4 = "198.51.100.7"

HOME_FACTS = NetworkFacts(
    gateway="192.0.2.1",
    interface="en0",
    gateway_mac=ROUTER_MAC,
    egress_interfaces=("en0",),
)
HOME_PIN = HomePin(
    router_mac=ROUTER_MAC, asn_v4=HOME_ASN, asn_v6=None, timezone=ZONE, pinned_at=1.0
)


@dataclass
class FakeProbes:
    facts: NetworkFacts = HOME_FACTS
    source: PowerSource = PowerSource.AC
    zone: str | None = ZONE
    network_calls: int = 0

    def network(self) -> NetworkFacts:
        self.network_calls += 1
        return self.facts

    def power(self) -> PowerSource:
        return self.source

    def os_timezone(self) -> str | None:
        return self.zone


@dataclass
class FakeLookup:
    addresses: dict[Family, str | None] = field(
        default_factory=lambda: {Family.V4: HOME_V4, Family.V6: None}
    )
    asns: dict[str, int | None] = field(default_factory=lambda: {HOME_V4: HOME_ASN})
    fail: Exception | None = None
    calls: int = 0

    async def address(self, family: Family) -> str | None:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return self.addresses.get(family)

    async def asn(self, address: str) -> int | None:
        return self.asns.get(address)
