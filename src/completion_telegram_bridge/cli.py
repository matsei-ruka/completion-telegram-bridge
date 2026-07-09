"""Command-line interface: setup, agent selection, serve."""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from completion_telegram_bridge import __version__
from completion_telegram_bridge.config import (
    SETTABLE_KEYS,
    BridgeConfig,
    coerce_config_value,
    config_path,
    default_config_dir,
    ensure_config_dir,
    load_config,
    save_config,
    update_config,
)
from completion_telegram_bridge.logging_setup import setup_logging

app = typer.Typer(
    name="ctb",
    help="completion-telegram-bridge — Even Realities G2 ↔ Telegram agent",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# Set by global callback; serve may escalate to --debug
_CLI_VERBOSE = False


def _session_base(cfg: BridgeConfig) -> str:
    """Telethon session path without .session suffix."""
    path = cfg.session_path()
    s = str(path)
    if s.endswith(".session"):
        return s[: -len(".session")]
    return s


def _session_file_exists(cfg: BridgeConfig) -> bool:
    base = _session_base(cfg)
    return Path(base + ".session").exists()


@app.callback()
def main_callback(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Verbose logs (DEBUG level, previews only — use serve --debug for full bodies)",
    ),
) -> None:
    global _CLI_VERBOSE
    _CLI_VERBOSE = verbose
    setup_logging(verbose=verbose)


@app.command("version")
def version_cmd() -> None:
    """Print package version."""
    console.print(__version__)


@app.command("status")
def status_cmd() -> None:
    """Show setup status (session, agent, token, listen address)."""
    cfg = load_config()
    conf = config_path()
    table = Table(title="completion-telegram-bridge status")
    table.add_column("Item")
    table.add_column("Value")
    table.add_row("config dir", str(default_config_dir()))
    table.add_row("config file", str(conf) + (" (exists)" if conf.exists() else " (missing)"))
    table.add_row("version", __version__)
    table.add_row("listen", f"{cfg.host}:{cfg.port}")
    table.add_row("model_id", cfg.model_id)
    table.add_row("api_token", "set" if cfg.api_token else "[red]not set[/red]")
    table.add_row(
        "telegram api",
        "set" if (cfg.telegram_api_id and cfg.telegram_api_hash) else "[red]not set[/red]",
    )
    table.add_row(
        "session",
        "[green]present[/green]" if _session_file_exists(cfg) else "[red]missing (ctb login)[/red]",
    )
    if cfg.agent_id or cfg.agent_username:
        agent = cfg.agent_title or cfg.agent_username or str(cfg.agent_id)
        uname = f" @{cfg.agent_username}" if cfg.agent_username else ""
        table.add_row("agent", f"{agent}{uname} (id={cfg.agent_id})")
    else:
        table.add_row("agent", "[red]not selected (ctb select-agent)[/red]")
    table.add_row("reply_timeout_sec", str(cfg.reply_timeout_sec))
    console.print(table)
    problems = cfg.validate_for_serve()
    if not _session_file_exists(cfg):
        problems = [p for p in problems if "session" not in p.lower()]
        problems.append(f"Telegram session missing (run: ctb login)")
    if problems:
        console.print("\n[yellow]Not ready to serve:[/yellow]")
        for p in problems:
            console.print(f"  • {p}")
    else:
        console.print("\n[green]Ready:[/green] run [bold]ctb serve[/bold]")


@app.command("config")
def config_cmd(
    action: Optional[str] = typer.Argument(None, help="'set' to update a key, or omit to show"),
    key: Optional[str] = typer.Argument(None, help="Config key"),
    value: Optional[str] = typer.Argument(None, help="New value"),
) -> None:
    """Show config (secrets redacted) or set a key: ctb config set <key> <value>."""
    if action is None:
        cfg = load_config()
        console.print_json(data=cfg.redacted_dict())
        console.print(f"\n[dim]file: {config_path()}[/dim]")
        return
    if action != "set":
        console.print("[red]Usage:[/red] ctb config | ctb config set <key> <value>")
        raise typer.Exit(1)
    if not key or value is None:
        console.print("[red]Usage:[/red] ctb config set <key> <value>")
        console.print(f"Settable keys: {', '.join(sorted(SETTABLE_KEYS))}")
        raise typer.Exit(1)
    if key not in SETTABLE_KEYS:
        console.print(f"[red]Unknown key[/red] {key!r}. Settable: {', '.join(sorted(SETTABLE_KEYS))}")
        raise typer.Exit(1)
    coerced = coerce_config_value(key, value)
    update_config(**{key: coerced})
    console.print(f"[green]Updated[/green] {key}")


