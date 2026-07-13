"""Voice (OpenAI audio) tests: extraction, API shape, and bridge wait logic."""

import asyncio
import base64
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from telethon.tl.types import DocumentAttributeAudio

from completion_telegram_bridge.api import (
    ChatMessage,
    VoiceInputError,
    create_app,
    extract_user_content,
)
from completion_telegram_bridge.config import (
    BRIDGE_MESSAGE_PREFIX,
    BridgeConfig,
    format_voice_caption,
)

# Generic marker shared by all clients (G2 glasses + Android assistant app).
EXPECTED_PREFIX = "[sent from personal assistant, answer fast and concise]"
from completion_telegram_bridge.telegram_bridge import (
    BridgeReply,
    ReplyAudio,
    ReplyTimeoutError,
    TelegramBridge,
    _PendingWait,
)

OGG_BYTES = b"OggS" + bytes(32)
OGG_B64 = base64.b64encode(OGG_BYTES).decode()


def audio_part(data: str = OGG_B64, fmt: str = "ogg") -> dict:
    return {"type": "input_audio", "input_audio": {"data": data, "format": fmt}}


def user_msg(content) -> ChatMessage:
    return ChatMessage(role="user", content=content)


# ---------------------------------------------------------------- caption


def test_voice_caption_uses_generic_prefix_when_empty():
    assert BRIDGE_MESSAGE_PREFIX == EXPECTED_PREFIX
    assert format_voice_caption("   ") == EXPECTED_PREFIX


def test_voice_caption_prepends_generic_prefix_to_text():
    caption = format_voice_caption("  ciao  ")
    assert caption.startswith(EXPECTED_PREFIX)
    # Prefix + blank line + text contract, same as the text transport.
    assert caption == f"{EXPECTED_PREFIX}\n\nciao"


# ---------------------------------------------------------------- extraction


def test_extract_audio_only():
    content = extract_user_content([user_msg([audio_part()])])
    assert content.voice == OGG_BYTES
    assert content.text == ""


def test_extract_text_and_audio():
    content = extract_user_content(
        [user_msg([{"type": "text", "text": "ciao"}, audio_part()])]
    )
    assert content.voice == OGG_BYTES
    assert content.text == "ciao"


def test_extract_rejects_unsupported_format():
    with pytest.raises(VoiceInputError, match="transcoding"):
        extract_user_content([user_msg([audio_part(fmt="wav")])])


def test_extract_rejects_bad_base64():
    with pytest.raises(VoiceInputError, match="base64"):
        extract_user_content([user_msg([audio_part(data="not@@base64!!")])])


def test_extract_rejects_multiple_audio_parts():
    with pytest.raises(VoiceInputError, match="one input_audio"):
        extract_user_content([user_msg([audio_part(), audio_part()])])


# ---------------------------------------------------------------- API shape


class FakeBridge:
    def __init__(self, reply: BridgeReply):
        self.reply = reply
        self.calls: list[dict] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def complete(self, user_prompt, *, voice=None, want_audio=False, req_id="-"):
        self.calls.append(
            {"prompt": user_prompt, "voice": voice, "want_audio": want_audio}
        )
        return self.reply


def make_client(reply: BridgeReply) -> tuple[TestClient, FakeBridge]:
    config = BridgeConfig(api_token="tok")
    bridge = FakeBridge(reply)
    app = create_app(config, bridge)
    return TestClient(app), bridge


AUTH = {"Authorization": "Bearer tok"}


def test_voice_completion_returns_openai_audio_shape():
    reply = BridgeReply(
        text="risposta",
        audio=ReplyAudio(data=OGG_BYTES, mime_type="audio/ogg", duration_ms=4200),
    )
    client, bridge = make_client(reply)
    with client:
        resp = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "telegram-agent",
                "modalities": ["text", "audio"],
                "audio": {"voice": "alloy", "format": "opus"},
                "messages": [{"role": "user", "content": [audio_part()]}],
            },
        )
    assert resp.status_code == 200
    message = resp.json()["choices"][0]["message"]
    assert message["content"] is None
    audio = message["audio"]
    assert base64.b64decode(audio["data"]) == OGG_BYTES
    assert audio["transcript"] == "risposta"
    assert audio["id"].startswith("audio_")
    assert audio["expires_at"] > int(time.time())
    call = bridge.calls[0]
    assert call["voice"] == OGG_BYTES
    assert call["want_audio"] is True


