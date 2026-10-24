"""The report's parser diagnostics: skeletons of stories without text, and nothing they say.

The stories here are the committed feed fixture's story (the 2026-09-24 capture's shape) bent the
ways a logged-in feed may carry its text where the parser does not look: a background-text
renderer with the words under ``body``, a message container whose ``message`` holds them one
level down, and a ``@defer`` part that delivers the message section after the story. Every
value in them is planted, and none of them may appear in the report.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
import string
import tempfile
import zoneinfo
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from fbcollector import diagnostics
from fbcollector.clock import FakeClock
from fbcollector.diagnostics import (
    ABSENT,
    ARRAY,
    EMPTY,
    ID,
    KEY,
    KNOWN_TEXT_PATHS,
    MIN_LENGTH,
    NULL,
    ODD,
    SCRIPT_LABELS,
    TEXT,
    RawScan,
    Sample,
    Script,
    known_paths,
    render_scan,
    render_scripts,
    safe_name,
    scan_raw,
    script_of,
    skeleton,
)
from fbcollector.frames import iter_graphql_frames
from fbcollector.model import Coverage, DomFacts, GraphqlBody, ReadOutcome, Step, Surface
from fbcollector.raw import save_step
from fbcollector.relay import looks_like_id
from fbcollector.report import Measured, generate, load, render, text_paths
from fbcollector.store import Store
from fbcollector.stories import MAX_SHARE_DEPTH, own_text

from .parser_fixtures import FIXTURES, load_steps

ZONE = zoneinfo.ZoneInfo("Europe/Sofia")
T0 = 1_790_000_000.0
FEED_URL = "https://www.facebook.com/?filter=groups&sk=h_chr"
STREAM = "CometNewsFeed_viewerConnection$stream$CometNewsFeed_viewer_news_feed"
DEFER = "CometFeedStoryMessageSection_story$defer$CometFeedStory_message"
BACKGROUND = "Търся превоз от София до Варна утре сутринта, двама души с малко багаж."
CONTAINER = "Продавам детска количка, почти нова, цена по договаряне, гр. Пловдив."
DEFERRED = "Пътувам за Бургас в петък след 17:00, имам три свободни места в колата."
AUTHOR = "Мария Иванова-Петрова"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# -- stories -----------------------------------------------------------------------------------


def fixture_story() -> dict[str, Any]:
    """The first story of the feed fixture's first pagination body."""
    body = load_steps("feed_hchr_synthetic.json")[1].graphql[0].body
    first = next(iter(iter_graphql_frames(body)))
    node: dict[str, Any] = first["data"]["viewer"]["news_feed"]["edges"][0]["node"]
    return copy.deepcopy(node)


def _renumber(node: dict[str, Any], post_id: str) -> dict[str, Any]:
    node["post_id"] = post_id
    node["comet_sections"]["content"]["story"]["post_id"] = post_id
    title = node["comet_sections"]["context_layout"]["story"]["comet_sections"]["title"]
    title["story"]["post_id"] = post_id
    for actor in node["comet_sections"]["content"]["story"]["actors"]:
        actor["name"] = AUTHOR
    return node


def _content(node: dict[str, Any]) -> dict[str, Any]:
    content: dict[str, Any] = node["comet_sections"]["content"]["story"]
    return content


def photo_story(post_id: str = "5000000000000101") -> dict[str, Any]:
    """No message anywhere: a photo-only post, legitimately without text."""
    node = _renumber(fixture_story(), post_id)
    content = _content(node)
    content["message"] = None
    del content["comet_sections"]["message"]
    del content["comet_sections"]["message_container"]
    return node


def background_story(words: str = BACKGROUND, post_id: str = "5000000000000102") -> dict[str, Any]:
    """A background-text post: the section is there, the words sit under ``story.body``."""
    node = photo_story(post_id)
    _content(node)["comet_sections"]["message"] = {
        "__typename": "CometFeedStoryBackgroundTextRenderingStrategy",
        "story": {
            "id": "UzpfSTEwMDA2NjM4ODk0NjYzOTpWSzoxMjM0NTY3ODk=",
            "text_format_metadata": {
                "__typename": "TextFormatMetadata",
                "background_image": {"uri": "https://scontent.xx.fbcdn.net/v/t39.30808-6/bg.png"},
                "preset_id": "106018623298955",
                "ranges_by_id": {
                    "100064455566778": {
                        "entity": {"url": "https://www.facebook.com/profile.php?id=100064455566778"}
                    }
                },
            },
            "body": {"__typename": "TextWithEntities", "text": words},
        },
    }
    return node


def container_story(words: str = CONTAINER, post_id: str = "5000000000000103") -> dict[str, Any]:
    """A message container whose ``message`` is blank and keeps the words one level down."""
    node = photo_story(post_id)
    _content(node)["comet_sections"]["message_container"] = {
        "__typename": "CometFeedStoryMessageContainerRenderingStrategy",
        "story": {
            "id": "UzpfSTEwMjow",
            "message": {
                "__typename": "TextWithEntities",
                "text": " ",
                "text_with_entities": {"__typename": "TextWithEntities", "text": words},
            },
        },
    }
    return node


def text_story(post_id: str = "5000000000000104") -> dict[str, Any]:
    return _renumber(fixture_story(), post_id)


# -- steps -------------------------------------------------------------------------------------


def frames_body(*frames: dict[str, Any], name: str = "CometNewsFeedPaginationQuery") -> GraphqlBody:
    raw = "for (;;);" + "\n".join(json.dumps(f, ensure_ascii=False) for f in frames)
    return GraphqlBody(friendly_name=name, body=raw.encode())


def feed_step(*nodes: dict[str, Any]) -> Step:
    edges = [{"node": n, "cursor": f"c{i}"} for i, n in enumerate(nodes)]
    first = {"data": {"viewer": {"news_feed": {"edges": edges}}}}
    return Step(final_url=FEED_URL, status=200, graphql=(frames_body(first),), dom=DomFacts())


def stream_step(first: dict[str, Any], *later: dict[str, Any], parts: list[Any]) -> Step:
    """``first`` in the query result, ``later`` as ``@stream`` edges, then ``parts``."""
    frames: list[dict[str, Any]] = [
        {"data": {"viewer": {"news_feed": {"edges": [{"node": first, "cursor": "c0"}]}}}}
    ]
    frames += [
        {
            "label": STREAM,
            "path": ["viewer", "news_feed", "edges", i],
            "data": {"node": node, "cursor": f"c{i}"},
        }
        for i, node in enumerate(later, start=1)
    ]
    frames += parts
    return Step(final_url=FEED_URL, status=200, graphql=(frames_body(*frames),), dom=DomFacts())


def message_part(edge: int, words: str) -> dict[str, Any]:
    return {
        "label": DEFER,
        "path": [
            "viewer",
            "news_feed",
            "edges",
            edge,
            "node",
            "comet_sections",
            "content",
            "story",
            "comet_sections",
            "message",
        ],
        "data": {
            "__typename": "CometFeedStoryDefaultMessageRenderingStrategy",
            "story": {"message": {"__typename": "TextWithEntities", "text": words}},
        },
    }


def save(raw: Path, step: Step, *, at: float, read_id: int = 1, index: int = 0, **kw: Any) -> None:
    raw.mkdir(exist_ok=True)
    surface = kw.pop("surface", Surface.FEED)
    group_id = kw.pop("group_id", None)
    save_step(
        raw, read_id=read_id, index=index, surface=surface, group_id=group_id, at=at, step=step
    )


def tree(report: str) -> dict[str, str]:
    """The drawn tree as ``dotted path -> what follows the colon``."""
    block = report.split("```text\n", 1)[1].split("\n```", 1)[0]
    out: dict[str, str] = {}
    stack: list[str] = []
    for line in block.splitlines()[1:]:
        level = (len(line) - len(line.lstrip(" "))) // 2
        label, _, note = line.strip().partition(": ")
        del stack[level - 1 :]
        stack.append(label.split(" <", 1)[0])
        if note:
            out[".".join(stack)] = note
    return out


def length(note: str) -> str:
    return note.split(" · ", 1)[0]


def values(tree_: Any) -> Iterator[Any]:
    """Every leaf value, except typenames (printed on purpose)."""
    stack = [tree_]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(v for k, v in value.items() if k != "__typename")
        elif isinstance(value, list):
            stack.extend(value)
        else:
            yield value


