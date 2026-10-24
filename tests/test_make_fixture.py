from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from tools import make_fixture
from tools.make_fixture import (
    DROP,
    Anonymizer,
    Pruner,
    body_text,
    build_document,
    build_steps,
    hollow,
    render_html,
    script_json,
)

from fbcollector.frames import iter_document_json
from fbcollector.model import GraphqlBody, Step, Surface
from fbcollector.parser import parse_step
from tests.collector.test_parser_properties import stories
from tests.test_fixture_secrets import scan

# The 2026-09-24 research captures live outside the repository (they hold third-party data);
# point FBC_RESEARCH_DIR at them to check that the committed fixtures are what the builder makes.
RESEARCH = Path(os.environ.get("FBC_RESEARCH_DIR", "/nonexistent"))
FIXTURES = Path(__file__).parent / "fixtures"


def prune(value: Any, keep: set[str] | None = None) -> Any:
    return Pruner(Anonymizer(keep or set())).prune(value)


class TestPruner:
    def test_only_allow_listed_keys_survive(self) -> None:
        capture = {
            "data": {
                "node": {
                    "post_id": "2283602175553462",
                    "encrypted_tracking": "AZsecret",
                    "click_tracking_linkshim_cb": "x",
                    "feedback": {"reactors": {"count": 3}},
                    "comet_sections": {"content": {"story": {"message": {"text": "hello"}}}},
                }
            },
            "extensions": {
                "is_final": True,
                "prefetch_uris_v2": [{"uri": "https://scontent.x/?oh=1"}],
            },
        }
        out = prune(capture)
        assert out == {
            "data": {
                "node": {
                    "post_id": "1000000000000001",
                    "comet_sections": {
                        "content": {
                            "story": {
                                "message": {
                                    "text": out["data"]["node"]["comet_sections"]["content"][
                                        "story"
                                    ]["message"]["text"]
                                }
                            }
                        }
                    },
                }
            }
        }
        assert (
            out["data"]["node"]["comet_sections"]["content"]["story"]["message"]["text"] != "hello"
        )

    def test_secrets_never_survive(self) -> None:
        page = {
            "require": [
                ["DTSGInitialData", [], {"token": "AQHsecret"}, 258],
                ["LSD", [], {"token": "lsdsecret"}, 323],
                ["CurrentUserInitialData", [], {"USER_ID": "100012345678901", "NAME": "Jane"}, 270],
                [
                    "X",
                    "y",
                    [],
                    [
                        {
                            "__bbox": {
                                "result": {
                                    "data": {
                                        "url": "https://www.facebook.com/x?fb_dtsg=1&xs=2&c_user=3"
                                    }
                                }
                            }
                        }
                    ],
                ],
            ]
        }
        out = prune(page)
        text = json.dumps(out)
        assert scan(text) == []
        assert "Jane" not in text and "secret" not in text

    def test_consistent_ids_across_keys_and_urls(self) -> None:
        node = {
            "post_id": "2283602175553462",
            "id": "2283602175553462",
            "permalink_url": "https://www.facebook.com/groups/Jerseyask/posts/2283602175553462/?__cft__=1",
            "wwwURL": "https://m.facebook.com/groups/Jerseyask/permalink/2283602175553462/",
            "to": {
                "__typename": "Group",
                "id": "317230128857353",
                "name": "Jersey",
                "vanity": "Jerseyask",
            },
            "actors": [
                {
                    "id": "pfbid0abc",
                    "name": "Daren",
                    "url": "https://www.facebook.com/profile.php?id=100063592120114&x=1",
                }
            ],
            "target_group": {"id": "317230128857353"},
        }
        out = prune(node)
        pid, gid = out["post_id"], out["to"]["id"]
        assert out["id"] == pid and out["target_group"]["id"] == gid
        slug = out["to"]["vanity"]
        assert out["permalink_url"] == f"https://www.facebook.com/groups/{slug}/posts/{pid}/"
        assert out["wwwURL"] == f"https://www.facebook.com/groups/{slug}/permalink/{pid}/"
        assert out["to"]["name"] == "Group 1"
        assert out["actors"][0]["name"] == "Person 2"
        assert out["actors"][0]["id"] == "opaque1"
        assert out["actors"][0]["url"].startswith("https://www.facebook.com/profile.php?id=1")

    def test_texts_are_consistent_and_keep_their_lines(self) -> None:
        anon = Anonymizer()
        first = anon.text("line one\n\nline three")
        assert anon.text("line one\n\nline three") == first
        assert first.count("\n") == 2 and first.split("\n")[1] == ""
        assert anon.text("other") != first

    def test_keep_passes_synthetic_values(self) -> None:
        node = {
            "post_id": "5000000000000001",
            "name": "Synthetic Group",
            "message": {"text": "Synthetic text"},
        }
        assert prune(node, {"5000000000000001", "Synthetic Group", "Synthetic text"}) == node

    def test_urls(self) -> None:
        anon = Anonymizer()
        assert (
            anon.url("https://scontent.fsof8-1.fna.fbcdn.net/v/x.jpg?oh=1")
            == "https://example.invalid/"
        )
        assert anon.url("not a url at all") == "https://example.invalid/"
        assert anon.url("https://[::1") == "https://example.invalid/"
        assert anon.url("https://www.facebook.com/?filter=groups&sk=h_chr&__tn__=x") == (
            "https://www.facebook.com/?filter=groups&sk=h_chr"
        )
        assert anon.url(
            "https://www.facebook.com/groups/1234567/?sorting_setting=CHRONOLOGICAL"
        ) == ("https://www.facebook.com/groups/100000001/?sorting_setting=CHRONOLOGICAL")
        assert anon.url("https://www.facebook.com/some.person") == "https://www.facebook.com/slug-1"

    def test_variables_and_framing(self) -> None:
        page = {
            "require": [
                [
                    "ScheduledServerJS",
                    "handle",
                    None,
                    [
                        {
                            "__bbox": {
                                "require": [
                                    [
                                        "RelayPrefetchedStreamCache",
                                        "next",
                                        [],
                                        [
                                            "adp_CometGroupRootQueryRelayPreloader_6ab5",
                                            {
                                                "__bbox": {
                                                    "complete": True,
                                                    "result": {
                                                        "data": {"group": {"id": "317230128857353"}}
                                                    },
                                                    "sequence_number": 0,
                                                }
                                            },
                                        ],
                                    ],
                                    [
                                        "CometModule",
                                        "init",
                                        [1, 2],
                                        [
                                            {
                                                "expectedPreloaders": [
                                                    {
                                                        "queryName": "Q",
                                                        "variables": {
                                                            "groupID": "317230128857353",
                                                            "orderby": [
                                                                "MOST_RECENT",
                                                                "bad value!",
                                                            ],
                                                            "scale": 2,
                                                            "fb_dtsg": "x",
                                                        },
                                                    }
                                                ]
                                            }
                                        ],
                                    ],
                                ]
                            }
                        }
                    ],
                ],
            ]
        }
        out = prune(page)
        entries = out["require"][0][2][0]["__bbox"]["require"]
        assert entries[0][:3] == ["RelayPrefetchedStreamCache", "next", []]
        assert entries[0][3][0] == "adp_CometGroupRootQueryRelayPreloader_1"
        variables = entries[1][0][0]["expectedPreloaders"][0]["variables"]
        assert variables == {"groupID": "100000000000001", "orderby": ["MOST_RECENT"]}

    def test_error_text_and_other_words(self) -> None:
        frame = {
            "errors": [
                {
                    "message": "Error 1675004 for 100012345678901",
                    "severity": "CRITICAL",
                    "code": 1675004,
                    "path": ["viewer", 0, "bad key!"],
                }
            ],
            "error": 1357001,
            "errorSummary": "Not logged in",
            "data": {"group": {"description": "Call Jane at 0888123456", "message": "free words"}},
        }
        out = prune(frame)
        assert out["errors"][0]["message"] == "Error 100000001 for 100000000000002"
        assert out["errors"][0]["code"] == 1675004
        assert out["errors"][0]["path"] == ["viewer", 0]
        assert out["errorSummary"] == "Not logged in"
        assert "Jane" not in out["data"]["group"]["description"]
        assert out["data"]["group"]["message"] != "free words"

    def test_scalars(self) -> None:
        out = prune(
            {
                "id": 317230128857353,
                "creation_time": 1790000000,
                "complete": True,
                "__typename": "Story",
                "label": "A$stream$B",
                "title": {"text": "x"},
                "subscribe_status": "IS_SUBSCRIBED",
                "group_address": "",
                "cursor": "AQH",
                "privacy_info": {"title": {"text": "Private group"}},
                "visibility": "not enum!",
                "has_next_page": 1.5,
                "url": 5,
            }
        )
        assert out["id"] == 100000000000001
        assert out["creation_time"] == 1790000000
        assert out["label"] == "A$stream$B"
        assert out["subscribe_status"] == "IS_SUBSCRIBED"
        assert out["group_address"] == ""
        assert out["cursor"] == "cursor-1"
        assert out["privacy_info"]["title"]["text"] == "Private group"
        assert out["visibility"] != "not enum!"
        assert out["has_next_page"] == 1.5
        assert out["url"] == 5  # not a string: nothing to anonymise

    def test_hollow(self) -> None:
        assert hollow({})
        assert hollow([None, "next", [], ["adp_X"]])
        assert not hollow([{"a": 1}])
        assert not hollow("x")
        assert prune({"junk": 1}) is DROP
        assert prune({}) == {}
        assert prune([["Module", "init", [1]]]) is DROP


