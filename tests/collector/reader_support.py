"""Synthetic pages, a scripted fake driver and a stub scheduler for the read-loop tests.

The pages are the smallest ones the real parser and detectors accept: an English SSR with a
logged-in ``USER_ID`` and the feed preloader (or a group header and the group preloader), and
GraphQL bodies with ``news_feed`` / ``group_feed`` edges. Nothing here touches a network.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fbcollector.clock import FakeClock
from fbcollector.config import CAUTIOUS
from fbcollector.detectors import FEED_URL, JOINS_URL, group_url
from fbcollector.driver.launch import LaunchInfo, SessionMode
from fbcollector.model import DomFacts, GraphqlBody, ReadOutcome, Step, Surface
from fbcollector.scheduler import Action, ActionKind, Grant, Hold
from fbcollector.store import Store
from fbwire.models import Profile

G1, G2, G3 = "400000000000001", "400000000000002", "400000000000003"
T0 = 1_790_000_000.0
USER = "100000000000009"


def post(n: int) -> str:
    return f"50000000000{n:05d}"


def story(
    post_id: str,
    gid: str = G1,
    created: float = T0,
    text: str | None = "hello",
    **extra: Any,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "__typename": "Story",
        "post_id": post_id,
        "creation_time": int(created),
        "to": {"__typename": "Group", "id": gid},
        "permalink_url": f"https://www.facebook.com/groups/{gid}/posts/{post_id}/?__cft__=x",
        "actors": [
            {
                "__typename": "User",
                "id": "100000000000077",
                "name": "Ann",
                "url": "https://www.facebook.com/ann",
            }
        ],
    }
    if text is not None:
        node["comet_sections"] = {"content": {"story": {"message": {"text": text}}}}
    else:
        node["comet_sections"] = {"content": {"story": {"message": {}}}}  # a section, no text
    node.update(extra)
    return node


def gql(frames: Sequence[dict[str, Any]], name: str = "Query", status: int = 200) -> GraphqlBody:
    text = "for (;;);" + "\n".join(json.dumps(f) for f in frames)
    return GraphqlBody(friendly_name=name, body=text.encode(), status=status)


def feed_body(*nodes: dict[str, Any], has_next: bool = True) -> GraphqlBody:
    edges = [{"node": n, "cursor": f"c{i}"} for i, n in enumerate(nodes)]
    frame = {
        "data": {
            "viewer": {
                "news_feed": {
                    "edges": edges,
                    "page_info": {"has_next_page": has_next, "end_cursor": "next"},
                }
            }
        }
    }
    return gql([frame], "CometNewsFeedPaginationQuery")


def group_body(*nodes: dict[str, Any]) -> GraphqlBody:
    edges = [{"node": n, "cursor": f"c{i}"} for i, n in enumerate(nodes)]
    frame = {"data": {"node": {"group_feed": {"edges": edges, "page_info": {}}}}}
    return gql([frame], "GroupsCometFeedRegularStoriesPaginationQuery")


def html(*values: Any, lang: str = "en", body: str = "") -> bytes:
    scripts = "".join(
        f'<script type="application/json" data-sjs>{json.dumps(v)}</script>' for v in values
    )
    return f'<html lang="{lang}"><head></head><body>{scripts}{body}</body></html>'.encode()


def user_define(user_id: str = USER) -> list[Any]:
    return ["CurrentUserInitialData", [], {"USER_ID": user_id, "ACCOUNT_ID": user_id}, 270]


def feed_document(*, user_id: str = USER, chrono: bool = True, lang: str = "en") -> bytes:
    preloader: dict[str, Any] = {
        "queryName": "CometNewsFeedQuery",
        "variables": {
            "feedStyle": "MOST_RECENT_GROUPS_FEED" if chrono else "DEFAULT",
            "orderby": ["MOST_RECENT"] if chrono else ["TOP_STORIES"],
        },
    }
    return html({"define": [user_define(user_id)]}, {"preloader": preloader}, lang=lang)


def group_document(
    gid: str = G1,
    *,
    join_state: str | None = "MEMBER",
    subscribe: str = "IS_SUBSCRIBED",
    sorting: str = "CHRONOLOGICAL",
    privacy: str = "Private group",
    name: str = "Flats",
    body: str = "",
) -> bytes:
    header = {
        "__bbox": {
            "result": {
                "data": {
                    "group": {
                        "id": gid,
                        "name": name,
                        "profile_header_renderer": {
                            "group": {
                                "id": gid,
                                "name": name,
                                "viewer_join_state": join_state,
                                "privacy_info": {"title": {"text": privacy}},
                                "if_viewer_can_change_follow_setting": {
                                    "id": gid,
                                    "subscribe_status": subscribe,
                                },
                            }
                        },
                    }
                }
            }
        }
    }
    if join_state is None:  # a page that does not say whether the account is a member
        own: dict[str, Any] = header["__bbox"]["result"]["data"]["group"]
        del own["profile_header_renderer"]["group"]["viewer_join_state"]
    preloader = {
        "queryName": "GroupsCometFeedRegularStoriesQuery",
        "variables": {"groupID": gid, "sortingSetting": sorting},
    }
    return html({"define": [user_define()]}, header, {"preloader": preloader}, body=body)


def joins_document() -> bytes:
    return html({"define": [user_define()]})


def joins_body(*groups: tuple[str, str], has_next: bool = False) -> GraphqlBody:
    nodes = [
        {
            "__typename": "Group",
            "id": gid,
            "name": name,
            "url": f"https://www.facebook.com/groups/{gid}/",
            "viewer_join_state": "MEMBER",
        }
        for gid, name in groups
    ]
    frame = {
        "data": {
            "viewer": {
                "groups_tab": {
                    "joined_groups": {
                        "edges": [{"node": n} for n in nodes],
                        "page_info": {"has_next_page": has_next, "end_cursor": "x"},
                    }
                }
            }
        }
    }
    return gql([frame], "GroupsCometAllJoinedGroupsSectionPaginationQuery")


def nav(
    url: str,
    document: bytes | None,
    *bodies: GraphqlBody,
    status: int | None = 200,
    posinset: int = 3,
    html_lang: str = "en",
    **dom: Any,
) -> Step:
    return Step(
        final_url=url,
        status=status,
        document=document,
        graphql=bodies,
        dom=DomFacts(html_lang=html_lang, posinset_count=posinset, **dom),
        latency_ms=900,
    )


def scroll(url: str, *bodies: GraphqlBody, fired: bool = True, **dom: Any) -> Step:
    return Step(
        final_url=url,
        status=None,
        graphql=bodies,
        dom=DomFacts(html_lang="en", posinset_count=5, **dom),
        pagination_fired=fired,
        latency_ms=1500,
    )


@dataclass
class FakeDriver:
    """Plays scripted steps; records every call. ``screenshot`` can check the store first."""

    navigations: deque[Step] = field(default_factory=deque)
    scrolls: deque[Step | Exception] = field(default_factory=deque)
    dwells: deque[Step] = field(default_factory=deque)
    cookies: frozenset[str] = frozenset({"c_user", "xs", "datr"})
    calls: list[str] = field(default_factory=list)
    shots: list[Path] = field(default_factory=list)
    on_screenshot: Any = None
    navigate_error: Exception | None = None
    last_url: str = "about:blank"
    launches: int = 0
    closed: int = 0
    modes: list[SessionMode] = field(default_factory=list)
    """The session mode of every launch, in order."""
    info: LaunchInfo = field(
        default_factory=lambda: LaunchInfo("chrome --no-proxy-server about:blank", 4242, None, None)
    )

    @property
    def running(self) -> bool:
        return self.launches > self.closed

    async def launch(self, *, mode: SessionMode) -> LaunchInfo:
        self.calls.append("launch")
        self.modes.append(mode)
        self.launches += 1
        return self.info

    async def navigate(self, url: str) -> Step:
        self.calls.append(f"navigate {url}")
        if self.navigate_error is not None:
            raise self.navigate_error
        step = self.navigations.popleft()
        self.last_url = step.final_url
        return step

    async def scroll_step(self) -> Step:
        self.calls.append("scroll")
        step = self.scrolls.popleft() if self.scrolls else scroll(self.last_url, fired=False)
        if isinstance(step, Exception):
            raise step
        return step

    async def dwell(self, seconds: float) -> Step:
        self.calls.append(f"dwell {seconds:.0f}")
        if self.dwells:
            return self.dwells.popleft()
        return Step(final_url=self.last_url, status=None, dom=DomFacts(html_lang="en"))

    async def park(self) -> None:
        self.calls.append("park")

    async def close(self) -> None:
        self.calls.append("close")
        self.closed += 1

    async def cookie_names(self, url: str = "https://www.facebook.com/") -> frozenset[str]:
        return self.cookies

    async def read_attribute(self, selector: str, name: str) -> str | None:
        return None

    async def screenshot(self, path: Path) -> Path | None:
        self.calls.append("screenshot")
        if self.on_screenshot is not None:
            await self.on_screenshot()
        self.shots.append(path)
        return path

    async def wait_closed(self) -> None:
        self.calls.append("wait_closed")


@dataclass
class StubScheduler:
    holds: deque[Hold | None] = field(default_factory=deque)
    finished: list[tuple[ReadOutcome, tuple[str, ...]]] = field(default_factory=list)
    permits: int = 0
    rechecks: deque[Hold | None] = field(default_factory=deque)
    """Answers to ``recheck`` (before the final look); empty: every gate is open."""
    confirms: deque[Hold | None] = field(default_factory=deque)
    """Answers to ``still_granted`` (before the navigation); empty: the grant stands."""

    async def scroll_permit(
        self, grant: Grant, *, steps_done: int, read_id: int | None
    ) -> Hold | None:
        self.permits += 1
        return self.holds.popleft() if self.holds else None

    async def read_finished(
        self, grant: Grant, *, outcome: ReadOutcome, gap_groups: Sequence[str] = ()
    ) -> None:
        self.finished.append((outcome, tuple(gap_groups)))

    async def recheck(self, grant: Grant) -> Hold | None:
        return self.rechecks.popleft() if self.rechecks else None

    async def still_granted(self, grant: Grant) -> Hold | None:
        return self.confirms.popleft() if self.confirms else None


def grant(
    kind: ActionKind,
    *,
    group: str | None = None,
    steps: int = 3,
    generation: int = 0,
    **kw: Any,
) -> Grant:
    """A grant as the scheduler would make it; ``generation`` is the control state's."""
    if kind is ActionKind.BACKSTOP:
        assert group is not None
        action = Action(kind, Surface.GROUP, group_url(group), group_id=group, **kw)
    elif kind is ActionKind.SYNC:
        action = Action(kind, Surface.JOINS, JOINS_URL, **kw)
    else:
        action = Action(kind, Surface.FEED, FEED_URL, **kw)
    action = Action(
        action.kind,
        action.surface,
        action.url,
        group_id=action.group_id,
        scroll_allowance=steps,
        reason=action.reason,
        catch_up=action.catch_up,
        deep=action.deep,
        block_id=action.block_id,
        min_dwell=action.min_dwell,
    )
    return Grant(action, Profile.CAUTIOUS, CAUTIOUS, generation=generation)


