"""Langfuse observability — one trace per task across both surfaces (PRD §8).

Every graph node emits a Langfuse **span** under a single per-task **trace**
correlated by ``task_id``. The coding node nests the Agent SDK message stream
(per-turn tool calls + token usage) as **child** spans of its node span, and the
LiteLLM gateway nodes (spec / router / verifier / memory) span on the reasoning
surface — so a single Langfuse trace covers the whole task across SDK + gateway.

The tracer is an injectable seam:

* :class:`NullTracer` — the no-op default, used wherever Langfuse is not
  configured (and in every test). Its spans are inert.
* :class:`LangfuseTracer` — sends to Langfuse via the lazily-imported SDK; a
  missing package / credentials degrades to the no-op rather than crashing.

Spans are context managers:

    with tracer.node("spec", metadata={...}) as span:
        ...
        span.child("sdk-turn-1", metadata={"tokens": 1234})
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import TracebackType
from typing import Any

logger = logging.getLogger(__name__)

#: The node span currently open (set by :func:`traced_node`), so lower layers
#: (e.g. the LiteLLM gateway) can nest child spans — per-LLM-call token usage —
#: under the active node without threading a span through every signature.
_current_span: ContextVar[Span | None] = ContextVar(
    "stromboli_current_span", default=None
)


def current_span() -> Span:
    """The active node span, or an inert :class:`Span` when none is open."""
    return _current_span.get() or Span()


class Span:
    """A no-op span. Subclasses record onto a backend. Usable as a CM."""

    def child(self, name: str, *, metadata: dict[str, Any] | None = None) -> Span:
        """Open a nested child span (e.g. one Agent SDK turn). No-op by default."""
        return self

    def update(self, **metadata: Any) -> None:
        """Attach metadata to the span. No-op by default."""

    def end(self) -> None:
        """Close the span. No-op by default."""

    def __enter__(self) -> Span:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.end()


class BuildTracer:
    """No-op tracer base. Subclasses send the calls to a backend."""

    def start(self, *, task_id: str, name: str) -> None:
        """Open the per-task trace, correlated by ``task_id``."""

    def node(self, name: str, *, metadata: dict[str, Any] | None = None) -> Span:
        """Open a node span on the trace (e.g. ``spec``, ``coding``)."""
        return Span()

    def tag(self, *tags: str) -> None:
        """Tag the trace (e.g. ``escalated``, ``failure``)."""

    def finish(self, *, error: str | None = None) -> None:
        """Close the trace, flushing if the backend needs it."""


class NullTracer(BuildTracer):
    """The default tracer: does nothing. Used when Langfuse is not configured."""


@contextmanager
def traced_node(
    tracer: BuildTracer, name: str, *, metadata: dict[str, Any] | None = None
) -> Iterator[Span]:
    """Open a node span, guaranteeing it is closed — and never failing a run.

    Tracing must never break the build, so any backend error inside the span is
    logged and swallowed (the node's own exceptions still propagate).
    """
    span = tracer.node(name, metadata=metadata)
    token = _current_span.set(span)
    try:
        yield span
    finally:
        _current_span.reset(token)
        try:
            span.end()
        except Exception as exc:  # noqa: BLE001 - tracing must never fail a build
            logger.warning("Span %s end failed: %s", name, exc)


class _LangfuseSpan(Span):
    """A Langfuse observation wrapper (best-effort; never raises out).

    Targets the Langfuse v3/v4 OTEL-style API: observations are created with
    ``start_observation`` and nest children the same way.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def child(self, name: str, *, metadata: dict[str, Any] | None = None) -> Span:
        try:
            return _LangfuseSpan(
                self._handle.start_observation(
                    name=name, as_type="span", metadata=metadata or {}
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse child span failed: %s", exc)
            return Span()

    def update(self, **metadata: Any) -> None:
        # ``input`` / ``output`` are first-class observation fields in Langfuse
        # (rendered as the span's I/O panes); everything else is metadata.
        reserved = {
            key: metadata.pop(key) for key in ("input", "output") if key in metadata
        }
        try:
            if metadata:
                reserved["metadata"] = metadata
            self._handle.update(**reserved)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse span update failed: %s", exc)

    def end(self) -> None:
        try:
            self._handle.end()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse span end failed: %s", exc)


class LangfuseTracer(BuildTracer):
    """Sends the per-task trace to Langfuse via the (lazily imported) SDK.

    One root observation per task carries the ``task_id`` (so node observations
    nest under a single trace); every call is best-effort.
    """

    def __init__(self, *, public_key: str, secret_key: str, host: str) -> None:
        import importlib

        langfuse_mod = importlib.import_module("langfuse")
        self._client: Any = langfuse_mod.Langfuse(
            public_key=public_key, secret_key=secret_key, host=host
        )
        self._root: Any = None

    def start(self, *, task_id: str, name: str) -> None:
        try:
            self._root = self._client.start_observation(
                name=name, as_type="span", metadata={"task_id": task_id}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse trace start failed: %s", exc)
            self._root = None

    def node(self, name: str, *, metadata: dict[str, Any] | None = None) -> Span:
        parent = self._root if self._root is not None else self._client
        try:
            return _LangfuseSpan(
                parent.start_observation(
                    name=name, as_type="span", metadata=metadata or {}
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse node span failed: %s", exc)
            return Span()

    def tag(self, *tags: str) -> None:
        # v4 has no clean trace-tag API outside a current-observation context;
        # attach tags to the root observation's metadata, best-effort.
        if self._root is not None:
            try:
                self._root.update(metadata={"tags": list(tags)})
            except Exception as exc:  # noqa: BLE001
                logger.warning("Langfuse tag failed: %s", exc)

    def finish(self, *, error: str | None = None) -> None:
        try:
            if self._root is not None:
                if error is not None:
                    self._root.update(level="ERROR", status_message=error)
                self._root.end()
            self._client.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse finish failed: %s", exc)


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
    strictly best-effort and never blocks a run.
    """
    if not (enabled and public_key and secret_key and host):
        return NullTracer()
    try:
        tracer = LangfuseTracer(
            public_key=public_key, secret_key=secret_key, host=host
        )
        # Validate credentials up front: if they don't authorize, degrade to the
        # no-op now rather than emitting a 401 on every span export (PRD §8).
        if not tracer._client.auth_check():
            logger.warning("Langfuse credentials rejected; tracing disabled.")
            return NullTracer()
        return tracer
    except Exception as exc:  # noqa: BLE001 - degrade rather than crash
        logger.warning("Langfuse unavailable (%s); tracing disabled.", exc)
        return NullTracer()


__all__ = [
    "BuildTracer",
    "LangfuseTracer",
    "NullTracer",
    "Span",
    "build_tracer",
    "current_span",
    "traced_node",
]