def names(tree_: Any) -> set[str]:
    """Keys and typenames: structure, which the report may print."""
    out: set[str] = set()
    stack = [tree_]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            out.update(k for k in value if isinstance(k, str))
            if isinstance(value.get("__typename"), str):
                out.add(value["__typename"])
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return out


def significant(value: float) -> int:
    """Significant digits of ``value`` as Python prints it: ``42.697752`` has 8, ``1500.0``
    has 2."""
    return len(repr(abs(value)).split("e", 1)[0].replace(".", "").strip("0"))


def floats(tree_: Any) -> set[str]:
    """Planted floats of 4+ significant digits (a latitude, a longitude), as Python prints
    them, without the sign."""
    return {repr(abs(v)) for v in values(tree_) if isinstance(v, float) and significant(v) >= 4}


def planted(tree_: Any) -> set[str]:
    """What must never appear in the report: strings of 6+ visible characters, id-sized
    numbers, floats of 4+ significant digits."""
    structure = names(tree_)
    out: set[str] = floats(tree_)
    for value in values(tree_):
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int) and abs(value) >= 10**6:
            out.add(str(value))
        elif isinstance(value, str) and len(value.strip()) >= 6 and value not in structure:
            out.add(value.strip())
    return out


CYRILLIC = re.compile(r"[\u0400-\u04ff]")
TOKEN = re.compile(r"(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9_-]{20,}")


def distinctive(tree_: Any) -> set[str]:
    """The planted values that cannot coincide with the report's own words, so finding one in
    the report can only be a leak: text with Cyrillic in it (the report has none of its own),
    id-sized numbers, floats of 4+ significant digits, URLs and long alphanumeric tokens.
    (Hypothesis finds the rest quickly:
    a planted ``---|---|---`` is a Markdown table rule.) Any other value is covered by the
    drawing staying the same when every value is replaced (``scrub``)."""
    out = {s for s in planted(tree_) if s.isdigit() or s.startswith("https://")}
    out |= {s for s in planted(tree_) if CYRILLIC.search(s) or TOKEN.fullmatch(s)}
    return out | floats(tree_)


def leaks(tree_: Any, report: str) -> list[str]:
    """Distinctive values found in ``report``; a Cyrillic letter anywhere counts as a leak."""
    found = [s for s in distinctive(tree_) if s in report]
    return found + CYRILLIC.findall(report)


async def _empty_db(path: Path) -> Path:
    async with Store(path, clock=FakeClock(start=T0)):
        pass
    return path


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    return asyncio.run(_empty_db(tmp_path / "state.sqlite"))


# -- names --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "0",
        "123456789012345",
        "pfbid02Hk5Zr8cvQ4kLp1mS7dNqX",
        "UzpfSTEwMDA2NjM4ODk0NjYzOTpWSzoxMjM0NTY3ODk=",
        "UzpfSTEwMjow",
        "UzpfSTI3NjoxMzkw",  # S:_I276:1390: too few humps, one digit; base64 of a typed id
        "ZmVlZGJhY2s6MTIz",
        "group_400000000000001",
        "aB3dE5fG7hJ9kLmN",
        "a+b/c=",
    ],
)
def test_id_like_keys_are_masked(name: str) -> None:
    assert looks_like_id(name)
    assert safe_name(name) == ID


@pytest.mark.parametrize(
    "name",
    [
        "comet_sections",
        "__typename",
        "wwwURL",
        "i18n_reaction_count",
        "h264",
        "CometFeedStoryDefaultContentStrategy",
        "XFBGroupsCometMallFeedUnit",
        "__module_operation_CometFeedStoryRichMessageRenderingStrategy_message",
        STREAM,
    ],
)
def test_schema_names_are_kept(name: str) -> None:
    assert not looks_like_id(name)
    assert safe_name(name) == name


@pytest.mark.parametrize("name", ["", "две думи", "a b", "x.y", "ключ", 7, None])
def test_other_odd_keys_are_masked(name: object) -> None:
    assert safe_name(name) == KEY
    assert safe_name(name, ODD) == ODD


def test_odd_typenames_are_masked() -> None:
    shape = skeleton({"__typename": "Пост с текст", "a": {"__typename": "12345678", "b": "x" * 12}})
    assert shape.typenames == {(): {ODD}, ("a",): {ID}}


# -- the parser's own text paths ----------------------------------------------------------------


def _where(label: str, value: Any) -> dict[str, Any]:
    """A story whose only text is ``value`` at the parser's ``label`` place."""
    section = {"story": {"message": {"text": value}}}
    places: dict[str, dict[str, Any]] = {
        "message": {"message": {"text": value}},
        "sections.message": {"comet_sections": {"message": section}},
        "sections.message_container": {"comet_sections": {"message_container": section}},
        "sections.rich_message": {
            "comet_sections": {"message": {"rich_message": [{"text": value}]}}
        },
        "attached_story": {"attached_story": {"message": {"text": value}}},
    }
    if label.startswith("content."):
        return {"comet_sections": {"content": {"story": places[label.removeprefix("content.")]}}}
    return places[label]


@pytest.mark.parametrize("label", [label for label, _w, _h in KNOWN_TEXT_PATHS])
def test_the_known_paths_are_where_the_parser_reads(label: str) -> None:
    story = _where(label, "words")
    if label != "attached_story":
        assert own_text(story)[:2] == ("words", label)
    assert known_paths(story)[label] == TEXT
    assert known_paths(_where(label, "  "))[label] == EMPTY
    assert known_paths({})[label] == ABSENT


def test_null_and_the_second_rich_message_place() -> None:
    story = {"comet_sections": {"message": {"story": {"rich_message": [{"text": "x"}]}}}}
    assert own_text(story)[1] == "sections.rich_message"
    assert known_paths(story)["sections.rich_message"] == TEXT
    nulls = known_paths({"message": None, "comet_sections": {"message_container": None}})
    assert nulls["message"] == NULL
    assert nulls["sections.message_container"] == NULL
    assert nulls["sections.message"] == ABSENT


# -- skeletons ------------------------------------------------------------------------------------


def test_skeleton_of_a_background_text_story() -> None:
    shape = skeleton(background_story())
    body = (
        "comet_sections",
        "content",
        "story",
        "comet_sections",
        "message",
        "story",
        "body",
        "text",
    )
    assert shape.lengths[body] == (len(BACKGROUND), len(BACKGROUND))
    assert shape.typenames[body[:5]] == {"CometFeedStoryBackgroundTextRenderingStrategy"}
    assert shape.typenames[body[:7]] == {"TextWithEntities"}
    assert shape.typenames[()] == {"Story"}
    assert (*body[:6], "id") in shape.lengths  # an opaque id as a value: only its length
    ranges = (*body[:6], "text_format_metadata", "ranges_by_id", ID, "entity", "url")
    assert shape.lengths[ranges] == (55, 55)  # a profile id as a key reads <id>
    assert shape.attachments == {"Photo", "StoryAttachment", "StoryAttachmentPhotoStyleRenderer"}
    assert shape.known["content.sections.message"] == EMPTY
    assert shape.known["content.message"] == NULL
    assert shape.known["content.sections.message_container"] == ABSENT
    assert ("__typename",) not in shape.lengths  # typenames are shown as such, not as strings


def test_skeleton_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    deep: dict[str, Any] = {"text": "x" * 20}
    for i in range(diagnostics.MAX_DEPTH):  # the text is MAX_DEPTH + 1 keys down
        deep = {f"k{i}": deep}
    shape = skeleton({"__typename": "Story", "a": deep, "b": ["y" * 30, "z" * 12], "c": 7})
    assert shape.deeper == 1
    assert shape.lengths == {("b", "[]"): (12, 30)}
    monkeypatch.setattr(diagnostics, "MAX_PATHS", 1)
    assert skeleton({"a": "x" * 12, "b": "y" * 12}).overflow == 1
    monkeypatch.setattr(diagnostics, "MAX_NODES", 2)
    assert skeleton({"a": {"b": {"c": "x" * 12}}}).lengths == {}
    assert skeleton(["x" * 12]).known == {}


BODY = ("comet_sections", "content", "story", "comet_sections", "message", "story", "body", "text")
SHARE = (
    "comet_sections",
    "content",
    "story",
    "comet_sections",
    "attached_story",
    "story",
    "attached_story",
)
"""Where ``stories._attached_level`` finds a shared story that sits deepest: 7 keys down."""


