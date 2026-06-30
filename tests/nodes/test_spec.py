"""Tests for the Spec node (PRD §6.2)."""

from __future__ import annotations

from stromboli.nodes.spec import make_spec
from stromboli.state import Spec, StromboliState
from tests.nodes._fakes import FakeGateway


def _state(req: str = "add a verbose flag") -> StromboliState:
    return StromboliState(task_id="t", source="cli", raw_request=req)


def test_spec_stub_without_gateway() -> None:
    node = make_spec(None)
    out = node(_state())
    spec = out["spec"]
    assert isinstance(spec, Spec)
    assert spec.ambiguous is False
    assert out["status"] == "specced"


def test_spec_via_gateway() -> None:
    gw = FakeGateway(
        {"goal": "add --verbose", "acceptance_criteria": ["prints debug"],
         "affected_paths": ["cli.py"], "constraints": [], "ambiguous": False}
    )
    node = make_spec(gw, model="haiku")
    out = node(_state())
    spec = out["spec"]
    assert isinstance(spec, Spec)
    assert spec.goal == "add --verbose"
    assert gw.calls[0]["model"] == "haiku"


def test_spec_ambiguous_flag_passthrough() -> None:
    gw = FakeGateway({"goal": "?", "ambiguous": True})
    out = make_spec(gw, model="haiku")(_state("do something"))
    spec = out["spec"]
    assert isinstance(spec, Spec)
    assert spec.ambiguous is True


def test_spec_gateway_failure_flags_ambiguous() -> None:
    from stromboli.llm.gateway import GatewayError

    gw = FakeGateway(error=GatewayError("proxy down"))
    out = make_spec(gw, model="haiku")(_state())
    spec = out["spec"]
    assert isinstance(spec, Spec)
    assert spec.ambiguous is True


def test_spec_uses_retriever_context() -> None:
    gw = FakeGateway({"goal": "g", "ambiguous": False})
    out = make_spec(gw, model="m", retriever=lambda _q: ["prior lesson A"])(_state())
    assert "prior lesson A" in gw.calls[0]["user"]
    assert out["memory_refs"] == ["prior lesson A"]


def test_spec_injects_project_context() -> None:
    gw = FakeGateway({"goal": "g", "ambiguous": False})
    node = make_spec(
        gw, model="m",
        project_context=lambda _s: "Project conventions (from x):\nUse framework Z",
    )
    node(_state())
    assert "Use framework Z" in gw.calls[0]["user"]
