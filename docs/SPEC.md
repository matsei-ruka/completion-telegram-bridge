# Spec: completion-telegram-bridge (v0.1)

**Status:** draft for review  
**Date:** 2026-07-09  
**Audience:** product + implementation (single-operator personal bridge)

This document defines the first version of the bridge that exposes an OpenAI-compatible chat completions HTTP API for Even Realities G2 custom agents, and fulfills each request by messaging a Telegram agent from the operator’s own Telegram account, waiting for the reply, and returning it as a completion.

---

## 1. Problem

Even Realities G2 (via Even Hub / Even AI agent configuration) can replace the default AI backend with a custom agent using three fields:

| Field | Meaning |
|-------|---------|
| **Name** | Label shown in the app |
| **URL** | HTTP endpoint the app will call |
| **Token** | Shared secret sent as `Authorization: Bearer <token>` |

The app expects an **OpenAI-style chat completions** request/response. There is no official public protocol doc; community reverse-engineering shows a minimal chat-completions body with transcribed voice text (not raw audio).

The operator’s personal agent already lives on **Telegram as a bot**. The glasses cannot talk to Telegram. This service is the adapter.

```
G2 / Even Hub
    │  POST chat completion (Bearer token)
    ▼
completion-telegram-bridge
    │  send message as user account
    ▼
Telegram  ──►  agent bot
    │  wait for reply from agent bot
    ▼
bridge returns OpenAI-shaped completion
    │
    ▼
G2 display
```

---

## 2. Goals (v1)

1. Accept authenticated chat-completion HTTP requests compatible with Even Realities G2 custom agent configuration.
2. Extract the latest user utterance from the request.
3. Send that text to a configured Telegram agent (bot) **using the operator’s Telegram user account** (user API / MTProto client), not the agent’s bot token.
4. Keep the HTTP request open until a matching agent reply arrives or a timeout elapses.
5. Return the agent’s text reply as an OpenAI-compatible non-streaming chat completion.
6. Run as a single-operator service with simple env/config configuration.

## 3. Non-goals (v1)

- Multi-tenant / multi-user hosting.
- Full OpenAI API surface (embeddings, images, tools/function-calling, assistants, files, etc.).
- Streaming (`stream: true`) unless G2 is later proven to require it.
- Replacing, hosting, or modifying the Telegram agent itself.
- Long-running background jobs with deferred delivery (e.g. “result later on Telegram only”) — v1 is synchronous wait-and-return only.
- Automatic content rewriting for the G2 HUD (truncation/markdown stripping may be optional later; not required for first usable cut).
- High-availability clustering or horizontal scale-out.

---

## 4. External protocol: Even Realities / OpenAI-compatible API

### 4.1 Observed client behaviour (community)

Community probes of the G2 custom agent feature report roughly:

```http
POST <configured-url> HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json
User-Agent: Dart/3.8 (dart:io)

{
  "model": "<string, often openclaw-related>",
  "messages": [
    { "role": "user", "content": "<transcribed voice text>" }
  ]
}
```

Notes:

- Voice → text is done on-device / by the app; the bridge receives **text**.
- Path may be the **root** of the configured URL, or `/v1/chat/completions` depending on how the user configures the base URL in Even Hub. Both shapes appear in the wild:
  - Full endpoint: `http://host:port/v1/chat/completions`
  - Base URL ending in `/v1` with the app appending `/chat/completions`
  - Some probes used POST to the exact configured URL with no path suffix
- Approximate client-side patience is on the order of **~30 seconds** (reported); long waits risk client timeout even if the bridge is still waiting on Telegram.

**Spec implication:** treat Even’s client as an imperfect OpenAI subset. Be liberal in what we accept; conservative and standard in what we return.

### 4.2 Endpoints the bridge MUST implement

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat/completions` | Primary completion entrypoint |
| `POST` | `/` | Alias of chat completions (same handler) for URL configs that post to root |
| `GET` | `/v1/models` | Optional but useful for smoke tests / app probes |
| `GET` | `/healthz` | Liveness (no auth required) |

If Even Hub is configured with base URL `http://host:port/v1`, the app is expected to call `/v1/chat/completions`. Supporting both `/` and `/v1/chat/completions` maximises compatibility.

