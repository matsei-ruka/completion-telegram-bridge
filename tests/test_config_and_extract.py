"""Unit tests that do not need Telegram."""

from completion_telegram_bridge.api import ChatMessage, extract_user_text
from completion_telegram_bridge.config import (
    BRIDGE_MESSAGE_PREFIX,
    format_outbound_message,
)

# The marker is generic across clients (G2 glasses + Android assistant app).
EXPECTED_PREFIX = "[sent from personal assistant, answer fast and concise]"


def test_marker_is_generic():
    assert BRIDGE_MESSAGE_PREFIX == EXPECTED_PREFIX


def test_format_outbound_message():
    out = format_outbound_message("  what time is it  ")
    assert out.startswith(EXPECTED_PREFIX)
    # Prefix + blank line + prompt contract.
    assert out == f"{EXPECTED_PREFIX}\n\nwhat time is it"


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