class Recorder:
    """Stands in for ``CollectorEvents``: remembers events and notifications. A notification
    is delivered at once (its ``on_sent`` awaited) unless ``undelivered`` (the bot failed or
    rate-limited it); ``fail_notify`` makes ``notify`` itself raise."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.notes: list[tuple[str, str]] = []
        self.dedups: dict[str, float | None] = {}
        self.fail_notify = False
        self.undelivered = False

    async def __call__(self, kind: Any, **kw: Any) -> None:
        self.events.append((kind.value, kw))

    async def notify(
        self,
        key: str,
        text: str,
        *,
        dedup: float | None = None,
        on_sent: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if self.fail_notify:
            raise OSError("the alert ledger is gone")
        self.notes.append((key, text))
        self.dedups[key] = dedup
        if on_sent is not None and not self.undelivered:
            await on_sent()

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


async def seed(store: Store, clock: FakeClock, *groups: str, monitored: bool = True) -> None:
    from fbcollector.model import GroupMeta
    from fbwire.models import MonitoredGroup

    for gid in groups:
        await store.upsert_group(
            GroupMeta(id=gid, name=f"Group {gid[-1]}", join_state="MEMBER"), synced=True
        )
    if monitored:
        await store.set_monitored([MonitoredGroup(group_id=g) for g in groups])
