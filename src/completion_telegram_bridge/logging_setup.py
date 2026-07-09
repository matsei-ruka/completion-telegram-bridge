"""Central logging setup for CLI and server."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Shared flag: when True, log full prompt/reply bodies (debug mode).
DEBUG_BODIES = False

_CONFIGURED = False


def setup_logging(
    *,
    debug: bool = False,
    verbose: bool = False,
    log_file: str | Path | None = None,
) -> None:
    """Configure root logging once.

    Levels:
      - default: INFO (requests summary, telegram send/reply summary, errors)
      - verbose (-v): DEBUG for our package only
      - debug: DEBUG for our package + full message bodies + more uvicorn detail
    """
    global _CONFIGURED, DEBUG_BODIES

    want_debug = bool(debug or verbose)
    DEBUG_BODIES = bool(debug)

    level = logging.DEBUG if want_debug else logging.INFO
    fmt = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    # Reconfigure if already set (e.g. serve after -v callback)
    for h in list(root.handlers):
        root.removeHandler(h)

    root.setLevel(level)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(console)

    if log_file:
        path = Path(log_file).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        root.addHandler(fh)
        logging.getLogger(__name__).info("Logging to file: %s", path)

    # Quiet noisy libraries unless full debug
    logging.getLogger("telethon").setLevel(logging.WARNING if not debug else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO if want_debug else logging.WARNING)

    # Our package always follows configured level
    logging.getLogger("completion_telegram_bridge").setLevel(level)

    _CONFIGURED = True
    mode = "debug (full bodies)" if debug else ("verbose" if verbose else "info")
    logging.getLogger(__name__).debug("Logging configured mode=%s level=%s", mode, logging.getLevelName(level))


def preview_text(text: str, limit: int = 120) -> str:
    """One-line preview for INFO logs."""
    one = " ".join(text.split())
    if len(one) <= limit:
        return one
    return one[: limit - 1] + "…"


def redact_auth_header(value: str | None) -> str:
    if not value:
        return "(missing)"
    if value.lower().startswith("bearer "):
        token = value.split(" ", 1)[1].strip()
        if len(token) <= 8:
            return "Bearer ***"
        return f"Bearer {token[:4]}…{token[-2:]}"
    return "(non-bearer)"