def test_a_short_text_is_listed_at_any_path() -> None:
    """A non-blank string under a ``text`` key is listed at any length (a short post is still
    a post). Other short strings, blank texts and strings in a list under ``text`` are not."""
    words = "До Варна"
    assert len(words) < MIN_LENGTH
    assert skeleton(background_story(words)).lengths[BODY] == (len(words), len(words))
    assert length(tree(_drawn(background_story(words)))[".".join(BODY)]) == str(len(words))
    blank = (*BODY[:4], "message_container", "story", "message", "text")
    assert blank not in skeleton(container_story()).lengths  # " " is blank
    assert skeleton({"id": "x" * 11, "text": ["x" * 11], "rich": [{"text": "x"}]}).lengths == {
        ("rich", "[]", "text"): (1, 1)
    }


def test_a_shape_is_structure_not_lengths() -> None:
    """Stories alike where the parser looks are one shape whatever their strings' lengths: an
    11-character author name (not listed) and a 21-character one (listed). Another renderer
    in the message section is another shape."""
    short = background_story("Варна-София", post_id="5000000000000601")
    long_ = background_story(post_id="5000000000000602")
    for story, author in ((short, "Иван Петров"), (long_, AUTHOR)):
        story["comet_sections"]["context_layout"]["story"]["actors"][0]["name"] = author
    other = background_story(post_id="5000000000000603")
    default = "CometFeedStoryDefaultMessageRenderingStrategy"
    _content(other)["comet_sections"]["message"]["__typename"] = default
    a, b, c = (skeleton(story) for story in (short, long_, other))
    context_name = ("comet_sections", "context_layout", "story", "actors", "[]", "name")
    assert context_name not in a.lengths
    assert context_name in b.lengths
    assert a.shape == b.shape != c.shape
    scan = RawScan(kept=1, steps=1, samples=[Sample(s, "feed", "Q") for s in (a, b, c)])
    lines = "\n".join(render_scan(scan, zone=ZONE))
    assert "in 2 shape(s)" in lines
    assert "| A | 2 |" in lines
    assert "| B | 1 |" in lines
    background_kind = "empty <CometFeedStoryBackgroundTextRenderingStrategy>"
    assert f"| `content.sections.message` | {background_kind} | empty <{default}> |" in lines
    # Drawn for the stories whose name is long enough (the fixture's own is 21 too).
    assert tree(lines)["comet_sections.context_layout.story.actors[].name"] == "21 · 2/3"


def shared_story(section: dict[str, Any], post_id: str, *, levels: int = 1) -> dict[str, Any]:
    """A photo post that shares a story (``levels`` shares deep) whose message section is
    ``section``, each share where the parser finds it deepest (``SHARE``)."""
    inner: dict[str, Any] = {
        "__typename": "Story",
        "comet_sections": {"content": {"story": {"comet_sections": {"message": section}}}},
    }
    for _ in range(levels - 1):
        inner = {"__typename": "Story", **dig_set(SHARE, inner)}
    node = photo_story(post_id)
    _content(node)["comet_sections"]["attached_story"] = dig_set(SHARE[4:], inner)["attached_story"]
    return node


