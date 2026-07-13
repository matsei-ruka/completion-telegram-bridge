"""Telegram user-client: send prompt, wait for agent reply."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field

from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeAudio, User

from completion_telegram_bridge.config import (
    BridgeConfig,
    format_outbound_message,
    format_voice_caption,
)
from completion_telegram_bridge.logging_setup import DEBUG_BODIES, preview_text

logger = logging.getLogger(__name__)


@dataclass
class BotDialog:
    id: int
    username: str | None
    title: str
    is_bot: bool = True


@dataclass
class ReplyAudio:
    """Voice note received from the agent, downloaded from Telegram."""

    data: bytes
    mime_type: str = "audio/ogg"
    duration_ms: int | None = None


@dataclass
class BridgeReply:
    """Aggregated agent reply: text chunks plus optional voice note."""

    text: str
    audio: ReplyAudio | None = None


@dataclass
class _PendingWait:
    agent_id: int
    after_message_id: int
    req_id: str = "-"
    want_audio: bool = False
    chunks: list[str] = field(default_factory=list)
    first_received: asyncio.Event = field(default_factory=asyncio.Event)
    voice_received: asyncio.Event = field(default_factory=asyncio.Event)
    voice_msg: object | None = None
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
        logger.debug("Connecting Telethon session…")
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
            logger.info("Telegram disconnected")

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
        logger.debug("Listed %d bot dialogs", len(bots))
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
                logger.debug(
                    "tg ignore msg id=%s sender=%s (waiting for agent=%s)",
                    pending.req_id,
                    sender_id,
                    pending.agent_id,
                )
                return
            if msg.id <= pending.after_message_id:
                logger.debug(
                    "tg ignore stale msg_id=%s after=%s req=%s",
                    msg.id,
                    pending.after_message_id,
                    pending.req_id,
                )
                return
            text = _message_text(msg)
            voice = getattr(msg, "voice", None)
            if voice is not None and pending.want_audio:
                if text:
                    # caption travels with the voice note; it is also the transcript
                    pending.chunks.append(text)
                pending.voice_msg = msg
                pending.last_message_at = time.monotonic()
                pending.first_received.set()
                pending.voice_received.set()
                logger.info(
                    "tg agent voice req=%s msg_id=%s mime=%s caption_chars=%d",
                    pending.req_id,
                    msg.id,
                    getattr(voice, "mime_type", "?"),
                    len(text),
                )
                return
            if not text:
                logger.info(
                    "tg agent media/empty msg_id=%s req=%s (waiting for text)",
                    msg.id,
                    pending.req_id,
                )
                return
            pending.chunks.append(text)
            pending.last_message_at = time.monotonic()
            pending.first_received.set()
            logger.info(
                "tg agent chunk req=%s msg_id=%s chunk=%d/%d chars=%d preview=%r",
                pending.req_id,
                msg.id,
                len(pending.chunks),
                self.config.reply_max_messages,
                len(text),
                preview_text(text),
            )
            if DEBUG_BODIES:
                logger.debug("tg agent chunk full req=%s:\n%s", pending.req_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tg handler error req=%s", pending.req_id if pending else "-")
            pending.error = exc
            pending.done.set()

    async def complete(
        self,
        user_prompt: str,
        *,
        voice: bytes | None = None,
        want_audio: bool = False,
        req_id: str = "-",
    ) -> BridgeReply:
        """Send prompt (text and/or voice note) to agent and return aggregated reply."""
        if self._lock.locked():
            logger.warning("tg busy req=%s (another completion in flight)", req_id)
            raise BusyError("Another completion is already in progress")
        acquired = False
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=0.05)
            acquired = True
        except asyncio.TimeoutError as exc:
            logger.warning("tg busy req=%s (lock timeout)", req_id)
            raise BusyError("Another completion is already in progress") from exc
        try:
            return await self._complete_locked(
                user_prompt, voice=voice, want_audio=want_audio, req_id=req_id
            )
        finally:
            if acquired:
                self._lock.release()

    async def _complete_locked(
        self,
        user_prompt: str,
        *,
        voice: bytes | None = None,
        want_audio: bool = False,
        req_id: str = "-",
    ) -> BridgeReply:
        agent = await self.resolve_agent_entity()
        agent_id = agent.id
        agent_label = getattr(agent, "username", None) or str(agent_id)

        if voice is not None:
            caption = format_voice_caption(user_prompt)
            logger.info(
                "tg send voice start req=%s agent=@%s id=%s voice_bytes=%d caption_chars=%d",
                req_id,
                agent_label,
                agent_id,
                len(voice),
                len(caption),
            )
            voice_file = io.BytesIO(voice)
            voice_file.name = "voice.ogg"
            try:
                sent = await self._client.send_file(
                    agent, voice_file, voice_note=True, caption=caption
                )
            except Exception:
                logger.exception("tg send voice failed req=%s agent=%s", req_id, agent_id)
                raise
        else:
            outbound = format_outbound_message(user_prompt)
            logger.info(
                "tg send start req=%s agent=@%s id=%s outbound_chars=%d",
                req_id,
                agent_label,
                agent_id,
                len(outbound),
            )
            if DEBUG_BODIES:
                logger.debug("tg send full req=%s:\n%s", req_id, outbound)
            try:
                sent = await self._client.send_message(agent, outbound)
            except Exception:
                logger.exception("tg send failed req=%s agent=%s", req_id, agent_id)
                raise

        after_id = sent.id
        logger.info(
            "tg send ok req=%s agent_id=%s msg_id=%s prompt_chars=%d preview=%r",
            req_id,
            agent_id,
            after_id,
            len(user_prompt),
            preview_text(user_prompt),
        )

        pending = _PendingWait(
            agent_id=agent_id,
            after_message_id=after_id,
            req_id=req_id,
            want_audio=want_audio,
        )
        self._pending = pending
        timeout = float(self.config.reply_timeout_sec)
        quiet = float(self.config.reply_quiet_ms) / 1000.0
        max_messages = int(self.config.reply_max_messages)

        logger.info(
            "tg wait req=%s timeout_sec=%s quiet_ms=%s max_messages=%s after_msg_id=%s",
            req_id,
            self.config.reply_timeout_sec,
            self.config.reply_quiet_ms,
            max_messages,
            after_id,
        )

        wait_started = time.monotonic()
        try:
            if want_audio:
                return await self._wait_for_voice(pending, timeout, wait_started)

            try:
                await asyncio.wait_for(pending.first_received.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                waited = int((time.monotonic() - wait_started) * 1000)
                logger.error(
                    "tg timeout req=%s waited_ms=%d after_msg_id=%s (no bot reply)",
                    req_id,
                    waited,
                    after_id,
                )
                raise ReplyTimeoutError(
                    f"Timed out after {self.config.reply_timeout_sec}s waiting for Telegram agent reply"
                ) from exc

            if pending.error:
                raise pending.error

            deadline = time.monotonic() + timeout
            while True:
                if len(pending.chunks) >= max_messages:
                    logger.debug("tg aggregation hit max_messages req=%s", req_id)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.debug("tg aggregation overall timeout during quiet wait req=%s", req_id)
                    break
                sleep_for = min(quiet, remaining)
                await asyncio.sleep(sleep_for)
                since_last = time.monotonic() - pending.last_message_at
                if since_last >= quiet - 1e-3:
                    break

            reply = "\n\n".join(pending.chunks).strip()
            if not reply:
                raise ReplyTimeoutError("Agent replied without usable text")
            waited = int((time.monotonic() - wait_started) * 1000)
            logger.info(
                "tg reply ready req=%s chunks=%d chars=%d waited_ms=%d preview=%r",
                req_id,
                len(pending.chunks),
                len(reply),
                waited,
                preview_text(reply),
            )
            if DEBUG_BODIES:
                logger.debug("tg reply full req=%s:\n%s", req_id, reply)
            return BridgeReply(text=reply)
        finally:
            self._pending = None

    async def _wait_for_voice(
        self, pending: _PendingWait, timeout: float, wait_started: float
    ) -> BridgeReply:
        """Voice mode: the agent's voice note closes the reply (SPEC §5.7).

        Text arriving before the voice note is accumulated as transcript and never
        ends the wait. On timeout, accumulated text is returned as a degraded
        text-only reply instead of failing.
        """
        req_id = pending.req_id
        try:
            await asyncio.wait_for(pending.voice_received.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            if pending.error:
                raise pending.error
            text = "\n\n".join(pending.chunks).strip()
            waited = int((time.monotonic() - wait_started) * 1000)
            if text:
                logger.warning(
                    "tg voice fallback req=%s waited_ms=%d: no voice note, returning %d text chars",
                    req_id,
                    waited,
                    len(text),
                )
                return BridgeReply(text=text)
            logger.error(
                "tg voice timeout req=%s waited_ms=%d (no reply at all)", req_id, waited
            )
            raise ReplyTimeoutError(
                f"Timed out after {self.config.reply_timeout_sec}s waiting for Telegram agent voice reply"
            ) from exc

        if pending.error:
            raise pending.error

        msg = pending.voice_msg
        data = await self._client.download_media(msg, file=bytes)
        if not data:
            raise RuntimeError("Agent voice note could not be downloaded")

        voice_doc = getattr(msg, "voice", None)
        mime = getattr(voice_doc, "mime_type", None) or "audio/ogg"
        duration_ms: int | None = None
        for attr in getattr(voice_doc, "attributes", None) or []:
            if isinstance(attr, DocumentAttributeAudio) and attr.duration:
                duration_ms = int(attr.duration * 1000)
                break

        text = "\n\n".join(pending.chunks).strip()
        waited = int((time.monotonic() - wait_started) * 1000)
        logger.info(
            "tg voice reply ready req=%s audio_bytes=%d mime=%s duration_ms=%s text_chars=%d waited_ms=%d",
            req_id,
            len(data),
            mime,
            duration_ms,
            len(text),
            waited,
        )
        if DEBUG_BODIES and text:
            logger.debug("tg voice transcript full req=%s:\n%s", req_id, text)
        return BridgeReply(
            text=text,
            audio=ReplyAudio(data=data, mime_type=mime, duration_ms=duration_ms),
        )


def _message_text(msg) -> str:
    text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    if text:
        return str(text).strip()
    raw = getattr(msg, "raw_text", None)
    if raw:
        return str(raw).strip()
    return ""


class BusyError(Exception):
    """Another completion is in flight."""


class ReplyTimeoutError(Exception):
    """Agent did not reply in time."""
