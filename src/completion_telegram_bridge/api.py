"""FastAPI OpenAI-compatible routes."""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from completion_telegram_bridge import __version__
from completion_telegram_bridge.config import (
    AUDIO_EXPIRES_SEC,
    MAX_VOICE_INPUT_BYTES,
    VOICE_INPUT_FORMATS,
    BridgeConfig,
)
from completion_telegram_bridge.logging_setup import DEBUG_BODIES, preview_text, redact_auth_header
from completion_telegram_bridge.telegram_bridge import BusyError, ReplyTimeoutError, TelegramBridge

logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    role: str
    content: Any = None


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool | None = False
    # OpenAI audio extensions: modalities selects audio output; `audio` is the
    # output config (voice/format) — accepted but replies are always Telegram OGG/Opus.
    modalities: list[str] | None = None
    audio: dict[str, Any] | None = None
    model_config = {"extra": "allow"}


class VoiceInputError(ValueError):
    """Invalid `input_audio` content part; message is safe to return to the client."""


@dataclass
class UserContent:
    text: str
    voice: bytes | None = None


def _decode_input_audio(part: dict[str, Any], existing: bytes | None) -> bytes:
    if existing is not None:
        raise VoiceInputError("Only one input_audio part per request is supported")
    payload = part.get("input_audio")
    if not isinstance(payload, dict):
        raise VoiceInputError("input_audio part must contain an 'input_audio' object")
    fmt = str(payload.get("format", "")).lower()
    if fmt not in VOICE_INPUT_FORMATS:
        raise VoiceInputError(
            f"Unsupported input_audio format {fmt!r}: the bridge forwards audio without "
            "transcoding, send OGG/Opus (format 'ogg' or 'opus')"
        )
    data = payload.get("data")
    if not data or not isinstance(data, str):
        raise VoiceInputError("input_audio.data must be a non-empty base64 string")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VoiceInputError("input_audio.data is not valid base64") from exc
    if not raw:
        raise VoiceInputError("input_audio.data decodes to zero bytes")
    if len(raw) > MAX_VOICE_INPUT_BYTES:
        raise VoiceInputError(
            f"input_audio exceeds the {MAX_VOICE_INPUT_BYTES} byte limit"
        )
    return raw


def extract_user_content(messages: list[ChatMessage]) -> UserContent:
    """Latest user message's text and/or voice input."""
    for msg in reversed(messages):
        if msg.role != "user":
            continue
        content = msg.content
        if content is None:
            continue
        if isinstance(content, str):
            text = content.strip()
            if text:
                return UserContent(text=text)
            continue
        if isinstance(content, list):
            parts: list[str] = []
            voice: bytes | None = None
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    if part.get("type") == "input_audio":
                        voice = _decode_input_audio(part, voice)
                    elif part.get("type") == "text" and part.get("text"):
                        parts.append(str(part["text"]))
                    elif "text" in part and part["text"]:
                        parts.append(str(part["text"]))
            joined = "\n".join(p.strip() for p in parts if p and str(p).strip()).strip()
            if joined or voice is not None:
                return UserContent(text=joined, voice=voice)
    raise ValueError("No user message with text or audio content found")


def extract_user_text(messages: list[ChatMessage]) -> str:
    content = extract_user_content(messages)
    if not content.text:
        raise ValueError("No user message with text content found")
    return content.text


def _err(message: str, type_: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "code": code}}


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        req_id = uuid.uuid4().hex[:8]
        request.state.req_id = req_id
        started = time.monotonic()
        client = request.client.host if request.client else "?"
        auth = redact_auth_header(request.headers.get("authorization"))
        ua = request.headers.get("user-agent", "")

        logger.info(
            "http request id=%s %s %s client=%s auth=%s ua=%r",
            req_id,
            request.method,
            request.url.path,
            client,
            auth,
            ua[:80],
        )
        if DEBUG_BODIES and request.method in {"POST", "PUT", "PATCH"}:
            # request.body() is cached by Starlette; safe for downstream parsers
            body = await request.body()
            try:
                text = body.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                text = f"<{len(body)} bytes>"
            logger.debug("http body id=%s %s", req_id, text[:8000])

        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.exception(
                "http unhandled error id=%s %s %s elapsed_ms=%d",
                req_id,
                request.method,
                request.url.path,
                elapsed_ms,
            )
            raise

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "http response id=%s status=%s elapsed_ms=%d",
            req_id,
            response.status_code,
            elapsed_ms,
        )
        return response