def dig_set(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """``{path[0]: {path[1]: ... value}}``."""
    for key in reversed(path):
        value = {key: value}
    out: dict[str, Any] = value
    return out


def test_a_share_fifteen_keys_down_is_drawn(tmp_path: Path) -> None:
    """The parser reads a share's text 15 keys down; an unknown renderer at that depth, in a
    share that comes out empty, is drawn with its length."""
    read = shared_story({"story": {"message": {"text": DEFERRED}}}, "5000000000000701")
    unknown = shared_story(
        {
            "__typename": "CometFeedStoryBackgroundTextRenderingStrategy",
            "story": {"body": {"__typename": "TextWithEntities", "text": BACKGROUND}},
        },
        "5000000000000702",
    )
    raw = tmp_path / "raw"
    save(raw, feed_step(read, unknown), at=T0)
    scan = scan_raw(raw)
    assert (scan.stories, scan.empty) == (2, 1)  # the parser reads the first share's text
    body = (*SHARE, *BODY)
    assert len(body) == 15
    assert scan.samples[0].skeleton.deeper == 0
    drawn = tree("\n".join(render_scan(scan, zone=ZONE)))
    assert length(drawn[".".join(body)]) == str(len(BACKGROUND))
    assert "(+" not in "\n".join(render_scan(scan, zone=ZONE))  # nothing left unlisted


def test_the_depth_is_as_deep_as_the_parser_reads(tmp_path: Path) -> None:
    """``MAX_DEPTH`` is the parser's reach. A share of a share of a share whose message
    section keeps the words 12 walk levels down is read, ``MAX_DEPTH`` keys down, and listed;
    one level more is neither read nor listed."""

    def story(levels: int, post_id: str) -> dict[str, Any]:
        section: dict[str, Any] = {"message": {"text": DEFERRED}}
        for i in range(levels):
            section = {f"k{i}": section}
        return shared_story(section, post_id, levels=MAX_SHARE_DEPTH)

    raw = tmp_path / "raw"
    reached, beyond = story(12, "5000000000000801"), story(13, "5000000000000802")
    save(raw, feed_step(reached), at=T0)
    save(raw, feed_step(beyond), at=T0 - 1, read_id=2)
    scan = scan_raw(raw)
    assert (scan.stories, scan.empty) == (2, 1)
    deepest = [p for p in skeleton(reached).lengths if p[-2:] == ("message", "text")]
    assert [len(p) for p in deepest] == [diagnostics.MAX_DEPTH]
    assert skeleton(reached).deeper == 0
    assert scan.samples[0].skeleton.deeper == 1  # the one the parser misses: not listed


# -- the saved steps ------------------------------------------------------------------------------


def test_unknown_text_paths_show_up_with_their_lengths(tmp_path: Path, empty_db: Path) -> None:
    raw = tmp_path / "raw"
    old = feed_step(photo_story("5000000000000201"), text_story("5000000000000202"))
    save(raw, old, at=T0 - 600)
    new = feed_step(background_story(), container_story(), photo_story(), text_story())
    save(raw, new, at=T0, read_id=2)
    report = generate(empty_db, zone=ZONE, now=T0 + 60, raw_dir=raw)
    drawn = tree(report)
    background = "comet_sections.content.story.comet_sections.message.story.body.text"
    container = (
        "comet_sections.content.story.comet_sections.message_container.story.message"
        ".text_with_entities.text"
    )
    assert length(drawn[background]) == str(len(BACKGROUND))
    assert length(drawn[container]) == str(len(CONTAINER))
    assert "message <CometFeedStoryBackgroundTextRenderingStrategy>" in report
    assert "Saved steps read: 2 of 2" in report
    assert "Distinct posts in them: 6, with empty text: 4" in report
    assert "in 3 shape(s)" in report
    assert "| A | 2 | 0 | feed 2 |" in report  # both photo posts: no section, rightly empty
    background_kind = "empty <CometFeedStoryBackgroundTextRenderingStrategy>"
    container_kind = "empty <CometFeedStoryMessageContainerRenderingStrategy>"
    assert f"| `content.sections.message` | absent | {background_kind} | absent |" in report
    assert f"| `content.sections.message_container` | absent | absent | {container_kind} |" in (
        report
    )


def test_nothing_the_stories_say_reaches_the_report(tmp_path: Path, empty_db: Path) -> None:
    raw = tmp_path / "raw"
    stories = [background_story(), container_story(), photo_story(), text_story()]
    save(raw, feed_step(*stories), at=T0)
    deferred = photo_story("5000000000000105")
    save(raw, stream_step(deferred, parts=[message_part(0, DEFERRED)]), at=T0 + 1, read_id=2)
    report = generate(empty_db, zone=ZONE, now=T0 + 60, raw_dir=raw)
    assert "## Parser diagnostics (structure only)" in report
    secrets = {DEFERRED}
    for story in [*stories, deferred]:
        secrets |= planted(story)
    assert {BACKGROUND, CONTAINER, AUTHOR, "5000000000000102", "400000000000001"} <= secrets
    assert [s for s in secrets if s in report] == []
    assert not [c for c in report if "\u0400" <= c <= "\u04ff"]  # no Cyrillic at all


def test_a_deferred_message_section_is_counted_at_its_story(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    empty_first, empty_streamed, with_text = (
        photo_story("5000000000000301"),
        photo_story("5000000000000302"),
        text_story("5000000000000303"),
    )
    other = "Друг текст, който вече е в историята."
    parts = [
        message_part(0, DEFERRED),
        message_part(1, DEFERRED + " Обадете се."),
        message_part(2, other),
        {"label": DEFER, "path": ["viewer", "news_feed"], "data": {"page_info": {}}},
        {"label": None, "path": ["viewer", "other_feed", "edges", 0, "node", "x"], "data": {}},
        {"path": ["viewer", "news_feed", "edges", 9, "node"], "data": {"id": "y" * 40}},
        {"label": "Етикет с текст", "path": [*message_part(0, "")["path"][:5], "feedback"]},
        {
            "label": DEFER,
            "path": [*message_part(0, "")["path"][:5], "ranges_by_id", "100064455566778", "ключ"],
            "data": {"entity": {"url": "https://www.facebook.com/profile.php?id=100064455566778"}},
        },
    ]
    save(raw, stream_step(empty_first, empty_streamed, with_text, parts=parts), at=T0)
    scan = scan_raw(raw)
    assert scan.empty == 2
    below = ("comet_sections", "content", "story", "comet_sections", "message")
    stats = scan.fragments[(below, DEFER)]
    assert (stats.frames, stats.at_empty) == (3, 2)
    assert stats.lengths[("story", "message", "text")][1] == len(DEFERRED + " Обадете се.")
    assert scan.fragments[((), "(none)")].frames == 1  # a part aimed at the node itself
    assert scan.fragments[(("feedback",), ODD)].frames == 1  # a label that is not a name
    lines = "\n".join(render_scan(scan, zone=ZONE))
    assert (
        f"| `comet_sections.content.story.comet_sections.message` | `{DEFER}` | 3 | 2 |"
        f" `story.message.text` {len(other)}-{len(DEFERRED) + 12}"
    ) in lines
    assert "| `(the story node)` | `(none)` | 1 | 0 | `id` 40 |" in lines
    # The keys below the node are masked like any key: an id reads <id>, a non-name <key>.
    assert scan.fragments[(("ranges_by_id", ID, KEY), DEFER)].at_empty == 1
    assert f"| `ranges_by_id.<id>.<key>` | `{DEFER}` | 1 | 1 | `entity.url` 55 |" in lines
    assert "100064455566778" not in lines
    assert not CYRILLIC.search(lines)


def test_the_classes_of_posts_without_words(tmp_path: Path, empty_db: Path) -> None:
    """The phase-0 shapes (the attachments fixture): distinct posts by class, and each
    shape's class; none of the posts' words, ids or links reach the report."""
    raw = tmp_path / "raw"
    for index, step in enumerate(load_steps("feed_attachments_synthetic.json")):
        save(raw, step, at=T0, index=index)
    save(raw, feed_step(photo_story(), text_story()), at=T0 - 1, read_id=2)  # a read again
    scan = scan_raw(raw)
    assert scan.classes == {"attachment_text": 3, "share_without_comment": 2, "media_only": 2}
    report = generate(empty_db, zone=ZONE, now=T0 + 60, raw_dir=raw)
    assert (
        "distinct posts: `attachment_text` 3, `share_without_comment` 2, `media_only` 2,"
        " `unexplained` 0"
    ) in report
    rows = [line for line in report.splitlines() if re.match(r"\| [A-Z] \|", line)]
    classes = sorted(row.rsplit(" | ", 1)[1].rstrip(" |") for row in rows)
    assert classes == [
        *["attachment_text 1"] * 3,  # a card's text only: drawn too, to confirm its paths
        *["media_only 1"] * 2,  # the fixture's photo, and the older read's: two shapes
        *["share_without_comment 1"] * 2,
    ]
    assert "with empty text: 4;" in report
    body = (FIXTURES / "feed_attachments_synthetic.json").read_text(encoding="utf-8")
    for value in (
        "Synthetic link title",
        "SYNTHETIC-HOMES.BG",
        "corner sofa",
        "Synthetic notice",
        "Synthetic caption",
        "5000000000000301",
        "example.invalid",
    ):
        assert value in body
        assert value not in report
    assert not CYRILLIC.search(report)  # the price "250 лв." among them


# -- the script of the texts ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "script"),
    [
        ("Продавам кола", Script.CYRILLIC),
        ("Здравей 2+1, тел. 0888", Script.CYRILLIC),  # digits and signs do not count
        ("Ѐ ѓ ї ґ", Script.CYRILLIC),  # the letters of other Cyrillic alphabets
        ("Selling a car", Script.LATIN),
        ("Ünïcödé café", Script.LATIN),
        ("ｆｕｌｌ ｗｉｄｔｈ", Script.LATIN),  # FULLWIDTH LATIN SMALL LETTER F
        ("Ελληνικά and English", Script.LATIN),  # another script's letters do not count
        ("Продавам BMW", Script.MIXED),
        ("IMOT.BG\nДвустаен", Script.MIXED),  # a card's site's name after Cyrillic words
        ("123 456 !?", Script.OTHER),
        ("😀🚗", Script.OTHER),
        ("𝐅𝐨𝐫 𝐬𝐚𝐥𝐞", Script.LATIN),  # MATHEMATICAL BOLD SMALL O: a styled Latin letter
        ("𝖲𝖠𝖫𝖤 𝗌𝖺𝗇𝗌", Script.LATIN),  # mathematical sans-serif
        ("𝐒𝐀𝐋𝐄 квартира", Script.MIXED),
        ("ℒ𝒾𝓋ℯ", Script.LATIN),  # script letters, some from the letterlike symbols
        ("Ⓢⓐⓛⓔ 🅢🅐🅛🅔", Script.LATIN),  # symbols named as Latin letters
        ("№ 5, 10 ㎡, ✝ квартира", Script.CYRILLIC),  # signs NFKC spells in letters do not
        ("🇧🇬 флаг", Script.CYRILLIC),  # a flag's regional indicator letters are no script
        ("αβγ δ", Script.OTHER),
        ("", Script.OTHER),
        (None, Script.OTHER),
    ],
)
def test_script_of(value: str | None, script: Script) -> None:
    assert script_of(value) is script


def worded_story(post_id: str, words: str | None, **extra: Any) -> dict[str, Any]:
    """A photo post with ``words`` as its message (none: a photo alone), and ``extra`` keys
    at its root."""
    node = photo_story(post_id)
    if words is not None:
        _content(node)["message"] = {"__typename": "TextWithEntities", "text": words}
    node.update(extra)
    return node


def script_section(report: str) -> str:
    return report.split("### The script of the post texts in the saved payloads", 1)[1]


def script_rows(section: str) -> dict[str, int]:
    """``label -> distinct posts`` of the script table."""
    rows = {}
    for label in (*SCRIPT_LABELS.values(), "**all**"):
        line = next(ln for ln in section.splitlines() if ln.startswith(f"| {label} |"))
        rows[label] = int(line.split(" | ")[1])
    return rows


TRANSLATED = "Преведено автоматично от английски"


def test_the_script_of_the_texts(tmp_path: Path, empty_db: Path) -> None:
    """Distinct posts by the script of their final text, and those with a translation key:
    counts and shares only, never a word, a key's value or an id."""
    raw = tmp_path / "raw"
    ids = [f"50000000000004{n:02d}" for n in range(8)]
    save(
        raw,
        feed_step(
            worded_story(ids[0], "Продавам детска количка"),
            worded_story(ids[1], "Selling a pushchair, barely used"),
            worded_story(ids[2], "Продавам BMW X5"),
            worded_story(ids[3], "Πωλείται 2+1"),  # Greek: neither script
            worded_story(ids[4], None),  # a photo alone: no text
            worded_story(
                ids[5],
                "Търся превоз до Варна",
                translatability_for_viewer={"source_dialect_name": TRANSLATED},
            ),
            worded_story(ids[6], "Looking for a ride", translated_message={"text": TRANSLATED}),
        ),
        at=T0,
    )
    again = worded_story(ids[0], "Продавам детска количка", translation={"text": TRANSLATED})
    save(raw, feed_step(again, worded_story(ids[7], "Трамвай")), at=T0 - 1, read_id=2)
    scan = scan_raw(raw)
    assert scan.stories == 8  # the post read again counts once, as the newest step saw it
    assert scan.scripts == {"cyrillic": 3, "latin": 2, "mixed": 1, "other": 2}
    assert scan.translated == 2
    assert scan.translation_keys == {"translatability_for_viewer": 1, "translated_message": 1}
    report = generate(empty_db, zone=ZONE, now=T0 + 60, raw_dir=raw)
    section = script_section(report)
    assert script_rows(section) == {
        "Cyrillic only": 3,
        "Latin only": 2,
        "Mixed (Cyrillic and Latin letters)": 1,
        "Other or none (no Cyrillic or Latin letter, or no text)": 2,
        "**all**": 8,
    }
    assert "| Cyrillic only | 3 | 37.5 % |" in section
    assert "| Other or none (no Cyrillic or Latin letter, or no text) | 2 | 25.0 % |" in section
    assert (
        "anywhere in the story: 2 of 8 (25.0 %); keys: `translatability_for_viewer` 1,"
        " `translated_message` 1."
    ) in section
    assert not CYRILLIC.search(report)
    for words in ("Selling", "pushchair", "Looking for a ride", "Πωλείται", "BMW", *ids):
        assert words not in report
    assert report.index("### Stories with empty text") < report.index("### The script of")


def link_card(title: str, source: str = "IMOT.BG") -> dict[str, Any]:
    return {
        "__typename": "StoryAttachment",
        "styles": {
            "__typename": "StoryAttachmentShareStyleRenderer",
            "attachment": {"title_with_entities": {"text": title}, "source": {"text": source}},
        },
    }


def test_the_script_of_the_authors_words_leaves_the_cards_out(
    tmp_path: Path, empty_db: Path
) -> None:
    """A translated post reads Latin only in its authors' words whatever its card says, and a
    Latin site's name does not make a Cyrillic post mixed there; the final text, card lines
    included, is its own column."""
    raw = tmp_path / "raw"
    ids = [f"50000000000006{n:02d}" for n in range(5)]
    translated = worded_story(ids[0], "Selling a pushchair")  # Facebook's English
    _content(translated)["attachments"] = [link_card("Детска количка")]
    cyrillic = worded_story(ids[1], "Продавам детска количка")
    _content(cyrillic)["attachments"] = [link_card("Детска количка, Пловдив")]
    card_only = worded_story(ids[2], None)
    _content(card_only)["attachments"] = [link_card("Детска количка", source="ОЛХ")]
    styled = worded_story(ids[3], "𝐅𝐨𝐫 𝐬𝐚𝐥𝐞, 𝖼𝗁𝖾𝖺𝗉")  # mathematical bold and sans-serif
    mixed = worded_story(ids[4], "𝐒𝐀𝐋𝐄 квартира")
    save(raw, feed_step(translated, cyrillic, card_only, styled, mixed), at=T0)
    scan = scan_raw(raw)
    assert scan.stories == 5
    assert scan.scripts == {"latin": 2, "cyrillic": 1, "mixed": 1, "other": 1}
    assert scan.final_scripts == {"latin": 1, "cyrillic": 1, "mixed": 3}
    section = script_section(generate(empty_db, zone=ZONE, now=T0 + 60, raw_dir=raw))
    assert "| Script | Authors' words | Share | Final text | Share |" in section
    assert "| Latin only | 2 | 40.0 % | 1 | 20.0 % |" in section
    assert "| Mixed (Cyrillic and Latin letters) | 1 | 20.0 % | 3 | 60.0 % |" in section
    assert "| **all** | 5 | 100.0 % | 5 | 100.0 % |" in section


def test_the_script_section_without_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert render_scripts(None)[-1] == "The saved payloads were not read."
    assert render_scripts(RawScan(kept=2, steps=2))[-1] == "No post in the saved steps read."
    monkeypatch.setattr(diagnostics, "MAX_KEY_NAMES", 2)
    scan = RawScan(
        stories=4,
        scripts=Counter({"latin": 4}),
        translated=3,
        translation_keys=Counter({"translation": 3, "b_translated": 1, "a_translated": 1}),
    )
    lines = "\n".join(render_scripts(scan))
    assert "| Cyrillic only | 0 | 0.0 % |" in lines
    assert "| Latin only | 4 | 100.0 % |" in lines
    assert "3 of 4 (75.0 %); keys: `translation` 3, `a_translated` 1, +1 more." in lines


def shape_trees(report: str) -> dict[str, dict[str, str]]:
    """Each shape's attachments drawing as ``letter -> dotted path -> what follows the key``
    (its typenames, then ``: length · stories`` or `` · stories``)."""
    out: dict[str, dict[str, str]] = {}
    for part in report.split("\nShape ")[1:]:
        letter, rest = part.split(":\n\n```text\n", 1)
        drawn: dict[str, str] = {}
        stack: list[str] = []
        for line in rest.split("\n```", 1)[0].splitlines()[1:]:
            level = (len(line) - len(line.lstrip(" "))) // 2
            label = line.strip().split(" ", 1)[0].split(":", 1)[0]
            del stack[level - 1 :]
            stack.append(label)
            drawn[".".join(stack)] = line.strip()[len(label) :].strip()
        out[letter] = drawn
    return out


def letter_of(report: str, typename: str) -> str:
    """The shape whose attachment typenames name ``typename``."""
    (row,) = [r for r in report.splitlines() if re.match(r"\| [A-Z] \|", r) and typename in r]
    return row[2]


CARDS = "comet_sections.content.story.attachments[]"
SHARED_CARDS = "comet_sections.content.story.attached_story.attachments[]"


def test_each_shape_draws_its_attachments(tmp_path: Path, empty_db: Path) -> None:
    """The subtrees under ``attachments[]`` and ``attached_story.attachments[]``, every string
    at any length and every typename, ``style_infos`` too: where a live run's link and listing
    words really sit. Nothing of what they say."""
    raw = tmp_path / "raw"
    for index, step in enumerate(load_steps("feed_attachments_synthetic.json")):
        save(raw, step, at=T0, index=index)
    report = generate(empty_db, zone=ZONE, now=T0 + 60, raw_dir=raw)
    trees = shape_trees(report)
    link = trees[letter_of(report, "ExternalUrl")]
    card = f"{CARDS}.styles.attachment"
    assert link[f"{CARDS}"] == "<StoryAttachment>"
    assert link[f"{card}.title_with_entities.text"] == ": 56 · 1/1"
    assert link[f"{card}.description.text"] == ": 74 · 1/1"
    assert link[f"{card}.source.text"] == ": 18 · 1/1"  # SYNTHETIC-HOMES.BG, 18 characters
    assert link[f"{card}.style_infos[]"] == "<ExternalShareAttachmentStyleInfo> · 1/1"
    assert link[f"{card}.web_link.url"] == ": 61 · 1/1"  # drawn, never read
    marketplace = trees[letter_of(report, "CommerceProductItemStyleInfo")]
    assert marketplace[f"{card}.style_infos[]"] == (
        "<CommerceProductItemStyleInfo|FeedStandardAttachmentStyleInfo|"
        "FutureOfFeedAttachmentStyleInfo> · 1/1"
    )
    assert marketplace[f"{card}.target.listing_price.formatted_amount"] == ": 7 · 1/1"
    shared = trees[letter_of(report, "GroupCommerceProductItem 1, StoryAttachment 1,")]
    target = f"{SHARED_CARDS}.styles.attachment.target"
    assert shared[f"{target}.marketplace_listing_title"] == ": 35 · 1/1"
    assert shared[f"{target}.formatted_price.text"] == ": 6 · 1/1"
    unavailable = trees[letter_of(report, "StoryAttachmentUnavailableStyleRenderer")]
    assert unavailable[f"{CARDS}.styles"] == "<StoryAttachmentUnavailableStyleRenderer>"
    bare = [r[2] for r in report.splitlines() if re.match(r"\| [A-Z] \|", r) and "| - |" in r]
    assert f"No attachments in shape(s) {bare[0]}." in report
    assert len(trees) == 5  # six shapes, one without attachments
    body = (FIXTURES / "feed_attachments_synthetic.json").read_text(encoding="utf-8")
    for value in ("Synthetic link title", "SYNTHETIC-HOMES.BG", "700000000000011", "https://"):
        assert value in body
        assert value not in report
    assert not CYRILLIC.search(report)


def test_the_attachments_drawing_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """At most ``ATTACHMENT_LINES`` lines a shape, the parser's keys and the style infos
    first; strings too deep or beyond the path cap are counted, not listed."""
    deep: dict[str, Any] = {"text": "x" * 5}
    for i in range(diagnostics.ATTACHMENT_DEPTH):
        deep = {f"k{i}": deep}
    card = {
        "__typename": "StoryAttachment",
        **{f"noise{i}": {"url": "u" * 30} for i in range(20)},
        "styles": {"attachment": {"title_with_entities": {"text": "Заглавие"}}},
        "style_infos": [{"__typename": "SomeStyleInfo"}],
        "deep": deep,
    }
    story = {"__typename": "Story", "attachments": [card]}
    sample = Sample(
        skeleton(story), "feed", "Q", attachments=diagnostics.attachment_skeleton(story)
    )
    monkeypatch.setattr(diagnostics, "ATTACHMENT_LINES", 8)
    lines = "\n".join(render_scan(RawScan(kept=1, steps=1, samples=[sample]), zone=ZONE))
    drawn = shape_trees(lines)["A"]
    assert drawn["attachments[].styles.attachment.title_with_entities.text"] == ": 8 · 1/1"
    assert drawn["attachments[].style_infos[]"] == "<SomeStyleInfo> · 1/1"
    assert not [path for path in drawn if "noise" in path]  # first in the payload, drawn last
    assert "20 more path(s) not drawn: a shape's drawing is capped at 8 lines." in lines
    assert "1 string(s) deeper or beyond the path cap, not listed." in lines
    monkeypatch.setattr(diagnostics, "MAX_PATHS", 1)
    capped = diagnostics.attachment_skeleton(story)
    assert capped is not None and (capped.overflow, capped.deeper) == (20, 1)
    assert diagnostics.attachment_skeleton({"attachments": []}) is None
    assert diagnostics.attachment_skeleton(["not a story"]) is None


def test_typenames_are_drawn_as_deep_as_the_strings() -> None:
    """A typename ``ATTACHMENT_DEPTH`` keys below the list is drawn; one key deeper, beside a
    string too deep to list, it is not. (The shapes table names it: the story's skeleton goes
    ``MAX_DEPTH`` keys down.)"""
    card: dict[str, Any] = {
        "__typename": "EdgeStyleInfo",
        "deeper": {"__typename": "TooDeepStyleInfo", "text": "x" * 5},
    }
    for i in range(diagnostics.ATTACHMENT_DEPTH):
        card = {f"k{i}": card}
    story = {"__typename": "Story", "attachments": [card]}
    drawn = diagnostics.attachment_skeleton(story)
    assert drawn is not None and drawn.deeper == 1
    assert set().union(*drawn.typenames.values()) == {"EdgeStyleInfo"}
    keys_below = [sum(k != ARRAY for k in path[1:]) for path in drawn.typenames]
    assert max(keys_below) == diagnostics.ATTACHMENT_DEPTH
    sample = Sample(skeleton(story), "feed", "Q", attachments=drawn)
    lines = "\n".join(render_scan(RawScan(kept=1, steps=1, samples=[sample]), zone=ZONE))
    tree = shape_trees(lines)["A"]
    assert [cell for cell in tree.values() if "StyleInfo" in cell] == ["<EdgeStyleInfo> · 1/1"]
    assert "1 string(s) deeper or beyond the path cap, not listed." in lines


def test_scan_bounds_and_damage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "raw"
    assert scan_raw(raw).kept == 0  # no directory yet
    for i in range(3):
        step = feed_step(photo_story(f"50000000000004{i}1"), photo_story(f"50000000000004{i}2"))
        save(raw, step, at=T0 + i, read_id=i + 1)
    (raw / "9999999999-r00000009-s00.step.json.gz").write_bytes(b"not gzip")
    joins = feed_step(photo_story("5000000000000499"))
    save(raw, joins, at=T0 - 10, read_id=8, surface=Surface.JOINS)
    scan = scan_raw(raw, sample=3)
    assert (scan.kept, scan.unreadable, scan.steps) == (5, 1, 2)  # stops once 3 are found
    assert (len(scan.samples), scan.empty) == (3, 4)
    assert scan_raw(raw, max_steps=1).steps == 0  # the damaged file was the newest
    assert scan_raw(raw, since=T0 + 2.5).kept == 2  # T0 + 2 (a second's slack), the damaged
    everything = scan_raw(raw)
    assert (everything.steps, everything.stories) == (4, 6)  # the joins step has no stories

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError

    monkeypatch.setattr(diagnostics, "read_story", boom)
    assert scan_raw(raw).raised == 6


def test_a_group_step_and_a_highlight(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    unit = {"__typename": "GroupHighlightPostUnit", "id": "77", "story": photo_story()}
    first = {"data": {"node": {"highlight_units": {"edges": [{"node": unit}]}}}}
    body = frames_body(first, name="Заявка за група")  # not a query name: not printed
    step = Step(final_url=FEED_URL, status=200, graphql=(body,), dom=DomFacts())
    save(raw, step, at=T0, surface=Surface.GROUP, group_id="400000000000001")
    lines = "\n".join(render_scan(scan_raw(raw), zone=ZONE))
    assert "| A | 1 | 0 | group 1, highlight 1 | <?> 1 | Story 1 |" in lines


def test_stories_in_the_page_itself(tmp_path: Path) -> None:
    """The SSR's stories: a header is dropped, a post seen twice is one, and a ``message``
    that is null and one that is absent are two shapes (the query asked for the field, or did
    not)."""
    raw = tmp_path / "raw"
    header = {"__typename": "GroupsSectionHeaderUnit", "title": {"text": "New posts today"}}
    null_message, absent_message = photo_story("5000000000000501"), photo_story("5000000000000502")
    del _content(absent_message)["message"]
    edges = [{"node": n} for n in (header, null_message, null_message, absent_message)]
    result = {"data": {"viewer": {"news_feed": {"edges": edges}}}}
    script = json.dumps(
        {"require": [["RelayPrefetchedStreamCache", {"__bbox": {"result": result}}]]}
    )
    page = f'<html><script type="application/json">{script}</script></html>'
    save(raw, Step(final_url=FEED_URL, status=200, document=page.encode()), at=T0)
    scan = scan_raw(raw)
    assert (scan.stories, scan.empty) == (2, 2)
    lines = render_scan(scan, zone=ZONE)
    assert "| A | 1 | 0 | feed 1 | ssr 1 | Story 1 |" in "\n".join(lines)
    assert "| `content.message` | null | absent |" in lines


def test_fragment_lengths_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "MAX_PATHS", 1)
    into: dict[tuple[str, ...], tuple[int, int]] = {("a",): (20, 20)}
    diagnostics._merge(into, {("a",): (12, 12), ("b",): (30, 30)})
    assert into == {("a",): (12, 20)}


def test_render_without_payloads_and_its_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    assert render_scan(None, zone=ZONE)[-1] == "The saved payloads were not read."
    assert "No saved payloads" in render_scan(RawScan(), zone=ZONE)[-1]
    assert "- No story with empty text in these steps." in render_scan(
        RawScan(kept=1, steps=1), zone=ZONE
    )
    samples = [
        Sample(
            skeleton(
                {
                    "__typename": "Story",
                    "media": {"__typename": f"Kind{i}"},  # one shape each
                    f"field{i}": "x" * 12,
                    "shared": "y" * (12 + i),
                }
            ),
            surface="feed",
            query=f"Query{i % 5}",
        )
        for i in range(30)
    ]
    scan = RawScan(kept=1, steps=1, samples=samples)
    monkeypatch.setattr(diagnostics, "MAX_TREE_LINES", 12)
    lines = "\n".join(render_scan(scan, zone=ZONE))
    assert "the 26 newest with empty text or with only a card's text" in lines
    assert "(`attachment_text`), in 26 shape(s)" in lines  # A to Z
    assert "No attachments in shape(s) A, B, C," in lines
    assert "| Z | 1 |" in lines
    assert "shared: 12-37 · 26/26\n" in lines  # in every shape: no letters
    assert "field0: 12 · 1/26 · A" in lines
    assert "16 rarer path(s) not drawn" in lines
    wide = [
        Sample(skeleton({"__typename": k, "v": "x" * 12}), surface="feed", query="Q")
        for k in ("A", "B", "C", "D")
    ]
    assert "(story) <A|B|C|+1>" in render_scan(RawScan(kept=1, samples=wide), zone=ZONE)


def test_the_fragment_table_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "MAX_FRAGMENTS", 2)
    scan = RawScan(kept=1, steps=1)
    for i in range(4):
        scan.fragments[((f"k{i}",), "L")] = diagnostics.FragmentStats(frames=i + 1)
    lines = render_scan(scan, zone=ZONE)
    assert lines[-1] == "| … 2 more kinds | | | | |"
    assert "| `k3` | `L` | 4 | 0 | - |" in lines


