"""Tests for the worker entrypoint's logging configuration.

Logging defaults to stderr; ``STROMBOLI_LOG_FILE`` adds a file handler (keeping
the console one) so a crashed build's traceback persists beyond the session.
"""

from __future__ import annotations

import logging
from pathlib import Path

from stromboli.__main__ import build_log_handlers


def test_default_handlers_are_stderr_only() -> None:
    handlers = build_log_handlers(None)
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert not isinstance(handlers[0], logging.FileHandler)


def test_log_file_adds_a_file_handler_keeping_the_console(tmp_path: Path) -> None:
    log_path = tmp_path / "stromboli.log"
    handlers = build_log_handlers(str(log_path))
    try:
        assert len(handlers) == 2
        assert any(isinstance(h, logging.StreamHandler) for h in handlers)
        file_handlers = [h for h in handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1
        assert Path(file_handlers[0].baseFilename) == log_path
    finally:
        for handler in handlers:
            handler.close()


def test_file_handler_actually_writes(tmp_path: Path) -> None:
    log_path = tmp_path / "out.log"
    handlers = build_log_handlers(str(log_path))
    logger = logging.getLogger("stromboli.test-main")
    logger.propagate = False
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    try:
        logger.error("boom happened")
        for handler in handlers:
            handler.flush()
        assert "boom happened" in log_path.read_text(encoding="utf-8")
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
