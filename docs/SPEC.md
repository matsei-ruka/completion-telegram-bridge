# Spec: completion-telegram-bridge (v0.2)

**Status:** approved for implementation  
**Date:** 2026-07-09  
**Audience:** product + implementation (single-operator personal bridge)

Bridge that exposes an OpenAI-compatible chat completions HTTP API for Even Realities G2 custom agents, and fulfills each request by messaging a Telegram agent from the operator’s own Telegram account, waiting for the reply, and returning it as a completion.

---

## 1. Problem

Even Realities G2 (via Even Hub / Even AI agent configuration) can replace the default AI backend with a custom agent using three fields:

| Field | Meaning |
|-------|---------|
| **Name** | Label shown in the app |
| **URL** | HTTP endpoint the app will call |
| **Token** | Shared secret sent as `Authorization: Bearer <token>` |

The app expects an **OpenAI-style chat completions** request/response. The operator’s personal agent already lives on **Telegram as a bot**. This service is the adapter.

```
G2 / Even Hub
    │  POST chat completion (Bearer token)
    │  HTTPS via nginx (public IP, standard 443)
    ▼
completion-telegram-bridge (local process)
    │  send message as user account
    ▼
Telegram  ──►  agent bot
    │  wait for reply from agent bot
    ▼
bridge returns OpenAI-shaped completion
```

---

## 2. Goals (v1)

1. Accept authenticated chat-completion HTTP requests compatible with Even Realities G2 custom agent configuration.
2. Extract the latest user utterance from the request.
3. Prefix every outbound Telegram message with a fixed G2 marker (see §5.5).
4. Send that text to a configured Telegram agent (bot) **using the operator’s Telegram user account** (user API / MTProto), not the agent’s bot token.
5. Keep the HTTP request open until a matching agent reply arrives or a timeout elapses.
6. Return the agent’s text reply as an OpenAI-compatible non-streaming chat completion.
7. Ship as **simple installable Python software** on a normal Linux server (public IP), process bound to localhost, **HTTPS terminated by nginx** on 443 with a real certificate.
8. Provide a **CLI** for Telegram login, listing/selecting the agent among bots the user chats with, and all other configuration.

## 3. Non-goals (v1)

- **Docker / containers** — not used, not documented as a deployment path.
- Multi-tenant / multi-user hosting.
- Full OpenAI API surface (embeddings, images, tools, assistants, files, etc.).
- Streaming (`stream: true`) — return `400` if requested.
- Replacing or rehosting the Telegram agent itself.
- Deferred long-running jobs (sync wait only).
- Auto HUD truncation/markdown stripping (optional later).
- High-availability clustering.

---

## 4. External protocol: OpenAI-compatible API

### 4.1 Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat/completions` | Primary completion entrypoint |
| `POST` | `/` | Alias of chat completions |
| `GET` | `/v1/models` | Smoke / app probe |
| `GET` | `/healthz` | Liveness (no auth) |

### 4.2 Authentication

- Header: `Authorization: Bearer <token>`
- Compared to configured `api_token` (constant-time).
- Invalid/missing → `401`.

### 4.3 Request

```json
{
  "model": "any-string",
  "messages": [
    { "role": "user", "content": "..." }
  ]
}
```

- Prompt = last message with `role == "user"` and string/text content.
- `stream: true` → `400`.
- Other OpenAI fields accepted and ignored.

### 4.4 Response (success)

Standard non-streaming `chat.completion` JSON, `choices[0].message.content` = agent reply text. Token usage may be zeros.

### 4.5 Errors

| HTTP | When |
|------|------|
| `400` | Bad body, empty user text, streaming requested |
| `401` | Bad/missing token |
| `429` | Concurrent request (single-flight) |
| `502` | Telegram failure |
| `504` | Agent reply timeout |
| `500` | Unexpected |

---

## 5. Telegram integration

### 5.1 Identity

| Side | Identity | Mechanism |
|------|----------|-----------|
| Bridge | Operator’s user account | Telethon (MTProto) session |
| Agent | Existing bot | Private DM chat |

### 5.2 Session

- One-time CLI login (`ctb login`) creates a session file under the config directory.
- Service fails fast if session missing/invalid.

### 5.3 Agent selection

- CLI lists **bots** the user has dialogs with (and allows search/resolve by username).
- Operator selects one; target peer id + username stored in config.
- Runtime only talks to that peer in private chat.

### 5.4 Outbound message format

Every message sent to the agent **must** start with this exact prefix line, then a blank line, then the user prompt:

```
[sent from Even Realities G2, answer fast and concise]

<user prompt text>
```

The fixed prefix is a product constant (not user-editable in v1).

### 5.5 Reply matching

