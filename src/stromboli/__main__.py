"""Run the Stromboli worker: ``python -m stromboli``.

Loads settings from the environment / ``.env`` (failing fast on any missing
variable), builds the fully-wired app, and serves it with uvicorn bound to
``127.0.0.1:8000`` — the address the Cloudflare Tunnel points at (see README).

Logging goes to stderr by default; set ``STROMBOLI_LOG_FILE`` to *also* persist
it to a file (the console handler is always kept) so a crashed build's traceback
survives the session.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from stromboli.app import create_stromboli_app

#: The log line format shared by every handler.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def build_log_handlers(log_file: str | None) -> list[logging.Handler]:
    """The logging handlers: always stderr; plus a file when ``log_file`` is set."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    return handlers


def configure_logging() -> None:
    """Configure root logging from ``STROMBOLI_LOG_LEVEL`` / ``STROMBOLI_LOG_FILE``."""
    logging.basicConfig(
        level=os.environ.get("STROMBOLI_LOG_LEVEL", "INFO"),
        format=LOG_FORMAT,
        handlers=build_log_handlers(os.environ.get("STROMBOLI_LOG_FILE")),
    )


def main() -> None:
    configure_logging()
    app = create_stromboli_app()
    host = os.environ.get("STROMBOLI_HOST", "127.0.0.1")
    port = int(os.environ.get("STROMBOLI_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
