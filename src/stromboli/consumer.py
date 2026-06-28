"""The build consumer — a real FIFO queue in front of the serial worker.

Previously a dispatch arriving while a build ran was rejected and lost. Here the
dispatch endpoint only *enqueues* (instant), and a single background consumer
thread pulls the ledger's oldest queued run and builds it, one at a time. So
ticking *Ready* on five tasks queues five builds — none dropped — and the queue
survives a restart because it lives in the ledger, not in memory.

The consumer wraps the existing :class:`~stromboli.worker.Worker` guard
(injected as ``process``): it runs the build, maps the
:class:`~stromboli.worker.DispatchOutcome` onto a terminal ledger state (built →
``done``; guard-declined → ``skipped``; exception → ``failed``), and stamps the
lifecycle so the status view and notifications have something to read.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from stromboli import reporting
from stromboli.ledger import RunLedger, RunRecord, RunState
from stromboli.worker import DispatchOutcome

logger = logging.getLogger(__name__)

#: A build entrypoint: given a task page id, run it and report the guard outcome.
ProcessFn = Callable[[str], DispatchOutcome]


class LifecycleListener(Protocol):
    """A sink notified as a run moves through its lifecycle.

    Implementations are best-effort and must not raise into the consumer (the
    consumer guards each call). The Notion acknowledgment is one listener; a
    Telegram notifier is the natural next one — both plug in here.
    """

    def queued(self, run: RunRecord, position: int) -> None: ...

    def building(self, run: RunRecord) -> None: ...

    def finished(self, run: RunRecord) -> None: ...


class NullListener:
    """The default no-op listener."""

    def queued(self, run: RunRecord, position: int) -> None: ...

    def building(self, run: RunRecord) -> None: ...

    def finished(self, run: RunRecord) -> None: ...


class CompositeListener:
    """Fans each lifecycle event out to several listeners, isolating failures.

    One listener raising (e.g. Telegram is down) must not stop the others (e.g.
    the Notion ack), so each child call is individually guarded.
    """

    def __init__(self, listeners: tuple[LifecycleListener, ...]) -> None:
        self._listeners = listeners

    def queued(self, run: RunRecord, position: int) -> None:
        self._each(lambda listener: listener.queued(run, position))

    def building(self, run: RunRecord) -> None:
        self._each(lambda listener: listener.building(run))

    def finished(self, run: RunRecord) -> None:
        self._each(lambda listener: listener.finished(run))

    def _each(self, call: Callable[[LifecycleListener], None]) -> None:
        for listener in self._listeners:
            try:
                call(listener)
            except Exception:  # noqa: BLE001 - one listener must not block the rest
                logger.warning("Lifecycle listener raised; continuing.", exc_info=True)

#: The coarse stage stamped while a build runs (fine stages are a follow-up).
STAGE_BUILDING = "building"

#: How long the idle consumer waits between empty polls, in seconds.
DEFAULT_POLL_INTERVAL = 0.5


def _final_state(outcome: DispatchOutcome) -> RunState:
    """Map a dispatch outcome onto the run's terminal ledger state."""
    return RunState.DONE if outcome.built else RunState.SKIPPED


class BuildConsumer:
    """Drains the ledger's queue serially in a background thread."""

    def __init__(
        self,
        ledger: RunLedger,
        process: ProcessFn,
        *,
        listener: LifecycleListener | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._ledger = ledger
        self._process = process
        self._listener = listener or NullListener()
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def ledger(self) -> RunLedger:
        """The run ledger this consumer drains (for status / position reads)."""
        return self._ledger

    def enqueue(
        self, page_id: str, *, task_name: str | None = None, engine: str | None = None
    ) -> RunRecord:
        """Record a dispatch as queued; the consumer will pick it up. Never drops."""
        run = self._ledger.enqueue(page_id, task_name=task_name, engine=engine)
        position = self._ledger.position(run.id)
        logger.info("Queued %s as run %d (position %d).", page_id, run.id, position)
        self._notify(lambda: self._listener.queued(run, position))
        return run

    def run_once(self) -> bool:
        """Claim and build the next queued run. Returns ``False`` if none waiting."""
        run = self._ledger.claim_next()
        if run is None:
            return False
        self._build(run)
        return True

    def _build(self, run: RunRecord) -> None:
        self._ledger.set_stage(run.id, STAGE_BUILDING)
        logger.info("Building run %d (%s).", run.id, run.page_id)
        self._notify(lambda: self._listener.building(run))
        try:
            # Let the engine report fine-grained stages straight to the ledger
            # for the duration of this build.
            with reporting.using(lambda stage: self._ledger.set_stage(run.id, stage)):
                outcome = self._process(run.page_id)
        except Exception as exc:  # noqa: BLE001 - one build must not kill the consumer
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("Run %d (%s) failed.", run.id, run.page_id)
            self._ledger.finish(run.id, RunState.FAILED, error=error)
        else:
            self._ledger.finish(run.id, _final_state(outcome), outcome=outcome.value)
            logger.info("Run %d (%s) finished: %s.", run.id, run.page_id, outcome.value)
        self._notify(lambda: self._listener.finished(self._ledger.get(run.id)))

    @staticmethod
    def _notify(action: Callable[[], None]) -> None:
        """Run a listener callback, swallowing failures so it can't break a build."""
        try:
            action()
        except Exception:  # noqa: BLE001 - a listener must never break the consumer
            logger.warning("Lifecycle listener raised; ignoring.", exc_info=True)

    # -- thread lifecycle ------------------------------------------------- #
    def run_forever(self) -> None:
        """Loop until :meth:`stop`, building queued runs and idling when empty."""
        logger.info("Build consumer started.")
        while not self._stop.is_set():
            try:
                did_work = self.run_once()
            except Exception:  # noqa: BLE001 - defensive: never let the loop die
                logger.exception("Consumer loop error; continuing.")
                did_work = False
            if not did_work:
                self._stop.wait(self._poll_interval)
        logger.info("Build consumer stopped.")

    def start(self) -> None:
        """Start the consumer in a daemon thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name="stromboli-consumer", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the consumer to stop and wait for the current build to drain."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


__all__ = [
    "DEFAULT_POLL_INTERVAL",
    "STAGE_BUILDING",
    "BuildConsumer",
    "CompositeListener",
    "LifecycleListener",
    "NullListener",
    "ProcessFn",
]
