"""The bot's Telegram side: updates in, router actions out. The same plumbing as ddiver's.

Handlers never see Telethon (``ddiver_core.bot``); this module turns a private message or a
button press into an ``Incoming``, asks the router, and lets the core's ``BotAdapter`` perform
the answer. It also publishes the command list and fetches an attached file for ``/import``,
bounded before the download and only for people allowed to use the bot at all.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Final

from telethon import errors, events
from telethon.tl import types as tl
from telethon.tl.functions.bots import SetBotCommandsRequest

from ddiver_core import portable
from ddiver_core.bot.router import Incoming, Router, Toast
from ddiver_core.botruntime import BotAdapter
from ddiver_core.i18n import DEFAULT_LANG, LANGS, t
from ddiver_core.storage import Storage

log = logging.getLogger(__name__)

PUBLIC_COMMANDS: Final = ("start", "menu", "test", "status", "export", "import", "help", "cancel")
"""What Telegram offers after "/". The owner's commands are listed in the owner's /status."""


class BotFront:
    def __init__(self, client: Any, adapter: BotAdapter, router: Router, storage: Storage) -> None:
        self._client = client
        self._adapter = adapter
        self._router = router
        self._storage = storage

    def register(self) -> None:
        """Subscribe to private messages and button presses (the bot answers nowhere else)."""
        self._client.add_event_handler(
            self._on_message, events.NewMessage(incoming=True, func=lambda e: e.is_private)
        )
        self._client.add_event_handler(
            self._on_callback, events.CallbackQuery(func=lambda e: e.is_private)
        )

    async def _on_message(self, event: Any) -> None:
        document, name = await self.attached_document(event)
        await self.on_update(event, text=event.raw_text, document=document, document_name=name)

    async def _on_callback(self, event: Any) -> None:
        data = event.data.decode("utf-8", "replace") if event.data else ""
        await self.on_update(event, data=data)

    async def publish_commands(self) -> None:
        """Cosmetic, and remembered by Telegram: a failure never keeps the bot from starting."""
        for lang in (*LANGS, ""):
            try:
                await self._client(
                    SetBotCommandsRequest(
                        scope=tl.BotCommandScopeDefault(),
                        lang_code=lang,
                        commands=[
                            tl.BotCommand(
                                command=name,
                                description=t(lang or DEFAULT_LANG, f"bot.cmd.{name}"),
                            )
                            for name in PUBLIC_COMMANDS
                        ],
                    )
                )
            except (errors.RPCError, ConnectionError, TimeoutError) as exc:
                log.warning("could not publish the command list for %r: %s", lang, exc)

    async def attached_document(self, event: Any) -> tuple[bytes | None, str | None]:
        """Download a small attached file for an allowed user, or return nothing."""
        document = getattr(getattr(event, "message", None), "document", None)
        if document is None or int(getattr(document, "size", 0) or 0) > portable.MAX_BYTES:
            return None, None
        user = await self._storage.get_user(int(event.sender_id))
        if user is None or not user.active:
            return None, None
        name = None
        for attribute in getattr(document, "attributes", None) or []:
            name = getattr(attribute, "file_name", None) or name
        try:
            content = await self._client.download_media(event.message, file=bytes)
        except (errors.RPCError, ConnectionError, TimeoutError, ValueError):
            log.warning("could not download an attached file", exc_info=True)
            return None, None
        return (content if isinstance(content, bytes) else None), name

    async def on_update(
        self,
        event: Any,
        *,
        text: str | None = None,
        data: str | None = None,
        document: bytes | None = None,
        document_name: str | None = None,
    ) -> None:
        try:
            sender = await event.get_sender()
            incoming = Incoming(
                user_id=int(event.sender_id),
                text=text,
                data=data,
                msg_id=getattr(event, "message_id", None),
                lang_code=getattr(sender, "lang_code", None),
                is_private=True,
                document=document,
                document_name=document_name,
            )
            actions = await self._router.dispatch(incoming)
            user = await self._storage.get_user(incoming.user_id)
            lang = (user.lang if user else None) or DEFAULT_LANG
            await self._adapter.perform(event, actions, lang)
            if data is not None and not any(isinstance(a, Toast) for a in actions):
                with contextlib.suppress(Exception):  # the button spins until answered
                    await event.answer()
        except Exception:
            log.exception("bot update failed")