# -- the text-path histogram -------------------------------------------------------------------


async def _reads(path: Path) -> Path:
    clock = FakeClock(start=T0 - 3 * 86400)
    async with Store(path, clock=clock) as store:
        for surface, paths, no_text, age, classes, cards in (
            (Surface.FEED, {"content.message": 5, "(none)": 2}, 0, 2 * 86400, {}, 2),
            (
                Surface.GROUP,
                {"content.sections.message": 3, "(none)": 1, "odd label!": 1},
                1,
                60,
                {"share_without_comment": 1, "odd class!": 1},
                1,
            ),
            (
                Surface.FEED,
                {"attached_story": 1, "(none)": 4, "listing": 1},
                2,
                30,
                {"unexplained": 2, "attachment_text": 1},
                None,  # a read from before the counter
            ),
            (Surface.JOINS, {"(none)": 9}, 0, 20, {"unexplained": 9}, 4),
        ):
            clock.jump_wall(T0 - age - clock.time())
            read = await store.start_read(surface)
            health: dict[str, Any] = {
                "text_paths": paths,
                "missing": {"no_text": no_text},
                "text_classes": classes,
            }
            if cards is not None:
                health["card_beside_words"] = cards
                health["translation_fields"] = cards + 1
            await store.finish_read(
                read,
                coverage=Coverage(),
                steps=1,
                outcome=ReadOutcome.COMPLETE,
                stats={"health": health},
            )
    return path


