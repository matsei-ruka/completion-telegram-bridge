# Spec: completion-telegram-bridge (v0.3)

**Status:** approved for implementation  
**Date:** 2026-07-13 (v0.3 adds voice; v0.2 approved 2026-07-09)  
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
3. Prefix every outbound Telegram message with a fixed generic marker (see §5.5).
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
| `POST` | `/v1` | Alias — Even Hub often POSTs here when URL ends with `/v1` |
| `POST` | `/chat/completions` | Alias |
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

**Voice (v0.3)** — same route, standard OpenAI audio shape:

```json
{
  "modalities": ["text", "audio"],
  "audio": { "voice": "alloy", "format": "opus" },
  "messages": [{
    "role": "user",
    "content": [{
      "type": "input_audio",
      "input_audio": { "data": "<base64>", "format": "ogg" }
    }]
  }]
}
```

- `input_audio.format` must be `ogg` or `opus` (OGG/Opus): the bridge forwards the audio to Telegram **without transcoding**, and Telegram renders a voice note only for OGG/Opus. This is the single deliberate extension over the OpenAI enum (`wav|mp3`).
- Decoded audio hard limit: 10 MB. One `input_audio` part per request.
- `modalities` containing `"audio"` selects voice-reply semantics (§5.7). The `audio` output config is accepted but ignored — replies are always the agent's OGG/Opus voice note.
- Text parts may accompany the audio; they become the voice note caption after the generic marker.

### 4.4 Response (success)

Standard non-streaming `chat.completion` JSON, `choices[0].message.content` = agent reply text. Token usage may be zeros.

**Voice (v0.3)** — when a voice note is returned, standard OpenAI audio-output shape:

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "audio": {
        "id": "audio_…",
        "data": "<base64 OGG/Opus>",
        "transcript": "agent text, if any",
        "expires_at": 1752403200
      }
    },
    "finish_reason": "stop"
  }]
}
```

- `transcript` = the agent's text messages/caption aggregated (the agent sends the same text it speaks).
- `expires_at` is advisory (now + 15 min): audio is inline, nothing is stored server-side.
- If the agent never sends a voice note within the timeout but did send text, the bridge degrades to a plain text completion (`content` set, no `audio`).

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
[sent from personal assistant, answer fast and concise]

<user prompt text>
```

The fixed prefix is a product constant (not user-editable in v1). It is generic
across clients (Even Realities G2 glasses and the Android assistant app).

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

### 5.7 Voice notes (v0.3)

The bridge is pure transport: no ASR, no TTS, no transcoding.

**Outbound.** `input_audio` bytes are sent as a real Telegram voice note
(`send_file(..., voice_note=True)`) from the operator's user account. The generic marker
(and any text parts) become the caption, truncated to Telegram's 1024-char limit.

**Inbound — the voice note closes the reply.** When the request has
`modalities: ["…", "audio"]`:

1. Agent text arriving before the voice note is accumulated as transcript and never
   ends the wait (interim/status messages are harmless).
2. The first qualifying voice note ends the wait immediately — no quiet window.
   Its caption joins the transcript. The file is downloaded via Telethon and returned
   inline as base64.
3. On `reply_timeout_sec` expiry with accumulated text: return a text-only completion
   (degraded, no error). With nothing: `504`.

Requests without audio modality keep the §5.5 text semantics unchanged (voice notes
are ignored except their caption text, as before).

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
- [ ] Outbound Telegram messages use the fixed generic prefix + blank line + prompt.
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
| Outbound prefix | `[sent from personal assistant, answer fast and concise]` + blank line |
| Agent pick | CLI lists bots / select by username |
| Stack | Python 3.11+, Telethon, FastAPI/uvicorn |
| Streaming | Unsupported (`400`) |
| Concurrency | Single-flight (`429`) |
| Timeout | `504` |
| Voice API shape (v0.3) | 100% OpenAI chat-completions audio: `input_audio` base64 in, `message.audio {id,data,transcript,expires_at}` out — no custom routes, no multipart, no media URLs |
| Voice input format (v0.3) | OGG/Opus only (`format: "ogg"|"opus"`); sole extension over OpenAI's `wav|mp3` enum — bridge never transcodes |
| Voice reply end (v0.3) | Agent's voice note closes the reply; interim text = transcript; timeout with text → degraded text completion |

---

*v0.3 — implementation proceeds from this document.*
