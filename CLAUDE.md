# completion-telegram-bridge

OpenAI-compatible chat-completions API that fulfills requests by DMing a Telegram
agent bot from the operator's own user account (Telethon/MTProto) and waiting for
the reply. Clients: Even Realities G2 glasses (text) and an Android assistant app
(voice). Single-operator, single-node, no Docker; nginx terminates TLS in front.

Authoritative design doc: `docs/SPEC.md`. Read it before changing behaviour.

## Layout

- `src/completion_telegram_bridge/api.py` — FastAPI app, auth, OpenAI request/response
  shaping (incl. audio: `input_audio` in, `message.audio` out)
- `src/completion_telegram_bridge/telegram_bridge.py` — Telethon client, single-flight
  lock, reply wait/aggregation (`BridgeReply`), voice note send/receive
- `src/completion_telegram_bridge/config.py` — pydantic config + product constants
  (G2 marker, timeouts, voice limits), stored at `~/.config/completion-telegram-bridge/`
- `src/completion_telegram_bridge/cli.py` — `ctb` typer CLI (login, select-agent, serve…)
- `src/completion_telegram_bridge/server.py` / `logging_setup.py` — uvicorn entry, logging

## Commands

```bash
.venv/bin/python -m pytest -q      # tests (no Telegram needed)
ctb serve --debug                  # run locally with full-body logs
```

## Hard constraints

- **API stays 100% OpenAI-compliant.** No custom routes/fields. Sole sanctioned
  divergence: `input_audio.format` accepts `ogg`/`opus` (OpenAI enum is `wav|mp3`).
- **Bridge is pure transport**: no ASR, no TTS, no transcoding, no audio content in logs
  (sizes/previews only; bearer tokens redacted).
- Text semantics: first agent message + `reply_quiet_ms` quiet window aggregation.
  Voice semantics (`modalities` includes `audio`): the agent's **voice note closes the
  reply**; interim text = transcript; timeout with text → degraded text completion.
- Production config lives on the remote server (`ctb config set …` there), e.g.
  `reply_timeout_sec` is 300 in production vs 45 default.

## Conventions

- Python 3.11+, ruff line-length 100, type hints, `from __future__ import annotations`.
- Log lines are terse key=value with a per-request `id=`/`req=` correlation id.
- Errors surface as OpenAI-style `{"error": {message, type, code}}` bodies.