def test_text_path_histogram(tmp_path: Path) -> None:
    data = load(asyncio.run(_reads(tmp_path / "state.sqlite")))
    paths = text_paths(data, now=T0)
    assert paths.rows["content.message"] == [5, 0, 0]  # two days old: not in the last 24 h
    assert paths.rows["content.sections.message"] == [0, 3, 3]
    assert paths.rows["<other>"] == [0, 1, 1]
    assert paths.rows["(none)"] == [6, 1, 5]  # the joins read is not counted
    assert paths.rows["attachment.listing"] == [1, 0, 1]  # recorded as "listing" before
    assert "listing" not in paths.rows
    assert list(paths.rows)[-2:] == ["<other>", "(none)"]
    assert paths.stories == [13, 5, 11]
    assert paths.unread == [2, 1, 3]
    report = render(data, zone=ZONE, now=T0)
    assert "| `content.sections.message` | 0 | 3 | 3 |" in report
    assert "| `attachment.listing` | 1 | 0 | 1 |" in report
    assert "| `attachment.link` | 0 | 0 | 0 |" in report
    assert "| **all stories** | 13 | 5 | 11 |" in report
    assert "feed 6 of 13 (46.2 %), group 1 of 5 (20.0 %), last 24 h 5 of 11 (45.5 %)" in report
    assert "(deferred as `no_text`): feed 2, group 1, last 24 h 3." in report
    # Counted against the stories of the reads that record them: the feed read from before
    # the counters (6 stories, in the last 24 h too) is left out of both sides, never read
    # as "none of its stories had one"; the joins read is not counted at all.
    assert paths.card_beside_words == Measured(found=[2, 1, 1], of=[7, 5, 5], without=1)
    assert paths.translation_fields == Measured(found=[3, 2, 2], of=[7, 5, 5], without=1)
    assert (
        "- With a key that speaks of a translation (`translat` in its name, whatever it holds)"
        " anywhere in the story: feed 3 of 7, group 2 of 5, last 24 h 2 of 5 (of the stories"
        " of the reads that record it; 1 older read without it left out)."
    ) in report
    assert (
        "- With a message of their own and a card's lines after it (a link's title,"
        " description and source, a listing's title, price and location): feed 2 of 7,"
        " group 1 of 5, last 24 h 1 of 5 (of the stories of the reads that record it; 1 older"
        " read without it left out). Their text path stays the message's."
    ) in report
    assert paths.classes == {
        "attachment_text": [1, 0, 1],
        "share_without_comment": [0, 1, 1],
        "media_only": [0, 0, 0],
        "unexplained": [2, 0, 2],  # the joins read is not counted
        "<other>": [0, 1, 1],
    }
    assert "| `unexplained` (no message, shared story or known card) | 2 | 0 | 2 |" in report
    assert "| `attachment_text` (the text of a link or listing card) | 1 | 0 | 1 |" in report
    assert "| `<other>` | 0 | 1 | 1 |" in report
    assert report.rstrip().endswith("The saved payloads were not read.")


