"""Tests for the node-span tracer seam (PRD §8)."""

from __future__ import annotations

from stromboli.observability.tracing import (
    BuildTracer,
    NullTracer,
    Span,
    build_tracer,
    traced_node,
)


def test_null_tracer_is_inert() -> None:
    tracer = NullTracer()
    tracer.start(task_id="t1", name="task")
    span = tracer.node("spec")
    assert isinstance(span, Span)
    child = span.child("sdk-turn-1", metadata={"tokens": 10})
    assert isinstance(child, Span)
    span.update(foo="bar")
    span.end()
    tracer.tag("escalated")
    tracer.finish()


def test_traced_node_closes_span_even_on_error() -> None:
    ends: list[str] = []

    class RecordingSpan(Span):
        def end(self) -> None:
            ends.append("ended")

    class RecordingTracer(BuildTracer):
        def node(self, name: str, *, metadata: dict[str, object] | None = None) -> Span:
            return RecordingSpan()

    tracer = RecordingTracer()
    try:
        with traced_node(tracer, "coding"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert ends == ["ended"]


def test_build_tracer_falls_back_to_null_when_unconfigured() -> None:
    assert isinstance(build_tracer(public_key=None), NullTracer)
    assert isinstance(build_tracer(enabled=False, public_key="p", secret_key="s",
                                   host="h"), NullTracer)


def test_traced_node_exposes_current_span() -> None:
    from stromboli.observability.tracing import current_span

    class _RecordingSpan(Span):
        def __init__(self) -> None:
            self.children: list[str] = []

        def child(self, name: str, *, metadata: object = None) -> Span:
            self.children.append(name)
            return Span()

    class _Tracer(BuildTracer):
        def __init__(self) -> None:
            self.span = _RecordingSpan()

        def node(self, name: str, *, metadata: object = None) -> Span:
            return self.span

    tracer = _Tracer()
    # Outside any node the current span is inert (child() is a no-op Span).
    assert isinstance(current_span(), Span)
    with traced_node(tracer, "spec"):
        # Inside, lower layers (the gateway) can nest children under the node.
        current_span().child("llm-call")
    assert tracer.span.children == ["llm-call"]
    # The contextvar is reset on exit.
    assert current_span() is not tracer.span