An inbound message is a reply if all hold:

1. Same chat as the configured agent.
2. From the agent peer (bot).
3. After the outbound message (message id / time).
4. Has extractable text or caption.

**Aggregation:** after first qualifying bot message, wait `reply_quiet_ms` (default 800) for more bot messages; each new one resets the quiet timer; stop at quiet expiry or `reply_max_messages` (default 10). Concatenate with `\n\n`.

### 5.6 Concurrency & timeouts

- Single-flight: second concurrent completion → `429`.
- `reply_timeout_sec` default `45` → `504` on expiry.

---

## 6. Configuration & CLI

### 6.1 Config store

- Directory: `~/.config/completion-telegram-bridge/` (override with `CTB_CONFIG_DIR`).
- Files:
  - `config.json` — settings
  - `telegram.session` — Telethon session (secret)

`config.json` fields (illustrative):

```json
{
  "api_token": "...",
  "host": "127.0.0.1",
  "port": 8787,
  "model_id": "telegram-agent",
  "telegram_api_id": 12345,
  "telegram_api_hash": "...",
  "agent_id": 123456789,
  "agent_username": "my_bot",
  "agent_title": "My Agent",
  "reply_timeout_sec": 45,
  "reply_quiet_ms": 800,
  "reply_max_messages": 10
}
```

Environment variables may override individual keys for advanced use; CLI is the primary path.

### 6.2 CLI (`ctb` / `python -m completion_telegram_bridge`)

| Command | Purpose |
|---------|---------|
| `ctb login` | Interactive Telegram login (phone, code, 2FA); needs api_id/hash first |
| `ctb logout` | Remove session (optional) |
| `ctb set-api` | Set Telegram `api_id` + `api_hash` |
| `ctb set-token` | Set bridge Bearer token (Even Hub Token) |
| `ctb agents` | List bot dialogs (bots the account has chatted with) |
| `ctb select-agent` | Interactive picker among bots; optional `@username` argument |
| `ctb config` | Show current config (secrets redacted) |
| `ctb config set <key> <value>` | Set scalar config keys (port, timeouts, host, …) |
| `ctb serve` | Run the HTTP bridge |
| `ctb status` | Session logged in? Agent set? Port? |

Interactive prompts are preferred for setup; non-interactive flags where practical.

---

## 7. Deployment (no Docker)

### 7.1 Runtime

- Install with pip/uv/pipx into a venv on the server.
- Process listens on **localhost only** by default (`127.0.0.1:8787`).
- Process manager: systemd user or system unit (example in README) — plain process, not a container.

### 7.2 Public HTTPS

- Server has a public IP and DNS name.
- **nginx** (or equivalent) terminates TLS on **443** with a real certificate (e.g. Let’s Encrypt).
- Reverse-proxy to `http://127.0.0.1:8787`.
- Even Hub URL: `https://bridge.example.com/v1` (or full path to chat completions, depending on app).
- Token: same as `api_token` in config.

### 7.3 Explicitly not supported as a product path

- Docker, Docker Compose, Kubernetes images, or container-first docs.

---

## 8. Security

1. Bearer token required; never log it.
2. Telegram session = full account access; file mode `0600`; never commit.
3. Bind localhost; only nginx faces the internet.
4. Prefer strong random `api_token`.
5. Do not log full prompts/replies at INFO.

---

## 9. Acceptance criteria (v1)

- [ ] Installable as a Python package; CLI entrypoint works.
- [ ] CLI can set api credentials, log in to Telegram, list bots, select agent, set API token, configure host/port/timeouts.
- [ ] `ctb serve` serves healthz + chat completions.
- [ ] Outbound Telegram messages use the fixed G2 prefix + blank line + prompt.
- [ ] Agent reply returned as OpenAI chat.completion.
- [ ] Multi-bubble aggregation + single-flight + timeout behaviour as specified.
- [ ] Documented nginx HTTPS reverse-proxy setup (no Docker).
- [ ] Secrets/session not in git; `.gitignore` covers config dir patterns if any local samples.

---

## 10. Decision log

| Topic | Decision |
|-------|----------|
| Containers | **No Docker** |
| Deploy | Python process + nginx HTTPS on 443 |
| Config UX | CLI-first under `~/.config/completion-telegram-bridge/` |
| Outbound prefix | `[sent from Even Realities G2, answer fast and concise]` + blank line |
| Agent pick | CLI lists bots / select by username |
| Stack | Python 3.11+, Telethon, FastAPI/uvicorn |
| Streaming | Unsupported (`400`) |
| Concurrency | Single-flight (`429`) |
| Timeout | `504` |

---

*v0.2 — implementation proceeds from this document.*
