"""Run the HTTP bridge with uvicorn."""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn

from completion_telegram_bridge.api import create_app
from completion_telegram_bridge.config import BridgeConfig, default_config_dir, load_config
from completion_telegram_bridge.logging_setup import setup_logging
from completion_telegram_bridge.telegram_bridge import TelegramBridge

logger = logging.getLogger(__name__)


def run_server(
    config: BridgeConfig | None = None,
    *,
    debug: bool = False,
    verbose: bool = False,
    log_file: str | Path | None = None,
) -> None:
    cfg = config or load_config()
    problems = cfg.validate_for_serve()
    if problems:
        raise SystemExit("Cannot start:\n  - " + "\n  - ".join(problems))

    # Default log file under config dir when debug and no path given
    resolved_log = log_file
    if resolved_log is None and debug:
        resolved_log = default_config_dir() / "bridge.debug.log"

    setup_logging(debug=debug, verbose=verbose or debug, log_file=resolved_log)

    session = cfg.session_path()
    session.parent.mkdir(parents=True, exist_ok=True)

    bridge = TelegramBridge(cfg)
    app = create_app(cfg, bridge)

    uv_level = "debug" if debug else "info"
    logger.info(
        "Starting server on http://%s:%s debug=%s log_file=%s",
        cfg.host,
        cfg.port,
        debug,
        resolved_log or "(stderr only)",
    )
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=uv_level, access_log=debug)
