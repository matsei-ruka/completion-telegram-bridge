# completion-telegram-bridge

Bridge an [Even Realities G2](https://www.evenrealities.com/) custom agent (OpenAI-compatible completion API) to a personal agent that lives on Telegram.

Even Realities G2 glasses can replace Even AI with a custom agent configured by:

| Setting | Purpose |
|---------|---------|
| **Name** | Display name for the agent |
| **URL** | Base URL of an OpenAI-format completions API |
| **Token** | Bearer token for that API |

This project implements that URL: an HTTP service that accepts OpenAI-style chat/completions requests, forwards the user message to your Telegram agent, waits for the reply, and returns it in the expected completion format.

## Why

Your personal agent already runs as a Telegram bot. The glasses cannot talk to Telegram directly; they only call a completion-style HTTP API. This bridge sits in the middle:

```
Even Realities G2  →  OpenAI-compatible HTTP API (this service)  →  Telegram  →  your agent bot
                         ←  wait for agent reply  ←  Telegram  ←
```

## How it works (intended design)

1. The glasses send a chat completion request (`POST .../v1/chat/completions` or equivalent) with the user’s prompt.
2. The bridge authenticates the request (configured token).
3. Using the **Telegram user API** (your own account, not the bot token of the agent), it sends the prompt as a message to the agent bot.
4. The HTTP request **stays open** while the bridge waits for the agent’s reply on Telegram.
5. When the agent responds, the bridge maps that text into an OpenAI-compatible completion response and returns it to the glasses.

Keeping the completion request open for the full round-trip matches how Even AI–style agents are expected to behave: one HTTP call, one answer.

## Goals

- **Drop-in URL** for Even Realities G2 custom agent configuration
- **OpenAI-compatible** request/response shape (chat completions)
- **Telegram user session** to message an existing bot/agent
- **Synchronous wait**: bridge holds the HTTP request until the agent replies (with sensible timeouts)
- Simple configuration (env / config file): Telegram session, agent chat id, API token, listen address

## Non-goals (for now)

- Multi-user / multi-tenant hosting
- Full OpenAI feature parity (streaming, tools, vision, embeddings, etc.) unless needed by the glasses
- Replacing or rehosting the Telegram agent itself

## Status

**Bootstrap.** Specs and implementation plans come next. No runtime yet.

## Configuration (preview)

Expected knobs once implemented:

| Variable / setting | Description |
|--------------------|-------------|
| API listen URL/port | Where the glasses (or a tunnel) will reach the bridge |
| API bearer token | Same value configured as **Token** on the glasses |
| Telegram session | User account credentials/session used to talk to the agent |
| Agent target | Bot username or chat id of your Telegram agent |
| Reply timeout | Max time to wait for the agent before failing the completion |

Exact names and formats will be fixed in the specs.

## Repo

```text
completion-telegram-bridge/
├── README.md          # this file
└── (specs & code TBD)
```

## License

TBD.
