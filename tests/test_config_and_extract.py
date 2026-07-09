"""Unit tests that do not need Telegram."""

from completion_telegram_bridge.api import ChatMessage, extract_user_text
from completion_telegram_bridge.config import G2_MESSAGE_PREFIX, format_outbound_message


def test_format_outbound_message():
    out = format_outbound_message("  what time is it  ")
    assert out.startswith(G2_MESSAGE_PREFIX)
    assert out == f"{G2_MESSAGE_PREFIX}\n\nwhat time is it"


def test_extract_last_user_message():
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="ok"),
        ChatMessage(role="user", content="second"),
    ]
    assert extract_user_text(messages) == "second"


def test_extract_multimodal_text_parts():
    messages = [
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "x"}},
            ],
        )
    ]
    assert extract_user_text(messages) == "hello"
