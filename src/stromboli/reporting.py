"""Context-scoped stage reporting — live "what's it doing right now".

The build runs deep inside the call chain (consumer → worker → pipeline →
engine), and the only place that knows the ledger run id is the consumer. Rather
than thread a reporter through every signature, the consumer installs one for the
duration of a build via :func:`using`, and the engine calls :func:`report_stage`
as each step completes. Because a build runs synchronously in the consumer
thread, the :class:`~contextvars.ContextVar` is visible the whole way down.

When no reporter is installed (e.g. the engine under test, or the Ralph loop)
:func:`report_stage` is a silent no-op, so callers never need to check.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from contextvars import ContextVar

logger = logging.getLogger(__name__)

#: Receives a short human stage string (e.g. "verifier (AC-002): not met").
StageReporter = Callable[[str], None]

_reporter: ContextVar[StageReporter | None] = ContextVar(
    "stromboli_stage_reporter", default=None
)


def report_stage(stage: str) -> None:
    """Report the current stage to the installed reporter, if any (best-effort)."""
    reporter = _reporter.get()
    if reporter is None:
        return
    try:
        reporter(stage)
    except Exception:  # noqa: BLE001 - reporting must never break a build
        logger.warning("Stage reporter raised; ignoring.", exc_info=True)


@contextlib.contextmanager
def using(reporter: StageReporter) -> Iterator[None]:
    """Install ``reporter`` for the duration of the ``with`` block."""
    token = _reporter.set(reporter)
    try:
        yield
    finally:
        _reporter.reset(token)


__all__ = [
    "StageReporter",
    "report_stage",
    "using",
]