### 4.3 Authentication

- Header: `Authorization: Bearer <token>`
- Compare against configured `BRIDGE_API_TOKEN` using constant-time equality.
- Missing/invalid token → `401` with OpenAI-ish error body:

```json
{
  "error": {
    "message": "Invalid Authentication",
    "type": "invalid_request_error",
    "code": "invalid_api_key"
  }
}
```

- Empty token configuration in the bridge is **not** allowed in production mode (fail fast on startup). A explicit “dev mode” flag may allow unauthenticated local testing.

### 4.4 Request body (accepted)

Minimal required shape:

```json
{
  "model": "any-string",
  "messages": [
    { "role": "system" | "user" | "assistant", "content": "..." }
  ]
}
```

Rules:

| Field | Behaviour |
|-------|-----------|
| `messages` | Required, non-empty array |
| User content | Take the **last** message with `role == "user"` and string `content` as the prompt sent to Telegram |
| `content` as array (multimodal) | v1: if array of parts, concatenate text parts; ignore non-text; if no text → `400` |
| `model` | Echoed back in the response; ignored for routing |
| `stream` | If `true`, v1 returns `400` with a clear message (non-streaming only) **or** ignores and responds non-streaming — **decision: return 400** so misconfig is obvious |
| `temperature`, `max_tokens`, etc. | Accepted and ignored |

### 4.5 Successful response body

Non-streaming OpenAI chat completion:

