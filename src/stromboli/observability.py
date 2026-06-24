"""Langfuse observability for a task build (US-013).

Each task build is a Langfuse **trace**; each Ralph iteration is a **span** on
that trace. Cost and token usage parsed from the CC output are recorded on the
spans and summed onto the trace, and circuit-breaker trips and failures are
tagged so a run that went wrong is findable.

The tracer is an injectable seam:

* :class:`NullTracer` — the no-op default, used everywhere Langfuse is not
  configured (and in every test).
* :class:`LangfuseTracer` — sends to Langfuse. It lazily imports the ``langfuse``
  package (an optional dependency) so the core install and the test suite never
  need it; a missing package or missing credentials degrades to the no-op tracer
  rather than crashing the build.

:func:`record_build_trace` is the orchestrator: given a finished
:class:`~stromboli.loop.LoopResult` it drives the tracer through start → one
span per iteration → totals → trip/failure tags → finish. It is pure control
flow over the tracer interface, so it is asserted against a recording fake.

Writing ``Cost`` / ``Tokens`` back to the Notion task is explicitly deferred to
phase 2 (Non-Goals); this module records them on the trace only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stromboli.engine.result import BuildResult
    from stromboli.notion import Task

logger = logging.getLogger(__name__)


class BuildTracer:
    """No-op tracer base. Subclasses send the calls to a backend.

    The methods are deliberately granular so :func:`record_build_trace` is the
    single place that knows how a build maps onto traces/spans/tags; a backend
    only implements the verbs.
    """

    def start(self, *, task_id: str, name: str) -> None:
        """Open the build trace."""

    def iteration(
        self,
        *,
        index: int,
        cost_usd: float | None,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Record one Ralph iteration as a span with its usage."""

    def record_totals(
        self, *, cost_usd: float, input_tokens: int, output_tokens: int
    ) -> None:
        """Record the build's summed cost / token usage on the trace."""

    def tag(self, *tags: str) -> None:
        """Tag the trace (e.g. a breaker trip or a failure)."""

    def finish(self, *, error: str | None = None) -> None:
        """Close the build trace, flushing if the backend needs it."""


class NullTracer(BuildTracer):
    """The default tracer: does nothing. Used when Langfuse is not configured."""


def record_build_trace(
    tracer: BuildTracer,
    task: Task,
    result: BuildResult,
    *,
    error: str | None = None,
) -> None:
    """Record a finished build onto ``tracer``: spans, usage, and tags.

    Consumes the engine-agnostic :class:`~stromboli.engine.result.BuildResult`
    (Ralph or graph) via its :meth:`usage_spans`. Never raises on a tracing
    problem — observability must not fail the build.
    """
    try:
        tracer.start(task_id=task.page_id, name=task.name)

        total_cost = 0.0
        total_in = 0
        total_out = 0
        for span in result.usage_spans():
            tracer.iteration(
                index=span.index,
                cost_usd=span.cost_usd,
                input_tokens=span.input_tokens,
                output_tokens=span.output_tokens,
            )
            total_cost += span.cost_usd or 0.0
            total_in += span.input_tokens
            total_out += span.output_tokens

        tracer.record_totals(
            cost_usd=total_cost, input_tokens=total_in, output_tokens=total_out
        )

        if result.tripped and result.trip is not None:
            tracer.tag("circuit-breaker", f"trip:{result.trip.reason.value}")
        if error is not None:
            tracer.tag("failure")

        tracer.finish(error=error)
    except Exception as exc:  # noqa: BLE001 - tracing must never fail the build
        logger.warning("Observability for %s failed: %s", task.page_id, exc)


class LangfuseTracer(BuildTracer):
    """Sends the build trace to Langfuse via the (lazily imported) SDK."""

    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        host: str,
    ) -> None:
        # Lazy import: ``langfuse`` is an optional dependency, so importing it
        # only here keeps the core install and the test suite free of it. The
        # client is held as ``Any`` because the Langfuse SDK surface varies
        # across major versions; every call is best-effort and guarded by the
        # try/except in :func:`record_build_trace`. This targets the v2 ``trace``
        # API — verify against the deployed SDK version when wiring it live.
        import importlib

        langfuse_mod = importlib.import_module("langfuse")
        self._client: Any = langfuse_mod.Langfuse(
            public_key=public_key, secret_key=secret_key, host=host
        )
        self._trace: Any = None

    def start(self, *, task_id: str, name: str) -> None:
        self._trace = self._client.trace(name=name, metadata={"task_id": task_id})

    def iteration(
        self,
        *,
        index: int,
        cost_usd: float | None,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        if self._trace is None:
            return
        self._trace.span(
            name=f"iteration-{index}",
            metadata={
                "cost_usd": cost_usd,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )

    def record_totals(
        self, *, cost_usd: float, input_tokens: int, output_tokens: int
    ) -> None:
        if self._trace is None:
            return
        self._trace.update(
            metadata={
                "total_cost_usd": cost_usd,
                "total_input_tokens": input_tokens,
                "total_output_tokens": output_tokens,
            }
        )

    def tag(self, *tags: str) -> None:
        if self._trace is not None:
            self._trace.update(tags=list(tags))

    def finish(self, *, error: str | None = None) -> None:
        if self._trace is not None and error is not None:
            self._trace.update(level="ERROR", status_message=error)
        self._client.flush()


def build_tracer(
    *,
    enabled: bool = True,
    public_key: str | None = None,
    secret_key: str | None = None,
    host: str | None = None,
) -> BuildTracer:
    """Construct the active tracer, falling back to :class:`NullTracer`.

    Returns a :class:`NullTracer` when disabled, when credentials are missing,
    or when the ``langfuse`` package is not installed — so observability is
    strictly best-effort and never blocks a build.
    """
    if not (enabled and public_key and secret_key and host):
        return NullTracer()
    try:
        return LangfuseTracer(
            public_key=public_key, secret_key=secret_key, host=host
        )
    except Exception as exc:  # noqa: BLE001 - degrade rather than crash
        logger.warning("Langfuse unavailable (%s); tracing disabled.", exc)
        return NullTracer()


__all__ = [
    "BuildTracer",
    "LangfuseTracer",
    "NullTracer",
    "build_tracer",
    "record_build_trace",
]
