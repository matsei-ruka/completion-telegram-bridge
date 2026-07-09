"""Run the HTTP bridge with uvicorn."""

from __future__ import annotations

import logging

import uvicorn

from completion_telegram_bridge.api import create_app
from completion_telegram_bridge.config import BridgeConfig, load_config
from completion_telegram_bridge.telegram_bridge import TelegramBridge

logger = logging.getLogger(__name__)


def run_server(config: BridgeConfig | None = None) -> None:
    cfg = config or load_config()
    problems = cfg.validate_for_serve()
    if problems:
        raise SystemExit("Cannot start:\n  - " + "\n  - ".join(problems))

    # Ensure session file parent exists
    session = cfg.session_path()
    session.parent.mkdir(parents=True, exist_ok=True)

    bridge = TelegramBridge(cfg)
    app = create_app(cfg, bridge)

    logger.info("Starting server on http://%s:%s", cfg.host, cfg.port)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
