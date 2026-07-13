"""Persistent configuration for completion-telegram-bridge."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Fixed product prefix on every outbound Telegram message (SPEC §5.4).
G2_MESSAGE_PREFIX = "[sent from Even Realities G2, answer fast and concise]"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_MODEL_ID = "telegram-agent"
DEFAULT_REPLY_TIMEOUT_SEC = 45
DEFAULT_REPLY_QUIET_MS = 800
DEFAULT_REPLY_MAX_MESSAGES = 10

# Voice input (OpenAI `input_audio` content parts).
# The bridge forwards audio as-is; Telegram renders a voice note only for OGG/Opus,
# so those are the only accepted input formats (no transcoding, SPEC §5.7).
VOICE_INPUT_FORMATS = frozenset({"ogg", "opus"})
# Telegram voice notes are ~3-4 KB/s Opus; this guards memory, not policy.
MAX_VOICE_INPUT_BYTES = 10 * 1024 * 1024
# Advisory `expires_at` on returned audio objects (data is inline, nothing is stored).
AUDIO_EXPIRES_SEC = 900

# Telegram caption hard limit.
MAX_CAPTION_CHARS = 1024

CONFIG_FILE_NAME = "config.json"
SESSION_FILE_NAME = "telegram.session"


def default_config_dir() -> Path:
    override = os.environ.get("CTB_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".config" / "completion-telegram-bridge"


class BridgeConfig(BaseModel):
    """User-facing settings stored in config.json."""

    api_token: str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    model_id: str = DEFAULT_MODEL_ID
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    agent_id: int | None = None
    agent_username: str | None = None
    agent_title: str | None = None
    reply_timeout_sec: int = DEFAULT_REPLY_TIMEOUT_SEC
    reply_quiet_ms: int = DEFAULT_REPLY_QUIET_MS
    reply_max_messages: int = DEFAULT_REPLY_MAX_MESSAGES

    def session_path(self, config_dir: Path | None = None) -> Path:
        base = config_dir or default_config_dir()
        return base / SESSION_FILE_NAME

    def redacted_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        if data.get("api_token"):
            data["api_token"] = _redact(data["api_token"])
        if data.get("telegram_api_hash"):
            data["telegram_api_hash"] = _redact(data["telegram_api_hash"])
        return data

    def validate_for_serve(self) -> list[str]:
        """Return list of human-readable problems blocking serve."""
        problems: list[str] = []
        if not self.api_token:
            problems.append("api_token is not set (run: ctb set-token)")
        if not self.telegram_api_id or not self.telegram_api_hash:
            problems.append("Telegram API credentials missing (run: ctb set-api)")
        if self.agent_id is None and not self.agent_username:
            problems.append("No agent selected (run: ctb select-agent)")
        session = self.session_path()
        # Telethon stores `<path>.session` when given path without suffix, or exact file
        candidates = [session]
        if session.suffix == ".session":
            candidates.append(Path(str(session)))
        else:
            candidates.append(Path(str(session) + ".session"))
        if not any(p.exists() for p in candidates):
            problems.append(f"Telegram session missing at {session} (run: ctb login)")
        return problems


def _redact(value: str, keep: int = 4) -> str:
    if len(value) <= keep:
        return "***"
    return value[:keep] + "…" + ("*" * min(8, len(value) - keep))


def config_path(config_dir: Path | None = None) -> Path:
    return (config_dir or default_config_dir()) / CONFIG_FILE_NAME


def ensure_config_dir(config_dir: Path | None = None) -> Path:
    path = config_dir or default_config_dir()
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(stat.S_IRWXU)  # 0700
    except OSError:
        pass
    return path


def load_config(config_dir: Path | None = None) -> BridgeConfig:
    path = config_path(config_dir)
    if not path.exists():
        return BridgeConfig()
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return BridgeConfig.model_validate(raw)


def save_config(cfg: BridgeConfig, config_dir: Path | None = None) -> Path:
    base = ensure_config_dir(config_dir)
    path = base / CONFIG_FILE_NAME
    payload = cfg.model_dump()
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
    return path


def update_config(config_dir: Path | None = None, **updates: Any) -> BridgeConfig:
    cfg = load_config(config_dir)
    data = cfg.model_dump()
    for key, value in updates.items():
        if key not in data:
            raise KeyError(f"Unknown config key: {key}")
        data[key] = value
    new_cfg = BridgeConfig.model_validate(data)
    save_config(new_cfg, config_dir)
    return new_cfg


# Keys that may be set via `ctb config set`
SETTABLE_KEYS = frozenset(
    {
        "api_token",
        "host",
        "port",
        "model_id",
        "telegram_api_id",
        "telegram_api_hash",
        "agent_id",
        "agent_username",
        "agent_title",
        "reply_timeout_sec",
        "reply_quiet_ms",
        "reply_max_messages",
    }
)


def coerce_config_value(key: str, value: str) -> Any:
    if key in {"port", "telegram_api_id", "agent_id", "reply_timeout_sec", "reply_quiet_ms", "reply_max_messages"}:
        if value.lower() in {"", "none", "null"}:
            return None
        return int(value)
    if value.lower() in {"none", "null"}:
        return None
    return value


def format_outbound_message(user_prompt: str) -> str:
    prompt = user_prompt.strip()
    return f"{G2_MESSAGE_PREFIX}\n\n{prompt}"


def format_voice_caption(user_prompt: str) -> str:
    """Caption for outbound voice notes: marker, plus any text sent with the audio."""
    prompt = user_prompt.strip()
    if not prompt:
        return G2_MESSAGE_PREFIX
    return format_outbound_message(prompt)[:MAX_CAPTION_CHARS]
