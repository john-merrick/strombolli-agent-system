"""Run the Stromboli worker: ``python -m stromboli``.

Loads settings from the environment / ``.env`` (failing fast on any missing
variable), builds the fully-wired app, and serves it with uvicorn bound to
``127.0.0.1:8000`` — the address the Cloudflare Tunnel points at (see README).
"""

from __future__ import annotations

import logging
import os

import uvicorn

from stromboli.app import create_stromboli_app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("STROMBOLI_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_stromboli_app()
    host = os.environ.get("STROMBOLI_HOST", "127.0.0.1")
    port = int(os.environ.get("STROMBOLI_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