@app.command("set-api")
def set_api_cmd(
    api_id: Optional[int] = typer.Option(None, "--api-id", help="Telegram api_id from my.telegram.org"),
    api_hash: Optional[str] = typer.Option(None, "--api-hash", help="Telegram api_hash"),
) -> None:
    """Set Telegram application api_id and api_hash (from https://my.telegram.org)."""
    ensure_config_dir()
    if api_id is None:
        api_id = IntPrompt.ask("Telegram api_id")
    if not api_hash:
        api_hash = Prompt.ask("Telegram api_hash", password=True)
    update_config(telegram_api_id=api_id, telegram_api_hash=api_hash)
    console.print("[green]Saved[/green] Telegram API credentials")


@app.command("set-token")
def set_token_cmd(
    token: Optional[str] = typer.Option(None, "--token", "-t", help="Bearer token for Even Hub"),
    generate: bool = typer.Option(False, "--generate", "-g", help="Generate a random token"),
) -> None:
    """Set the bridge API token (same value as Even Hub Token field)."""
    ensure_config_dir()
    if generate:
        token = secrets.token_urlsafe(32)
        console.print(f"Generated token: [bold]{token}[/bold]")
    elif not token:
        token = Prompt.ask("API token (Even Hub Token)", password=False)
    if not token:
        console.print("[red]Empty token[/red]")
        raise typer.Exit(1)
    update_config(api_token=token)
    console.print("[green]Saved[/green] api_token")


@app.command("login")
def login_cmd(
    phone: Optional[str] = typer.Option(None, "--phone", help="Phone number with country code"),
) -> None:
    """Interactive Telegram user login (creates session file)."""
    cfg = load_config()
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        console.print("[red]Set API credentials first:[/red] ctb set-api")
        raise typer.Exit(1)
    ensure_config_dir()
    asyncio.run(_login_async(cfg, phone))


