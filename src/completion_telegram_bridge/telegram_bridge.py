"""Telegram user-client: send prompt, wait for agent reply."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from telethon import TelegramClient, events
from telethon.tl.types import User

from completion_telegram_bridge.config import BridgeConfig, format_outbound_message

logger = logging.getLogger(__name__)


@dataclass
class BotDialog:
    id: int
    username: str | None
    title: str
    is_bot: bool = True


@dataclass
class _PendingWait:
    agent_id: int
    after_message_id: int
    chunks: list[str] = field(default_factory=list)
    first_received: asyncio.Event = field(default_factory=asyncio.Event)
    last_message_at: float = 0.0
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None


class TelegramBridge:
    """Owns a Telethon client and single-flight completion waits."""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        if not config.telegram_api_id or not config.telegram_api_hash:
            raise RuntimeError("Telegram API id/hash not configured")
        session = str(config.session_path())
        # Telethon appends .session if not present; we pass path without forcing double suffix
        if session.endswith(".session"):
            session = session[: -len(".session")]
        self._client = TelegramClient(
            session,
            config.telegram_api_id,
            config.telegram_api_hash,
        )
        self._lock = asyncio.Lock()
        self._pending: _PendingWait | None = None
        self._started = False

    @property
    def client(self) -> TelegramClient:
        return self._client

    async def start(self) -> None:
        if self._started:
            return
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized; run: ctb login")
        self._client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
        self._started = True
        me = await self._client.get_me()
        logger.info(
            "Telegram connected as %s (id=%s)",
            getattr(me, "username", None) or getattr(me, "first_name", "?"),
            getattr(me, "id", "?"),
        )

    async def stop(self) -> None:
        if self._started:
            await self._client.disconnect()
            self._started = False

    async def resolve_agent_entity(self) -> User:
        cfg = self.config
        if cfg.agent_id is not None:
            entity = await self._client.get_entity(cfg.agent_id)
        elif cfg.agent_username:
            uname = cfg.agent_username.lstrip("@")
            entity = await self._client.get_entity(uname)
        else:
            raise RuntimeError("No agent configured")
        if not isinstance(entity, User) or not entity.bot:
            # Still allow if marked as bot in dialogs; some entities may differ
            if not getattr(entity, "bot", False):
                logger.warning("Selected agent id=%s may not be a bot", getattr(entity, "id", "?"))
        return entity  # type: ignore[return-value]

    async def list_bot_dialogs(self, limit: int = 200) -> list[BotDialog]:
        bots: list[BotDialog] = []
        async for dialog in self._client.iter_dialogs(limit=limit):
            entity = dialog.entity
            if not isinstance(entity, User):
                continue
            if not getattr(entity, "bot", False):
                continue
            title = dialog.name or entity.first_name or entity.username or str(entity.id)
            bots.append(
                BotDialog(
                    id=entity.id,
                    username=entity.username,
                    title=title,
                    is_bot=True,
                )
            )
        bots.sort(key=lambda b: (b.title or "").lower())
        return bots

    async def resolve_username(self, username: str) -> BotDialog:
        uname = username.lstrip("@")
        entity = await self._client.get_entity(uname)
        if not isinstance(entity, User):
            raise RuntimeError(f"@{uname} is not a user/bot")
        title = " ".join(
            p for p in [entity.first_name, entity.last_name] if p
        ) or entity.username or str(entity.id)
        return BotDialog(
            id=entity.id,
            username=entity.username,
            title=title,
            is_bot=bool(getattr(entity, "bot", False)),
        )

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        pending = self._pending
        if pending is None:
            return
        try:
            msg = event.message
            if msg is None:
                return
            sender_id = event.sender_id
            if sender_id != pending.agent_id:
                return
            if msg.id <= pending.after_message_id:
                return
            text = _message_text(msg)
            if not text:
                return
            pending.chunks.append(text)
            pending.last_message_at = time.monotonic()
            pending.first_received.set()
            logger.debug("Captured agent chunk (%d chars), total chunks=%d", len(text), len(pending.chunks))
        except Exception as exc:  # noqa: BLE001
            pending.error = exc
            pending.done.set()

    async def complete(self, user_prompt: str) -> str:
        """Send prompt to agent and return aggregated reply text."""
        if self._lock.locked():
            raise BusyError("Another completion is already in progress")
        acquired = False
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=0.05)
            acquired = True
        except asyncio.TimeoutError as exc:
            raise BusyError("Another completion is already in progress") from exc
        try:
            return await self._complete_locked(user_prompt)
        finally:
            if acquired:
                self._lock.release()

    async def _complete_locked(self, user_prompt: str) -> str:
        agent = await self.resolve_agent_entity()
        agent_id = agent.id
        outbound = format_outbound_message(user_prompt)
        sent = await self._client.send_message(agent, outbound)
        after_id = sent.id
        logger.info(
            "Sent prompt to agent id=%s msg_id=%s prompt_len=%d",
            agent_id,
            after_id,
            len(user_prompt),
        )

        pending = _PendingWait(agent_id=agent_id, after_message_id=after_id)
        self._pending = pending
        timeout = float(self.config.reply_timeout_sec)
        quiet = float(self.config.reply_quiet_ms) / 1000.0
        max_messages = int(self.config.reply_max_messages)

        try:
            try:
                await asyncio.wait_for(pending.first_received.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise ReplyTimeoutError(
                    f"Timed out after {self.config.reply_timeout_sec}s waiting for Telegram agent reply"
                ) from exc

            if pending.error:
                raise pending.error

            # Aggregate further bubbles until quiet period
            deadline = time.monotonic() + timeout  # overall cap still applies
            while True:
                if len(pending.chunks) >= max_messages:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sleep_for = min(quiet, remaining)
                await asyncio.sleep(sleep_for)
                # If a new message arrived during sleep, last_message_at is recent — continue
                since_last = time.monotonic() - pending.last_message_at
                if since_last >= quiet - 1e-3:
                    break
                # else loop and sleep again for remaining quiet window

            reply = "\n\n".join(pending.chunks).strip()
            if not reply:
                raise ReplyTimeoutError("Agent replied without usable text")
            logger.info("Agent reply ready chunks=%d chars=%d", len(pending.chunks), len(reply))
            return reply
        finally:
            self._pending = None


def _message_text(msg) -> str:
    text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    if text:
        return str(text).strip()
    # caption on media
    raw = getattr(msg, "raw_text", None)
    if raw:
        return str(raw).strip()
    return ""


class BusyError(Exception):
    """Another completion is in flight."""


class ReplyTimeoutError(Exception):
    """Agent did not reply in time."""
