# completion-telegram-bridge

Bridge an [Even Realities G2](https://www.evenrealities.com/) custom agent (OpenAI-compatible completion API) to a personal agent that lives on Telegram.

```
Even Realities G2  →  HTTPS (nginx)  →  this service  →  Telegram  →  your agent bot
                                              ←  wait for reply  ←
```

Every message sent to Telegram is prefixed with:

```text
[sent from Even Realities G2, answer fast and concise]

<your prompt>
```

## Requirements

- Python 3.11+
- A Linux (or macOS) server with a public IP (or any host nginx can reach)
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- Your personal Telegram account (user session — not the agent bot token)
- nginx + TLS certificate for public HTTPS (recommended)

**No Docker.** Install with pip, run a process, put nginx in front.

## Install

```bash
# on the server
python3 -m venv ~/venvs/ctb
source ~/venvs/ctb/bin/activate
pip install -U pip
pip install git+https://github.com/matsei-ruka/completion-telegram-bridge.git

# or from a clone
git clone https://github.com/matsei-ruka/completion-telegram-bridge.git
cd completion-telegram-bridge
pip install -e .
```

CLI entrypoint: **`ctb`** (also `python -m completion_telegram_bridge`).

Config and session live under:

```text
~/.config/completion-telegram-bridge/
  config.json
  telegram.session
```

Override directory with `CTB_CONFIG_DIR`.

## Setup (CLI)

```bash
# 1. Telegram app credentials (my.telegram.org)
ctb set-api

# 2. Log in with your user account (phone + code + optional 2FA)
ctb login

# 3. List bots you already chat with, then pick the agent
ctb agents
ctb select-agent
# or directly:
ctb select-agent @YourAgentBot

# 4. API token for Even Hub (generate a strong one)
ctb set-token --generate

# 5. Optional: bind address/port (default 127.0.0.1:8787)
ctb config set port 8787
ctb config set host 127.0.0.1

# 6. Check readiness
ctb status

# 7. Run
ctb serve
```

Other commands:

| Command | Purpose |
|---------|---------|
| `ctb config` | Show config (secrets redacted) |
| `ctb config set <key> <value>` | Set a scalar setting |
| `ctb set-token` | Set/replace Bearer token |
| `ctb logout` | Delete local Telegram session |
| `ctb version` | Package version |

## Even Hub (glasses) configuration

| Field | Value |
|-------|--------|
| **Name** | e.g. `My Telegram Agent` |
| **URL** | `https://bridge.example.com/v1` (or full `…/v1/chat/completions`) |
| **Token** | Same string as `ctb set-token` |

The service accepts:

- `POST /v1/chat/completions`
- `POST /` (alias)
- `GET /v1/models` (auth required)
- `GET /healthz` (no auth)

## nginx (HTTPS on 443)

Listen on localhost only for the app; terminate TLS in nginx.

```nginx
# /etc/nginx/sites-available/completion-telegram-bridge
server {
    listen 443 ssl http2;
    server_name bridge.example.com;

    ssl_certificate     /etc/letsencrypt/live/bridge.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bridge.example.com/privkey.pem;

    # Completions can wait on Telegram for a while
    proxy_read_timeout  120s;
    proxy_send_timeout  120s;

    location / {
        proxy_pass         http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Enable site, obtain cert (e.g. certbot), reload nginx.

### systemd (optional)

```ini
# /etc/systemd/system/ctb.service
[Unit]
Description=completion-telegram-bridge
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER
Environment=CTB_CONFIG_DIR=/home/YOUR_USER/.config/completion-telegram-bridge
ExecStart=/home/YOUR_USER/venvs/ctb/bin/ctb serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ctb
```

## Logging & debug mode

Logs go to **stderr** by default (journald captures them if you use systemd).

| Mode | Command | What you see |
|------|---------|----------------|
| Normal | `ctb serve` | Each HTTP request/response, auth failures, Telegram send/wait/reply **previews**, timeouts, errors |
| Verbose | `ctb -v serve` | DEBUG details (ignored TG messages, aggregation, etc.) without full message bodies |
| **Debug** | `ctb serve --debug` | Full request bodies, full Telegram outbound + agent replies, uvicorn access log, and a file log |
| Custom file | `ctb serve --log-file /var/log/ctb.log` | Same as current level, also append to that path |

With `--debug`, logs are also written to:

```text
~/.config/completion-telegram-bridge/bridge.debug.log
```

(unless you pass `--log-file`).

Examples:

```bash
# watch live
ctb serve --debug

# systemd / background: tail the debug file
tail -f ~/.config/completion-telegram-bridge/bridge.debug.log

# production-ish: INFO + persistent file
ctb serve --log-file /var/log/ctb/bridge.log
```

Each completion gets a short **request id** (e.g. `id=a1b2c3d4`) on every related log line so you can follow one glasses request end-to-end.

**Note:** debug mode logs full prompts and agent answers (personal content). Use only while troubleshooting; bearer tokens are redacted.

## Smoke test

```bash
curl -s http://127.0.0.1:8787/healthz

curl -s -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"telegram-agent","messages":[{"role":"user","content":"ping"}]}' \
  http://127.0.0.1:8787/v1/chat/completions
```

## Behaviour notes

- Uses your **Telegram user** session to DM the selected bot.
- Holds the HTTP request open until the bot replies (or timeout, default 45s).
- Multi-bubble bot replies are concatenated after a short quiet period.
- Only one completion at a time (`429` if busy).
- Streaming is not supported (`400`).

## Spec

See [`docs/SPEC.md`](docs/SPEC.md).

## License

MIT (see package metadata).