def test_a_counter_counts_only_the_reads_that_record_it() -> None:
    measured = Measured()
    measured.add({"x": 2}, "x", 10, (0, 2))
    assert measured.counts() == "feed 2 of 10, group 0 of 0, last 24 h 2 of 10"
    measured.add({}, "x", 30, (1,))  # a read from before the counter
    measured.add({"x": 0}, "x", 4, (1,))
    assert measured == Measured(found=[2, 0, 2], of=[10, 4, 10], without=1)
    assert measured.counts().endswith(
        "group 0 of 4, last 24 h 2 of 10 (of the stories of the reads that record it;"
        " 1 older read without it left out)"
    )
    measured.add({"y": 1}, "x", 5, (0,))
    assert measured.counts().endswith("; 2 older reads without it left out)")


# -- properties: nothing but structure leaves ---------------------------------------------------

VOCAB = [
    "story",
    "text",
    "body",
    "title",
    "id",
    "url",
    "name",
    "node",
    "content",
    "attachments",
    "media",
    "styles",
    "tracking",
    "cursor",
    "comet_sections",
    "message",
    "wwwURL",
    "__isNode",
]
SAFE_VOCAB = [k for k in VOCAB if k != "message"]
"""Keys that cannot give a story its text where they are planted (for the pipeline test)."""
CARD_SAFE_VOCAB = [k for k in SAFE_VOCAB if k != "title"]
"""Keys that cannot give a card words the parser reads (for the pipeline test). Written out,
not taken from ``stories.ATTACHMENT_TEXT_KEYS``: were ``url`` or ``tracking`` ever read, the
test would still plant them and fail."""
TYPENAMES = ["Story", "TextWithEntities", "StoryAttachment", "Photo", "User", "Group"]

id_keys = st.one_of(
    st.integers(min_value=0, max_value=10**20).map(str),
    st.builds(
        lambda a, b: base64.b64encode(f"S:_I{a}:{b}".encode()).decode(),
        st.integers(min_value=0, max_value=10**17),
        st.integers(min_value=0, max_value=10**17),
    ),
    st.text(alphabet=BASE58, min_size=30, max_size=50).map(lambda s: "pfbid0" + s),
)
ids = st.integers(min_value=10**6, max_value=2**63 - 1)
bulgarian = st.text(
    alphabet=st.one_of(st.characters(min_codepoint=0x410, max_codepoint=0x44F), st.just(" ")),
    min_size=MIN_LENGTH,
    max_size=80,
)
leaves = st.one_of(
    bulgarian,
    st.text(min_size=MIN_LENGTH, max_size=60),
    st.text(max_size=MIN_LENGTH - 1),
    ids,
    ids.map(str),
    st.builds(lambda g, p: f"https://www.facebook.com/groups/{g}/posts/{p}/", ids, ids),
    st.text(alphabet=string.ascii_letters + string.digits + "-_", min_size=20, max_size=60),
    st.integers(min_value=-5, max_value=5),
    st.floats(allow_nan=False, allow_infinity=False),  # a post's latitude and longitude
    st.booleans(),
    st.none(),
)


