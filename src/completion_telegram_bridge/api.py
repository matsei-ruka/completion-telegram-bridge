"""FastAPI OpenAI-compatible routes."""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from completion_telegram_bridge import __version__
from completion_telegram_bridge.config import BridgeConfig
from completion_telegram_bridge.telegram_bridge import BusyError, ReplyTimeoutError, TelegramBridge

logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    role: str
    content: Any = None


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool | None = False
    model_config = {"extra": "allow"}


def extract_user_text(messages: list[ChatMessage]) -> str:
    for msg in reversed(messages):
        if msg.role != "user":
            continue
        content = msg.content
        if content is None:
            continue
        if isinstance(content, str):
            text = content.strip()
            if text:
                return text
            continue
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    if part.get("type") == "text" and part.get("text"):
                        parts.append(str(part["text"]))
                    elif "text" in part and part["text"]:
                        parts.append(str(part["text"]))
            joined = "\n".join(p.strip() for p in parts if p and str(p).strip()).strip()
            if joined:
                return joined
    raise ValueError("No user message with text content found")


def _err(message: str, type_: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "code": code}}


def create_app(config: BridgeConfig, bridge: TelegramBridge) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await bridge.start()
        logger.info("Telegram bridge ready")
        try:
            yield
        finally:
            await bridge.stop()

    app = FastAPI(
        title="completion-telegram-bridge",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    def require_auth(authorization: str | None) -> None:
        expected = config.api_token
        if not expected:
            raise HTTPException(
                status_code=500,
                detail=_err("Server misconfigured: no api_token", "server_error", "misconfigured"),
            )
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail=_err("Invalid Authentication", "invalid_request_error", "invalid_api_key"),
            )
        token = authorization.split(" ", 1)[1].strip()
        if not secrets.compare_digest(token, expected):
            raise HTTPException(
                status_code=401,
                detail=_err("Invalid Authentication", "invalid_request_error", "invalid_api_key"),
            )

    @app.exception_handler(HTTPException)
    async def http_exc_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = detail
        else:
            body = _err(str(detail), "invalid_request_error", "error")
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(authorization)
        mid = config.model_id or "telegram-agent"
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
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_auth(authorization)
        if body.stream:
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
            user_text = extract_user_text(body.messages)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=_err(str(exc), "invalid_request_error", "invalid_messages"),
            ) from exc

        model = body.model or config.model_id or "telegram-agent"
        started = time.monotonic()
        try:
            reply = await bridge.complete(user_text)
        except BusyError as exc:
            raise HTTPException(
                status_code=429,
                detail=_err(str(exc), "rate_limit_error", "concurrent_request"),
            ) from exc
        except ReplyTimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail=_err(str(exc), "timeout_error", "telegram_reply_timeout"),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("Telegram completion failed")
            raise HTTPException(
                status_code=502,
                detail=_err(f"Telegram error: {exc}", "server_error", "telegram_error"),
            ) from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "completion ok model=%s elapsed_ms=%d reply_chars=%d",
            model,
            elapsed_ms,
            len(reply),
        )
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

    app.post("/v1/chat/completions")(chat_completions)
    app.post("/")(chat_completions)

    return app