def create_app(config: BridgeConfig, bridge: TelegramBridge) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("Starting Telegram client…")
        await bridge.start()
        logger.info(
            "Telegram bridge ready agent_id=%s agent_username=%s timeout_sec=%s",
            config.agent_id,
            config.agent_username,
            config.reply_timeout_sec,
        )
        try:
            yield
        finally:
            logger.info("Stopping Telegram client…")
            await bridge.stop()

    app = FastAPI(
        title="completion-telegram-bridge",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestLogMiddleware)

    def require_auth(authorization: str | None, req_id: str = "-") -> None:
        expected = config.api_token
        if not expected:
            logger.error("auth fail id=%s reason=no_api_token_configured", req_id)
            raise HTTPException(
                status_code=500,
                detail=_err("Server misconfigured: no api_token", "server_error", "misconfigured"),
            )
        if not authorization or not authorization.lower().startswith("bearer "):
            logger.warning("auth fail id=%s reason=missing_or_malformed_bearer", req_id)
            raise HTTPException(
                status_code=401,
                detail=_err("Invalid Authentication", "invalid_request_error", "invalid_api_key"),
            )
        token = authorization.split(" ", 1)[1].strip()
        if not secrets.compare_digest(token, expected):
            logger.warning("auth fail id=%s reason=token_mismatch", req_id)
            raise HTTPException(
                status_code=401,
                detail=_err("Invalid Authentication", "invalid_request_error", "invalid_api_key"),
            )
        logger.debug("auth ok id=%s", req_id)

    @app.exception_handler(HTTPException)
    async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
        req_id = getattr(request.state, "req_id", "-")
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = detail
            code = detail.get("error", {}).get("code", "")
            msg = detail.get("error", {}).get("message", "")
        else:
            body = _err(str(detail), "invalid_request_error", "error")
            code = "error"
            msg = str(detail)
        logger.warning(
            "http error id=%s status=%s code=%s message=%s",
            req_id,
            exc.status_code,
            code,
            msg,
        )
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/models")
    async def list_models(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        req_id = getattr(request.state, "req_id", "-")
        require_auth(authorization, req_id)
        mid = config.model_id or "telegram-agent"
        logger.info("models list id=%s model=%s", req_id, mid)
        return {
            "object": "list",
            "data": [
                {
                    "id": mid,
                    "object": "model",
                    "owned_by": "completion-telegram-bridge",
                }
            ],
        }

    async def chat_completions(
        body: ChatCompletionRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        req_id = getattr(request.state, "req_id", "-")
        require_auth(authorization, req_id)

        if body.stream:
            logger.warning("reject stream=true id=%s", req_id)
            raise HTTPException(
                status_code=400,
                detail=_err(
                    "Streaming is not supported",
                    "invalid_request_error",
                    "stream_not_supported",
                ),
            )
        if not body.messages:
            raise HTTPException(
                status_code=400,
                detail=_err("messages is required", "invalid_request_error", "invalid_messages"),
            )
        try:
            user = extract_user_content(body.messages)
        except VoiceInputError as exc:
            logger.warning("invalid audio input id=%s err=%s", req_id, exc)
            raise HTTPException(
                status_code=400,
                detail=_err(str(exc), "invalid_request_error", "invalid_audio"),
            ) from exc
        except ValueError as exc:
            logger.warning("invalid messages id=%s err=%s", req_id, exc)
            raise HTTPException(
                status_code=400,
                detail=_err(str(exc), "invalid_request_error", "invalid_messages"),
            ) from exc

        want_audio = "audio" in [m.lower() for m in (body.modalities or [])]
        model = body.model or config.model_id or "telegram-agent"
        logger.info(
            "completion start id=%s model=%s messages=%d prompt_chars=%d voice_bytes=%s "
            "want_audio=%s prompt=%r",
            req_id,
            model,
            len(body.messages),
            len(user.text),
            len(user.voice) if user.voice is not None else 0,
            want_audio,
            preview_text(user.text),
        )
        if DEBUG_BODIES and user.text:
            logger.debug("completion full prompt id=%s:\n%s", req_id, user.text)

        started = time.monotonic()
        try:
            reply = await bridge.complete(
                user.text, voice=user.voice, want_audio=want_audio, req_id=req_id
            )
        except BusyError as exc:
            logger.warning("completion busy id=%s", req_id)
            raise HTTPException(
                status_code=429,
                detail=_err(str(exc), "rate_limit_error", "concurrent_request"),
            ) from exc
        except ReplyTimeoutError as exc:
            logger.error("completion timeout id=%s: %s", req_id, exc)
            raise HTTPException(
                status_code=504,
                detail=_err(str(exc), "timeout_error", "telegram_reply_timeout"),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("completion telegram error id=%s", req_id)
            raise HTTPException(
                status_code=502,
                detail=_err(f"Telegram error: {exc}", "server_error", "telegram_error"),
            ) from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        logger.info(
            "completion ok id=%s completion_id=%s model=%s elapsed_ms=%d reply_chars=%d "
            "audio_bytes=%s reply=%r",
            req_id,
            completion_id,
            model,
            elapsed_ms,
            len(reply.text),
            len(reply.audio.data) if reply.audio is not None else 0,
            preview_text(reply.text),
        )
        if DEBUG_BODIES and reply.text:
            logger.debug("completion full reply id=%s:\n%s", req_id, reply.text)

        if reply.audio is not None:
            # OpenAI audio-output shape: content null, audio holds data + transcript
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "audio": {
                    "id": f"audio_{uuid.uuid4().hex[:24]}",
                    "data": base64.b64encode(reply.audio.data).decode("ascii"),
                    "transcript": reply.text,
                    "expires_at": int(time.time()) + AUDIO_EXPIRES_SEC,
                },
            }
        else:
            message = {"role": "assistant", "content": reply.text}

        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

    # Even Hub / G2 may POST to the configured URL as-is. Observed in production:
#   POST /v1  (base URL ending in /v1)
# also support standard OpenAI paths and bare root.
    app.post("/v1/chat/completions")(chat_completions)
    app.post("/chat/completions")(chat_completions)
    app.post("/v1")(chat_completions)
    app.post("/")(chat_completions)

    return app