@given(stories())
def test_the_allow_list_keeps_everything_the_parser_reads(case: tuple[dict[str, Any], Any]) -> None:
    node, _expected = case
    keep: set[str] = set()
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str):
            keep.add(current)
    frame = {"data": {"viewer": {"news_feed": {"edges": [{"node": node}]}}}}
    pruned = prune(frame, keep)

    def parse(value: Any) -> Any:
        body = GraphqlBody(friendly_name="Q", body=json.dumps(value).encode())
        return parse_step(
            Step(final_url="u", status=200, graphql=(body,)), surface=Surface.FEED
        ).stories

    assert parse(pruned) == parse(frame)


class TestRendering:
    def test_script_json_cannot_close_the_script(self) -> None:
        text = script_json({"text": "</script><script>alert(1)</script>"})
        assert "</script" not in text
        html = render_html([{"a": "<b>"}], source="x --> <evil>", synthetic=True)
        assert "-->" in html.splitlines()[1] and "<evil>" not in html
        assert list(iter_document_json(html)) == [{"a": "<b>"}]

    def test_build_document_drops_empty_blocks(self) -> None:
        values = [{"require": [["Module", "init", [], []]]}, {"data": {"viewer": None}}, "x"]
        html = build_document(values, Pruner(Anonymizer()), source="s", synthetic=False)
        assert list(iter_document_json(html)) == [{"data": {"viewer": None}}]
        assert "synthetic: no" in html

    def test_body_text(self) -> None:
        pruner = Pruner(Anonymizer())
        frames = [{"data": {"viewer": None}}, {"data": {"x": 1}}, {"junk": 1}]
        assert body_text(frames, pruner) == 'for (;;);{"data":{"viewer":null}}'
        assert body_text([{"label": "L"}], pruner, guard=False) == '{"label":"L"}'

    def test_build_steps(self, tmp_path: Path) -> None:
        (tmp_path / "body.txt").write_text('for (;;);{"data":{"viewer":null},"x":1}\n{"label":"L"}')
        manifest = {
            "steps": [
                {
                    "final_url": "https://www.facebook.com/?filter=groups&sk=h_chr",
                    "document": "d.html",
                    "graphql": [
                        {"friendly_name": "Q", "body_file": "body.txt", "capped": True},
                        {
                            "friendly_name": "R",
                            "frames": [{"data": {}}],
                            "guard": False,
                            "status": 500,
                        },
                    ],
                    "dom": {"posinset_count": 2},
                },
                {"final_url": "https://www.facebook.com/", "pagination_fired": False},
            ]
        }
        built = build_steps(manifest, tmp_path, Pruner(Anonymizer()), source="s", synthetic=False)
        first, second = built["steps"]
        assert built["_fixture"]["synthetic"] is False
        assert first["graphql"][0] == {
            "friendly_name": "Q",
            "status": 200,
            "body": 'for (;;);{"data":{"viewer":null}}\n{"label":"L"}',
            "capped": True,
        }
        assert first["graphql"][1] == {"friendly_name": "R", "status": 500, "body": '{"data":{}}'}
        assert first["pagination_fired"] is True and first["dom"] == {"posinset_count": 2}
        assert second["graphql"] == [] and second["pagination_fired"] is False