```json
{
  "id": "chatcmpl-<uuid>",
  "object": "chat.completion",
  "created": 1710000000,
  "model": "<echo request model or configured default>",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "<agent reply text>"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

Notes:

- Token usage may be zeros in v1 (we are not an LLM).
- `Content-Type: application/json`.
- HTTP `200`.

### 4.6 Models list (optional probe)

`GET /v1/models` → `200`:

```json
{
  "object": "list",
  "data": [
    { "id": "telegram-agent", "object": "model", "owned_by": "completion-telegram-bridge" }
  ]
}
```

Configurable model id is fine; default `telegram-agent`.

---

## 5. Telegram integration

### 5.1 Role of the Telegram client

The bridge acts as the **operator’s personal Telegram user**, not as the agent bot.

| Side | Identity | Mechanism |
|------|----------|-----------|
| Bridge | Operator’s user account | Telegram **user API** (MTProto), session string or session file |
| Agent | Existing bot | Already running; receives DMs/group messages like any chat |

Rationale: the agent is already a bot the operator messages from their phone. Reusing the user account preserves existing bot conversations, auth state, and agent context without requiring bot-token dual access or webhook changes on the agent.

### 5.2 Library / stack (implementation recommendation)

- Language: **Python 3.11+** (good Telegram user-client ecosystem; easy for a personal service).
- Client: **Telethon** or **Pyrogram** (pick one in implementation plan; Telethon is a reasonable default).
- HTTP: FastAPI or Starlette + uvicorn (async end-to-end fits waiting on Telegram events).

The stack choice is not frozen in this v0.1 product spec; the behavioural contract below is.

### 5.3 Session bootstrap

1. One-time interactive login (phone number + code + optional 2FA password) produces a **session** (file or string).
2. Session is stored outside the git repo (path via config).
3. Service startup loads the session and connects; fails loudly if invalid/expired.
4. API id/hash (from my.telegram.org) are required configuration secrets.

### 5.4 Target agent

Config identifies the chat to message:

| Setting | Example | Notes |
|---------|---------|--------|
| `TELEGRAM_AGENT_TARGET` | `@my_agent_bot` or numeric id | Resolve username on startup; cache peer |

v1 assumes a **1:1 private chat** with the bot (simplest, clearest reply correlation). Groups are out of scope unless explicitly enabled later.

### 5.5 Send path

On each authenticated completion request:

1. Extract prompt text (section 4.4).
2. Optionally prefix with a small marker for correlation (section 5.7).
3. `send_message` to the agent target.
4. Record `outbound_message_id`, `chat_id`, timestamp, and a waiter keyed by correlation rules.
5. Do **not** return HTTP yet.

### 5.6 Wait path — what counts as “the reply”

Default policy for v1 (**first qualifying inbound message**):

An inbound message counts as the agent reply if **all** of:

1. It is in the same chat as the agent target.
2. It is **from the agent peer** (bot), not from the operator and not from other users.
3. It is received **after** the outbound message was sent (server time / message id ordering).
4. It is not a service/empty message; has extractable text (or caption).

Additional rules:

| Case | Behaviour |
|------|-----------|
| Multiple rapid messages from agent | **Concatenate** text parts in order, with `\n\n` separators, until **quiet period** elapses or hard max messages reached (see timeouts) |
| Agent edits a message | Ignore edits in v1; only new messages |
| Agent sends media without caption | Treat as empty content → wait for next text message or timeout |
| Agent is typing / chat actions | Ignore; do not complete the request |
| Operator sends another message from phone in the same chat during wait | Does not complete the request; still wait for bot |
| Stale bot messages from earlier conversations | Ignored if before the outbound message id / send timestamp |

**Quiet period (multi-message aggregation):** after the first qualifying bot message, wait `TELEGRAM_REPLY_QUIET_MS` (default **800 ms**) for more bot messages; each new bot message resets the quiet timer. Stop when quiet period passes or `TELEGRAM_REPLY_MAX_MESSAGES` (default **10**) is hit. Then complete the HTTP response with the concatenated text.

This handles agents that split long answers into several Telegram bubbles.

### 5.7 Correlation (optional hardening)

v1 default (no marker) is acceptable for single-flight personal use.

Optional config `TELEGRAM_CORRELATION_PREFIX=true`:

- Outbound text becomes something like: `[g2-<shortid>]\n<user prompt>`
- Prefer replies that reference the same id if the agent echoes it; otherwise fall back to “first reply after outbound”.

Not required for MVP if only one request is in flight.

### 5.8 Concurrency

| Setting | Default | Behaviour |
|---------|---------|-----------|
| Max concurrent completion waits | `1` | Additional requests get `429` or queue — **decision: reject with 429** in v1 to avoid mixed replies |

Rationale: one personal chat with one bot makes concurrent waiters hard to disambiguate without correlation markers. Serialising is safer for v1.

### 5.9 Timeouts

| Name | Default | Meaning |
|------|---------|---------|
| `TELEGRAM_REPLY_TIMEOUT_SEC` | `45` | Max total wait for first qualifying bot message after send |
| `TELEGRAM_REPLY_QUIET_MS` | `800` | Aggregation quiet period after last bot message |
| `HTTP_SERVER_TIMEOUT` | ≥ reply timeout + small margin | Server must not close earlier than the wait |

If the **client** (G2) times out around ~30s, the bridge may still be waiting; that is acceptable — document that the agent should answer quickly for glasses UX, and that longer agent work is a known limitation of pure sync bridging.

On timeout:

- HTTP `504` (or `200` with error message in assistant content — **decision: `504` with error JSON** so failures are explicit):

```json
{
  "error": {
    "message": "Timed out waiting for Telegram agent reply",
    "type": "timeout_error",
    "code": "telegram_reply_timeout"
  }
}
```

Optional later: `BRIDGE_TIMEOUT_AS_ASSISTANT=true` returns `200` with a short “agent did not reply in time” message for friendlier HUD behaviour.

---

## 6. End-to-end request lifecycle

```
1. Accept POST /v1/chat/completions (or /)
2. Authenticate Bearer token
3. Parse JSON; extract last user text
4. If another wait is active → 429
5. Connect/use Telegram client; send text to agent
6. Subscribe/wait for qualifying bot messages (with aggregation)
7a. Success → build chat.completion JSON → 200
7b. Timeout → 504
7c. Telegram error → 502
7d. Bad request → 400
```

Logging (structured, no secrets):

- request id
- prompt length (not full body by default; debug flag may log text)
- outbound telegram message id
- wait duration
- reply length
- outcome (ok / timeout / error)

---

## 7. Configuration

All configuration via environment variables (optional `.env` loaded only if present; never committed).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BRIDGE_HOST` | no | `0.0.0.0` | Bind address |
| `BRIDGE_PORT` | no | `8787` | Bind port |
| `BRIDGE_API_TOKEN` | **yes** | — | Bearer token; same value as Even Hub “Token” |
| `BRIDGE_MODEL_ID` | no | `telegram-agent` | Id reported by `/v1/models` and default response model |
| `TELEGRAM_API_ID` | **yes** | — | From my.telegram.org |
| `TELEGRAM_API_HASH` | **yes** | — | From my.telegram.org |
| `TELEGRAM_SESSION_PATH` | **yes** | — | Path to session file (or session string env — pick one in impl) |
| `TELEGRAM_AGENT_TARGET` | **yes** | — | `@bot_username` or numeric id |
| `TELEGRAM_REPLY_TIMEOUT_SEC` | no | `45` | Wait budget for agent reply |
| `TELEGRAM_REPLY_QUIET_MS` | no | `800` | Multi-bubble aggregation quiet period |
| `TELEGRAM_REPLY_MAX_MESSAGES` | no | `10` | Cap on aggregated bot messages |
| `LOG_LEVEL` | no | `INFO` | Logging verbosity |
| `BRIDGE_DEV_INSECURE_NO_AUTH` | no | `false` | If true, skip auth (local only) |

