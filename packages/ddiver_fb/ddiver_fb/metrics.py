"""The brain's Prometheus series: the core's delivery and matcher counters plus ``fbb_*``.

Names follow DESIGN "Operations": ``fbb_ingest_items_total{result}``,
``fbb_collector_status_age_seconds`` and ``fbb_group_last_item_age_seconds{group}``. The core
keeps its series flat on purpose; the two labelled ones here are bounded by construction — four
item results, and at most 25 monitored groups.

The collector has no metrics server of its own (it sits behind the owner's router); what DESIGN
lists as its series are re-exported here from the lease holder's last ``Status``: ``fbc_state``
(the brain's effective state, OFFLINE included), ``fbc_gate_closed{gate}``,
``fbc_budget_used``/``fbc_budget_remaining{kind,window}``, the outbox and the counters the
status carries. They describe the last report; ``fbb_collector_status_age_seconds`` says how
old it is.

The core's ``ddiver_listener_connected`` is left out: it describes a Telegram user session, and
this app has none. A series that always reads 0 would page whoever wires ddiver's alert rules to
this job too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from ddiver_core.metrics import Metric, MetricsSource
from ddiver_fb.source import SourceMetrics
from fbwire import CollectorState, ItemOutcome, Status

CollectorReport = Callable[[], tuple[CollectorState | None, Status | None]]
COUNTER_SERIES = (
    ("outbox_dropped", "fbc_outbox_dropped_total", "Posts the full outbox dropped, oldest first"),
    ("outbox_rejected", "fbc_outbox_rejected_total", "Posts the brain answered as rejected"),
    ("events_dropped", "fbc_events_dropped_total", "Events the full queue dropped"),
)
REFUSED_PREFIX = "uplink_refused_"

OMITTED = frozenset({"ddiver_listener_connected"})


class FbMetricsSource(MetricsSource):
    def __init__(
        self,
        *,
        items: SourceMetrics,
        status_age: Callable[[], float | None],
        group_ages: Callable[[], dict[int, float]],
        bot_connected: Callable[[], bool],
        ingest_up: Callable[[], bool],
        delivery: Any = None,
        matcher: Any = None,
        watchdog: Any = None,
        uptime: Callable[[], float] = lambda: 0.0,
        collector: CollectorReport = lambda: (None, None),
    ) -> None:
        super().__init__(delivery=delivery, matcher=matcher, watchdog=watchdog, uptime=uptime)
        self._items = items
        self._status_age = status_age
        self._group_ages = group_ages
        self._bot_connected = bot_connected
        self._ingest_up = ingest_up
        self._collector = collector

    def render(self) -> str:
        """The core's renderer writes HELP and TYPE per series; a labelled family needs them once.

        Prometheus refuses a second ``# HELP`` for the same name, so every series after a
        family's first keeps only its sample line. A family's series are yielded together.
        """
        parts: list[str] = []
        seen: set[str] = set()
        for metric in self.collect():
            text = metric.render()
            if metric.name in seen:
                text = text.split("\n", 2)[2]
            seen.add(metric.name)
            parts.append(text)
        return "".join(parts)

    def collect(self) -> Iterator[Metric]:
        for metric in super().collect():
            if metric.name not in OMITTED:
                yield metric
        yield Metric(
            "fbb_bot_connected",
            "gauge",
            "1 while the bot's connection to Telegram is up",
            int(self._bot_connected()),
        )
        yield Metric(
            "fbb_ingest_up",
            "gauge",
            "1 while the ingest server accepts connections",
            int(self._ingest_up()),
        )
        for outcome in ItemOutcome:
            yield Metric(
                "fbb_ingest_items_total",
                "counter",
                "Posts the collector sent, by what the brain answered",
                self._items.results.get(outcome, 0),
                labels=(("result", outcome.value),),
            )
        age = self._status_age()
        if age is not None:
            yield Metric(
                "fbb_collector_status_age_seconds",
                "gauge",
                "Seconds since the lease holder's last status report",
                age,
            )
        for group, seconds in sorted(self._group_ages().items()):
            yield Metric(
                "fbb_group_last_item_age_seconds",
                "gauge",
                "Seconds since the newest post seen in a monitored group was written",
                seconds,
                labels=(("group", str(group)),),
            )
        yield from self._collector_series()

    def _collector_series(self) -> Iterator[Metric]:
        """The lease holder's last report as ``fbc_*`` (see the module); none before one."""
        state, status = self._collector()
        if state is None or status is None:
            return
        for each in CollectorState:
            yield Metric(
                "fbc_state",
                "gauge",
                "1 for the lease holder's state (offline is the brain's verdict)",
                int(each is state),
                labels=(("state", each.value),),
            )
        for gate, open_ in sorted(status.gates.items()):
            yield Metric(
                "fbc_gate_closed",
                "gauge",
                "1 while that gate kept the collector from reading at its last check",
                int(not open_),
                labels=(("gate", gate),),
            )
        for name, help_, pick in (
            ("fbc_budget_used", "Navigations or scroll steps spent in the window", "used"),
            ("fbc_budget_remaining", "Navigations or scroll steps left in the window", "left"),
        ):
            for window in status.budget:
                yield Metric(
                    name,
                    "gauge",
                    help_,
                    window.used if pick == "used" else window.remaining,
                    labels=(("kind", window.kind.value), ("window", str(window.window_seconds))),
                )
        counters = status.counters
        if "outbox_depth" in counters:
            yield Metric(
                "fbc_outbox_depth", "gauge", "Posts waiting at home", counters["outbox_depth"]
            )
        for key, name, help_ in COUNTER_SERIES:
            if key in counters:
                yield Metric(name, "counter", help_, counters[key])
        for key, value in sorted(counters.items()):
            if key.startswith(REFUSED_PREFIX):
                yield Metric(
                    "fbc_uplink_refused_total",
                    "counter",
                    "Requests the brain answered but refused, by endpoint",
                    value,
                    labels=(("endpoint", key.removeprefix(REFUSED_PREFIX)),),
                )