class TestCli:
    def test_document_and_steps(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        capture = tmp_path / "capture.html"
        block = {"data": {"viewer": {"id": "1234567"}}, "extra": "x", "other": "y"}
        capture.write_text(f'<script type="application/json">{json.dumps(block)}</script>')
        out = tmp_path / "out.html"
        assert (
            make_fixture.main(
                ["document", str(capture), str(out), "--source", "test", "--extra-key", "extra"]
            )
            == 0
        )
        built = next(iter(iter_document_json(out.read_bytes())))
        assert built == {"data": {"viewer": {"id": "100000001"}}, "extra": "x"}
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps(
                {
                    "steps": [
                        {
                            "final_url": "https://www.facebook.com/",
                            "graphql": [{"frames": [{"data": {}}]}],
                        }
                    ]
                }
            )
        )
        out_json = tmp_path / "out.json"
        assert make_fixture.main(["steps", str(manifest), str(out_json), "--source", "test"]) == 0
        assert (
            json.loads(out_json.read_text())["steps"][0]["graphql"][0]["body"]
            == 'for (;;);{"data":{}}'
        )
        assert capsys.readouterr().out.splitlines() == [str(out), str(out_json)]


def test_the_fixtures_built_without_captures_are_reproducible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``synthetic`` needs no research capture: its fixtures are checked on every run."""
    assert make_fixture.main(["synthetic", str(tmp_path)]) == 0
    built = sorted(p.name for p in tmp_path.iterdir())
    assert built == [make_fixture.ATTACHMENTS_FIXTURE]
    for name in built:
        assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes(), name
    assert capsys.readouterr().out == f"{tmp_path / make_fixture.ATTACHMENTS_FIXTURE}\n"
    built_fixture = json.loads((tmp_path / built[0]).read_text(encoding="utf-8"))
    assert built_fixture["_fixture"]["synthetic"] is True
    assert scan((tmp_path / built[0]).read_text(encoding="utf-8")) == []


def test_the_attachment_fixture_keeps_only_listed_keys() -> None:
    """The keys the parser must never read are kept on purpose, and only those."""
    text = (FIXTURES / make_fixture.ATTACHMENTS_FIXTURE).read_text(encoding="utf-8")
    body = json.loads(text)["steps"][0]["graphql"][0]["body"]
    keys: set[str] = set()
    for line in body.removeprefix("for (;;);").splitlines():
        stack: list[Any] = [json.loads(line)]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                keys.update(value)
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    assert keys - make_fixture.ALLOWED_KEYS == make_fixture.ATTACHMENT_EXTRA_KEYS


@pytest.mark.skipif(not RESEARCH.is_dir(), reason="FBC_RESEARCH_DIR is not set")
def test_committed_fixtures_are_reproducible(tmp_path: Path) -> None:
    assert make_fixture.main(["research", str(RESEARCH), str(tmp_path)]) == 0
    built = sorted(p.name for p in tmp_path.iterdir())
    assert built == sorted(p.name for p in FIXTURES.iterdir() if p.suffix in {".html", ".json"})
    for name in built:
        assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes(), name