### 7.1 Even Hub configuration (operator)

| Even Hub field | Value |
|----------------|-------|
| Name | Any label, e.g. `My Telegram Agent` |
| URL | Reachable base, e.g. `http://<host>:8787/v1` or full `http://<host>:8787/v1/chat/completions` (document tested form during implementation) |
| Token | Same as `BRIDGE_API_TOKEN` |

Network: phone/app must reach the bridge (LAN, Tailscale, or reverse tunnel). Public internet exposure should be avoided without TLS and strong token; prefer private network / Tailscale for v1.

---

## 8. Security & privacy

1. **Token:** required; constant-time compare; never log the token.
2. **Telegram session:** treat as a full account credential; `0600` file perms; gitignore session paths.
3. **API id/hash:** secrets; env only.
4. **Prompt/reply logging:** off by default at INFO; optional DEBUG with care (personal messages).
5. **Binding:** default all interfaces is convenient for LAN devices; document firewall / Tailscale recommendations.
6. **No TLS in-process for v1** — terminate TLS at reverse proxy if exposed; personal Tailscale is preferred.
7. Telegram ToS: user-client automation can risk account limits; document that this is a personal, low-rate bridge, not a bulk scraper.

---

## 9. Error model (summary)

| HTTP | When |
|------|------|
| `400` | Malformed JSON, missing messages, empty user text, `stream: true` |
| `401` | Bad/missing Bearer token |
| `429` | Concurrent request rejected (single-flight) |
| `502` | Telegram send/connect failure |
| `504` | Agent reply timeout |
| `500` | Unexpected internal error |

Error body shape (OpenAI-inspired):

```json
{
  "error": {
    "message": "human readable",
    "type": "machine_category",
    "code": "stable_code"
  }
}
```

---

## 10. Display / UX notes (G2)

Not hard requirements for v1 correctness, but useful operator guidance:

- G2 HUD is small (community: on the order of hundreds of characters readable).
- Prefer short agent replies when used from glasses.
- Markdown, long code, and URLs may render poorly; agents can be prompted separately for “HUD mode”.
- v1 does **not** auto-truncate unless we add `BRIDGE_MAX_REPLY_CHARS` later.

---

## 11. Testing strategy (v1)

| Level | What |
|-------|------|
| Unit | Message extraction; reply aggregation; auth; timeout maths |
| Integration (mocked Telegram) | Full HTTP → fake inbound events → completion JSON |
| Manual smoke | `curl` chat completions against a real test bot or the real agent |
| Device | Configure Even Hub URL/token; speak a short prompt; confirm HUD shows agent reply |