def trees(keys: st.SearchStrategy[str]) -> st.SearchStrategy[Any]:
    def objects(children: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
        return st.builds(
            lambda d, kind: {**d, "__typename": kind} if kind else d,
            st.dictionaries(keys, children, max_size=5),
            st.one_of(st.none(), st.sampled_from(TYPENAMES), bulgarian, id_keys),
        )

    return st.recursive(
        leaves, lambda c: st.one_of(st.lists(c, max_size=4), objects(c)), max_leaves=30
    )


def scrub(value: Any) -> Any:
    """The same tree with every value replaced by one of the same type, length and blankness."""
    if isinstance(value, dict):
        return {k: v if k == "__typename" else scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return "".join(" " if c.isspace() else "x" for c in value)
    if isinstance(value, float):
        return 0.0
    if isinstance(value, int) and not isinstance(value, bool):
        return 0
    return value


def _drawn(tree_: Any) -> str:
    scan = RawScan(kept=1, steps=1, samples=[Sample(skeleton(tree_), "feed", "Q")])
    return "\n".join(render_scan(scan, zone=ZONE))


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(trees(st.one_of(st.sampled_from(VOCAB), id_keys)))
def test_the_drawing_depends_on_structure_only(tree_: Any) -> None:
    drawn = _drawn(tree_)
    assert drawn == _drawn(scrub(tree_))
    assert leaks(tree_, drawn) == []
    structure = names(tree_) - set(VOCAB) - set(TYPENAMES)  # the id-like keys used
    assert [k for k in structure if len(k) >= 6 and k in drawn] == []


CARD_VOCAB = [*VOCAB, "title_with_entities", "description", "source", "style_infos", "target"]
"""Where a card keeps its words too: the attachments drawing shows only their lengths."""


def _cards_drawn(own: Any, shared: Any) -> str:
    story = {"attachments": [own], "attached_story": {"attachments": [shared]}}
    sample = Sample(skeleton({}), "feed", "Q", attachments=diagnostics.attachment_skeleton(story))
    scan = RawScan(kept=1, steps=1, samples=[sample])
    return "\n".join(render_scan(scan, zone=ZONE))


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    own=trees(st.one_of(st.sampled_from(CARD_VOCAB), id_keys)),
    shared=trees(st.one_of(st.sampled_from(CARD_VOCAB), id_keys)),
)
def test_the_attachments_drawing_depends_on_structure_only(own: Any, shared: Any) -> None:
    drawn = _cards_drawn(own, shared)
    assert drawn == _cards_drawn(scrub(own), scrub(shared))
    assert leaks(own, drawn) == []
    assert leaks(shared, drawn) == []
    structure = (names(own) | names(shared)) - set(CARD_VOCAB) - set(TYPENAMES)
    assert [k for k in structure if len(k) >= 6 and k in drawn] == []  # the id-like keys


@given(id_keys)
def test_realistic_id_keys_read_id(key: str) -> None:
    assert looks_like_id(key)
    drawn = _drawn({"comet_sections": {key: {"text": "x" * 20}}})
    assert f"{ID}\n" in drawn or f"{ID} " in drawn
    assert len(key) < 6 or key not in drawn


@pytest.fixture(scope="module")
def module_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return asyncio.run(_empty_db(tmp_path_factory.mktemp("db") / "state.sqlite"))


below_node = st.lists(
    st.one_of(
        st.sampled_from(SAFE_VOCAB),
        id_keys,
        st.integers(min_value=0, max_value=3),
        st.text(alphabet=st.characters(min_codepoint=0x430, max_codepoint=0x44F), min_size=1),
    ),
    max_size=4,
)
"""A ``@defer`` part's path below ``edges[i].node``: schema keys, ids, indices, odd keys."""


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    subtree=trees(st.one_of(st.sampled_from(SAFE_VOCAB), id_keys)),
    words=st.one_of(
        bulgarian,
        st.text(min_size=MIN_LENGTH, max_size=200),
        st.text(min_size=1, max_size=MIN_LENGTH - 1).filter(str.strip),  # listed: a `text`
    ),
    author=st.one_of(bulgarian, st.text(min_size=6, max_size=40)),
    post_id=ids,
    fragment=trees(st.one_of(st.sampled_from(VOCAB), id_keys)),
    below=below_node,
    card=trees(st.one_of(st.sampled_from(CARD_SAFE_VOCAB), id_keys)),
)
@example(  # words under every key a card may hold: none of them may give the story text
    subtree=None,
    words="Думи на фона на поста",
    author="Иван Иванов",
    post_id=1_000_000_000_000_001,
    fragment=None,
    below=[],
    card={key: f"Думи под ключа {key}" for key in CARD_SAFE_VOCAB},
)
def test_planted_values_never_reach_the_report(
    module_db: Path,
    *,
    subtree: Any,
    words: str,
    author: str,
    post_id: int,
    fragment: Any,
    below: list[str | int],
    card: Any,
) -> None:
    def report_of(
        words: str, author: str, subtree: Any, fragment: Any, card: Any
    ) -> tuple[dict[str, Any], str]:
        story = background_story(words, post_id=str(post_id))
        _content(story)["comet_sections"]["unknown_renderer"] = subtree
        _content(story)["attachments"].append(card)  # drawn under the shape's attachments
        story["comet_sections"]["context_layout"]["story"]["actors"][0]["name"] = author
        part = {
            "label": DEFER,
            "path": ["viewer", "news_feed", "edges", 0, "node", *below],
            "data": {"story": fragment},
        }
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder)
            save(raw, stream_step(story, parts=[part]), at=T0)
            return story, generate(module_db, zone=ZONE, now=T0 + 60, raw_dir=raw)

    story, report = report_of(words, author, subtree, fragment, card)
    assert "with empty text: 1;" in report
    assert "body <TextWithEntities>" in report
    assert f"` | `{DEFER}` | 1 | 1 | " in report  # the part is counted at the empty story
    assert leaks(story, report) == []
    assert leaks(fragment, report) == []
    schema = {*VOCAB, *TYPENAMES, "__typename"}  # names the report may print
    raw_keys = ({k for k in below if isinstance(k, str)} | names(fragment)) - schema
    assert [k for k in raw_keys if len(k) >= 6 and k in report] == []  # the id-like ones
    assert "#### The attachments of each shape" in report
    assert leaks(card, report) == []
    scrubbed = report_of(scrub(words), scrub(author), scrub(subtree), scrub(fragment), scrub(card))
    assert report == scrubbed[1]


# -- property: the script section says how many, never what -----------------------------------

cyrillic_words = st.text(alphabet="абвгджзийклмнпрстфхцчшщюя", min_size=6, max_size=12)
latin_words = st.text(alphabet="qjzxkvwyhf", min_size=6, max_size=12)
"""Latin runs no line of the report can hold: none of its words has six of these letters."""
other_bits = st.sampled_from(["123", "4 5", "αβγδ", "λόγος", "😀", "№ 7", "---"])
post_texts = st.one_of(
    st.none(),  # a photo alone: no text
    st.lists(st.one_of(cyrillic_words, latin_words, other_bits), min_size=1, max_size=5).map(
        " ".join
    ),
)
translation_names = st.one_of(
    st.none(), st.sampled_from(["translatability_for_viewer", "translation", "isTranslatable"])
)
GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")
LATIN_RUN = re.compile(r"[qjzxkvwyhf]{6,}")


def _script_oracle(words: str | None) -> str:
    cyrillic = bool(CYRILLIC.search(words or ""))
    latin = bool(re.search(r"[A-Za-z]", words or ""))
    if cyrillic and latin:
        return "Mixed (Cyrillic and Latin letters)"
    if cyrillic:
        return "Cyrillic only"
    return "Latin only" if latin else "Other or none (no Cyrillic or Latin letter, or no text)"


def _same_script(value: str) -> str:
    """``value`` with every letter replaced by one of its script, every digit by ``0``."""
    out = []
    for char in value:
        if CYRILLIC.match(char):
            out.append("ж")
        elif GREEK.match(char):
            out.append("λ")
        elif char.isascii() and char.isalpha():
            out.append("q")
        else:
            out.append("0" if char.isdigit() else char)
    return "".join(out)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    posts=st.lists(
        st.tuples(post_texts, translation_names, st.one_of(cyrillic_words, latin_words)),
        min_size=1,
        max_size=6,
    )
)
def test_the_script_section_never_prints_text(
    module_db: Path, posts: list[tuple[str | None, str | None, str]]
) -> None:
    """Whatever the posts say, and whatever their translation keys hold, the script section
    prints the posts per script and the keys' names: the right counts, no word of theirs,
    and the same section when every letter is replaced by another of its script."""

    def section_of(posts: list[tuple[str | None, str | None, str]]) -> tuple[str, str]:
        nodes = []
        for index, (words, key, value) in enumerate(posts):
            extra = {} if key is None else {key: {"text": value}}
            nodes.append(worded_story(f"50000000000005{index:02d}", words, **extra))
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder)
            save(raw, feed_step(*nodes), at=T0)
            report = generate(module_db, zone=ZONE, now=T0 + 60, raw_dir=raw, diagnostics_only=True)
        return report, script_section(report)

    report, section = section_of(posts)
    expected = Counter(_script_oracle(words) for words, _key, _value in posts)
    rows = script_rows(section)
    assert rows.pop("**all**") == len(posts)
    assert rows == {label: expected.get(label, 0) for label in rows}
    keyed = sum(key is not None for _words, key, _value in posts)
    assert f"anywhere in the story: {keyed} of {len(posts)} (" in section
    assert not CYRILLIC.search(report)
    assert not GREEK.search(report)
    assert not LATIN_RUN.search(report)
    scrubbed = [
        (None if words is None else _same_script(words), key, "x" * len(value))
        for words, key, value in posts
    ]
    assert section_of(scrubbed)[1] == section