def test_text_completion_unchanged():
    client, bridge = make_client(BridgeReply(text="ok"))
    with client:
        resp = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "ping"}]},
        )
    assert resp.status_code == 200
    message = resp.json()["choices"][0]["message"]
    assert message["content"] == "ok"
    assert "audio" not in message
    call = bridge.calls[0]
    assert call["voice"] is None
    assert call["want_audio"] is False


def test_voice_fallback_text_only_reply():
    client, _ = make_client(BridgeReply(text="solo testo"))
    with client:
        resp = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "modalities": ["text", "audio"],
                "messages": [{"role": "user", "content": [audio_part()]}],
            },
        )
    assert resp.status_code == 200
    message = resp.json()["choices"][0]["message"]
    assert message["content"] == "solo testo"
    assert "audio" not in message


def test_unsupported_input_format_is_400():
    client, _ = make_client(BridgeReply(text="x"))
    with client:
        resp = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": [audio_part(fmt="mp3")]}]},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_audio"


# ------------------------------------------------------- bridge wait logic


class FakeTelethonClient:
    def __init__(self, media: bytes):
        self.media = media

    async def download_media(self, msg, file=None):
        return self.media


def make_bridge(media: bytes = OGG_BYTES) -> TelegramBridge:
    bridge = object.__new__(TelegramBridge)
    bridge.config = BridgeConfig(reply_timeout_sec=1)
    bridge._client = FakeTelethonClient(media)
    bridge._pending = None
    return bridge


def text_event(msg_id: int, text: str, sender: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(id=msg_id, message=text, voice=None),
        sender_id=sender,
    )


def voice_event(msg_id: int, caption: str = "", sender: int = 7) -> SimpleNamespace:
    voice_doc = SimpleNamespace(
        mime_type="audio/ogg",
        attributes=[DocumentAttributeAudio(duration=4, voice=True)],
    )
    return SimpleNamespace(
        message=SimpleNamespace(id=msg_id, message=caption, voice=voice_doc),
        sender_id=sender,
    )


async def test_voice_note_closes_reply_and_keeps_interim_text():
    bridge = make_bridge()
    pending = _PendingWait(agent_id=7, after_message_id=10, want_audio=True)
    bridge._pending = pending

    await bridge._on_new_message(text_event(11, "ci sto lavorando"))
    assert not pending.voice_received.is_set()

    await bridge._on_new_message(voice_event(12, caption="ecco la risposta"))
    assert pending.voice_received.is_set()

    reply = await bridge._wait_for_voice(pending, timeout=1.0, wait_started=time.monotonic())
    assert reply.audio is not None
    assert reply.audio.data == OGG_BYTES
    assert reply.audio.duration_ms == 4000
    assert "ci sto lavorando" in reply.text
    assert "ecco la risposta" in reply.text


async def test_voice_timeout_falls_back_to_text():
    bridge = make_bridge()
    pending = _PendingWait(agent_id=7, after_message_id=10, want_audio=True)
    bridge._pending = pending

    await bridge._on_new_message(text_event(11, "solo testo, niente voce"))

    reply = await bridge._wait_for_voice(pending, timeout=0.05, wait_started=time.monotonic())
    assert reply.audio is None
    assert reply.text == "solo testo, niente voce"


async def test_voice_timeout_without_any_reply_raises():
    bridge = make_bridge()
    pending = _PendingWait(agent_id=7, after_message_id=10, want_audio=True)
    bridge._pending = pending

    with pytest.raises(ReplyTimeoutError):
        await bridge._wait_for_voice(pending, timeout=0.05, wait_started=time.monotonic())


async def test_text_mode_ignores_voice_note():
    bridge = make_bridge()
    pending = _PendingWait(agent_id=7, after_message_id=10, want_audio=False)
    bridge._pending = pending

    # caption text is still collected; the audio itself is ignored in text mode
    await bridge._on_new_message(voice_event(12, caption="didascalia"))
    assert not pending.voice_received.is_set()
    assert pending.chunks == ["didascalia"]