Minimum acceptance curls (after session login helper exists):

```bash
# health
curl -s http://127.0.0.1:8787/healthz

# models
curl -s -H "Authorization: Bearer $BRIDGE_API_TOKEN" \
  http://127.0.0.1:8787/v1/models

# completion
curl -s -H "Authorization: Bearer $BRIDGE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"telegram-agent","messages":[{"role":"user","content":"ping"}]}' \
  http://127.0.0.1:8787/v1/chat/completions
```

---

## 12. Repo layout (target after implementation)

```text
completion-telegram-bridge/
├── README.md
├── docs/
│   └── SPEC.md              # this document
├── pyproject.toml / requirements.txt
├── src/completion_telegram_bridge/
│   ├── __init__.py
│   ├── main.py              # app entry
│   ├── api.py               # HTTP routes
│   ├── openai_types.py      # request/response shaping
│   ├── telegram_bridge.py   # send + wait logic
│   └── config.py
├── scripts/
│   └── login_telegram.py    # one-time session bootstrap
└── tests/
```

Exact layout can shift in the implementation plan; listed here for orientation.

---

## 13. Open questions (need your input)

Please mark answers when reviewing this draft.

1. **Language / Telegram library** — OK to lock Python + Telethon for v1?
2. **URL form you will put in Even Hub** — base `/v1` vs full path to `/v1/chat/completions`? (We can support both either way.)
3. **Timeout UX** — prefer hard `504` vs friendly `200` assistant message on timeout for glasses?
4. **Multi-bubble aggregation** — is quiet-period concatenation correct for your agent, or always “first message only”?
5. **Correlation prefix** — do you want `[g2-…]` prefixes on outbound messages, or clean text only?
6. **Agent target** — private DM with one bot only for v1 (assumed yes)?
7. **Reply content** — return raw Telegram text as-is, or apply max length / strip markdown for HUD?
8. **Session storage** — session file path vs string env var preference?
9. **Deployment host** — always-on machine on LAN/Tailscale, or laptop-only when home?
10. **Any Even Hub build quirks you already know** (required headers, exact path, streaming)?

---

## 14. Acceptance criteria (v1 done)

- [ ] Service starts with valid env + Telegram session and logs ready state.
- [ ] `GET /healthz` returns ok without auth.
- [ ] Invalid token → `401`.
- [ ] Valid `POST /v1/chat/completions` with a user message delivers that text to the configured Telegram agent from the user account.
- [ ] When the agent replies with text, the HTTP response is `200` with OpenAI chat.completion JSON containing that text.
- [ ] Multi-bubble agent replies within the quiet window are concatenated.
- [ ] If the agent does not reply in time → `504` (unless timeout-as-assistant is enabled later).
- [ ] Second concurrent completion while one is waiting → `429`.
- [ ] Documented Even Hub configuration steps in README work for the operator’s glasses.
- [ ] Secrets and session files are not committed; example env is provided as `.env.example`.

---

## 15. Out of scope follow-ups (post-v1)

- Streaming completions if G2 needs them.
- Correlation-aware concurrent requests.
- Deferred long tasks (immediate ACK on glasses + full answer only on Telegram).
- Reply sanitisation / HUD formatter.
- Docker compose + systemd unit templates.
- Official Even protocol updates once documented by Even Realities.

---

## 16. Decision log (v0.1 defaults)

| Topic | Default decision | Revisit if |
|-------|------------------|------------|
| API shape | OpenAI chat.completions non-streaming | G2 requires streaming |
| Dual paths | `/` and `/v1/chat/completions` | Confirmed single path from your app build |
| Telegram identity | User account (MTProto) | Agent offers a better channel |
| Reply selection | First bot text after send + quiet aggregation | Agent always single-message |
| Concurrency | Single-flight | Correlation markers added |
| Timeout HTTP | `504` | Glasses need soft failures |
| Auth | Bearer shared secret | — |

---

*End of v0.1 draft. Comment with corrections and answers to §13; next step is an implementation plan, then code.*
