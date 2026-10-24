"""The bot's Telegram side, with a client that records instead of connecting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ddiver_core.bot.router import Reply, Router
from ddiver_core.botruntime import BotAdapter
from ddiver_core.portable import MAX_BYTES
from ddiver_core.storage import Storage
from ddiver_fb.botio import PUBLIC_COMMANDS, BotFront
from tests.brain.helpers import MOD, OWNER, STRANGER, FakeTelethon


class Client(FakeTelethon):
    """Adds what the adapter and the downloader call."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[tuple[int, str, dict[str, Any]]] = []
        self.download: bytes | Exception = b'{"format": "ddiver-fb.config"}'

    async def send_message(self, peer: int, text: str, **kw: Any) -> None:
        self.messages.append((peer, text, kw))

    async def download_media(self, message: Any, *, file: type) -> bytes:
        if isinstance(self.download, Exception):
            raise self.download
        return self.download


@dataclass
class Document:
    size: int = 100
    attributes: list[Any] = field(default_factory=list)


@dataclass
class Named:
    file_name: str


@dataclass
class Message:
    document: Document | None = None


@dataclass
class Sender:
    lang_code: str | None = "uk"


@dataclass
class Event:
    sender_id: int
    raw_text: str = ""
    data: bytes | None = None
    message: Message = field(default_factory=Message)
    message_id: int | None = None
    answered: int = 0
    broken: bool = False

    async def get_sender(self) -> Sender:
        if self.broken:
            raise ConnectionError("gone")
        return Sender()

    async def answer(self, *args: Any, **kw: Any) -> None:
        self.answered += 1


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def front(client: Client, storage: Storage) -> BotFront:
    return BotFront(client, BotAdapter(client), Router(storage), storage)


class TestUpdates:
    async def test_register_subscribes_to_messages_and_buttons(self, front: BotFront, client):
        front.register()
        assert len(client.handlers) == 2

    async def test_a_message_goes_through_the_router_and_back(
        self, front: BotFront, client: Client
    ):
        await front._on_message(Event(sender_id=OWNER, raw_text="/whatever"))
        ((peer, text, _kw),) = client.messages
        assert peer == OWNER and text == "Unknown command. Use /menu."

    async def test_a_button_press_is_answered_when_nothing_else_answers_it(
        self, front: BotFront, client: Client, storage: Storage
    ):
        router = front._router

        @router.callback("ping")
        async def ping(_ctx: Any) -> list[Any]:
            return [Reply("bot.cancelled")]

        event = Event(sender_id=OWNER, data=b"ping")
        await front._on_callback(event)
        assert event.answered == 1  # Telegram keeps the button spinning until it is
        toast = Event(sender_id=OWNER, data=None)  # "" is unknown: the Toast answers it
        await front._on_callback(toast)
        assert toast.answered == 0

    async def test_a_failing_update_is_logged_not_raised(
        self, front: BotFront, caplog: pytest.LogCaptureFixture
    ):
        await front.on_update(Event(sender_id=OWNER, broken=True), text="hi")
        assert "bot update failed" in caplog.text


class TestDocuments:
    async def test_no_document(self, front: BotFront):
        assert await front.attached_document(Event(sender_id=OWNER)) == (None, None)

    async def test_too_large_is_not_downloaded(self, front: BotFront, client: Client):
        client.download = AssertionError("must not download")
        big = Event(sender_id=OWNER, message=Message(Document(size=MAX_BYTES + 1)))
        assert await front.attached_document(big) == (None, None)

    async def test_only_for_people_allowed_to_use_the_bot(self, front: BotFront, storage: Storage):
        await storage.ensure_user(STRANGER)
        event = Event(sender_id=STRANGER, message=Message(Document()))
        assert await front.attached_document(event) == (None, None)
        event = Event(sender_id=999_999, message=Message(Document()))
        assert await front.attached_document(event) == (None, None)

    async def test_an_allowed_user_gets_the_bytes_and_the_name(self, front: BotFront):
        event = Event(sender_id=MOD, message=Message(Document(attributes=[Named("x.json")])))
        content, name = await front.attached_document(event)
        assert content is not None and name == "x.json"

    async def test_a_failed_download_is_nothing(self, front: BotFront, client: Client):
        client.download = ValueError("expired")
        event = Event(sender_id=OWNER, message=Message(Document()))
        assert await front.attached_document(event) == (None, None)

    async def test_a_message_with_a_file_reaches_the_router(self, front: BotFront, client):
        event = Event(sender_id=OWNER, raw_text="", message=Message(Document()))
        await front._on_message(event)
        assert client.messages  # the fallback answered


class TestCommands:
    async def test_published_for_every_language(self, front: BotFront, client: Client):
        await front.publish_commands()
        assert len(client.requests) == 4
        names = [c.command for c in client.requests[0].commands]
        assert names == list(PUBLIC_COMMANDS)

    async def test_a_failure_does_not_stop_the_bot(
        self, front: BotFront, client: Client, caplog: pytest.LogCaptureFixture
    ):
        client.fail_requests = True
        await front.publish_commands()
        assert caplog.text.count("could not publish the command list") == 4