async def _login_async(cfg: BridgeConfig, phone: str | None) -> None:
    from telethon import TelegramClient

    session = _session_base(cfg)
    client = TelegramClient(session, cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        console.print(
            f"[green]Already logged in[/green] as "
            f"{getattr(me, 'username', None) or me.first_name} (id={me.id})"
        )
        await client.disconnect()
        return

    from telethon.errors import SessionPasswordNeededError

    phone = phone or Prompt.ask("Phone number (+countrycode…)")
    await client.send_code_request(phone)
    code = Prompt.ask("Code from Telegram")
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        password = Prompt.ask("2FA password", password=True)
        await client.sign_in(password=password)

    me = await client.get_me()
    console.print(
        f"[green]Logged in[/green] as "
        f"{getattr(me, 'username', None) or me.first_name} (id={me.id})"
    )
    console.print(f"Session: {session}.session")
    await client.disconnect()


@app.command("logout")
def logout_cmd(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete the local Telegram session file."""
    cfg = load_config()
    path = Path(_session_base(cfg) + ".session")
    journal = Path(str(path) + "-journal")
    if not path.exists():
        console.print("No session file found.")
        return
    if not yes and not Confirm.ask(f"Delete {path}?"):
        raise typer.Abort()
    path.unlink(missing_ok=True)
    journal.unlink(missing_ok=True)
    console.print("[green]Session removed[/green]")


@app.command("agents")
def agents_cmd(
    limit: int = typer.Option(200, "--limit", help="Max dialogs to scan"),
) -> None:
    """List bots you have dialogs with."""
    cfg = load_config()
    _require_session_ready(cfg)
    bots = asyncio.run(_list_bots(cfg, limit))
    if not bots:
        console.print("No bot dialogs found. Message your agent bot once from Telegram, then retry.")
        return
    table = Table(title="Bot dialogs")
    table.add_column("#", style="dim")
    table.add_column("Title")
    table.add_column("Username")
    table.add_column("ID")
    for i, b in enumerate(bots, 1):
        table.add_row(str(i), b.title, f"@{b.username}" if b.username else "—", str(b.id))
    console.print(table)
    if cfg.agent_id:
        console.print(f"\nCurrently selected agent id=[bold]{cfg.agent_id}[/bold]")


@app.command("select-agent")
def select_agent_cmd(
    username: Optional[str] = typer.Argument(
        None,
        help="Bot @username to select (optional; interactive if omitted)",
    ),
    limit: int = typer.Option(200, "--limit", help="Max dialogs to scan"),
) -> None:
    """Select which Telegram bot is the agent (list + pick, or pass @username)."""
    cfg = load_config()
    _require_session_ready(cfg)
    asyncio.run(_select_agent_async(cfg, username, limit))


async def _list_bots(cfg: BridgeConfig, limit: int):
    from completion_telegram_bridge.telegram_bridge import TelegramBridge

    bridge = TelegramBridge(cfg)
    await bridge.start()
    try:
        return await bridge.list_bot_dialogs(limit=limit)
    finally:
        await bridge.stop()


async def _select_agent_async(cfg: BridgeConfig, username: str | None, limit: int) -> None:
    from completion_telegram_bridge.telegram_bridge import BotDialog, TelegramBridge

    bridge = TelegramBridge(cfg)
    await bridge.start()
    try:
        if username:
            bot = await bridge.resolve_username(username)
            if not bot.is_bot:
                console.print(
                    f"[yellow]Warning:[/yellow] @{bot.username or username} does not look like a bot"
                )
            _save_agent(bot)
            return

        bots = await bridge.list_bot_dialogs(limit=limit)
        if not bots:
            console.print(
                "No bot dialogs found.\n"
                "Open Telegram, message your agent bot at least once, then run this again.\n"
                "Or: [bold]ctb select-agent @YourBotUsername[/bold]"
            )
            return

        table = Table(title="Select agent bot")
        table.add_column("#")
        table.add_column("Title")
        table.add_column("Username")
        table.add_column("ID")
        for i, b in enumerate(bots, 1):
            table.add_row(str(i), b.title, f"@{b.username}" if b.username else "—", str(b.id))
        console.print(table)
        console.print("Enter list number, @username, or numeric id. Empty cancels.")
        raw = Prompt.ask("Agent").strip()
        if not raw:
            console.print("Cancelled.")
            return

        selected: BotDialog | None = None
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(bots):
                selected = bots[n - 1]
            else:
                for b in bots:
                    if b.id == n:
                        selected = b
                        break
                if selected is None:
                    # resolve by id via Telethon
                    entity = await bridge.client.get_entity(n)
                    from telethon.tl.types import User

                    if isinstance(entity, User):
                        selected = BotDialog(
                            id=entity.id,
                            username=entity.username,
                            title=entity.first_name or str(entity.id),
                            is_bot=bool(entity.bot),
                        )
        else:
            selected = await bridge.resolve_username(raw)

        if selected is None:
            console.print("[red]Could not resolve selection[/red]")
            raise typer.Exit(1)

        _save_agent(selected)
    finally:
        await bridge.stop()


def _save_agent(bot) -> None:
    update_config(
        agent_id=bot.id,
        agent_username=bot.username,
        agent_title=bot.title,
    )
    console.print(
        f"[green]Selected agent[/green] {bot.title} "
        f"(@{bot.username or '—'}) id={bot.id}"
    )


def _require_session_ready(cfg: BridgeConfig) -> None:
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        console.print("[red]Set API credentials first:[/red] ctb set-api")
        raise typer.Exit(1)
    if not _session_file_exists(cfg):
        console.print("[red]Not logged in:[/red] ctb login")
        raise typer.Exit(1)


@app.command("serve")
def serve_cmd(
    host: Optional[str] = typer.Option(None, "--host", help="Bind host (default from config)"),
    port: Optional[int] = typer.Option(None, "--port", help="Bind port (default from config)"),
    debug: bool = typer.Option(
        False,
        "--debug",
        "-d",
        help="Debug mode: full request/Telegram bodies, DEBUG logs, write bridge.debug.log",
    ),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        help="Also write logs to this file (default in --debug: ~/.config/.../bridge.debug.log)",
    ),
) -> None:
    """Start the OpenAI-compatible HTTP bridge (bind localhost; put nginx in front).

    Logging:
      ctb serve              INFO to stderr (request summary, send/reply previews, errors)
      ctb -v serve           DEBUG without full message bodies
      ctb serve --debug      full bodies + file log under config dir
      ctb serve --log-file /var/log/ctb.log
    """
    cfg = load_config()
    if host is not None:
        cfg.host = host
    if port is not None:
        cfg.port = port
    from completion_telegram_bridge.server import run_server

    run_server(
        cfg,
        debug=debug,
        verbose=_CLI_VERBOSE,
        log_file=log_file,
    )


if __name__ == "__main__":
    app()
